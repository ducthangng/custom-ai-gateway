"""The 5 release checks. Each one takes a Scenario and returns a CheckResult;
release_check.run takes care of caching, comparing with approved results and writing files.

They run cheapest first, and the first failure stops the run:
  config_is_valid (<1 s) -> api_unchanged (~1 s) -> routing_unchanged (<1 s) -> cache_works (~1 s)
  -> speed_under_load (LOAD_SECONDS, default 30 s, starts the real gateway with uvicorn)
"""

import asyncio
import contextlib
import hashlib
import json
import os
import random
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROUTER = "gateway.plugins.rules_router:RulesRouter"
REDIS_CACHE = "gateway.plugins.redis_cache:RedisCache"
OVERRIDABLE_PARAMS = {"temperature", "top_p", "max_tokens", "level"}


@dataclass
class CheckResult:
    """What one check found: numbers (metrics), problems (failures) and a log for humans."""
    metrics: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)     # [{id, message, evidence, line}]
    evidence: dict = field(default_factory=dict)     # metric -> one line that explains the number
    lines: list = field(default_factory=list)

    def log(self, msg: str) -> int:
        """Add one line to the log and return its line number."""
        self.lines.append(msg)
        return len(self.lines)

    def fail(self, fid: str, message: str) -> None:
        """Record a problem. fid must stay the same across runs, so known problems can be recognised."""
        line = self.log(f"FAIL {message}")
        self.failures.append({"id": fid, "message": message, "evidence": f"FAIL {message}", "line": line})


@dataclass
class Scenario:
    """What is being tested: which routes.yaml, which env overrides, and where the approved results are."""
    routes_path: str
    env: dict                    # scenario env overrides, applied to child processes and lookups
    approved_dir: Path
    catalog_path: str = "catalog.yaml"

    def getenv(self, key: str, default: str) -> str:
        """Read an env var, letting the scenario's own value win."""
        return self.env.get(key, os.getenv(key, default))


def _short_hash(text: str) -> str:
    """10-character hash, used to build a stable failure id from a long text."""
    return hashlib.sha256(text.encode()).hexdigest()[:10]


@contextlib.contextmanager
def _temporary_env(**kv):
    """Set env vars only inside a `with` block (plugins read their settings from env)."""
    old = {k: os.environ.get(k) for k in kv}
    os.environ.update({k: str(v) for k, v in kv.items()})
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _build_test_gateway(scenario: Scenario, db_dir: str, cache_class: str | None = None, **settings):
    """A gateway app running in this process: SQLite, a mock model with no delay, and the JJ router
    using the scenario's routes.yaml."""
    from gateway.config import Settings
    from gateway.main import create_app
    from gateway.providers.mock_provider import MockProvider

    MockProvider.delay_s = 0
    s = Settings(database_url=f"sqlite+aiosqlite:///{db_dir}/q.db", catalog_path=scenario.catalog_path,
                 static_dir=f"{db_dir}/no-ui", router_class=ROUTER, cache_class=cache_class, **settings)
    return create_app(s)


def _send_chat(client, message: str, routing: str = "auto") -> list[tuple[str, dict]]:
    """POST one chat and return its stream as a list of (event name, data)."""
    r = client.post("/api/chat", json={"message": message, "settings": {
        "provider": "mock", "model": "echo", "routing": routing}})
    if r.status_code != 200:
        return [("http", {"status": r.status_code, "body": r.text[:200]})]
    out = []
    for block in r.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        out.append((lines.get("event", "?"), json.loads(lines.get("data", "{}"))))
    return out


# ---------------------------------------------------------------- 1. config_is_valid
def config_is_valid(scenario: Scenario) -> CheckResult:
    """Read routes.yaml and catch mistakes before anything runs: bad YAML, models that are not in
    catalog.yaml, keywords that are not text, regex patterns that do not compile."""
    from gateway.catalog import load_catalog

    res = CheckResult()
    try:
        cfg = yaml.safe_load(Path(scenario.routes_path).read_text())
    except yaml.YAMLError as e:
        res.fail("yaml", f"routes.yaml is not valid YAML: {e}")
        return res
    catalog = load_catalog(scenario.catalog_path)
    if "routes" not in cfg:
        res.fail("missing:routes", "top-level key 'routes' missing")
    if not isinstance(cfg.get("min_score", 1), int) or cfg.get("min_score", 1) < 1:
        res.fail("min_score", f"min_score must be an int >= 1, got {cfg.get('min_score')!r}")
    names = set()
    for r in cfg.get("routes", []):
        name = r.get("name", "?")
        if name in names:
            res.fail(f"dup-route:{name}", f"route '{name}' defined twice")
        names.add(name)
        tgt = r.get("target") or {}
        cands = tgt.get("candidates") or []
        if not cands:
            res.fail(f"target:{name}", f"route '{name}': target.candidates is empty")
        for c in cands:
            provider, _, model = str(c).partition("/")
            if catalog.get(provider, model) is None:
                res.fail(f"candidate:{name}:{c}", f"route '{name}': candidate {c!r} is not in catalog.yaml")
        for k in sorted(set(tgt.get("params") or {}) - OVERRIDABLE_PARAMS):
            res.fail(f"param:{name}:{k}", f"route '{name}': param {k!r} is ignored by the gateway")
        if not (r.get("keywords") or r.get("patterns")):
            res.fail(f"norules:{name}", f"route '{name}' has no keywords or patterns")
        for kw in r.get("keywords") or []:
            if not isinstance(kw, str):
                res.fail(f"kwtype:{name}:{kw!r}", f"route '{name}': keyword {kw!r} is not a string (quote it)")
        for p in r.get("patterns") or []:
            try:
                re.compile(p)
            except (re.error, TypeError) as e:
                res.fail(f"regex:{name}:{_short_hash(str(p))}",
                         f"route '{name}': pattern {p!r} does not compile: {e}")
    res.metrics = {"routes": len(names), "errors": len(res.failures)}
    res.log(f"checked {len(names)} routes against {scenario.catalog_path}, {len(res.failures)} errors")
    return res


# ---------------------------------------------------------------- 2. api_unchanged
def _api_shape(openapi: dict) -> dict:
    """Boil an OpenAPI spec down to what clients depend on: endpoints, fields, required fields."""
    schemas = openapi.get("components", {}).get("schemas", {})
    return {"paths": sorted(f"{m.upper()} {p}" for p, ops in openapi["paths"].items() for m in ops),
            "schemas": {n: sorted(s.get("properties", {})) for n, s in schemas.items()},
            "required": {n: sorted(s.get("required", [])) for n, s in schemas.items()}}


def api_unchanged(scenario: Scenario) -> CheckResult:
    """Make sure the API did not break for the web UI: no endpoint or field removed, no new required
    field, and a real chat still streams meta -> text -> done."""
    from fastapi.testclient import TestClient

    res = CheckResult()
    with tempfile.TemporaryDirectory() as tmp, _temporary_env(GATEWAY_ROUTES_PATH=scenario.routes_path):
        app = _build_test_gateway(scenario, tmp, cache_class="gateway.plugins.cache:MemoryCache")
        now = _api_shape(app.openapi())
        was = _api_shape(json.loads((scenario.approved_dir / "openapi.json").read_text()))
        for p in sorted(set(was["paths"]) - set(now["paths"])):
            res.fail(f"path:{p}", f"endpoint removed: {p}")
        for name, props in was["schemas"].items():
            for prop in sorted(set(props) - set(now["schemas"].get(name, []))):
                res.fail(f"prop:{name}.{prop}", f"schema {name}: property '{prop}' removed")
        for name, req in now["required"].items():
            for prop in sorted(set(req) - set(was["required"].get(name, []))):
                res.fail(f"req:{name}.{prop}", f"schema {name}: '{prop}' became required (breaks the UI)")

        with TestClient(app) as c:
            ev = _send_chat(c, "python TypeError in my script")
            names = [e for e, _ in ev]
            model = next((d for e, d in ev if e == "model"), {})
            done = ev[-1][1] if names and names[-1] == "done" else {}
            cat = c.get("/api/catalog")
            checks = {
                "healthz 200": c.get("/api/healthz").status_code == 200,
                "catalog: router enabled": cat.status_code == 200 and cat.json().get("router_enabled") is True,
                "SSE starts with meta": names[:1] == ["meta"],
                "SSE ends with done, status ok": done.get("status") == "ok",
                "router decided the model": model.get("routed_by") == "router",
                "answer text streamed": any(e == "text" and d.get("text") for e, d in ev),
                "empty message -> 422": c.post("/api/chat", json={"message": "", "settings": {
                    "provider": "mock", "model": "echo"}}).status_code == 422,
            }
        for k, ok in checks.items():
            if ok:
                res.log(f"ok   live: {k}")
            else:
                res.fail(f"live:{k}", f"live call: {k}")
    res.metrics = {"paths": len(now["paths"]), "breaking_changes": len(res.failures)}
    res.log(f"{len(now['paths'])} paths, {len(res.failures)} breaking changes")
    return res


# ---------------------------------------------------------------- 3. routing_unchanged
def routing_unchanged(scenario: Scenario) -> CheckResult:
    """Ask JJ to route 100 fixed prompts and compare with the approved decisions.
    A prompt that now goes to the wrong route is a failure; one that got fixed is just logged."""
    from gateway.routing import RuleClassifier, load_routes

    res = CheckResult()
    clf = RuleClassifier(load_routes(scenario.routes_path))
    prompts = []
    for f in ("evals/dev.jsonl", "evals/test.jsonl"):
        with open(f) as fh:
            prompts += [json.loads(line) for line in fh if line.strip()]
    approved = json.loads((scenario.approved_dir / "routing_decisions.json").read_text())
    correct = changed = improved = 0
    decisions = {}
    for d in prompts:
        pred = clf.classify(d["prompt"]).route
        decisions[d["prompt"]] = pred
        correct += pred == d["label"]
        was = approved.get(d["prompt"])
        if was is not None and was != pred:
            changed += 1
            if pred == d["label"]:
                improved += 1
                res.log(f"improved: {d['prompt']!r} {was} -> {pred}")
            else:
                res.fail(f"route:{_short_hash(d['prompt'])}",
                         f"{d['prompt']!r}: was {was}, now {pred} (label {d['label']})")
    res.metrics = {"accuracy": round(correct / len(prompts), 4), "changed": changed, "improved": improved}
    res.evidence["accuracy"] = f"accuracy {res.metrics['accuracy']:.1%} on {len(prompts)} fixed prompts"
    res.log(res.evidence["accuracy"] + f", {changed} decisions changed vs approved ({improved} improved)")
    res.decisions = decisions                        # saved by --approve
    return res


# ---------------------------------------------------------------- 4. cache_works
def cache_works(scenario: Scenario) -> CheckResult:
    """Send the same prompts twice: the second time must come from Redis without calling the model.
    Then make Redis unreachable: chats must still work, just without the cache."""
    from unittest import mock

    from fakeredis import FakeAsyncRedis
    from fastapi.testclient import TestClient

    res = CheckResult()
    with open("evals/dev.jsonl") as fh:
        prompts = [json.loads(line)["prompt"] for line in fh if line.strip()][:20]
    shared = FakeAsyncRedis(decode_responses=True)
    with (tempfile.TemporaryDirectory() as tmp, _temporary_env(GATEWAY_ROUTES_PATH=scenario.routes_path),
          mock.patch("gateway.plugins.redis_cache.aioredis.Redis.from_url", lambda *a, **k: shared),
          TestClient(_build_test_gateway(scenario, tmp, cache_class=REDIS_CACHE, cache_timeout_s=1.0)) as c):
        first = [_send_chat(c, p)[-1][1] for p in prompts]
        second = [_send_chat(c, p)[-1][1] for p in prompts]
        other_mode = [_send_chat(c, p, routing="manual")[-1][1] for p in prompts[:5]]
    for p, d in zip(prompts, second, strict=True):
        if d.get("cache_status") != "hit":
            res.fail(f"miss:{_short_hash(p)}",
                     f"repeat of {p!r} was not a cache hit (cache_status={d.get('cache_status')})")
        elif d.get("output_tokens"):
            res.fail(f"tokens:{_short_hash(p)}",
                     f"cache hit for {p!r} still reported {d['output_tokens']} output tokens")
    for p, d in zip(prompts[:5], other_mode, strict=True):
        if d.get("cache_status") == "hit":
            res.fail(f"mode:{_short_hash(p)}", f"manual request for {p!r} reused the auto-mode entry")

    with (tempfile.TemporaryDirectory() as tmp,
          _temporary_env(GATEWAY_ROUTES_PATH=scenario.routes_path, GATEWAY_REDIS_URL="redis://127.0.0.1:1/0"),
          TestClient(_build_test_gateway(scenario, tmp, cache_class=REDIS_CACHE, cache_timeout_s=0.2)) as c):
        down = [_send_chat(c, p)[-1] for p in prompts[:5]]
    for (event, d), p in zip(down, prompts[:5], strict=True):
        if event != "done" or d.get("status") != "ok":
            res.fail(f"failopen:{_short_hash(p)}", f"Redis down: {p!r} ended with {event} {d}")

    n = len(prompts)
    res.metrics = {
        "first_pass_hit_rate": round(sum(d.get("cache_status") == "hit" for d in first) / n, 4),
        "repeat_hit_rate": round(sum(d.get("cache_status") == "hit" for d in second) / n, 4),
        "fail_open_ok_rate": round(sum(e == "done" and d.get("status") == "ok" for e, d in down) / len(down), 4),
    }
    res.evidence["repeat_hit_rate"] = f"{res.metrics['repeat_hit_rate']:.0%} of {n} repeated prompts served from cache"
    res.evidence["fail_open_ok_rate"] = f"{res.metrics['fail_open_ok_rate']:.0%} of chats ok with Redis down"
    res.log(f"{n} prompts x2: first pass {res.metrics['first_pass_hit_rate']:.0%} hits, "
            f"repeat {res.metrics['repeat_hit_rate']:.0%} hits; manual mode never reuses auto entries; "
            f"Redis down: {res.metrics['fail_open_ok_rate']:.0%} ok")
    return res


# ---------------------------------------------------------------- 5. speed_under_load
def _find_free_port() -> int:
    """Ask the OS for a TCP port nobody is using."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_up(url: str, timeout_s: float = 30) -> None:
    """Poll url until it answers 200, or give up after timeout_s."""
    import httpx
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up")


async def _send_steady_traffic(url: str, seconds: float, concurrency: int, rps: float,
                               prompts: list[str]) -> list[tuple[int, str, float, float]]:
    """Send chats at a fixed rate (rps) from `concurrency` workers, so latency measures the gateway
    and not how fast this machine can fire requests.
    Rows: (http status, last SSE event, ms until the last event, seconds since start)."""
    import httpx
    out: list[tuple[int, str, float, float]] = []
    rng = random.Random(42)
    start = time.perf_counter()
    stop = start + seconds
    interval = concurrency / rps

    async def worker(i: int):
        """One worker: send a chat, wait for its turn, repeat until time is up."""
        nxt = start + i * interval / concurrency
        async with httpx.AsyncClient(base_url=url, timeout=30) as c:
            while (now := time.perf_counter()) < stop:
                if nxt > now:
                    await asyncio.sleep(nxt - now)
                nxt += interval
                t0 = time.perf_counter()
                last, code = "", 0
                try:
                    async with c.stream("POST", "/api/chat", json={"message": rng.choice(prompts), "settings": {
                            "provider": "mock", "model": "echo", "routing": "auto"}}) as r:
                        code = r.status_code
                        async for line in r.aiter_lines():
                            if line.startswith("event: "):
                                last = line[7:]
                except httpx.HTTPError:
                    code = 0
                out.append((code, last, (time.perf_counter() - t0) * 1000, t0 - start))
    await asyncio.gather(*(worker(i) for i in range(concurrency)))
    return out


def speed_under_load(scenario: Scenario) -> CheckResult:
    """Start the real gateway (uvicorn + Redis) and send steady traffic for LOAD_SECONDS.
    Reports p50/p95 latency and error rate; slower than allowed or any errors -> fail."""
    res = CheckResult()
    seconds = float(scenario.getenv("LOAD_SECONDS", "30"))
    concurrency = int(scenario.getenv("LOAD_CONCURRENCY", "8"))
    rps = float(scenario.getenv("LOAD_RPS", "20"))
    warmup = float(scenario.getenv("LOAD_WARMUP_S", "3"))
    redis_url = scenario.getenv("GATEWAY_REDIS_URL", "redis://localhost:6379/0")
    port = _find_free_port()
    with tempfile.TemporaryDirectory() as tmp:
        db = scenario.getenv("RELEASE_CHECK_DATABASE_URL", f"sqlite+aiosqlite:///{tmp}/load.db")
        env = {**os.environ, **scenario.env, "GATEWAY_DATABASE_URL": db,
               "GATEWAY_ROUTES_PATH": scenario.routes_path, "GATEWAY_ROUTER_CLASS": ROUTER,
               "GATEWAY_CACHE_CLASS": REDIS_CACHE, "GATEWAY_REDIS_URL": redis_url,
               "GATEWAY_STATIC_DIR": f"{tmp}/no-ui",
               "GATEWAY_MOCK_DELAY_S": scenario.getenv("GATEWAY_MOCK_DELAY_S", "0.002")}
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "gateway.main:app", "--port", str(port),
                                 "--log-level", "warning"], env=env)
        try:
            _wait_until_up(f"http://127.0.0.1:{port}/api/healthz")
            import redis
            redis.Redis.from_url(redis_url).flushdb()
            with open("evals/dev.jsonl") as fh:
                prompts = [json.loads(line)["prompt"] for line in fh if line.strip()][:30]
            rows = asyncio.run(_send_steady_traffic(f"http://127.0.0.1:{port}", seconds + warmup,
                                                    concurrency, rps, prompts))
            rows = [r for r in rows if r[3] >= warmup]
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    ok = sorted(ms for code, last, ms, _ in rows if code == 200 and last == "done")
    q = lambda p: round(ok[min(len(ok) - 1, int(p * (len(ok) - 1)))], 2) if ok else None
    n = len(rows)
    res.metrics = {"requests": n, "rps": round(n / seconds, 1), "p50_ms": q(0.5), "p95_ms": q(0.95),
                   "mean_ms": round(statistics.fmean(ok), 2) if ok else None,
                   "error_rate": round((n - len(ok)) / max(1, n), 4)}
    for k in ("p95_ms", "error_rate"):
        res.evidence[k] = f"{k}={res.metrics[k]} ({n} chats, offered {rps:g} rps, {seconds:g}s)"
    if res.metrics["error_rate"] > 0:
        bad = [f"{code}/{last or '-'}" for code, last, _, _ in rows if not (code == 200 and last == "done")]
        res.log(f"non-ok results (http/last event): {sorted(set(bad))[:10]}")
    res.log(f"{n} chats in {seconds:g}s (after {warmup:g}s warm-up), offered {rps:g} rps by {concurrency} workers, "
            f"30 prompts (repeats hit the Redis cache), db={db.split(':')[0]}")
    res.log(json.dumps(res.metrics))
    return res


# The checks, in the order they run.
CHECKS = {"config_is_valid": config_is_valid, "api_unchanged": api_unchanged,
          "routing_unchanged": routing_unchanged, "cache_works": cache_works, "speed_under_load": speed_under_load}

# How much worse than approved a metric may get. Written into approved_results/<check>.json by --approve.
TOLERANCES = {
    "routing_unchanged": {"accuracy": {"better": "higher", "abs": 0.02}},
    "cache_works": {"repeat_hit_rate": {"better": "higher", "abs": 0.0},
                    "fail_open_ok_rate": {"better": "higher", "abs": 0.0}},
    # laptop vs CI pod timings differ: generous on latency, strict on correctness.
    "speed_under_load": {"p95_ms": {"better": "lower", "rel": 1.0, "abs": 50},
                         "error_rate": {"better": "lower", "abs": 0.01}},
}

# Files that decide each check's result. If none changed since a passing run, the check is skipped.
# (checks.py and compare.py are added to every check by release_check.run.fingerprint_inputs)
GATEWAY_CODE = ["gateway/*.py", "gateway/*/*.py"]
INPUT_FILES = {
    "config_is_valid": ["{routes}", "catalog.yaml"],
    "api_unchanged": [*GATEWAY_CODE, "catalog.yaml", "{approved}/openapi.json"],
    "routing_unchanged": ["{routes}", "gateway/routing/*.py", "evals/dev.jsonl", "evals/test.jsonl",
                          "{approved}/routing_decisions.json"],
    "cache_works": ["{routes}", *GATEWAY_CODE, "evals/dev.jsonl"],
    "speed_under_load": ["{routes}", *GATEWAY_CODE, "catalog.yaml"],
}

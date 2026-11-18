"""The five qualification stages. Each returns a Result; qualify.run handles caching, golden
comparison and writing <stage>.log / <stage>.json.

Cost order (fail fast): config_lint (<1 s) -> contract (~1 s) -> routing (<1 s) -> cache (~1 s)
-> load (LOAD_SECONDS, default 30 s, starts the real gateway with uvicorn).
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
class Result:
    metrics: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)     # [{id, message, evidence, line}]
    evidence: dict = field(default_factory=dict)     # metric -> evidence line
    lines: list = field(default_factory=list)

    def log(self, msg: str) -> int:
        self.lines.append(msg)
        return len(self.lines)

    def fail(self, fid: str, message: str) -> None:
        line = self.log(f"FAIL {message}")
        self.failures.append({"id": fid, "message": message, "evidence": f"FAIL {message}", "line": line})


@dataclass
class Ctx:
    routes_path: str
    env: dict                    # scenario env overrides, applied to child processes and lookups
    golden_dir: Path
    catalog_path: str = "catalog.yaml"

    def getenv(self, key: str, default: str) -> str:
        return self.env.get(key, os.getenv(key, default))


def _hid(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:10]


@contextlib.contextmanager
def _env(**kv):
    """Temporarily set env vars (plugins are built with zero args and read their config from env)."""
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


def _test_app(ctx: Ctx, db_dir: str, cache_class: str | None = None, **settings):
    """In-process gateway: SQLite, mock provider without delay, JJ router on the scenario's routes."""
    from gateway.config import Settings
    from gateway.main import create_app
    from gateway.providers.mock_provider import MockProvider

    MockProvider.delay_s = 0
    s = Settings(database_url=f"sqlite+aiosqlite:///{db_dir}/q.db", catalog_path=ctx.catalog_path,
                 static_dir=f"{db_dir}/no-ui", router_class=ROUTER, cache_class=cache_class, **settings)
    return create_app(s)


def _chat(client, message: str, routing: str = "auto") -> list[tuple[str, dict]]:
    r = client.post("/api/chat", json={"message": message, "settings": {
        "provider": "mock", "model": "echo", "routing": routing}})
    if r.status_code != 200:
        return [("http", {"status": r.status_code, "body": r.text[:200]})]
    out = []
    for block in r.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        out.append((lines.get("event", "?"), json.loads(lines.get("data", "{}"))))
    return out


# ---------------------------------------------------------------- 1. config_lint
def config_lint(ctx: Ctx) -> Result:
    from gateway.catalog import load_catalog

    res = Result()
    try:
        cfg = yaml.safe_load(Path(ctx.routes_path).read_text())
    except yaml.YAMLError as e:
        res.fail("yaml", f"routes.yaml is not valid YAML: {e}")
        return res
    catalog = load_catalog(ctx.catalog_path)
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
                res.fail(f"regex:{name}:{_hid(str(p))}", f"route '{name}': pattern {p!r} does not compile: {e}")
    res.metrics = {"routes": len(names), "errors": len(res.failures)}
    res.log(f"checked {len(names)} routes against {ctx.catalog_path}, {len(res.failures)} errors")
    return res


# ---------------------------------------------------------------- 2. contract
def _schema_fields(openapi: dict) -> dict:
    schemas = openapi.get("components", {}).get("schemas", {})
    return {"paths": sorted(f"{m.upper()} {p}" for p, ops in openapi["paths"].items() for m in ops),
            "schemas": {n: sorted(s.get("properties", {})) for n, s in schemas.items()},
            "required": {n: sorted(s.get("required", [])) for n, s in schemas.items()}}


def contract(ctx: Ctx) -> Result:
    from fastapi.testclient import TestClient

    res = Result()
    with tempfile.TemporaryDirectory() as tmp, _env(GATEWAY_ROUTES_PATH=ctx.routes_path):
        app = _test_app(ctx, tmp, cache_class="gateway.plugins.cache:MemoryCache")
        now = _schema_fields(app.openapi())
        was = _schema_fields(json.loads((ctx.golden_dir / "openapi.json").read_text()))
        for p in sorted(set(was["paths"]) - set(now["paths"])):
            res.fail(f"path:{p}", f"endpoint removed: {p}")
        for name, props in was["schemas"].items():
            for prop in sorted(set(props) - set(now["schemas"].get(name, []))):
                res.fail(f"prop:{name}.{prop}", f"schema {name}: property '{prop}' removed")
        for name, req in now["required"].items():
            for prop in sorted(set(req) - set(was["required"].get(name, []))):
                res.fail(f"req:{name}.{prop}", f"schema {name}: '{prop}' became required (breaks the UI)")

        with TestClient(app) as c:
            ev = _chat(c, "python TypeError in my script")
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


# ---------------------------------------------------------------- 3. routing
def routing(ctx: Ctx) -> Result:
    from gateway.routing import RuleClassifier, load_routes

    res = Result()
    clf = RuleClassifier(load_routes(ctx.routes_path))
    prompts = []
    for f in ("evals/dev.jsonl", "evals/test.jsonl"):
        with open(f) as fh:
            prompts += [json.loads(line) for line in fh if line.strip()]
    golden = json.loads((ctx.golden_dir / "routing_decisions.json").read_text())
    correct = changed = improved = 0
    decisions = {}
    for d in prompts:
        pred = clf.classify(d["prompt"]).route
        decisions[d["prompt"]] = pred
        correct += pred == d["label"]
        was = golden.get(d["prompt"])
        if was is not None and was != pred:
            changed += 1
            if pred == d["label"]:
                improved += 1
                res.log(f"improved: {d['prompt']!r} {was} -> {pred}")
            else:
                res.fail(f"route:{_hid(d['prompt'])}", f"{d['prompt']!r}: was {was}, now {pred} (label {d['label']})")
    res.metrics = {"accuracy": round(correct / len(prompts), 4), "changed": changed, "improved": improved}
    res.evidence["accuracy"] = f"accuracy {res.metrics['accuracy']:.1%} on {len(prompts)} fixed prompts"
    res.log(res.evidence["accuracy"] + f", {changed} decisions changed vs golden ({improved} improved)")
    res.decisions = decisions                        # used by --write-golden
    return res


# ---------------------------------------------------------------- 4. cache
def cache(ctx: Ctx) -> Result:
    """Exact cache through the real pipeline: a repeated request must be served from Redis with no
    model call, and a dead Redis must not break chats (fail-open)."""
    from unittest import mock

    from fakeredis import FakeAsyncRedis
    from fastapi.testclient import TestClient

    res = Result()
    with open("evals/dev.jsonl") as fh:
        prompts = [json.loads(line)["prompt"] for line in fh if line.strip()][:20]
    shared = FakeAsyncRedis(decode_responses=True)
    with (tempfile.TemporaryDirectory() as tmp, _env(GATEWAY_ROUTES_PATH=ctx.routes_path),
          mock.patch("gateway.plugins.redis_cache.aioredis.Redis.from_url", lambda *a, **k: shared),
          TestClient(_test_app(ctx, tmp, cache_class=REDIS_CACHE, cache_timeout_s=1.0)) as c):
        first = [_chat(c, p)[-1][1] for p in prompts]
        second = [_chat(c, p)[-1][1] for p in prompts]
        other_mode = [_chat(c, p, routing="manual")[-1][1] for p in prompts[:5]]
    for p, d in zip(prompts, second, strict=True):
        if d.get("cache_status") != "hit":
            res.fail(f"miss:{_hid(p)}", f"repeat of {p!r} was not a cache hit (cache_status={d.get('cache_status')})")
        elif d.get("output_tokens"):
            res.fail(f"tokens:{_hid(p)}", f"cache hit for {p!r} still reported {d['output_tokens']} output tokens")
    for p, d in zip(prompts[:5], other_mode, strict=True):
        if d.get("cache_status") == "hit":
            res.fail(f"mode:{_hid(p)}", f"manual request for {p!r} reused the auto-mode entry")

    with (tempfile.TemporaryDirectory() as tmp,
          _env(GATEWAY_ROUTES_PATH=ctx.routes_path, GATEWAY_REDIS_URL="redis://127.0.0.1:1/0"),
          TestClient(_test_app(ctx, tmp, cache_class=REDIS_CACHE, cache_timeout_s=0.2)) as c):
        down = [_chat(c, p)[-1] for p in prompts[:5]]
    for (event, d), p in zip(down, prompts[:5], strict=True):
        if event != "done" or d.get("status") != "ok":
            res.fail(f"failopen:{_hid(p)}", f"Redis down: {p!r} ended with {event} {d}")

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


# ---------------------------------------------------------------- 5. load
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, timeout_s: float = 30) -> None:
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


async def _drive(url: str, seconds: float, concurrency: int, rps: float,
                 prompts: list[str]) -> list[tuple[int, str, float, float]]:
    """Paced: each worker sends one chat every concurrency/rps seconds, so the offered load is fixed
    and latency measures the gateway, not how fast this laptop can fire requests.
    Rows: (http status, final SSE event, ms to the last event, t since start)."""
    import httpx
    out: list[tuple[int, str, float, float]] = []
    rng = random.Random(42)
    start = time.perf_counter()
    stop = start + seconds
    interval = concurrency / rps

    async def worker(i: int):
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


def load(ctx: Ctx) -> Result:
    res = Result()
    seconds = float(ctx.getenv("LOAD_SECONDS", "30"))
    concurrency = int(ctx.getenv("LOAD_CONCURRENCY", "8"))
    rps = float(ctx.getenv("LOAD_RPS", "20"))
    warmup = float(ctx.getenv("LOAD_WARMUP_S", "3"))
    redis_url = ctx.getenv("GATEWAY_REDIS_URL", "redis://localhost:6379/0")
    port = _free_port()
    with tempfile.TemporaryDirectory() as tmp:
        db = ctx.getenv("QUALIFY_DATABASE_URL", f"sqlite+aiosqlite:///{tmp}/load.db")
        env = {**os.environ, **ctx.env, "GATEWAY_DATABASE_URL": db, "GATEWAY_ROUTES_PATH": ctx.routes_path,
               "GATEWAY_ROUTER_CLASS": ROUTER, "GATEWAY_CACHE_CLASS": REDIS_CACHE, "GATEWAY_REDIS_URL": redis_url,
               "GATEWAY_STATIC_DIR": f"{tmp}/no-ui", "GATEWAY_MOCK_DELAY_S": ctx.getenv("GATEWAY_MOCK_DELAY_S", "0.002")}
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "gateway.main:app", "--port", str(port),
                                 "--log-level", "warning"], env=env)
        try:
            _wait(f"http://127.0.0.1:{port}/api/healthz")
            import redis
            redis.Redis.from_url(redis_url).flushdb()
            with open("evals/dev.jsonl") as fh:
                prompts = [json.loads(line)["prompt"] for line in fh if line.strip()][:30]
            rows = asyncio.run(_drive(f"http://127.0.0.1:{port}", seconds + warmup, concurrency, rps, prompts))
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


STAGES = {"config_lint": config_lint, "contract": contract, "routing": routing, "cache": cache, "load": load}

# Tolerances written into golden/<stage>.json at --write-golden time.
COMPARE = {
    "routing": {"accuracy": {"better": "higher", "abs": 0.02}},
    "cache": {"repeat_hit_rate": {"better": "higher", "abs": 0.0},
              "fail_open_ok_rate": {"better": "higher", "abs": 0.0}},
    # laptop vs CI pod timings differ: generous on latency, strict on correctness.
    "load": {"p95_ms": {"better": "lower", "rel": 1.0, "abs": 50}, "error_rate": {"better": "lower", "abs": 0.01}},
}

# Files whose content decides the result. Unchanged inputs -> reuse the cached passing result.
# (qualify/stages.py + qualify/compare.py are added to every stage by qualify.run.inputs_key)
CODE = ["gateway/*.py", "gateway/*/*.py"]
INPUTS = {
    "config_lint": ["{routes}", "catalog.yaml"],
    "contract": [*CODE, "catalog.yaml", "{golden}/openapi.json"],
    "routing": ["{routes}", "gateway/routing/*.py", "evals/dev.jsonl", "evals/test.jsonl",
                "{golden}/routing_decisions.json"],
    "cache": ["{routes}", *CODE, "evals/dev.jsonl"],
    "load": ["{routes}", *CODE, "catalog.yaml"],
}

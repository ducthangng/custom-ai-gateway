"""Release check: run the 2 checks and compare each one with its approved result.

  python -m release_check.run --all                                   # both checks, stop at the first failure
  python -m release_check.run --check routing_unchanged --scenario worse_routing
  python -m release_check.run --all --cache-dir .release-check-cache  # skip checks whose inputs did not change
  python -m release_check.run --all --approve                         # save this run as the new approved results

A scenario is a folder in release_check/practice_scenarios/<name>/ with a routes.yaml and/or a
scenario.env that replace the real ones. "clean" means: no scenario, test the code as it is.
Exit code 1 if a check got worse than its approved result.
"""

import argparse
import glob
import hashlib
import json
import logging
import shutil
import sys
import time
from pathlib import Path

from release_check.checks import CHECKS, INPUT_FILES, TOLERANCES, Scenario
from release_check.compare import find_what_got_worse

APPROVED = Path("release_check/approved_results")
SCENARIOS = Path("release_check/practice_scenarios")
# Env vars that change check results, so they are part of the fingerprint even when set outside a scenario.
# (The archived checks used LOAD_* here; the 2 current checks read none.)
ENV_KEYS: list[str] = []


def load_scenario(name: str, extra_env: list[str]) -> Scenario:
    """Build the Scenario to test: the real routes.yaml for "clean", or the files of
    practice_scenarios/<name>/ on top of it. extra_env holds KEY=VALUE overrides."""
    folder = SCENARIOS / name
    if name != "clean" and not folder.is_dir():
        raise SystemExit(f"unknown scenario {name!r}")
    routes = folder / "routes.yaml"
    env = {}
    if (folder / "scenario.env").exists():
        for line in (folder / "scenario.env").read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update(dict(e.split("=", 1) for e in extra_env))
    return Scenario(routes_path=str(routes if routes.exists() else "routes.yaml"), env=env, approved_dir=APPROVED)


def fingerprint_inputs(check: str, scenario: Scenario) -> str:
    """Hash every file and env var that can change this check's result.
    Same fingerprint as a previous passing run -> the check can be skipped."""
    h = hashlib.sha256()
    always = ["release_check/checks.py", "release_check/compare.py", f"{{approved}}/{check}.json"]
    for pattern in INPUT_FILES[check] + always:
        pattern = pattern.replace("{routes}", scenario.routes_path).replace("{approved}", str(scenario.approved_dir))
        for f in sorted(glob.glob(pattern)):
            h.update(f.encode() + b"\0" + Path(f).read_bytes())
    env = {k: scenario.getenv(k, "") for k in ENV_KEYS} | scenario.env
    h.update(json.dumps(env, sort_keys=True).encode())
    return h.hexdigest()[:16]


def run_check(check: str, scenario: Scenario, out: Path, cache_dir: Path | None, scenario_name: str) -> dict:
    """Run one check (or reuse its cached pass), compare it with the approved result,
    and write <check>.log + <check>.json into out. Returns the summary with status pass/fail."""
    out.mkdir(parents=True, exist_ok=True)
    key = fingerprint_inputs(check, scenario)
    cached = cache_dir / f"{check}-{key}.json" if cache_dir else None
    if cached and cached.exists():
        summary = {**json.loads(cached.read_text()), "skipped": "cache", "scenario": scenario_name}
        (out / f"{check}.log").write_text(f"SKIP {check}: inputs unchanged (key {key}), reusing passing result\n")
        (out / f"{check}.json").write_text(json.dumps(summary, indent=2))
        print(f"[{check}] skipped: inputs unchanged (key {key})")
        return summary

    t0 = time.perf_counter()
    res = CHECKS[check](scenario)
    summary = {"check": check, "scenario": scenario_name, "inputs_key": key, "skipped": False,
               "duration_s": round(time.perf_counter() - t0, 2), "metrics": res.metrics,
               "failures": res.failures, "evidence": res.evidence, "log": f"{check}.log"}
    approved_file = scenario.approved_dir / f"{check}.json"
    worse = find_what_got_worse(summary, json.loads(approved_file.read_text())) if approved_file.exists() else \
        {"new_failures": res.failures, "worse_metrics": []}
    summary["status"] = "fail" if worse["new_failures"] or worse["worse_metrics"] else "pass"
    summary["worse"] = worse
    (out / f"{check}.log").write_text("\n".join(res.lines) + "\n")
    (out / f"{check}.json").write_text(json.dumps(summary, indent=2))
    if hasattr(res, "decisions"):
        (out / "routing_decisions.json").write_text(json.dumps(res.decisions, indent=2, sort_keys=True))
    print(f"[{check}] {summary['status']} in {summary['duration_s']}s  metrics={res.metrics}")
    for f in worse["new_failures"][:10]:
        print(f"   {f['evidence']}  ({check}.log:{f['line']})")
    for m in worse["worse_metrics"]:
        print(f"   worse {m['metric']}: {m['now']} vs approved {m['approved']} (limit {m['limit']})")
    if cached and summary["status"] == "pass":           # only passing results are reusable
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(summary, indent=2))
    return summary


def save_as_approved(out: Path) -> None:
    """Copy this run's results into approved_results/, with the tolerances from checks.TOLERANCES.
    Only do this on purpose, after reviewing the run."""
    APPROVED.mkdir(parents=True, exist_ok=True)
    for check in CHECKS:
        s = json.loads((out / f"{check}.json").read_text())
        s = {k: s[k] for k in ("check", "metrics", "failures", "evidence")}
        s["compare"] = TOLERANCES.get(check, {})
        (APPROVED / f"{check}.json").write_text(json.dumps(s, indent=2) + "\n")
    shutil.copy(out / "routing_decisions.json", APPROVED / "routing_decisions.json")
    print(f"approved results written to {APPROVED}/")


def main() -> int:
    """Command line: run one check or all of them, stopping at the first failure."""
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", choices=list(CHECKS))
    g.add_argument("--all", action="store_true")
    ap.add_argument("--scenario", default="clean", help="clean, or a folder name in practice_scenarios/")
    ap.add_argument("--env", action="append", default=[], help="extra KEY=VALUE overrides")
    ap.add_argument("--out", type=Path, default=Path("reports/release_check"))
    ap.add_argument("--cache-dir", type=Path, default=None)
    ap.add_argument("--approve", action="store_true", help="save this run as the new approved results")
    args = ap.parse_args()

    logging.getLogger("httpx").setLevel(logging.WARNING)   # one line per request is noise here
    scenario = load_scenario(args.scenario, args.env)
    if args.approve:
        APPROVED.mkdir(parents=True, exist_ok=True)
        (APPROVED / "routing_decisions.json").write_text("{}")
        for check in CHECKS:
            (APPROVED / f"{check}.json").unlink(missing_ok=True)
        args.cache_dir = None
    checks = list(CHECKS) if args.all else [args.check]
    for check in checks:
        if run_check(check, scenario, args.out, args.cache_dir, args.scenario)["status"] == "fail" \
                and not args.approve:
            return 1                                       # fail fast: later checks don't run
    if args.approve:
        save_as_approved(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

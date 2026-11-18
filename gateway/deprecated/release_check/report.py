"""Write a short report of a release check: only what got worse than the approved results.

  python -m release_check.report --results reports/release_check

Checks that did not change get one line. Checks that never ran (an earlier one failed) are listed.
Each new failure shows its evidence and where to find it in <check>.log.
"""

import json
from pathlib import Path

from release_check.compare import find_what_got_worse

CHECK_ORDER = ["config_is_valid", "routing_unchanged"]


def build_report(results_dir: Path, approved_dir: Path) -> tuple[dict, str]:
    """Compare every check's result in results_dir with approved_dir.
    Returns the report as a dict (for JSON) and as Markdown (for humans)."""
    checks, problems, unchanged, not_run = {}, [], [], []
    for check in CHECK_ORDER:
        approved_file, result_file = approved_dir / f"{check}.json", results_dir / f"{check}.json"
        if not approved_file.exists():
            continue
        if not result_file.exists():
            not_run.append(check)
            continue
        result = json.loads(result_file.read_text())
        worse = find_what_got_worse(result, json.loads(approved_file.read_text()))
        checks[check] = {"skipped": result.get("skipped"), **worse}
        for f in worse["new_failures"]:
            problems.append({"check": check, "kind": "new_failure", "message": f["message"],
                             "evidence": f["evidence"], "where": f"{check}.log:{f['line']}"})
        for m in worse["worse_metrics"]:
            problems.append({"check": check, "kind": "worse_metric", "metric": m["metric"],
                             "approved": m["approved"], "now": m["now"], "limit": m["limit"],
                             "evidence": m.get("evidence")})
        if not worse["new_failures"] and not worse["worse_metrics"]:
            unchanged.append(f"{check} (cached)" if result.get("skipped") else check)

    md = ["# Release check vs approved results", ""]
    if not problems:
        md.append("Nothing got worse.")
    else:
        md += [f"**{len(problems)} problem(s)** (only what is new vs the approved results):", ""]
        for p in problems:
            if p["kind"] == "new_failure":
                md.append(f"- `{p['check']}` new failure ({p['where']}): `{p['evidence']}`")
            else:
                md.append(f"- `{p['check']}` {p['metric']}: {p['now']} vs approved {p['approved']} "
                          f"(limit {p['limit']}). Evidence: `{p['evidence']}`")
    md += ["", f"Unchanged: {', '.join(unchanged) or '-'}",
           f"Not run (an earlier check failed): {', '.join(not_run) or '-'}"]
    report = {"problems": problems, "unchanged": unchanged, "not_run": not_run, "checks": checks}
    return report, "\n".join(md)


def main() -> int:
    """Command line: print the report, optionally save it, exit 1 if anything got worse."""
    import argparse
    ap = argparse.ArgumentParser(description="python -m release_check.report --results reports/release_check")
    ap.add_argument("--results", type=Path, required=True, help="folder written by release_check.run")
    ap.add_argument("--approved", type=Path, default=Path("release_check/approved_results"))
    ap.add_argument("--out", type=Path, default=None, help="writes report.json + report.md here")
    ap.add_argument("--no-fail", action="store_true", help="exit 0 even when something got worse")
    a = ap.parse_args()
    report, md = build_report(a.results, a.approved)
    print(md)
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "report.json").write_text(json.dumps(report, indent=2))
        (a.out / "report.md").write_text(md + "\n")
    return 0 if a.no_fail or not report["problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

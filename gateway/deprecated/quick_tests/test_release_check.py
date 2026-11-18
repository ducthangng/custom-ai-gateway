"""The release_check tool itself: does it catch what it should, and skip what did not change?"""

import json
from pathlib import Path

from release_check.compare import find_what_got_worse
from release_check.report import build_report
from release_check.run import fingerprint_inputs, load_scenario, run_check

APPROVED = Path("release_check/approved_results")


def test_only_new_failures_and_worse_metrics_are_reported():
    """Known failures are ignored, and a metric only counts as worse once it passes its tolerance."""
    approved = {"metrics": {"p95_ms": 20, "accuracy": 0.97}, "failures": [{"id": "known"}],
                "compare": {"p95_ms": {"better": "lower", "rel": 1.0, "abs": 25},
                            "accuracy": {"better": "higher", "abs": 0.02}}}
    result = {"metrics": {"p95_ms": 60, "accuracy": 0.96},
              "failures": [{"id": "known"}, {"id": "new", "evidence": "x"}]}
    d = find_what_got_worse(result, approved)
    assert [f["id"] for f in d["new_failures"]] == ["new"]
    assert d["worse_metrics"] == []                            # 60 <= 20*2+25 ; 0.96 >= 0.95
    d = find_what_got_worse({**result, "metrics": {"p95_ms": 66, "accuracy": 0.94}}, approved)
    assert {m["metric"] for m in d["worse_metrics"]} == {"p95_ms", "accuracy"}


def test_scenario_replaces_routes_and_env():
    """'clean' uses the real routes.yaml; a practice scenario swaps in its own; --env adds overrides."""
    assert load_scenario("clean", []).routes_path == "routes.yaml"
    assert load_scenario("broken_config", []).routes_path.endswith("practice_scenarios/broken_config/routes.yaml")
    assert load_scenario("clean", ["LOAD_RPS=5"]).env == {"LOAD_RPS": "5"}


def test_fingerprint_changes_only_when_inputs_change():
    """Same inputs give the same fingerprint; other routes or env give a new one, but only
    for the checks that read them."""
    clean, worse = load_scenario("clean", []), load_scenario("worse_routing", [])
    assert fingerprint_inputs("routing_unchanged", clean) == fingerprint_inputs("routing_unchanged", clean)
    assert fingerprint_inputs("routing_unchanged", clean) != fingerprint_inputs("routing_unchanged", worse)
    assert fingerprint_inputs("api_unchanged", clean) == fingerprint_inputs("api_unchanged", worse)
    assert fingerprint_inputs("speed_under_load", clean) != \
        fingerprint_inputs("speed_under_load", load_scenario("clean", ["LOAD_RPS=5"]))


def test_broken_config_scenario_fails_the_config_check_with_evidence(tmp_path):
    """The planted bugs (unquoted null, bad regex, unknown model) are all reported, each with a log line."""
    s = run_check("config_is_valid", load_scenario("broken_config", []), tmp_path, None, "broken_config")
    assert s["status"] == "fail"
    msgs = " ".join(f["message"] for f in s["failures"])
    assert "not a string" in msgs and "does not compile" in msgs and "not in catalog.yaml" in msgs
    assert (tmp_path / "config_is_valid.log").read_text().startswith("FAIL")


def test_clean_routing_check_passes_and_a_second_run_is_skipped(tmp_path):
    """A passing check is cached; running it again with the same inputs skips it."""
    cache = tmp_path / "cache"
    first = run_check("routing_unchanged", load_scenario("clean", []), tmp_path / "a", cache, "clean")
    second = run_check("routing_unchanged", load_scenario("clean", []), tmp_path / "b", cache, "clean")
    assert first["status"] == "pass" and not first["skipped"]
    assert second["skipped"] == "cache"


def test_worse_routing_scenario_fails_the_routing_check(tmp_path):
    """Removing the code keywords makes some prompts lose their route, and the check says which."""
    s = run_check("routing_unchanged", load_scenario("worse_routing", []), tmp_path, None, "worse_routing")
    assert s["status"] == "fail" and s["metrics"]["changed"] > 0
    assert all("now none" in f["message"] for f in s["failures"])


def test_api_and_cache_checks_pass_on_clean_code(tmp_path):
    """The code as it is passes the API and cache checks."""
    for check in ("api_unchanged", "cache_works"):
        assert run_check(check, load_scenario("clean", []), tmp_path, None, "clean")["status"] == "pass", check


def test_report_lists_checks_that_never_ran(tmp_path):
    """After the first check fails, the report lists its problems and the 4 checks that never ran."""
    run_check("config_is_valid", load_scenario("broken_config", []), tmp_path, None, "broken_config")
    report, md = build_report(tmp_path, APPROVED)
    assert report["not_run"] == ["api_unchanged", "routing_unchanged", "cache_works", "speed_under_load"]
    assert len(report["problems"]) == 3 and "config_is_valid.log:1" in md
    json.dumps(report)

"""JJ (rules router): classifier rules, candidate selection, and the plugin inside the gateway."""

import asyncio
import json
import re

import pytest
import yaml

from gateway.catalog import load_catalog
from gateway.plugins.router import RouteRequest, Target
from gateway.plugins.rules_router import RulesRouter
from gateway.routing import RuleClassifier, load_routes
from gateway.routing.classifier import NO_ROUTE
from gateway.routing.config import parse_candidate
from tests.conftest import run_chat

TINY = """
min_score: 1
routes:
  - name: code
    target: {candidates: [anthropic/claude-sonnet-5-5, mock/echo], params: {level: high}}
    keywords: [python, stack trace]
    patterns: ['\\b\\w+(Error|Exception)\\b']
  - name: math
    target: {candidates: [openai/gpt-5], params: {temperature: 0}}
    keywords: [integral, python]
    patterns: ['\\d+\\s*[-+*/^]\\s*\\d+']
"""
CATALOG = load_catalog("catalog.yaml")


@pytest.fixture
def tiny(tmp_path):
    p = tmp_path / "routes.yaml"
    p.write_text(TINY)
    return str(p)


def route(router, prompt, available=frozenset({"mock"})):
    req = RouteRequest(prompt, [], Target("mock", "echo"), {}, CATALOG, set(available))
    return asyncio.run(router.route(req))


# ---------------------------------------------------------------- classifier
def test_keyword_and_pattern_routes(tiny):
    clf = RuleClassifier(load_routes(tiny))
    d = clf.classify("explain this stack trace")
    assert (d.route, d.matched) == ("code", ["stack trace"])
    assert clf.classify("what is 12 * 7").route == "math"


def test_more_matches_wins_and_ties_go_to_first_route(tiny):
    clf = RuleClassifier(load_routes(tiny))
    assert (clf.classify("python TypeError in my script").route, clf.classify("python").route) == ("code", "code")


def test_whole_words_case_insensitive_and_no_route(tiny):
    clf = RuleClassifier(load_routes(tiny))
    assert clf.classify("PYTHON help").route == "code"
    assert clf.classify("pythonic").route == NO_ROUTE          # 'pythonic' is not the word 'python'
    assert clf.classify("tell me about the weather").route == NO_ROUTE


def test_min_score_threshold(tiny):
    cfg = load_routes(tiny)
    strict = RuleClassifier(type(cfg)(**{**cfg.__dict__, "min_score": 2}))
    assert strict.classify("integral").route == NO_ROUTE
    assert strict.classify("python TypeError").route == "code"


def test_config_version_changes_with_content(tmp_path, tiny):
    other = tmp_path / "other.yaml"
    other.write_text(TINY + "\n# edited\n")
    assert load_routes(tiny).version != load_routes(str(other)).version


def test_bad_candidate_rejected():
    with pytest.raises(ValueError):
        parse_candidate("gpt-5")


# ---------------------------------------------------------------- plugin
def test_first_available_candidate_wins(tiny):
    r = RulesRouter(tiny)
    d = route(r, "python TypeError", {"mock"})
    assert (d.provider, d.model, d.params) == ("mock", "echo", {"level": "high"})
    assert "route code" in d.reason and d.score == 2.0
    d = route(r, "python TypeError", {"anthropic", "mock"})
    assert (d.provider, d.model) == ("anthropic", "claude-sonnet-5-5")


def test_no_route_or_no_usable_candidate_keeps_user_choice(tiny):
    r = RulesRouter(tiny)
    assert route(r, "tell me about the weather") is None
    assert route(r, "integral of x", {"mock"}) is None          # math only lists openai


def test_reason_hides_regex_source(tiny):
    d = route(RulesRouter(tiny), "python TypeError")
    assert "Error|Exception" not in d.reason and "+1 pattern" in d.reason


def test_real_routes_yaml_smoke():
    r = RulesRouter("routes.yaml")
    assert route(r, "why does my go program deadlock").reason.startswith("route code")
    assert route(r, "solve x^2 - 4 = 0").reason.startswith("route math")
    assert route(r, "hey, tell me a joke").reason.startswith("route chitchat")


def test_router_decides_inside_the_gateway(make_client):
    with make_client(router_class="gateway.plugins.rules_router:RulesRouter") as c:
        events = run_chat(c, "python TypeError in my script")
    model = next(d for e, d in events if e == "model")
    assert model["routed_by"] == "router" and model["route_reason"].startswith("route code")
    assert model["params"]["level"] == "high"
    assert events[-1][0] == "done"


# ---------------------------------------------------------------- routes.yaml static checks
with open("routes.yaml") as _f:
    CFG = yaml.safe_load(_f)


def norm(s: str) -> str:
    return s.strip().lower()


def test_routes_have_rules_utterances_and_catalog_candidates():
    assert CFG["min_score"] >= 1
    for r in CFG["routes"]:
        assert r.get("keywords") or r.get("patterns"), r["name"]
        assert len(r["utterances"]) >= 5, r["name"]
        for c in r["target"]["candidates"]:
            assert CATALOG.get(*parse_candidate(c)) is not None, f"{r['name']}: {c} not in catalog.yaml"


def test_keywords_are_strings_and_patterns_compile():
    # YAML turns bare `null`, `yes`, `no`, `on` into None/bool: that bit us once.
    for r in CFG["routes"]:
        for kw in r.get("keywords", []):
            assert isinstance(kw, str), f"{r['name']}: keyword {kw!r} is not a string (quote it)"
        for p in r.get("patterns", []):
            re.compile(p)


def prompts(path: str) -> set[str]:
    with open(path) as f:
        return {norm(json.loads(line)["prompt"]) for line in f if line.strip()}


def test_no_eval_leakage_and_disjoint_splits():
    train = {norm(u) for r in CFG["routes"] for u in r["utterances"]}
    dev, test = prompts("evals/dev.jsonl"), prompts("evals/test.jsonl")
    assert not (train & dev) and not (train & test)
    assert not (dev & test)

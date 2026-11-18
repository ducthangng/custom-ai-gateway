"""JJ, the rules router: how it picks a route, which model it then picks, and checks on the real routes.yaml."""

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
from quick_tests.helpers import send_chat

TINY_ROUTES = """
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
def tiny_routes_file(tmp_path):
    """A small routes.yaml with two routes (code, math), written to a temp file."""
    p = tmp_path / "routes.yaml"
    p.write_text(TINY_ROUTES)
    return str(p)


def ask_router(router, prompt, providers_with_key=frozenset({"mock"})):
    """Ask the router about one prompt, pretending only providers_with_key have an API key."""
    req = RouteRequest(prompt, [], Target("mock", "echo"), {}, CATALOG, set(providers_with_key))
    return asyncio.run(router.route(req))


# ---------------------------------------------------------------- picking a route
def test_keywords_and_patterns_pick_a_route(tiny_routes_file):
    """A keyword ('stack trace') or a regex pattern ('12 * 7') is enough to pick a route."""
    clf = RuleClassifier(load_routes(tiny_routes_file))
    d = clf.classify("explain this stack trace")
    assert (d.route, d.matched) == ("code", ["stack trace"])
    assert clf.classify("what is 12 * 7").route == "math"


def test_route_with_most_matches_wins_and_ties_go_to_the_first(tiny_routes_file):
    """'python' is in both routes: more matches wins, and a tie goes to the route listed first."""
    clf = RuleClassifier(load_routes(tiny_routes_file))
    assert (clf.classify("python TypeError in my script").route, clf.classify("python").route) == ("code", "code")


def test_keywords_match_whole_words_ignoring_case(tiny_routes_file):
    """'PYTHON' matches 'python', 'pythonic' does not, and a prompt with no match gets no route."""
    clf = RuleClassifier(load_routes(tiny_routes_file))
    assert clf.classify("PYTHON help").route == "code"
    assert clf.classify("pythonic").route == NO_ROUTE          # 'pythonic' is not the word 'python'
    assert clf.classify("tell me about the weather").route == NO_ROUTE


def test_route_needs_at_least_min_score_matches(tiny_routes_file):
    """With min_score: 2, one match is not enough to pick a route."""
    cfg = load_routes(tiny_routes_file)
    strict = RuleClassifier(type(cfg)(**{**cfg.__dict__, "min_score": 2}))
    assert strict.classify("integral").route == NO_ROUTE
    assert strict.classify("python TypeError").route == "code"


def test_routes_version_changes_when_the_file_changes(tmp_path, tiny_routes_file):
    """The version shown in route reasons is a hash of the file, so any edit changes it."""
    other = tmp_path / "other.yaml"
    other.write_text(TINY_ROUTES + "\n# edited\n")
    assert load_routes(tiny_routes_file).version != load_routes(str(other)).version


def test_candidate_without_provider_is_rejected():
    """Candidates must be written provider/model, e.g. openai/gpt-5."""
    with pytest.raises(ValueError):
        parse_candidate("gpt-5")


# ---------------------------------------------------------------- picking a model
def test_first_model_whose_provider_has_a_key_is_picked(tiny_routes_file):
    """From the route's candidate list, JJ takes the first one whose provider has an API key."""
    r = RulesRouter(tiny_routes_file)
    d = ask_router(r, "python TypeError", {"mock"})
    assert (d.provider, d.model, d.params) == ("mock", "echo", {"level": "high"})
    assert "route code" in d.reason and d.score == 2.0
    d = ask_router(r, "python TypeError", {"anthropic", "mock"})
    assert (d.provider, d.model) == ("anthropic", "claude-sonnet-5-5")


def test_user_choice_is_kept_when_no_route_or_no_usable_model(tiny_routes_file):
    """JJ returns nothing (so your own settings are used) if no route matches, or if no
    candidate model has an API key."""
    r = RulesRouter(tiny_routes_file)
    assert ask_router(r, "tell me about the weather") is None
    assert ask_router(r, "integral of x", {"mock"}) is None          # math only lists openai


def test_reason_shown_to_user_hides_regex_text(tiny_routes_file):
    """The reason in the UI says '+1 pattern' instead of printing the raw regex."""
    d = ask_router(RulesRouter(tiny_routes_file), "python TypeError")
    assert "Error|Exception" not in d.reason and "+1 pattern" in d.reason


def test_real_routes_yaml_routes_sample_prompts():
    """Quick sanity check on the real routes.yaml: one prompt each for code, math and chitchat."""
    r = RulesRouter("routes.yaml")
    assert ask_router(r, "why does my go program deadlock").reason.startswith("route code")
    assert ask_router(r, "solve x^2 - 4 = 0").reason.startswith("route math")
    assert ask_router(r, "hey, tell me a joke").reason.startswith("route chitchat")


def test_router_picks_the_model_inside_a_real_chat(make_client):
    """JJ plugged into the gateway: a coding prompt is routed to the code route's model and params."""
    with make_client(router_class="gateway.plugins.rules_router:RulesRouter") as c:
        events = send_chat(c, "python TypeError in my script")
    model = next(d for e, d in events if e == "model")
    assert model["routed_by"] == "router" and model["route_reason"].startswith("route code")
    assert model["params"]["level"] == "high"
    assert events[-1][0] == "done"


# ---------------------------------------------------------------- checks on the real routes.yaml
with open("routes.yaml") as _f:
    REAL_ROUTES = yaml.safe_load(_f)


def normalize(s: str) -> str:
    """Lowercase and trim, so 'Hello ' and 'hello' count as the same prompt."""
    return s.strip().lower()


def test_every_route_has_rules_examples_and_known_models():
    """Each route has keywords or patterns, at least 5 example prompts, and only models from catalog.yaml."""
    assert REAL_ROUTES["min_score"] >= 1
    for r in REAL_ROUTES["routes"]:
        assert r.get("keywords") or r.get("patterns"), r["name"]
        assert len(r["utterances"]) >= 5, r["name"]
        for c in r["target"]["candidates"]:
            assert CATALOG.get(*parse_candidate(c)) is not None, f"{r['name']}: {c} not in catalog.yaml"


def test_keywords_are_text_and_patterns_compile():
    """YAML turns bare null/yes/no/on into None or True/False: every keyword must stay text."""
    for r in REAL_ROUTES["routes"]:
        for kw in r.get("keywords", []):
            assert isinstance(kw, str), f"{r['name']}: keyword {kw!r} is not a string (quote it)"
        for p in r.get("patterns", []):
            re.compile(p)


def prompts_in(path: str) -> set[str]:
    """All prompts of an eval file (one JSON object per line), normalized."""
    with open(path) as f:
        return {normalize(json.loads(line)["prompt"]) for line in f if line.strip()}


def test_eval_prompts_are_not_copied_from_route_examples():
    """The eval sets must not contain the routes' own examples, and dev and test must not
    overlap; otherwise the accuracy score would be cheating."""
    examples = {normalize(u) for r in REAL_ROUTES["routes"] for u in r["utterances"]}
    dev, test = prompts_in("evals/dev.jsonl"), prompts_in("evals/test.jsonl")
    assert not (examples & dev) and not (examples & test)
    assert not (dev & test)

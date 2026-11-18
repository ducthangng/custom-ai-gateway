"""Release check: 2 simple checks on routes.yaml, run by the Jenkins job `qualify` before a release.

    python -m release_check.run                     # run both checks
    python -m release_check.run models_exist        # run one check
    python -m release_check.run --save              # accept today's routing as the new expected_routes.json

Prints PASS or FAIL (with the reasons) for each check. Exit code 1 if a check fails.
"""

import json
import sys
from pathlib import Path

import yaml

from gateway.catalog import load_catalog
from gateway.routing import RuleClassifier, load_routes

EXPECTED_ROUTES = Path("release_check/expected_routes.json")


def models_exist() -> list[str]:
    """Every model named in routes.yaml must exist in catalog.yaml. Returns the problems found."""
    catalog = load_catalog("catalog.yaml")
    routes = yaml.safe_load(Path("routes.yaml").read_text())["routes"]
    problems = []
    for route in routes:
        for model in route["target"]["candidates"]:
            provider, _, name = model.partition("/")
            if catalog.get(provider, name) is None:
                problems.append(f"route '{route['name']}' uses {model}, which is not in catalog.yaml")
    return problems


def routes_unchanged() -> list[str]:
    """Route 100 saved prompts again; each must still get the route stored in expected_routes.json."""
    router = RuleClassifier(load_routes("routes.yaml"))
    expected = json.loads(EXPECTED_ROUTES.read_text())
    problems = []
    for prompt, expected_route in expected.items():
        route_now = router.classify(prompt).route
        if route_now != expected_route:
            problems.append(f"{prompt!r}: expected {expected_route}, got {route_now}")
    return problems


def save_expected_routes() -> None:
    """Write today's route for each saved prompt into expected_routes.json. Only do this on purpose."""
    router = RuleClassifier(load_routes("routes.yaml"))
    prompts = json.loads(EXPECTED_ROUTES.read_text())
    routes = {prompt: router.classify(prompt).route for prompt in prompts}
    EXPECTED_ROUTES.write_text(json.dumps(routes, indent=2, sort_keys=True))
    print(f"saved {len(routes)} expected routes to {EXPECTED_ROUTES}")


CHECKS = {"models_exist": models_exist, "routes_unchanged": routes_unchanged}

def main(args: list[str]) -> int:
    """Run the checks named on the command line (or all of them) and print PASS / FAIL."""
    if args == ["--save"]:
        save_expected_routes()
        return 0
    failed = False
    for name in args or CHECKS:
        problems = CHECKS[name]()
        print(f"{'FAIL' if problems else 'PASS'}  {name}")
        for p in problems:
            print(f"      {p}")
        failed = failed or bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

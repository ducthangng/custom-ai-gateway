"""RuleClassifier: maps a prompt to a route name using keywords + regex from routes.yaml."""

import re
from dataclasses import dataclass, field

from gateway.routing.config import RoutesConfig

NO_ROUTE = "none"


@dataclass
class Decision:
    route: str  # route name, or NO_ROUTE
    score: float
    matched: list[str] = field(default_factory=list)  # evidence: which rules fired


class RuleClassifier:
    """Score = number of distinct rules (keywords + patterns) of a route that match.

    Keywords are case-insensitive whole words; patterns are regex, case-sensitive.
    Highest score wins; ties go to the route listed first in routes.yaml.
    Score below `min_score` -> NO_ROUTE.
    """

    def __init__(self, cfg: RoutesConfig):
        self.cfg = cfg
        self.rules: list[tuple[str, list[tuple[str, re.Pattern]]]] = []
        for r in cfg.routes:
            compiled = [(kw, re.compile(rf"(?<!\w){re.escape(kw)}(?!\w)", re.IGNORECASE)) for kw in r.keywords]
            compiled += [(p, re.compile(p)) for p in r.patterns]
            self.rules.append((r.name, compiled))

    def classify(self, prompt: str) -> Decision:
        best = Decision(NO_ROUTE, 0.0)
        for name, rules in self.rules:
            matched = [src for src, rx in rules if rx.search(prompt)]
            if len(matched) > best.score:  # strict '>' keeps the first route on ties
                best = Decision(name, float(len(matched)), matched)
        if best.score < self.cfg.min_score:
            return Decision(NO_ROUTE, best.score)
        return best

"""JJ, the built-in router plugin: rule-based routing from routes.yaml.

    GATEWAY_ROUTER_CLASS=gateway.plugins.rules_router:RulesRouter
    GATEWAY_ROUTES_PATH=routes.yaml        (default)

A matched route picks the first candidate model whose provider has credentials. No match, or no
usable candidate -> None, so the gateway keeps the user's own settings.
"""

from gateway.plugins.router import RouteDecision, RouteRequest
from gateway.routing import RuleClassifier, load_routes


class RulesRouter:
    display_name = "JJ"

    def __init__(self, routes_path: str | None = None):
        self.cfg = load_routes(routes_path)
        self.classifier = RuleClassifier(self.cfg)
        self.routes = {r.name: r for r in self.cfg.routes}

    async def route(self, req: RouteRequest) -> RouteDecision | None:
        d = self.classifier.classify(req.prompt)
        r = self.routes.get(d.route)
        if r is None:
            return None
        for provider, model in r.target.candidates:
            if provider in req.available_providers and req.catalog.get(provider, model) is not None:
                return RouteDecision(
                    provider=provider,
                    model=model,
                    params=dict(r.target.params),
                    reason=f"route {d.route} ({self._evidence(r.patterns, d.matched)}) · routes {self.cfg.version}",
                    score=d.score,
                )
        return None

    @staticmethod
    def _evidence(patterns: list[str], matched: list[str]) -> str:
        """'python, script +1 pattern': regex sources would be unreadable in the UI."""
        words = [m for m in matched if m not in set(patterns)]
        n_pat = len(matched) - len(words)
        out = ", ".join(words[:3]) + (f" +{len(words) - 3}" if len(words) > 3 else "")
        if n_pat:
            out += f"{' ' if out else ''}+{n_pat} pattern{'s' if n_pat > 1 else ''}"
        return out

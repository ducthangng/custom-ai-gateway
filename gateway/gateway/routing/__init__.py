"""JJ: rule-based prompt routing (no ML dependencies). Used by gateway.plugins.rules_router."""

from gateway.routing.classifier import Decision, RuleClassifier
from gateway.routing.config import Route, RoutesConfig, Target, load_routes

__all__ = ["Decision", "Route", "RoutesConfig", "RuleClassifier", "Target", "load_routes"]

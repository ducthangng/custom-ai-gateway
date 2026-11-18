"""Load routes.yaml. The file hash is the config version (shown in the request log reason)."""

import hashlib
import os
from dataclasses import dataclass, field

import yaml


@dataclass(frozen=True)
class Target:
    candidates: list[tuple[str, str]]  # (provider, model) in order of preference
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Route:
    name: str
    target: Target
    keywords: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    utterances: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RoutesConfig:
    version: str  # first 12 hex chars of sha256(routes.yaml)
    routes: list[Route]
    min_score: int


def parse_candidate(spec: str) -> tuple[str, str]:
    provider, sep, model = spec.partition("/")
    if not sep or not provider or not model:
        raise ValueError(f"candidate must look like 'provider/model', got {spec!r}")
    return provider, model


def load_routes(path: str | None = None) -> RoutesConfig:
    path = path or os.getenv("GATEWAY_ROUTES_PATH", "routes.yaml")
    with open(path, "rb") as f:
        raw = f.read()  # hash and parse the same bytes
    cfg = yaml.safe_load(raw)
    routes = [
        Route(
            name=r["name"],
            target=Target([parse_candidate(c) for c in r["target"]["candidates"]], r["target"].get("params") or {}),
            keywords=r.get("keywords") or [],
            patterns=r.get("patterns") or [],
            utterances=r.get("utterances") or [],
        )
        for r in cfg["routes"]
    ]
    return RoutesConfig(hashlib.sha256(raw).hexdigest()[:12], routes, int(cfg.get("min_score", 1)))

"""Shared test setup. pytest loads this file automatically (the name conftest.py is required)."""

import pytest
from fastapi.testclient import TestClient

from gateway.config import Settings
from gateway.main import create_app
from gateway.providers.mock_provider import MockProvider


@pytest.fixture(autouse=True)
def no_delay_and_no_real_api_keys(monkeypatch):
    """Runs before every test: the mock model answers instantly, and real provider keys
    from your shell are hidden so no test ever calls a paid API."""
    monkeypatch.setattr(MockProvider, "delay_s", 0)
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(env, raising=False)


@pytest.fixture
def make_client(tmp_path):
    """Returns a function that builds a test gateway with a fresh SQLite database.
    Pass settings to change it, e.g. make_client(cache_class=...)."""
    def _make(**overrides) -> TestClient:
        settings = Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
            catalog_path="catalog.yaml",
            static_dir=str(tmp_path / "no-ui"),
            router_timeout_s=0.2,
            **overrides,
        )
        return TestClient(create_app(settings))

    return _make


@pytest.fixture
def client(make_client):
    """A test gateway with default settings: no cache, no router."""
    with make_client() as c:
        yield c

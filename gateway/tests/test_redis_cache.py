"""RedisCache plugin: unit tests on fakeredis, fail-open through the gateway, and (marker
`integration`) the same checks against a real Redis at GATEWAY_REDIS_URL."""

import asyncio
import os

import pytest
from fakeredis import FakeAsyncRedis

from gateway.plugins.redis_cache import RedisCache
from tests.conftest import run_chat


def run(coro):
    return asyncio.run(coro)


def test_get_set_ttl_on_fakeredis():
    c = RedisCache(client=FakeAsyncRedis(decode_responses=True))

    async def go():
        assert await c.get("k") is None
        await c.set("k", '{"text": "hi"}', ttl_s=60)
        assert await c.get("k") == '{"text": "hi"}'
        assert 0 < await c.r.ttl("k") <= 60
        await c.set("n", "v")                     # no TTL
        assert await c.r.ttl("n") == -1
    run(go())


def test_second_identical_request_is_a_cache_hit(make_client, monkeypatch):
    shared = FakeAsyncRedis(decode_responses=True)
    monkeypatch.setattr("gateway.plugins.redis_cache.aioredis.Redis.from_url", lambda *a, **k: shared)
    with make_client(cache_class="gateway.plugins.redis_cache:RedisCache") as c:
        first = run_chat(c, "cache me please", routing="manual")
        second = run_chat(c, "cache me please", routing="manual", conversation_id=None)
    assert first[-1][1]["cache_status"] == "miss"
    assert second[-1][1]["cache_status"] == "hit" and second[-1][1]["output_tokens"] == 0


def test_redis_down_fails_open(make_client, monkeypatch):
    monkeypatch.setenv("GATEWAY_REDIS_URL", "redis://127.0.0.1:1/0")      # nothing listens on port 1
    with make_client(cache_class="gateway.plugins.redis_cache:RedisCache", cache_timeout_s=0.2) as c:
        events = run_chat(c, "still answers", routing="manual")
    done = events[-1][1]
    assert events[-1][0] == "done" and done["status"] == "ok" and done["cache_status"] == "error"


@pytest.mark.integration
def test_real_redis_roundtrip():
    c = RedisCache(os.getenv("GATEWAY_REDIS_URL", "redis://localhost:6379/0"))

    async def go():
        await c.set("gw:test:roundtrip", "v", ttl_s=5)
        assert await c.get("gw:test:roundtrip") == "v"
        await c.r.delete("gw:test:roundtrip")
        await c.shutdown()
    run(go())

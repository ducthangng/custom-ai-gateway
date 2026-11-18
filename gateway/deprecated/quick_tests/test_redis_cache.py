"""The Redis cache plugin. Most tests use fakeredis (in memory); the one marked `integration`
needs a real Redis at GATEWAY_REDIS_URL (CI starts one; locally: make redis-up)."""

import asyncio
import os

import pytest
from fakeredis import FakeAsyncRedis

from gateway.plugins.redis_cache import RedisCache
from quick_tests.helpers import send_chat

REDIS_CACHE = "gateway.plugins.redis_cache:RedisCache"


def run_async(coro):
    """Run an async function from a normal test."""
    return asyncio.run(coro)


def test_cache_stores_values_and_expires_them():
    """get returns what set stored; with ttl_s the key expires, without it the key stays."""
    c = RedisCache(client=FakeAsyncRedis(decode_responses=True))

    async def go():
        assert await c.get("k") is None
        await c.set("k", '{"text": "hi"}', ttl_s=60)
        assert await c.get("k") == '{"text": "hi"}'
        assert 0 < await c.r.ttl("k") <= 60
        await c.set("n", "v")                     # no TTL
        assert await c.r.ttl("n") == -1
    run_async(go())


def test_second_identical_request_is_a_cache_hit(make_client, monkeypatch):
    """Through the whole gateway: the second same chat comes from Redis and uses 0 model tokens."""
    shared = FakeAsyncRedis(decode_responses=True)
    monkeypatch.setattr("gateway.plugins.redis_cache.aioredis.Redis.from_url", lambda *a, **k: shared)
    with make_client(cache_class=REDIS_CACHE) as c:
        first = send_chat(c, "cache me please", routing="manual")
        second = send_chat(c, "cache me please", routing="manual", conversation_id=None)
    assert first[-1][1]["cache_status"] == "miss"
    assert second[-1][1]["cache_status"] == "hit" and second[-1][1]["output_tokens"] == 0


def test_chat_still_works_when_redis_is_down(make_client, monkeypatch):
    """If Redis cannot be reached the chat still answers; cache_status just says 'error'."""
    monkeypatch.setenv("GATEWAY_REDIS_URL", "redis://127.0.0.1:1/0")      # nothing listens on port 1
    with make_client(cache_class=REDIS_CACHE, cache_timeout_s=0.2) as c:
        events = send_chat(c, "still answers", routing="manual")
    done = events[-1][1]
    assert events[-1][0] == "done" and done["status"] == "ok" and done["cache_status"] == "error"


@pytest.mark.integration
def test_real_redis_stores_and_returns_a_value():
    """Same set/get as above, against a real Redis server."""
    c = RedisCache(os.getenv("GATEWAY_REDIS_URL", "redis://localhost:6379/0"))

    async def go():
        await c.set("gw:test:roundtrip", "v", ttl_s=5)
        assert await c.get("gw:test:roundtrip") == "v"
        await c.r.delete("gw:test:roundtrip")
        await c.shutdown()
    run_async(go())

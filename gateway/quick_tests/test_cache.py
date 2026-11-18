"""The Redis cache: what we save, we can read back. FakeAsyncRedis is a Redis in memory, so no server is needed."""

import asyncio

from fakeredis import FakeAsyncRedis

from gateway.plugins.redis_cache import RedisCache


def test_cache_gives_back_what_was_saved():
    cache = RedisCache(client=FakeAsyncRedis(decode_responses=True))

    async def save_then_read():
        await cache.set("question", "answer")
        return await cache.get("question")

    assert asyncio.run(save_then_read()) == "answer"

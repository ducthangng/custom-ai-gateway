"""Redis KV cache plugin: shared by every gateway replica, survives gateway restarts.

    GATEWAY_CACHE_CLASS=gateway.plugins.redis_cache:RedisCache
    GATEWAY_REDIS_URL=redis://localhost:6379/0

Client retries are off: the gateway already treats a slow or failed call as a miss (fail-open,
GATEWAY_CACHE_TIMEOUT_S), and redis-py's default retries would only make that miss slower.
"""

import os

import redis.asyncio as aioredis
from redis.backoff import NoBackoff
from redis.retry import Retry


class RedisCache:
    def __init__(self, url: str | None = None, client: aioredis.Redis | None = None):
        self.url = url or os.getenv("GATEWAY_REDIS_URL", "redis://localhost:6379/0")
        timeout = float(os.getenv("GATEWAY_CACHE_TIMEOUT_S", "0.05"))
        self.r = client or aioredis.Redis.from_url(
            self.url, socket_timeout=timeout, socket_connect_timeout=timeout,
            retry=Retry(NoBackoff(), 0), decode_responses=True,
        )

    async def get(self, key: str) -> str | None:
        return await self.r.get(key)

    async def set(self, key: str, value: str, ttl_s: int | None = None) -> None:
        await self.r.set(key, value, ex=ttl_s or None)

    async def shutdown(self) -> None:
        await self.r.aclose()

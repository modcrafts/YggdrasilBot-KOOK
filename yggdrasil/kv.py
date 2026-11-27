from functools import cache

import redis.asyncio as aioredis

from yggdrasil.config import REDIS_CONFIG


@cache
def redis_client(*, blocking: bool = False) -> aioredis.Redis:
    client = aioredis.Redis.from_url(str(REDIS_CONFIG.url), decode_responses=True)
    if blocking:
        # This separate pool is for blocking commands with a deadline set by the caller.
        # Override after parsing the URL, whose options take precedence over constructor kwargs.
        client.connection_pool.connection_kwargs["socket_timeout"] = None
    return client

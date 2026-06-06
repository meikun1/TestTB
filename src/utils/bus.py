"""
Redis pub/sub шина для координации между воркерами/дашбордом.

Каналы:
  POOL_RELOAD — сигнал «перечитать пул аккаунтов из БД».
                Публикуется при intake API, enable/disable, бане.
                Подписываются все sender-воркеры.

Использование:
  await publish_pool_reload(reason="intake:acc01")          # из dashboard
  async for msg in subscribe_pool_reload(): ...             # в sender
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import redis.asyncio as redis_async
from loguru import logger

from config import settings


_client: redis_async.Redis | None = None


def _redis() -> redis_async.Redis:
    global _client
    if _client is None:
        _client = redis_async.from_url(settings.redis_url, decode_responses=True)
    return _client


async def publish_pool_reload(reason: str = "", account_id: int | None = None) -> None:
    """Уведомить всех sender-воркеров что в пуле что-то поменялось."""
    payload = json.dumps({"reason": reason, "account_id": account_id})
    try:
        await _redis().publish(settings.redis_channel_pool, payload)
    except Exception as e:
        # Падение Redis не должно валить intake API
        logger.warning(f"Redis publish failed: {e}")


async def subscribe_pool_reload() -> AsyncIterator[dict]:
    """
    Подписка на сигналы reload. Делает auto-reconnect при разрывах.
    Yield-ит dict с полями {reason, account_id}.
    """
    while True:
        try:
            pubsub = _redis().pubsub()
            await pubsub.subscribe(settings.redis_channel_pool)
            logger.info(f"Subscribed to {settings.redis_channel_pool}")
            try:
                async for msg in pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    try:
                        yield json.loads(msg["data"])
                    except (ValueError, KeyError, TypeError):
                        yield {"reason": "unknown", "account_id": None}
            finally:
                await pubsub.unsubscribe(settings.redis_channel_pool)
                await pubsub.close()
        except Exception as e:
            logger.error(f"Redis subscribe error: {e}, reconnect in 5s")
            await asyncio.sleep(5)


async def close_bus() -> None:
    global _client
    if _client is not None:
        try:
            await _client.close()
        except Exception:
            pass
        _client = None

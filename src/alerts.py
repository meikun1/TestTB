"""
Простой Telegram bot-уведомитель для health/error алертов.

Использует только HTTP API Telegram'а — без telethon, без зависимостей кроме httpx.
Если ALERT_BOT_TOKEN / ALERT_CHAT_ID не заданы — no-op.

Использование:
    from .alerts import alert
    await alert("Sender stuck on shard 5")
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import httpx
from loguru import logger

from .config import settings


# Cooldown между одинаковыми алертами чтобы не спамить
_last_sent: dict[str, datetime] = {}
_DEFAULT_COOLDOWN = timedelta(minutes=30)


async def alert(text: str, key: str | None = None, silent: bool = False) -> None:
    """
    Шлёт текст в чат ALERT_CHAT_ID через бота ALERT_BOT_TOKEN.

    key — если задан, применяется cooldown 30 мин для одинаковых key
          (чтобы не спамить о том же раз в минуту)
    silent — disable_notification в Telegram
    """
    if not settings.alert_bot_token or not settings.alert_chat_id:
        logger.debug(f"[alert skipped] {text[:200]}")
        return

    if key:
        last = _last_sent.get(key)
        if last and datetime.utcnow() - last < _DEFAULT_COOLDOWN:
            return
        _last_sent[key] = datetime.utcnow()

    url = f"https://api.telegram.org/bot{settings.alert_bot_token}/sendMessage"
    payload = {
        "chat_id": settings.alert_chat_id,
        "text": f"⚠️ tg-broadcaster\n\n{text[:3500]}",
        "disable_notification": silent,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as cli:
            r = await cli.post(url, json=payload)
            if r.status_code != 200:
                logger.warning(f"alert send {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"alert send failed: {e}")

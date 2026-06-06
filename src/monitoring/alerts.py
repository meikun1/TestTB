"""
Простой клиент для отправки алертов в Telegram через Bot API.

ALERT_BOT_TOKEN — токен бота от @BotFather
ALERT_CHAT_ID   — куда слать (твой chat_id или id группы, начинается с '-' для групп)
"""
from __future__ import annotations

import httpx
from loguru import logger

from config import settings


async def send_alert(text: str, *, silent: bool = False) -> None:
    """
    Шлёт сообщение в назначенный чат. Никогда не пробрасывает исключения —
    мониторинг не должен валить основной процесс.
    """
    if not settings.alert_bot_token or not settings.alert_chat_id:
        logger.debug(f"[alert skipped, no token/chat] {text}")
        return

    url = f"https://api.telegram.org/bot{settings.alert_bot_token}/sendMessage"
    payload = {
        "chat_id": settings.alert_chat_id,
        "text": f"⚠️ tg-assistant: {text}",
        "disable_notification": silent,
        "parse_mode": "HTML",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as cli:
            r = await cli.post(url, json=payload)
            if r.status_code != 200:
                logger.warning(f"Alert send {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"Alert send failed: {e}")

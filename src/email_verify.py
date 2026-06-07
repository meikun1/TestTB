"""
Email-верификация при логине Telegram через catch-all почтовый сервер.

Когда Telegram требует подтвердить email:
  1) generate_email() выдаёт уникальный <tg_8hex>@EMAIL_DOMAIN
  2) Адрес сохраняется в UsedEmail (UNIQUE)
  3) Подаётся Telegram через telethon
  4) wait_for_code() поллит IMAP, ищет письма на этот адрес
  5) Парсит 5-6 значный код из тела, возвращает
  6) mark_verified() закрывает запись

Каждый адрес используется ровно один раз — гарантия через UNIQUE-индекс.
"""
from __future__ import annotations

import asyncio
import email as emaillib
import imaplib
import re
import secrets
import time
from datetime import datetime
from email.header import decode_header
from typing import Optional

from loguru import logger
from sqlalchemy import select

from .config import settings
from .db import async_session
from .models import UsedEmail


_CODE_RE = re.compile(r"(?<!\d)(\d{5,6})(?!\d)")


# ============================================================================
# Генерация уникального адреса
# ============================================================================

async def generate_email(account_id: int | None = None) -> str:
    if not settings.email_domain:
        raise RuntimeError("EMAIL_DOMAIN не задан в .env")

    for _ in range(20):
        candidate = f"tg_{secrets.token_hex(4)}@{settings.email_domain}"
        async with async_session() as session:
            exists = (await session.execute(
                select(UsedEmail).where(UsedEmail.address == candidate)
            )).scalar_one_or_none()
            if exists is not None:
                continue
            session.add(UsedEmail(address=candidate, account_id=account_id))
            await session.commit()
            logger.info(f"[email] выдан адрес {candidate}")
            return candidate
    raise RuntimeError("не смог сгенерировать уникальный email за 20 попыток")


async def mark_verified(address: str, code: str) -> None:
    async with async_session() as session:
        row = (await session.execute(
            select(UsedEmail).where(UsedEmail.address == address)
        )).scalar_one_or_none()
        if row is not None:
            row.verified_at = datetime.utcnow()
            row.code_received = code
            await session.commit()


# ============================================================================
# IMAP — поиск кода
# ============================================================================

def _decode(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    parts = []
    for chunk, enc in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(enc or "utf-8", errors="ignore"))
        else:
            parts.append(chunk)
    return "".join(parts)


def _extract_body(msg) -> str:
    plains, htmls = [], []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="ignore")
            except (LookupError, UnicodeDecodeError):
                text = payload.decode("utf-8", errors="ignore")
            (plains if ctype == "text/plain" else htmls).append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                plains.append(payload.decode(charset, errors="ignore"))
            except (LookupError, UnicodeDecodeError):
                plains.append(payload.decode("utf-8", errors="ignore"))
    return "\n\n".join(plains + htmls)


def _extract_code(body: str) -> Optional[str]:
    m = _CODE_RE.search(body)
    return m.group(1) if m else None


def _imap_connect():
    if settings.imap_use_ssl:
        m = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    else:
        m = imaplib.IMAP4(settings.imap_host, settings.imap_port)
    m.login(settings.imap_user, settings.imap_password)
    m.select("INBOX")
    return m


def _check_for_code_sync(target_addr: str) -> Optional[str]:
    target = target_addr.lower()
    try:
        m = _imap_connect()
    except Exception as e:
        logger.error(f"[email] IMAP connect failed: {e}")
        return None
    try:
        typ, data = m.search(None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return None
        for num in data[0].split():
            typ, msg_data = m.fetch(num, "(RFC822)")
            if typ != "OK":
                continue
            raw = msg_data[0][1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            msg = emaillib.message_from_bytes(raw)
            to_field = _decode(msg.get("To")).lower()
            delivered_to = _decode(msg.get("Delivered-To")).lower()
            if target not in to_field and target not in delivered_to:
                continue
            body = _extract_body(msg)
            code = _extract_code(body)
            if code:
                m.store(num, "+FLAGS", "\\Seen")
                logger.info(f"[email] code {code} найден для {target}")
                return code
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return None


async def wait_for_code(
    address: str,
    timeout: int | None = None,
    poll_interval: int = 5,
) -> Optional[str]:
    if not settings.imap_host:
        raise RuntimeError("IMAP_HOST не задан в .env")

    timeout = timeout or settings.email_code_timeout_seconds
    deadline = time.monotonic() + timeout
    logger.info(f"[email] жду код на {address} (timeout {timeout}s)")

    while time.monotonic() < deadline:
        code = await asyncio.get_running_loop().run_in_executor(
            None, _check_for_code_sync, address
        )
        if code:
            await mark_verified(address, code)
            return code
        await asyncio.sleep(poll_interval)

    logger.warning(f"[email] таймаут — код для {address} не пришёл")
    return None


# ============================================================================
# CLI test
# ============================================================================

async def _test_cli() -> None:
    """
    python -m src.email_verify
    Генерит адрес, ждёт письмо, печатает код. Для smoke-теста IMAP.
    """
    from .db import init_db
    await init_db()
    addr = await generate_email()
    print(f"Сгенерирован адрес: {addr}")
    print("Отправь на него любое письмо с 5-6 значным числом в теле.")
    code = await wait_for_code(addr, timeout=120)
    if code:
        print(f"✓ код найден: {code}")
    else:
        print("✗ за 2 минуты ничего не пришло — проверь IMAP конфиг")


if __name__ == "__main__":
    asyncio.run(_test_cli())

"""
Подсистема email-верификации при логине Telegram.

Когда Telegram во время `client.start()` требует email-подтверждение,
мы:
  1) генерируем уникальный адрес <random>@<EMAIL_DOMAIN>, кладём в БД
  2) подаём адрес в Telethon (он отправит запрос на серверы Telegram)
  3) Telegram присылает письмо с кодом на этот адрес
  4) IMAP-клиент периодически опрашивает catch-all почтовый ящик
  5) находит письмо адресованное нашему сгенерированному адресу,
     парсит из него 5-6-значный код
  6) отдаёт код Telethon'у

Адрес сохраняется в `used_emails` и больше не выдаётся повторно —
один Telegram-аккаунт = один свой email.
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

from config import settings
from src.utils import async_session, UsedEmail


# Telegram-коды — 5 или 6 цифр. Парсер ищет первое подходящее число в теле.
_CODE_RE = re.compile(r"(?<!\d)(\d{5,6})(?!\d)")


# ============================================================================
# Генерация уникального адреса
# ============================================================================

async def generate_email(account_id: int | None = None) -> str:
    """
    Создаёт <8-hex>@<EMAIL_DOMAIN>, проверяет уникальность по БД,
    записывает в used_emails.

    Префикс tg_ помогает фильтровать в почте и в логах.
    """
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
            logger.info(f"[email_verify] выдан адрес {candidate}")
            return candidate
    raise RuntimeError("не смог сгенерировать уникальный email за 20 попыток")


async def mark_verified(address: str) -> None:
    async with async_session() as session:
        row = (await session.execute(
            select(UsedEmail).where(UsedEmail.address == address)
        )).scalar_one_or_none()
        if row is not None:
            row.verified_at = datetime.utcnow()
            await session.commit()


# ============================================================================
# IMAP — поиск кода
# ============================================================================

def _decode(value: str | bytes | None) -> str:
    """Безопасное декодирование MIME-заголовков (для To/Subject)."""
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


def _extract_body(msg: emaillib.message.Message) -> str:
    """Собирает текстовое содержимое (text/plain приоритет, потом html)."""
    plains: list[str] = []
    htmls: list[str] = []

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
            if ctype == "text/plain":
                plains.append(text)
            else:
                htmls.append(text)
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
    """
    Ищет 5-6-значный код в теле письма.
    У Telegram код обычно идёт после фразы 'Login code:' / 'Your code:' /
    'код подтверждения:', но мы просто берём первое чистое 5-6-значное число.
    """
    m = _CODE_RE.search(body)
    return m.group(1) if m else None


def _imap_connect() -> imaplib.IMAP4:
    if settings.imap_use_ssl:
        m = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    else:
        m = imaplib.IMAP4(settings.imap_host, settings.imap_port)
    m.login(settings.imap_user, settings.imap_password)
    m.select("INBOX")
    return m


def _check_for_code_sync(target_addr: str) -> Optional[str]:
    """
    Один раз сходить в IMAP и поискать письма для target_addr.
    Возвращает код или None.
    Возможно блокирующая операция — вызывается через run_in_executor.
    """
    target = target_addr.lower()
    try:
        m = _imap_connect()
    except Exception as e:
        logger.error(f"[email_verify] IMAP connect failed: {e}")
        return None

    try:
        # Берём только непрочитанные
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

            to_field = _decode(msg.get("To") or "").lower()
            delivered_to = _decode(msg.get("Delivered-To") or "").lower()
            if target not in to_field and target not in delivered_to:
                continue

            body = _extract_body(msg)
            code = _extract_code(body)
            if code:
                # помечаем прочитанным чтобы не подобрать второй раз
                m.store(num, "+FLAGS", "\\Seen")
                logger.info(f"[email_verify] code {code} найден для {target}")
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
    """
    Опрашивает IMAP пока не придёт код или не выйдет таймаут.
    Не блокирует event-loop (IMAP-вызов в executor'е).
    """
    if not settings.imap_host:
        raise RuntimeError("IMAP_HOST не задан в .env")

    timeout = timeout or settings.email_code_timeout_seconds
    deadline = time.monotonic() + timeout
    logger.info(f"[email_verify] жду код на {address} (timeout {timeout}s)")

    while time.monotonic() < deadline:
        code = await asyncio.get_running_loop().run_in_executor(
            None, _check_for_code_sync, address
        )
        if code:
            await mark_verified(address)
            return code
        await asyncio.sleep(poll_interval)

    logger.warning(f"[email_verify] таймаут — код для {address} не пришёл")
    return None


# ============================================================================
# CLI — для теста IMAP-настроек
# ============================================================================

async def _test_cli() -> None:
    """
    python -m src.accounts.email_verify
    Генерит адрес, ждёт письмо, печатает код. Удобно для проверки IMAP-конфига.
    """
    addr = await generate_email()
    print(f"Сгенерирован адрес: {addr}")
    print(f"Отправь на него любое письмо с 5-6-значным числом в теле.")
    code = await wait_for_code(addr, timeout=120)
    if code:
        print(f"✓ код найден: {code}")
    else:
        print("✗ за 2 минуты ничего не пришло — проверь IMAP_HOST/USER/PASSWORD")


if __name__ == "__main__":
    asyncio.run(_test_cli())

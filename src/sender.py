"""
Sender-воркер: берёт Draft'ы из очереди и отправляет.

Запуск:
    python -m src.sender --shard 0
    python -m src.sender --shard 1
    ...

В docker-compose поднимается WORKER_COUNT штук, каждый со своим shard_id.
Шардинг: account.id % WORKER_COUNT == shard_id.

Логика:
  1. Active-hours check (UZ timezone)
  2. Claim Draft через SELECT FOR UPDATE SKIP LOCKED
  3. Проверка opt-out (contact в BlockedContact)
  4. Carrier-throttling (Beeline UZ — мягче лимит)
  5. Account-aware FloodWait state
  6. Отправка текста
  7. Если есть attachment — пауза, отправка вложения
  8. Запись в SendLog
  9. Случайная пауза 30-90 сек
"""
import argparse
import asyncio
import random
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import desc, func, select
from telethon import TelegramClient
from telethon.errors import (
    ChatWriteForbiddenError,
    FloodWaitError,
    PeerFloodError,
    UserPrivacyRestrictedError,
)

from .attachments import send_attachment
from .config import settings
from .db import async_session, init_db
from .models import Account, BlockedContact, Contact, Draft, SendLog
from .telethon_client import build_client
from .timewindow import is_active_now, seconds_until_active


# ============================================================================
# Carrier-aware лимиты
# ============================================================================

async def _check_carrier_capacity(session, account: Account) -> tuple[bool, str]:
    """
    Если RESPECT_CARRIER_LIMITS — не превышать CARRIER_HOURLY_CAP суммарно
    на всех аккаунтах одного carrier за последний час.
    Для Beeline дополнительно множитель.
    """
    if not settings.respect_carrier_limits or not account.carrier:
        return True, "ok"

    since = datetime.utcnow() - timedelta(hours=1)
    sent_carrier = (await session.execute(
        select(func.count(SendLog.id))
        .join(Account, Account.id == SendLog.account_id)
        .where(Account.carrier == account.carrier)
        .where(SendLog.sent_at >= since)
        .where(SendLog.success.is_(True))
        .where(SendLog.kind == "message")
    )).scalar() or 0

    cap = settings.carrier_hourly_cap
    if account.carrier == "beeline":
        cap = int(cap * settings.beeline_uz_limit_multiplier)

    if sent_carrier >= cap:
        return False, f"carrier_cap:{account.carrier}:{sent_carrier}/{cap}"
    return True, "ok"


# ============================================================================
# Очередь
# ============================================================================

async def claim_next_draft(
    session, shard_id: int, shard_count: int,
) -> tuple[Draft, Contact, Account] | None:
    q = (
        select(Draft, Contact, Account)
        .join(Contact, Draft.contact_id == Contact.id)
        .join(Account, Draft.account_id == Account.id)
        .where(Draft.status == "pending")
        .where(Account.enabled.is_(True))
        .where(Account.status == "active")
        .where((Draft.account_id % shard_count) == shard_id)
        .order_by(desc(Contact.last_msg_at), Draft.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True, of=Draft)
    )
    row = (await session.execute(q)).first()
    if not row:
        return None
    return row


# ============================================================================
# Отправка
# ============================================================================

async def _attachment_delay(draft: Draft) -> int:
    if draft.attachment_delay_seconds and draft.attachment_delay_seconds > 0:
        return draft.attachment_delay_seconds
    return random.randint(
        settings.attachment_delay_min, settings.attachment_delay_max,
    )


async def send_one(
    client: TelegramClient,
    account: Account,
    contact: Contact,
    draft: Draft,
    session,
) -> tuple[str, Optional[int]]:
    """
    Возвращает (status, extra):
      ('sent', None)         — успешно
      ('flood', seconds)     — FloodWait
      ('banned', None)       — PeerFlood, аккаунт горит
      ('skip', None)         — privacy/forbidden/etc
    """
    name = contact.first_name or contact.username or str(contact.tg_user_id)
    has_attachment = bool(draft.attachment_kind and draft.attachment_ref)

    try:
        # 1) Текст
        await client.send_message(contact.tg_user_id, draft.text, link_preview=True)
        logger.success(f"[{account.name}] text → {name}")
        session.add(SendLog(
            draft_id=draft.id, account_id=account.id,
            success=True, kind="message",
        ))

        # 2) Вложение (если есть)
        if has_attachment:
            delay = await _attachment_delay(draft)
            logger.info(
                f"[{account.name}] {draft.attachment_kind} через {delay}s → {name}"
            )
            await asyncio.sleep(delay)
            try:
                await send_attachment(
                    client, contact, draft, caption=draft.attachment_caption,
                )
                logger.success(
                    f"[{account.name}] {draft.attachment_kind} ✓ → {name}"
                )
                session.add(SendLog(
                    draft_id=draft.id, account_id=account.id,
                    success=True, kind="attachment",
                ))
            except FileNotFoundError as e:
                logger.error(f"[{account.name}] {e}")
                session.add(SendLog(
                    draft_id=draft.id, account_id=account.id,
                    success=False, kind="attachment", error=str(e)[:200],
                ))

        draft.status = "sent"
        draft.sent_at = datetime.utcnow()
        account.last_send_at = datetime.utcnow()
        return "sent", None

    except FloodWaitError as e:
        wait = int(e.seconds * 1.15)
        logger.warning(f"[{account.name}] FloodWait {wait}s")
        return "flood", wait

    except PeerFloodError:
        logger.critical(f"[{account.name}] PeerFloodError — аккаунт сжёгся")
        draft.status = "failed"
        session.add(SendLog(
            draft_id=draft.id, account_id=account.id,
            success=False, error="peer_flood", kind="message",
        ))
        return "banned", None

    except (UserPrivacyRestrictedError, ChatWriteForbiddenError) as e:
        logger.info(f"[{account.name}] privacy/forbidden для {name}")
        draft.status = "failed"
        session.add(SendLog(
            draft_id=draft.id, account_id=account.id,
            success=False, error=e.__class__.__name__, kind="message",
        ))
        return "skip", None

    except Exception as e:
        logger.error(f"[{account.name}] send error: {e}")
        draft.status = "failed"
        session.add(SendLog(
            draft_id=draft.id, account_id=account.id,
            success=False, error=str(e)[:200], kind="message",
        ))
        return "skip", None


async def _mark_account_flood(account_id: int, seconds: int) -> None:
    async with async_session() as session:
        acc = await session.get(Account, account_id)
        if acc:
            acc.flood_until = datetime.utcnow() + timedelta(seconds=seconds)
            acc.status = "flood"
            acc.status_reason = f"FloodWait {seconds}s"
            await session.commit()


async def _mark_account_banned(account_id: int, reason: str) -> None:
    async with async_session() as session:
        acc = await session.get(Account, account_id)
        if acc:
            acc.status = "banned"
            acc.status_reason = reason
            acc.enabled = False
            await session.commit()


async def _account_in_flood(session, account: Account) -> bool:
    if account.flood_until and account.flood_until > datetime.utcnow():
        return True
    return False


# ============================================================================
# Главный цикл воркера
# ============================================================================

# Кеш подключённых клиентов на воркер
_clients: dict[int, TelegramClient] = {}


async def _get_client(account: Account) -> TelegramClient | None:
    """Lazy connect + caching."""
    if account.id in _clients:
        return _clients[account.id]
    try:
        client = build_client(account)
        await client.connect()
        if not await client.is_user_authorized():
            logger.error(f"[{account.name}] не авторизован — пропускаю")
            await client.disconnect()
            return None
        _clients[account.id] = client
        logger.info(f"[{account.name}] подключён")
        return client
    except Exception as e:
        logger.error(f"[{account.name}] connect error: {e}")
        return None


async def _close_all_clients() -> None:
    for client in _clients.values():
        try:
            await client.disconnect()
        except Exception:
            pass
    _clients.clear()


async def run(shard_id: int) -> None:
    await init_db()
    shard_count = settings.worker_count
    logger.info(f"Sender shard={shard_id}/{shard_count} started")

    try:
        while True:
            # Active hours window
            if not is_active_now():
                wait = seconds_until_active()
                wait = min(wait, 3600)  # просыпаемся раз в час чтобы проверить
                logger.info(f"Вне active-окна, сплю {wait}s")
                await asyncio.sleep(wait)
                continue

            async with async_session() as session:
                row = await claim_next_draft(session, shard_id, shard_count)
                if row is None:
                    await session.commit()
                    logger.info("Очередь пустая, сплю 5 мин")
                    await asyncio.sleep(300)
                    continue

                draft, contact, account = row

                # Аккаунт в flood-cooldown?
                if await _account_in_flood(session, account):
                    await session.commit()  # release lock
                    await asyncio.sleep(30)
                    continue

                # Контакт в blocklist?
                blocked = (await session.execute(
                    select(BlockedContact).where(
                        BlockedContact.tg_user_id == contact.tg_user_id
                    )
                )).scalar_one_or_none()
                if blocked:
                    draft.status = "rejected"
                    await session.commit()
                    continue

                # Carrier capacity
                ok, reason = await _check_carrier_capacity(session, account)
                if not ok:
                    logger.info(f"[{account.name}] {reason}")
                    await session.commit()  # release lock без изменения draft
                    await asyncio.sleep(60)
                    continue

                # Подключить telethon-клиент
                client = await _get_client(account)
                if client is None:
                    draft.status = "failed"
                    await session.commit()
                    continue

                # Отправка
                status, extra = await send_one(
                    client, account, contact, draft, session,
                )
                await session.commit()

                if status == "sent":
                    delay = random.randint(
                        settings.min_delay_seconds, settings.max_delay_seconds,
                    )
                    logger.info(f"[{account.name}] следующая через {delay}s")
                    await asyncio.sleep(delay)
                elif status == "flood":
                    await _mark_account_flood(account.id, extra)
                    await asyncio.sleep(5)
                elif status == "banned":
                    await _mark_account_banned(account.id, "PeerFlood")
                else:
                    await asyncio.sleep(15)

    finally:
        await _close_all_clients()
        logger.info(f"Sender shard={shard_id} остановлен")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.shard))


if __name__ == "__main__":
    main()

"""
Sender-воркер: обрабатывает свой шард аккаунтов.

Account-centric pattern для масштаба до 10к аккаунтов:
- Воркер обрабатывает SHARD_ID аккаунтов (account.id % WORKER_COUNT == shard)
- Внутри воркера до SENDER_PARALLEL_ACCOUNTS параллельных задач, каждая
  работает с ОДНИМ аккаунтом за раз: подключается → шлёт все его pending
  драфты подряд (с паузами 30-90s) → отключается → берёт следующий
- Один воркер может вести несколько шардов: --shards 0-9 или --shards 5

Зачем account-centric:
- Каждый коннект используется по полной (не connect/disconnect на каждое
  сообщение), но и не висит вечно — освобождает память
- 100 шард × 20 параллельных = 2000 одновременных коннектов суммарно
  (40-80 GB RAM) при 10к аккаунтов в БД

Запуск:
    python -m src.sender --shards 0         # один шард
    python -m src.sender --shards 0-9       # шарды 0,1,2,3,4,5,6,7,8,9
    python -m src.sender --shards 0,3,7     # конкретные шарды
"""
import argparse
import asyncio
import random
import re
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
from .models import Account, BlockedContact, Contact, Draft, SendLog, ShardHeartbeat
from .telethon_client import build_client
from .timewindow import is_active_now, seconds_until_active


# Heartbeat: каждый шард пишет свой статус в БД каждые ~60 сек
async def _heartbeat(shard_id: int, sent_total: int, note: str = "") -> None:
    try:
        async with async_session() as session:
            hb = await session.get(ShardHeartbeat, shard_id)
            now = datetime.utcnow()
            if hb is None:
                session.add(ShardHeartbeat(
                    shard_id=shard_id, last_beat=now,
                    sent_count_total=sent_total, note=note,
                ))
            else:
                hb.last_beat = now
                hb.sent_count_total = sent_total
                hb.note = note
            await session.commit()
    except Exception as e:
        logger.debug(f"heartbeat error: {e}")


# ============================================================================
# Carrier-aware лимиты
# ============================================================================

async def _check_carrier_capacity(session, account: Account) -> tuple[bool, str]:
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
# Получение работы
# ============================================================================

async def get_shard_accounts_with_pending(shard_id: int, shard_count: int) -> list[Account]:
    """Аккаунты в моём шарде у которых есть pending драфты."""
    async with async_session() as session:
        q = (
            select(Account)
            .join(Draft, Draft.account_id == Account.id)
            .where(Account.enabled.is_(True))
            .where(Account.status == "active")
            .where(Draft.status == "pending")
            .where((Account.id % shard_count) == shard_id)
            .group_by(Account.id)
            .order_by(Account.id)
        )
        return list((await session.execute(q)).scalars().all())


async def get_next_pending_draft_for_account(
    account: Account,
) -> tuple[Draft, Contact] | None:
    """Берёт следующий pending draft этого аккаунта (SKIP LOCKED)."""
    async with async_session() as session:
        q = (
            select(Draft, Contact)
            .join(Contact, Draft.contact_id == Contact.id)
            .where(Draft.account_id == account.id)
            .where(Draft.status == "pending")
            .order_by(desc(Contact.last_msg_at), Draft.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True, of=Draft)
        )
        row = (await session.execute(q)).first()
        if not row:
            return None
        draft, contact = row

        # Проверка blocklist
        blocked = (await session.execute(
            select(BlockedContact)
            .where(BlockedContact.tg_user_id == contact.tg_user_id)
        )).scalar_one_or_none()
        if blocked:
            draft.status = "rejected"
            await session.commit()
            # Рекурсивно следующий
            return await get_next_pending_draft_for_account(account)

        # Carrier capacity
        ok, reason = await _check_carrier_capacity(session, account)
        if not ok:
            logger.info(f"[{account.name}] {reason}")
            await session.commit()  # release lock без изменения
            return None  # ждём окно

        # Возвращаем — основная транзакция продолжит после
        # (драфт остаётся pending пока send не сделает его sent/failed)
        await session.commit()
        return draft, contact


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
) -> tuple[str, Optional[int]]:
    name = contact.first_name or contact.username or str(contact.tg_user_id)
    has_attachment = bool(draft.attachment_kind and draft.attachment_ref)

    async with async_session() as session:
        # Перезагружаем draft в новой сессии чтобы записать статус
        draft = await session.get(Draft, draft.id)
        if draft is None or draft.status != "pending":
            return "skip", None  # кто-то другой уже взял
        try:
            await client.send_message(contact.tg_user_id, draft.text, link_preview=True)
            logger.success(f"[{account.name}] text → {name}")
            session.add(SendLog(
                draft_id=draft.id, account_id=account.id,
                success=True, kind="message",
            ))

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
            acc_in_session = await session.get(Account, account.id)
            if acc_in_session:
                acc_in_session.last_send_at = datetime.utcnow()
            await session.commit()
            return "sent", None

        except FloodWaitError as e:
            wait = int(e.seconds * 1.15)
            logger.warning(f"[{account.name}] FloodWait {wait}s")
            await session.rollback()
            return "flood", wait

        except PeerFloodError:
            logger.critical(f"[{account.name}] PeerFloodError — аккаунт сжёгся")
            draft.status = "failed"
            session.add(SendLog(
                draft_id=draft.id, account_id=account.id,
                success=False, error="peer_flood", kind="message",
            ))
            await session.commit()
            return "banned", None

        except (UserPrivacyRestrictedError, ChatWriteForbiddenError) as e:
            logger.info(f"[{account.name}] privacy/forbidden для {name}")
            draft.status = "failed"
            session.add(SendLog(
                draft_id=draft.id, account_id=account.id,
                success=False, error=e.__class__.__name__, kind="message",
            ))
            await session.commit()
            return "skip", None

        except Exception as e:
            logger.error(f"[{account.name}] send error: {e}")
            draft.status = "failed"
            session.add(SendLog(
                draft_id=draft.id, account_id=account.id,
                success=False, error=str(e)[:200], kind="message",
            ))
            await session.commit()
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


# ============================================================================
# Account-centric worker (одна async-задача — один аккаунт за раз)
# ============================================================================

async def process_account(account: Account, sem: asyncio.Semaphore) -> None:
    """
    Подключается к аккаунту → шлёт все его pending драфты подряд → disconnect.
    Между сообщениями этого аккаунта: пауза MIN_DELAY..MAX_DELAY.
    """
    async with sem:
        # Если аккаунт в flood-cooldown — пропускаем сразу
        async with async_session() as session:
            fresh = await session.get(Account, account.id)
            if not fresh or fresh.flood_until and fresh.flood_until > datetime.utcnow():
                return
            account = fresh

        client = build_client(account)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning(f"[{account.name}] не авторизован, skip")
                return

            sent_count = 0
            while True:
                # Active hours check
                if not is_active_now():
                    logger.debug(f"[{account.name}] вне active-окна")
                    break

                row = await get_next_pending_draft_for_account(account)
                if row is None:
                    break  # очередь этого аккаунта пуста

                draft, contact = row
                status, extra = await send_one(client, account, contact, draft)

                if status == "sent":
                    sent_count += 1
                    delay = random.randint(
                        settings.min_delay_seconds, settings.max_delay_seconds,
                    )
                    logger.debug(f"[{account.name}] next через {delay}s")
                    await asyncio.sleep(delay)
                elif status == "flood":
                    await _mark_account_flood(account.id, extra)
                    break  # из этого аккаунта выходим, другие могут работать
                elif status == "banned":
                    await _mark_account_banned(account.id, "PeerFlood")
                    break
                else:  # skip
                    await asyncio.sleep(2)

            if sent_count > 0:
                logger.info(f"[{account.name}] цикл завершён, отправлено {sent_count}")
        except Exception as e:
            logger.error(f"[{account.name}] worker error: {e}")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass


async def run_shard(shard_id: int, shard_count: int) -> None:
    """Главный цикл одного шарда."""
    logger.info(
        f"Shard {shard_id}/{shard_count} started "
        f"(parallel={settings.sender_parallel_accounts})"
    )
    sem = asyncio.Semaphore(settings.sender_parallel_accounts)
    sent_total = 0

    # Heartbeat task — пишет в БД раз в 60 сек
    async def _hb_loop():
        while True:
            await _heartbeat(shard_id, sent_total, note="active")
            await asyncio.sleep(60)

    hb_task = asyncio.create_task(_hb_loop())

    try:
        while True:
            try:
                if not is_active_now():
                    wait = min(seconds_until_active(), 3600)
                    logger.info(f"shard={shard_id}: вне active, сплю {wait}s")
                    await _heartbeat(shard_id, sent_total, note="sleeping")
                    await asyncio.sleep(wait)
                    continue

                accounts = await get_shard_accounts_with_pending(shard_id, shard_count)
                if not accounts:
                    logger.debug(f"shard={shard_id}: очередь пуста")
                    await asyncio.sleep(120)
                    continue

                logger.info(
                    f"shard={shard_id}: {len(accounts)} аккаунтов c pending драфтами"
                )

                # Подсчёт sent до и после батча — для heartbeat метрик
                from sqlalchemy import func as _func
                async with async_session() as _s:
                    before = (await _s.execute(
                        select(_func.count(SendLog.id))
                        .where(SendLog.success.is_(True))
                        .where((SendLog.account_id % shard_count) == shard_id)
                    )).scalar() or 0

                tasks = [
                    asyncio.create_task(process_account(acc, sem))
                    for acc in accounts
                ]
                await asyncio.gather(*tasks, return_exceptions=True)

                async with async_session() as _s:
                    after = (await _s.execute(
                        select(_func.count(SendLog.id))
                        .where(SendLog.success.is_(True))
                        .where((SendLog.account_id % shard_count) == shard_id)
                    )).scalar() or 0
                sent_total = after
                logger.info(
                    f"shard={shard_id}: цикл завершён, +{after - before} sends "
                    f"(total {after})"
                )
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"shard={shard_id} loop error: {e}")
                await asyncio.sleep(60)
    finally:
        hb_task.cancel()
        try:
            await hb_task
        except (asyncio.CancelledError, Exception):
            pass


# ============================================================================
# Entry point — поддерживает несколько шардов в одном процессе
# ============================================================================

def parse_shards(spec: str) -> list[int]:
    """
    '0' → [0]
    '0-9' → [0,1,2,...,9]
    '0,3,7' → [0,3,7]
    """
    result = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            result.extend(range(int(a), int(b) + 1))
        else:
            result.append(int(part))
    return result


async def run(shards: list[int]) -> None:
    await init_db()
    shard_count = settings.worker_count

    for s in shards:
        if not (0 <= s < shard_count):
            raise SystemExit(
                f"shard {s} вне диапазона [0, {shard_count}). "
                f"Проверь WORKER_COUNT в .env"
            )

    logger.info(
        f"Sender process started: shards {shards} из {shard_count} всего"
    )

    tasks = [run_shard(s, shard_count) for s in shards]
    await asyncio.gather(*tasks)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--shards",
        type=str,
        required=True,
        help='формат: "0" / "0-9" / "0,3,7"',
    )
    args = parser.parse_args()
    shards = parse_shards(args.shards)
    asyncio.run(run(shards))


if __name__ == "__main__":
    main()

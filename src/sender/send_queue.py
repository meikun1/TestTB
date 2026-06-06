"""
Шардированный sender-воркер.

Каждый процесс держит подмножество пула: account.id % WORKER_COUNT == SHARD_ID.
Запуск: python -m src.sender.send_queue --shard 0 --mode auto

Логика отправки:
  1) Достаём драфт из БД (SKIP LOCKED для безопасной конкуренции воркеров)
  2) Берём sticky-аккаунт. Если он не в этом шарде — пропускаем (другой воркер возьмёт)
  3) Pre-send check (safety)
  4) Отправляем текст
  5) Если есть вложение:
       attachment_delay_seconds == 0 → файл отдельным сообщением сразу после
                                       (с задержкой по дефолту из настроек)
       attachment_delay_seconds > 0  → ждём ровно столько и отправляем
  6) Засыпаем перед следующим контактом
"""
import argparse
import asyncio
import random
from datetime import datetime
from typing import Optional

from loguru import logger
from sqlalchemy import select, desc, update
from telethon.errors import (
    FloodWaitError, PeerFloodError, UserPrivacyRestrictedError, ChatWriteForbiddenError,
)

from config import settings
from src.accounts import AccountPool, Dispatcher, AccountUnavailable
from src.utils import async_session, Account, Contact, Draft, SendLog
from .safety import pre_send_check, is_sleeping_hours, is_working_hours
from .attachments import send_attachment


async def claim_next_draft(
    session, shard_id: int | None, shard_count: int
) -> tuple[Draft, Contact] | None:
    """
    Берёт следующий драфт из очереди и атомарно ставит ему claim_at,
    чтобы параллельный воркер не схватил тот же. SELECT ... FOR UPDATE SKIP LOCKED.

    Для шардирования фильтруем по account_id % shard_count.
    """
    q = (
        select(Draft, Contact)
        .join(Contact, Draft.contact_id == Contact.id)
        .where(Draft.status.in_(["approved", "pending"]))
        .where(Contact.category.in_(["warm", "cooling"]))
    )
    if shard_id is not None:
        # postgres-only: Draft.account_id % shard_count == shard_id
        q = q.where((Draft.account_id % shard_count) == shard_id)

    q = q.order_by(desc(Contact.score), Draft.created_at.asc()).limit(20)

    # SKIP LOCKED работает только на Postgres
    if settings.use_postgres:
        q = q.with_for_update(skip_locked=True)

    rows = (await session.execute(q)).all()
    if not rows:
        return None

    # имитация: не всегда берём первого
    if len(rows) > 1 and random.random() < settings.skip_probability:
        idx = random.randint(1, min(len(rows) - 1, 4))
    else:
        idx = 0

    draft, contact = rows[idx]
    # помечаем «в работе»: статус остаётся pending/approved до фактической отправки,
    # но обновим updated_at контакта чтобы другой воркер не схватил тот же контекст
    # (опционально; основной анти-race — SKIP LOCKED)
    return draft, contact


async def simulate_human_behavior(client, contact: Contact, text: str) -> None:
    try:
        if settings.read_incoming_before_send:
            await client.send_read_acknowledge(contact.tg_user_id)
            await asyncio.sleep(random.uniform(2, 8))
        if settings.simulate_typing:
            typing_time = min(
                len(text) * settings.typing_seconds_per_char + random.uniform(2, 5),
                settings.typing_max_seconds,
            )
            async with client.action(contact.tg_user_id, "typing"):
                await asyncio.sleep(typing_time)
    except Exception as e:
        logger.debug(f"Behavior sim failed: {e}")


async def _attachment_delay(draft: Draft) -> int:
    delay = draft.attachment_delay_seconds or 0
    if delay > 0:
        return delay
    return random.randint(
        settings.attachment_default_delay_min,
        settings.attachment_default_delay_max,
    )


async def send_draft(
    client, account: Account, contact: Contact, draft: Draft, session
) -> tuple[str, Optional[int]]:
    """
    Отправляет текст драфта + (опционально) вложение.
    Возвращает (status, extra_seconds):
        ('sent', None)         — успех
        ('flood', seconds)     — FloodWait, аккаунту отдых
        ('banned', None)       — PeerFlood / privacy ужесточил
        ('skip', None)         — единичная ошибка, продолжаем
    """
    name = contact.first_name or contact.username or str(contact.tg_user_id)
    has_attachment = bool(draft.attachment_kind and draft.attachment_ref)

    try:
        # 1) Имитация и отправка текста
        await simulate_human_behavior(client, contact, draft.text)
        await client.send_message(contact.tg_user_id, draft.text, link_preview=True)
        logger.success(f"[{account.name}] text → {name}")
        session.add(SendLog(
            draft_id=draft.id, account_id=account.id,
            success=True, kind="message",
        ))

        # 2) Вложение
        if has_attachment:
            delay = await _attachment_delay(draft)
            logger.info(
                f"[{account.name}] вложение через {delay}s ({draft.attachment_kind})"
            )
            await asyncio.sleep(delay)
            try:
                await send_attachment(
                    client, contact, draft,
                    caption=draft.attachment_caption,
                )
                logger.success(
                    f"[{account.name}] attachment ({draft.attachment_kind}) → {name}"
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
        return "sent", None

    except FloodWaitError as e:
        wait = int(e.seconds * 1.15)
        logger.warning(f"[{account.name}] FloodWait {wait}s")
        return "flood", wait
    except PeerFloodError:
        logger.critical(f"[{account.name}] PeerFloodError — аккаунт горит")
        draft.status = "failed"
        session.add(SendLog(
            draft_id=draft.id, account_id=account.id,
            success=False, error="peer_flood", kind="message",
        ))
        return "banned", None
    except (UserPrivacyRestrictedError, ChatWriteForbiddenError) as e:
        logger.info(f"[{account.name}] privacy/forbidden для {name}: {e.__class__.__name__}")
        draft.status = "failed"
        session.add(SendLog(
            draft_id=draft.id, account_id=account.id,
            success=False, error="privacy", kind="message",
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


def compute_next_delay() -> int:
    base = random.randint(settings.min_delay_seconds, settings.max_delay_seconds)
    hour = datetime.now().hour
    if 18 <= hour < 22:
        base = int(base * 0.7)
    jitter = random.uniform(0.8, 1.2)
    return max(int(base * jitter), settings.min_delay_seconds // 2)


async def run(mode: str, shard_id: int | None) -> None:
    shard_count = settings.worker_count
    pool = AccountPool(shard_id=shard_id, shard_count=shard_count)
    await pool.load()

    if not pool.active_ids():
        logger.error(
            f"Шард {shard_id}: ни одного активного аккаунта. "
            f"Спим 60s и пробуем снова."
        )
        # ждём через тот же refresh loop — может intake API подкинет
        pool.start_background_refresh()
        while not pool.active_ids():
            await asyncio.sleep(60)

    pool.start_background_refresh()
    dispatcher = Dispatcher(pool)
    logger.info(
        f"Sender shard={shard_id}/{shard_count} запущен в '{mode}', "
        f"{len(pool.active_ids())} аккаунтов в локальном пуле"
    )

    try:
        while True:
            now = datetime.now()
            if is_sleeping_hours(now):
                logger.info("Sleeping hours — 30 мин")
                await asyncio.sleep(1800)
                continue
            if not is_working_hours(now):
                logger.info("Вне окна — 15 мин")
                await asyncio.sleep(900)
                continue

            async with async_session() as session:
                row = await claim_next_draft(session, shard_id, shard_count)
                if row is None:
                    await asyncio.sleep(300)
                    continue

                draft, contact = row

                # draft.account вне нашего пула? значит другой шард его обработает
                if not pool.get_account(draft.account_id):
                    if shard_id is None or draft.account_id % shard_count == shard_id:
                        # наш шард, но аккаунт сейчас не в пуле (banned/disabled)
                        logger.warning(
                            f"Draft #{draft.id}: account_id={draft.account_id} "
                            f"вне пула, reject"
                        )
                        draft.status = "rejected"
                        await session.commit()
                    # иначе — не наш шард, не трогаем
                    await asyncio.sleep(2)
                    continue

                try:
                    account = await dispatcher.pick(
                        preferred_account_id=draft.account_id
                    )
                except AccountUnavailable:
                    await asyncio.sleep(60)
                    continue

                if account.id != draft.account_id:
                    # sticky-аккаунт занят — ждём
                    await asyncio.sleep(30)
                    continue

                ok, reason = await pre_send_check(session, account, contact, draft)
                if not ok:
                    logger.info(
                        f"[{account.name}] skip {contact.first_name}: {reason}"
                    )
                    if reason.startswith("daily_limit"):
                        await session.commit()
                        await asyncio.sleep(1800)
                        continue
                    if reason.startswith("low_reply_rate"):
                        await session.commit()
                        await asyncio.sleep(6 * 3600)
                        continue
                    if reason in ("stranger", "too_recent", "duplicate_text") \
                            or reason.startswith("draft_invalid") \
                            or reason.startswith("category"):
                        draft.status = "rejected"
                        await session.commit()
                        continue
                    await session.commit()
                    await asyncio.sleep(300)
                    continue

                if mode == "review":
                    name = contact.first_name or contact.username
                    print(f"\n[{account.name}] → {name} ({contact.category}, score={contact.score})")
                    print(f"  attach: {draft.attachment_kind or '—'}")
                    print(f"  {draft.text}\n")
                    action = input("[s]end / [k]skip / [q]uit: ").strip().lower()
                    if action == "q":
                        break
                    if action == "k":
                        draft.status = "rejected"
                        await session.commit()
                        continue
                    if action != "s":
                        await session.commit()
                        continue
                elif mode == "auto" and draft.status != "approved" \
                        and not settings.use_local_model:
                    await asyncio.sleep(60)
                    continue

                client = pool.get_client(account.id)
                status, extra = await send_draft(
                    client, account, contact, draft, session
                )
                await session.commit()

                if status == "sent":
                    await dispatcher.mark_sent(account)
                    delay = compute_next_delay()
                    logger.info(f"[{account.name}] next send in {delay}s")
                    await asyncio.sleep(delay)
                elif status == "flood":
                    await dispatcher.mark_flood(account, extra)
                    await asyncio.sleep(5)
                elif status == "banned":
                    await dispatcher.mark_banned(account, "PeerFlood")
                    if not pool.active_ids():
                        logger.critical("Все аккаунты шарда выбиты — спим 5 мин")
                        await asyncio.sleep(300)
                else:
                    await asyncio.sleep(30)
    finally:
        await pool.close()
        logger.info(f"Sender shard={shard_id} остановлен")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["review", "auto"], default="auto")
    parser.add_argument(
        "--shard", type=int, default=None,
        help="ID шарда (0..WORKER_COUNT-1). Не задан → один процесс на весь пул"
    )
    args = parser.parse_args()
    asyncio.run(run(args.mode, args.shard))

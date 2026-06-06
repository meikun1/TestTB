"""
Отправка сообщений с антиспам-логикой.

Режимы:
  --mode review    — спрашивает подтверждение перед каждой отправкой
  --mode auto      — автоматически отправляет approved черновики

Правила безопасности:
  - не больше MAX_MESSAGES_PER_DAY в сутки
  - случайные паузы MIN_DELAY..MAX_DELAY секунд между отправками
  - только в окно WORKING_HOURS_START..END
  - если день кончился — ждёт следующее окно
"""
import argparse
import asyncio
import random
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select, func

from config import settings
from src.fetcher.fetch_dialogs import make_client
from src.utils import async_session, Contact, Draft, SendLog


def is_within_working_hours(now: datetime) -> bool:
    return settings.working_hours_start <= now.hour < settings.working_hours_end


async def sent_today(session) -> int:
    since = datetime.utcnow() - timedelta(hours=24)
    result = await session.execute(
        select(func.count(SendLog.id))
        .where(SendLog.sent_at >= since)
        .where(SendLog.success.is_(True))
    )
    return result.scalar() or 0


async def get_next_pending(session) -> tuple[Draft, Contact] | None:
    """Берём следующий approved (в auto) или pending (в review) черновик."""
    result = await session.execute(
        select(Draft, Contact)
        .join(Contact, Draft.contact_id == Contact.id)
        .where(Draft.status.in_(["approved", "pending"]))
        .order_by(Contact.score.desc(), Draft.created_at.asc())
        .limit(1)
    )
    row = result.first()
    return row if row else None


def show_for_review(contact: Contact, draft: Draft) -> str:
    name = contact.first_name or contact.username or str(contact.tg_user_id)
    print("\n" + "=" * 60)
    print(f"Контакт: {name}  (score {contact.score}, {contact.category})")
    print(f"Вариант: {draft.variant_label}")
    print("-" * 60)
    print(draft.text)
    print("=" * 60)
    return input("Действие [s=send / k=skip / e=edit / q=quit]: ").strip().lower()


async def send_one(client, contact: Contact, draft: Draft, session) -> bool:
    try:
        await client.send_message(contact.tg_user_id, draft.text)
        draft.status = "sent"
        draft.sent_at = datetime.utcnow()
        session.add(SendLog(draft_id=draft.id, success=True))
        logger.success(f"Sent to {contact.first_name or contact.username}")
        return True
    except Exception as e:
        draft.status = "failed"
        session.add(SendLog(draft_id=draft.id, success=False, error=str(e)))
        logger.error(f"Failed to send: {e}")
        return False


async def run(mode: str) -> None:
    client = make_client()
    await client.start(phone=settings.tg_phone)
    logger.info(f"Sender started in '{mode}' mode")

    while True:
        async with async_session() as session:
            # Лимит на день
            today_count = await sent_today(session)
            if today_count >= settings.max_messages_per_day:
                logger.warning(
                    f"Daily limit reached ({today_count}/{settings.max_messages_per_day}). "
                    "Sleeping 1h."
                )
                await asyncio.sleep(3600)
                continue

            # Рабочие часы
            if not is_within_working_hours(datetime.now()):
                logger.info("Outside working hours, sleeping 15 min")
                await asyncio.sleep(900)
                continue

            row = await get_next_pending(session)
            if row is None:
                logger.info("No pending drafts. Sleeping 5 min.")
                await asyncio.sleep(300)
                continue

            draft, contact = row

            if mode == "review":
                action = show_for_review(contact, draft)
                if action == "q":
                    break
                if action == "k":
                    draft.status = "rejected"
                    await session.commit()
                    continue
                if action == "e":
                    new_text = input("Новый текст (Enter — отмена): ")
                    if new_text.strip():
                        draft.text = new_text
                if action not in ("s", "e"):
                    continue
            elif mode == "auto" and draft.status != "approved":
                # В auto-режиме отправляем только то, что явно одобрено
                await asyncio.sleep(60)
                continue

            success = await send_one(client, contact, draft, session)
            await session.commit()

            if success:
                delay = random.randint(
                    settings.min_delay_seconds, settings.max_delay_seconds
                )
                logger.info(f"Sleeping {delay}s before next send")
                await asyncio.sleep(delay)

    await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["review", "auto"], default="review")
    args = parser.parse_args()
    asyncio.run(run(args.mode))

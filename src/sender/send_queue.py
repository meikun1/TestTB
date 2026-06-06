"""
Отправка сообщений с защитой от антиспам-триггеров Telegram.

Имитирует человеческое поведение:
  - читает входящие перед тем как писать
  - "печатает" с реалистичной скоростью
  - иногда пропускает кандидатов из очереди
  - спит в неактивные часы
  - падает на FloodWait/PeerFlood с правильной реакцией

Запуск:
  python -m src.sender.send_queue --mode review
  python -m src.sender.send_queue --mode auto
"""
import argparse
import asyncio
import random
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select, desc
from telethon.errors import FloodWaitError, PeerFloodError, UserPrivacyRestrictedError
from telethon import functions, types

from config import settings
from src.fetcher.fetch_dialogs import make_client
from src.utils import async_session, Contact, Draft, SendLog
from .safety import (
    pre_send_check,
    is_sleeping_hours,
    is_working_hours,
    get_daily_limit,
    get_first_send_date,
    count_sent_in_window,
)


# Глобальный аварийный стоп — устанавливается при PeerFlood
EMERGENCY_STOP_UNTIL: datetime | None = None


async def get_next_candidate(session) -> tuple[Draft, Contact] | None:
    """Очередь: warm + cooling, отсортирована по score."""
    result = await session.execute(
        select(Draft, Contact)
        .join(Contact, Draft.contact_id == Contact.id)
        .where(Draft.status.in_(["approved", "pending"]))
        .where(Contact.category.in_(["warm", "cooling"]))
        .order_by(desc(Contact.score), Draft.created_at.asc())
        .limit(20)
    )
    rows = result.all()
    if not rows:
        return None

    # С вероятностью skip_probability пропускаем первого — имитация
    # того что человек не пишет всем подряд в идеальном порядке
    if len(rows) > 1 and random.random() < settings.skip_probability:
        return rows[random.randint(1, min(len(rows) - 1, 4))]
    return rows[0]


async def simulate_human_behavior(client, contact: Contact, text: str) -> None:
    """Перед отправкой: открыть диалог, прочитать входящие, попечатать."""
    try:
        # 1) Зайти в диалог — Telegram видит, что мы его открыли
        if settings.read_incoming_before_send:
            await client.send_read_acknowledge(contact.tg_user_id)
            await asyncio.sleep(random.uniform(2, 8))

        # 2) Имитация набора
        if settings.simulate_typing:
            typing_time = min(
                len(text) * settings.typing_seconds_per_char + random.uniform(2, 5),
                settings.typing_max_seconds,
            )
            async with client.action(contact.tg_user_id, "typing"):
                await asyncio.sleep(typing_time)
    except Exception as e:
        # Не критично — если не получилось имитировать, просто шлём
        logger.debug(f"Behavior sim failed: {e}")


async def handle_send_error(e: Exception, draft: Draft, session) -> str:
    """Возвращает действие: 'stop' | 'wait:N' | 'skip'."""
    global EMERGENCY_STOP_UNTIL

    if isinstance(e, PeerFloodError):
        # Самый плохой сигнал — Telegram уже считает нас спамом
        EMERGENCY_STOP_UNTIL = datetime.utcnow() + timedelta(days=2)
        logger.critical(
            "PeerFloodError! Telegram ограничил отправку незнакомцам. "
            "Останавливаюсь на 48 часов."
        )
        draft.status = "failed"
        session.add(SendLog(draft_id=draft.id, success=False, error="peer_flood"))
        return "stop"

    if isinstance(e, FloodWaitError):
        wait = int(e.seconds * 1.15)  # +15% запас
        logger.warning(f"FloodWait: ждём {wait}s ({e.seconds}s + 15%)")
        return f"wait:{wait}"

    if isinstance(e, UserPrivacyRestrictedError):
        logger.info("Контакт ограничил приём — пропускаем")
        draft.status = "failed"
        session.add(SendLog(draft_id=draft.id, success=False, error="privacy"))
        return "skip"

    logger.error(f"Неизвестная ошибка отправки: {e}")
    draft.status = "failed"
    session.add(SendLog(draft_id=draft.id, success=False, error=str(e)[:200]))
    return "skip"


async def send_message(client, contact: Contact, draft: Draft, session) -> str:
    """Возвращает 'sent' | 'stop' | 'wait:N' | 'skip'."""
    try:
        await simulate_human_behavior(client, contact, draft.text)
        await client.send_message(contact.tg_user_id, draft.text)
        draft.status = "sent"
        draft.sent_at = datetime.utcnow()
        session.add(SendLog(draft_id=draft.id, success=True))
        name = contact.first_name or contact.username or str(contact.tg_user_id)
        logger.success(f"Отправлено → {name}")
        return "sent"
    except Exception as e:
        return await handle_send_error(e, draft, session)


def compute_next_delay() -> int:
    """
    Время до следующей отправки. Не равномерное, а с уклоном к большим
    паузам днём и меньшим вечером (когда люди реально активнее в мессенджерах).
    """
    base = random.randint(settings.min_delay_seconds, settings.max_delay_seconds)

    # Вечером (18-22) паузы чуть короче — пиковое время в мессенджерах
    hour = datetime.now().hour
    if 18 <= hour < 22:
        base = int(base * 0.7)

    # +/- 20% случайного шума
    jitter = random.uniform(0.8, 1.2)
    return max(int(base * jitter), settings.min_delay_seconds // 2)


def show_for_review(contact: Contact, draft: Draft) -> str:
    name = contact.first_name or contact.username or str(contact.tg_user_id)
    print("\n" + "=" * 60)
    print(f"Контакт: {name}  (score {contact.score}, {contact.category})")
    print(f"Вариант: {draft.variant_label}")
    print("-" * 60)
    print(draft.text)
    print("=" * 60)
    return input("Действие [s=send / k=skip / e=edit / q=quit]: ").strip().lower()


async def run(mode: str) -> None:
    global EMERGENCY_STOP_UNTIL

    client = make_client()
    await client.start(phone=settings.tg_phone)
    logger.info(f"Sender запущен в режиме '{mode}'")

    while True:
        # Аварийный стоп
        if EMERGENCY_STOP_UNTIL and datetime.utcnow() < EMERGENCY_STOP_UNTIL:
            wait = (EMERGENCY_STOP_UNTIL - datetime.utcnow()).total_seconds()
            logger.warning(f"Emergency stop ещё {int(wait)}s")
            await asyncio.sleep(min(wait, 3600))
            continue

        async with async_session() as session:
            now = datetime.now()

            # Жёсткие временные окна
            if is_sleeping_hours(now):
                logger.info("Sleeping hours, сплю 30 мин")
                await asyncio.sleep(1800)
                continue
            if not is_working_hours(now):
                logger.info("Вне рабочего окна, сплю 15 мин")
                await asyncio.sleep(900)
                continue

            row = await get_next_candidate(session)
            if row is None:
                logger.info("Очередь пуста. Сплю 10 мин.")
                await asyncio.sleep(600)
                continue

            draft, contact = row

            # Все проверки безопасности
            ok, reason = await pre_send_check(session, contact, draft)
            if not ok:
                logger.info(f"Skip {contact.first_name}: {reason}")

                # Решаем что делать с этим черновиком
                if reason.startswith("daily_limit") or reason.startswith("hourly_limit"):
                    await session.commit()
                    await asyncio.sleep(1800)
                    continue
                if reason.startswith("low_reply_rate"):
                    logger.warning("Низкий reply rate — пауза 6 часов")
                    await session.commit()
                    await asyncio.sleep(6 * 3600)
                    continue
                if reason in ("stranger", "too_recent", "duplicate_text") or reason.startswith("draft_invalid") or reason.startswith("category"):
                    # Этот конкретный кандидат не подходит — отбрасываем и берём следующего
                    draft.status = "rejected"
                    await session.commit()
                    continue
                await session.commit()
                await asyncio.sleep(300)
                continue

            # Режим review — спросить пользователя
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
                    await session.commit()
                    continue
            elif mode == "auto" and draft.status != "approved" and not settings.use_local_model:
                # В auto-режиме без своей модели всё же требуем approved
                # (со своей моделью можно доверять pending — мы её контролируем)
                await asyncio.sleep(60)
                continue

            result = await send_message(client, contact, draft, session)
            await session.commit()

            if result == "sent":
                delay = compute_next_delay()
                logger.info(f"Следующая отправка через {delay}s")
                await asyncio.sleep(delay)
            elif result == "stop":
                break
            elif result.startswith("wait:"):
                await asyncio.sleep(int(result.split(":")[1]))
            else:  # skip
                await asyncio.sleep(60)

    await client.disconnect()
    logger.info("Sender остановлен")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["review", "auto"], default="review")
    args = parser.parse_args()
    asyncio.run(run(args.mode))

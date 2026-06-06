"""
Очередь отправки через пул аккаунтов.

Каждый драфт привязан к своему аккаунту (sticky). Диспетчер проверяет
дневной/часовой лимит конкретного аккаунта, его flood-cooldown и статус.
Если у назначенного аккаунта нет capacity — драфт ждёт следующей итерации.

Запуск:
  python -m src.sender.send_queue --mode review
  python -m src.sender.send_queue --mode auto
"""
import argparse
import asyncio
import random
from datetime import datetime

from loguru import logger
from sqlalchemy import select, desc
from telethon.errors import FloodWaitError, PeerFloodError, UserPrivacyRestrictedError

from config import settings
from src.accounts import AccountPool, Dispatcher, AccountUnavailable
from src.utils import async_session, Account, Contact, Draft, SendLog
from .safety import pre_send_check, is_sleeping_hours, is_working_hours


async def get_next_candidate(
    session, account_id: int | None = None
) -> tuple[Draft, Contact] | None:
    """Очередь по приоритету скоринга. Если задан account_id — только его."""
    q = (
        select(Draft, Contact)
        .join(Contact, Draft.contact_id == Contact.id)
        .where(Draft.status.in_(["approved", "pending"]))
        .where(Contact.category.in_(["warm", "cooling"]))
        .order_by(desc(Contact.score), Draft.created_at.asc())
        .limit(20)
    )
    if account_id is not None:
        q = q.where(Draft.account_id == account_id)
    rows = (await session.execute(q)).all()
    if not rows:
        return None

    if len(rows) > 1 and random.random() < settings.skip_probability:
        return rows[random.randint(1, min(len(rows) - 1, 4))]
    return rows[0]


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


async def send_message(
    client, account: Account, contact: Contact, draft: Draft, session
) -> tuple[str, int | None]:
    """
    Возвращает (status, extra):
      ('sent', None)         — успешно
      ('flood', seconds)     — FloodWaitError, аккаунту дать отдохнуть
      ('banned', None)       — PeerFlood / AuthKey: аккаунт сжёгся
      ('skip', None)         — приватность или прочая разовая ошибка
    """
    try:
        await simulate_human_behavior(client, contact, draft.text)
        await client.send_message(contact.tg_user_id, draft.text)
        draft.status = "sent"
        draft.sent_at = datetime.utcnow()
        session.add(SendLog(
            draft_id=draft.id,
            account_id=account.id,
            success=True,
        ))
        name = contact.first_name or contact.username or str(contact.tg_user_id)
        logger.success(f"[{account.name}] → {name}")
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
            success=False, error="peer_flood",
        ))
        return "banned", None
    except UserPrivacyRestrictedError:
        logger.info(f"[{account.name}] контакт закрыт privacy — пропуск")
        draft.status = "failed"
        session.add(SendLog(
            draft_id=draft.id, account_id=account.id,
            success=False, error="privacy",
        ))
        return "skip", None
    except Exception as e:
        logger.error(f"[{account.name}] send error: {e}")
        draft.status = "failed"
        session.add(SendLog(
            draft_id=draft.id, account_id=account.id,
            success=False, error=str(e)[:200],
        ))
        return "skip", None


def compute_next_delay() -> int:
    base = random.randint(settings.min_delay_seconds, settings.max_delay_seconds)
    hour = datetime.now().hour
    if 18 <= hour < 22:
        base = int(base * 0.7)
    jitter = random.uniform(0.8, 1.2)
    return max(int(base * jitter), settings.min_delay_seconds // 2)


def show_for_review(account: Account, contact: Contact, draft: Draft) -> str:
    name = contact.first_name or contact.username or str(contact.tg_user_id)
    print("\n" + "=" * 60)
    print(f"Аккаунт: {account.name}")
    print(f"Контакт: {name}  (score {contact.score}, {contact.category})")
    print(f"Вариант: {draft.variant_label}")
    print("-" * 60)
    print(draft.text)
    print("=" * 60)
    return input("Действие [s=send / k=skip / e=edit / q=quit]: ").strip().lower()


async def run(mode: str) -> None:
    pool = AccountPool()
    await pool.load()
    if not pool.active_ids():
        logger.error("Нет ни одного авторизованного аккаунта. "
                     "Запусти: python -m src.accounts.login --all")
        return

    pool.start_background_refresh()
    dispatcher = Dispatcher(pool)
    logger.info(f"Sender запущен в '{mode}' режиме, "
                f"{len(pool.active_ids())} аккаунтов в пуле")

    try:
        while True:
            now = datetime.now()
            if is_sleeping_hours(now):
                logger.info("Sleeping hours — сплю 30 мин")
                await asyncio.sleep(1800)
                continue
            if not is_working_hours(now):
                logger.info("Вне рабочего окна — сплю 15 мин")
                await asyncio.sleep(900)
                continue

            async with async_session() as session:
                row = await get_next_candidate(session)
                if row is None:
                    logger.info("Очередь пуста — сплю 10 мин")
                    await asyncio.sleep(600)
                    continue

                draft, contact = row

                # Если назначенный аккаунт уже не в пуле (забанили/disable) —
                # драфт некому отправить, выкидываем
                if not pool.get_account(draft.account_id):
                    logger.warning(
                        f"Draft #{draft.id}: account_id={draft.account_id} "
                        f"вне пула (banned/disabled), reject"
                    )
                    draft.status = "rejected"
                    await session.commit()
                    continue

                try:
                    account = await dispatcher.pick(
                        preferred_account_id=draft.account_id
                    )
                except AccountUnavailable as e:
                    logger.warning(f"Нет доступных аккаунтов: {e}. Сплю 20 мин")
                    await asyncio.sleep(1200)
                    continue

                # Если предпочтительный занят (flood/limit), а взяли другого —
                # драфт ждёт окна своего sticky-аккаунта
                if account.id != draft.account_id:
                    logger.debug(
                        f"Sticky-аккаунт {draft.account_id} занят, "
                        f"драфт #{draft.id} ждёт"
                    )
                    await asyncio.sleep(60)
                    continue

                ok, reason = await pre_send_check(session, account, contact, draft)
                if not ok:
                    logger.info(f"[{account.name}] skip {contact.first_name}: {reason}")
                    if reason.startswith("daily_limit"):
                        await session.commit()
                        await asyncio.sleep(1800)
                        continue
                    if reason.startswith("low_reply_rate"):
                        logger.warning(f"[{account.name}] reply rate низкий — пауза 6ч")
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
                    action = show_for_review(account, contact, draft)
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
                elif mode == "auto" and draft.status != "approved" \
                        and not settings.use_local_model:
                    await asyncio.sleep(60)
                    continue

                client = pool.get_client(account.id)
                status, extra = await send_message(
                    client, account, contact, draft, session
                )
                await session.commit()

                if status == "sent":
                    await dispatcher.mark_sent(account)
                    delay = compute_next_delay()
                    logger.info(f"[{account.name}] следующая отправка через {delay}s")
                    await asyncio.sleep(delay)
                elif status == "flood":
                    await dispatcher.mark_flood(account, extra)
                    # переходим к следующей итерации — диспетчер возьмёт другого
                    await asyncio.sleep(5)
                elif status == "banned":
                    await dispatcher.mark_banned(account, "PeerFlood")
                    if not pool.active_ids():
                        logger.critical("Все аккаунты сгорели — выхожу")
                        break
                else:
                    await asyncio.sleep(30)
    finally:
        await pool.close()
        logger.info("Sender остановлен")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["review", "auto"], default="review")
    args = parser.parse_args()
    asyncio.run(run(args.mode))

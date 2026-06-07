"""
Глобальный opt-out обработчик — polling-based для масштаба 1000+ аккаунтов.

Не держит 10к постоянных Telethon-соединений (это убило бы память).
Вместо этого: каждые OPTOUT_POLL_INTERVAL_SECONDS подключается к каждому
аккаунту, проверяет N свежих входящих сообщений, отключается.

Если найдено сообщение с STOP-фразой → tg_user_id отправителя в blocklist
(глобально, для всех акк).

Запуск:
    python -m src.optout --shards 0        — обрабатывать только свой шард
    python -m src.optout --shards 0-9      — несколько шард в процессе
    python -m src.optout --shards all      — все шарды (для малого парка)
"""
import argparse
import asyncio
import re
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select

from .config import settings
from .db import async_session, init_db
from .models import Account, BlockedContact
from .telethon_client import build_client


STOP_PATTERNS = re.compile(
    r"\b("
    r"stop|unsubscribe|отпиши(те)?|не\s*пиши(те)?|"
    r"прекрати(те)?|удали(те)?|отстань(те)?|"
    r"stoplist|неинтересно|отписка|"
    r"to'xta|kerakmas|yozma"
    r")\b",
    re.IGNORECASE,
)


async def _add_to_blocklist(tg_user_id: int, reason: str) -> bool:
    """Возвращает True если добавили (нового)."""
    async with async_session() as session:
        exists = (await session.execute(
            select(BlockedContact).where(BlockedContact.tg_user_id == tg_user_id)
        )).scalar_one_or_none()
        if exists:
            return False
        session.add(BlockedContact(
            tg_user_id=tg_user_id,
            reason=reason,
            blocked_at=datetime.utcnow(),
        ))
        await session.commit()
        logger.warning(f"[optout] user {tg_user_id} → blocklist: {reason}")
        return True


async def poll_account(account: Account, since_window_seconds: int) -> int:
    """
    Подключается к аккаунту, проверяет последние OPTOUT_MESSAGES_PER_POLL
    сообщений в диалогах с непрочитанными. Возвращает кол-во добавленных
    в blocklist.

    since_window_seconds — игнорируем сообщения старше этого порога.
    """
    since = datetime.utcnow() - timedelta(seconds=since_window_seconds)
    added = 0

    client = build_client(account)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return 0

        dialogs = await client.get_dialogs(limit=100)
        for dialog in dialogs:
            if not dialog.is_user:
                continue
            if dialog.unread_count == 0:
                continue

            async for msg in client.iter_messages(
                dialog, limit=settings.optout_messages_per_poll
            ):
                if not msg.text:
                    continue
                if msg.out:
                    continue
                msg_date = msg.date.replace(tzinfo=None)
                if msg_date < since:
                    break  # сообщения от старше окна — дальше можно не идти

                if STOP_PATTERNS.search(msg.text):
                    sender_id = msg.sender_id
                    if sender_id and await _add_to_blocklist(
                        sender_id,
                        f"poll:{account.name}:{STOP_PATTERNS.search(msg.text).group(0)}"
                    ):
                        added += 1
    except Exception as e:
        logger.debug(f"[optout/{account.name}] poll error: {e}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return added


async def run_shard(shard_id: int | None, shard_count: int) -> None:
    """Цикл polling одного шарда (или всех, если shard_id=None)."""
    name = f"shard={shard_id}" if shard_id is not None else "all"
    logger.info(
        f"[optout {name}] poll каждые {settings.optout_poll_interval_seconds}s"
    )

    while True:
        try:
            async with async_session() as session:
                q = select(Account).where(Account.enabled.is_(True))
                if shard_id is not None:
                    q = q.where((Account.id % shard_count) == shard_id)
                accounts = list((await session.execute(q)).scalars().all())

            if not accounts:
                logger.debug(f"[optout {name}] нет активных аккаунтов")
                await asyncio.sleep(settings.optout_poll_interval_seconds)
                continue

            logger.info(f"[optout {name}] polling {len(accounts)} аккаунтов")

            # Параллельно через семафор (не больше 10 одновременно чтобы не
            # перегрузить ни сервер ни Telegram)
            sem = asyncio.Semaphore(10)
            results = []

            async def _one(acc):
                async with sem:
                    return await poll_account(
                        acc, settings.optout_poll_interval_seconds * 2
                    )

            results = await asyncio.gather(
                *[_one(a) for a in accounts], return_exceptions=True
            )

            total_added = sum(r for r in results if isinstance(r, int))
            if total_added > 0:
                logger.info(f"[optout {name}] добавлено в blocklist: {total_added}")

            await asyncio.sleep(settings.optout_poll_interval_seconds)
        except Exception as e:
            logger.error(f"[optout {name}] cycle error: {e}")
            await asyncio.sleep(60)


def parse_shards(spec: str) -> list[int] | None:
    if spec.lower() == "all":
        return None  # обработать все
    result = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            result.extend(range(int(a), int(b) + 1))
        else:
            result.append(int(part))
    return result


async def run(shards: list[int] | None) -> None:
    await init_db()
    shard_count = settings.worker_count

    if shards is None:
        await run_shard(None, shard_count)
    else:
        tasks = [run_shard(s, shard_count) for s in shards]
        await asyncio.gather(*tasks)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--shards", type=str, default="all",
        help='формат: "all" / "0" / "0-9" / "0,3,7"',
    )
    args = parser.parse_args()
    shards = parse_shards(args.shards)
    asyncio.run(run(shards))


if __name__ == "__main__":
    main()

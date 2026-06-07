"""
Слушатель входящих сообщений — глобальный opt-out.

Подключает Telethon ко всем enabled-аккаунтам в events.NewMessage режиме.
Если получено сообщение с маркер-фразой STOP/UNSUBSCRIBE/ОТПИШИ и т.п. —
добавить tg_user_id в BlockedContact. После этого ни один аккаунт больше
не будет писать этому контакту.

Запуск:
    python -m src.optout
"""
import asyncio
import re
from datetime import datetime

from loguru import logger
from sqlalchemy import select
from telethon import events

from .db import async_session, init_db
from .models import Account, BlockedContact
from .telethon_client import build_client


STOP_PATTERNS = re.compile(
    r"\b("
    r"stop|unsubscribe|отпиши(те)?|не\s*пиши(те)?|"
    r"прекрати(те)?|удали(те)?|отстань(те)?|"
    r"stoplist|неинтересно|отписка|"
    r"to'xta|kerakmas|yozma"  # узбекские варианты
    r")\b",
    re.IGNORECASE,
)


_clients: list = []


async def _add_to_blocklist(tg_user_id: int, reason: str) -> None:
    async with async_session() as session:
        exists = (await session.execute(
            select(BlockedContact).where(BlockedContact.tg_user_id == tg_user_id)
        )).scalar_one_or_none()
        if exists:
            return
        session.add(BlockedContact(
            tg_user_id=tg_user_id,
            reason=reason,
            blocked_at=datetime.utcnow(),
        ))
        await session.commit()
        logger.warning(f"[optout] user {tg_user_id} добавлен в blocklist: {reason}")


async def _attach_listener(client, account_name: str) -> None:
    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        try:
            text = event.raw_text or ""
            if not text:
                return
            m = STOP_PATTERNS.search(text)
            if not m:
                return
            sender_id = event.sender_id
            if sender_id is None:
                return
            phrase = m.group(0)
            await _add_to_blocklist(
                sender_id,
                f"auto:{account_name}:{phrase}",
            )
        except Exception as e:
            logger.error(f"[optout/{account_name}] handler error: {e}")


async def run() -> None:
    await init_db()

    async with async_session() as session:
        accounts = (await session.execute(
            select(Account).where(Account.enabled.is_(True))
        )).scalars().all()

    logger.info(f"Optout listener: подключаю {len(accounts)} аккаунтов")

    for acc in accounts:
        try:
            client = build_client(acc)
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning(f"[{acc.name}] не авторизован — пропуск")
                await client.disconnect()
                continue
            await _attach_listener(client, acc.name)
            _clients.append((acc.name, client))
            logger.info(f"[{acc.name}] слушаю входящие")
        except Exception as e:
            logger.error(f"[{acc.name}] init failed: {e}")

    if not _clients:
        logger.error("Не подключился ни один аккаунт. Выхожу.")
        return

    logger.success(f"Optout listener активен на {len(_clients)} аккаунтах")

    # Держим event-loop живым
    try:
        await asyncio.gather(*[
            client.run_until_disconnected() for _, client in _clients
        ])
    finally:
        for name, client in _clients:
            try:
                await client.disconnect()
            except Exception:
                pass


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

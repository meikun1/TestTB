"""
Выгрузка диалогов и истории сообщений из Telegram.

Использует Telethon (user API, MTProto). При первом запуске Telegram
попросит код подтверждения — введи его в консоли.

Запуск: python -m src.fetcher.fetch_dialogs
"""
import asyncio
from datetime import datetime

from loguru import logger
from sqlalchemy import select
from telethon import TelegramClient
from telethon.tl.types import User

from config import settings, PROJECT_ROOT
from src.utils import init_db, async_session, Contact, Message


# Сколько последних сообщений вытаскивать на каждый диалог.
# 200 — компромисс между полнотой контекста и скоростью.
MESSAGES_PER_DIALOG = 200


def make_client() -> TelegramClient:
    session_path = PROJECT_ROOT / "data" / settings.tg_session_name
    return TelegramClient(str(session_path), settings.tg_api_id, settings.tg_api_hash)


async def upsert_contact(session, user: User) -> Contact:
    result = await session.execute(
        select(Contact).where(Contact.tg_user_id == user.id)
    )
    contact = result.scalar_one_or_none()

    if contact is None:
        contact = Contact(
            tg_user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            phone=user.phone,
        )
        session.add(contact)
        await session.flush()
    else:
        contact.username = user.username
        contact.first_name = user.first_name
        contact.last_name = user.last_name

    return contact


async def save_messages(session, contact: Contact, messages_data: list[dict]) -> int:
    """Сохраняет сообщения, пропуская дубликаты по tg_message_id."""
    existing = await session.execute(
        select(Message.tg_message_id).where(Message.contact_id == contact.id)
    )
    existing_ids = {row[0] for row in existing.all()}

    new_count = 0
    for m in messages_data:
        if m["tg_message_id"] in existing_ids:
            continue
        session.add(Message(contact_id=contact.id, **m))
        new_count += 1

    return new_count


async def fetch_all() -> None:
    await init_db()
    client = make_client()
    await client.start(phone=settings.tg_phone)
    logger.info("Telegram client started")

    total_contacts = 0
    total_messages = 0

    async for dialog in client.iter_dialogs():
        # Берём только личные диалоги, не каналы и не группы
        if not dialog.is_user:
            continue
        entity = dialog.entity
        if not isinstance(entity, User) or entity.bot:
            continue

        async with async_session() as session:
            contact = await upsert_contact(session, entity)

            messages_data = []
            async for msg in client.iter_messages(entity, limit=MESSAGES_PER_DIALOG):
                if not msg.text:  # пропускаем медиа без подписей
                    continue
                messages_data.append(
                    {
                        "tg_message_id": msg.id,
                        "from_me": msg.out,
                        "text": msg.text,
                        "date": msg.date.replace(tzinfo=None),
                    }
                )

            if messages_data:
                contact.total_messages = len(messages_data)
                contact.last_msg_at = messages_data[0]["date"]
                contact.last_msg_from_me = messages_data[0]["from_me"]

            added = await save_messages(session, contact, messages_data)
            await session.commit()

            total_contacts += 1
            total_messages += added
            logger.info(
                f"[{total_contacts}] {entity.first_name or entity.username}: "
                f"+{added} messages"
            )

    await client.disconnect()
    logger.success(
        f"Done. Contacts: {total_contacts}, new messages: {total_messages}"
    )


if __name__ == "__main__":
    asyncio.run(fetch_all())

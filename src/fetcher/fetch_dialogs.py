"""
Выгрузка диалогов и истории сообщений из Telegram — для конкретного аккаунта.

Использует Telethon (user API, MTProto). При первом запуске для каждого
неавторизованного аккаунта Telegram попросит код подтверждения.

Запуск:
    python -m src.fetcher.fetch_dialogs --account acc01
    python -m src.fetcher.fetch_dialogs --all
"""
import argparse
import asyncio

from loguru import logger
from sqlalchemy import select
from telethon import TelegramClient
from telethon.tl.types import User

from src.accounts.pool import build_client
from src.utils import init_db, async_session, Account, Contact, Message


# Сколько последних сообщений вытаскивать на каждый диалог (можно переопределить в .env).
from config import settings as _settings  # noqa: E402
MESSAGES_PER_DIALOG = _settings.fetcher_messages_per_dialog


async def upsert_contact(session, account_id: int, user: User) -> Contact:
    result = await session.execute(
        select(Contact).where(
            Contact.account_id == account_id,
            Contact.tg_user_id == user.id,
        )
    )
    contact = result.scalar_one_or_none()

    if contact is None:
        contact = Contact(
            account_id=account_id,
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


async def fetch_one_account(account: Account) -> None:
    client: TelegramClient = build_client(account)
    await client.start(phone=account.phone)
    logger.info(f"[{account.name}] Telegram client started")

    total_contacts = 0
    total_messages = 0

    async for dialog in client.iter_dialogs():
        if not dialog.is_user:
            continue
        entity = dialog.entity
        if not isinstance(entity, User) or entity.bot:
            continue

        async with async_session() as session:
            contact = await upsert_contact(session, account.id, entity)

            messages_data = []
            async for msg in client.iter_messages(entity, limit=MESSAGES_PER_DIALOG):
                if not msg.text:
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

    await client.disconnect()
    logger.success(
        f"[{account.name}] Done. Contacts: {total_contacts}, "
        f"new messages: {total_messages}"
    )


async def fetch_all_accounts() -> None:
    async with async_session() as session:
        accs = (
            await session.execute(
                select(Account).where(Account.enabled.is_(True))
            )
        ).scalars().all()

    for acc in accs:
        try:
            await fetch_one_account(acc)
        except Exception as e:
            logger.error(f"[{acc.name}] fetch failed: {e}")


async def fetch_by_name(name: str) -> None:
    async with async_session() as session:
        acc = (
            await session.execute(select(Account).where(Account.name == name))
        ).scalar_one_or_none()
        if not acc:
            raise SystemExit(f"Аккаунт {name} не найден в БД")
    await fetch_one_account(acc)


async def main(account_name: str | None, all_flag: bool) -> None:
    await init_db()
    if all_flag:
        await fetch_all_accounts()
    elif account_name:
        await fetch_by_name(account_name)
    else:
        raise SystemExit("Укажи --account <name> или --all")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", type=str, default=None)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.account, args.all))

"""
Интерактивный логин одного аккаунта. Telegram пришлёт код — введи в консоли.

Если в sessions/<name>.session уже лежит готовая сессия (например, выгрузили
с warmup-сервера) — авторизация пропустится.

Запуск:
    python -m src.accounts.login <account_name>
    python -m src.accounts.login --all        # для всех неавторизованных
"""
import argparse
import asyncio

from loguru import logger
from sqlalchemy import select

from src.utils import async_session, Account
from .pool import build_client


async def login_one(name: str) -> None:
    async with async_session() as session:
        acc = (
            await session.execute(select(Account).where(Account.name == name))
        ).scalar_one_or_none()
        if not acc:
            logger.error(f"Аккаунт {name} не найден в БД")
            return

    client = build_client(acc)
    try:
        await client.start(phone=acc.phone)
        me = await client.get_me()
        logger.success(
            f"[{acc.name}] авторизован как {me.first_name} (@{me.username})"
        )
    finally:
        await client.disconnect()


async def login_all() -> None:
    async with async_session() as session:
        accs = (
            await session.execute(
                select(Account).where(Account.enabled.is_(True))
            )
        ).scalars().all()

    for acc in accs:
        client = build_client(acc)
        try:
            await client.connect()
            if await client.is_user_authorized():
                logger.info(f"[{acc.name}] уже авторизован")
                continue
            await client.disconnect()
        except Exception as e:
            logger.warning(f"[{acc.name}] connect error: {e}")
            continue

        logger.info(f"[{acc.name}] требуется авторизация")
        await login_one(acc.name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("name", nargs="?")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    if args.all:
        asyncio.run(login_all())
    elif args.name:
        asyncio.run(login_one(args.name))
    else:
        parser.error("Укажи имя аккаунта или --all")


if __name__ == "__main__":
    main()

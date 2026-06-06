"""
Логин аккаунта с авто-обработкой email-подтверждения.

Если в sessions/<name>.session или Account.session_string уже лежит
готовая сессия — авторизация пропускается.

Если Telegram во время логина запросит email-верификацию
(EmailUnconfirmedError) — мы автоматически:
  1) сгенерим уникальный <random>@EMAIL_DOMAIN
  2) передадим его Telegram'у
  3) дождёмся письма по IMAP
  4) распарсим код, отдадим Telegram'у

Запуск:
    python -m src.accounts.login <account_name>
    python -m src.accounts.login --all
"""
import argparse
import asyncio
import getpass

from loguru import logger
from sqlalchemy import select
from telethon.errors import (
    EmailUnconfirmedError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from config import settings
from src.utils import async_session, Account
from .pool import build_client
from .email_verify import generate_email, wait_for_code


async def _code_callback() -> str:
    """SMS-код вводится из консоли."""
    return input("Telegram прислал SMS-код → ")


async def _password_callback() -> str:
    """2FA-пароль вводится скрыто."""
    return getpass.getpass("2FA пароль → ")


async def _handle_email_verify(client, account: Account, phone_code: str) -> None:
    """
    Вызывается когда Telegram бросает EmailUnconfirmedError при sign_in
    после ввода SMS-кода.
    """
    if not settings.email_domain or not settings.imap_host:
        raise RuntimeError(
            f"[{account.name}] Telegram требует email, "
            f"но EMAIL_DOMAIN / IMAP_HOST не настроены в .env"
        )

    addr = await generate_email(account_id=account.id)
    logger.info(f"[{account.name}] подаю email {addr} в Telegram")

    # Telethon: для подачи нового адреса используем sign_in с email
    try:
        await client.sign_in(phone=account.phone, code=phone_code, email=addr)
    except EmailUnconfirmedError:
        # Это норма: после подачи email Telegram отправляет на него код,
        # и до его подтверждения логин не завершён.
        pass

    code = await wait_for_code(addr)
    if not code:
        raise RuntimeError(
            f"[{account.name}] не дождались email-кода (таймаут "
            f"{settings.email_code_timeout_seconds}s). Проверь почтовый сервер."
        )

    logger.info(f"[{account.name}] получил код {code} из почты, подтверждаю")
    await client.sign_in(phone=account.phone, code=code)


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
        await client.connect()
        if await client.is_user_authorized():
            logger.info(f"[{acc.name}] уже авторизован")
            await _persist_string_session(acc, client)
            return

        # Запрос SMS-кода
        sent = await client.send_code_request(acc.phone)
        logger.info(f"[{acc.name}] запросили SMS-код (type={sent.type.__class__.__name__})")
        sms_code = await _code_callback()

        # Основной sign_in. Возможные ветки:
        try:
            await client.sign_in(phone=acc.phone, code=sms_code)
        except SessionPasswordNeededError:
            # 2FA облако-пароль
            await client.sign_in(password=await _password_callback())
        except EmailUnconfirmedError:
            # Telegram дополнительно требует email — обрабатываем
            await _handle_email_verify(client, acc, sms_code)
        except PhoneCodeInvalidError:
            logger.error(f"[{acc.name}] неверный SMS-код")
            return

        me = await client.get_me()
        await _persist_string_session(acc, client)
        logger.success(
            f"[{acc.name}] авторизован как {me.first_name} (@{me.username})"
        )
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _persist_string_session(acc: Account, client) -> None:
    """Кладёт StringSession обратно в БД — для переносимости."""
    try:
        session_str = StringSession.save(client.session)
    except Exception as e:
        logger.warning(f"[{acc.name}] не удалось получить StringSession: {e}")
        return
    async with async_session() as session:
        db_acc = await session.get(Account, acc.id)
        db_acc.session_string = session_str
        await session.commit()


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
                await _persist_string_session(acc, client)
                await client.disconnect()
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

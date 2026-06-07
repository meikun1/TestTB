"""
Импорт аккаунтов из CSV + выгрузка контактов каждого.

CSV-формат:
    name,phone,session_string,device_model,system_version,app_version,lang_code,system_lang_code
    acc01,+998901234567,1BVtsOK4Bu...,iPhone 14,iOS 16.5.1,10.2.0,ru,ru-UZ

При импорте:
  - валидация UZ-номера (carrier prefix)
  - запись Account
  - подключение Telethon с device-params
  - email/captcha авто-обработка
  - geo-check через GetNearestDc
  - выгрузка диалогов (только личные с историей)
  - запись Contact + определение language_hint

Запуск:
    python -m src.import_accounts accounts.csv
"""
import argparse
import asyncio
import csv
from datetime import datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from telethon import functions
from telethon.errors import (
    EmailUnconfirmedError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import User

from .config import settings
from .carrier import detect_carrier, is_valid_uz_number
from .db import async_session, init_db
from .email_verify import generate_email, wait_for_code
from .language import detect_language
from .models import Account, Contact
from .telethon_client import build_client


# Сколько последних сообщений берём чтобы оценить language_hint и last_msg_at
MESSAGES_TO_INSPECT = 50


async def _upsert_account(row: dict) -> Account | None:
    """Создаёт/обновляет аккаунт в БД. Возвращает Account или None если skip."""
    name = row["name"].strip()
    phone = row["phone"].strip()

    if not is_valid_uz_number(phone):
        logger.warning(f"[{name}] номер {phone} не валиден для UZ — skip")
        return None

    carrier = detect_carrier(phone)

    async with async_session() as session:
        existing = (await session.execute(
            select(Account).where(Account.name == name)
        )).scalar_one_or_none()

        fields = dict(
            phone=phone,
            session_string=row["session_string"].strip(),
            device_model=(row.get("device_model") or "").strip() or None,
            system_version=(row.get("system_version") or "").strip() or None,
            app_version=(row.get("app_version") or "").strip() or None,
            lang_code=(row.get("lang_code") or "").strip()
                      or settings.default_lang_code,
            system_lang_code=(row.get("system_lang_code") or "").strip()
                             or settings.default_system_lang_code,
            carrier=carrier,
        )

        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
            await session.commit()
            await session.refresh(existing)
            return existing

        acc = Account(name=name, **fields)
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        return acc


async def _ensure_authorized(client, account: Account) -> bool:
    """
    Подключение + проверка авторизации. Если требуется email — обработать.
    Возвращает True если аккаунт авторизован.
    """
    await client.connect()
    if await client.is_user_authorized():
        return True

    # session_string не валиден / просрочен — пробуем re-auth
    logger.warning(f"[{account.name}] session_string не авторизован, "
                   "пробую заново через phone")
    try:
        sent = await client.send_code_request(account.phone)
        logger.info(f"[{account.name}] code requested ({sent.type.__class__.__name__})")
        sms_code = input(f"[{account.name}] Telegram прислал SMS-код: ").strip()
        try:
            await client.sign_in(phone=account.phone, code=sms_code)
        except SessionPasswordNeededError:
            import getpass
            pwd = getpass.getpass(f"[{account.name}] 2FA пароль: ")
            await client.sign_in(password=pwd)
        except EmailUnconfirmedError:
            await _handle_email_verify(client, account, sms_code)
        except PhoneCodeInvalidError:
            logger.error(f"[{account.name}] неверный SMS-код")
            return False
    except Exception as e:
        logger.error(f"[{account.name}] re-auth failed: {e}")
        return False

    return await client.is_user_authorized()


async def _handle_email_verify(client, account: Account, sms_code: str) -> None:
    if not settings.email_domain or not settings.imap_host:
        raise RuntimeError(
            f"[{account.name}] Telegram требует email, "
            "но EMAIL_DOMAIN/IMAP_HOST не настроены"
        )
    addr = await generate_email(account_id=account.id)
    logger.info(f"[{account.name}] подаю email {addr}")
    try:
        await client.sign_in(phone=account.phone, code=sms_code, email=addr)
    except EmailUnconfirmedError:
        pass  # ожидаемо — Telegram отправил код на email
    code = await wait_for_code(addr)
    if not code:
        raise RuntimeError(
            f"[{account.name}] не дождались email-кода "
            f"({settings.email_code_timeout_seconds}s)"
        )
    logger.info(f"[{account.name}] получил код из email, подтверждаю")
    await client.sign_in(phone=account.phone, code=code)


async def _geo_check(client, account: Account) -> str | None:
    """
    Проверяет nearest_dc через Telegram API. Возвращает причину mismatch
    или None если всё ок.
    """
    try:
        nearest = await client(functions.help.GetNearestDcRequest())
        country = (nearest.country or "").upper()
        if country != settings.expected_nearest_dc_country.upper():
            return f"nearest_dc.country={country}, expected={settings.expected_nearest_dc_country}"
    except Exception as e:
        logger.warning(f"[{account.name}] GetNearestDc failed: {e}")
        # не критично — не помечаем mismatch
    return None


async def _fetch_contacts(client, account: Account) -> tuple[int, int]:
    """Выгружает диалоги, сохраняет/обновляет Contact'ы. Возвращает (total, new)."""
    total = 0
    new = 0

    async for dialog in client.iter_dialogs():
        if not dialog.is_user:
            continue
        entity = dialog.entity
        if not isinstance(entity, User) or entity.bot:
            continue

        # собрать последние N сообщений для language detection и метаданных
        messages = []
        async for msg in client.iter_messages(entity, limit=MESSAGES_TO_INSPECT):
            if msg.text:
                messages.append({
                    "text": msg.text,
                    "from_me": msg.out,
                    "date": msg.date.replace(tzinfo=None),
                })

        if not messages:
            continue  # пустые диалоги (только медиа без текста) — игнор

        total_messages = len(messages)
        last_msg_at = messages[0]["date"]

        # Определить language по конкатенации текста
        joined_text = " ".join(m["text"] for m in messages[:30])
        language_hint = detect_language(joined_text)

        async with async_session() as session:
            existing = (await session.execute(
                select(Contact).where(
                    Contact.account_id == account.id,
                    Contact.tg_user_id == entity.id,
                )
            )).scalar_one_or_none()

            if existing:
                existing.username = entity.username
                existing.first_name = entity.first_name
                existing.last_name = entity.last_name
                existing.phone = entity.phone
                existing.last_msg_at = last_msg_at
                existing.total_messages = total_messages
                if language_hint and not existing.language_hint:
                    existing.language_hint = language_hint
            else:
                session.add(Contact(
                    account_id=account.id,
                    tg_user_id=entity.id,
                    username=entity.username,
                    first_name=entity.first_name,
                    last_name=entity.last_name,
                    phone=entity.phone,
                    last_msg_at=last_msg_at,
                    total_messages=total_messages,
                    language_hint=language_hint,
                ))
                new += 1
            await session.commit()

        total += 1

    return total, new


async def import_one(row: dict) -> None:
    acc = await _upsert_account(row)
    if acc is None:
        return

    client = build_client(acc)
    try:
        if not await _ensure_authorized(client, acc):
            async with async_session() as session:
                db_acc = await session.get(Account, acc.id)
                db_acc.status = "disabled"
                db_acc.status_reason = "auth_failed"
                db_acc.enabled = False
                await session.commit()
            return

        # Geo-check
        geo_mismatch = await _geo_check(client, acc)
        if geo_mismatch:
            logger.warning(f"[{acc.name}] geo-mismatch: {geo_mismatch}")
            async with async_session() as session:
                db_acc = await session.get(Account, acc.id)
                db_acc.status = "geo_mismatch"
                db_acc.status_reason = geo_mismatch
                db_acc.enabled = False
                await session.commit()
            return

        # Сохранить актуальный StringSession обратно (на случай если был обновлён при re-auth)
        from telethon.sessions import StringSession
        try:
            new_session = StringSession.save(client.session)
            async with async_session() as session:
                db_acc = await session.get(Account, acc.id)
                db_acc.session_string = new_session
                await session.commit()
        except Exception as e:
            logger.debug(f"[{acc.name}] couldn't refresh session_string: {e}")

        # Выгрузить контакты
        total, new = await _fetch_contacts(client, acc)
        logger.success(f"[{acc.name}] контакты: {total} всего, {new} новых")

    except Exception as e:
        logger.error(f"[{acc.name}] import failed: {e}")
        async with async_session() as session:
            db_acc = await session.get(Account, acc.id)
            db_acc.status = "disabled"
            db_acc.status_reason = f"import_error: {str(e)[:200]}"
            db_acc.enabled = False
            await session.commit()
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def run_import(csv_path: Path) -> None:
    await init_db()

    if not csv_path.exists():
        raise SystemExit(f"CSV не найден: {csv_path}")

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [r for r in reader]

    logger.info(f"Импорт {len(rows)} аккаунтов из {csv_path}")
    for i, row in enumerate(rows, 1):
        logger.info(f"--- [{i}/{len(rows)}] {row.get('name')} ---")
        try:
            await import_one(row)
        except Exception as e:
            logger.error(f"row {row.get('name')}: {e}")
        # маленькая пауза между аккаунтами чтобы не лупить Telegram'у залпом
        await asyncio.sleep(3)

    logger.success("Импорт завершён")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    args = parser.parse_args()
    asyncio.run(run_import(args.csv))


if __name__ == "__main__":
    main()

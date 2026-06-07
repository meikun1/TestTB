"""
Утилита проверки fingerprint в БД.

Показывает каждый аккаунт и какие у него device-поля заполнены.
Помогает убедиться что используется реальный fingerprint, а не дефолты.

Запуск:
    python -m src.verify_fingerprints           — таблица по всем аккаунтам
    python -m src.verify_fingerprints --bad     — только неполные
    python -m src.verify_fingerprints --account acc01  — для одного
"""
import argparse
import asyncio

from loguru import logger
from sqlalchemy import select

from .db import async_session, init_db
from .models import Account
from .telethon_client import DEVICE_FIELDS, missing_fingerprint_fields


def _format_account(acc: Account) -> str:
    missing = missing_fingerprint_fields(acc)
    if not missing:
        status_str = "✓ REAL"
    else:
        status_str = f"✗ missing: {','.join(missing)}"

    return (
        f"{acc.name:20s} {acc.phone:18s} "
        f"status={acc.status:14s} "
        f"device={acc.device_model or '—':25s} "
        f"os={acc.system_version or '—':15s} "
        f"app={acc.app_version or '—':10s} "
        f"lang={acc.lang_code or '—':6s} "
        f"sys_lang={acc.system_lang_code or '—':8s} "
        f"{status_str}"
    )


async def run(only_bad: bool, account_name: str | None) -> None:
    await init_db()

    async with async_session() as session:
        q = select(Account).order_by(Account.name)
        if account_name:
            q = q.where(Account.name == account_name)
        accounts = (await session.execute(q)).scalars().all()

    if not accounts:
        print("Аккаунтов нет.")
        return

    total = len(accounts)
    real_count = 0
    partial_count = 0
    missing_count = 0

    for acc in accounts:
        missing = missing_fingerprint_fields(acc)
        if not missing:
            real_count += 1
            if not only_bad:
                print(_format_account(acc))
        elif len(missing) == len(DEVICE_FIELDS):
            missing_count += 1
            print(_format_account(acc))
        else:
            partial_count += 1
            print(_format_account(acc))

    print("\n" + "=" * 80)
    print(f"Всего аккаунтов: {total}")
    print(f"  ✓ REAL fingerprint (все 5 полей): {real_count}")
    print(f"  ⚠ Частичный fingerprint:           {partial_count}")
    print(f"  ✗ Без fingerprint вообще:          {missing_count}")
    print("=" * 80)

    if partial_count + missing_count > 0:
        print(
            "\nДля production нужны все 5 полей у каждого аккаунта. "
            "Дозаполни CSV и переимпортируй проблемные."
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bad", action="store_true",
        help="показать только неполные fingerprint",
    )
    parser.add_argument(
        "--account", type=str, default=None,
        help="конкретный аккаунт по имени",
    )
    args = parser.parse_args()
    asyncio.run(run(args.bad, args.account))


if __name__ == "__main__":
    main()

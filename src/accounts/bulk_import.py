"""
Массовый импорт аккаунтов из CSV.

Формат CSV (с заголовком):
    name,phone,session_path,proxy,daily_limit,hourly_limit

Поля session_path / proxy могут быть пустыми.
Если session_path пуст — будет сгенерирован как sessions/<name>.session.
Если proxy пуст — аккаунт пойдёт без прокси (не рекомендую на масштабе).

Запуск:
    python -m src.accounts.bulk_import accounts.csv
"""
import argparse
import asyncio
import csv
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from config import settings
from src.utils import init_db, async_session, Account


async def import_csv(path: Path) -> None:
    await init_db()

    created = 0
    updated = 0
    async with async_session() as session:
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row["name"].strip()
                if not name:
                    continue

                session_path = row.get("session_path", "").strip() \
                    or f"sessions/{name}.session"

                existing = (
                    await session.execute(
                        select(Account).where(Account.name == name)
                    )
                ).scalar_one_or_none()

                fields = dict(
                    phone=row["phone"].strip(),
                    session_path=session_path,
                    proxy=(row.get("proxy") or "").strip() or None,
                    daily_limit=int(row.get("daily_limit") or settings.default_daily_limit),
                    hourly_limit=int(row.get("hourly_limit") or settings.default_hourly_limit),
                )

                if existing:
                    for k, v in fields.items():
                        setattr(existing, k, v)
                    updated += 1
                else:
                    session.add(Account(name=name, **fields))
                    created += 1

        await session.commit()

    logger.success(f"Импорт завершён: создано {created}, обновлено {updated}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    args = parser.parse_args()
    if not args.csv.exists():
        raise SystemExit(f"Файл не найден: {args.csv}")
    asyncio.run(import_csv(args.csv))


if __name__ == "__main__":
    main()

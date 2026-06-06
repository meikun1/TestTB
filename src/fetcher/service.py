"""
Фоновый сервис догрузки истории по всем аккаунтам.

Раз в FETCHER_INTERVAL_HOURS проходит по enabled-аккаунтам, обновляет историю
последних N сообщений. Работает параллельно с sender'ами (не блокирует их).

Запуск:
    python -m src.fetcher.service
    или внутри docker-compose как отдельный сервис.
"""
import asyncio
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select

from config import settings
from src.utils import init_db, async_session, Account
from .fetch_dialogs import fetch_one_account


async def fetch_loop() -> None:
    await init_db()
    interval = settings.fetcher_interval_hours * 3600

    while True:
        try:
            cutoff = datetime.utcnow() - timedelta(hours=settings.fetcher_interval_hours)
            async with async_session() as session:
                accs = (await session.execute(
                    select(Account)
                    .where(Account.enabled.is_(True))
                    .where(
                        (Account.last_fetched_at.is_(None))
                        | (Account.last_fetched_at < cutoff)
                    )
                )).scalars().all()

            logger.info(f"Fetcher: к выгрузке {len(accs)} аккаунтов")

            for acc in accs:
                try:
                    await fetch_one_account(acc)
                    async with async_session() as session:
                        db_acc = await session.get(Account, acc.id)
                        db_acc.last_fetched_at = datetime.utcnow()
                        await session.commit()
                except Exception as e:
                    logger.error(f"[{acc.name}] fetch failed: {e}")
                # маленькая пауза между аккаунтами чтобы не давить на Telegram
                await asyncio.sleep(5)

            logger.success(f"Fetcher cycle done. Сплю {interval}s")
            await asyncio.sleep(interval)
        except Exception as e:
            logger.error(f"Fetcher loop error: {e}, retry in 60s")
            await asyncio.sleep(60)


def main() -> None:
    asyncio.run(fetch_loop())


if __name__ == "__main__":
    main()

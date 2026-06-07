"""
Фоновый worker для обработки intake-pending аккаунтов.

Когда intake API получает аккаунт в async-режиме (`?async=true` или явное
указание в payload), он сохраняет Account с `intake_status='pending_intake'`
и возвращает 200 сразу — НЕ ждёт Telethon-connect и fetch контактов.

Этот воркер раз в N секунд:
  1. Берёт N аккаунтов со status='pending_intake' (SKIP LOCKED)
  2. Параллельно запускает import_one(interactive=False, fetch_contacts=True)
  3. Помечает intake_status='done' / 'failed'

Запуск:
    python -m src.intake_worker
"""
import asyncio
from datetime import datetime

from loguru import logger
from sqlalchemy import select

from .alerts import alert
from .config import settings
from .db import async_session, init_db
from .import_accounts import import_one
from .models import Account


async def _process_one(account_id: int) -> None:
    """Обработка одного аккаунта."""
    async with async_session() as session:
        acc = await session.get(Account, account_id)
        if not acc or acc.intake_status != "pending_intake":
            return
        acc.intake_status = "processing"
        await session.commit()
        # Собираем row для import_one
        row = {
            "name": acc.name,
            "phone": acc.phone,
            "session_string": acc.session_string,
            "device_model": acc.device_model or "",
            "system_version": acc.system_version or "",
            "app_version": acc.app_version or "",
            "lang_code": acc.lang_code or "",
            "system_lang_code": acc.system_lang_code or "",
        }

    try:
        result = await import_one(
            row, interactive=False, fetch_contacts=True,
        )
        final = "done" if result["status"] == "active" else "failed"
        async with async_session() as session:
            acc = await session.get(Account, account_id)
            if acc:
                acc.intake_status = final
                await session.commit()
        logger.info(
            f"[intake_worker] {row['name']}: {result['status']} "
            f"({result['contacts_total']} contacts)"
        )
    except Exception as e:
        logger.error(f"[intake_worker] {row['name']} failed: {e}")
        async with async_session() as session:
            acc = await session.get(Account, account_id)
            if acc:
                acc.intake_status = "failed"
                acc.status_reason = f"intake_worker_error: {str(e)[:200]}"
                await session.commit()


async def run() -> None:
    await init_db()
    sem = asyncio.Semaphore(settings.intake_worker_parallel)
    poll = settings.intake_worker_poll_interval
    logger.info(
        f"[intake_worker] poll каждые {poll}s, parallel={settings.intake_worker_parallel}"
    )

    while True:
        try:
            async with async_session() as session:
                # SKIP LOCKED — чтобы несколько worker-копий не схватили одно
                accounts = (await session.execute(
                    select(Account.id)
                    .where(Account.intake_status == "pending_intake")
                    .with_for_update(skip_locked=True)
                    .limit(settings.intake_worker_parallel * 3)
                )).scalars().all()

            if not accounts:
                await asyncio.sleep(poll)
                continue

            logger.info(f"[intake_worker] беру {len(accounts)} аккаунтов")

            async def _wrap(aid):
                async with sem:
                    await _process_one(aid)

            await asyncio.gather(
                *[_wrap(aid) for aid in accounts], return_exceptions=True,
            )
        except Exception as e:
            logger.error(f"[intake_worker] loop error: {e}")
            await alert(f"intake_worker error: {e}", key="intake_worker_loop")
            await asyncio.sleep(60)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

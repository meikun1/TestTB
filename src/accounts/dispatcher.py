"""
Диспетчер: выбирает следующего аккаунта для отправки.

Учитывает:
  - дневной лимит каждого аккаунта (из таблицы Account)
  - часовой лимит
  - flood_until (после FloodWaitError аккаунт «остывает»)
  - статус (banned/disabled — выкидываем)
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, func

from src.utils import async_session, Account, SendLog


class AccountUnavailable(Exception):
    """Все аккаунты исчерпаны или в cooldown."""


async def _sent_in_window(session, account_id: int, hours: float) -> int:
    since = datetime.utcnow() - timedelta(hours=hours)
    result = await session.execute(
        select(func.count(SendLog.id))
        .where(SendLog.account_id == account_id)
        .where(SendLog.sent_at >= since)
        .where(SendLog.success.is_(True))
    )
    return result.scalar() or 0


class Dispatcher:
    """Round-robin по аккаунтам с учётом capacity."""

    def __init__(self, pool) -> None:
        self.pool = pool
        # позиция round-robin: чтобы не наваливать всё на первый аккаунт
        self._cursor = 0

    async def has_capacity(self, account: Account) -> tuple[bool, str]:
        """Можно ли через этот аккаунт сейчас отправлять."""
        now = datetime.utcnow()

        if account.status != "active":
            return False, f"status={account.status}"
        if account.flood_until and account.flood_until > now:
            left = (account.flood_until - now).total_seconds()
            return False, f"flood_wait {int(left)}s"

        async with async_session() as session:
            day_count = await _sent_in_window(session, account.id, 24)
            if day_count >= account.daily_limit:
                return False, f"daily {day_count}/{account.daily_limit}"
            hour_count = await _sent_in_window(session, account.id, 1)
            if hour_count >= account.hourly_limit:
                return False, f"hourly {hour_count}/{account.hourly_limit}"

        return True, "ok"

    async def pick(self, preferred_account_id: int | None = None) -> Account:
        """
        Возвращает аккаунт, через который сейчас можно слать.

        preferred — если у драфта уже есть назначенный аккаунт, проверяем его
        в первую очередь (sticky-маппинг контактов).
        """
        if preferred_account_id is not None:
            acc = self.pool.get_account(preferred_account_id)
            if acc:
                ok, _ = await self.has_capacity(acc)
                if ok:
                    return acc

        ids = self.pool.active_ids()
        if not ids:
            raise AccountUnavailable("Пул пустой")

        # round-robin: пробуем всех начиная с cursor
        n = len(ids)
        for i in range(n):
            idx = (self._cursor + i) % n
            acc = self.pool.get_account(ids[idx])
            if acc is None:
                # аккаунт исчез между snapshot и pick (live-refresh выкинул)
                continue
            ok, _ = await self.has_capacity(acc)
            if ok:
                self._cursor = (idx + 1) % n
                return acc

        raise AccountUnavailable("Все аккаунты в cooldown или исчерпали лимит")

    async def mark_flood(self, account: Account, seconds: int) -> None:
        """После FloodWaitError — отложить аккаунт на N секунд."""
        async with async_session() as session:
            db_acc = await session.get(Account, account.id)
            db_acc.flood_until = datetime.utcnow() + timedelta(seconds=seconds)
            db_acc.status = "flood"
            db_acc.status_reason = f"FloodWait {seconds}s"
            await session.commit()
        account.flood_until = db_acc.flood_until
        account.status = db_acc.status

    async def mark_banned(self, account: Account, reason: str) -> None:
        """PeerFlood / AuthKey / privacy — выводим из ротации насовсем."""
        async with async_session() as session:
            db_acc = await session.get(Account, account.id)
            db_acc.status = "banned"
            db_acc.status_reason = reason
            db_acc.enabled = False
            await session.commit()
        account.status = "banned"
        account.enabled = False

    async def mark_sent(self, account: Account) -> None:
        async with async_session() as session:
            db_acc = await session.get(Account, account.id)
            db_acc.last_send_at = datetime.utcnow()
            if db_acc.status == "flood" and (
                db_acc.flood_until is None or db_acc.flood_until < datetime.utcnow()
            ):
                db_acc.status = "active"
                db_acc.status_reason = None
            await session.commit()

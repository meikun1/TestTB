"""
Фоновый мониторинг состояния пула. Раз в HEALTH_CHECK_INTERVAL_SECONDS
считает метрики и шлёт алерт в Telegram если что-то превышает порог.

Метрики:
  - доля active-аккаунтов в пуле (если упала ниже X — алерт)
  - глобальный reply rate за последние 48ч
  - сколько аккаунтов «сгорело» за последний час (banned, status_reason)
  - очередь pending-драфтов растёт быстрее чем sender успевает
  - сколько аккаунтов в flood-cooldown прямо сейчас
"""
import asyncio
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select, func, desc

from config import settings
from src.utils import (
    init_db, async_session, Account, Contact, Draft, Message, SendLog
)
from .alerts import send_alert


# Состояние: чтобы не спамить одинаковыми алертами каждый цикл
_last_alerts: dict[str, datetime] = {}
_ALERT_COOLDOWN = timedelta(minutes=30)


def _should_alert(key: str) -> bool:
    last = _last_alerts.get(key)
    if last is None or datetime.utcnow() - last > _ALERT_COOLDOWN:
        _last_alerts[key] = datetime.utcnow()
        return True
    return False


async def _account_stats(session) -> dict:
    by_status = dict((s, c) for s, c in (await session.execute(
        select(Account.status, func.count(Account.id))
        .where(Account.enabled.is_(True))
        .group_by(Account.status)
    )).all())
    total = sum(by_status.values()) or 0
    return {
        "total_enabled": total,
        "active": by_status.get("active", 0),
        "flood": by_status.get("flood", 0),
        "banned": by_status.get("banned", 0),
    }


async def _burn_in_window(session, hours: int) -> int:
    """Сколько аккаунтов перешло в banned за последние N часов."""
    since = datetime.utcnow() - timedelta(hours=hours)
    # приближение: считаем по send_log с error='peer_flood'
    result = await session.execute(
        select(func.count(func.distinct(SendLog.account_id)))
        .where(SendLog.sent_at >= since)
        .where(SendLog.error.like("peer_flood%"))
    )
    return result.scalar() or 0


async def _global_reply_rate(session) -> tuple[float, int]:
    since = datetime.utcnow() - timedelta(hours=settings.reply_rate_check_hours)
    sends = (await session.execute(
        select(Draft, SendLog)
        .join(SendLog, SendLog.draft_id == Draft.id)
        .where(SendLog.sent_at >= since, SendLog.success.is_(True))
        .where(SendLog.kind == "message")
        .order_by(desc(SendLog.sent_at))
        .limit(500)
    )).all()
    if len(sends) < 20:
        return 1.0, len(sends)
    replied = 0
    for draft, send_log in sends:
        reply = await session.execute(
            select(func.count(Message.id))
            .where(
                Message.contact_id == draft.contact_id,
                Message.from_me.is_(False),
                Message.date >= send_log.sent_at,
            )
        )
        if (reply.scalar() or 0) > 0:
            replied += 1
    return replied / len(sends), len(sends)


async def _check_once() -> None:
    async with async_session() as session:
        st = await _account_stats(session)
        if st["total_enabled"] == 0:
            return

        active_ratio = st["active"] / st["total_enabled"]
        if active_ratio < settings.alert_active_ratio_threshold:
            if _should_alert("active_ratio"):
                await send_alert(
                    f"<b>Active ratio low: {active_ratio:.0%}</b>\n"
                    f"active={st['active']} flood={st['flood']} banned={st['banned']} "
                    f"total={st['total_enabled']}"
                )

        burn_1h = await _burn_in_window(session, hours=1)
        burn_rate = burn_1h / st["total_enabled"]
        if burn_rate > settings.alert_burn_rate_threshold:
            if _should_alert("burn_rate"):
                await send_alert(
                    f"<b>Burn rate {burn_rate:.0%} за час</b>\n"
                    f"{burn_1h}/{st['total_enabled']} аккаунтов сгорели"
                )

        reply_rate, sample = await _global_reply_rate(session)
        if sample >= 50 and reply_rate < settings.alert_reply_rate_threshold:
            if _should_alert("reply_rate"):
                await send_alert(
                    f"<b>Reply rate {reply_rate:.1%}</b> на выборке {sample} отправок\n"
                    f"Антифрод реагирует или плохой оффер. Снижай темп."
                )

        pending = (await session.execute(
            select(func.count(Draft.id))
            .where(Draft.status.in_(["pending", "approved"]))
        )).scalar() or 0
        sent_1h = (await session.execute(
            select(func.count(SendLog.id))
            .where(SendLog.sent_at >= datetime.utcnow() - timedelta(hours=1))
            .where(SendLog.success.is_(True), SendLog.kind == "message")
        )).scalar() or 0

        logger.info(
            f"[health] active={st['active']}/{st['total_enabled']} "
            f"flood={st['flood']} banned={st['banned']} "
            f"reply_rate={reply_rate:.1%}(n={sample}) "
            f"queue={pending} sent_1h={sent_1h}"
        )


async def run() -> None:
    await init_db()
    interval = settings.health_check_interval_seconds
    logger.info(f"Healthcheck: каждые {interval}s")
    await send_alert("Healthcheck запущен", silent=True)

    while True:
        try:
            await _check_once()
        except Exception as e:
            logger.error(f"Healthcheck error: {e}")
        await asyncio.sleep(interval)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

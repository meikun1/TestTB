"""
Scheduler — фоновый процесс с периодическими задачами.

Делает «через cron» без cron'а — лёгкий asyncio-loop с интервалами.

Задачи:
  - auto_generate     каждые 15 мин: создаёт драфты для новых аккаунтов
  - auto_cleanup      каждый час: чистит send_log старше N дней
  - auto_health       каждые 5 мин: проверяет shard heartbeats, доли active,
                       алертит в Telegram-бот если что-то сломалось
  - auto_prune        раз в сутки: pruning старых данных (контакты без
                       активности >180 дней, банкнутые аккаунты и т.п.)

Запуск:
    python -m src.scheduler
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from loguru import logger
from sqlalchemy import delete, func, select, update

from .alerts import alert
from .config import settings
from .db import async_session, init_db
from .generate import generate_for_account, _parse_attach
from .models import (
    Account,
    BlockedContact,
    Contact,
    Draft,
    GenerateRun,
    SendLog,
    ShardHeartbeat,
    UsedEmail,
    CaptchaChallenge,
)


# ============================================================================
# Task: auto_generate — создаёт драфты для новых аккаунтов
# ============================================================================

async def task_auto_generate() -> None:
    """
    Идём по active-аккаунтам у которых ЕЩЁ НЕТ pending драфтов и contacts > 0,
    создаём для них драфты с DEFAULT_ATTACH (если задан в .env).
    """
    if not settings.default_attach:
        logger.debug("[auto_generate] DEFAULT_ATTACH пуст — пропуск")
        return

    try:
        attach_kind, attach_ref = _parse_attach(settings.default_attach)
    except Exception as e:
        logger.error(f"[auto_generate] DEFAULT_ATTACH parse error: {e}")
        return

    started = datetime.utcnow()
    async with async_session() as session:
        # Аккаунты которые активны, intake_status=done, и у которых сейчас
        # нет pending драфтов И есть контакты
        # (используем подзапрос чтобы не делать N+1)
        accs_with_pending_q = select(Draft.account_id).where(
            Draft.status == "pending"
        ).distinct()

        accs_q = (
            select(Account)
            .where(Account.enabled.is_(True))
            .where(Account.status == "active")
            .where(Account.intake_status == "done")
            .where(~Account.id.in_(accs_with_pending_q))
        )
        accounts = (await session.execute(accs_q)).scalars().all()

        if not accounts:
            logger.debug("[auto_generate] нет акк без очереди — нечего делать")
            return

        # Аккаунты у которых есть хоть один контакт
        good_accounts = []
        for acc in accounts:
            cc = (await session.execute(
                select(func.count(Contact.id))
                .where(Contact.account_id == acc.id)
                .where(Contact.total_messages > 0)
                .where(Contact.last_msg_at.is_not(None))
            )).scalar() or 0
            if cc > 0:
                good_accounts.append(acc)

        # Глобальный blocklist один раз
        blocked = set(r[0] for r in (await session.execute(
            select(BlockedContact.tg_user_id)
        )).all())

        # Записать начало run
        run = GenerateRun(trigger="scheduler", started_at=started)
        session.add(run)
        await session.commit()
        await session.refresh(run)

    logger.info(
        f"[auto_generate] {len(good_accounts)} аккаунтов без очереди, "
        f"генерирую драфты"
    )

    total_drafts = 0
    for acc in good_accounts:
        try:
            n = await generate_for_account(
                acc,
                attach_kind, attach_ref,
                settings.default_attach_caption,
                settings.default_attach_delay,
                blocked,
            )
            total_drafts += n
        except Exception as e:
            logger.error(f"[auto_generate] {acc.name}: {e}")

    async with async_session() as session:
        run = await session.get(GenerateRun, run.id)
        if run:
            run.finished_at = datetime.utcnow()
            run.accounts_touched = len(good_accounts)
            run.drafts_created = total_drafts
            await session.commit()

    logger.success(
        f"[auto_generate] done: {len(good_accounts)} акк, +{total_drafts} драфтов"
    )


# ============================================================================
# Task: auto_cleanup — чистка старых данных
# ============================================================================

async def task_auto_cleanup() -> None:
    cutoff = datetime.utcnow() - timedelta(days=settings.send_log_retention_days)
    async with async_session() as session:
        # Удаляем старые send_log (но только success — failed оставляем для отладки)
        deleted_sendlog = (await session.execute(
            delete(SendLog)
            .where(SendLog.sent_at < cutoff)
            .where(SendLog.success.is_(True))
        )).rowcount
        # Архивируем сделанные драфты — удаляем sent старше retention
        deleted_drafts = (await session.execute(
            delete(Draft)
            .where(Draft.sent_at < cutoff)
            .where(Draft.status == "sent")
        )).rowcount
        # Verified emails старше 90 дней
        em_cutoff = datetime.utcnow() - timedelta(days=90)
        deleted_emails = (await session.execute(
            delete(UsedEmail)
            .where(UsedEmail.verified_at.is_not(None))
            .where(UsedEmail.verified_at < em_cutoff)
        )).rowcount
        # Captcha старше 30 дней
        cap_cutoff = datetime.utcnow() - timedelta(days=30)
        deleted_captchas = (await session.execute(
            delete(CaptchaChallenge)
            .where(CaptchaChallenge.requested_at < cap_cutoff)
        )).rowcount
        await session.commit()

    logger.info(
        f"[auto_cleanup] sendlog={deleted_sendlog} drafts={deleted_drafts} "
        f"emails={deleted_emails} captchas={deleted_captchas}"
    )


# ============================================================================
# Task: auto_prune — глубокая чистка раз в сутки
# ============================================================================

async def task_auto_prune() -> None:
    # 1. Контакты без активности >N дней → drop (не шлём такому пользователю)
    contact_cutoff = datetime.utcnow() - timedelta(days=settings.contact_inactive_days)
    async with async_session() as session:
        pruned_contacts = (await session.execute(
            delete(Contact)
            .where(Contact.last_msg_at.is_not(None))
            .where(Contact.last_msg_at < contact_cutoff)
        )).rowcount
        # 2. Failed/rejected drafts старше 7 дней
        d_cutoff = datetime.utcnow() - timedelta(days=7)
        pruned_drafts = (await session.execute(
            delete(Draft)
            .where(Draft.status.in_(["failed", "rejected"]))
            .where(Draft.created_at < d_cutoff)
        )).rowcount
        # 3. Banned/disabled аккаунты — освобождаем session_string чтобы не
        # держать в памяти большие строки (могут быть мегабайтами)
        scrubbed = (await session.execute(
            update(Account)
            .where(Account.enabled.is_(False))
            .where(Account.status.in_(["banned", "disabled", "geo_mismatch"]))
            .where(Account.session_string != "")
            .values(session_string="")
        )).rowcount
        await session.commit()

    logger.info(
        f"[auto_prune] contacts={pruned_contacts} drafts={pruned_drafts} "
        f"scrubbed_sessions={scrubbed}"
    )


# ============================================================================
# Task: auto_health — мониторинг и алерты
# ============================================================================

async def task_auto_health() -> None:
    async with async_session() as session:
        # 1. Heartbeats: какие шарды отвалились
        stale_cutoff = datetime.utcnow() - timedelta(
            seconds=settings.health_shard_stale_seconds
        )
        # Получаем все известные heartbeats
        beats = (await session.execute(
            select(ShardHeartbeat).order_by(ShardHeartbeat.shard_id)
        )).scalars().all()
        stale_shards = [b.shard_id for b in beats if b.last_beat < stale_cutoff]

        if stale_shards:
            await alert(
                f"Shards stale ({len(stale_shards)} штук, не отвечают >"
                f"{settings.health_shard_stale_seconds // 60} мин):\n"
                f"{stale_shards}",
                key="stale_shards",
            )

        # 2. Доли аккаунтов по статусам
        by_status = dict((s, c) for s, c in (await session.execute(
            select(Account.status, func.count(Account.id))
            .where(Account.enabled.is_(True))
            .group_by(Account.status)
        )).all())
        total_enabled = sum(by_status.values()) or 1
        active = by_status.get("active", 0)
        active_ratio = active / total_enabled

        if active_ratio < settings.health_active_ratio_min:
            await alert(
                f"Active ratio = {active_ratio:.0%} "
                f"({active}/{total_enabled}). Что-то горит:\n"
                f"by_status: {by_status}",
                key="low_active_ratio",
            )

        # 3. Отсутствие отправок за последний час
        no_send_cutoff = datetime.utcnow() - timedelta(
            minutes=settings.health_no_send_alert_minutes
        )
        recent_sends = (await session.execute(
            select(func.count(SendLog.id))
            .where(SendLog.sent_at >= no_send_cutoff)
            .where(SendLog.success.is_(True))
        )).scalar() or 0
        # Только если есть pending драфты в очереди — отсутствие отправок реально проблема
        pending = (await session.execute(
            select(func.count(Draft.id)).where(Draft.status == "pending")
        )).scalar() or 0

        if recent_sends == 0 and pending > 0:
            await alert(
                f"Нет успешных отправок за последние "
                f"{settings.health_no_send_alert_minutes} мин, "
                f"при этом {pending} pending драфтов в очереди. "
                f"Возможно sender'ы залипли.",
                key="no_sends",
            )

        # 4. Память: warning если много intake-pending зависло
        stuck_intake = (await session.execute(
            select(func.count(Account.id))
            .where(Account.intake_status == "processing")
        )).scalar() or 0
        if stuck_intake > 50:
            await alert(
                f"{stuck_intake} аккаунтов застряли в intake_status='processing'. "
                f"intake_worker может быть мёртв или зависший.",
                key="stuck_intake",
            )

    logger.debug(
        f"[health] active={active_ratio:.0%} pending_drafts={pending} "
        f"stale_shards={len(stale_shards)}"
    )


# ============================================================================
# Главный цикл: периодически дёргает каждую задачу
# ============================================================================

async def _run_periodic(
    name: str,
    fn: Callable[[], Awaitable[None]],
    interval: int,
    start_delay: int = 5,
) -> None:
    """Запускает fn() каждые interval секунд. Не падает на исключениях."""
    await asyncio.sleep(start_delay)
    while True:
        started = datetime.utcnow()
        try:
            await fn()
        except Exception as e:
            logger.error(f"[scheduler/{name}] error: {e}")
            await alert(f"scheduler {name} crashed: {e}", key=f"sched_{name}")
        took = (datetime.utcnow() - started).total_seconds()
        wait = max(interval - took, 1)
        await asyncio.sleep(wait)


async def run() -> None:
    await init_db()
    logger.info("Scheduler started")
    await alert("Scheduler запущен", silent=True, key="scheduler_start")

    tasks = [
        _run_periodic(
            "auto_generate", task_auto_generate, settings.auto_generate_interval,
            start_delay=30,
        ),
        _run_periodic(
            "auto_cleanup", task_auto_cleanup, settings.auto_cleanup_interval,
            start_delay=300,
        ),
        _run_periodic(
            "auto_health", task_auto_health, settings.auto_health_interval,
            start_delay=60,
        ),
        _run_periodic(
            "auto_prune", task_auto_prune, settings.auto_prune_interval,
            start_delay=600,
        ),
    ]
    await asyncio.gather(*tasks)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

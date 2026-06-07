"""
Framework для решения капч Telegram.

Точный механизм будет уточнён владельцем проекта позже. Сейчас:
  - Записываем challenge в таблицу CaptchaChallenge
  - Default solver = 'manual': ждём пока admin через CLI/psql решит
  - Расширение: интеграция с 2captcha / anticaptcha / custom через
    переменную CAPTCHA_SOLVER

API одинаковое: await solve_captcha(account_id, challenge_type, data)
возвращает строку-решение или None если timeout/нерешено.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Optional

from loguru import logger
from sqlalchemy import select

from .config import settings
from .db import async_session
from .models import Account, CaptchaChallenge


class CaptchaRequired(Exception):
    """Возникает когда Telegram потребовал капчу — оборачивает сырое исключение."""

    def __init__(self, challenge_type: str, challenge_data: str | None = None):
        self.challenge_type = challenge_type
        self.challenge_data = challenge_data


async def solve_captcha(
    account_id: int,
    challenge_type: str,
    challenge_data: str | None = None,
    timeout: int | None = None,
) -> Optional[str]:
    """
    Создаёт challenge в БД, ждёт solution.
    """
    timeout = timeout or settings.captcha_timeout_seconds

    # 1. Записать challenge в БД
    async with async_session() as session:
        ch = CaptchaChallenge(
            account_id=account_id,
            challenge_type=challenge_type,
            challenge_data=challenge_data,
            status="pending",
        )
        session.add(ch)
        await session.commit()
        await session.refresh(ch)
        challenge_id = ch.id

    logger.warning(
        f"[captcha] account={account_id} type={challenge_type} — "
        f"создан challenge #{challenge_id}, ожидаю решения"
    )

    # 2. Решить
    if settings.captcha_solver == "manual":
        return await _wait_manual_solve(challenge_id, timeout)

    elif settings.captcha_solver == "2captcha":
        return await _solve_via_2captcha(challenge_id, challenge_type, challenge_data, timeout)

    elif settings.captcha_solver == "anticaptcha":
        return await _solve_via_anticaptcha(challenge_id, challenge_type, challenge_data, timeout)

    else:
        logger.error(f"[captcha] unknown solver {settings.captcha_solver}")
        await _mark_failed(challenge_id, "unknown_solver")
        return None


async def _wait_manual_solve(challenge_id: int, timeout: int) -> Optional[str]:
    """
    Manual mode: ждём пока админ обновит row в БД руками через psql:
      UPDATE captcha_challenges SET status='solved', solution='<answer>' WHERE id=N;
    """
    deadline = time.monotonic() + timeout
    poll = 5

    while time.monotonic() < deadline:
        async with async_session() as session:
            ch = await session.get(CaptchaChallenge, challenge_id)
            if ch and ch.status == "solved" and ch.solution:
                ch.solved_at = datetime.utcnow()
                await session.commit()
                logger.success(f"[captcha] #{challenge_id} решён: {ch.solution[:40]}")
                return ch.solution
            if ch and ch.status == "failed":
                logger.warning(f"[captcha] #{challenge_id} помечен failed admin'ом")
                return None
        await asyncio.sleep(poll)

    await _mark_failed(challenge_id, "timeout")
    return None


async def _solve_via_2captcha(
    challenge_id: int,
    challenge_type: str,
    challenge_data: str | None,
    timeout: int,
) -> Optional[str]:
    """Stub: TODO интеграция с 2captcha API."""
    logger.error("[captcha] 2captcha solver — не реализовано, нужны детали")
    await _mark_failed(challenge_id, "not_implemented")
    return None


async def _solve_via_anticaptcha(
    challenge_id: int,
    challenge_type: str,
    challenge_data: str | None,
    timeout: int,
) -> Optional[str]:
    """Stub: TODO интеграция с anti-captcha API."""
    logger.error("[captcha] anticaptcha solver — не реализовано, нужны детали")
    await _mark_failed(challenge_id, "not_implemented")
    return None


async def _mark_failed(challenge_id: int, reason: str) -> None:
    async with async_session() as session:
        ch = await session.get(CaptchaChallenge, challenge_id)
        if ch:
            ch.status = "failed"
            ch.solution = reason
            ch.solved_at = datetime.utcnow()
            await session.commit()


async def mark_account_needs_captcha(account_id: int, reason: str) -> None:
    """Вывести аккаунт из ротации если капча не решается."""
    async with async_session() as session:
        acc = await session.get(Account, account_id)
        if acc:
            acc.status = "needs_captcha"
            acc.status_reason = reason
            acc.enabled = False
            await session.commit()
    logger.error(f"[captcha] account {account_id} выведен: needs_captcha ({reason})")

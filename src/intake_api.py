"""
FastAPI HTTP-сервер для приёма аккаунтов из внешнего источника
(warmup-пайплайн, ферма прогрева, твоё собственное хранилище и т.п.).

Endpoints:
  POST   /api/accounts/intake          — создать/обновить аккаунт + опц. fetch
  GET    /api/accounts                 — список со статусами
  GET    /api/accounts/{name}          — статус одного
  POST   /api/accounts/{name}/disable  — вывести из ротации
  POST   /api/accounts/{name}/enable   — вернуть в ротацию
  GET    /api/healthz                  — liveness (без auth)
  GET    /api/stats                    — общая статистика пула

Все endpoints (кроме /api/healthz) защищены Bearer-токеном из INTAKE_TOKEN.

Запуск:
  python -m src.intake_api
  или: uvicorn src.intake_api:app --host 0.0.0.0 --port 8090
"""
from __future__ import annotations

import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from loguru import logger
from pydantic import BaseModel, Field as PydField
from sqlalchemy import func, select

from .config import settings
from .db import async_session, init_db
from .import_accounts import import_one
from .models import Account, BlockedContact, CaptchaChallenge, Contact, Draft, SendLog
from .telethon_client import DEVICE_FIELDS, has_complete_fingerprint, missing_fingerprint_fields


# ============================================================================
# App lifecycle
# ============================================================================

@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    logger.info(
        f"Intake API listening {settings.intake_host}:{settings.intake_port} "
        f"(token: {settings.intake_token[:8]}...)"
    )
    yield


app = FastAPI(
    title="TG Broadcaster Intake API",
    version="1.0",
    lifespan=lifespan,
)


# ============================================================================
# Auth
# ============================================================================

def require_token(request: Request) -> None:
    """Bearer token из INTAKE_TOKEN."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    token = auth.split(" ", 1)[1].strip()
    if not secrets.compare_digest(token, settings.intake_token):
        raise HTTPException(401, "invalid token")


# ============================================================================
# Request/Response models
# ============================================================================

class IntakeRequest(BaseModel):
    """Payload для POST /api/accounts/intake."""
    name: str = PydField(min_length=1, max_length=64)
    phone: str = PydField(min_length=4, max_length=32)
    session_string: str = PydField(min_length=10)

    # Реальный device-fingerprint — все 5 нужны при STRICT_REAL_FINGERPRINT=true
    device_model: str | None = None
    system_version: str | None = None
    app_version: str | None = None
    lang_code: str | None = None
    system_lang_code: str | None = None

    # Опциональные флаги
    auto_fetch_contacts: bool = True  # выкачать контакты сразу
    enabled: bool = True


class IntakeResponse(BaseModel):
    account_id: int | None
    name: str
    created: bool
    status: str
    reason: str | None
    contacts_total: int
    contacts_new: int
    duration_seconds: float


class AccountResponse(BaseModel):
    id: int
    name: str
    phone: str
    status: str
    status_reason: str | None
    enabled: bool
    carrier: str | None
    has_real_fingerprint: bool
    missing_fingerprint_fields: list[str]
    last_send_at: datetime | None
    created_at: datetime
    contacts_count: int


class StatsResponse(BaseModel):
    accounts_total: int
    accounts_by_status: dict
    contacts_total: int
    drafts_pending: int
    drafts_sent: int
    drafts_failed: int
    sent_last_hour: int
    sent_last_24h: int
    failed_last_24h: int
    blocked_contacts: int
    captcha_pending: int


# ============================================================================
# Helpers
# ============================================================================

def _account_to_response(acc: Account, contacts_count: int) -> AccountResponse:
    return AccountResponse(
        id=acc.id,
        name=acc.name,
        phone=acc.phone,
        status=acc.status,
        status_reason=acc.status_reason,
        enabled=acc.enabled,
        carrier=acc.carrier,
        has_real_fingerprint=has_complete_fingerprint(acc),
        missing_fingerprint_fields=missing_fingerprint_fields(acc),
        last_send_at=acc.last_send_at,
        created_at=acc.created_at,
        contacts_count=contacts_count,
    )


async def _contacts_count(session, account_id: int) -> int:
    r = await session.execute(
        select(func.count(Contact.id)).where(Contact.account_id == account_id)
    )
    return r.scalar() or 0


# ============================================================================
# Endpoints
# ============================================================================

@app.get("/api/healthz")
async def healthz():
    """Liveness — без auth, для оркестратора."""
    try:
        async with async_session() as session:
            await session.execute(select(func.count(Account.id)))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(503, f"db error: {e}")


@app.post(
    "/api/accounts/intake",
    response_model=IntakeResponse,
    dependencies=[Depends(require_token)],
)
async def intake(payload: IntakeRequest):
    """
    Принимает аккаунт (создать/обновить) + опционально выкачивает контакты.

    Если STRICT_REAL_FINGERPRINT=true (default), missing device-поля → 422.

    Body — см. IntakeRequest. Возвращает IntakeResponse с фактическим статусом.

    Timeout — INTAKE_SYNC_TIMEOUT_SECONDS из .env (по умолчанию 180 сек).
    Если fetch_contacts=true и у акк много диалогов, может занять время.
    """
    started = time.monotonic()
    row = {
        "name": payload.name,
        "phone": payload.phone,
        "session_string": payload.session_string,
        "device_model": payload.device_model or "",
        "system_version": payload.system_version or "",
        "app_version": payload.app_version or "",
        "lang_code": payload.lang_code or "",
        "system_lang_code": payload.system_lang_code or "",
    }

    try:
        result = await asyncio.wait_for(
            import_one(
                row,
                interactive=False,
                fetch_contacts=payload.auto_fetch_contacts,
            ),
            timeout=settings.intake_sync_timeout_seconds,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            504,
            f"intake timed out after {settings.intake_sync_timeout_seconds}s — "
            f"увеличь INTAKE_SYNC_TIMEOUT_SECONDS или поставь auto_fetch_contacts=false",
        )

    duration = round(time.monotonic() - started, 2)
    logger.info(
        f"[intake] {payload.name} → status={result['status']} "
        f"contacts={result['contacts_total']} ({duration}s)"
    )

    # Если enabled=false в payload — выводим из ротации после импорта
    if not payload.enabled and result["account_id"]:
        async with async_session() as session:
            acc = await session.get(Account, result["account_id"])
            if acc:
                acc.enabled = False
                await session.commit()

    return IntakeResponse(
        account_id=result["account_id"],
        name=result["name"],
        created=result["created"],
        status=result["status"],
        reason=result["reason"],
        contacts_total=result["contacts_total"],
        contacts_new=result["contacts_new"],
        duration_seconds=duration,
    )


@app.get(
    "/api/accounts",
    response_model=list[AccountResponse],
    dependencies=[Depends(require_token)],
)
async def list_accounts(
    limit: int = 100,
    offset: int = 0,
    status_: Optional[str] = None,
):
    """Список аккаунтов с статусами и contacts_count."""
    async with async_session() as session:
        q = select(Account).order_by(Account.name)
        if status_:
            q = q.where(Account.status == status_)
        q = q.offset(offset).limit(min(limit, 500))
        accs = (await session.execute(q)).scalars().all()
        return [
            _account_to_response(a, await _contacts_count(session, a.id))
            for a in accs
        ]


@app.get(
    "/api/accounts/{name}",
    response_model=AccountResponse,
    dependencies=[Depends(require_token)],
)
async def get_account(name: str):
    async with async_session() as session:
        acc = (await session.execute(
            select(Account).where(Account.name == name)
        )).scalar_one_or_none()
        if not acc:
            raise HTTPException(404, "account not found")
        return _account_to_response(acc, await _contacts_count(session, acc.id))


@app.post(
    "/api/accounts/{name}/disable",
    dependencies=[Depends(require_token)],
)
async def disable_account(name: str):
    async with async_session() as session:
        acc = (await session.execute(
            select(Account).where(Account.name == name)
        )).scalar_one_or_none()
        if not acc:
            raise HTTPException(404, "account not found")
        acc.enabled = False
        await session.commit()
    return {"ok": True, "name": name, "enabled": False}


@app.post(
    "/api/accounts/{name}/enable",
    dependencies=[Depends(require_token)],
)
async def enable_account(name: str):
    async with async_session() as session:
        acc = (await session.execute(
            select(Account).where(Account.name == name)
        )).scalar_one_or_none()
        if not acc:
            raise HTTPException(404, "account not found")
        if acc.status in ("banned", "geo_mismatch", "disabled"):
            # Возвращаем в активный
            acc.status = "active"
            acc.status_reason = None
        acc.enabled = True
        await session.commit()
    return {"ok": True, "name": name, "enabled": True, "status": acc.status}


@app.get(
    "/api/stats",
    response_model=StatsResponse,
    dependencies=[Depends(require_token)],
)
async def stats():
    """Общая статистика по пулу."""
    async with async_session() as session:
        accounts_total = (await session.execute(
            select(func.count(Account.id))
        )).scalar() or 0
        by_status = dict((s, c) for s, c in (await session.execute(
            select(Account.status, func.count(Account.id)).group_by(Account.status)
        )).all())
        contacts_total = (await session.execute(
            select(func.count(Contact.id))
        )).scalar() or 0
        drafts_pending = (await session.execute(
            select(func.count(Draft.id)).where(Draft.status == "pending")
        )).scalar() or 0
        drafts_sent = (await session.execute(
            select(func.count(Draft.id)).where(Draft.status == "sent")
        )).scalar() or 0
        drafts_failed = (await session.execute(
            select(func.count(Draft.id)).where(Draft.status == "failed")
        )).scalar() or 0
        since_1h = datetime.utcnow() - timedelta(hours=1)
        since_24h = datetime.utcnow() - timedelta(hours=24)
        sent_1h = (await session.execute(
            select(func.count(SendLog.id))
            .where(SendLog.sent_at >= since_1h, SendLog.success.is_(True))
        )).scalar() or 0
        sent_24h = (await session.execute(
            select(func.count(SendLog.id))
            .where(SendLog.sent_at >= since_24h, SendLog.success.is_(True))
        )).scalar() or 0
        failed_24h = (await session.execute(
            select(func.count(SendLog.id))
            .where(SendLog.sent_at >= since_24h, SendLog.success.is_(False))
        )).scalar() or 0
        blocked = (await session.execute(
            select(func.count(BlockedContact.id))
        )).scalar() or 0
        captcha_pending = (await session.execute(
            select(func.count(CaptchaChallenge.id))
            .where(CaptchaChallenge.status == "pending")
        )).scalar() or 0

    return StatsResponse(
        accounts_total=accounts_total,
        accounts_by_status=by_status,
        contacts_total=contacts_total,
        drafts_pending=drafts_pending,
        drafts_sent=drafts_sent,
        drafts_failed=drafts_failed,
        sent_last_hour=sent_1h,
        sent_last_24h=sent_24h,
        failed_last_24h=failed_24h,
        blocked_contacts=blocked,
        captcha_pending=captcha_pending,
    )


def main() -> None:
    uvicorn.run(
        "src.intake_api:app",
        host=settings.intake_host,
        port=settings.intake_port,
        reload=False,
    )


if __name__ == "__main__":
    main()

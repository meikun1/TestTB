"""
Веб-дашборд + API для мульти-аккаунтного пула.

Страницы (HTTP Basic admin:<DASHBOARD_TOKEN>):
  GET /                 — обзор: счётчики, активность за 24ч
  GET /accounts         — таблица аккаунтов
  GET /accounts/{name}  — детали + последние отправки
  GET /queue            — очередь черновиков по аккаунтам
  GET /send-log         — последние 200 отправок

JSON API (Bearer <DASHBOARD_TOKEN>):
  POST /api/accounts/intake — принять сессию извне (StringSession),
                              создать/обновить аккаунт
  GET  /api/accounts        — список со статусами
  GET  /api/accounts/{name} — статус одного
  GET  /api/healthz         — без auth, пинг для оркестратора

Запуск:
  python -m src.dashboard.app
  или: uvicorn src.dashboard.app:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field as PydField
from sqlalchemy import select, func, desc

from config import settings, PROJECT_ROOT
from src.utils import async_session, Account, Contact, Draft, SendLog, bus
from .auth import require_auth


TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="TG Assistant Dashboard")


# ---------- Helpers ----------

async def _sent_in_window(session, account_id: int, hours: float) -> int:
    since = datetime.utcnow() - timedelta(hours=hours)
    result = await session.execute(
        select(func.count(SendLog.id))
        .where(SendLog.account_id == account_id)
        .where(SendLog.sent_at >= since)
        .where(SendLog.success.is_(True))
    )
    return result.scalar() or 0


async def _account_row(session, acc: Account) -> dict:
    sent_24h = await _sent_in_window(session, acc.id, 24)
    sent_1h = await _sent_in_window(session, acc.id, 1)
    return {
        "id": acc.id,
        "name": acc.name,
        "phone": acc.phone,
        "status": acc.status,
        "status_reason": acc.status_reason,
        "enabled": acc.enabled,
        "has_proxy": bool(acc.proxy),
        "daily_limit": acc.daily_limit,
        "hourly_limit": acc.hourly_limit,
        "sent_24h": sent_24h,
        "sent_1h": sent_1h,
        "flood_until": acc.flood_until.isoformat() if acc.flood_until else None,
        "last_send_at": acc.last_send_at.isoformat() if acc.last_send_at else None,
    }


# ---------- HTML страницы ----------

@app.get("/", response_class=HTMLResponse)
async def overview(request: Request, _: str = Depends(require_auth)):
    async with async_session() as session:
        total = (await session.execute(
            select(func.count(Account.id))
        )).scalar() or 0
        by_status = dict(
            (s, c) for s, c in (await session.execute(
                select(Account.status, func.count(Account.id))
                .group_by(Account.status)
            )).all()
        )
        since = datetime.utcnow() - timedelta(hours=24)
        sent_24h = (await session.execute(
            select(func.count(SendLog.id))
            .where(SendLog.sent_at >= since, SendLog.success.is_(True))
        )).scalar() or 0
        failed_24h = (await session.execute(
            select(func.count(SendLog.id))
            .where(SendLog.sent_at >= since, SendLog.success.is_(False))
        )).scalar() or 0
        pending = (await session.execute(
            select(func.count(Draft.id))
            .where(Draft.status.in_(["pending", "approved"]))
        )).scalar() or 0
        contacts = (await session.execute(
            select(func.count(Contact.id))
        )).scalar() or 0

    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "total": total,
            "by_status": by_status,
            "sent_24h": sent_24h,
            "failed_24h": failed_24h,
            "pending": pending,
            "contacts": contacts,
        },
    )


@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request, _: str = Depends(require_auth)):
    async with async_session() as session:
        accs = (await session.execute(
            select(Account).order_by(Account.name)
        )).scalars().all()
        rows = [await _account_row(session, a) for a in accs]
    return templates.TemplateResponse(request, "accounts.html", {"accounts": rows})


@app.get("/accounts/{name}", response_class=HTMLResponse)
async def account_detail(
    request: Request, name: str, _: str = Depends(require_auth)
):
    async with async_session() as session:
        acc = (await session.execute(
            select(Account).where(Account.name == name)
        )).scalar_one_or_none()
        if not acc:
            raise HTTPException(404, f"account {name} not found")

        row = await _account_row(session, acc)
        contacts_count = (await session.execute(
            select(func.count(Contact.id)).where(Contact.account_id == acc.id)
        )).scalar() or 0

        recent = (await session.execute(
            select(SendLog, Draft, Contact)
            .join(Draft, SendLog.draft_id == Draft.id)
            .join(Contact, Draft.contact_id == Contact.id)
            .where(SendLog.account_id == acc.id)
            .order_by(desc(SendLog.sent_at))
            .limit(30)
        )).all()
        recent_rows = [
            {
                "sent_at": sl.sent_at,
                "success": sl.success,
                "error": sl.error,
                "contact": c.first_name or c.username or str(c.tg_user_id),
                "text": (d.text or "")[:120],
            }
            for sl, d, c in recent
        ]

    return templates.TemplateResponse(
        request,
        "account_detail.html",
        {"acc": row, "contacts_count": contacts_count, "recent": recent_rows},
    )


@app.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request, _: str = Depends(require_auth)):
    async with async_session() as session:
        rows = (await session.execute(
            select(Account.name, Draft.status, func.count(Draft.id))
            .join(Account, Account.id == Draft.account_id)
            .group_by(Account.name, Draft.status)
            .order_by(Account.name)
        )).all()
    grouped: dict[str, dict[str, int]] = {}
    for name, status_, count in rows:
        grouped.setdefault(name, {})[status_] = count
    return templates.TemplateResponse(request, "queue.html", {"grouped": grouped})


@app.get("/send-log", response_class=HTMLResponse)
async def send_log_page(request: Request, _: str = Depends(require_auth)):
    async with async_session() as session:
        rows = (await session.execute(
            select(SendLog, Account.name, Draft, Contact)
            .join(Account, Account.id == SendLog.account_id)
            .join(Draft, Draft.id == SendLog.draft_id)
            .join(Contact, Contact.id == Draft.contact_id)
            .order_by(desc(SendLog.sent_at))
            .limit(200)
        )).all()
    items = [
        {
            "sent_at": sl.sent_at,
            "account": name,
            "success": sl.success,
            "error": sl.error,
            "contact": c.first_name or c.username or str(c.tg_user_id),
            "text": (d.text or "")[:100],
        }
        for sl, name, d, c in rows
    ]
    return templates.TemplateResponse(request, "send_log.html", {"items": items})


# ---------- JSON API ----------

class IntakeRequest(BaseModel):
    name: str = PydField(min_length=1, max_length=64)
    phone: str = PydField(min_length=3, max_length=32)
    session_string: str = PydField(min_length=10)
    proxy: str | None = None
    daily_limit: int | None = None
    hourly_limit: int | None = None
    enabled: bool = True


class IntakeResponse(BaseModel):
    account_id: int
    name: str
    created: bool


@app.post("/api/accounts/intake", response_model=IntakeResponse)
async def intake(payload: IntakeRequest, _: str = Depends(require_auth)):
    """
    Принимает свежепрогретую сессию от внешнего источника (warmup-пайплайн)
    и регистрирует/обновляет аккаунт в пуле. После этого sender сразу
    подхватит его при следующем рестарте/перезагрузке пула.
    """
    async with async_session() as session:
        existing = (await session.execute(
            select(Account).where(Account.name == payload.name)
        )).scalar_one_or_none()

        fields = dict(
            phone=payload.phone,
            session_string=payload.session_string,
            proxy=payload.proxy,
            daily_limit=payload.daily_limit or settings.default_daily_limit,
            hourly_limit=payload.hourly_limit or settings.default_hourly_limit,
            enabled=payload.enabled,
        )

        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
            # сбрасываем горелые статусы — внешний источник дал свежую сессию
            existing.status = "active"
            existing.status_reason = None
            existing.flood_until = None
            # шард-id на случай первого назначения
            existing.shard_id = existing.id % settings.worker_count
            await session.commit()
            account_id = existing.id
            created = False
        else:
            acc = Account(name=payload.name, **fields)
            session.add(acc)
            await session.commit()
            await session.refresh(acc)
            # шард-id присваиваем сразу
            acc.shard_id = acc.id % settings.worker_count
            await session.commit()
            account_id = acc.id
            created = True

    # уведомить sender-воркеры что в пуле новый/обновлённый аккаунт
    await bus.publish_pool_reload(
        reason=f"intake:{payload.name}",
        account_id=account_id,
    )
    return IntakeResponse(account_id=account_id, name=payload.name, created=created)


@app.get("/api/accounts")
async def api_accounts(_: str = Depends(require_auth)):
    async with async_session() as session:
        accs = (await session.execute(
            select(Account).order_by(Account.name)
        )).scalars().all()
        return [await _account_row(session, a) for a in accs]


@app.get("/api/accounts/{name}")
async def api_account(name: str, _: str = Depends(require_auth)):
    async with async_session() as session:
        acc = (await session.execute(
            select(Account).where(Account.name == name)
        )).scalar_one_or_none()
        if not acc:
            raise HTTPException(404, "not found")
        return await _account_row(session, acc)


@app.get("/api/healthz")
async def healthz():
    """Без auth — для liveness probe."""
    try:
        async with async_session() as session:
            await session.execute(select(func.count(Account.id)))
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


def main() -> None:
    uvicorn.run(
        "src.dashboard.app:app",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        reload=False,
    )


if __name__ == "__main__":
    main()

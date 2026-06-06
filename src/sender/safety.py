"""
Антиспам-проверки. Каждый чек возвращает (ok: bool, reason: str).
Если хоть один false — отправку не делаем.

Все проверки скоупятся на конкретный аккаунт (account_id).
Лимиты per-account живут в таблице Account, диспетчер их и проверяет.
Здесь — проверки на уровне контакта и черновика.
"""
import hashlib
import re
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, func, desc

from config import settings
from src.utils import Account, Contact, Message, Draft, SendLog


# ---------- Окна времени ----------

def is_sleeping_hours(now: datetime) -> bool:
    """Обязательный «сон»: ничего не отправляем."""
    h = now.hour
    s, e = settings.mandatory_sleep_start, settings.mandatory_sleep_end
    if s < e:
        return s <= h < e
    return h >= s or h < e


def is_working_hours(now: datetime) -> bool:
    return settings.working_hours_start <= now.hour < settings.working_hours_end


def is_weekend(now: datetime) -> bool:
    return now.weekday() >= 5


# ---------- Динамические лимиты ----------

def effective_daily_limit(
    account: Account,
    now: datetime,
    first_send_date: Optional[datetime],
) -> int:
    """С учётом ramp-up и выходных, применённых к лимиту конкретного аккаунта."""
    limit = account.daily_limit

    if settings.rampup_enabled and first_send_date:
        days_running = (now - first_send_date).days
        if days_running < settings.rampup_days:
            progress = days_running / settings.rampup_days
            rampup_limit = int(
                settings.rampup_start_limit
                + (account.daily_limit - settings.rampup_start_limit) * progress
            )
            limit = min(limit, rampup_limit)

    if is_weekend(now):
        limit = int(limit * settings.weekend_multiplier)

    return max(limit, 1)


# ---------- Подсчёт активности (per-account) ----------

async def count_sent_in_window(session, account_id: int, hours: float) -> int:
    since = datetime.utcnow() - timedelta(hours=hours)
    result = await session.execute(
        select(func.count(SendLog.id))
        .where(SendLog.account_id == account_id)
        .where(SendLog.sent_at >= since, SendLog.success.is_(True))
    )
    return result.scalar() or 0


async def get_first_send_date(session, account_id: int) -> Optional[datetime]:
    result = await session.execute(
        select(func.min(SendLog.sent_at))
        .where(SendLog.account_id == account_id, SendLog.success.is_(True))
    )
    return result.scalar()


# ---------- Reply rate (per-account) ----------

async def get_reply_rate(session, account_id: int) -> tuple[float, int]:
    since = datetime.utcnow() - timedelta(hours=settings.reply_rate_check_hours)
    sends = (await session.execute(
        select(Draft, SendLog)
        .join(SendLog, SendLog.draft_id == Draft.id)
        .where(
            SendLog.account_id == account_id,
            SendLog.sent_at >= since,
            SendLog.success.is_(True),
        )
        .order_by(desc(SendLog.sent_at))
        .limit(settings.reply_rate_window)
    )).all()

    if len(sends) < 5:
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


# ---------- Валидация черновика ----------

URL_RE = re.compile(r"https?://|t\.me/|tg://", re.IGNORECASE)


def validate_draft_text(text: str) -> tuple[bool, str]:
    text = text.strip()
    if len(text) < 10:
        return False, "too_short"
    if len(text) > 600:
        return False, "too_long"

    if URL_RE.search(text[:30]):
        return False, "url_at_start"

    bad_patterns = [
        "не упустите",
        "только сегодня",
        "успей",
        "акция",
        "промокод",
        "перейди по ссылке",
    ]
    lower = text.lower()
    for p in bad_patterns:
        if p in lower:
            return False, f"bad_pattern:{p}"

    return True, "ok"


def text_hash(text: str) -> str:
    norm = "".join(c.lower() for c in text if c.isalnum())
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


async def is_duplicate_text(session, text: str, days: int = 90) -> bool:
    """
    Не отправляли ли мы недавно идентичный по сути текст — глобально,
    через любой аккаунт. На масштабе одинаковые тексты с разных номеров —
    самый быстрый путь к кластеризации в антифроде.
    """
    h = text_hash(text)
    since = datetime.utcnow() - timedelta(days=days)
    sent_drafts = (await session.execute(
        select(Draft.text)
        .where(Draft.status == "sent", Draft.sent_at >= since)
    )).scalars().all()

    for existing in sent_drafts:
        if text_hash(existing) == h:
            return True
    return False


# ---------- Per-contact проверки ----------

async def can_send_to_contact(session, contact: Contact) -> tuple[bool, str]:
    incoming = await session.execute(
        select(func.count(Message.id))
        .where(Message.contact_id == contact.id, Message.from_me.is_(False))
    )
    if (incoming.scalar() or 0) < settings.min_incoming_messages:
        return False, "stranger"

    cutoff = datetime.utcnow() - timedelta(days=settings.min_days_between_sends)
    recent_send = await session.execute(
        select(func.count(Draft.id))
        .where(
            Draft.contact_id == contact.id,
            Draft.status == "sent",
            Draft.sent_at >= cutoff,
        )
    )
    if (recent_send.scalar() or 0) > 0:
        return False, "too_recent"

    if contact.category not in ("warm", "cooling"):
        return False, f"category:{contact.category}"

    return True, "ok"


# ---------- Главная проверка перед отправкой ----------

async def pre_send_check(
    session,
    account: Account,
    contact: Contact,
    draft: Draft,
) -> tuple[bool, str]:
    """
    Account-level лимиты (daily/hourly/flood/banned) проверяет диспетчер.
    Здесь — окна времени, ramp-up-эффективный потолок, reply rate
    и проверки на контакт/текст.
    """
    now = datetime.now()

    if is_sleeping_hours(now):
        return False, "sleeping_hours"
    if not is_working_hours(now):
        return False, "outside_working_hours"

    # Перепроверяем дневной лимит с учётом ramp-up
    first_send = await get_first_send_date(session, account.id)
    daily_cap = effective_daily_limit(account, now, first_send)
    sent_24h = await count_sent_in_window(session, account.id, 24)
    if sent_24h >= daily_cap:
        return False, f"daily_limit:{sent_24h}/{daily_cap}"

    rate, total = await get_reply_rate(session, account.id)
    if total >= 5 and rate < settings.reply_rate_min:
        return False, f"low_reply_rate:{rate:.2f}"

    ok, reason = await can_send_to_contact(session, contact)
    if not ok:
        return False, reason

    ok, reason = validate_draft_text(draft.text)
    if not ok:
        return False, f"draft_invalid:{reason}"

    if await is_duplicate_text(session, draft.text):
        return False, "duplicate_text"

    return True, "ok"

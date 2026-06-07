"""Active hours по timezone Узбекистана."""
from datetime import datetime

import pytz

from .config import settings


def is_active_now() -> bool:
    """True если сейчас active-окно по ACTIVE_TIMEZONE."""
    tz = pytz.timezone(settings.active_timezone)
    now = datetime.now(tz)
    return settings.active_hours_start <= now.hour < settings.active_hours_end


def seconds_until_active() -> int:
    """Сколько секунд до начала следующего active-окна. Если сейчас active — 0."""
    if is_active_now():
        return 0

    tz = pytz.timezone(settings.active_timezone)
    now = datetime.now(tz)

    # Сегодня ACTIVE_HOURS_START ещё впереди?
    today_start = now.replace(
        hour=settings.active_hours_start, minute=0, second=0, microsecond=0,
    )
    if now < today_start:
        return int((today_start - now).total_seconds())

    # Иначе — завтра ACTIVE_HOURS_START
    from datetime import timedelta
    tomorrow_start = today_start + timedelta(days=1)
    return int((tomorrow_start - now).total_seconds())

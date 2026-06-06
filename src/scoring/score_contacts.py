"""
Скоринг контактов.

Категории:
  warm    — общались <14 дней назад
  cooling — 14–60 дней
  cold    — 60–180 дней
  dead    — >180 дней или односторонний диалог

Score (0..1) — насколько уместно сейчас писать. Учитывает:
  - давность последнего сообщения (чем свежее, тем выше)
  - кто писал последним (если ты — score ниже, человек не ответил)
  - объём истории (короткие диалоги — менее тёплые)
  - двусторонность

Запуск: python -m src.scoring.score_contacts
"""
import asyncio
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select, func

from src.utils import async_session, Contact, Message


WARM_DAYS = 14
COOLING_DAYS = 60
COLD_DAYS = 180


def categorize(days_since: float, from_me_count: int, from_them_count: int) -> str:
    # Односторонний диалог (только я писал, ответов почти нет) → мёртвый
    if from_them_count == 0 or from_me_count / max(from_them_count, 1) > 5:
        return "dead"

    if days_since <= WARM_DAYS:
        return "warm"
    if days_since <= COOLING_DAYS:
        return "cooling"
    if days_since <= COLD_DAYS:
        return "cold"
    return "dead"


def score(
    days_since: float,
    last_from_me: bool,
    total: int,
    from_me_count: int,
    from_them_count: int,
) -> float:
    """Простая эвристика 0..1."""
    # Базовая компонента: затухание по времени (полураспад ~30 дней)
    time_score = 0.5 ** (days_since / 30)

    # Штраф если я писал последним и нет ответа
    if last_from_me:
        time_score *= 0.4

    # Бонус за длинную историю
    history_bonus = min(total / 100, 1.0) * 0.2

    # Бонус за двусторонность
    if from_them_count and from_me_count:
        ratio = min(from_me_count, from_them_count) / max(from_me_count, from_them_count)
        symmetry_bonus = ratio * 0.2
    else:
        symmetry_bonus = 0

    return min(time_score + history_bonus + symmetry_bonus, 1.0)


async def score_all() -> None:
    now = datetime.utcnow()
    updated = 0

    async with async_session() as session:
        contacts = (await session.execute(select(Contact))).scalars().all()

        for c in contacts:
            # Считаем from_me / from_them
            from_me_q = await session.execute(
                select(func.count(Message.id)).where(
                    Message.contact_id == c.id, Message.from_me.is_(True)
                )
            )
            from_them_q = await session.execute(
                select(func.count(Message.id)).where(
                    Message.contact_id == c.id, Message.from_me.is_(False)
                )
            )
            from_me_count = from_me_q.scalar() or 0
            from_them_count = from_them_q.scalar() or 0

            if c.last_msg_at is None:
                c.category = "dead"
                c.score = 0.0
                continue

            days_since = (now - c.last_msg_at).total_seconds() / 86400
            c.category = categorize(days_since, from_me_count, from_them_count)
            c.score = round(
                score(
                    days_since,
                    bool(c.last_msg_from_me),
                    c.total_messages,
                    from_me_count,
                    from_them_count,
                ),
                3,
            )
            updated += 1

        await session.commit()

    logger.success(f"Scored {updated} contacts")

    # Печатаем топ-15 для проверки
    async with async_session() as session:
        top = (
            await session.execute(
                select(Contact)
                .where(Contact.category.in_(["warm", "cooling"]))
                .order_by(Contact.score.desc())
                .limit(15)
            )
        ).scalars().all()

        logger.info("Top-15 warm/cooling:")
        for c in top:
            name = c.first_name or c.username or str(c.tg_user_id)
            logger.info(f"  {c.score:.3f} [{c.category:<7}] {name}")


if __name__ == "__main__":
    asyncio.run(score_all())

"""
Генерация черновиков сообщений для топ-N контактов.

Использует Claude API. Позже заменим на локальный inference (vLLM + LoRA).

Запуск:
    python -m src.generator.generate_drafts --top 10 --goal personal
    python -m src.generator.generate_drafts --top 10 --goal project --hook "запускаю новый сервис для X"
"""
import argparse
import asyncio
import json
import random
from datetime import datetime

from anthropic import AsyncAnthropic
from loguru import logger
from sqlalchemy import select, desc
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings
from src.utils import async_session, Contact, Message, Draft
from .prompts import SYSTEM_PROMPT, USER_TEMPLATE


client = AsyncAnthropic(api_key=settings.anthropic_api_key)
MODEL = "claude-opus-4-7"


async def collect_style_examples(session, account_id: int, limit: int = 8) -> str:
    """Собирает несколько свежих реплик данного аккаунта — для few-shot стиля."""
    result = await session.execute(
        select(Message.text)
        .join(Contact, Contact.id == Message.contact_id)
        .where(Contact.account_id == account_id)
        .where(Message.from_me.is_(True))
        .where(func_len(Message.text).between(20, 200))
        .order_by(desc(Message.date))
        .limit(limit * 3)
    )
    texts = [t for (t,) in result.all()]
    random.shuffle(texts)
    return "\n".join(f"- {t}" for t in texts[:limit])


def func_len(col):
    # SQLite LENGTH() — короткий хелпер
    from sqlalchemy import func
    return func.length(col)


async def collect_history(session, contact_id: int, limit: int = 15) -> str:
    """Последние N сообщений по контакту, новые сверху."""
    result = await session.execute(
        select(Message)
        .where(Message.contact_id == contact_id)
        .order_by(desc(Message.date))
        .limit(limit)
    )
    msgs = result.scalars().all()
    lines = []
    for m in msgs:
        author = "Я" if m.from_me else "Он"
        lines.append(f"[{m.date:%Y-%m-%d}] {author}: {m.text}")
    return "\n".join(lines) if lines else "(история пустая)"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20))
async def call_llm(system: str, user: str) -> dict:
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = resp.content[0].text.strip()
    # Срезаем возможные ```json ... ``` обёртки
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip(" `\n")
    return json.loads(text)


async def generate_for_contact(session, contact: Contact, goal: str, hook: str) -> None:
    style_examples = await collect_style_examples(session, contact.account_id)
    history = await collect_history(session, contact.id)

    days_since = (
        (datetime.utcnow() - contact.last_msg_at).days
        if contact.last_msg_at
        else 999
    )
    last_author = "меня" if contact.last_msg_from_me else "него"
    name = contact.first_name or contact.username or "контакт"

    user_msg = USER_TEMPLATE.format(
        style_examples=style_examples or "(нет примеров)",
        name=name,
        category=contact.category or "unknown",
        days_since=days_since,
        last_author=last_author,
        history=history,
        goal=goal,
        hook=hook or "(нет специального повода)",
    )

    try:
        result = await call_llm(SYSTEM_PROMPT, user_msg)
    except Exception as e:
        logger.error(f"LLM error for {name}: {e}")
        return

    if result.get("skip"):
        logger.info(f"Skip {name}: {result.get('reason')}")
        return

    for variant in result.get("variants", []):
        session.add(
            Draft(
                contact_id=contact.id,
                account_id=contact.account_id,
                text=variant["text"],
                variant_label=variant.get("label"),
                status="pending",
            )
        )
    logger.success(f"Drafts for {name}: {len(result.get('variants', []))}")


async def main(top_n: int, goal: str, hook: str, account_name: str | None) -> None:
    """
    Генерирует черновики для топ-N контактов.

    Без --account: top_n берётся ПО КАЖДОМУ аккаунту отдельно (для масштаба).
    С --account <name>: только контакты этого аккаунта.
    """
    from src.utils import Account

    async with async_session() as session:
        acc_query = select(Account).where(Account.enabled.is_(True))
        if account_name:
            acc_query = acc_query.where(Account.name == account_name)
        accounts = (await session.execute(acc_query)).scalars().all()

        if not accounts:
            logger.warning("Нет подходящих аккаунтов")
            return

        for acc in accounts:
            contacts = (
                await session.execute(
                    select(Contact)
                    .where(Contact.account_id == acc.id)
                    .where(Contact.category.in_(["warm", "cooling"]))
                    .order_by(Contact.score.desc())
                    .limit(top_n)
                )
            ).scalars().all()

            logger.info(f"[{acc.name}] Generating drafts for {len(contacts)} contacts...")
            for c in contacts:
                await generate_for_contact(session, c, goal, hook)
                await session.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument(
        "--goal",
        choices=["personal", "project", "reactivate"],
        default="personal",
    )
    parser.add_argument("--hook", type=str, default="")
    parser.add_argument(
        "--account",
        type=str,
        default=None,
        help="Только для одного аккаунта; иначе по всем enabled",
    )
    args = parser.parse_args()

    asyncio.run(main(args.top, args.goal, args.hook, args.account))

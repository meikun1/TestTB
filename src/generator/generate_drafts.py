"""
Генерация черновиков сообщений + опционально прикрепление файла/ссылки.

Запуск:
    python -m src.generator.generate_drafts --top 10
    python -m src.generator.generate_drafts --top 10 --account acc01 \
        --attach photo:attachments/offer.png --attach-caption "оффер"

Аргумент --attach задаёт вложение, которое сохранится во ВСЕ создаваемые
драфты этого запуска. Формат: <kind>:<ref>
  photo:attachments/offer.png
  video:attachments/promo.mp4
  document:attachments/brochure.pdf
  voice:attachments/intro.ogg
  link:https://example.com/offer

Если хочешь персонализировать вложение под контакт — это делается отдельным
скриптом, который пишет напрямую в Draft.attachment_*.
"""
import argparse
import asyncio
import json
import random
from datetime import datetime

from anthropic import AsyncAnthropic
from loguru import logger
from sqlalchemy import select, desc, func
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings
from src.utils import async_session, Account, Contact, Message, Draft
from .prompts import SYSTEM_PROMPT, USER_TEMPLATE, attachment_hint


client = AsyncAnthropic(api_key=settings.anthropic_api_key)
MODEL = settings.anthropic_model


def func_len(col):
    return func.length(col)


async def collect_style_examples(session, account_id: int, limit: int = 8) -> str:
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


async def collect_history(session, contact_id: int, limit: int = 15) -> str:
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
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip(" `\n")
    return json.loads(text)


def _parse_attach(s: str | None) -> tuple[str | None, str | None]:
    """photo:path/foo.png → ('photo', 'path/foo.png')"""
    if not s:
        return None, None
    if ":" not in s:
        raise ValueError(f"--attach должен быть kind:ref, получил: {s}")
    kind, ref = s.split(":", 1)
    valid = {"photo", "video", "audio", "voice", "document", "link"}
    if kind not in valid:
        raise ValueError(f"attach kind должен быть одним из {valid}, получил {kind}")
    return kind, ref


async def generate_for_contact(
    session,
    contact: Contact,
    goal: str,
    hook: str,
    attach_kind: str | None,
    attach_ref: str | None,
    attach_caption: str | None,
    attach_delay: int,
) -> None:
    style_examples = await collect_style_examples(session, contact.account_id)
    history = await collect_history(session, contact.id)

    days_since = (
        (datetime.utcnow() - contact.last_msg_at).days
        if contact.last_msg_at else 999
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
        attachment_hint=attachment_hint(attach_kind),
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
                attachment_kind=attach_kind,
                attachment_ref=attach_ref,
                attachment_caption=attach_caption,
                attachment_delay_seconds=attach_delay,
            )
        )
    logger.success(f"Drafts for {name}: {len(result.get('variants', []))}")


async def main(
    top_n: int, goal: str, hook: str, account_name: str | None,
    attach: str | None, attach_caption: str | None, attach_delay: int,
) -> None:
    attach_kind, attach_ref = _parse_attach(attach)

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

            logger.info(
                f"[{acc.name}] generating drafts for {len(contacts)} contacts"
                + (f" (+attach={attach_kind})" if attach_kind else "")
            )
            for c in contacts:
                await generate_for_contact(
                    session, c, goal, hook,
                    attach_kind, attach_ref, attach_caption, attach_delay,
                )
                await session.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument(
        "--goal",
        choices=["personal", "project", "reactivate", "offer"],
        default="personal",
    )
    parser.add_argument("--hook", type=str, default="")
    parser.add_argument("--account", type=str, default=None)
    parser.add_argument(
        "--attach", type=str, default=None,
        help="kind:ref, напр. photo:attachments/offer.png или link:https://...",
    )
    parser.add_argument("--attach-caption", type=str, default=None)
    parser.add_argument(
        "--attach-delay", type=int, default=15,
        help="секунд между текстом и вложением; 0 = вложение caption'ом",
    )
    args = parser.parse_args()

    asyncio.run(main(
        args.top, args.goal, args.hook, args.account,
        args.attach, args.attach_caption, args.attach_delay,
    ))

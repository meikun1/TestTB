"""
Генерация очереди Draft'ов для рассылки.

Для каждого enabled-аккаунта (status='active') берёт все контакты
с history и создаёт Draft с шаблоном + опциональным вложением.

Запуск:
    python -m src.generate --attach photo:attachments/offer.png --attach-caption "посмотри"
    python -m src.generate --attach link:https://example.com/offer
    python -m src.generate  # только текст без вложения
"""
import argparse
import asyncio

from loguru import logger
from sqlalchemy import select

from .db import async_session, init_db
from .models import Account, BlockedContact, Contact, Draft
from .templates import pick_local_name, pick_template, render


VALID_KINDS = {"photo", "video", "audio", "voice", "document", "link"}


def _parse_attach(s: str | None) -> tuple[str | None, str | None]:
    """photo:attachments/foo.png → ('photo', 'attachments/foo.png')"""
    if not s:
        return None, None
    if ":" not in s:
        raise ValueError(f"--attach должен быть kind:ref, получил: {s}")
    kind, ref = s.split(":", 1)
    if kind not in VALID_KINDS:
        raise ValueError(f"--attach kind должен быть из {VALID_KINDS}, получил: {kind}")
    return kind, ref


async def generate_for_account(
    account: Account,
    attach_kind: str | None,
    attach_ref: str | None,
    attach_caption: str | None,
    attach_delay: int,
) -> int:
    """Возвращает количество созданных драфтов."""
    async with async_session() as session:
        # Все контакты с историей переписки
        contacts = (await session.execute(
            select(Contact)
            .where(Contact.account_id == account.id)
            .where(Contact.total_messages > 0)
            .where(Contact.last_msg_at.is_not(None))
        )).scalars().all()

        # Глобальный blocklist
        blocked = set(r[0] for r in (await session.execute(
            select(BlockedContact.tg_user_id)
        )).all())

        # Дроп тех у кого уже есть pending Draft на этом аккаунте (не дублируем)
        existing_drafts = set(r[0] for r in (await session.execute(
            select(Draft.contact_id)
            .where(Draft.account_id == account.id)
            .where(Draft.status.in_(["pending"]))
        )).all())

        created = 0
        for contact in contacts:
            if contact.tg_user_id in blocked:
                continue
            if contact.id in existing_drafts:
                continue

            template = pick_template(contact.language_hint)
            name = contact.first_name or contact.username or pick_local_name(contact.id)
            text = render(template, name)

            session.add(Draft(
                contact_id=contact.id,
                account_id=account.id,
                text=text,
                attachment_kind=attach_kind,
                attachment_ref=attach_ref,
                attachment_caption=attach_caption,
                attachment_delay_seconds=attach_delay,
                status="pending",
            ))
            created += 1

        await session.commit()
        return created


async def run_generate(
    attach: str | None,
    attach_caption: str | None,
    attach_delay: int,
    account_name: str | None,
) -> None:
    await init_db()

    attach_kind, attach_ref = _parse_attach(attach)

    async with async_session() as session:
        q = select(Account).where(
            Account.enabled.is_(True),
            Account.status == "active",
        )
        if account_name:
            q = q.where(Account.name == account_name)
        accounts = (await session.execute(q)).scalars().all()

    if not accounts:
        logger.warning("Нет подходящих аккаунтов (нужен enabled=true + status=active)")
        return

    logger.info(
        f"Generating drafts для {len(accounts)} аккаунтов"
        + (f" с вложением {attach_kind}" if attach_kind else " (только текст)")
    )

    total = 0
    for acc in accounts:
        n = await generate_for_account(
            acc, attach_kind, attach_ref, attach_caption, attach_delay,
        )
        logger.info(f"[{acc.name}] +{n} draft(ов)")
        total += n

    logger.success(f"Всего создано {total} драфтов")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--attach",
        type=str,
        default=None,
        help="kind:ref, напр. photo:attachments/offer.png или link:https://...",
    )
    parser.add_argument(
        "--attach-caption", type=str, default=None,
        help="подпись к медиа (необязательно)",
    )
    parser.add_argument(
        "--attach-delay", type=int, default=15,
        help="секунд между текстом и вложением; 0 = вложение caption'ом",
    )
    parser.add_argument(
        "--account", type=str, default=None,
        help="генерить только для одного аккаунта (по имени)",
    )
    args = parser.parse_args()

    asyncio.run(run_generate(
        args.attach, args.attach_caption, args.attach_delay, args.account,
    ))


if __name__ == "__main__":
    main()

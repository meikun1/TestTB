"""Модели БД проекта."""
from datetime import datetime

from sqlalchemy import (
    String, Integer, Float, DateTime, Text, Boolean, ForeignKey,
    UniqueConstraint, BigInteger, Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Account(Base):
    """Telegram-аккаунт из которого ведём рассылку."""
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    phone: Mapped[str] = mapped_column(String(32))

    # Telethon StringSession (главное — она авторизует акк)
    session_string: Mapped[str] = mapped_column(Text)

    # Реальный device-fingerprint того клиента где аккаунт исходно жил
    device_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    system_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lang_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    system_lang_code: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # active | flood | banned | disabled | needs_email | needs_captcha | geo_mismatch | suspicious
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # pending_intake | processing | done | failed — этап обработки intake-воркером
    intake_status: Mapped[str] = mapped_column(
        String(32), default="done", index=True
    )

    flood_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_send_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Carrier: 'beeline' | 'ucell' | 'ums' | 'uzmobile' | 'newop' | 'other'
    carrier: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    contacts: Mapped[list["Contact"]] = relationship(back_populates="account")


class ShardHeartbeat(Base):
    """
    Каждый sender-shard раз в минуту пишет timestamp. Scheduler смотрит
    на эту таблицу и алертит если кто-то застрял (heartbeat старше 5 мин).
    """
    __tablename__ = "shard_heartbeats"

    shard_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_beat: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sent_count_total: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class GenerateRun(Base):
    """
    Журнал авто-генераций драфтов scheduler'ом. Чтобы не дублировать
    запуски и видеть когда последний раз пересчитывали очередь.
    """
    __tablename__ = "generate_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accounts_touched: Mapped[int] = mapped_column(Integer, default=0)
    drafts_created: Mapped[int] = mapped_column(Integer, default=0)
    trigger: Mapped[str] = mapped_column(String(32), default="auto")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Contact(Base):
    """Контакт аккаунта с которым уже была переписка."""
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("account_id", "tg_user_id", name="uq_account_tguser"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )

    tg_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)

    last_msg_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_messages: Mapped[int] = mapped_column(Integer, default=0)

    # 'ru' | 'uz_cyrl' | 'uz_latn' | null
    language_hint: Mapped[str | None] = mapped_column(String(16), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    account: Mapped["Account"] = relationship(back_populates="contacts")


class Draft(Base):
    """Подготовленное сообщение для отправки конкретному контакту."""
    __tablename__ = "drafts"
    __table_args__ = (
        Index("ix_drafts_status_account", "status", "account_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )

    text: Mapped[str] = mapped_column(Text)
    # pending | sent | failed | rejected
    status: Mapped[str] = mapped_column(String(32), default="pending")

    # photo | video | audio | voice | document | link | null
    attachment_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    attachment_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_delay_seconds: Mapped[int] = mapped_column(Integer, default=15)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SendLog(Base):
    """Журнал всех отправок (текст и вложение — отдельными записями)."""
    __tablename__ = "send_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    sent_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )
    success: Mapped[bool] = mapped_column(Boolean, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 'message' | 'attachment'
    kind: Mapped[str] = mapped_column(String(32), default="message")


class BlockedContact(Base):
    """Глобальный opt-out: получатели которым ВСЕ аккаунты больше не пишут."""
    __tablename__ = "blocked_contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    blocked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class UsedEmail(Base):
    """Реестр email-адресов которые мы выдавали Telegram для верификации."""
    __tablename__ = "used_emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    code_received: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CaptchaChallenge(Base):
    """Лог запросов капчи (для отладки + для manual-режима)."""
    __tablename__ = "captcha_challenges"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    challenge_type: Mapped[str] = mapped_column(String(64))
    challenge_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    solved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    solution: Mapped[str | None] = mapped_column(Text, nullable=True)
    # pending | solved | failed | timeout
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)

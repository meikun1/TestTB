"""Модели БД для мульти-аккаунтной рассылки (Postgres-ready)."""
from datetime import datetime
from sqlalchemy import (
    String, Integer, Float, DateTime, Text, Boolean, ForeignKey,
    UniqueConstraint, BigInteger,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    phone: Mapped[str] = mapped_column(String(32))

    # Источник сессии — один из двух:
    #   session_string: Telethon StringSession (приоритет, удобно для API-intake)
    #   session_path:   путь к .session-файлу (для интерактивного логина)
    session_string: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_path: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # socks5://user:pass@host:port  или  http://user:pass@host:port  (null = без прокси)
    proxy: Mapped[str | None] = mapped_column(String(255), nullable=True)

    daily_limit: Mapped[int] = mapped_column(Integer, default=50)
    hourly_limit: Mapped[int] = mapped_column(Integer, default=8)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # active | flood | banned | disabled
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # пока flood_until > now — аккаунт пропускаем
    flood_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_send_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # для шардинга sender'ов: account_id % WORKER_COUNT == worker_shard_id
    # денормализованное поле, чтобы потом можно было быстро группировать в дашборде
    shard_id: Mapped[int] = mapped_column(Integer, default=0, index=True)

    # время последней фоновой выгрузки истории
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    contacts: Mapped[list["Contact"]] = relationship(back_populates="account")


class Contact(Base):
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

    # Скоринг
    category: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    last_msg_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_msg_from_me: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    total_messages: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)

    # Контекст для генерации
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    hook: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    account: Mapped["Account"] = relationship(back_populates="contacts")
    messages: Mapped[list["Message"]] = relationship(back_populates="contact")
    drafts: Mapped[list["Draft"]] = relationship(back_populates="contact")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), index=True
    )
    tg_message_id: Mapped[int] = mapped_column(BigInteger)
    from_me: Mapped[bool] = mapped_column(Boolean)
    text: Mapped[str] = mapped_column(Text)
    date: Mapped[datetime] = mapped_column(DateTime, index=True)

    contact: Mapped["Contact"] = relationship(back_populates="messages")


class Draft(Base):
    """
    Черновик сообщения. Может содержать вложение любого типа.

    Если attachment_kind == 'link' — поле attachment_ref содержит URL, который
    либо вшит в text, либо отправится отдельным сообщением.

    Если attachment_kind in ('photo','video','audio','voice','document') —
    attachment_ref это путь к файлу на диске (относительно PROJECT_ROOT).

    attachment_delay_seconds:
        0       — вложение идёт CAPTION'ом к media (одно сообщение)
        >0      — сначала текст, пауза delay секунд, потом отдельным сообщением
                   медиа/файл (имитация «дописал, потом прикрепил»)
    """
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), index=True
    )
    # денормализовано: позволяет диспетчеру быстро фильтровать по аккаунту
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )

    text: Mapped[str] = mapped_column(Text)
    variant_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # pending | approved | rejected | sent | failed
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)

    # Вложение (опционально)
    # photo | video | audio | voice | document | link | NULL
    attachment_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    attachment_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_delay_seconds: Mapped[int] = mapped_column(Integer, default=15)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    contact: Mapped["Contact"] = relationship(back_populates="drafts")


class SendLog(Base):
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
    # 'message' | 'attachment' | 'message+attachment'
    kind: Mapped[str] = mapped_column(String(32), default="message")

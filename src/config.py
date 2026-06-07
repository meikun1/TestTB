"""Глобальная конфигурация из .env."""
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # === Telegram API ===
    tg_api_id: int = Field(..., alias="TG_API_ID")
    tg_api_hash: str = Field(..., alias="TG_API_HASH")

    # === Postgres ===
    postgres_host: str = Field("postgres", alias="POSTGRES_HOST")
    postgres_port: int = Field(5432, alias="POSTGRES_PORT")
    postgres_user: str = Field("tg", alias="POSTGRES_USER")
    postgres_password: str = Field("tg", alias="POSTGRES_PASSWORD")
    postgres_db: str = Field("tg_broadcaster", alias="POSTGRES_DB")

    # === Sender ===
    worker_count: int = Field(10, alias="WORKER_COUNT")
    min_delay_seconds: int = Field(30, alias="MIN_DELAY_SECONDS")
    max_delay_seconds: int = Field(90, alias="MAX_DELAY_SECONDS")
    attachment_delay_min: int = Field(5, alias="ATTACHMENT_DELAY_MIN")
    attachment_delay_max: int = Field(30, alias="ATTACHMENT_DELAY_MAX")

    # === Геоконсистентность UZ ===
    active_timezone: str = Field("Asia/Tashkent", alias="ACTIVE_TIMEZONE")
    active_hours_start: int = Field(9, alias="ACTIVE_HOURS_START")
    active_hours_end: int = Field(23, alias="ACTIVE_HOURS_END")

    # === Device-fingerprint strictness ===
    # ВАЖНО: вся идея проекта строится на том что каждый аккаунт ходит с
    # реального device-fingerprint того клиента, с которого он изначально
    # логинился. Дефолты Telethon ("Desktop"/"en") для +998 UZ номера выглядят
    # для антифрода как угнанная сессия. Поэтому по умолчанию strict-режим.
    #
    # STRICT_REAL_FINGERPRINT=true (default):
    #   импорт ОБЯЗАТЕЛЬНО требует все 5 device-полей в CSV.
    #   Если хоть одно поле пустое → аккаунт отвергается с
    #   status='disabled' reason='missing_real_fingerprint'.
    #
    # STRICT_REAL_FINGERPRINT=false (НЕ рекомендую):
    #   пустые поля молча заменяются на FALLBACK_* значения ниже.
    #   Использовать только если знаешь что делаешь.
    strict_real_fingerprint: bool = Field(True, alias="STRICT_REAL_FINGERPRINT")
    fallback_lang_code: str = Field("ru", alias="FALLBACK_LANG_CODE")
    fallback_system_lang_code: str = Field("ru-UZ", alias="FALLBACK_SYSTEM_LANG_CODE")

    respect_carrier_limits: bool = Field(True, alias="RESPECT_CARRIER_LIMITS")
    beeline_uz_limit_multiplier: float = Field(
        0.6, alias="BEELINE_UZ_LIMIT_MULTIPLIER"
    )
    carrier_hourly_cap: int = Field(50, alias="CARRIER_HOURLY_CAP")
    expected_country_code: str = Field("998", alias="EXPECTED_COUNTRY_CODE")
    expected_nearest_dc_country: str = Field(
        "UZ", alias="EXPECTED_NEAREST_DC_COUNTRY"
    )

    # === Email-верификация ===
    email_domain: str = Field("", alias="EMAIL_DOMAIN")
    imap_host: str = Field("", alias="IMAP_HOST")
    imap_port: int = Field(993, alias="IMAP_PORT")
    imap_use_ssl: bool = Field(True, alias="IMAP_USE_SSL")
    imap_user: str = Field("", alias="IMAP_USER")
    imap_password: str = Field("", alias="IMAP_PASSWORD")
    email_code_timeout_seconds: int = Field(180, alias="EMAIL_CODE_TIMEOUT_SECONDS")

    # === Captcha ===
    captcha_solver: str = Field("manual", alias="CAPTCHA_SOLVER")
    captcha_timeout_seconds: int = Field(300, alias="CAPTCHA_TIMEOUT_SECONDS")

    # === Алерты ===
    alert_bot_token: str = Field("", alias="ALERT_BOT_TOKEN")
    alert_chat_id: str = Field("", alias="ALERT_CHAT_ID")

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()

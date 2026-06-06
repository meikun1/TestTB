"""Конфигурация проекта. Все настройки читаются из .env"""
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

    # Telegram
    tg_api_id: int = Field(..., alias="TG_API_ID")
    tg_api_hash: str = Field(..., alias="TG_API_HASH")
    tg_phone: str = Field(..., alias="TG_PHONE")
    tg_session_name: str = Field("tg_assistant_session", alias="TG_SESSION_NAME")

    # LLM
    anthropic_api_key: str = Field("", alias="ANTHROPIC_API_KEY")

    # Антиспам
    max_messages_per_day: int = Field(20, alias="MAX_MESSAGES_PER_DAY")
    min_delay_seconds: int = Field(300, alias="MIN_DELAY_SECONDS")
    max_delay_seconds: int = Field(2400, alias="MAX_DELAY_SECONDS")
    working_hours_start: int = Field(10, alias="WORKING_HOURS_START")
    working_hours_end: int = Field(22, alias="WORKING_HOURS_END")

    # Хранилище
    db_path: str = Field("data/tg_assistant.db", alias="DB_PATH")

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{PROJECT_ROOT / self.db_path}"


settings = Settings()

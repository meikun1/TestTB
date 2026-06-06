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
    local_model_url: str = Field("http://localhost:8000/v1", alias="LOCAL_MODEL_URL")
    use_local_model: bool = Field(False, alias="USE_LOCAL_MODEL")

    # === Лимиты отправки ===
    max_messages_per_day: int = Field(20, alias="MAX_MESSAGES_PER_DAY")
    max_messages_per_hour: int = Field(4, alias="MAX_MESSAGES_PER_HOUR")
    weekend_multiplier: float = Field(0.5, alias="WEEKEND_MULTIPLIER")

    # === Паузы между отправками ===
    min_delay_seconds: int = Field(420, alias="MIN_DELAY_SECONDS")
    max_delay_seconds: int = Field(3600, alias="MAX_DELAY_SECONDS")

    # === Окно активности ===
    working_hours_start: int = Field(10, alias="WORKING_HOURS_START")
    working_hours_end: int = Field(22, alias="WORKING_HOURS_END")
    mandatory_sleep_start: int = Field(0, alias="MANDATORY_SLEEP_START")
    mandatory_sleep_end: int = Field(8, alias="MANDATORY_SLEEP_END")

    # === Имитация поведения ===
    simulate_typing: bool = Field(True, alias="SIMULATE_TYPING")
    typing_seconds_per_char: float = Field(0.08, alias="TYPING_SECONDS_PER_CHAR")
    typing_max_seconds: int = Field(45, alias="TYPING_MAX_SECONDS")
    read_incoming_before_send: bool = Field(True, alias="READ_INCOMING_BEFORE_SEND")
    skip_probability: float = Field(0.15, alias="SKIP_PROBABILITY")

    # === Защита от перегрева ===
    min_incoming_messages: int = Field(3, alias="MIN_INCOMING_MESSAGES")
    min_days_between_sends: int = Field(30, alias="MIN_DAYS_BETWEEN_SENDS")
    reply_rate_window: int = Field(15, alias="REPLY_RATE_WINDOW")
    reply_rate_min: float = Field(0.25, alias="REPLY_RATE_MIN")
    reply_rate_check_hours: int = Field(48, alias="REPLY_RATE_CHECK_HOURS")

    # === Ramp-up ===
    rampup_enabled: bool = Field(True, alias="RAMPUP_ENABLED")
    rampup_days: int = Field(14, alias="RAMPUP_DAYS")
    rampup_start_limit: int = Field(3, alias="RAMPUP_START_LIMIT")

    # Хранилище
    db_path: str = Field("data/tg_assistant.db", alias="DB_PATH")

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{PROJECT_ROOT / self.db_path}"


settings = Settings()

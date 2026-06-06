"""Конфигурация проекта. Все настройки читаются из .env

ВАЖНО: per-account параметры (phone, session_path, proxy, daily/hourly limit)
живут в таблице `accounts` в БД. Здесь — только глобальные ручки, общие
для всех аккаунтов: API-ключи, окна активности, ramp-up, typing и т.п.
"""
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

    # Telegram API — общий на все аккаунты (один app на my.telegram.org)
    tg_api_id: int = Field(..., alias="TG_API_ID")
    tg_api_hash: str = Field(..., alias="TG_API_HASH")

    # Глобальный тумблер: если false — поле Account.proxy игнорируется,
    # клиенты ходят напрямую. Удобно для локального теста; на проде включай.
    proxies_enabled: bool = Field(True, alias="PROXIES_ENABLED")

    # LLM
    anthropic_api_key: str = Field("", alias="ANTHROPIC_API_KEY")
    local_model_url: str = Field("http://localhost:8000/v1", alias="LOCAL_MODEL_URL")
    use_local_model: bool = Field(False, alias="USE_LOCAL_MODEL")

    # === Дефолтные лимиты при импорте нового аккаунта ===
    # (per-account значения хранятся в таблице accounts и могут отличаться)
    default_daily_limit: int = Field(50, alias="DEFAULT_DAILY_LIMIT")
    default_hourly_limit: int = Field(8, alias="DEFAULT_HOURLY_LIMIT")
    weekend_multiplier: float = Field(0.5, alias="WEEKEND_MULTIPLIER")

    # === Паузы между отправками (глобально) ===
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

    # === Dashboard / API ===
    dashboard_host: str = Field("0.0.0.0", alias="DASHBOARD_HOST")
    dashboard_port: int = Field(8080, alias="DASHBOARD_PORT")
    # Любой токен для basic-auth дашборда (логин: admin, пароль: токен)
    # и Bearer для API. На проде сгенерируй длинный.
    dashboard_token: str = Field("change-me", alias="DASHBOARD_TOKEN")

    # Хранилище
    db_path: str = Field("data/tg_assistant.db", alias="DB_PATH")

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{PROJECT_ROOT / self.db_path}"


settings = Settings()

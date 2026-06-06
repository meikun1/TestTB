"""Конфигурация проекта. Все настройки читаются из .env

ВАЖНО: per-account параметры (phone, session, proxy, daily/hourly limit)
живут в таблице `accounts` в БД. Здесь — только глобальные ручки,
общие для всех аккаунтов: API-ключи, окна активности, ramp-up, инфра.
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

    # === Telegram API (один app на my.telegram.org) ===
    tg_api_id: int = Field(..., alias="TG_API_ID")
    tg_api_hash: str = Field(..., alias="TG_API_HASH")

    # Глобальный тумблер прокси (см. docs/MULTI_ACCOUNT.md)
    proxies_enabled: bool = Field(True, alias="PROXIES_ENABLED")

    # === LLM ===
    anthropic_api_key: str = Field("", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field("claude-sonnet-4-6", alias="ANTHROPIC_MODEL")
    local_model_url: str = Field("http://localhost:8000/v1", alias="LOCAL_MODEL_URL")
    use_local_model: bool = Field(False, alias="USE_LOCAL_MODEL")

    # === БД (Postgres) ===
    # На SQLite в 500 акк не идёт; SQLite оставлен как fallback для локальной отладки.
    # Boolean USE_POSTGRES управляет тем, какой URL соберёт db_url.
    use_postgres: bool = Field(True, alias="USE_POSTGRES")
    postgres_host: str = Field("postgres", alias="POSTGRES_HOST")
    postgres_port: int = Field(5432, alias="POSTGRES_PORT")
    postgres_user: str = Field("tg", alias="POSTGRES_USER")
    postgres_password: str = Field("tg", alias="POSTGRES_PASSWORD")
    postgres_db: str = Field("tg_assistant", alias="POSTGRES_DB")
    # SQLite fallback
    db_path: str = Field("data/tg_assistant.db", alias="DB_PATH")

    # === Redis (live-reload пула, координация воркеров) ===
    redis_url: str = Field("redis://redis:6379/0", alias="REDIS_URL")
    redis_channel_pool: str = Field(
        "tg_assistant.pool.reload", alias="REDIS_CHANNEL_POOL"
    )

    # === Sender-воркеры (шардинг) ===
    # Сколько процессов sender'а будут крутить пул параллельно.
    # account.shard_id = account.id % worker_count → каждый воркер берёт свою долю.
    worker_count: int = Field(10, alias="WORKER_COUNT")

    # === Фоновый fetcher ===
    fetcher_interval_hours: int = Field(24, alias="FETCHER_INTERVAL_HOURS")
    fetcher_messages_per_dialog: int = Field(
        200, alias="FETCHER_MESSAGES_PER_DIALOG"
    )

    # === Дефолтные лимиты для нового аккаунта ===
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

    # === Вложения ===
    # Пауза между «текст отправлен» и «вложение отправлено», если
    # attachment_delay_seconds на драфте == 0 → используется автоопределение
    # из диапазона [min, max] секунд.
    attachment_default_delay_min: int = Field(5, alias="ATTACHMENT_DEFAULT_DELAY_MIN")
    attachment_default_delay_max: int = Field(30, alias="ATTACHMENT_DEFAULT_DELAY_MAX")

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

    # === Live reload пула (fallback-опрос если Redis недоступен) ===
    pool_reload_interval_seconds: int = Field(
        300, alias="POOL_RELOAD_INTERVAL_SECONDS"
    )

    # === Health-checker / мониторинг ===
    health_check_interval_seconds: int = Field(
        300, alias="HEALTH_CHECK_INTERVAL_SECONDS"
    )
    # Алерт если доля active-аккаунтов в пуле падает ниже X
    alert_active_ratio_threshold: float = Field(
        0.7, alias="ALERT_ACTIVE_RATIO_THRESHOLD"
    )
    # Алерт если глобальный reply rate за 48ч падает ниже X
    alert_reply_rate_threshold: float = Field(
        0.10, alias="ALERT_REPLY_RATE_THRESHOLD"
    )
    # Алерт если за час сгорело >X% аккаунтов
    alert_burn_rate_threshold: float = Field(
        0.05, alias="ALERT_BURN_RATE_THRESHOLD"
    )

    # === Telegram-бот для алертов ===
    # Создай бота через @BotFather → токен сюда.
    # ALERT_CHAT_ID — куда слать: твой chat_id или id группы (с минусом).
    alert_bot_token: str = Field("", alias="ALERT_BOT_TOKEN")
    alert_chat_id: str = Field("", alias="ALERT_CHAT_ID")

    # === Dashboard / API ===
    dashboard_host: str = Field("0.0.0.0", alias="DASHBOARD_HOST")
    dashboard_port: int = Field(8080, alias="DASHBOARD_PORT")
    dashboard_token: str = Field("change-me", alias="DASHBOARD_TOKEN")

    # === Email verification (catch-all почтовый сервер) ===
    # Telegram иногда при логине требует подтвердить email. Мы генерим
    # случайный <random>@<EMAIL_DOMAIN>, подаём его, ждём код в IMAP.
    email_domain: str = Field("", alias="EMAIL_DOMAIN")
    imap_host: str = Field("", alias="IMAP_HOST")
    imap_port: int = Field(993, alias="IMAP_PORT")
    imap_user: str = Field("", alias="IMAP_USER")
    imap_password: str = Field("", alias="IMAP_PASSWORD")
    imap_use_ssl: bool = Field(True, alias="IMAP_USE_SSL")
    # сколько секунд ждать код в почте при логине
    email_code_timeout_seconds: int = Field(180, alias="EMAIL_CODE_TIMEOUT_SECONDS")

    @property
    def db_url(self) -> str:
        if self.use_postgres:
            return (
                f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
                f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            )
        return f"sqlite+aiosqlite:///{PROJECT_ROOT / self.db_path}"


settings = Settings()

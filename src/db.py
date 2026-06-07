"""Подключение к Postgres (с поддержкой PgBouncer для масштаба)."""
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

from .config import settings, PROJECT_ROOT
from .models import Base


# Нужные папки на случай локального запуска
(PROJECT_ROOT / "logs").mkdir(parents=True, exist_ok=True)
(PROJECT_ROOT / "attachments").mkdir(parents=True, exist_ok=True)
(PROJECT_ROOT / "sessions").mkdir(parents=True, exist_ok=True)


# Параметры engine зависят от того, идём ли через PgBouncer.
# При PgBouncer transaction-mode нельзя prepared statements (отдельные
# соединения для каждой транзакции, prepared statement не переживает).
_engine_kwargs = dict(
    echo=False,
    pool_pre_ping=True,
)
if settings.use_pgbouncer:
    # asyncpg специфика — отключаем prepared statement cache
    _engine_kwargs["connect_args"] = {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
    }
    # pool со стороны приложения тоже не нужен большой —
    # PgBouncer сам пулит коннекты
    _engine_kwargs["pool_size"] = 5
    _engine_kwargs["max_overflow"] = 5
else:
    # Прямой коннект к Postgres
    _engine_kwargs["pool_size"] = 20
    _engine_kwargs["max_overflow"] = 10


engine = create_async_engine(settings.db_url, **_engine_kwargs)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Создаёт схему при первом запуске."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

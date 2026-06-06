"""Асинхронное подключение к Postgres (или SQLite в fallback)."""
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession

from config import settings, PROJECT_ROOT
from .models import Base


# Для SQLite — гарантируем папку data/
if not settings.use_postgres:
    (PROJECT_ROOT / settings.db_path).parent.mkdir(parents=True, exist_ok=True)
(PROJECT_ROOT / "logs").mkdir(parents=True, exist_ok=True)
(PROJECT_ROOT / "attachments").mkdir(parents=True, exist_ok=True)


# pool_size 20 на воркер: с 10 воркерами + dashboard + fetcher = ~200 коннектов.
# Postgres по умолчанию 100, поэтому увеличь max_connections в postgresql.conf
# (в docker-compose уже выставлено через POSTGRES_MAX_CONNECTIONS).
engine = create_async_engine(
    settings.db_url,
    echo=False,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Создаёт таблицы при первом запуске."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

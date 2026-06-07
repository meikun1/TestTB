"""Подключение к Postgres + init_db."""
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

from .config import settings, PROJECT_ROOT
from .models import Base


# Нужные папки на случай локального запуска
(PROJECT_ROOT / "logs").mkdir(parents=True, exist_ok=True)
(PROJECT_ROOT / "attachments").mkdir(parents=True, exist_ok=True)
(PROJECT_ROOT / "sessions").mkdir(parents=True, exist_ok=True)


engine = create_async_engine(
    settings.db_url,
    echo=False,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Создаёт схему при первом запуске."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

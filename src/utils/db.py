"""Асинхронное подключение к SQLite через SQLAlchemy."""
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession

from config import settings, PROJECT_ROOT
from .models import Base


# Гарантируем существование папок для БД и telethon-сессии
(PROJECT_ROOT / settings.db_path).parent.mkdir(parents=True, exist_ok=True)
(PROJECT_ROOT / "logs").mkdir(parents=True, exist_ok=True)


engine = create_async_engine(settings.db_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Создаёт таблицы при первом запуске."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

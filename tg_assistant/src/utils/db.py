"""Асинхронное подключение к SQLite через SQLAlchemy."""
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession

from config import settings
from .models import Base


engine = create_async_engine(settings.db_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Создаёт таблицы при первом запуске."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

"""
Пул аккаунтов: подгружает их из БД, держит подключённые TelegramClient'ы.

Каждый аккаунт получает свой session-файл и (опционально) свой прокси.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from loguru import logger
from sqlalchemy import select
from telethon import TelegramClient

from config import settings, PROJECT_ROOT
from src.utils import async_session, Account


def parse_proxy(url: str | None):
    """
    Конвертирует строку прокси в формат, который понимает telethon.
    Поддерживает: socks5://user:pass@host:port, socks4://..., http://...
    Возвращает кортеж или None.
    """
    if not url:
        return None
    import socks  # noqa: PLC0415  (PySocks)

    u = urlparse(url)
    scheme_map = {
        "socks5": socks.SOCKS5,
        "socks4": socks.SOCKS4,
        "http": socks.HTTP,
        "https": socks.HTTP,
    }
    proxy_type = scheme_map.get(u.scheme.lower())
    if proxy_type is None:
        raise ValueError(f"Неизвестный тип прокси: {u.scheme}")
    return (proxy_type, u.hostname, u.port, True, u.username, u.password)


def build_client(account: Account) -> TelegramClient:
    """Создаёт (но не подключает) TelegramClient для аккаунта."""
    session_path = PROJECT_ROOT / account.session_path
    session_path.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        str(session_path),
        settings.tg_api_id,
        settings.tg_api_hash,
        proxy=parse_proxy(account.proxy),
    )


class AccountPool:
    """Держит живые клиенты для активных аккаунтов."""

    def __init__(self) -> None:
        self._clients: dict[int, TelegramClient] = {}
        self._accounts: dict[int, Account] = {}

    async def load(self) -> None:
        """Подгружает enabled-аккаунты из БД и подключает клиенты."""
        async with async_session() as session:
            rows = (
                await session.execute(
                    select(Account).where(Account.enabled.is_(True))
                )
            ).scalars().all()

        for acc in rows:
            try:
                client = build_client(acc)
                await client.connect()
                if not await client.is_user_authorized():
                    logger.warning(
                        f"[{acc.name}] не авторизован, пропущен. "
                        f"Запусти: python -m src.accounts.login {acc.name}"
                    )
                    await client.disconnect()
                    continue
                self._clients[acc.id] = client
                self._accounts[acc.id] = acc
                logger.info(f"[{acc.name}] подключён")
            except Exception as e:
                logger.error(f"[{acc.name}] ошибка подключения: {e}")

        logger.success(f"Пул собран: {len(self._clients)} активных аккаунтов")

    def get_client(self, account_id: int) -> TelegramClient | None:
        return self._clients.get(account_id)

    def get_account(self, account_id: int) -> Account | None:
        return self._accounts.get(account_id)

    def active_ids(self) -> list[int]:
        return list(self._clients.keys())

    async def close(self) -> None:
        for client in self._clients.values():
            try:
                await client.disconnect()
            except Exception:
                pass
        self._clients.clear()
        self._accounts.clear()

"""
Пул аккаунтов: подгружает их из БД, держит подключённые TelegramClient'ы.

Каждый аккаунт получает свой session-файл/StringSession и (опционально) свой
прокси. Поддерживает live-reload: фоновая задача периодически перечитывает
БД, подключает новые intake-аккаунты и отключает забаненные.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from loguru import logger
from sqlalchemy import select
from telethon import TelegramClient
from telethon.sessions import StringSession

from config import settings, PROJECT_ROOT
from src.utils import async_session, Account


# Поля, изменение которых требует пересоздания клиента
_REBUILD_FIELDS = ("session_string", "session_path", "proxy")


def parse_proxy(url: str | None):
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
    if account.session_string:
        session = StringSession(account.session_string)
    else:
        path = PROJECT_ROOT / (
            account.session_path or f"sessions/{account.name}.session"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        session = str(path)

    proxy = parse_proxy(account.proxy) if settings.proxies_enabled else None
    return TelegramClient(
        session,
        settings.tg_api_id,
        settings.tg_api_hash,
        proxy=proxy,
    )


async def _connect_account(acc: Account) -> TelegramClient | None:
    """Подключает один аккаунт. Возвращает клиент или None при ошибке."""
    try:
        client = build_client(acc)
        await client.connect()
        if not await client.is_user_authorized():
            logger.warning(
                f"[{acc.name}] не авторизован, пропущен. "
                f"Запусти: python -m src.accounts.login {acc.name}"
            )
            await client.disconnect()
            return None
        return client
    except Exception as e:
        logger.error(f"[{acc.name}] ошибка подключения: {e}")
        return None


class AccountPool:
    """Держит живые клиенты для активных аккаунтов."""

    def __init__(self) -> None:
        self._clients: dict[int, TelegramClient] = {}
        self._accounts: dict[int, Account] = {}
        self._refresh_task: asyncio.Task | None = None

    async def load(self) -> None:
        """Первоначальная загрузка пула."""
        if not settings.proxies_enabled:
            logger.warning(
                "PROXIES_ENABLED=false — все аккаунты идут напрямую с IP сервера. "
                "Только для теста, не для масштаба."
            )
        await self.refresh(initial=True)
        logger.success(f"Пул собран: {len(self._clients)} активных аккаунтов")

    async def refresh(self, initial: bool = False) -> None:
        """
        Сверяет пул с БД:
          - подключает новые enabled-аккаунты
          - отключает те, что стали disabled/banned/удалены
          - пересобирает клиент, если сменились session/proxy
        """
        async with async_session() as session:
            rows = (await session.execute(
                select(Account).where(Account.enabled.is_(True))
            )).scalars().all()

        db_by_id = {a.id: a for a in rows}

        # 1) Удалённые / disabled / больше не enabled — отключаем
        gone = [aid for aid in self._clients if aid not in db_by_id]
        for aid in gone:
            name = self._accounts.get(aid).name if self._accounts.get(aid) else aid
            logger.info(f"[{name}] выведен из пула")
            await self._disconnect(aid)

        # 2) Существующие — проверяем что не изменились session/proxy
        for aid, db_acc in db_by_id.items():
            if aid not in self._clients:
                continue
            cached = self._accounts[aid]
            needs_rebuild = any(
                getattr(cached, f) != getattr(db_acc, f) for f in _REBUILD_FIELDS
            )
            if needs_rebuild:
                logger.info(f"[{db_acc.name}] session/proxy сменились, пересобираю")
                await self._disconnect(aid)
                client = await _connect_account(db_acc)
                if client is not None:
                    self._clients[aid] = client
                    self._accounts[aid] = db_acc
            else:
                # Лимиты и т.п. — просто обновляем кеш без рестарта клиента
                self._accounts[aid] = db_acc

        # 3) Новые — подключаем
        new_ids = [aid for aid in db_by_id if aid not in self._clients]
        for aid in new_ids:
            acc = db_by_id[aid]
            client = await _connect_account(acc)
            if client is not None:
                self._clients[aid] = client
                self._accounts[aid] = acc
                if not initial:
                    logger.success(f"[{acc.name}] подключён (live)")
                else:
                    logger.info(f"[{acc.name}] подключён")

    async def _disconnect(self, account_id: int) -> None:
        client = self._clients.pop(account_id, None)
        self._accounts.pop(account_id, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    def start_background_refresh(self) -> None:
        """Запускает фоновую задачу live-reload пула."""
        interval = settings.pool_reload_interval_seconds
        if interval <= 0:
            logger.info("Live-reload отключён (POOL_RELOAD_INTERVAL_SECONDS=0)")
            return
        self._refresh_task = asyncio.create_task(self._refresh_loop(interval))
        logger.info(f"Live-reload пула: каждые {interval}s")

    async def _refresh_loop(self, interval: int) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Pool refresh failed: {e}")

    def get_client(self, account_id: int) -> TelegramClient | None:
        return self._clients.get(account_id)

    def get_account(self, account_id: int) -> Account | None:
        return self._accounts.get(account_id)

    def active_ids(self) -> list[int]:
        return list(self._clients.keys())

    async def close(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except (asyncio.CancelledError, Exception):
                pass
            self._refresh_task = None

        for client in list(self._clients.values()):
            try:
                await client.disconnect()
            except Exception:
                pass
        self._clients.clear()
        self._accounts.clear()

"""
Пул аккаунтов с шардингом и live-reload через Redis.

Каждый sender-воркер запускается с параметром shard_id ∈ [0..WORKER_COUNT-1]
и держит подключённые TelegramClient'ы только для тех аккаунтов, у которых
account.id % WORKER_COUNT == shard_id.

При публикации сигнала в Redis-канал POOL_RELOAD пул мгновенно перечитывает
БД (без 5-минутной задержки опроса).
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from loguru import logger
from sqlalchemy import select
from telethon import TelegramClient
from telethon.sessions import StringSession

from config import settings, PROJECT_ROOT
from src.utils import async_session, Account, bus


_REBUILD_FIELDS = ("session_string", "session_path", "proxy")


def parse_proxy(url: str | None):
    if not url:
        return None
    import socks  # noqa: PLC0415

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
    try:
        client = build_client(acc)
        await client.connect()
        if not await client.is_user_authorized():
            logger.warning(
                f"[{acc.name}] не авторизован — пропускаю. "
                f"Запусти: python -m src.accounts.login {acc.name}"
            )
            await client.disconnect()
            return None
        return client
    except Exception as e:
        logger.error(f"[{acc.name}] ошибка подключения: {e}")
        return None


class AccountPool:
    """
    Шардированный пул. shard_id=None означает «все аккаунты» (для dashboard/fetcher).
    """

    def __init__(self, shard_id: int | None = None, shard_count: int | None = None) -> None:
        self._clients: dict[int, TelegramClient] = {}
        self._accounts: dict[int, Account] = {}
        self._refresh_task: asyncio.Task | None = None
        self._listen_task: asyncio.Task | None = None
        self.shard_id = shard_id
        self.shard_count = shard_count or settings.worker_count

    def _in_my_shard(self, account_id: int) -> bool:
        if self.shard_id is None:
            return True
        return account_id % self.shard_count == self.shard_id

    async def load(self) -> None:
        if not settings.proxies_enabled:
            logger.warning(
                "PROXIES_ENABLED=false — все аккаунты идут напрямую с IP сервера. "
                "Только для теста, не для масштаба."
            )
        if self.shard_id is not None:
            logger.info(
                f"Pool shard {self.shard_id}/{self.shard_count} initializing"
            )
        await self.refresh(initial=True)
        logger.success(f"Пул собран: {len(self._clients)} активных аккаунтов")

    async def refresh(self, initial: bool = False) -> None:
        async with async_session() as session:
            q = select(Account).where(Account.enabled.is_(True))
            rows = (await session.execute(q)).scalars().all()

        # фильтруем по шарду
        db_by_id = {a.id: a for a in rows if self._in_my_shard(a.id)}

        # удалённые/вышедшие из шарда
        gone = [aid for aid in self._clients if aid not in db_by_id]
        for aid in gone:
            name = self._accounts.get(aid).name if self._accounts.get(aid) else aid
            logger.info(f"[{name}] выведен из пула")
            await self._disconnect(aid)

        # существующие — проверка на rebuild
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
                self._accounts[aid] = db_acc

        # новые
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
        """Подписка на Redis + fallback-опрос БД."""
        self._refresh_task = asyncio.create_task(self._poll_loop())
        self._listen_task = asyncio.create_task(self._redis_loop())
        logger.info(
            f"Live-reload: Redis-подписка + fallback опрос каждые "
            f"{settings.pool_reload_interval_seconds}s"
        )

    async def _poll_loop(self) -> None:
        interval = settings.pool_reload_interval_seconds
        if interval <= 0:
            return
        while True:
            try:
                await asyncio.sleep(interval)
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Pool poll refresh failed: {e}")

    async def _redis_loop(self) -> None:
        try:
            async for msg in bus.subscribe_pool_reload():
                # Если событие про конкретный account_id — проверь шард
                aid = msg.get("account_id")
                if aid is not None and not self._in_my_shard(aid):
                    continue
                logger.info(f"Redis reload signal: {msg.get('reason')}")
                try:
                    await self.refresh()
                except Exception as e:
                    logger.error(f"Pool live-refresh failed: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Redis listen loop crashed: {e}")

    def get_client(self, account_id: int) -> TelegramClient | None:
        return self._clients.get(account_id)

    def get_account(self, account_id: int) -> Account | None:
        return self._accounts.get(account_id)

    def active_ids(self) -> list[int]:
        return list(self._clients.keys())

    async def close(self) -> None:
        for task in (self._refresh_task, self._listen_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._refresh_task = None
        self._listen_task = None

        for client in list(self._clients.values()):
            try:
                await client.disconnect()
            except Exception:
                pass
        self._clients.clear()
        self._accounts.clear()
        await bus.close_bus()

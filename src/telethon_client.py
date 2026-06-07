"""
Фабрика TelegramClient с РЕАЛЬНЫМ device-fingerprint аккаунта.

КРИТИЧНО: значения берутся СТРОГО из БД (account.*), никаких генерируемых
дефолтов на этом уровне. Если поле в БД None — Telethon использует свой
fallback ("Desktop"/"en"/"en"), что для UZ-аккаунтов = красный флаг.

Поэтому правильный путь:
  - В strict-режиме (STRICT_REAL_FINGERPRINT=true) импорт отвергает аккаунты
    с неполным fingerprint → они никогда не попадают сюда без всех полей
  - В non-strict — подставляются FALLBACK_* значения УЖЕ В МОМЕНТ ИМПОРТА
    и пишутся в БД. Здесь же — никогда дополнительных подмен.
"""
from loguru import logger
from telethon import TelegramClient
from telethon.sessions import StringSession

from .config import settings
from .models import Account


# Полный набор device-fields которые мы трекаем
DEVICE_FIELDS = (
    "device_model",
    "system_version",
    "app_version",
    "lang_code",
    "system_lang_code",
)


def has_complete_fingerprint(account: Account) -> bool:
    """True если все 5 полей заполнены."""
    return all(getattr(account, f) for f in DEVICE_FIELDS)


def missing_fingerprint_fields(account: Account) -> list[str]:
    return [f for f in DEVICE_FIELDS if not getattr(account, f)]


def build_client(account: Account) -> TelegramClient:
    """
    Создаёт TelegramClient ровно с тем device-fingerprint что лежит в БД.
    Никаких generation/подстановок на этом уровне.
    """
    kwargs = {}
    for f in DEVICE_FIELDS:
        v = getattr(account, f)
        if v:
            kwargs[f] = v

    if len(kwargs) < len(DEVICE_FIELDS):
        missing = missing_fingerprint_fields(account)
        # Если попали сюда с неполным fingerprint — это ошибка валидации
        # на стороне импорта. В strict-режиме такого быть не должно.
        logger.warning(
            f"[{account.name}] неполный device-fingerprint, отсутствуют: "
            f"{missing}. Telethon подставит свои дефолты — это палево!"
        )

    logger.debug(
        f"[{account.name}] подключаюсь с fingerprint: "
        f"device_model={kwargs.get('device_model')!r} "
        f"system_version={kwargs.get('system_version')!r} "
        f"app_version={kwargs.get('app_version')!r} "
        f"lang_code={kwargs.get('lang_code')!r} "
        f"system_lang_code={kwargs.get('system_lang_code')!r}"
    )

    return TelegramClient(
        StringSession(account.session_string),
        settings.tg_api_id,
        settings.tg_api_hash,
        **kwargs,
    )

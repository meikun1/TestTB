"""Фабрика TelegramClient с реальным device-fingerprint аккаунта."""
from telethon import TelegramClient
from telethon.sessions import StringSession

from .config import settings
from .models import Account


def build_client(account: Account) -> TelegramClient:
    """
    Создаёт TelegramClient с device-параметрами акк (sticky).
    Если поля None — Telethon использует дефолты библиотеки.
    """
    kwargs = {}
    if account.device_model:
        kwargs["device_model"] = account.device_model
    if account.system_version:
        kwargs["system_version"] = account.system_version
    if account.app_version:
        kwargs["app_version"] = account.app_version
    if account.lang_code:
        kwargs["lang_code"] = account.lang_code
    if account.system_lang_code:
        kwargs["system_lang_code"] = account.system_lang_code

    return TelegramClient(
        StringSession(account.session_string),
        settings.tg_api_id,
        settings.tg_api_hash,
        **kwargs,
    )

"""
Отправка вложений разных типов через Telethon.

Поддерживаемые kind:
  photo    — изображение, отображается inline
  video    — видео с streaming preview
  audio    — аудио-трек
  voice    — голосовое сообщение
  document — любой файл как документ
  link     — URL, отправляется текстом, Telegram сам рисует preview
"""
from pathlib import Path

from loguru import logger
from telethon import TelegramClient

from .config import PROJECT_ROOT
from .models import Contact, Draft


KIND_FILE_KWARGS = {
    "photo":    {"force_document": False},
    "video":    {"force_document": False, "supports_streaming": True},
    "audio":    {"force_document": False},
    "voice":    {"voice_note": True},
    "document": {"force_document": True},
}


def _resolve_path(ref: str) -> Path:
    p = Path(ref)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


async def send_attachment(
    client: TelegramClient,
    contact: Contact,
    draft: Draft,
    caption: str | None = None,
) -> None:
    """Шлёт вложение. Исключения пробрасываются наружу — там общий handler."""
    kind = draft.attachment_kind
    ref = draft.attachment_ref
    if not kind or not ref:
        return

    if kind == "link":
        await client.send_message(contact.tg_user_id, ref, link_preview=True)
        return

    kwargs = KIND_FILE_KWARGS.get(kind)
    if kwargs is None:
        raise ValueError(f"Неизвестный attachment_kind: {kind}")

    path = _resolve_path(ref)
    if not path.exists():
        raise FileNotFoundError(f"Файл вложения не найден: {path}")

    await client.send_file(
        contact.tg_user_id,
        str(path),
        caption=caption,
        **kwargs,
    )

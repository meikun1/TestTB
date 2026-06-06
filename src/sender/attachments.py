"""
Отправка вложений разных типов через Telethon.

Поддерживаемые kind:
  photo      — изображение, отображается как картинка
  video      — видео, проигрывается inline
  audio      — аудио-трек (mp3)
  voice      — голосовое сообщение
  document   — любой файл, отображается как документ-вложение
  link       — URL, отправляется как текст (Telegram сам рендерит preview)

Файлы лежат в attachments/<acc_or_global>/<file>.
В Draft.attachment_ref хранится путь относительно PROJECT_ROOT (или URL для link).
"""
from __future__ import annotations

from pathlib import Path

from loguru import logger
from telethon import TelegramClient

from config import PROJECT_ROOT
from src.utils import Contact, Draft


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
    """
    Отправляет вложение драфта. Не ловит исключения — пусть пробрасываются
    выше в send_message_with_attachment, чтобы попасть в общий error-handler
    (FloodWait/PeerFlood/прочее).
    """
    kind = draft.attachment_kind
    ref = draft.attachment_ref
    if not kind or not ref:
        return

    if kind == "link":
        # Telegram сам рисует превью; добавлять «https://...» в виде отдельного
        # текста часто естественнее, чем как caption к ничему.
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

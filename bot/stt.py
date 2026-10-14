"""Расшифровка голосовых сообщений (speech-to-text).

OpenRouter текстовые модели не принимают аудио, поэтому распознавание речи
вынесено в отдельный клиент со своими настройками (STT_*):

  * Groq  — бесплатный тариф, модель `whisper-large-v3-turbo`, очень быстро;
  * OpenAI — `whisper-1` / `gpt-4o-mini-transcribe`;
  * любой другой OpenAI-совместимый endpoint с /audio/transcriptions.

Если STT_* не заданы, используются те же ключ и URL, что и для чата.
"""

from __future__ import annotations

import logging

from openai import AsyncOpenAI

from .config import Settings

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None
_client_signature: tuple[str, str | None] | None = None


def is_configured(settings: Settings) -> bool:
    """Настроена ли расшифровка речи.

    `stt_enabled` вычисляется в config: отдельный STT_API_KEY либо официальный
    OpenAI. Провайдер чата (OpenRouter и подобные) аудио не расшифровывает.
    """
    return settings.stt_enabled and bool(settings.stt_model)


def get_client(settings: Settings) -> AsyncOpenAI:
    """Клиент для STT. Пересоздаётся, если настройки изменились (важно для тестов)."""
    global _client, _client_signature

    signature = (settings.stt_api_key, settings.stt_base_url)
    if _client is None or _client_signature != signature:
        _client = AsyncOpenAI(api_key=settings.stt_api_key, base_url=settings.stt_base_url)
        _client_signature = signature

    return _client


async def transcribe(audio: bytes, filename: str, settings: Settings) -> str:
    """Расшифровать аудио в текст. Бросает исключение, если сервис недоступен."""
    kwargs: dict[str, object] = {
        "model": settings.stt_model,
        # SDK принимает пару (имя файла, байты) — .ogg из Telegram подходит как есть.
        "file": (filename, audio),
    }
    if settings.stt_language:
        kwargs["language"] = settings.stt_language  # "ru", "kk", "en"; пусто = автоопределение

    response = await get_client(settings).audio.transcriptions.create(**kwargs)  # type: ignore[arg-type]

    text = getattr(response, "text", None) or ""
    logger.info("STT: расшифровано %d символов (%s)", len(text), settings.stt_model)
    return text.strip()

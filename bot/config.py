"""Конфигурация приложения.

Все настройки читаются из переменных окружения (файл .env).
Модуль намеренно не зависит от других модулей проекта — его можно
импортировать откуда угодно без риска циклических импортов.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Читаем .env в os.environ до первого обращения к переменным.
load_dotenv()


class ConfigError(RuntimeError):
    """Не задана или задана некорректно обязательная настройка."""


def _required(name: str) -> str:
    """Вернуть обязательную переменную окружения или упасть с понятной ошибкой."""
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"Не задана переменная окружения {name}. "
            "Скопируй .env.example в .env и заполни значения."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    """Вернуть необязательную переменную окружения."""
    return os.getenv(name, "").strip() or default


def _parse_hhmm(raw: str, name: str) -> tuple[int, int]:
    """Разобрать строку вида '07:30' в пару (час, минута)."""
    try:
        hours, minutes = raw.split(":", maxsplit=1)
        hour, minute = int(hours), int(minutes)
    except (ValueError, AttributeError) as exc:
        raise ConfigError(f"{name} должен быть в формате HH:MM, получено: {raw!r}") from exc

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"{name}: время вне диапазона — {raw!r}")
    return hour, minute


def _optional_hhmm(name: str, default: str) -> tuple[int, int] | None:
    """Время из необязательной переменной, где «не задана» и «пустая» — разное.

    `_optional` схлопывает оба случая в одно значение, а здесь нужна разница:
    отсутствие `EVENING_CHECKIN_TIME` означает «работай как обычно» (21:30),
    а явно пустое значение — «выключи вечерний чек-ин». Поэтому читаем
    окружение напрямую.
    """
    raw = os.getenv(name)
    if raw is None:
        return _parse_hhmm(default, name)
    raw = raw.strip()
    if not raw:
        return None
    return _parse_hhmm(raw, name)


def _parse_offsets(raw: str) -> tuple[int, ...]:
    """Разобрать список смещений напоминаний: '10080,1440,60' -> (10080, 1440, 60)."""
    offsets: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            offsets.append(int(chunk))
        except ValueError as exc:
            raise ConfigError(
                f"DEFAULT_REMIND_OFFSETS: {chunk!r} не является числом (минуты до срока)"
            ) from exc

    # Сортируем по убыванию: сначала самые ранние напоминания (они дальше всего от срока).
    return tuple(sorted(set(offsets), reverse=True)) or (1440, 60)


@dataclass(frozen=True, slots=True)
class Settings:
    """Все настройки приложения в одном неизменяемом объекте."""

    bot_token: str
    owner_id: int
    openai_api_key: str
    openai_base_url: str | None
    openai_model: str
    stt_api_key: str
    stt_base_url: str | None
    stt_model: str
    stt_language: str | None
    stt_enabled: bool
    tz: ZoneInfo
    tz_name: str
    db_path: str
    default_offsets: tuple[int, ...]
    digest: tuple[int, int] | None
    evening: tuple[int, int] | None
    weekly_day: str | None
    weekly_time: tuple[int, int] | None
    health_port: int | None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Собрать настройки из окружения (кэшируется — окружение читается один раз)."""
    tz_name = _optional("TZ", "Asia/Almaty")
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:  # ZoneInfoNotFoundError и подобные
        raise ConfigError(
            f"Неизвестный часовой пояс TZ={tz_name!r}. Примеры: Asia/Almaty, Asia/Aqtobe, UTC."
        ) from exc

    owner_raw = _required("OWNER_TELEGRAM_ID")
    try:
        owner_id = int(owner_raw)
    except ValueError as exc:
        raise ConfigError(
            "OWNER_TELEGRAM_ID должен быть числом (например 123456789). "
            "Свой ID можно узнать у бота @userinfobot."
        ) from exc

    digest_raw = _optional("DAILY_DIGEST_TIME")
    digest = _parse_hhmm(digest_raw, "DAILY_DIGEST_TIME") if digest_raw else None

    # Вечерний чек-ин: итог дня и приглашение записать часы подготовки.
    # По умолчанию включён, пустое значение — выключен.
    evening = _optional_hhmm("EVENING_CHECKIN_TIME", "21:30")

    weekly_day = _optional("WEEKLY_REVIEW_DAY").lower() or None
    if weekly_day and weekly_day not in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}:
        raise ConfigError(
            f"WEEKLY_REVIEW_DAY должен быть mon..sun, получено: {weekly_day!r}"
        )
    weekly_raw = _optional("WEEKLY_REVIEW_TIME", "19:00")
    weekly_time = _parse_hhmm(weekly_raw, "WEEKLY_REVIEW_TIME") if weekly_day else None

    health_raw = _optional("HEALTH_PORT")
    try:
        health_port = int(health_raw) if health_raw else None
    except ValueError as exc:
        raise ConfigError(f"HEALTH_PORT должен быть числом, получено: {health_raw!r}") from exc

    # --- STT (расшифровка голосовых) ---------------------------------
    # OpenRouter и большинство агрегаторов НЕ умеют /audio/transcriptions,
    # поэтому распознавание речи настраивается отдельно.
    openai_api_key = _required("OPENAI_API_KEY")
    openai_base_url = _optional("OPENAI_BASE_URL") or None

    stt_key = _optional("STT_API_KEY")
    if stt_key:
        # Ключ для STT задан отдельно. Если URL не указан — это Groq
        # (самый популярный бесплатный Whisper с OpenAI-совместимым API).
        stt_base_url = _optional("STT_BASE_URL") or "https://api.groq.com/openai/v1"
        stt_enabled = True
    else:
        stt_base_url = openai_base_url
        # Без отдельного ключа распознавание доступно только на официальном OpenAI.
        stt_enabled = openai_base_url is None

    return Settings(
        bot_token=_required("BOT_TOKEN"),
        owner_id=owner_id,
        openai_api_key=openai_api_key,
        openai_base_url=openai_base_url,
        openai_model=_optional("OPENAI_MODEL", "openai/gpt-4o-mini"),
        stt_api_key=stt_key or openai_api_key,
        stt_base_url=stt_base_url,
        # Groq отдаёт whisper-large-v3-turbo, официальный OpenAI — whisper-1.
        stt_model=_optional("STT_MODEL") or ("whisper-large-v3-turbo" if stt_key else "whisper-1"),
        stt_language=_optional("STT_LANGUAGE") or None,
        stt_enabled=stt_enabled,
        tz=tz,
        tz_name=tz_name,
        db_path=_optional("DB_PATH", "./data/assistant.db"),
        default_offsets=_parse_offsets(_optional("DEFAULT_REMIND_OFFSETS", "10080,1440,60")),
        digest=digest,
        evening=evening,
        weekly_day=weekly_day,
        weekly_time=weekly_time,
        health_port=health_port,
    )

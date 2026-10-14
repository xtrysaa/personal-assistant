"""Работа со временем и русскими формулировками дат.

Правило проекта: **в базе данных всё хранится в UTC**, а вводится и
показывается пользователю — в его локальной зоне (settings.tz).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

MONTHS_SHORT = (
    "янв", "фев", "мар", "апр", "май", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
)

WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


# --------------------------------------------------------------------------- #
#  Парсинг и конвертация
# --------------------------------------------------------------------------- #

def now_utc() -> datetime:
    """Текущий момент в UTC (timezone-aware)."""
    return datetime.now(timezone.utc)


def parse_iso(value: str) -> datetime:
    """Разобрать ISO-строку из БД. Наивные значения считаем UTC."""
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def parse_local(raw: str, tz: ZoneInfo, default_time: time = time(23, 59)) -> datetime:
    """Разобрать дату, введённую пользователем, и вернуть момент в UTC.

    Поддерживаются форматы:
        "2026-11-15"              -> дата, время по умолчанию
        "2026-11-15T15:00"        -> дата и время
        "2026-11-15T15:00:00"     -> дата и время с секундами
        "2026-11-15T15:00:00+05:00" -> явное смещение (используется как есть)

    Наивные значения трактуются как локальное время пользователя.
    """
    text = raw.strip().replace(" ", "T")
    if not text:
        raise ValueError("Пустая дата")

    if len(text) == 10:  # только дата
        moment = datetime.combine(date.fromisoformat(text), default_time)
    else:
        moment = datetime.fromisoformat(text)

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)

    return moment.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
#  Границы периодов (нужны для /today, /week, дайджестов)
# --------------------------------------------------------------------------- #

def day_bounds(tz: ZoneInfo, offset_days: int = 0) -> tuple[datetime, datetime]:
    """Границы суток [start, end) в UTC для локального дня, сдвинутого на offset_days."""
    local_date = datetime.now(tz).date() + timedelta(days=offset_days)
    start = datetime.combine(local_date, time.min, tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def in_days(tz: ZoneInfo, days: int) -> datetime:
    """Момент UTC, соответствующий концу локального дня через `days` суток."""
    return day_bounds(tz, offset_days=days)[1]


# --------------------------------------------------------------------------- #
#  Человекочитаемый вывод
# --------------------------------------------------------------------------- #

def fmt_due(due_iso: str | None, tz: ZoneInfo) -> str:
    """'2026-11-15T18:59:00+00:00' -> '15 ноя, 23:59'. None -> 'без срока'."""
    if not due_iso:
        return "без срока"
    local = parse_iso(due_iso).astimezone(tz)
    return f"{local.day} {MONTHS_SHORT[local.month - 1]}, {local:%H:%M}"


def fmt_day(due_iso: str | None, tz: ZoneInfo) -> str:
    """То же, но с днём недели: 'сб, 15 ноя, 23:59'."""
    if not due_iso:
        return "без срока"
    local = parse_iso(due_iso).astimezone(tz)
    return f"{WEEKDAYS_SHORT[local.weekday()]}, {local.day} {MONTHS_SHORT[local.month - 1]}, {local:%H:%M}"


def plural(number: int, one: str, few: str, many: str) -> str:
    """Русская форма множественного числа: plural(2, 'день', 'дня', 'дней') -> 'дня'."""
    number = abs(number) % 100
    if 11 <= number <= 19:
        return many
    last = number % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _fmt_delta(delta: timedelta) -> str:
    """'3 дня', '5 часов', '12 минут'.

    Округляем вниз осознанно: для дедлайна лучше показать меньше оставшегося
    времени, чем больше — так задача выглядит насущнее, а не «ещё успею».
    """
    total = int(delta.total_seconds())
    if total >= 86400:
        days = total // 86400
        return f"{days} {plural(days, 'день', 'дня', 'дней')}"
    if total >= 3600:
        hours = total // 3600
        return f"{hours} {plural(hours, 'час', 'часа', 'часов')}"
    minutes = max(total // 60, 1)
    return f"{minutes} {plural(minutes, 'минуту', 'минуты', 'минут')}"


def human_left(due_iso: str | None, tz: ZoneInfo, now: datetime | None = None) -> str:
    """'через 3 дня' / 'просрочено на 5 часов' / '' если срока нет."""
    if not due_iso:
        return ""
    moment = now or now_utc()
    delta = parse_iso(due_iso) - moment
    if delta.total_seconds() < 0:
        return f"просрочено на {_fmt_delta(-delta)}"
    return f"через {_fmt_delta(delta)}"

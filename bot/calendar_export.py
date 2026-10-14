"""Экспорт задач в iCalendar (.ics) — импорт в Google Calendar, Apple, Outlook.

Файл .ics — это обычный текст по RFC 5545, поэтому генерируем его сами:
внешняя библиотека здесь не нужна, а на бесплатном хостинге 500 МБ RAM
и лишняя зависимость дороже, чем сорок строк форматирования.

Что важно из стандарта и почему:
* строки разделяются CRLF, файл заканчивается CRLF — иначе часть
  календарей молча считает файл битым;
* `,` `;` `\\` и переводы строк в значениях экранируются;
* строки длиннее 75 октетов сворачиваются, продолжение начинается с пробела
  (режем по символам, а не по байтам: разрезанная пополам кириллица — это
  невалидный UTF-8);
* `UID` стабилен и привязан к id задачи: повторный импорт обновляет событие,
  а не создаёт второй экземпляр;
* `DTSTART`/`DTEND` — в UTC с суффиксом Z. Так не нужен блок VTIMEZONE,
  а календарь сам покажет время в зоне пользователя.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from . import timeutil

# RFC 5545: PRIORITY — число от 1 (наивысший) до 9 (наинизший).
# Наши уровни ложатся так: critical=1, high=2, normal=5, low=9.
PRIORITY_MAP: dict[str, int] = {"critical": 1, "high": 2, "normal": 5, "low": 9}
DEFAULT_PRIORITY = 5

# Событие без явного конца календарь отображает как «весь день» или не
# показывает вовсе, а RFC требует DTEND строго позже DTSTART. Полчаса —
# разумная длительность для дедлайна.
EVENT_MINUTES = 30

# За сколько минут предупредить внутри самого календаря.
ALARM_MINUTES = 60

UID_DOMAIN = "personalassistant"
LINE_LIMIT = 75  # октетов, включая CRLF


def build_ics(
    tasks: Iterable[dict[str, Any]],
    tz: ZoneInfo,
    *,
    now: datetime | None = None,
) -> str:
    """Собрать календарь из задач. Задачи без срока пропускаются.

    Без срока событие невозможно: у VEVENT обязателен DTSTART, и «когда-нибудь»
    в календарь не положить.
    """
    stamp = (now or timeutil.now_utc()).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Personal Assistant//Alnur//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]

    for task in tasks:
        if not task.get("due_at"):
            continue
        lines.extend(_event(task, tz, stamp=stamp))

    lines.append("END:VCALENDAR")
    return "".join(f"{_fold(line)}\r\n" for line in lines)


def _event(task: dict[str, Any], tz: ZoneInfo, *, stamp: str) -> list[str]:
    """Один VEVENT с напоминанием внутри календаря."""
    start = timeutil.parse_iso(task["due_at"]).astimezone(timezone.utc)
    end = start + timedelta(minutes=EVENT_MINUTES)
    title = _escape(str(task["title"]))

    lines = [
        "BEGIN:VEVENT",
        # UID не должен меняться между экспортами: по нему календарь понимает,
        # что это то же событие, и обновляет его вместо создания дубликата.
        f"UID:task-{task['id']}@{UID_DOMAIN}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{start:%Y%m%dT%H%M%SZ}",
        f"DTEND:{end:%Y%m%dT%H%M%SZ}",
        f"SUMMARY:{title}",
    ]

    description = _description(task, tz)
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")

    lines.append(f"PRIORITY:{PRIORITY_MAP.get(task.get('priority'), DEFAULT_PRIORITY)}")
    lines.extend([
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{title}",
        f"TRIGGER:-PT{ALARM_MINUTES}M",
        "END:VALARM",
        "END:VEVENT",
    ])
    return lines


def _description(task: dict[str, Any], tz: ZoneInfo) -> str:
    """Детали + служебная строка: по ней легко проверить, что время не поехало."""
    parts: list[str] = []
    if task.get("details"):
        parts.append(str(task["details"]))

    parts.append(f"Категория: {task.get('category', 'other')}")
    parts.append(f"Срок у ассистента: {timeutil.fmt_day(task.get('due_at'), tz)}")
    parts.append(f"Задача #{task['id']} · закрыть: /done {task['id']}")
    return "\n".join(parts)


def _escape(value: str) -> str:
    """Экранировать значение по RFC 5545.

    Порядок важен: сначала обратный слэш, иначе экранированные им символы
    будут экранированы повторно.
    """
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str, limit: int = LINE_LIMIT) -> str:
    """Свернуть длинную строку по октетам (RFC 5545 §3.1).

    Идём по символам и считаем их длину в UTF-8: резать готовую строку по
    байтам нельзя — «Категория» распалась бы на невалидную последовательность.
    Строка продолжения начинается с пробела, и он входит в лимит, поэтому
    у продолжений бюджет на один октет меньше.
    """
    if len(line.encode("utf-8")) <= limit:
        return line

    chunks: list[str] = []
    current = ""
    used = 0

    for char in line:
        size = len(char.encode("utf-8"))
        budget = limit if not chunks else limit - 1
        if used + size > budget:
            chunks.append(current)
            current = ""
            used = 0
        current += char
        used += size

    chunks.append(current)
    return "\r\n ".join(chunks)

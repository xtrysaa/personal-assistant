"""Единое форматирование задач для сообщений в Telegram (HTML).

Все функции экранируют пользовательский текст: parse_mode=HTML, поэтому
символы <, >, & из названий задач нужно превращать в сущности.
"""

from __future__ import annotations

import re
from html import escape
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from . import timeutil

# Теги, которые Telegram умеет рендерить и которые нам разрешено пропускать.
_ALLOWED_TAG = re.compile(r"</?(?:b|i|u|s|code|pre|a)(?:\s+[^<>]*)?>", re.IGNORECASE)

CATEGORY_EMOJI: dict[str, str] = {
    "study": "📚",          # школа, домашка, проекты
    "exams": "📝",          # контрольные, SAT, IELTS, ЕНТ
    "university": "🎓",     # поступление, заявки, документы
    "flight": "✈️",         # Air Astana Ab Initio, лётная подготовка
    "sport": "🚴",          # тренировки, велосипед, хайкинг
    "health": "🩺",         # сон, врачи, восстановление
    "personal": "🏡",       # личное, семья, быт
    "relationships": "💛",  # время с девушкой, друзья
    "other": "🗂",
}

PRIORITY_EMOJI: dict[str, str] = {
    "low": "▫️",
    "normal": "▪️",
    "high": "❗",
    "critical": "🔥",
}

CATEGORIES = tuple(CATEGORY_EMOJI)
PRIORITIES = tuple(PRIORITY_EMOJI)

_OFFSET_LABELS: dict[int, str] = {
    0: "в момент срока",
    10: "за 10 минут",
    30: "за 30 минут",
    60: "за час",
    120: "за 2 часа",
    180: "за 3 часа",
    360: "за 6 часов",
    720: "за 12 часов",
    1440: "за сутки",
    2880: "за 2 дня",
    10080: "за неделю",
    20160: "за 2 недели",
    43200: "за месяц",
}


def category_emoji(category: str | None) -> str:
    return CATEGORY_EMOJI.get(category or "other", CATEGORY_EMOJI["other"])


def sanitize_html(text: str) -> str:
    """Подготовить текст от модели к отправке с parse_mode=HTML.

    Модель может написать «x < y» или «<важно>» — Telegram на таком падает с
    ошибкой парсинга. Поэтому разрешаем только безопасные теги (b, i, u, s,
    code, pre, a), а все остальные <, >, & экранируем.
    """
    parts: list[str] = []
    position = 0
    for match in _ALLOWED_TAG.finditer(text):
        parts.append(escape(text[position : match.start()]))
        parts.append(match.group(0))
        position = match.end()
    parts.append(escape(text[position:]))
    return "".join(parts)


def reminder_label(offset_minutes: int) -> str:
    """10080 -> 'за неделю', 1440 -> 'за сутки'."""
    if offset_minutes in _OFFSET_LABELS:
        return _OFFSET_LABELS[offset_minutes]
    if offset_minutes % 1440 == 0:
        days = offset_minutes // 1440
        return f"за {days} {timeutil.plural(days, 'день', 'дня', 'дней')}"
    if offset_minutes % 60 == 0:
        hours = offset_minutes // 60
        return f"за {hours} {timeutil.plural(hours, 'час', 'часа', 'часов')}"
    return f"за {offset_minutes} минут"


def task_line(task: dict[str, Any], tz: ZoneInfo) -> str:
    """Одна строка для списков: '❗ 📝 #7 Контрольная по физике — сегодня, 15:00 (через 3 часа)'."""
    parts = [
        PRIORITY_EMOJI.get(task.get("priority", "normal"), PRIORITY_EMOJI["normal"]),
        category_emoji(task.get("category")),
        f"<b>#{task['id']}</b>",
        escape(str(task["title"])),
    ]
    line = " ".join(parts)

    if task.get("due_at"):
        left = timeutil.human_left(task["due_at"], tz)
        line += f"\n     🕒 {timeutil.fmt_day(task['due_at'], tz)}"
        if left:
            line += f" · <i>{left}</i>"
    else:
        line += "\n     🕒 без срока"

    return line


def task_card(task: dict[str, Any], tz: ZoneInfo) -> str:
    """Подробная карточка задачи (с деталями и расписанием напоминаний)."""
    lines = [
        f"{category_emoji(task.get('category'))} <b>#{task['id']} {escape(str(task['title']))}</b>",
        f"Срок: <b>{timeutil.fmt_day(task.get('due_at'), tz)}</b>"
        + (f" ({timeutil.human_left(task.get('due_at'), tz)})" if task.get("due_at") else ""),
        f"Тип: {task.get('category')} · важность: {task.get('priority')}",
    ]
    if task.get("details"):
        lines.append(f"Детали: {escape(str(task['details']))}")

    reminders = task.get("reminders") or []
    pending = [r for r in reminders if not r.get("sent_at")]
    if pending:
        labels = ", ".join(reminder_label(int(r["offset_minutes"])) for r in pending)
        lines.append(f"🔔 Напомню: {labels}")

    return "\n".join(lines)


def task_list(tasks: Iterable[dict[str, Any]], tz: ZoneInfo) -> str:
    """Нумерованный (по id) список задач одной строкой каждая."""
    return "\n\n".join(task_line(task, tz) for task in tasks)


def render_reminder(item: dict[str, Any], tz: ZoneInfo) -> str:
    """Текст проактивного напоминания, который бот присылает сам."""
    label = reminder_label(int(item["offset_minutes"]))
    lines = [
        f"⏰ <b>Напоминание {label}</b>",
        "",
        f"{category_emoji(item.get('category'))} <b>{escape(str(item['title']))}</b>",
    ]
    if item.get("due_at"):
        lines.append(
            f"🕒 Срок: {timeutil.fmt_day(item['due_at'], tz)} "
            f"({timeutil.human_left(item['due_at'], tz)})"
        )
    if item.get("details"):
        lines.append(f"ℹ️ {escape(str(item['details']))}")
    lines.append("")
    lines.append(f"<i>Задача #{item['task_id']} · закрыть: /done {item['task_id']}</i>")
    return "\n".join(lines)

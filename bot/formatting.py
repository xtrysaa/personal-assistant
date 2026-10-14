"""Единое форматирование задач для сообщений в Telegram (HTML).

Все функции экранируют пользовательский текст: parse_mode=HTML, поэтому
символы <, >, & из названий задач нужно превращать в сущности.

Здесь же живёт единый визуальный язык ассистента: маркер ``▸`` для ключевых
строк и линия ``───────────────`` как разделитель смысловых блоков. Один набор
обозначений на все сообщения — иначе ответы модели, напоминания и дайджест
звучат как три разных человека.
"""

from __future__ import annotations

import re
from datetime import datetime
from html import escape
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from . import recurrence, timeutil

# Теги, которые Telegram умеет рендерить и которые нам разрешено пропускать.
# ▸ и ─ — обычные символы, этого ограничения они не касаются.
_ALLOWED_TAG = re.compile(r"</?(?:b|i|u|s|code|pre|a)(?:\s+[^<>]*)?>", re.IGNORECASE)

# Маркер ключевой строки (статус, срок, деталь).
BULLET = "▸ "

# Разделитель смысловых блоков внутри одного сообщения.
DIVIDER = "───────────────"

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


def recurrence_label(rule: str | None) -> str:
    """Подпись повторения: 'каждый вторник и четверг'.

    Битое правило отдаём как есть: показать пользователю сырую строку лучше,
    чем уронить рендер списка из-за одной испорченной записи.
    """
    if not rule:
        return ""
    try:
        return recurrence.label(rule)
    except ValueError:
        return str(rule)


def task_line(task: dict[str, Any], tz: ZoneInfo) -> str:
    """Одна строка для списков: '❗ 📝 #7 Контрольная по физике' + срок ниже."""
    parts = [
        PRIORITY_EMOJI.get(task.get("priority", "normal"), PRIORITY_EMOJI["normal"]),
        category_emoji(task.get("category")),
        f"<b>#{task['id']}</b>",
        escape(str(task["title"])),
    ]
    line = " ".join(parts)

    if task.get("recurrence"):
        # Повторяющееся дело важно отличать от разового прямо в списке:
        # иначе непонятно, появится ли оно снова после отметки «выполнено».
        line += " 🔁"

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
        DIVIDER,
    ]

    due = f"{BULLET}Срок: <b>{timeutil.fmt_day(task.get('due_at'), tz)}</b>"
    if task.get("due_at"):
        due += f" · <i>{timeutil.human_left(task['due_at'], tz)}</i>"
    lines.append(due)

    if task.get("recurrence"):
        lines.append(f"{BULLET}Повтор: <b>{escape(recurrence_label(task['recurrence']))}</b>")

    lines.append(f"{BULLET}Тип: {task.get('category')} · важность: {task.get('priority')}")

    if task.get("details"):
        lines.append(f"{BULLET}Детали: {escape(str(task['details']))}")

    reminders = task.get("reminders") or []
    pending = [r for r in reminders if not r.get("sent_at")]
    if pending:
        labels = ", ".join(reminder_label(int(r["offset_minutes"])) for r in pending)
        lines.append(f"{BULLET}Напомню: {labels}")

    return "\n".join(lines)


def task_list(tasks: Iterable[dict[str, Any]], tz: ZoneInfo) -> str:
    """Нумерованный (по id) список задач одной строкой каждая."""
    return "\n\n".join(task_line(task, tz) for task in tasks)


def render_reminder(item: dict[str, Any], tz: ZoneInfo) -> str:
    """Текст проактивного напоминания, который бот присылает сам."""
    label = reminder_label(int(item["offset_minutes"]))
    lines = [
        f"⏰ <b>Напоминание {label}</b>",
        DIVIDER,
        f"{BULLET}{category_emoji(item.get('category'))} "
        f"<b>{escape(str(item['title']))}</b>",
    ]

    if item.get("due_at"):
        line = f"{BULLET}Срок: {timeutil.fmt_day(item['due_at'], tz)}"
        left = timeutil.human_left(item["due_at"], tz)
        if left:
            line += f" · <i>{left}</i>"
        lines.append(line)

    if item.get("details"):
        lines.append(f"{BULLET}{escape(str(item['details']))}")

    lines.append(f"{BULLET}Закрыть: <code>/done {item['task_id']}</code>")
    return "\n".join(lines)


def evening_checkin(
    done: Iterable[dict[str, Any]],
    left: Iterable[dict[str, Any]],
    tz: ZoneInfo,
    *,
    plain: bool = False,
) -> str:
    """Вечерний вопрос: итог дня и приглашение записать часы подготовки.

    `plain=True` возвращает ту же мысль без HTML — в таком виде вопрос кладётся
    в историю диалога. Разметку в истории модель начинает копировать в ответы,
    а пользователь видит теги; дешевле не показывать их ей вообще.
    """
    done, left = list(done), list(left)

    def bold(text: str) -> str:
        return text if plain else f"<b>{text}</b>"

    def titles(tasks: list[dict[str, Any]]) -> str:
        names = [str(task["title"]) for task in tasks[:3]]
        if not plain:
            names = [escape(name) for name in names]
        tail = f" и ещё {len(tasks) - 3}" if len(tasks) > 3 else ""
        return f" — {', '.join(names)}{tail}"

    local_now = datetime.now(tz)
    label = (
        f"{local_now.day} {timeutil.MONTHS_SHORT[local_now.month - 1]}, "
        f"{timeutil.WEEKDAYS_SHORT[local_now.weekday()]}"
    )

    lines = [f"🌙 {bold('Итог дня')} · {label}", DIVIDER]
    lines.append(f"{BULLET}Закрыто: {bold(str(len(done)))}" + (titles(done) if done else ""))
    lines.append(f"{BULLET}Осталось: {bold(str(len(left)))}" + (titles(left) if left else ""))

    lines.append("")
    # Ответ на этот вопрос модель разберёт как запись в прогресс (log_hours):
    # машина состояний для этого не нужна, вопрос уже лежит в истории диалога.
    lines.append(
        "Что успели сегодня? Напишите одной строкой — например "
        "«2 часа математики, пробник SAT на 680» — и я запишу это в подготовку."
    )
    return "\n".join(lines)

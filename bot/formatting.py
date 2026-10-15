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
from datetime import date, datetime, timedelta
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

# Кружки срочности в списках: один взгляд — и видно, за что браться.
URGENCY_HOT = "🔴"      # просрочено или сгорит в ближайшие часы
URGENCY_SOON = "🟠"     # сегодня или завтра
URGENCY_CALM = "⚪"     # дальше или без срока

URGENCY_LEGEND = "🔴 горит · 🟠 сегодня-завтра · ⚪ спокойно"

# Порог «горит» — меньше трёх часов до срока (и всё просроченное: его остаток
# отрицателен). У «сегодня-завтра» порог не в часах, а в календарных днях —
# см. urgency_emoji.
_HOT_WINDOW = timedelta(hours=3)
_DAY = timedelta(days=1)

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

# Важность дела — словами, без значка. Раньше у каждой важности был свой эмодзи
# в строке списка, но там теперь стоит кружок срочности (см. urgency_emoji),
# и второй значок рядом только путал. В карточке важность идёт текстом.
CATEGORIES = tuple(CATEGORY_EMOJI)
PRIORITIES = ("low", "normal", "high", "critical")

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
    # Согласование обязательно: смещение бывает любым (страховочное напоминание
    # ставится на «сколько получится»), и «за 4 минут» выглядит как ошибка.
    return f"за {offset_minutes} {timeutil.plural(offset_minutes, 'минуту', 'минуты', 'минут')}"


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


def urgency_emoji(due_iso: str | None, tz: ZoneInfo, now: datetime | None = None) -> str:
    """Кружок срочности: красный — горит, оранжевый — скоро, белый — спокойно."""
    if not due_iso:
        return URGENCY_CALM

    moment = now or timeutil.now_utc()
    due = timeutil.parse_iso(due_iso)
    if due - moment <= _HOT_WINDOW:  # сюда попадает и просроченное: остаток отрицателен
        return URGENCY_HOT

    # «Сегодня-завтра» — по календарю, а не «в течение 48 часов»: глядя
    # во вторник вечером, дело на вечер среды видно как послезавтрашнее,
    # и называть его «завтра» было бы неправдой.
    if due.astimezone(tz).date() <= moment.astimezone(tz).date() + _DAY:
        return URGENCY_SOON
    return URGENCY_CALM


def _late_note(due_iso: str | None, tz: ZoneInfo, now: datetime) -> str:
    """Подпись «просрочено на 2 дня» — и только она.

    Всё остальное о сроке строка уже говорит сама: время стоит в её начале,
    а насколько оно близко — видно по кружку. «через 4 часа» под каждой строкой
    было повторением того же самого дважды, и именно оно превращало список
    в шум. Просрочку же по одному «09:00» не прочитать: это нынешнее утро или
    вчерашнее? Её и оставляем.
    """
    if not due_iso or timeutil.parse_iso(due_iso) >= now:
        return ""
    return timeutil.human_left(due_iso, tz, now)


def task_line(task: dict[str, Any], tz: ZoneInfo, now: datetime | None = None) -> str:
    """Одна строка списка: '🔴 15:00 📝 Контрольная по физике · #7'.

    Раньше строка занимала два-три ряда: срок уезжал на отдельную строку
    с отступом, а между делами вставлялась пустая строка. На десятке дел это
    сплошная простыня, в которой глазу не за что зацепиться. Теперь всё дело —
    одна строка, а день вынесен в заголовок группы (см. task_list), поэтому
    повторять дату в каждой строке не нужно.
    """
    moment = now or timeutil.now_utc()
    due_iso = task.get("due_at")

    parts = [urgency_emoji(due_iso, tz, moment)]
    if due_iso:
        # Время в начале строки: по нему список и просматривают.
        parts.append(f"<b>{timeutil.parse_iso(due_iso).astimezone(tz):%H:%M}</b>")
    parts.append(category_emoji(task.get("category")))
    parts.append(escape(str(task["title"])))
    if task.get("recurrence"):
        # Повторяющееся дело важно отличать от разового прямо в списке:
        # иначе непонятно, появится ли оно снова после отметки «выполнено».
        parts.append("🔁")

    line = f"{' '.join(parts)} · <code>#{task['id']}</code>"

    note = _late_note(due_iso, tz, moment)
    if note:
        line += f" · <i>{note}</i>"
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


def _local_date(due_iso: str | None, tz: ZoneInfo) -> date | None:
    """Локальная дата срока — ключ группировки. None = дела без срока."""
    if not due_iso:
        return None
    return timeutil.parse_iso(due_iso).astimezone(tz).date()


def _day_header(day: date | None, today: date) -> str:
    """'Сегодня · ср, 15 окт' / 'Завтра · чт, 16 окт' / 'Пт, 17 окт' / 'Без срока'."""
    if day is None:
        return "<b>Без срока</b>"

    label = f"{timeutil.WEEKDAYS_SHORT[day.weekday()]}, {day.day} {timeutil.MONTHS_SHORT[day.month - 1]}"
    if day == today:
        return f"<b>Сегодня</b> · {label}"
    if day == today + _DAY:
        return f"<b>Завтра</b> · {label}"
    return f"<b>{label}</b>"


def task_list(
    tasks: Iterable[dict[str, Any]],
    tz: ZoneInfo,
    *,
    group_days: bool = True,
    now: datetime | None = None,
) -> str:
    """Список дел: одна строка на дело, дни — заголовками.

    Группировка по дням здесь не украшение, а способ читать список: заголовок
    задаёт день, строки — только время и название. Так десять дел занимают
    десять строк вместо тридцати, и по списку можно пробежать глазами.

    `group_days=False` нужен там, где день уже назван в шапке сообщения
    (/today, /tomorrow): второй такой же заголовок внутри был бы повтором.

    `now` — момент, на который считается список. Нужен там, где список
    рисуется не «прямо сейчас» (и в проверках): иначе один и тот же набор дел
    выглядел бы по-разному в зависимости от секунды запуска.
    """
    tasks = list(tasks)
    if not tasks:
        return ""

    moment = now or timeutil.now_utc()

    if not group_days:
        return "\n".join(task_line(task, tz, moment) for task in tasks)

    groups: dict[date | None, list[dict[str, Any]]] = {}
    for task in tasks:
        groups.setdefault(_local_date(task.get("due_at"), tz), []).append(task)

    # Дела без срока — всегда последними, остальные по возрастанию даты.
    # Порядок внутри дня сохраняется таким, каким его отдала база (по сроку).
    today = moment.astimezone(tz).date()
    blocks = [
        f"{_day_header(day, today)}\n"
        + "\n".join(task_line(task, tz, moment) for task in groups[day])
        for day in sorted(groups, key=lambda value: (value is None, value or today))
    ]
    return "\n\n".join(blocks)


def urgency_legend(tasks: Iterable[dict[str, Any]], tz: ZoneInfo, *, now: datetime | None = None) -> str:
    """Подсказка, что значат кружки, — но только когда есть на что смотреть.

    Раньше она уезжала в конец каждого списка, и в утреннем дайджесте, где
    блоков три, повторялась трижды. Теперь она одна на сообщение и появляется
    лишь тогда, когда в списке есть горящее или близкое: если все дела
    спокойные, объяснять нечего.
    """
    moment = now or timeutil.now_utc()
    if not any(urgency_emoji(task.get("due_at"), tz, moment) != URGENCY_CALM for task in tasks):
        return ""
    return f"<i>{URGENCY_LEGEND}</i>"


def render_reminder(item: dict[str, Any], tz: ZoneInfo) -> str:
    """Текст проактивного напоминания, который бот присылает сам.

    Флаг ``late`` означает, что напоминание не удалось отправить вовремя
    (сервер лежал, пропадала сеть). Молча догонять нельзя: пользователь решит,
    что бот опять забыл, — поэтому задержку называем прямо.
    """
    label = reminder_label(int(item["offset_minutes"]))
    header = f"⏰ <b>Напоминание {label}</b>"
    if item.get("late"):
        header += " · <i>с задержкой</i>"

    lines = [
        header,
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

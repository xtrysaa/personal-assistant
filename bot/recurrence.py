"""Правила повторения задач: «каждый вторник и четверг».

Формат хранения выбран строкой, а не JSON, по двум причинам: он читается
глазами прямо в sqlite3 (`SELECT id, recurrence FROM tasks`) и легко парсится
без разбора структуры. Поддерживаются только базовые правила:

    daily          — каждый день
    weekly:1,3     — по вторникам и четвергам

Числа — это ``datetime.weekday()`` (0 = понедельник), то есть тот же порядок,
что в ``timeutil.WEEKDAYS_SHORT``. Интервалов («раз в две недели») и конечной
даты серии здесь сознательно нет — они пока не нужны, а лишний формат это
лишний код, который надо тестировать и поддерживать.

Модуль не знает ни про базу, ни про Telegram: только чистые функции.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DAILY = "daily"
WEEKLY = "weekly"

_PREFIX = f"{WEEKLY}:"

# Целиком, а не «каждый + слово»: в русском согласование разное
# («каждый вторник», но «каждую среду» и «каждое воскресенье»).
_WEEKDAYS_ONE = (
    "каждый понедельник",
    "каждый вторник",
    "каждую среду",
    "каждый четверг",
    "каждую пятницу",
    "каждую субботу",
    "каждое воскресенье",
)

# Дательный множественного («по вторникам») — для нескольких дней.
_WEEKDAYS_MANY = (
    "понедельникам", "вторникам", "средам", "четвергам", "пятницам", "субботам", "воскресеньям",
)


def parse(rule: str) -> tuple[str, tuple[int, ...]]:
    """Разобрать правило: 'weekly:3,1' -> ('weekly', (1, 3)).

    Возвращает (вид, дни недели). Для 'daily' список дней пуст.
    Мусор — ValueError: его поймает execute_tool и вернёт модели понятный текст.
    """
    text = str(rule or "").strip().lower()

    if text == DAILY:
        return DAILY, ()

    if text.startswith(_PREFIX):
        raw_days = text[len(_PREFIX) :]
        days: list[int] = []
        for chunk in raw_days.split(","):
            chunk = chunk.strip()
            if not chunk.isdigit():
                raise ValueError(
                    f"Правило повторения {rule!r}: {chunk!r} не день недели. "
                    "Ожидается 'daily' или 'weekly:1,3' (0 = понедельник)."
                )
            day = int(chunk)
            if not 0 <= day <= 6:
                raise ValueError(
                    f"Правило повторения {rule!r}: день недели {day} вне диапазона 0..6."
                )
            if day not in days:
                days.append(day)
        if not days:
            raise ValueError(f"Правило повторения {rule!r}: не указан ни один день недели.")
        return WEEKLY, tuple(sorted(days))

    raise ValueError(
        f"Неизвестное правило повторения {rule!r}. Ожидается 'daily' или 'weekly:1,3'."
    )


def dump(rule: str) -> str:
    """Канонический вид правила — для сравнения и хранения.

    Канонизация здесь не косметика: 'weekly:3,1' и 'weekly:1,3' — одно и то же,
    и в базе они должны лежать одинаково. Полный набор дней сводим к 'daily'.
    """
    kind, days = parse(rule)
    if kind == DAILY or len(days) == 7:
        return DAILY
    return _PREFIX + ",".join(str(day) for day in days)


def label(rule: str) -> str:
    """Человеческая подпись: 'каждый день', 'каждый вторник', 'по вторникам и четвергам'."""
    kind, days = parse(rule)

    if kind == DAILY or len(days) == 7:
        return "каждый день"
    if len(days) == 1:
        return _WEEKDAYS_ONE[days[0]]
    if len(days) == 2:
        return f"по {_WEEKDAYS_MANY[days[0]]} и {_WEEKDAYS_MANY[days[1]]}"
    return "по " + ", ".join(_WEEKDAYS_MANY[day] for day in days[:-1]) + f" и {_WEEKDAYS_MANY[days[-1]]}"


def next_due(due_utc: datetime, rule: str, tz: ZoneInfo) -> datetime:
    """Следующий срок серии — строго после текущего.

    Время суток сохраняем ЛОКАЛЬНОЕ, а не UTC: «тренировка в 16:00» должна
    остаться в 16:00. При переходе на летнее время арифметика в UTC сдвинула бы
    её на час. Считаем через local-дату пользователя и только потом переводим
    в UTC — так же, как это делает timeutil.parse_local.
    """
    kind, days = parse(rule)
    local = due_utc.astimezone(tz)

    if kind == DAILY:
        target = local.date() + timedelta(days=1)
    else:
        target = None
        for step in range(1, 8):  # ищем ближайший день недели из правила
            candidate = local.date() + timedelta(days=step)
            if candidate.weekday() in days:
                target = candidate
                break
        if target is None:  # недостижимо: дни недели проверены на диапазон в parse
            raise ValueError(f"Не удалось найти следующий день для правила {rule!r}.")

    moment = datetime.combine(target, local.time(), tzinfo=tz)
    return moment.astimezone(timezone.utc)

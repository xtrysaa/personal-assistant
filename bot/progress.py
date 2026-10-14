"""Учёт подготовки: баллы пробников и часы занятий.

Всё здесь — чистые функции без сети и Telegram: их проверяет smoke-тест,
а используют и команда /progress, и инструмент get_progress для модели.
Считать статистику в двух местах по-разному — верный способ получить два
разных ответа на один вопрос, поэтому арифметика живёт только тут
(`summarize`), а рендер (`build_report`) берёт готовые числа.

График рисуется символами ``█`` и ``░``, а не картинкой: на бесплатном
хостинге 500 МБ RAM, и matplotlib туда не влезет. Полоса из символов
работает в любом клиенте Telegram и не требует зависимости.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from . import timeutil

KIND_SCORE = "score"
KIND_HOURS = "hours"
KINDS = (KIND_SCORE, KIND_HOURS)

SUBJECT_LABELS: dict[str, str] = {
    "sat": "SAT",
    "ielts": "IELTS",
    "ent": "ЕНТ",
    "math": "Математика",
    "physics": "Физика",
    "english": "Английский",
    "russian": "Русский язык",
    "history": "История",
    "other": "Другое",
}

SUBJECT_EMOJI: dict[str, str] = {
    "sat": "🎯",
    "ielts": "🌍",
    "ent": "📋",
    "math": "📐",
    "physics": "🧲",
    "english": "🔤",
    "russian": "📖",
    "history": "🏛",
    "other": "📌",
}

# Максимум шкалы по умолчанию. Нужен, чтобы полоса графика была осмысленной,
# даже когда модель не назвала максимум: 680 без шкалы ничего не говорит.
DEFAULT_SCALES: dict[str, float] = {
    "sat": 1600.0,
    "ielts": 9.0,
    "ent": 140.0,
}

# Порядок вывода в отчёте: сначала то, к чему он готовится целенаправленно.
SUBJECTS = tuple(SUBJECT_LABELS)

# Синонимы, которыми пишут в чате. Нужны, чтобы «математика» и «math» не
# разъехались по двум разным строкам статистики.
_SUBJECT_SYNONYMS: dict[str, str] = {
    "sat": "sat",
    "sat math": "sat",
    "пробник sat": "sat",
    "ielts": "ielts",
    "ent": "ent",
    "ент": "ent",
    "math": "math",
    "математика": "math",
    "матан": "math",
    "физика": "physics",
    "physics": "physics",
    "english": "english",
    "английский": "english",
    "англ": "english",
    "русский": "russian",
    "русский язык": "russian",
    "история": "history",
    "history": "history",
    "другое": "other",
    "прочее": "other",
    "other": "other",
}

BAR_WIDTH = 12
_FULL = "█"
_EMPTY = "░"

# Больше суток в одной записи — почти наверняка опечатка («20» вместо «2.0»).
_MAX_HOURS_PER_ENTRY = 24.0


# --------------------------------------------------------------------------- #
#  Предметы
# --------------------------------------------------------------------------- #

def resolve_subject(raw: Any) -> str | None:
    """Привести название предмета к белому списку. Незнакомое -> None.

    None, а не 'other': вызывающий сам решает, что делать с непонятным
    вводом — команда /progress предложит список, а инструмент вернёт модели
    ошибку, чтобы она переспросила.
    """
    if raw is None:
        return None
    text = " ".join(str(raw).strip().lower().split())
    if not text:
        return None
    if text in SUBJECT_LABELS:
        return text
    return _SUBJECT_SYNONYMS.get(text)


def subject_label(subject: str) -> str:
    return SUBJECT_LABELS.get(subject, subject)


def subject_emoji(subject: str) -> str:
    return SUBJECT_EMOJI.get(subject, SUBJECT_EMOJI["other"])


# --------------------------------------------------------------------------- #
#  Значения
# --------------------------------------------------------------------------- #

def parse_value(raw: Any, *, kind: str) -> float:
    """Разобрать число (балл или часы). Мусор и отрицательные — ValueError.

    Запятая как десятичный разделитель принимается: «6,5» по IELTS пишут
    чаще, чем «6.5».
    """
    if isinstance(raw, bool) or raw is None:
        raise ValueError("Не указано числовое значение.")

    try:
        value = float(str(raw).replace(",", ".").strip())
    except ValueError as exc:
        raise ValueError(f"{raw!r} не похоже на число.") from exc

    if value <= 0:
        raise ValueError("Значение должно быть больше нуля.")

    if kind == KIND_HOURS and value > _MAX_HOURS_PER_ENTRY:
        raise ValueError(
            f"{value:g} ч за одну запись — слишком много: в сутках 24 часа. "
            "Записывай занятия по отдельным дням."
        )

    return value


def format_value(kind: str, value: float) -> str:
    """'680' для балла, '1.5 часа' / '2 часа' для часов."""
    if kind != KIND_HOURS:
        return f"{value:g}"
    # Дробное количество требует родительного единственного: «1.5 часа».
    if float(value).is_integer():
        return f"{value:g} {timeutil.plural(int(value), 'час', 'часа', 'часов')}"
    return f"{value:g} часа"


def bar(value: float, peak: float, width: int = BAR_WIDTH) -> str:
    """Полоса прогресса: value в диапазоне 0..peak -> '████░░░░░░░░'.

    Ненулевое значение всегда получает хотя бы один закрашенный блок: иначе
    «20 минут» и «нет занятий» выглядели бы на графике одинаково.
    """
    if peak <= 0 or value <= 0:
        return _EMPTY * width
    filled = int(round(value / peak * width))
    filled = max(1, min(width, filled))
    return _FULL * filled + _EMPTY * (width - filled)


# --------------------------------------------------------------------------- #
#  Сводка и отчёт
# --------------------------------------------------------------------------- #

def summarize(rows: Iterable[dict[str, Any]], *, since: str | None = None) -> dict[str, Any]:
    """Свернуть записи прогресса в статистику по предметам.

    rows  — строки из db.list_progress в любом порядке.
    since — локальная дата, с которой считаются ЧАСЫ. К баллам она не
        применяется: по предмету интересен последний результат и динамика
        к предыдущему, а не окно в неделю.
    """
    score_rows: dict[str, list[dict[str, Any]]] = {}
    hours_total: dict[str, float] = {}
    hours_sessions: dict[str, int] = {}

    for row in rows:
        subject = row["subject"]
        value = float(row["value"])

        if row["kind"] == KIND_SCORE:
            score_rows.setdefault(subject, []).append(row)
        elif row["kind"] == KIND_HOURS:
            if since is not None and row["occurred_on"] < since:
                continue
            hours_total[subject] = hours_total.get(subject, 0.0) + value
            hours_sessions[subject] = hours_sessions.get(subject, 0) + 1

    scores: dict[str, dict[str, Any]] = {}
    for subject, items in score_rows.items():
        # По возрастанию даты: последний элемент — свежий результат.
        items.sort(key=lambda row: (row["occurred_on"], row["id"]))
        last, previous = items[-1], (items[-2] if len(items) > 1 else None)

        entry: dict[str, Any] = {
            "value": float(last["value"]),
            "on": last["occurred_on"],
            "scale": last.get("scale") or DEFAULT_SCALES.get(subject),
            "best": max(float(item["value"]) for item in items),
            "attempts": len(items),
        }
        if previous is not None:
            entry["prev"] = float(previous["value"])
            entry["delta"] = round(float(last["value"]) - float(previous["value"]), 2)
        scores[subject] = entry

    # Порядок: сначала предметы из белого списка в его порядке, потом остальные.
    order = {subject: index for index, subject in enumerate(SUBJECTS)}
    scores = dict(sorted(scores.items(), key=lambda item: order.get(item[0], 99)))

    hours = {
        subject: {"total": round(total, 2), "sessions": hours_sessions[subject]}
        for subject, total in sorted(hours_total.items(), key=lambda item: -item[1])
    }

    return {
        "since": since,
        "scores": scores,
        "hours": hours,
        "hours_total": round(sum(hours_total.values()), 2),
    }


def select(data: dict[str, Any], subject: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Выбрать блоки сводки по одному предмету (или все, если subject = None).

    Нужна и команде /progress, и инструменту get_progress: без общего места
    фильтр по предмету рано или поздно разошёлся бы между ними.
    """
    if not subject:
        return data["scores"], data["hours"]
    return (
        {subject: data["scores"][subject]} if subject in data["scores"] else {},
        {subject: data["hours"][subject]} if subject in data["hours"] else {},
    )


def build_report(
    rows: Iterable[dict[str, Any]],
    *,
    since: str,
    until: str,
    subject: str | None = None,
) -> str:
    """Текст отчёта для /progress. HTML, как и всё, что уходит в Telegram."""
    data = summarize(rows, since=since)
    scores, hours = select(data, subject)
    hours_total = sum(item["total"] for item in hours.values())

    title = "📊 <b>Подготовка</b>"
    if subject:
        title += f" · {subject_emoji(subject)} {subject_label(subject)}"

    blocks: list[str] = []

    if scores:
        lines = ["<b>Пробники</b>"]
        for name, stat in scores.items():
            scale = stat["scale"]
            head = f"▸ {subject_emoji(name)} <b>{subject_label(name)}</b> — {stat['value']:g}"
            if scale:
                head += f" / {scale:g}"
            head += f" · <i>{_delta_text(stat)}</i>"
            lines.append(head)
            lines.append(f"   {bar(stat['value'], scale or stat['value'])}")
            if stat["attempts"] > 1:
                lines.append(f"   <i>лучший: {stat['best']:g} · попыток: {stat['attempts']}</i>")
        blocks.append("\n".join(lines))

    if hours:
        lines = ["<b>Часы занятий</b>"]
        peak = max(item["total"] for item in hours.values())
        for name, stat in hours.items():
            lines.append(
                f"▸ {subject_emoji(name)} {subject_label(name)} — "
                f"<b>{format_value(KIND_HOURS, stat['total'])}</b>"
            )
            lines.append(f"   {bar(stat['total'], peak)}")
        lines.append(f"Всего: <b>{format_value(KIND_HOURS, hours_total)}</b>")
        blocks.append("\n".join(lines))

    header = f"{title}\n<i>{_period(since, until)}</i>"

    if not blocks:
        return (
            f"{header}\n\n<i>Записей пока нет.</i>\n"
            "Напиши, например: «сдал пробник SAT на 680» или «2 часа математики»."
        )

    return header + "\n\n" + "\n\n───────────────\n".join(blocks)


def _delta_text(stat: dict[str, Any]) -> str:
    """'+40 к прошлому' / '−20 к прошлому' / 'первый результат'."""
    delta = stat.get("delta")
    if delta is None:
        return "первый результат"
    if delta > 0:
        return f"+{delta:g} к прошлому"
    if delta < 0:
        # U+2212: типографский минус, читается лучше дефиса в строке с цифрами.
        return f"−{abs(delta):g} к прошлому"
    return "без изменений"


def _period(since: str, until: str) -> str:
    """'7 окт — 14 окт' из двух локальных дат."""
    return f"{_short_date(since)} — {_short_date(until)}"


def _short_date(value: str) -> str:
    moment = date.fromisoformat(value)
    return f"{moment.day} {timeutil.MONTHS_SHORT[moment.month - 1]}"

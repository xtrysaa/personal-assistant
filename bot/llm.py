"""Слой LLM: разговор с моделью + function calling (tool use).

Логика такая:
1. Пользователь пишет свободным текстом (или присылает фото/голосовое).
2. Модель либо просто отвечает, либо просит вызвать инструмент (add_task и т.д.).
3. Мы выполняем инструмент локально (SQLite), возвращаем результат модели.
4. Модель формулирует финальный ответ — уже зная, что реально попало в базу.

Так модель никогда не «галлюцинирует» сохранение: она видит реальный id и дату.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, time, timedelta
from typing import Any, Sequence

from openai import AsyncOpenAI

from . import db, formatting, progress, recurrence, timeutil
from .config import Settings, get_settings
from .prompts import build_system_prompt

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None

# Сколько последних сообщений диалога подкладывать модели как контекст.
HISTORY_LIMIT = 12

# Максимум шагов «модель → инструмент → модель» в одном запросе.
MAX_STEPS = 6

# Старые напоминания (просроченные сильнее этого окна) не отправляем.
REMINDER_GRACE = timedelta(hours=6)

# Предел размера картинки до base64-кодирования. Base64 добавляет ещё треть,
# а провайдер платит за это токенами: снимок экрана на 3 МБ уже велик.
IMAGE_MAX_BYTES = 3 * 1024 * 1024


def get_client() -> AsyncOpenAI:
    """Ленивая инициализация клиента (нужна, чтобы .env прочитался к моменту вызова)."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,  # None = официальный OpenAI
        )
    return _client


# --------------------------------------------------------------------------- #
#  Описание инструментов для модели
# --------------------------------------------------------------------------- #

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "add_task",
            "description": "Создать новое дело, дедлайн или событие в расписании пользователя.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Короткое название (до 80 символов) на языке пользователя.",
                    },
                    "due_at_local": {
                        "type": "string",
                        "description": (
                            "Срок в локальном времени пользователя, формат YYYY-MM-DDTHH:MM:SS. "
                            "Если срока нет — null."
                        ),
                    },
                    "category": {
                        "type": "string",
                        "enum": list(formatting.CATEGORIES),
                        "description": "Тип дела.",
                    },
                    "priority": {
                        "type": "string",
                        "enum": list(formatting.PRIORITIES),
                        "description": "Важность дела.",
                    },
                    "details": {
                        "type": "string",
                        "description": "Детали: требования, ссылки, что именно сделать. Необязательно.",
                    },
                    "remind_offsets_minutes": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "За сколько минут до срока напомнить. "
                            "Не указывай, если пользователь не просил особый режим."
                        ),
                    },
                    "recurrence": {
                        "type": "string",
                        "description": (
                            "Правило повторения для регулярных дел: 'daily' — каждый день, "
                            "'weekly:1,3' — по вторникам и четвергам (0 = понедельник, 6 = воскресенье). "
                            "Передавай ТОЛЬКО вместе с due_at_local. Для разовых дел не указывай."
                        ),
                    },
                },
                "required": ["title", "category", "priority"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "Показать дела из базы: за период, по категории или по поиску в названии.",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_local": {
                        "type": "string",
                        "description": "Начало периода (YYYY-MM-DD) в локальном времени. Необязательно.",
                    },
                    "to_local": {
                        "type": "string",
                        "description": "Конец периода (YYYY-MM-DD, не включая этот день). Необязательно.",
                    },
                    "category": {"type": "string", "enum": list(formatting.CATEGORIES)},
                    "status": {
                        "type": "string",
                        "enum": ["open", "completed", "any"],
                        "description": "По умолчанию open.",
                    },
                    "query": {"type": "string", "description": "Поиск по названию и деталям."},
                    "limit": {"type": "integer", "description": "Максимум строк, по умолчанию 30."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task",
            "description": "Изменить существующую задачу: перенести срок, переименовать, изменить важность или детали.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "id задачи из list_tasks."},
                    "new_title": {"type": "string"},
                    "new_due_at_local": {
                        "type": "string",
                        "description": "Новый срок, YYYY-MM-DDTHH:MM:SS (или YYYY-MM-DD).",
                    },
                    "new_category": {"type": "string", "enum": list(formatting.CATEGORIES)},
                    "new_priority": {"type": "string", "enum": list(formatting.PRIORITIES)},
                    "new_details": {"type": "string"},
                    "remind_offsets_minutes": {"type": "array", "items": {"type": "integer"}},
                    "new_recurrence": {
                        "type": "string",
                        "description": (
                            "Новое правило повторения: 'daily', 'weekly:1,3' "
                            "или 'none', чтобы снова сделать дело разовым. "
                            "У задачи обязательно должен остаться срок."
                        ),
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "Отметить задачу выполненной.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "query": {
                        "type": "string",
                        "description": "Если id неизвестен — часть названия задачи.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_task",
            "description": "Удалить задачу.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "query": {"type": "string", "description": "Если id неизвестен — часть названия."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_score",
            "description": (
                "Записать результат пробного теста или экзамена (SAT, IELTS, ЕНТ, школьные). "
                "Это НЕ задача: так копится статистика подготовки и видна динамика."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "enum": list(progress.SUBJECTS),
                        "description": "Предмет или экзамен.",
                    },
                    "value": {"type": "number", "description": "Набранный балл."},
                    "scale": {
                        "type": "number",
                        "description": (
                            "Максимум шкалы: SAT 1600, IELTS 9, ЕНТ 140. "
                            "Если пользователь не назвал — не передавай, подставим известный максимум."
                        ),
                    },
                    "occurred_on_local": {
                        "type": "string",
                        "description": "Дата теста YYYY-MM-DD, если это был не сегодняшний день.",
                    },
                    "note": {"type": "string", "description": "Заметка: что далось тяжело, где просел."},
                },
                "required": ["subject", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_hours",
            "description": (
                "Записать время, потраченное на подготовку. Это НЕ задача: "
                "так видно, сколько часов реально уходит на каждый предмет."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "enum": list(progress.SUBJECTS)},
                    "hours": {"type": "number", "description": "Количество часов (например 1.5)."},
                    "occurred_on_local": {
                        "type": "string",
                        "description": "Дата занятия YYYY-MM-DD, если это был не сегодняшний день.",
                    },
                    "note": {"type": "string", "description": "Что именно делал."},
                },
                "required": ["subject", "hours"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_progress",
            "description": (
                "Показать статистику подготовки: последние баллы пробников с динамикой "
                "и потраченные часы. Используй, если спрашивают про прогресс, "
                "«как у меня с SAT», «сколько я занимался»."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "enum": list(progress.SUBJECTS)},
                    "days": {
                        "type": "integer",
                        "description": "За сколько последних дней считать часы. По умолчанию 7.",
                    },
                },
            },
        },
    },
]


# --------------------------------------------------------------------------- #
#  Основной цикл
# --------------------------------------------------------------------------- #

async def run_agent(
    user_text: str,
    hint: str | None = None,
    images: Sequence[tuple[str, bytes]] = (),
) -> str:
    """Обработать сообщение пользователя и вернуть ответ ассистента.

    `hint` — служебная подсказка модели (например, для команды /add:
    «пользователь явно просит создать задачу»). В историю она не попадает.

    `images` — пары (mime, байты) приложенных картинок. В историю диалога
    уходит ТОЛЬКО текст: base64 в базе раздувал бы её без предела, а контекст
    от этого не выигрывает — расписание имеет смысл один раз, при разборе.
    """
    settings = get_settings()
    history = await db.get_history(limit=HISTORY_LIMIT)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(settings)}
    ]
    messages.extend(history)

    content = user_text if not hint else f"{user_text}\n\n[служебная подсказка: {hint}]"
    messages.append({"role": "user", "content": _build_user_content(content, images)})

    await db.add_message("user", user_text)

    for _ in range(MAX_STEPS):
        response = await get_client().chat.completions.create(
            model=settings.openai_model,
            messages=messages,  # type: ignore[arg-type]
            tools=TOOLS,  # type: ignore[arg-type]
            tool_choice="auto",
            temperature=0.2,
        )
        message = response.choices[0].message

        if not message.tool_calls:
            answer = (message.content or "").strip() or "Не понял, повтори, пожалуйста."
            await db.add_message("assistant", answer)
            return answer

        # Сообщение модели с запросами инструментов добавляем в переписку.
        messages.append(message.model_dump(exclude_none=True))

        for call in message.tool_calls:
            arguments = _load_arguments(call.function.arguments)
            logger.info("tool_call %s(%s)", call.function.name, arguments)
            result = await execute_tool(call.function.name, arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )

    fallback = "Слишком много шагов подряд. Напишите, пожалуйста, запрос одним сообщением."
    await db.add_message("assistant", fallback)
    return fallback


def _build_user_content(
    text: str, images: Sequence[tuple[str, bytes]]
) -> str | list[dict[str, Any]]:
    """Собрать `content` для запроса к модели.

    Без картинок возвращаем обычную строку: так запрос остаётся ровно таким,
    каким был до появления фото, и текстовый/голосовой пути не меняются.
    С картинками — список частей в формате OpenAI vision, который OpenRouter
    принимает как есть:

        [{"type": "text", "text": ...},
         {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}]
    """
    if not images:
        return text

    parts: list[dict[str, Any]] = [
        {"type": "text", "text": text or "Разбери, что на изображении."}
    ]
    for mime, payload in images:
        if len(payload) > IMAGE_MAX_BYTES:
            raise ValueError(
                f"Изображение больше {IMAGE_MAX_BYTES // (1024 * 1024)} МБ — "
                "отправь снимок меньшего размера."
            )
        encoded = base64.b64encode(payload).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
    return parts


def _load_arguments(raw: str | None) -> dict[str, Any]:
    """Разобрать JSON-аргументы инструмента; пустая строка = пустой объект."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Не удалось разобрать аргументы инструмента: %r", raw)
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --------------------------------------------------------------------------- #
#  Выполнение инструментов
# --------------------------------------------------------------------------- #

async def execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Единая точка выполнения любого инструмента. Ошибки возвращаем модели, а не бросаем."""
    settings = get_settings()
    handlers = {
        "add_task": _tool_add_task,
        "list_tasks": _tool_list_tasks,
        "update_task": _tool_update_task,
        "complete_task": _tool_complete_task,
        "delete_task": _tool_delete_task,
        "log_score": _tool_log_score,
        "log_hours": _tool_log_hours,
        "get_progress": _tool_get_progress,
    }

    handler = handlers.get(name)
    if handler is None:
        return {"ok": False, "error": f"Неизвестный инструмент: {name}"}

    try:
        return await handler(arguments, settings)
    except ValueError as exc:  # ожидаемые ошибки валидации — отдаём модели текстом
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — не роняем бота из-за одного сбоя
        logger.exception("Сбой инструмента %s", name)
        return {"ok": False, "error": f"Внутренняя ошибка: {exc}"}


async def _tool_add_task(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    title = str(args.get("title") or "").strip()
    if not title:
        raise ValueError("Не указано название задачи (title).")

    due_at = _parse_due(args.get("due_at_local"), settings, allow_past=False)

    raw_offsets = args.get("remind_offsets_minutes")
    if raw_offsets:
        offsets: Sequence[int] = tuple(int(offset) for offset in raw_offsets)
    else:
        offsets = settings.default_offsets

    task = await db.create_task(
        title=title,
        due_at=due_at,
        category=_pick(args.get("category"), formatting.CATEGORIES, "other"),
        priority=_pick(args.get("priority"), formatting.PRIORITIES, "normal"),
        details=(str(args["details"]).strip() if args.get("details") else None),
        offsets=offsets,
        source="chat",
        recurrence=_recurrence_arg(args.get("recurrence")),
    )
    return {"ok": True, "task": _dump(task, settings)}


async def _tool_list_tasks(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    status = args.get("status") or "open"
    if status not in {"open", "completed", "any"}:
        status = "open"

    # Границы периода — локальная полночь. В list_tasks конец периода исключается (< end),
    # поэтому «to_local: 2026-11-16» означает «до конца 15 ноября» включительно.
    start = _parse_day(args.get("from_local"), settings)
    end = _parse_day(args.get("to_local"), settings)

    limit = int(args.get("limit") or 30)
    limit = max(1, min(limit, 100))

    tasks = await db.list_tasks(
        status=status,
        start=start,
        end=end,
        category=args.get("category") if args.get("category") in formatting.CATEGORIES else None,
        query=(str(args["query"]).strip() or None) if args.get("query") else None,
        limit=limit,
    )

    return {
        "ok": True,
        "count": len(tasks),
        "today_local": datetime.now(settings.tz).strftime("%Y-%m-%d"),
        "tasks": [_dump(task, settings) for task in tasks],
    }


async def _tool_update_task(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    task_id = await _resolve_task_id(args, settings)

    changes: dict[str, Any] = {}
    if args.get("new_title"):
        changes["title"] = str(args["new_title"]).strip()
    if args.get("new_details"):
        changes["details"] = str(args["new_details"]).strip()
    if args.get("new_category") in formatting.CATEGORIES:
        changes["category"] = args["new_category"]
    if args.get("new_priority") in formatting.PRIORITIES:
        changes["priority"] = args["new_priority"]
    if args.get("new_recurrence") is not None:
        changes["recurrence"] = _recurrence_arg(args["new_recurrence"])  # None = сделать разовой

    touch_due = bool(args.get("new_due_at_local"))
    due_at = _parse_due(args["new_due_at_local"], settings, allow_past=True) if touch_due else None

    raw_offsets = args.get("remind_offsets_minutes")
    offsets = tuple(int(offset) for offset in raw_offsets) if raw_offsets else None

    if not changes and not touch_due and offsets is None:
        raise ValueError("Нечего менять: не переданы новые значения.")

    task = await db.update_task(
        task_id,
        changes,
        due_at=due_at,
        touch_due=touch_due,
        offsets=offsets or (settings.default_offsets if touch_due else None),
    )
    if task is None:
        raise ValueError(f"Задача #{task_id} не найдена (возможно, удалена).")

    return {"ok": True, "task": _dump(task, settings)}


async def _tool_complete_task(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    task_id = await _resolve_task_id(args, settings)
    task = await db.complete_task(task_id)
    if task is None:
        raise ValueError(f"Задача #{task_id} не найдена.")
    return {"ok": True, "task": _dump(task, settings)}


async def _tool_delete_task(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    task_id = await _resolve_task_id(args, settings)
    task = await db.get_task(task_id)
    if task is None:
        raise ValueError(f"Задача #{task_id} не найдена.")
    await db.delete_task(task_id)
    return {"ok": True, "deleted": {"id": task_id, "title": task["title"]}}


# --------------------------------------------------------------------------- #
#  Инструменты прогресса подготовки
# --------------------------------------------------------------------------- #

async def _tool_log_score(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    subject = _subject_arg(args.get("subject"))
    value = progress.parse_value(args.get("value"), kind=progress.KIND_SCORE)

    # Шкалу берём из аргумента, иначе — известный максимум для этого экзамена.
    # Без шкалы полоса на графике теряет смысл: 680 само по себе ничего не значит.
    scale = args.get("scale")
    scale_value = float(scale) if scale else progress.DEFAULT_SCALES.get(subject)
    if scale_value and value > scale_value:
        raise ValueError(
            f"Балл {value:g} больше максимума шкалы ({scale_value:g}). "
            "Уточни значение или передай правильный scale."
        )

    entry = await db.add_progress(
        kind=progress.KIND_SCORE,
        subject=subject,
        value=value,
        scale=scale_value,
        note=_note_arg(args),
        occurred_on=args.get("occurred_on_local"),
    )
    return {
        "ok": True,
        "entry": _dump_progress(entry),
        "today_local": datetime.now(settings.tz).strftime("%Y-%m-%d"),
    }


async def _tool_log_hours(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    subject = _subject_arg(args.get("subject"))
    hours = progress.parse_value(args.get("hours"), kind=progress.KIND_HOURS)

    entry = await db.add_progress(
        kind=progress.KIND_HOURS,
        subject=subject,
        value=hours,
        note=_note_arg(args),
        occurred_on=args.get("occurred_on_local"),
    )
    return {
        "ok": True,
        "entry": _dump_progress(entry),
        "today_local": datetime.now(settings.tz).strftime("%Y-%m-%d"),
    }


async def _tool_get_progress(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    days = int(args.get("days") or 7)
    days = max(1, min(days, 180))

    subject = _subject_arg(args["subject"]) if args.get("subject") else None

    until = datetime.now(settings.tz).date()
    since = (until - timedelta(days=days - 1)).isoformat()

    data = progress.summarize(await db.list_progress(subject=subject), since=since)
    scores, hours = progress.select(data, subject)

    return {
        "ok": True,
        "today_local": until.isoformat(),
        "hours_period_days": days,
        "hours_since": since,
        "scores": scores,
        "hours": hours,
        "hours_total": sum(item["total"] for item in hours.values()),
    }


# --------------------------------------------------------------------------- #
#  Вспомогательные функции
# --------------------------------------------------------------------------- #

def _pick(value: Any, allowed: Sequence[str], default: str) -> str:
    """Вернуть значение, если оно из белого списка, иначе default."""
    return value if isinstance(value, str) and value in allowed else default


def _recurrence_arg(raw: Any) -> str | None:
    """Разобрать правило повторения из аргументов модели.

    None и слова-отказы означают «дело разовое»: модели проще сказать 'none',
    чем не передать поле вовсе. Мусор вернёт ValueError из recurrence.dump —
    execute_tool отдаст его модели текстом, и она исправится.
    """
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in {"", "none", "no", "off", "нет", "никогда"}:
        return None
    return recurrence.dump(text)


def _subject_arg(raw: Any) -> str:
    """Предмет прогресса из белого списка. Незнакомый — ошибка для модели."""
    subject = progress.resolve_subject(raw)
    if subject is None:
        raise ValueError(
            f"Неизвестный предмет {raw!r}. Доступные: {', '.join(progress.SUBJECTS)}."
        )
    return subject


def _note_arg(args: dict[str, Any]) -> str | None:
    """Необязательная заметка к записи прогресса."""
    return str(args["note"]).strip() if args.get("note") else None


def _dump_progress(entry: dict[str, Any]) -> dict[str, Any]:
    """Компактное представление записи прогресса для модели."""
    result: dict[str, Any] = {
        "id": entry["id"],
        "kind": entry["kind"],
        "subject": progress.subject_label(entry["subject"]),
        "value": entry["value"],
        "display": progress.format_value(entry["kind"], entry["value"]),
        "occurred_on": entry["occurred_on"],
    }
    if entry.get("scale"):
        result["scale"] = entry["scale"]
    if entry.get("note"):
        result["note"] = entry["note"]
    return result


def _parse_due(raw: Any, settings: Settings, *, allow_past: bool) -> datetime | None:
    """Разобрать срок из аргументов модели и вернуть момент в UTC."""
    if not raw:
        return None
    try:
        due_at = timeutil.parse_local(str(raw), settings.tz)
    except ValueError as exc:
        raise ValueError(
            f"Не удалось разобрать дату {raw!r}. Формат: YYYY-MM-DDTHH:MM:SS."
        ) from exc

    if not allow_past and due_at < timeutil.now_utc():
        raise ValueError(
            f"Дата {raw!r} уже в прошлом (сегодня "
            f"{datetime.now(settings.tz):%Y-%m-%d}). Уточни у пользователя год или день."
        )
    return due_at


def _parse_day(raw: Any, settings: Settings) -> datetime | None:
    """Разобрать дату-границу периода: локальная полночь этого дня (UTC)."""
    if not raw:
        return None
    try:
        return timeutil.parse_local(str(raw), settings.tz, default_time=time.min)
    except ValueError as exc:
        raise ValueError(f"Не удалось разобрать дату {raw!r}.") from exc


async def _resolve_task_id(args: dict[str, Any], settings: Settings) -> int:
    """Найти id задачи: напрямую или по текстовому запросу.

    Если по запросу нашлось несколько задач — возвращаем ошибку со списком,
    чтобы модель уточнила у пользователя, а не угадывала.
    """
    raw_id = args.get("task_id")
    if raw_id not in (None, "", 0):
        return int(raw_id)

    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("Нужно передать task_id или query (часть названия задачи).")

    matches = await db.list_tasks(status="open", query=query, limit=10)
    if not matches:
        raise ValueError(f"Открытых задач по запросу «{query}» не найдено.")

    if len(matches) > 1:
        variants = "; ".join(
            f"#{task['id']} {task['title']} ({timeutil.fmt_due(task['due_at'], settings.tz)})"
            for task in matches
        )
        raise ValueError(
            f"Найдено несколько задач: {variants}. Уточни у пользователя, какая именно."
        )

    return int(matches[0]["id"])


def _recurrence_label(rule: str | None) -> str | None:
    """Подпись правила повторения. Битое правило отдаём как есть, а не роняем ответ."""
    if not rule:
        return None
    try:
        return recurrence.label(rule)
    except ValueError:
        return str(rule)


def _dump(task: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Компактное представление задачи для модели (и для JSON-ответа инструмента)."""
    result: dict[str, Any] = {
        "id": task["id"],
        "title": task["title"],
        "due_local": timeutil.fmt_day(task.get("due_at"), settings.tz),
        "left": timeutil.human_left(task.get("due_at"), settings.tz),
        "category": task["category"],
        "priority": task["priority"],
        "status": task["status"],
        "details": task.get("details"),
        # Именно reminders_planned, а не reminders: это не строки из БД,
        # а подписи вида «за сутки», понятные модели.
        "reminders_planned": [
            formatting.reminder_label(int(item["offset_minutes"]))
            for item in task.get("reminders", [])
            if not item.get("sent_at")
        ],
    }

    if task.get("recurrence"):
        result["recurrence"] = _recurrence_label(task["recurrence"])

    # Появляется только у complete_task: следующая задача серии, которую
    # он создал. Модель должна сказать о ней, а не оставить пользователя
    # в неведении, что серия продолжилась.
    next_task = task.get("next_task")
    if next_task:
        result["next_occurrence_local"] = timeutil.fmt_day(next_task.get("due_at"), settings.tz)

    return result

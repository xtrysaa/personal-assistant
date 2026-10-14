"""Слой LLM: разговор с моделью + function calling (tool use).

Логика такая:
1. Пользователь пишет свободным текстом.
2. Модель либо просто отвечает, либо просит вызвать инструмент (add_task и т.д.).
3. Мы выполняем инструмент локально (SQLite), возвращаем результат модели.
4. Модель формулирует финальный ответ — уже зная, что реально попало в базу.

Так модель никогда не «галлюцинирует» сохранение: она видит реальный id и дату.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta
from typing import Any, Sequence

from openai import AsyncOpenAI

from . import db, formatting, timeutil
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
]


# --------------------------------------------------------------------------- #
#  Основной цикл
# --------------------------------------------------------------------------- #

async def run_agent(user_text: str, hint: str | None = None) -> str:
    """Обработать сообщение пользователя и вернуть ответ ассистента.

    `hint` — служебная подсказка модели (например, для команды /add:
    «пользователь явно просит создать задачу»). В историю она не попадает.
    """
    settings = get_settings()
    history = await db.get_history(limit=HISTORY_LIMIT)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(settings)}
    ]
    messages.extend(history)

    content = user_text if not hint else f"{user_text}\n\n[служебная подсказка: {hint}]"
    messages.append({"role": "user", "content": content})

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

    fallback = "Слишком много шагов подряд. Напиши, пожалуйста, запрос одним сообщением."
    await db.add_message("assistant", fallback)
    return fallback


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
#  Вспомогательные функции
# --------------------------------------------------------------------------- #

def _pick(value: Any, allowed: Sequence[str], default: str) -> str:
    """Вернуть значение, если оно из белого списка, иначе default."""
    return value if isinstance(value, str) and value in allowed else default


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


def _dump(task: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Компактное представление задачи для модели (и для JSON-ответа инструмента)."""
    return {
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

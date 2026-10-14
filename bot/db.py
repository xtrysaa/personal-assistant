"""Слой доступа к данным: SQLite через aiosqlite.

Почему SQLite: это файл на диске, отдельный сервер БД не нужен — идеально
для бесплатного хостинга. Включён режим WAL, чтобы чтения не блокировались
записями при одновременной работе бота и планировщика.

Схема:
    tasks      — все дела/дедлайны
    reminders  — запланированные напоминания (одна строка = одно напоминание)
    history    — последние сообщения диалога, чтобы у ИИ был контекст
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Sequence

import aiosqlite

from . import timeutil

_DB_PATH = "./data/assistant.db"

# Поля, которые разрешено менять через update_task (защита от SQL-инъекции
# через имя колонки — значение всегда идёт параметром).
_UPDATABLE_FIELDS = frozenset({"title", "details", "category", "priority"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT    NOT NULL,
    details      TEXT,
    due_at       TEXT,                                  -- UTC ISO8601, NULL = без срока
    category     TEXT    NOT NULL DEFAULT 'other',
    priority     TEXT    NOT NULL DEFAULT 'normal',
    status       TEXT    NOT NULL DEFAULT 'open',       -- open | completed
    source       TEXT    NOT NULL DEFAULT 'chat',
    created_at   TEXT    NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS reminders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    offset_minutes INTEGER NOT NULL,                    -- за сколько минут до срока
    fire_at        TEXT    NOT NULL,                    -- UTC ISO8601
    sent_at        TEXT                                 -- NULL = ещё не отправлено
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reminders_task_offset
    ON reminders (task_id, offset_minutes);

CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL,                           -- user | assistant
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_due   ON tasks (status, due_at);
CREATE INDEX IF NOT EXISTS idx_reminders_pending  ON reminders (sent_at, fire_at);
"""


@asynccontextmanager
async def _connect() -> AsyncIterator[aiosqlite.Connection]:
    """Открыть соединение с нужными PRAGMA и row_factory."""
    async with aiosqlite.connect(_DB_PATH) as connection:
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        yield connection


async def init_db(path: str) -> None:
    """Создать папку, применить схему и включить WAL. Вызывается один раз при старте."""
    global _DB_PATH
    _DB_PATH = path

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    async with aiosqlite.connect(path) as connection:
        await connection.executescript("PRAGMA journal_mode = WAL;\n" + SCHEMA)
        await connection.commit()


# --------------------------------------------------------------------------- #
#  Задачи
# --------------------------------------------------------------------------- #

def _row_to_task(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "details": row["details"],
        "due_at": row["due_at"],
        "category": row["category"],
        "priority": row["priority"],
        "status": row["status"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


async def _sync_reminders(
    connection: aiosqlite.Connection,
    task_id: int,
    due_iso: str | None,
    offsets: Sequence[int],
) -> None:
    """Пересоздать напоминания задачи под новую дату срока.

    Удаляем ВСЕ строки задачи, включая уже отправленные: иначе старая строка
    с тем же offset_minutes (например «за час») заблокирует создание нового
    напоминания из-за уникального индекса, и после переноса срока напоминание
    просто пропадёт. Напоминания в прошлом не создаём, чтобы после смены даты
    не прилетела пачка старых.
    """
    await connection.execute("DELETE FROM reminders WHERE task_id = ?", (task_id,))
    if not due_iso:
        return

    due = timeutil.parse_iso(due_iso)
    now = timeutil.now_utc()
    for offset in offsets:
        fire_at = due - timedelta(minutes=offset)
        if fire_at <= now:
            continue
        await connection.execute(
            "INSERT OR IGNORE INTO reminders (task_id, offset_minutes, fire_at) VALUES (?, ?, ?)",
            (task_id, offset, fire_at.isoformat()),
        )


async def create_task(
    *,
    title: str,
    due_at: datetime | None = None,
    category: str = "other",
    priority: str = "normal",
    details: str | None = None,
    offsets: Sequence[int] = (),
    source: str = "chat",
) -> dict[str, Any]:
    """Создать задачу и напоминания к ней. Возвращает задачу с напоминаниями."""
    due_iso = due_at.astimezone(timezone.utc).isoformat() if due_at else None

    async with _connect() as connection:
        cursor = await connection.execute(
            """
            INSERT INTO tasks (title, details, due_at, category, priority, status, source, created_at)
            VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                title.strip(),
                details,
                due_iso,
                category,
                priority,
                source,
                timeutil.now_utc().isoformat(),
            ),
        )
        task_id = int(cursor.lastrowid or 0)
        await _sync_reminders(connection, task_id, due_iso, offsets)
        await connection.commit()

    task = await get_task(task_id)
    assert task is not None  # только что создали
    return task


async def get_task(task_id: int) -> dict[str, Any] | None:
    """Задача по id вместе со списком её напоминаний."""
    async with _connect() as connection:
        cursor = await connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = await cursor.fetchone()
        if row is None:
            return None

        task = _row_to_task(row)
        cursor = await connection.execute(
            """
            SELECT offset_minutes, fire_at, sent_at
            FROM reminders WHERE task_id = ?
            ORDER BY fire_at ASC
            """,
            (task_id,),
        )
        task["reminders"] = [dict(reminder) for reminder in await cursor.fetchall()]
        return task


async def list_tasks(
    *,
    status: str = "open",
    start: datetime | None = None,
    end: datetime | None = None,
    category: str | None = None,
    query: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Выборка задач с фильтрами.

    status : "open" | "completed" | "any"
    start/end : границы по сроку в UTC (задачи без срока при этом не попадают)
    """
    sql = ["SELECT * FROM tasks WHERE 1 = 1"]
    params: list[Any] = []

    if status != "any":
        sql.append("AND status = ?")
        params.append(status)

    if start is not None:
        sql.append("AND due_at IS NOT NULL AND due_at >= ?")
        params.append(start.astimezone(timezone.utc).isoformat())
    if end is not None:
        sql.append("AND due_at IS NOT NULL AND due_at < ?")
        params.append(end.astimezone(timezone.utc).isoformat())

    if category:
        sql.append("AND category = ?")
        params.append(category)

    if query:
        sql.append("AND (title LIKE ? OR IFNULL(details, '') LIKE ?)")
        pattern = f"%{query}%"
        params.extend([pattern, pattern])

    # Сортировка: сначала ближайшие по сроку, задачи без срока — в конце.
    sql.append("ORDER BY (due_at IS NULL), due_at ASC, id ASC LIMIT ?")
    params.append(limit)

    async with _connect() as connection:
        cursor = await connection.execute(" ".join(sql), params)
        return [_row_to_task(row) for row in await cursor.fetchall()]


async def update_task(
    task_id: int,
    changes: dict[str, Any],
    *,
    due_at: datetime | None = None,
    touch_due: bool = False,
    offsets: Sequence[int] | None = None,
) -> dict[str, Any] | None:
    """Обновить задачу. `touch_due=True` означает «меняем срок» (даже на None).

    `changes` — только текстовые/служебные поля. Смена статуса на completed
    автоматически закрывает задачу и снимает неотправленные напоминания.
    """
    assignments: list[str] = []
    params: list[Any] = []

    for field, value in changes.items():
        if field in _UPDATABLE_FIELDS and value is not None:
            assignments.append(f"{field} = ?")
            params.append(value)

    if touch_due:
        assignments.append("due_at = ?")
        params.append(due_at.astimezone(timezone.utc).isoformat() if due_at else None)

    new_status = changes.get("status")
    if new_status in {"open", "completed"}:
        assignments.append("status = ?")
        params.append(new_status)
        if new_status == "completed":
            assignments.append("completed_at = ?")
            params.append(timeutil.now_utc().isoformat())
        else:  # переоткрытие задачи
            assignments.append("completed_at = NULL")

    async with _connect() as connection:
        exists = await connection.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,))
        if await exists.fetchone() is None:
            return None

        if assignments:
            await connection.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?",  # noqa: S608 — имена колонок из белого списка
                [*params, task_id],
            )

        if new_status == "completed":
            await connection.execute(
                "DELETE FROM reminders WHERE task_id = ? AND sent_at IS NULL", (task_id,)
            )
        elif touch_due or offsets is not None:
            cursor = await connection.execute(
                "SELECT due_at FROM tasks WHERE id = ?", (task_id,)
            )
            row = await cursor.fetchone()
            due_iso = row["due_at"] if row else None
            await _sync_reminders(connection, task_id, due_iso, offsets or ())

        await connection.commit()

    return await get_task(task_id)


async def complete_task(task_id: int) -> dict[str, Any] | None:
    """Отметить задачу выполненной."""
    return await update_task(task_id, {"status": "completed"})


async def delete_task(task_id: int) -> bool:
    """Удалить задачу вместе с напоминаниями (ON DELETE CASCADE)."""
    async with _connect() as connection:
        cursor = await connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await connection.commit()
        return cursor.rowcount > 0


# --------------------------------------------------------------------------- #
#  Напоминания
# --------------------------------------------------------------------------- #

async def due_reminders(
    now: datetime, grace: timedelta
) -> tuple[list[dict[str, Any]], list[int]]:
    """Напоминания, которые пора отправить.

    Возвращает пару:
        fresh      — актуальные напоминания (отправляем)
        stale_ids  — просроченные сильнее, чем `grace` (помечаем отправленными без
                     отправки, чтобы после долгого простоя не прилетела пачка старых)
    """
    now_iso = now.astimezone(timezone.utc).isoformat()
    floor_iso = (now - grace).astimezone(timezone.utc).isoformat()

    async with _connect() as connection:
        cursor = await connection.execute(
            """
            SELECT r.id             AS reminder_id,
                   r.offset_minutes AS offset_minutes,
                   r.fire_at        AS fire_at,
                   t.id             AS task_id,
                   t.title          AS title,
                   t.details        AS details,
                   t.due_at         AS due_at,
                   t.category       AS category,
                   t.priority       AS priority
            FROM reminders AS r
            JOIN tasks AS t ON t.id = r.task_id
            WHERE r.sent_at IS NULL
              AND t.status = 'open'
              AND r.fire_at <= ?
            ORDER BY r.fire_at ASC
            LIMIT 50
            """,
            (now_iso,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]

    fresh = [row for row in rows if row["fire_at"] >= floor_iso]
    stale_ids = [row["reminder_id"] for row in rows if row["fire_at"] < floor_iso]
    return fresh, stale_ids


async def mark_reminders(reminder_ids: Sequence[int], *, sent: bool = True) -> None:
    """Пометить напоминания отправленными (или снять отметку)."""
    if not reminder_ids:
        return
    value = timeutil.now_utc().isoformat() if sent else None
    placeholders = ",".join("?" * len(reminder_ids))

    async with _connect() as connection:
        await connection.execute(
            f"UPDATE reminders SET sent_at = ? WHERE id IN ({placeholders})",  # noqa: S608
            [value, *reminder_ids],
        )
        await connection.commit()


# --------------------------------------------------------------------------- #
#  История диалога (контекст для LLM)
# --------------------------------------------------------------------------- #

async def add_message(role: str, content: str) -> None:
    """Сохранить сообщение диалога."""
    async with _connect() as connection:
        await connection.execute(
            "INSERT INTO history (role, content, created_at) VALUES (?, ?, ?)",
            (role, content, timeutil.now_utc().isoformat()),
        )
        await connection.commit()


async def get_history(limit: int = 12) -> list[dict[str, str]]:
    """Последние `limit` сообщений в хронологическом порядке."""
    async with _connect() as connection:
        cursor = await connection.execute(
            "SELECT role, content FROM history ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


async def clear_history() -> None:
    """Очистить контекст диалога (команда /reset)."""
    async with _connect() as connection:
        await connection.execute("DELETE FROM history")
        await connection.commit()

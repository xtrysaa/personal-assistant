"""Слой доступа к данным: SQLite через aiosqlite.

Почему SQLite: это файл на диске, отдельный сервер БД не нужен — идеально
для бесплатного хостинга. Включён режим WAL, чтобы чтения не блокировались
записями при одновременной работе бота и планировщика.

Схема:
    tasks      — все дела/дедлайны (в т.ч. серии повторяющихся)
    reminders  — запланированные напоминания (одна строка = одно напоминание)
    progress   — баллы пробников и часы подготовки
    history    — последние сообщения диалога, чтобы у ИИ был контекст

Схема описана дважды, и это осознанно: ``SCHEMA`` — актуальное состояние
(его получает свежая база одним скриптом), ``MIGRATIONS`` — шаги, которыми
старая база догоняет актуальное. ``CREATE TABLE IF NOT EXISTS`` умеет добавлять
таблицы, но не умеет добавлять колонку в существующую таблицу — ровно поэтому
миграции и нужны.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator, Sequence
from zoneinfo import ZoneInfo

import aiosqlite

from . import progress, recurrence, timeutil

logger = logging.getLogger(__name__)

_DB_PATH = "./data/assistant.db"

# Часовой пояс и набор напоминаний нужны слою данных ровно для одной операции:
# когда закрывается повторяющаяся задача, следующая серия должна встать на тот
# же ЛОКАЛЬНЫЙ час. Устанавливаются в init_db (см. run.py).
_TZ: ZoneInfo = ZoneInfo("Asia/Almaty")
_DEFAULT_OFFSETS: tuple[int, ...] = (10080, 1440, 60)

# Поля, которые разрешено менять через update_task (защита от SQL-инъекции
# через имя колонки — значение всегда идёт параметром).
_UPDATABLE_FIELDS = frozenset({"title", "details", "category", "priority", "recurrence"})

# Поля, для которых явный None означает «очистить», а не «аргумент не передан».
# У остальных текстовых полей None = «не менять» (см. update_task).
_NULLABLE_FIELDS = frozenset({"recurrence"})

# Страховочное напоминание для дела, срок которого уже на носу (см.
# _fallback_reminder): за сколько минут до срока его ставить и на сколько
# отложить его отправку, если и это время уже прошло.
_FALLBACK_LEAD_MINUTES = 10
_FALLBACK_DELAY_SECONDS = 30

# Сколько «догоняющих» напоминаний разрешено отправить за один тик.
# После долгого простоя (сервер лежал, пропала сеть) их может накопиться
# несколько десятков — остальные уйдут следующими тиками, раз в минуту.
_MAX_LATE_PER_TICK = 5

# Сколько пропущенных вхождений серии разрешено перешагнуть, перенося её вперёд
# (см. _roll_recurrence). Предел — только страховка от бесконечного цикла:
# ежедневная серия за три года укладывается в тысячу шагов.
_MAX_ROLL_STEPS = 1000

# Актуальная версия схемы. Свежая база получает её сразу, старая проходит
# MIGRATIONS шаг за шагом.
SCHEMA_VERSION = 3

_PROGRESS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS progress (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,                          -- score | hours
    subject     TEXT NOT NULL,                          -- sat | ielts | ent | math | ...
    value       REAL NOT NULL,                          -- балл или часы
    scale       REAL,                                   -- максимум шкалы балла; для часов NULL
    note        TEXT,
    occurred_on TEXT NOT NULL,                          -- ЛОКАЛЬНАЯ дата YYYY-MM-DD (см. add_progress)
    created_at  TEXT NOT NULL
)
""".strip()

_PROGRESS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_progress_kind_subject
    ON progress (kind, subject, occurred_on)
""".strip()

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT    NOT NULL,
    details      TEXT,
    due_at       TEXT,                                  -- UTC ISO8601, NULL = без срока
    category     TEXT    NOT NULL DEFAULT 'other',
    priority     TEXT    NOT NULL DEFAULT 'normal',
    status       TEXT    NOT NULL DEFAULT 'open',       -- open | completed
    recurrence   TEXT,                                  -- NULL = разовая; см. bot/recurrence.py
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

{_PROGRESS_TABLE_SQL};

{_PROGRESS_INDEX_SQL};

CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL,                           -- user | assistant
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_due   ON tasks (status, due_at);
CREATE INDEX IF NOT EXISTS idx_tasks_recurrence   ON tasks (recurrence, due_at);
CREATE INDEX IF NOT EXISTS idx_reminders_pending  ON reminders (sent_at, fire_at);
"""

# Шаги миграции: (версия, запросы). Каждый выполняется один раз и отмечается
# в PRAGMA user_version. Все запросы идемпотентны — повторный запуск после
# оборванного деплоя не должен ломать старт бота.
MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    # 1 — серии повторяющихся задач (см. bot/recurrence.py). Единственный шаг,
    #     который SCHEMA выполнить не может: колонку в существующую таблицу
    #     добавить через CREATE TABLE IF NOT EXISTS нельзя.
    (1, ("ALTER TABLE tasks ADD COLUMN recurrence TEXT",)),
    # 2 — дашборд подготовки (см. bot/progress.py).
    (2, (_PROGRESS_TABLE_SQL, _PROGRESS_INDEX_SQL)),
    # 3 — индекс под выборку серий в напоминаниях и списках.
    (3, ("CREATE INDEX IF NOT EXISTS idx_tasks_recurrence ON tasks (recurrence, due_at)",)),
)

# «ALTER TABLE ... ADD COLUMN» в SQLite не поддерживает IF NOT EXISTS —
# наличие колонки приходится проверять вручную (см. _apply_migration).
_ADD_COLUMN = re.compile(
    r"ALTER\s+TABLE\s+(?P<table>\w+)\s+ADD\s+COLUMN\s+(?P<column>\w+)", re.IGNORECASE
)


@asynccontextmanager
async def _connect() -> AsyncIterator[aiosqlite.Connection]:
    """Открыть соединение с нужными PRAGMA и row_factory."""
    async with aiosqlite.connect(_DB_PATH) as connection:
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        yield connection


async def init_db(
    path: str,
    *,
    tz: ZoneInfo | None = None,
    default_offsets: Sequence[int] | None = None,
) -> None:
    """Создать папку, привести схему к актуальной версии и включить WAL.

    Вызывается один раз при старте. И свежая база, и та, что уже месяц работает
    на сервере, приходят здесь к одному состоянию схемы.
    """
    global _DB_PATH, _TZ, _DEFAULT_OFFSETS
    _DB_PATH = path
    if tz is not None:
        _TZ = tz
    if default_offsets:
        _DEFAULT_OFFSETS = tuple(default_offsets)

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    async with aiosqlite.connect(path) as connection:
        connection.row_factory = aiosqlite.Row

        # Признак старой базы надо снять ДО применения SCHEMA: после него
        # таблица tasks существует в любом случае, и отличить новую базу
        # от работающей уже не получится.
        legacy = await _table_exists(connection, "tasks")

        # WAL включаем отдельным запросом: внутри executescript он не сохраняется.
        await connection.execute("PRAGMA journal_mode = WAL")

        if legacy:
            # Миграции идут ДО SCHEMA, и порядок здесь принципиален. SCHEMA
            # описывает актуальное состояние и создаёт индекс по колонке
            # recurrence. На старой базе такой колонки ещё нет, поэтому
            # «сначала SCHEMA, потом миграции» падает с no such column:
            # recurrence — причём падает ДО того, как миграция её добавит.
            await _migrate(connection)

        await connection.executescript(SCHEMA)

        if not legacy:
            # Свежая база получила всё из SCHEMA — миграции ей не нужны,
            # достаточно зафиксировать актуальную версию. Делаем это после
            # executescript: если он упадёт, версия не соврёт о состоянии схемы.
            await connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        await connection.commit()

    logger.info("База готова: %s (user_version = %d)", path, SCHEMA_VERSION)


async def _table_exists(connection: aiosqlite.Connection, table: str) -> bool:
    """Есть ли такая таблица в базе."""
    cursor = await connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    )
    return await cursor.fetchone() is not None


async def _has_column(connection: aiosqlite.Connection, table: str, column: str) -> bool:
    """Есть ли такая колонка в таблице (PRAGMA table_info)."""
    cursor = await connection.execute(f"PRAGMA table_info({table})")  # noqa: S608 — имя из шаблона миграции
    return any(row["name"] == column for row in await cursor.fetchall())


async def _apply_migration(connection: aiosqlite.Connection, statement: str) -> None:
    """Выполнить один запрос миграции.

    `ALTER TABLE ... ADD COLUMN` в SQLite не поддерживает IF NOT EXISTS, поэтому
    наличие колонки проверяем сами. Без этой проверки повторный запуск после
    обрыва между ALTER и записью user_version упал бы с «duplicate column name»,
    и бот не поднялся бы вообще — ровно там, где база цела.
    """
    match = _ADD_COLUMN.search(statement)
    if match and await _has_column(connection, match["table"], match["column"]):
        logger.info(
            "Шаг миграции пропущен: колонка %s.%s уже есть", match["table"], match["column"]
        )
        return
    await connection.execute(statement)


async def _migrate(connection: aiosqlite.Connection) -> None:
    """Догнать схему уже работающей базы до SCHEMA_VERSION.

    База, созданная до появления миграций, находится на user_version = 0 — и
    миграция №1 обязана выполниться: именно она добавляет колонку recurrence,
    которую новый код уже читает. Поэтому здесь НЕЛЬЗЯ «считать» дофичевую базу
    версией 1: шаг `if version <= current` просто пропустил бы её.
    """
    cursor = await connection.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    current = int(row[0])

    for version, statements in MIGRATIONS:
        if version <= current:
            continue
        for statement in statements:
            await _apply_migration(connection, statement)
        # Подстановка версии в PRAGMA, а не параметр: SQLite не принимает
        # placeholder в user_version. Значение — целая константа из кода.
        await connection.execute(f"PRAGMA user_version = {version}")
        logger.info("Схема обновлена до версии %d", version)

    if current < SCHEMA_VERSION:
        logger.info("Миграции завершены: было %d, стало %d", current, SCHEMA_VERSION)


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
        "recurrence": row["recurrence"],
        "source": row["source"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


def _normalize_recurrence(rule: Any) -> str | None:
    """Привести правило повторения к каноническому виду.

    None и пустая строка означают «задача разовая». Мусор не пропускаем:
    разбор правила — единственный источник правды в bot/recurrence.py.
    """
    if rule is None or not str(rule).strip():
        return None
    return recurrence.dump(str(rule))


def _fallback_reminder(due: datetime, now: datetime) -> tuple[int, datetime] | None:
    """Единственное напоминание для дела, срок которого уже на носу.

    Наборы смещений рассчитаны на то, что дело заводят заранее. Но «тренировка
    сегодня в 16:00», сказанная в 15:30, не попадает ни в одно из них: «за час»
    уже прошло, «за сутки» и «за неделю» тем более. Раньше в этом случае у дела
    не оставалось НИ ОДНОГО напоминания, и бот молчал — при том что модель
    отвечала «напомню». Ровно так и выглядит «он забывает напоминать».

    Здесь ставим одно напоминание на разумное время: за _FALLBACK_LEAD_MINUTES
    до срока, а если и это прошло — совсем скоро, но не позже самого срока.
    """
    if due <= now:
        return None

    fire_at = due - timedelta(minutes=_FALLBACK_LEAD_MINUTES)
    if fire_at <= now:
        fire_at = min(now + timedelta(seconds=_FALLBACK_DELAY_SECONDS), due)

    # Смещение выводим из фактического момента отправки: иначе подпись
    # напоминания («за 10 минут») разошлась бы с тем, когда оно придёт.
    return max(0, round((due - fire_at).total_seconds() / 60)), fire_at


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

    Но если после этого не осталось ни одного, а срок ещё впереди — ставим
    страховочное (см. _fallback_reminder). Дело с будущим сроком обязано иметь
    напоминание: иначе пользователь ждёт его и не получает.
    """
    await connection.execute("DELETE FROM reminders WHERE task_id = ?", (task_id,))
    if not due_iso:
        return

    due = timeutil.parse_iso(due_iso)
    now = timeutil.now_utc()

    planned = [
        (offset, due - timedelta(minutes=offset))
        for offset in offsets
        if due - timedelta(minutes=offset) > now
    ]
    if not planned:
        fallback = _fallback_reminder(due, now)
        if fallback is None:  # срок уже прошёл — напоминать поздно
            return
        planned = [fallback]

    for offset, fire_at in planned:
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
    recurrence: str | None = None,
) -> dict[str, Any]:
    """Создать задачу и напоминания к ней. Возвращает задачу с напоминаниями.

    `recurrence` — правило повторения в формате bot/recurrence.py ('daily'
    или 'weekly:1,3'). Требует срока: серию не от чего отсчитывать.
    """
    rule = _normalize_recurrence(recurrence)
    if rule and due_at is None:
        raise ValueError(
            "Повторяющаяся задача должна иметь срок — иначе непонятно, от какой даты "
            "считать следующий раз."
        )

    due_iso = due_at.astimezone(timezone.utc).isoformat() if due_at else None

    async with _connect() as connection:
        cursor = await connection.execute(
            """
            INSERT INTO tasks
                (title, details, due_at, category, priority, status, recurrence, source, created_at)
            VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?)
            """,
            (
                title.strip(),
                details,
                due_iso,
                category,
                priority,
                rule,
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
    completed_between: tuple[datetime, datetime] | None = None,
    category: str | None = None,
    query: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Выборка задач с фильтрами.

    status : "open" | "completed" | "any"
    start/end : границы по сроку в UTC (задачи без срока при этом не попадают)
    completed_between : границы по МОМЕНТУ закрытия, а не по сроку. Нужны там,
        где вопрос звучит как «что сделано сегодня/за неделю»: задача, закрытая
        сегодня, могла иметь срок на следующий месяц, и фильтр по сроку её
        бы потерял.
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

    if completed_between is not None:
        done_start, done_end = completed_between
        sql.append("AND completed_at IS NOT NULL AND completed_at >= ? AND completed_at < ?")
        params.extend([
            done_start.astimezone(timezone.utc).isoformat(),
            done_end.astimezone(timezone.utc).isoformat(),
        ])

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

    Для `recurrence` явный None — это «сделать задачу разовой», а не «не менять»
    (см. _NULLABLE_FIELDS).
    """
    assignments: list[str] = []
    params: list[Any] = []

    for field, value in changes.items():
        if field not in _UPDATABLE_FIELDS:
            continue
        if value is None and field not in _NULLABLE_FIELDS:
            continue  # None у текстового поля = «аргумент не передан»
        if field == "recurrence":
            value = _normalize_recurrence(value)
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

        # Повтор без срока бессмыслен: непонятно, от какой даты считать серию.
        # Проверяем итоговое состояние строки ДО commit: исключение закроет
        # соединение без фиксации, и SQLite откатит правки целиком.
        cursor = await connection.execute(
            "SELECT due_at, recurrence FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        if row is not None and row["recurrence"] and not row["due_at"]:
            raise ValueError(
                "Повторяющаяся задача должна иметь срок: сначала назначь дату через update_task."
            )

        await connection.commit()

    return await get_task(task_id)


async def complete_task(task_id: int) -> dict[str, Any] | None:
    """Отметить задачу выполненной. Для серии — создать следующую.

    Ссылка на следующую задачу серии возвращается в ключе ``next_task``
    (None, если задача разовая или уже была закрыта).

    Почему новая задача, а не переиспользование id: на id висят напоминания
    (уникальный индекс по task_id + offset_minutes), completed_at и ссылки
    в истории — переиспользование сломало бы всё разом.
    """
    before = await get_task(task_id)
    if before is None:
        return None

    if before["status"] != "open":
        # Уже закрыта. Без этой проверки двойной тап по кнопке «выполнено»
        # породил бы две копии серии.
        before["next_task"] = None
        return before

    task = await update_task(task_id, {"status": "completed"})
    if task is None:
        return None

    task["next_task"] = await _roll_recurrence(before)
    return task


async def _roll_recurrence(before: dict[str, Any]) -> dict[str, Any] | None:
    """Создать следующую задачу серии по правилу повторения закрытой задачи.

    `before` — снимок задачи ДО закрытия: у закрытой уже удалены неотправленные
    напоминания, а набор смещений нужно перенести в новую серию.
    """
    rule = before.get("recurrence")
    if not rule or not before.get("due_at"):
        return None

    # Следующее вхождение обязано быть в будущем. Закрыть серию можно и с
    # опозданием, и тогда «срок + один период» уже прошёл: раньше в этом случае
    # создавалось дело с прошедшей датой, у которого не оставалось ни одного
    # напоминания (все сроки срабатывания позади), — и на этом серия застревала
    # навсегда, потому что следующее закрытие повторяло ту же ошибку. «Каждый
    # день английский» достаточно было отметить на следующее утро после срока.
    # Пропущенные вхождения именно пропускаем, а не догоняем задним числом.
    next_due = timeutil.parse_iso(before["due_at"])
    now = timeutil.now_utc()
    try:
        for _ in range(_MAX_ROLL_STEPS):
            next_due = recurrence.next_due(next_due, rule, _TZ)
            if next_due > now:
                break
        else:
            logger.warning(
                "Серия задачи #%s (%s) не догнала сегодня за %d шагов — не переношу",
                before["id"], rule, _MAX_ROLL_STEPS,
            )
            return None
    except ValueError:
        logger.warning("Не удалось перенести серию задачи #%s: правило %r", before["id"], rule)
        return None

    # Смещения напоминаний переносим с исходной задачи. Но доживший до закрытия
    # набор — это ещё не то, что просил пользователь: при создании серии «за
    # сутки» и «за неделю» могли не поместиться (первое вхождение было слишком
    # близко) и отсеяться, и тогда серия навсегда оставалась с одним «за час»,
    # хотя следующее вхождение может быть и через четыре дня. Если перенесённые
    # смещения целиком укладываются в набор по умолчанию, значит серия и была
    # заведена на нём, — берём его целиком и даём отсеяться заново по новому
    # сроку. Набор, где есть что-то своё, переносим как есть.
    carried = [int(item["offset_minutes"]) for item in (before.get("reminders") or [])]
    if not carried or set(carried) <= set(_DEFAULT_OFFSETS):
        offsets = list(_DEFAULT_OFFSETS)
    else:
        offsets = carried

    try:
        return await create_task(
            title=before["title"],
            due_at=next_due,
            category=before["category"],
            priority=before["priority"],
            details=before.get("details"),
            offsets=offsets,
            source=before.get("source") or "chat",
            recurrence=rule,
        )
    except ValueError:
        logger.exception("Не удалось создать следующую задачу серии #%s", before["id"])
        return None


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
        to_send   — список к отправке. У каждого элемента есть флаг ``late``:
                    True — напоминание пролежало дольше grace-окна, но срок
                    задачи ещё не прошёл, и напоминание сохраняет смысл
                    («пробник завтра» полезно услышать и с опозданием).
        stale_ids — безнадёжно устаревшие: и напоминание просрочено, и срок уже
                    прошёл. Помечаем отправленными молча, чтобы после долгого
                    простоя не прилетела пачка старых.

    Раньше в stale уходило ВСЁ, что старше grace, — и после ночного падения
    сервера терялись напоминания о делах, до которых ещё оставалось время.
    Пользователь видел не «сервер лежал», а «бот опять забыл».
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

    on_time: list[dict[str, Any]] = []
    late: list[dict[str, Any]] = []
    stale_ids: list[int] = []

    for row in rows:
        if row["fire_at"] >= floor_iso:
            row["late"] = False
            on_time.append(row)
            continue

        # Напоминание сильно просрочено — но живо ли само дело?
        due_at = row["due_at"]
        if due_at and timeutil.parse_iso(due_at) > now:
            row["late"] = True
            late.append(row)
        else:
            stale_ids.append(row["reminder_id"])

    if len(late) > _MAX_LATE_PER_TICK:
        # Первыми идут самые горящие: при равном опоздании важнее тот, у кого
        # срок ближе. Остальные останутся неотправленными и уйдут следующим
        # тиком — по пять в минуту, а не простынёй на весь экран.
        late.sort(key=lambda row: row["due_at"] or "")
        late = late[:_MAX_LATE_PER_TICK]

    return on_time + late, stale_ids


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
#  Прогресс подготовки (баллы пробников и часы занятий)
# --------------------------------------------------------------------------- #

async def add_progress(
    *,
    kind: str,
    subject: str,
    value: float,
    scale: float | None = None,
    note: str | None = None,
    occurred_on: str | None = None,
) -> dict[str, Any]:
    """Записать балл пробника или часы подготовки.

    `occurred_on` — ЛОКАЛЬНАЯ дата пользователя (YYYY-MM-DD), и это единственное
    место в проекте, где нарушено правило «в базе всё в UTC». Причина: аналитика
    привязана к дню пользователя. Вечерний чек-ин в 23:30 по Алматы — это ещё
    сегодняшний день, а по UTC уже завтрашний, и запись уехала бы не в тот день.
    """
    if kind not in progress.KINDS:
        raise ValueError(f"Неизвестный вид записи: {kind!r}. Ожидается score или hours.")

    if occurred_on and str(occurred_on).strip():
        try:
            occurred_on = date.fromisoformat(str(occurred_on).strip()).isoformat()
        except ValueError as exc:
            raise ValueError(
                f"Не удалось разобрать дату {occurred_on!r} (нужен формат YYYY-MM-DD)."
            ) from exc
    else:
        occurred_on = datetime.now(_TZ).date().isoformat()

    created_at = timeutil.now_utc().isoformat()
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            INSERT INTO progress (kind, subject, value, scale, note, occurred_on, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (kind, subject, float(value), scale, note, occurred_on, created_at),
        )
        await connection.commit()
        entry_id = int(cursor.lastrowid or 0)

    return {
        "id": entry_id,
        "kind": kind,
        "subject": subject,
        "value": float(value),
        "scale": scale,
        "note": note,
        "occurred_on": occurred_on,
        "created_at": created_at,
    }


async def list_progress(
    *,
    kind: str | None = None,
    subject: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Записи прогресса, свежие сверху.

    Окна по датам здесь намеренно нет: баллы и часы считаются по разным
    периодам (часы — за последнюю неделю, балл сравнивается с предыдущим
    пробником хоть за прошлый месяц). Фильтровать по дате в запросе значило бы
    выкинуть предыдущий результат и вместе с ним всю динамику — поэтому окно
    применяет progress.summarize, и делает это осознанно.
    """
    sql = ["SELECT * FROM progress WHERE 1 = 1"]
    params: list[Any] = []

    if kind:
        sql.append("AND kind = ?")
        params.append(kind)
    if subject:
        sql.append("AND subject = ?")
        params.append(subject)

    sql.append("ORDER BY occurred_on DESC, id DESC LIMIT ?")
    params.append(limit)

    async with _connect() as connection:
        cursor = await connection.execute(" ".join(sql), params)
        return [dict(row) for row in await cursor.fetchall()]


async def delete_progress(entry_id: int) -> bool:
    """Удалить запись прогресса (ошибочный балл или часы)."""
    async with _connect() as connection:
        cursor = await connection.execute("DELETE FROM progress WHERE id = ?", (entry_id,))
        await connection.commit()
        return cursor.rowcount > 0


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

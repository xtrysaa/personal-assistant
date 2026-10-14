"""Быстрая проверка логики без Telegram и без сети.

Что покрывает: парсинг дат, работу с SQLite, выборки, движок напоминаний,
инструменты LLM (add/list/update/complete/delete) и рендер сообщений.

Запуск из корня проекта:
    python scripts/smoke_test.py
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

# Скрипт лежит в scripts/, а пакет bot — в корне проекта.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Изолированная тестовая конфигурация (ставим ДО импорта bot.config).
_TMP_DB = Path(tempfile.mkdtemp(prefix="assistant-smoke-")) / "test.db"
os.environ.update(
    BOT_TOKEN="123456:TEST",
    OWNER_TELEGRAM_ID="1",
    OPENAI_API_KEY="test-key",
    OPENAI_BASE_URL="https://openrouter.ai/api/v1",
    OPENAI_MODEL="deepseek/deepseek-v4.1-flash",
    TZ="Asia/Almaty",
    DB_PATH=str(_TMP_DB),
    DEFAULT_REMIND_OFFSETS="10080,1440,60",
)

from bot import calendar_export, db, formatting, llm, progress, recurrence, timeutil  # noqa: E402
from bot.config import get_settings  # noqa: E402

# Схема ДО появления новых фич: колонки recurrence и таблицы progress ещё нет.
# Нужна, чтобы проверить миграции на копии боевой базы, а не на свежей —
# на свежей базе ошибка в порядке применения миграций не проявляется.
LEGACY_SCHEMA = """
CREATE TABLE tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT    NOT NULL,
    details      TEXT,
    due_at       TEXT,
    category     TEXT    NOT NULL DEFAULT 'other',
    priority     TEXT    NOT NULL DEFAULT 'normal',
    status       TEXT    NOT NULL DEFAULT 'open',
    source       TEXT    NOT NULL DEFAULT 'chat',
    created_at   TEXT    NOT NULL,
    completed_at TEXT
);

CREATE TABLE reminders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    offset_minutes INTEGER NOT NULL,
    fire_at        TEXT    NOT NULL,
    sent_at        TEXT
);

CREATE UNIQUE INDEX idx_reminders_task_offset ON reminders (task_id, offset_minutes);

CREATE TABLE history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_tasks_status_due  ON tasks (status, due_at);
CREATE INDEX idx_reminders_pending ON reminders (sent_at, fire_at);
"""

# Windows-консоль по умолчанию cp1251 — иначе кириллица выглядит кракозябрами.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}{'' if condition else f'  -> {detail}'}")
    if not condition:
        FAILURES.append(label)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def iso_local(moment: datetime, tz) -> str:
    """Локальное время пользователя в формате, который просим у модели."""
    return moment.astimezone(tz).strftime("%Y-%m-%dT%H:%M:%S")


def _raises(call) -> bool:
    """True, если вызов упал ожидаемой ошибкой валидации."""
    try:
        call()
    except (ValueError, TypeError):
        return True
    return False


async def main() -> None:
    settings = get_settings()
    tz = settings.tz
    now = timeutil.now_utc()
    await db.init_db(settings.db_path)

    # ------------------------------------------------------------------ #
    section("1. Парсинг дат")
    check("дата без времени -> 23:59 локального дня",
          timeutil.parse_local("2026-11-15", tz).astimezone(tz).strftime("%H:%M") == "23:59")
    check("граница периода -> локальная полночь",
          timeutil.parse_local("2026-11-15", tz, default_time=time.min)
          .astimezone(tz).strftime("%H:%M") == "00:00")
    check("пробел вместо T",
          timeutil.parse_local("2026-11-15 15:00", tz).astimezone(tz).strftime("%H:%M") == "15:00")
    check("смещение +05:00 учитывается",
          timeutil.parse_local("2026-11-15T15:00:00+05:00", tz).isoformat().startswith("2026-11-15T10:00"))
    check("месяц, а не 2 дня", timeutil.human_left((now + timedelta(days=3, hours=1)).isoformat(), tz) == "через 3 дня",
          timeutil.human_left((now + timedelta(days=3, hours=1)).isoformat(), tz))
    check("часы", timeutil.human_left((now + timedelta(hours=5, minutes=1)).isoformat(), tz) == "через 5 часов",
          timeutil.human_left((now + timedelta(hours=5, minutes=1)).isoformat(), tz))
    check("просрочка", "просрочено на 2 часа" == timeutil.human_left((now - timedelta(hours=2)).isoformat(), tz))

    # ------------------------------------------------------------------ #
    section("2. Создание задач через инструменты LLM")
    exam = await llm.execute_tool("add_task", {
        "title": "Контрольная по физике",
        "due_at_local": iso_local(now + timedelta(hours=2), tz),
        "category": "exams",
        "priority": "high",
    })
    check("add_task вернул ok", exam.get("ok") is True, str(exam))
    exam_id = exam["task"]["id"]
    # Срок через 2 часа: «за сутки» уже в прошлом, остаётся только «за час».
    check("срок через 2 часа -> одно напоминание",
          exam["task"]["reminders_planned"] == ["за час"], str(exam["task"]["reminders_planned"]))

    flight = await llm.execute_tool("add_task", {
        "title": "Air Astana Ab Initio — дедлайн документов",
        "due_at_local": iso_local(now + timedelta(days=40), tz),
        "category": "flight",
        "priority": "critical",
        "details": "Паспорт, медкомиссия, аттестат",
    })
    check("далёкий дедлайн -> три напоминания",
          flight["task"]["reminders_planned"] == ["за неделю", "за сутки", "за час"],
          str(flight["task"]["reminders_planned"]))

    run = await llm.execute_tool("add_task", {
        "title": "Утренняя пробежка",
        "due_at_local": iso_local(now + timedelta(hours=3), tz),
        "category": "sport",
        "priority": "normal",
    })
    run_id = run["task"]["id"]
    check("спортивная задача создана", run.get("ok") is True, str(run))

    someday = await llm.execute_tool("add_task", {
        "title": "Идеи для проекта Умай",
        "due_at_local": None,
        "category": "other",
        "priority": "low",
    })
    check("задача без срока сохраняется", someday["task"]["due_local"] == "без срока")

    past = await llm.execute_tool("add_task", {
        "title": "Прошлое событие",
        "due_at_local": "2020-01-01T10:00:00",
        "category": "other",
        "priority": "low",
    })
    check("прошлая дата отклонена", past.get("ok") is False, str(past))

    # ------------------------------------------------------------------ #
    section("3. Выборки")
    today_label = datetime.now(tz).strftime("%Y-%m-%d")
    tomorrow_label = (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%d")
    today_tasks = await llm.execute_tool("list_tasks", {"from_local": today_label, "to_local": tomorrow_label})
    titles = {task["title"] for task in today_tasks["tasks"]}
    check("в сутки попадают только задачи со сроком в них",
          titles <= {"Контрольная по физике", "Утренняя пробежка"}, str(titles))
    check("далёкий дедлайн не попал в сегодня", "Air Astana Ab Initio — дедлайн документов" not in titles)

    upcoming = await llm.execute_tool("list_tasks", {"status": "open", "limit": 10})
    check("открытых задач 4", upcoming["count"] == 4, str(upcoming["count"]))

    by_query = await llm.execute_tool("list_tasks", {"query": "Air Astana"})
    check("поиск по названию", by_query["count"] == 1, str(by_query["count"]))

    by_category = await llm.execute_tool("list_tasks", {"category": "flight"})
    check("фильтр по категории", by_category["count"] == 1, str(by_category["count"]))

    check("сортировка по сроку",
          [task["id"] for task in upcoming["tasks"]] == [exam_id, run_id, flight["task"]["id"], someday["task"]["id"]],
          str([task["id"] for task in upcoming["tasks"]]))

    # ------------------------------------------------------------------ #
    section("4. Движок напоминаний")
    fresh, stale = await db.due_reminders(now, timedelta(hours=6))
    check("сейчас отправлять нечего", fresh == [] and stale == [], f"{fresh} {stale}")

    # Напоминание «за час» для задачи со сроком через 3 часа сработает через 2 часа.
    fire_at = now + timedelta(hours=2)
    fresh, stale = await db.due_reminders(fire_at + timedelta(seconds=5), timedelta(hours=6))
    check("наступили напоминания двух задач", len(fresh) == 2, str(len(fresh)))
    check("порядок по времени срабатывания",
          [item["task_id"] for item in fresh] == [exam_id, run_id],
          str([item["task_id"] for item in fresh]))

    reminder_text = formatting.render_reminder(fresh[0], tz) if fresh else ""
    check("текст напоминания отрендерен",
          "Напоминание за час" in reminder_text and "Контрольная по физике" in reminder_text,
          reminder_text)

    await db.mark_reminders([item["reminder_id"] for item in fresh], sent=True)
    again, _ = await db.due_reminders(fire_at + timedelta(seconds=10), timedelta(hours=6))
    check("повторно не отправляются", again == [], str(again))

    late, dropped = await db.due_reminders(fire_at + timedelta(days=2), timedelta(hours=6))
    check("устаревшие не отправляются, а помечаются",
          late == [] and dropped == [], f"late={late} dropped={dropped}")

    # Отдельная задача, чтобы проверить именно уход в stale.
    stale_task = await llm.execute_tool("add_task", {
        "title": "Зарядка",
        "due_at_local": iso_local(now + timedelta(hours=2), tz),
        "category": "health",
        "priority": "low",
    })
    pending, _ = await db.due_reminders(now + timedelta(hours=1, minutes=5), timedelta(hours=6))
    check("напоминание новой задачи подхвачено", len(pending) == 1, str(pending))
    late, dropped = await db.due_reminders(now + timedelta(days=1), timedelta(hours=6))
    check("просроченное уходит в stale, а не в отправку",
          late == [] and len(dropped) == 1 and dropped[0] == pending[0]["reminder_id"],
          f"late={late} dropped={dropped}")
    await db.delete_task(stale_task["task"]["id"])

    # ------------------------------------------------------------------ #
    section("5. Изменение и удаление")
    moved = await llm.execute_tool("update_task", {
        "task_id": exam_id,
        "new_due_at_local": iso_local(now + timedelta(days=10), tz),
        "new_priority": "critical",
    })
    check("перенос срока + новая важность",
          moved["ok"] and moved["task"]["priority"] == "critical", str(moved))
    check("напоминания пересчитаны под новый срок",
          moved["task"]["reminders_planned"] == ["за неделю", "за сутки", "за час"],
          str(moved["task"]["reminders_planned"]))

    done = await llm.execute_tool("complete_task", {"query": "Контрольная"})
    check("закрытие по части названия", done["ok"] and done["task"]["status"] == "completed", str(done))

    closed = await db.get_task(exam_id)
    check("напоминания закрытой задачи сняты",
          all(item["sent_at"] for item in closed["reminders"]), str(closed["reminders"]))

    check("в открытых осталось 3",
          len(await db.list_tasks(status="open", limit=10)) == 3,
          str([task["title"] for task in await db.list_tasks(status="open", limit=10)]))

    removed = await llm.execute_tool("delete_task", {"task_id": run_id})
    check("удаление", removed["ok"] is True and await db.get_task(run_id) is None)

    missing = await llm.execute_tool("complete_task", {"task_id": 999})
    check("ошибка по несуществующей задаче", missing.get("ok") is False, str(missing))

    ambiguous = await llm.execute_tool("complete_task", {"query": "е"})
    check("неоднозначный запрос -> просьба уточнить",
          ambiguous.get("ok") is False and "несколько" in ambiguous.get("error", ""), str(ambiguous))

    # ------------------------------------------------------------------ #
    section("6. Рендер и HTML")
    remaining = await db.list_tasks(status="open", limit=10)
    rendered = formatting.task_list(remaining, tz)
    check("список отрендерен", "Air Astana" in rendered and f"#{flight['task']['id']}" in rendered)
    flight_row = await db.get_task(flight["task"]["id"])
    check("карточка содержит детали и напоминания",
          "медкомиссия" in formatting.task_card(flight_row, tz))
    check("экранирование тегов",
          formatting.sanitize_html("<b>жирный</b> и 5 < 7 & <script>")
          == "<b>жирный</b> и 5 &lt; 7 &amp; &lt;script&gt;",
          formatting.sanitize_html("<b>жирный</b> и 5 < 7 & <script>"))

    # ------------------------------------------------------------------ #
    section("7. История диалога")
    await db.add_message("user", "привет")
    await db.add_message("assistant", "здравствуй")
    history = await db.get_history(limit=5)
    check("история в хронологическом порядке",
          [item["role"] for item in history] == ["user", "assistant"], str(history))
    await db.clear_history()
    check("сброс истории", await db.get_history(limit=5) == [])

    # ------------------------------------------------------------------ #
    section("8. Подготовка: баллы и часы")
    score = await llm.execute_tool("log_score", {"subject": "sat", "value": 640})
    check("балл пробника записан", score.get("ok") is True, str(score))
    check("шкала SAT подставлена автоматически",
          score["entry"]["scale"] == 1600, str(score["entry"]))

    synonym = await llm.execute_tool("log_score", {"subject": "математика", "value": "680"})
    check("синоним «математика» и строка вместо числа",
          synonym["entry"]["subject"] == "Математика" and synonym["entry"]["value"] == 680.0,
          str(synonym["entry"]))

    grown = await llm.execute_tool("log_score", {"subject": "sat", "value": 700})
    check("второй балл по предмету принят", grown.get("ok") is True, str(grown))

    too_big = await llm.execute_tool("log_score", {"subject": "sat", "value": 2000})
    check("балл выше шкалы отклонён",
          too_big.get("ok") is False and "максимума" in too_big.get("error", ""), str(too_big))

    unknown = await llm.execute_tool("log_score", {"subject": "физра", "value": 100})
    check("незнакомый предмет отклонён",
          unknown.get("ok") is False and "Неизвестный предмет" in unknown.get("error", ""),
          str(unknown))

    hours = await llm.execute_tool("log_hours", {"subject": "math", "hours": 2.5})
    check("часы записаны", hours.get("ok") is True and hours["entry"]["display"] == "2.5 часа",
          str(hours))

    await llm.execute_tool("log_hours", {"subject": "english", "hours": 1})
    long_ago = (datetime.now(tz) - timedelta(days=40)).strftime("%Y-%m-%d")
    await llm.execute_tool("log_hours", {"subject": "math", "hours": 3, "occurred_on_local": long_ago})

    impossible = await llm.execute_tool("log_hours", {"subject": "math", "hours": 26})
    check("26 часов за раз отклонены",
          impossible.get("ok") is False and "24 часа" in impossible.get("error", ""), str(impossible))

    report = await llm.execute_tool("get_progress", {})
    check("часы за окно посчитаны, старое не попало",
          report["hours_total"] == 3.5, str(report["hours_total"]))
    check("динамика SAT +60 к прошлому",
          report["scores"]["sat"]["delta"] == 60.0, str(report["scores"]["sat"]))

    text = progress.build_report(
        await db.list_progress(),
        since=(datetime.now(tz) - timedelta(days=6)).strftime("%Y-%m-%d"),
        until=datetime.now(tz).strftime("%Y-%m-%d"),
    )
    check("отчёт содержит полосу и динамику",
          "█" in text and "+60 к прошлому" in text and "2.5 часа" in text, text)

    # ------------------------------------------------------------------ #
    section("9. Повторяющиеся задачи")
    check("разбор правила", recurrence.parse("weekly:1,3") == ("weekly", (1, 3)))
    check("канонизация порядка дней", recurrence.dump("weekly:3,1") == "weekly:1,3")
    check("полная неделя сводится к daily", recurrence.dump("weekly:0,1,2,3,4,5,6") == "daily")
    check("подпись нескольких дней",
          recurrence.label("weekly:1,3") == "по вторникам и четвергам",
          recurrence.label("weekly:1,3"))
    check("подпись одного дня", recurrence.label("weekly:2") == "каждую среду",
          recurrence.label("weekly:2"))
    check("мусор в правиле — ошибка", _raises(lambda: recurrence.parse("каждый вторник")))

    # Среда 16:00 по Алматы: следующий вторник — уже через границу недели.
    wednesday = datetime(2026, 10, 14, 16, 0, tzinfo=tz).astimezone(timezone.utc)
    next_one = recurrence.next_due(wednesday, "weekly:1,3", tz).astimezone(tz)
    check("через границу недели: ср -> чт той же недели",
          next_one.strftime("%a %H:%M") == "Thu 16:00", str(next_one))
    thursday = datetime(2026, 10, 15, 16, 0, tzinfo=tz).astimezone(timezone.utc)
    after = recurrence.next_due(thursday, "weekly:1,3", tz).astimezone(tz)
    check("с чт следующий — вторник новой недели",
          after.strftime("%a %H:%M") == "Tue 16:00" and after.date().day == 20, str(after))
    check("локальное время суток сохраняется",
          recurrence.next_due(wednesday, "daily", tz).astimezone(tz).strftime("%H:%M") == "16:00")

    series = await llm.execute_tool("add_task", {
        "title": "Тренировка",
        "due_at_local": iso_local(now + timedelta(days=1), tz),
        "category": "sport",
        "priority": "normal",
        "recurrence": "weekly:1,3",
    })
    check("серия создана", series.get("ok") is True, str(series))
    series_id = series["task"]["id"]
    check("правило сохранено канонично",
          (await db.get_task(series_id))["recurrence"] == "weekly:1,3")
    check("галочка повтора в строке списка",
          "🔁" in formatting.task_line(await db.get_task(series_id), tz))

    no_due = await llm.execute_tool("add_task", {
        "title": "Серия без срока", "recurrence": "daily", "due_at_local": None,
    })
    check("повтор без срока отклонён",
          no_due.get("ok") is False and "срок" in no_due.get("error", "").lower(), str(no_due))

    bad_rule = await llm.execute_tool("add_task", {
        "title": "Серия с мусором", "recurrence": "weekly:9",
        "due_at_local": iso_local(now + timedelta(days=1), tz),
    })
    check("нераспознанное правило отклонено", bad_rule.get("ok") is False, str(bad_rule))

    open_before = len(await db.list_tasks(status="open", limit=50))
    closed = await db.complete_task(series_id)
    shifted = closed["next_task"]
    check("серия продолжилась новой задачей",
          shifted is not None and shifted["id"] != series_id and shifted["recurrence"] == "weekly:1,3",
          str(shifted))
    check("следующая серия встала на будний день правила",
          timeutil.parse_iso(shifted["due_at"]).astimezone(tz).weekday() in (1, 3),
          str(shifted["due_at"]))
    check("в открытых задач столько же (серия не размножилась)",
          len(await db.list_tasks(status="open", limit=50)) == open_before, "количество изменилось")

    again_closed = await db.complete_task(series_id)
    check("повторное закрытие не плодит копии", again_closed["next_task"] is None, str(again_closed))

    detached = await llm.execute_tool("update_task", {
        "task_id": shifted["id"], "new_recurrence": "нет",
    })
    check("серию можно отключить",
          detached["ok"] and (await db.get_task(shifted["id"]))["recurrence"] is None, str(detached))

    # ------------------------------------------------------------------ #
    section("10. Изображения")
    plain_content = llm._build_user_content("привет", ())
    check("без картинок контент остаётся строкой",
          isinstance(plain_content, str) and plain_content == "привет", str(plain_content))

    with_image = llm._build_user_content("разбери расписание", [("image/jpeg", b"\xff\xd8fake")])
    check("с картинкой контент — список частей", isinstance(with_image, list) and len(with_image) == 2,
          str(with_image))
    check("текст идёт первым",
          with_image[0]["type"] == "text" and with_image[1]["type"] == "image_url", str(with_image))
    check("картинка в формате data URL",
          with_image[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"),
          str(with_image[1]))

    no_text = llm._build_user_content("", [("image/jpeg", b"\xff\xd8fake")])
    check("подпись необязательна", no_text[0]["text"], str(no_text))

    oversized = b"0" * (llm.IMAGE_MAX_BYTES + 1)
    check("слишком большое изображение — ошибка",
          _raises(lambda: llm._build_user_content("x", [("image/jpeg", oversized)])))

    # ------------------------------------------------------------------ #
    section("11. Экспорт в календарь")
    ics_tasks = [await db.get_task(flight["task"]["id"])]
    ics_tasks[0]["title"] = "Дедлайн, Air Astana; документы"
    ics_tasks[0]["details"] = "Паспорт\nмедкомиссия"
    ics = calendar_export.build_ics(ics_tasks, tz)

    check("переводы строк только CRLF", "\n" not in ics.replace("\r\n", ""), repr(ics[:80]))
    check("файл заканчивается CRLF", ics.endswith("\r\n"))
    check("BEGIN/END:VEVENT сбалансированы",
          ics.count("BEGIN:VEVENT") == ics.count("END:VEVENT") == 1, str(ics.count("BEGIN:VEVENT")))
    check("начинается с BEGIN:VCALENDAR", ics.startswith("BEGIN:VCALENDAR\r\n"))
    check("запятая и точка с запятой экранированы",
          "SUMMARY:Дедлайн\\, Air Astana\\; документы" in ics, ics)
    check("перевод строки в описании экранирован", "\\n" in ics, ics)
    check("UID привязан к задаче",
          f"UID:task-{ics_tasks[0]['id']}@{calendar_export.UID_DOMAIN}" in ics, ics)
    check("нет строк длиннее 75 октетов",
          all(len(line.encode("utf-8")) <= calendar_export.LINE_LIMIT
              for line in ics.split("\r\n")),
          str(max(len(line.encode("utf-8")) for line in ics.split("\r\n"))))
    check("UID стабилен между выгрузками",
          calendar_export.build_ics(ics_tasks, tz) == ics)

    dateless = [await db.get_task(someday["task"]["id"])]
    check("задачи без срока пропускаются",
          "BEGIN:VEVENT" not in calendar_export.build_ics(dateless, tz))
    check("пустой список даёт пустой календарь",
          "BEGIN:VCALENDAR" in calendar_export.build_ics([], tz))

    # ------------------------------------------------------------------ #
    section("12. Конфигурация и вечерний чек-ин")
    check("вечерний чек-ин включён по умолчанию", settings.evening == (21, 30), str(settings.evening))

    os.environ["EVENING_CHECKIN_TIME"] = ""
    get_settings.cache_clear()
    check("пустое значение выключает чек-ин", get_settings().evening is None)
    os.environ["EVENING_CHECKIN_TIME"] = "22:15"
    get_settings.cache_clear()
    check("время чек-ина читается из окружения", get_settings().evening == (22, 15))
    del os.environ["EVENING_CHECKIN_TIME"]
    get_settings.cache_clear()
    check("без переменной снова 21:30", get_settings().evening == (21, 30))

    check("сообщение чек-ина в общем стиле",
          formatting.DIVIDER in formatting.evening_checkin(dateless, [], tz)
          and f"{formatting.BULLET}Осталось" in formatting.evening_checkin(dateless, [], tz))
    plain_checkin = formatting.evening_checkin(dateless, [], tz, plain=True)
    check("в историю чек-ин уходит без HTML",
          "<b>" not in plain_checkin and "<i>" not in plain_checkin, plain_checkin)
    check("в историю попадает тот же вопрос",
          "Итог дня" in plain_checkin and "Что успели сегодня?" in plain_checkin, plain_checkin)

    # ------------------------------------------------------------------ #
    #  Последней: init_db меняет глобальный _DB_PATH, после неё возвращаем его.
    section("13. Миграция старой базы")
    legacy_path = Path(tempfile.mkdtemp(prefix="assistant-legacy-")) / "old.db"
    with sqlite3.connect(legacy_path) as connection:
        connection.executescript(LEGACY_SCHEMA)
        connection.execute(
            "INSERT INTO tasks (title, due_at, category, priority, status, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Старая задача", now.isoformat(), "study", "high", "open", "chat", now.isoformat()),
        )
        connection.execute(
            "INSERT INTO history (role, content, created_at) VALUES (?, ?, ?)",
            ("user", "старое сообщение", now.isoformat()),
        )

    await db.init_db(str(legacy_path), tz=tz, default_offsets=settings.default_offsets)

    with sqlite3.connect(legacy_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        indexes = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )}
        rows = connection.execute("SELECT title, category, priority FROM tasks").fetchall()
        history_rows = connection.execute("SELECT content FROM history").fetchall()

    check(f"версия схемы поднялась до {db.SCHEMA_VERSION}", version == db.SCHEMA_VERSION, str(version))
    check("колонка recurrence появилась", "recurrence" in columns, str(columns))
    check("таблица progress создана", "progress" in tables, str(tables))
    check("индекс по сериям создан", "idx_tasks_recurrence" in indexes, str(indexes))
    check("данные задач целы",
          rows == [("Старая задача", "study", "high")], str(rows))
    check("история диалога цела",
          history_rows == [("старое сообщение",)], str(history_rows))

    # Повторный запуск — обычная ситуация после оборванного деплоя.
    await db.init_db(str(legacy_path), tz=tz, default_offsets=settings.default_offsets)
    with sqlite3.connect(legacy_path) as connection:
        check("повторная миграция идемпотентна",
              connection.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION)

    # Возвращаем рабочую базу: дальше тесты её больше не трогают, но глобальная
    # переменная не должна остаться указывать на временный файл.
    await db.init_db(str(_TMP_DB), tz=tz, default_offsets=settings.default_offsets)

    # ------------------------------------------------------------------ #
    print("\n" + "=" * 46)
    if FAILURES:
        print(f"❌ Провалено проверок: {len(FAILURES)}")
        for name in FAILURES:
            print(f"   - {name}")
        raise SystemExit(1)
    print("✅ Все проверки пройдены")


if __name__ == "__main__":
    asyncio.run(main())

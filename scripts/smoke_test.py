"""Быстрая проверка логики без Telegram и без сети.

Что покрывает: парсинг дат, работу с SQLite, выборки, движок напоминаний,
инструменты LLM (add/list/update/complete/delete) и рендер сообщений.

Запуск из корня проекта:
    python scripts/smoke_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, time, timedelta
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

from bot import db, formatting, llm, timeutil  # noqa: E402
from bot.config import get_settings  # noqa: E402

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
    print("\n" + "=" * 46)
    if FAILURES:
        print(f"❌ Провалено проверок: {len(FAILURES)}")
        for name in FAILURES:
            print(f"   - {name}")
        raise SystemExit(1)
    print("✅ Все проверки пройдены")


if __name__ == "__main__":
    asyncio.run(main())

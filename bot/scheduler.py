"""Фоновые задачи: проактивные напоминания, утренний дайджест, недельный обзор.

Как устроены напоминания (важно для надёжности на бесплатном хостинге):
напоминания не живут в памяти планировщика, а хранятся строками в таблице
`reminders`. Раз в минуту задача `_tick` забирает из базы те напоминания,
чей срок уже наступил, отправляет их и помечает отправленными.

Поэтому бот переживает перезапуск, деплой и падение: ничего не теряется,
и нет дублей — отправка помечается в базе сразу после ответа Telegram.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import db, formatting, timeutil
from .config import Settings
from .llm import REMINDER_GRACE

logger = logging.getLogger(__name__)


class AssistantScheduler:
    """Обёртка над APScheduler: запуск, остановка и сами задания."""

    def __init__(self, bot: Bot, settings: Settings) -> None:
        self.bot = bot
        self.settings = settings
        self.scheduler = AsyncIOScheduler(timezone=settings.tz)

    # ------------------------------------------------------------------ #
    #  Жизненный цикл
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Зарегистрировать задания и запустить планировщик."""
        self.scheduler.add_job(
            self._tick_reminders,
            IntervalTrigger(seconds=60),
            id="reminders",
            max_instances=1,   # не допускаем наложения, если отправка подвисла
            coalesce=True,     # пропущенные запуски схлопываем в один
            misfire_grace_time=30,
        )

        if self.settings.digest:
            hour, minute = self.settings.digest
            self.scheduler.add_job(
                self._daily_digest,
                CronTrigger(hour=hour, minute=minute, timezone=self.settings.tz),
                id="digest",
                max_instances=1,
                coalesce=True,
            )

        if self.settings.evening:
            hour, minute = self.settings.evening
            self.scheduler.add_job(
                self._evening_checkin,
                CronTrigger(hour=hour, minute=minute, timezone=self.settings.tz),
                id="evening",
                max_instances=1,
                coalesce=True,
            )

        if self.settings.weekly_day and self.settings.weekly_time:
            hour, minute = self.settings.weekly_time
            self.scheduler.add_job(
                self._weekly_review,
                CronTrigger(
                    day_of_week=self.settings.weekly_day,
                    hour=hour,
                    minute=minute,
                    timezone=self.settings.tz,
                ),
                id="weekly",
                max_instances=1,
                coalesce=True,
            )

        self.scheduler.start()
        logger.info("Планировщик запущен (%s)", ", ".join(job.id for job in self.scheduler.get_jobs()))

    def shutdown(self) -> None:
        """Остановить планировщик (только если он был запущен)."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------ #
    #  Напоминания
    # ------------------------------------------------------------------ #

    async def _tick_reminders(self) -> None:
        """Раз в минуту: отправить наступившие напоминания."""
        try:
            fresh, stale_ids = await db.due_reminders(timeutil.now_utc(), REMINDER_GRACE)

            # Просроченные сильнее grace-окна помечаем отправленными молча,
            # иначе после недельного простоя прилетела бы пачка старых напоминаний.
            if stale_ids:
                await db.mark_reminders(stale_ids, sent=True)
                logger.info("Пропущено устаревших напоминаний: %d", len(stale_ids))

            for item in fresh:
                text = formatting.render_reminder(item, self.settings.tz)
                if await self._send(text):
                    await db.mark_reminders([item["reminder_id"]], sent=True)
        except Exception:  # noqa: BLE001 — задание не должно умирать молча
            logger.exception("Ошибка в обработке напоминаний")

    # ------------------------------------------------------------------ #
    #  Дайджесты
    # ------------------------------------------------------------------ #

    async def _daily_digest(self) -> None:
        """Утренний обзор: сегодня, просроченное и ближайшая неделя."""
        try:
            tz = self.settings.tz
            start_today, end_today = timeutil.day_bounds(tz)
            now = timeutil.now_utc()

            today = await db.list_tasks(status="open", start=start_today, end=end_today, limit=30)
            overdue = await db.list_tasks(status="open", end=now, limit=20)
            week = await db.list_tasks(
                status="open", start=end_today, end=timeutil.in_days(tz, 7), limit=40
            )

            local_now = datetime.now(tz)
            date_label = (
                f"{local_now.day} {timeutil.MONTHS_SHORT[local_now.month - 1]}, "
                f"{timeutil.WEEKDAYS_SHORT[local_now.weekday()]}"
            )
            blocks = [
                f"☀️ <b>Доброе утро, Альнур</b> · {date_label}",
                formatting.DIVIDER,
                self._block("Сегодня", today, empty="На сегодня дел нет — можно спокойно готовиться."),
            ]

            if overdue:
                blocks.append(self._block("⚠️ Просрочено", overdue))

            blocks.append(self._block("Ближайшая неделя", week, empty="На неделю пока пусто."))

            if not today and not overdue:
                blocks.append("<i>Хороший момент, чтобы продвинуть SAT/IELTS или заявку в Air Astana.</i>")

            await self._send("\n\n".join(blocks))
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка утреннего дайджеста")

    async def _evening_checkin(self) -> None:
        """Вечерний вопрос: итог дня и приглашение записать часы подготовки.

        Машины состояний здесь нет намеренно: вопрос дополнительно кладётся в
        историю диалога как обычная реплика ассистента, поэтому когда Альнур
        ответит, модель уже видит контекст и сама вызывает `log_hours`.

        В историю идёт текст БЕЗ HTML: разметку модель начинает копировать в
        свои ответы, и пользователь видит теги.
        """
        try:
            tz = self.settings.tz
            now = timeutil.now_utc()
            start_today, end_today = timeutil.day_bounds(tz)

            done = await db.list_tasks(
                status="completed", completed_between=(start_today, now), limit=30
            )
            # Всё, что должно было быть закрыто к этому моменту: и сегодняшнее,
            # и подвисшее с прошлых дней — вечером важно видеть и то, и другое.
            left = await db.list_tasks(status="open", end=end_today, limit=30)

            text = formatting.evening_checkin(done, left, tz)
            if await self._send(text):
                await db.add_message("assistant", formatting.evening_checkin(done, left, tz, plain=True))
        except Exception:  # noqa: BLE001 — задание не должно умирать молча
            logger.exception("Ошибка вечернего чек-ина")

    async def _weekly_review(self) -> None:
        """Недельный обзор в воскресенье: что сделано, что впереди, где перегруз."""
        try:
            tz = self.settings.tz
            now = timeutil.now_utc()
            week_start, _ = timeutil.day_bounds(tz, offset_days=-6)

            upcoming = await db.list_tasks(
                status="open", start=now, end=timeutil.in_days(tz, 7), limit=60
            )
            no_deadline = await db.list_tasks(status="open", limit=60)
            # Считаем по моменту закрытия, а не по сроку: задача, закрытая в
            # эту неделю, могла иметь срок на следующий месяц, и фильтр по
            # сроку потерял бы её.
            done = await db.list_tasks(
                status="completed", completed_between=(week_start, now), limit=60
            )

            by_category: dict[str, int] = {}
            for task in upcoming:
                by_category[task["category"]] = by_category.get(task["category"], 0) + 1

            blocks = ["📊 <b>Итоги недели</b>", formatting.DIVIDER]
            if done:
                blocks.append(f"{formatting.BULLET}Закрыто задач: <b>{len(done)}</b>")
            else:
                blocks.append(f"{formatting.BULLET}Закрытых задач за неделю нет.")

            blocks.append(self._block("На следующей неделе", upcoming, empty="Пусто."))

            if by_category:
                summary = ", ".join(
                    f"{formatting.category_emoji(cat)} {cat}: {count}"
                    for cat, count in sorted(by_category.items(), key=lambda item: -item[1])
                )
                blocks.append(f"{formatting.BULLET}<b>Куда уходит время:</b> {summary}")

            without_due = [task for task in no_deadline if not task["due_at"]]
            if without_due:
                blocks.append(
                    f"{formatting.BULLET}<b>Без срока: {len(without_due)}</b> — "
                    f"{', '.join('#%d %s' % (task['id'], task['title']) for task in without_due[:5])}"
                    "\nСтоит назначить даты или удалить лишнее."
                )

            blocks.append(
                "<i>Спланировать неделю: напиши «помоги распланировать неделю» — "
                "я предложу блоки под Air Astana, SAT/IELTS, школу и тренировки.</i>"
            )
            await self._send("\n\n".join(blocks))
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка недельного обзора")

    # ------------------------------------------------------------------ #
    #  Утилиты
    # ------------------------------------------------------------------ #

    def _block(self, title: str, tasks: list[dict[str, Any]], *, empty: str | None = None) -> str:
        """Секция сообщения со списком задач."""
        if not tasks:
            return f"<b>{title}</b>\n<i>{empty or 'Пусто.'}</i>"
        body = formatting.task_list(tasks, self.settings.tz)
        return f"<b>{title}</b>\n{body}"

    async def _send(self, text: str) -> bool:
        """Отправить сообщение владельцу. True — доставлено."""
        try:
            await self.bot.send_message(self.settings.owner_id, text)
            return True
        except TelegramAPIError:
            logger.exception("Telegram не принял сообщение планировщика")
            return False

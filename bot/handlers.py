"""Обработчики Telegram: команды, свободный текст, голосовые, фото, кнопки."""

from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta
from html import escape
from typing import Any, Sequence

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    TelegramObject,
)

from . import calendar_export, db, formatting, llm, progress, stt, timeutil
from .config import get_settings

logger = logging.getLogger(__name__)

router = Router(name="assistant")

# Telegram ограничивает сообщение 4096 символами — оставляем запас на разметку.
MAX_MESSAGE_LEN = 4000

# Сколько кнопок «выполнено» показывать под списком.
MAX_BUTTONS = 12

# Горизонт экспорта в календарь по умолчанию и его разумные границы.
EXPORT_DEFAULT_DAYS = 30
EXPORT_MAX_DAYS = 365

# За сколько последних дней считать часы подготовки в /progress.
PROGRESS_DAYS = 7

HELP_TEXT = """<b>Личный ассистент Альнура</b>

Пишите свободным текстом — разберу сам, дело это или запись в подготовку,
и поставлю напоминания. Можно надиктовать голосовым или прислать фото
расписания или задания.

<b>Примеры</b>
▸ 15 ноября дедлайн подачи документов в Air Astana Ab Initio
▸ Каждый вторник и четверг тренировка в 16:00
▸ Сдал пробник SAT на 680 · два часа математики

<b>Команды</b>
/today — дела на сегодня
/tomorrow — на завтра
/week — ближайшие 7 дней
/all — все открытые дела
/overdue — просроченное
/add — добавить дело текстом
/plan — помощь с планированием
/progress — подготовка: баллы и часы
/export — календарь файлом .ics
/done <code>12</code> — отметить выполненным
/del <code>12</code> — удалить
/reset — сбросить контекст диалога
/help — эта справка"""


# --------------------------------------------------------------------------- #
#  Доступ: только владелец
# --------------------------------------------------------------------------- #

class OwnerOnlyMiddleware(BaseMiddleware):
    """Пускает только владельца бота.

    Бот поднимается на публичном хостинге, но токен и данные личные — все
    остальные получают отказ. Проверка идёт по OWNER_TELEGRAM_ID.
    """

    async def __call__(self, handler: Any, event: TelegramObject, data: dict[str, Any]) -> Any:
        settings = get_settings()
        user = data.get("event_from_user")

        if user is None or user.id != settings.owner_id:
            logger.warning("Отказано в доступе: id=%s", getattr(user, "id", "unknown"))
            if isinstance(event, Message):
                await event.answer("🔒 Это приватный ассистент.")
            elif isinstance(event, CallbackQuery):
                await event.answer("🔒 Нет доступа", show_alert=True)
            return None

        return await handler(event, data)


router.message.middleware(OwnerOnlyMiddleware())
router.callback_query.middleware(OwnerOnlyMiddleware())


# --------------------------------------------------------------------------- #
#  Утилиты вывода
# --------------------------------------------------------------------------- #

def _split(text: str, size: int = MAX_MESSAGE_LEN) -> list[str]:
    """Разбить длинный текст на части по границам строк."""
    parts: list[str] = []
    current = ""

    for line in text.split("\n"):
        # Строка сама длиннее лимита — режем жёстко.
        while len(line) > size:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:size])
            line = line[size:]

        if not current:
            current = line
        elif len(current) + len(line) + 1 <= size:
            current = f"{current}\n{line}"
        else:
            parts.append(current)
            current = line

    if current:
        parts.append(current)
    return parts or [""]


async def _reply(message: Message, text: str, *, sanitize: bool = False) -> None:
    """Отправить ответ, разбив на части при необходимости."""
    if sanitize:
        text = formatting.sanitize_html(text)
    for part in _split(text):
        await message.answer(part)


def _tasks_keyboard(tasks: Sequence[dict[str, Any]]) -> InlineKeyboardMarkup | None:
    """Кнопки «выполнено» под списком задач."""
    rows = [
        [InlineKeyboardButton(text=f"✅ #{task['id']}", callback_data=f"done:{task['id']}")]
        for task in tasks[:MAX_BUTTONS]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _send_tasks(message: Message, title: str, tasks: list[dict[str, Any]], empty: str) -> None:
    """Показать список задач с кнопками закрытия."""
    if not tasks:
        await message.answer(f"<b>{title}</b>\n<i>{empty}</i>")
        return

    settings = get_settings()
    text = f"<b>{title}</b> · {len(tasks)}\n\n{formatting.task_list(tasks, settings.tz)}"
    await message.answer(text, reply_markup=_tasks_keyboard(tasks))


async def _ask_agent(message: Message, text: str, hint: str | None = None) -> None:
    """Отдать текст модели и ответить пользователю."""
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        answer = await llm.run_agent(text, hint=hint)
    except Exception:  # noqa: BLE001 — пользователь должен получить внятную реакцию
        logger.exception("Ошибка обращения к LLM")
        answer = (
            "▸ Модель не ответила — связь потеряна.\n"
            "Проверьте <code>OPENAI_API_KEY</code>, <code>OPENAI_BASE_URL</code> "
            "и <code>OPENAI_MODEL</code> в <code>.env</code> и лимиты провайдера.\n"
            "Дела при этом в порядке: они лежат в базе, а не в диалоге."
        )
    await _reply(message, answer, sanitize=True)


# --------------------------------------------------------------------------- #
#  Команды
# --------------------------------------------------------------------------- #

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Старт: только справка — примеры уже внутри неё."""
    await _reply(message, HELP_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await _reply(message, HELP_TEXT)


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    settings = get_settings()
    start, end = timeutil.day_bounds(settings.tz)
    tasks = await db.list_tasks(status="open", start=start, end=end, limit=40)
    await _send_tasks(message, "📅 Сегодня", tasks, "На сегодня дел нет.")


@router.message(Command("tomorrow"))
async def cmd_tomorrow(message: Message) -> None:
    settings = get_settings()
    start, end = timeutil.day_bounds(settings.tz, offset_days=1)
    tasks = await db.list_tasks(status="open", start=start, end=end, limit=40)
    await _send_tasks(message, "📅 Завтра", tasks, "На завтра дел нет.")


@router.message(Command("week"))
async def cmd_week(message: Message) -> None:
    settings = get_settings()
    start, _ = timeutil.day_bounds(settings.tz)
    tasks = await db.list_tasks(
        status="open", start=start, end=timeutil.in_days(settings.tz, 7), limit=50
    )
    await _send_tasks(message, "🗓 Ближайшие 7 дней", tasks, "На неделю пока пусто.")


@router.message(Command("all"))
async def cmd_all(message: Message) -> None:
    tasks = await db.list_tasks(status="open", limit=60)
    await _send_tasks(message, "🗂 Все открытые дела", tasks, "Открытых дел нет.")


@router.message(Command("overdue"))
async def cmd_overdue(message: Message) -> None:
    tasks = await db.list_tasks(status="open", end=timeutil.now_utc(), limit=40)
    await _send_tasks(message, "⚠️ Просрочено", tasks, "Просроченного нет.")


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject) -> None:
    text = (command.args or "").strip()
    if not text:
        await message.answer(
            "▸ Напишите, что добавить. Пример:\n"
            "<code>/add 15 ноября дедлайн подачи документов Air Astana</code>"
        )
        return
    await _ask_agent(
        message,
        text,
        hint="пользователь использует команду /add: создай задачу через add_task "
        "и подтверди её одной короткой строкой.",
    )


@router.message(Command("plan"))
async def cmd_plan(message: Message, command: CommandObject) -> None:
    text = (command.args or "").strip()
    if not text:
        await message.answer(
            "▸ Что распланировать? Пример:\n<code>/plan подготовка к SAT за 3 недели</code>\n"
            "<code>/plan повторение математики к субботе</code>"
        )
        return
    await _ask_agent(message, f"Помоги распланировать: {text}")


@router.message(Command("done"))
async def cmd_done(message: Message, command: CommandObject) -> None:
    task_id = _parse_id(command.args)
    if task_id is None:
        await message.answer("▸ Укажи id задачи: <code>/done 12</code>")
        return
    task = await db.complete_task(task_id)
    if task is None:
        await message.answer(f"▸ Задача #{task_id} не найдена.")
        return
    await message.answer(_done_text(task, task_id))


@router.message(Command("del"))
async def cmd_del(message: Message, command: CommandObject) -> None:
    task_id = _parse_id(command.args)
    if task_id is None:
        await message.answer("▸ Укажи id задачи: <code>/del 12</code>")
        return
    task = await db.get_task(task_id)
    if task is None:
        await message.answer(f"▸ Задача #{task_id} не найдена.")
        return
    await db.delete_task(task_id)
    await message.answer(f"▸ Удалено: <b>{escape(task['title'])}</b> (#{task_id})")


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    await db.clear_history()
    await message.answer("▸ Контекст диалога очищен. Сами задачи остались на месте.")


@router.message(Command("progress"))
async def cmd_progress(message: Message, command: CommandObject) -> None:
    """Дашборд подготовки. Считается локально — без обращения к модели."""
    settings = get_settings()
    raw = (command.args or "").strip()

    subject = None
    if raw:
        subject = progress.resolve_subject(raw)
        if subject is None:
            await message.answer(
                f"▸ Не знаю такого предмета: <code>{escape(raw)}</code>\n"
                f"Доступные: {', '.join(progress.SUBJECTS)}"
            )
            return

    until = datetime.now(settings.tz).date()
    since = (until - timedelta(days=PROGRESS_DAYS - 1)).isoformat()

    report = progress.build_report(
        await db.list_progress(subject=subject),
        since=since,
        until=until.isoformat(),
        subject=subject,
    )
    await _reply(message, report)


@router.message(Command("export"))
async def cmd_export(message: Message, command: CommandObject) -> None:
    """Выгрузить открытые дела файлом .ics для Google Calendar и подобных."""
    settings = get_settings()
    days = _parse_horizon(command.args)

    start, _ = timeutil.day_bounds(settings.tz)
    tasks = await db.list_tasks(
        status="open", start=start, end=timeutil.in_days(settings.tz, days), limit=300
    )

    if not tasks:
        await message.answer(
            f"▸ За ближайшие {days} {timeutil.plural(days, 'день', 'дня', 'дней')} "
            "задач со сроком нет — выгружать нечего."
        )
        return

    payload = calendar_export.build_ics(tasks, settings.tz).encode("utf-8")
    await message.answer_document(
        BufferedInputFile(payload, filename="assistant.ics"),
        caption=(
            f"▸ Календарь на {days} {timeutil.plural(days, 'день', 'дня', 'дней')} · "
            f"{len(tasks)} {timeutil.plural(len(tasks), 'задача', 'задачи', 'задач')}\n"
            "Импорт в Google Calendar: Настройки → Импорт и экспорт."
        ),
    )


def _parse_horizon(raw: str | None) -> int:
    """Горизонт экспорта в днях: '/export 60' -> 60. По умолчанию 30."""
    if raw and raw.strip().isdigit():
        return max(1, min(int(raw.strip()), EXPORT_MAX_DAYS))
    return EXPORT_DEFAULT_DAYS


def _done_text(task: dict[str, Any], task_id: int) -> str:
    """Подтверждение закрытия. Отдельно сообщаем, если серия продолжилась."""
    text = f"▸ Готово: <b>{escape(task['title'])}</b> (#{task_id})"
    next_task = task.get("next_task")
    if next_task and next_task.get("due_at"):
        settings = get_settings()
        text += (
            f"\n▸ Следующая {formatting.recurrence_label(next_task.get('recurrence'))}: "
            f"<b>{timeutil.fmt_day(next_task['due_at'], settings.tz)}</b>"
        )
    return text


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    """Подсказка для настройки OWNER_TELEGRAM_ID."""
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


def _parse_id(raw: str | None) -> int | None:
    """Достать числовой id из аргументов команды: '/done 12' -> 12, '#12' -> 12."""
    if not raw:
        return None
    cleaned = raw.strip().lstrip("#")
    return int(cleaned) if cleaned.isdigit() else None


# --------------------------------------------------------------------------- #
#  Кнопки
# --------------------------------------------------------------------------- #

@router.callback_query(F.data.startswith("done:"))
async def cb_complete(query: CallbackQuery) -> None:
    """Закрыть задачу кнопкой под списком."""
    if query.message is None or not isinstance(query.message, Message):
        await query.answer()
        return

    raw_id = (query.data or "").split(":", maxsplit=1)[-1]
    if not raw_id.isdigit():
        await query.answer("Некорректный id")
        return

    task_id = int(raw_id)
    task = await db.complete_task(task_id)
    if task is None:
        await query.answer("Задача уже удалена", show_alert=True)
        return

    await query.answer("Готово")

    # Убираем кнопку этой задачи из клавиатуры и дописываем отметку в текст.
    markup: InlineKeyboardMarkup | None = None
    if query.message.reply_markup is not None:
        rows = [
            [button for button in row if button.callback_data != query.data]
            for row in query.message.reply_markup.inline_keyboard
        ]
        rows = [row for row in rows if row]
        markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    # Тот же текст, что и у /done: путь через кнопку тоже должен сказать,
    # если у повторяющегося дела появилась следующая дата.
    new_text = f"{query.message.html_text.rstrip()}\n\n{_done_text(task, task_id)}"
    try:
        await query.message.edit_text(new_text[:MAX_MESSAGE_LEN], reply_markup=markup)
    except Exception:  # noqa: BLE001 — чаще всего «message is not modified», это не ошибка
        logger.debug("Не удалось обновить сообщение со списком", exc_info=True)


# --------------------------------------------------------------------------- #
#  Голосовые и аудио
# --------------------------------------------------------------------------- #

@router.message(F.voice | F.audio)
async def handle_voice(message: Message, bot: Bot) -> None:
    """Расшифровать голосовое и обработать как обычный текст."""
    settings = get_settings()
    media = message.voice or message.audio
    if media is None:
        return

    if not stt.is_configured(settings):
        await message.answer(
            "▸ Голосовые пока не настроены.\n"
            "Добавьте в <code>.env</code> бесплатный ключ Groq (console.groq.com):\n"
            "<code>STT_API_KEY=gsk_...</code>\n"
            "и перезапустите бота. Либо напишите текстом."
        )
        return

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    status = await message.answer("▸ Слушаю…")

    try:
        buffer = io.BytesIO()
        await bot.download(media, destination=buffer)
        filename = getattr(media, "file_name", None) or "voice.ogg"
        transcript = await stt.transcribe(buffer.getvalue(), filename, settings)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось расшифровать голосовое")
        await status.edit_text("▸ Не получилось распознать. Скажите ещё раз или напишите текстом.")
        return

    if not transcript:
        await status.edit_text("▸ Речь не разобрал — попробуйте ближе к микрофону.")
        return

    # Показываем расшифровку: если модель что-то поняла не так, это будет видно.
    await status.edit_text(f"▸ <i>«{escape(transcript)}»</i>")
    await _ask_agent(
        message,
        transcript,
        hint="сообщение пришло голосом и было расшифровано автоматически; "
        "если расшифровка выглядит бессмысленной — попроси повторить текстом.",
    )


# --------------------------------------------------------------------------- #
#  Фото и скриншоты
# --------------------------------------------------------------------------- #

@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot) -> None:
    """Разобрать фото расписания, списка заданий или условия задачи.

    Порядок регистрации здесь важен: этот хендлер обязан стоять выше
    catch-all `handle_other`, иначе тот ответит «пришли словами» и фото
    не дойдёт до модели.
    """
    photo = _pick_photo(message.photo or [])
    if photo is None:
        limit = llm.IMAGE_MAX_BYTES // (1024 * 1024)
        await message.answer(
            f"▸ Снимок больше {limit} МБ — такой я не разберу.\n"
            "Пришлите скриншот или фото меньшего размера либо опишите словами."
        )
        return

    caption = (message.caption or "").strip()
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    status = await message.answer("▸ Смотрю изображение…")

    try:
        buffer = io.BytesIO()
        await bot.download(photo, destination=buffer)
        answer = await llm.run_agent(
            caption or "Фото без подписи.",
            hint=(
                "пользователь прислал изображение: это может быть расписание, список заданий, "
                "скриншот классрума или условие задачи. Разбери, что на нём, и заведи дела "
                "через add_task — по одной задаче на пункт, со сроками и категориями. "
                "Если это не расписание и не задание, коротко опиши, что видишь, "
                "и спроси, что с этим сделать."
            ),
            images=[("image/jpeg", buffer.getvalue())],
        )
    except Exception:  # noqa: BLE001 — пользователь должен увидеть внятную реакцию
        logger.exception("Не удалось разобрать изображение")
        await status.edit_text(
            "▸ Не получилось разобрать изображение. Пришлите его ещё раз "
            "или опишите срок словами."
        )
        return

    # Служебное «Смотрю изображение…» убираем: ответ идёт отдельным сообщением,
    # а два подряд выглядят как сбой.
    try:
        await status.delete()
    except Exception:  # noqa: BLE001 — сообщение могли уже удалить
        logger.debug("Не удалось удалить статус", exc_info=True)
    await _reply(message, answer, sanitize=True)


def _pick_photo(sizes: Sequence[PhotoSize]) -> PhotoSize | None:
    """Самый крупный размер фото в пределах лимита.

    Telegram присылает набор размеров от маленького к большому. Берём
    наибольший допустимый: чем больше картинка, тем лучше модель читает
    мелкий текст расписания, но у base64 есть цена в токенах.
    """
    usable = [size for size in sizes if (size.file_size or 0) <= llm.IMAGE_MAX_BYTES]
    return usable[-1] if usable else None


# --------------------------------------------------------------------------- #
#  Свободный текст
# --------------------------------------------------------------------------- #

@router.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message) -> None:
    """Главный сценарий: обычное сообщение → агент с инструментами."""
    await _ask_agent(message, message.text or "")


@router.message(F.text)
async def handle_unknown_command(message: Message) -> None:
    """Сюда попадают только неизвестные команды (текст без / перехвачен выше)."""
    await message.answer("▸ Такой команды нет. Список: /help")


@router.message()
async def handle_other(message: Message) -> None:
    """Стикеры, документы, видео и прочее, что бот не разбирает."""
    await message.answer(
        "▸ Понимаю текст, голосовые и фото. Пришлите расписание снимком "
        "или опишите словами — например:\n"
        "<i>«в пятницу в 18:00 занятие с репетитором по математике»</i>"
    )

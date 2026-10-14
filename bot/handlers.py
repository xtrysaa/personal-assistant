"""Обработчики Telegram: команды, свободный текст, голосовые, кнопки."""

from __future__ import annotations

import io
import logging
from html import escape
from typing import Any, Sequence

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from . import db, formatting, llm, stt, timeutil
from .config import get_settings

logger = logging.getLogger(__name__)

router = Router(name="assistant")

# Telegram ограничивает сообщение 4096 символами — оставляем запас на разметку.
MAX_MESSAGE_LEN = 4000

# Сколько кнопок «выполнено» показывать под списком.
MAX_BUTTONS = 12

HELP_TEXT = """<b>Личный ассистент Альнура</b> 🤖

Пиши свободным текстом — я сам разберу, что это дело, и поставлю напоминания.

<b>Примеры</b>
• 15 ноября дедлайн подачи документов в Air Astana Ab Initio
• Завтра в 15:00 контрольная по физике
• Каждую неделю по воскресеньям разбор ошибок SAT, начиная с этого
• Напомни за месяц до регистрации на IELTS
• Что у меня на этой неделе?
• Помоги распланировать повторение математики к субботе

<b>Голосовые</b> 🎤 — можешь просто надиктовать, я расшифрую и запишу.

<b>Команды</b>
/today — дела на сегодня
/tomorrow — на завтра
/week — ближайшие 7 дней
/all — все открытые дела
/overdue — просроченное
/add — добавить дело текстом
/plan — помощь с планированием
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
            "⚠️ Не получилось получить ответ от модели.\n"
            "Проверь OPENAI_API_KEY, OPENAI_BASE_URL и OPENAI_MODEL в .env "
            "и лимиты провайдера."
        )
    await _reply(message, answer, sanitize=True)


# --------------------------------------------------------------------------- #
#  Команды
# --------------------------------------------------------------------------- #

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await _reply(message, HELP_TEXT)
    await message.answer(
        "С чего начнём? Например: <i>«15 ноября дедлайн Air Astana Ab Initio»</i>"
    )


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
    await _send_tasks(message, "⚠️ Просрочено", tasks, "Просроченного нет — красота.")


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject) -> None:
    text = (command.args or "").strip()
    if not text:
        await message.answer(
            "Напиши, что добавить. Пример:\n"
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
            "Что распланировать? Пример:\n<code>/plan подготовка к SAT за 3 недели</code>\n"
            "<code>/plan повторение математики к субботе</code>"
        )
        return
    await _ask_agent(message, f"Помоги распланировать: {text}")


@router.message(Command("done"))
async def cmd_done(message: Message, command: CommandObject) -> None:
    task_id = _parse_id(command.args)
    if task_id is None:
        await message.answer("Укажи id задачи: <code>/done 12</code>")
        return
    task = await db.complete_task(task_id)
    if task is None:
        await message.answer(f"Задача #{task_id} не найдена.")
        return
    await message.answer(f"✅ Готово: <b>{escape(task['title'])}</b> (#{task_id})")


@router.message(Command("del"))
async def cmd_del(message: Message, command: CommandObject) -> None:
    task_id = _parse_id(command.args)
    if task_id is None:
        await message.answer("Укажи id задачи: <code>/del 12</code>")
        return
    task = await db.get_task(task_id)
    if task is None:
        await message.answer(f"Задача #{task_id} не найдена.")
        return
    await db.delete_task(task_id)
    await message.answer(f"🗑 Удалено: <b>{escape(task['title'])}</b> (#{task_id})")


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    await db.clear_history()
    await message.answer("🧹 Контекст диалога очищен. Сами задачи остались на месте.")


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    """Подсказка для настройки OWNER_TELEGRAM_ID."""
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


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

    await query.answer("Готово ✅")

    # Убираем кнопку этой задачи из клавиатуры и дописываем отметку в текст.
    markup: InlineKeyboardMarkup | None = None
    if query.message.reply_markup is not None:
        rows = [
            [button for button in row if button.callback_data != query.data]
            for row in query.message.reply_markup.inline_keyboard
        ]
        rows = [row for row in rows if row]
        markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    new_text = f"{query.message.html_text.rstrip()}\n\n✅ Выполнено: #{task_id} {escape(task['title'])}"
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
            "🎤 Голосовые пока не настроены.\n\n"
            "Добавь в <code>.env</code> бесплатный ключ Groq (console.groq.com):\n"
            "<code>STT_API_KEY=gsk_...</code>\n"
            "и перезапусти бота. Либо напиши текстом."
        )
        return

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    status = await message.answer("🎧 Слушаю…")

    try:
        buffer = io.BytesIO()
        await bot.download(media, destination=buffer)
        filename = getattr(media, "file_name", None) or "voice.ogg"
        transcript = await stt.transcribe(buffer.getvalue(), filename, settings)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось расшифровать голосовое")
        await status.edit_text("⚠️ Не получилось распознать. Скажи ещё раз или напиши текстом.")
        return

    if not transcript:
        await status.edit_text("🎤 Речь не разобрал — попробуй ближе к микрофону.")
        return

    # Показываем расшифровку: если модель что-то поняла не так, это будет видно.
    await status.edit_text(f"🎤 <i>«{escape(transcript)}»</i>")
    await _ask_agent(
        message,
        transcript,
        hint="сообщение пришло голосом и было расшифровано автоматически; "
        "если расшифровка выглядит бессмысленной — попроси повторить текстом.",
    )


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
    await message.answer("Не знаю такой команды 🤔 Список: /help")


@router.message()
async def handle_other(message: Message) -> None:
    """Фото, стикеры, документы и прочее."""
    await message.answer(
        "Пока понимаю текст и голосовые. Пришли дедлайн словами — например:\n"
        "<i>«в пятницу в 18:00 занятие с репетитором по математике»</i>"
    )

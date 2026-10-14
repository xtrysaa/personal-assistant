"""Точка входа: бот + планировщик напоминаний + health-check сервер.

Запуск локально:   python run.py
На сервере:        systemctl start assistant-bot  (см. README.md)
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand

from bot import db, stt
from bot.config import ConfigError, get_settings
from bot.handlers import router
from bot.health import start_health_server
from bot.scheduler import AssistantScheduler

# Меню команд в Telegram (кнопка «/» слева от поля ввода).
COMMANDS = [
    BotCommand(command="today", description="Дела на сегодня"),
    BotCommand(command="tomorrow", description="Дела на завтра"),
    BotCommand(command="week", description="Ближайшие 7 дней"),
    BotCommand(command="all", description="Все открытые дела"),
    BotCommand(command="overdue", description="Просроченное"),
    BotCommand(command="add", description="Добавить дело текстом"),
    BotCommand(command="plan", description="Помочь распланировать"),
    BotCommand(command="done", description="Отметить выполненным: /done 12"),
    BotCommand(command="del", description="Удалить задачу: /del 12"),
    BotCommand(command="reset", description="Сбросить контекст диалога"),
    BotCommand(command="help", description="Как пользоваться"),
]


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Каждый апдейт от Telegram логируется отдельно — для личного бота это шум.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    settings = get_settings()
    await db.init_db(settings.db_path)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    scheduler = AssistantScheduler(bot, settings)
    runner = None

    # Всё, что может упасть (неверный токен, занятый порт, сеть), держим в try,
    # чтобы finally гарантированно закрыл соединения и не оставил «Unclosed client session».
    try:
        # Планировщик стартует до polling: напоминания начнут проверяться сразу.
        scheduler.start()

        # Только для облачных хостингов, которым нужен открытый порт (Render и т.п.).
        if settings.health_port:
            runner = await start_health_server(settings.health_port)

        await bot.set_my_commands(COMMANDS)
        me = await bot.get_me()
        logging.info(
            "Бот запущен: @%s | часовой пояс: %s | модель: %s | голосовые: %s",
            me.username,
            settings.tz_name,
            settings.openai_model,
            "включены" if stt.is_configured(settings) else "выключены",
        )

        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    except TelegramUnauthorizedError as error:
        # Без эмодзи: сообщение печатается в консоль, а Windows-консоль часто в cp1251.
        raise SystemExit(
            "\nОшибка: Telegram отклонил BOT_TOKEN.\n"
            "  Проверь значение в .env (скопировано целиком?).\n"
            "  Новый токен: @BotFather -> /mybots -> API Token\n"
        ) from error
    finally:
        scheduler.shutdown()
        if runner is not None:
            await runner.cleanup()
        await bot.session.close()
        logging.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ConfigError as error:
        raise SystemExit(f"\nОшибка конфигурации: {error}\n") from error
    except (KeyboardInterrupt, SystemExit):
        pass

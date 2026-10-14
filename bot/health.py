"""Мини HTTP-сервер для health-check.

Нужен только на облачных хостингах, которые ждут открытый порт (Render, Koyeb):
внешний пинг-сервис раз в 10 минут дёргает `/health`, и контейнер не засыпает.
Для Oracle Cloud / VPS / домашнего сервера не нужен — оставь HEALTH_PORT пустым.

Long polling Telegram исходящий, поэтому публичный домен и SSL боту не требуются.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiohttp import web

logger = logging.getLogger(__name__)

_STARTED_AT = datetime.now(timezone.utc)


async def _health(_request: web.Request) -> web.Response:
    """Отдаёт статус и аптайм процесса."""
    uptime = int((datetime.now(timezone.utc) - _STARTED_AT).total_seconds())
    return web.json_response({"status": "ok", "uptime_seconds": uptime})


async def start_health_server(port: int) -> web.AppRunner:
    """Поднять сервер на 0.0.0.0:port и вернуть runner (чтобы потом остановить)."""
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()

    logger.info("Health-check сервер слушает 0.0.0.0:%d", port)
    return runner

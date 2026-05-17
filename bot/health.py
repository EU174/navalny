"""
health.py — HTTP health-check server for Fly.io.

Fly.io pings /health every 30s. If it fails — machine restarts.
Also serves GET / for convenience.
"""

import time
import logging

from aiohttp import web

from bot.config import HEALTH_PORT, SOURCE_CHANNELS, LANGUAGES

log = logging.getLogger("tg-aggregator")

# Updated by main loop after each cycle
last_cycle_ts: float = 0.0


async def _handler(request: web.Request) -> web.Response:
    ago = time.time() - last_cycle_ts if last_cycle_ts else -1
    return web.json_response({
        "status": "ok",
        "last_cycle_secs_ago": round(ago, 1),
        "sources": len(SOURCE_CHANNELS),
        "targets": len(LANGUAGES),
    })


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", _handler)
    app.router.add_get("/health", _handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HEALTH_PORT)
    await site.start()
    log.info("Health-check server listening on :%d", HEALTH_PORT)

"""
health.py — HTTP health-check + stats for T16 /status.
"""

import time
import logging

from aiohttp import web

from bot.config import HEALTH_PORT, SOURCE_CHANNELS, LANGUAGES

log = logging.getLogger("tg-aggregator")

last_cycle_ts: float = 0.0
last_cycle_published: int = 0
last_cycle_errors: int = 0
total_published: int = 0
today_published: int = 0
today_errors: int = 0
today_date: str = ""


def get_stats() -> dict:
    """Return current stats dict (used by /status command and health endpoint)."""
    from bot.translator import deepl_chars_used
    ago = time.time() - last_cycle_ts if last_cycle_ts else -1
    return {
        "status": "ok",
        "last_cycle_secs_ago": round(ago, 1),
        "last_cycle_published": last_cycle_published,
        "last_cycle_errors": last_cycle_errors,
        "total_published": total_published,
        "today_published": today_published,
        "today_errors": today_errors,
        "deepl_chars_used": deepl_chars_used,
        "sources": len(SOURCE_CHANNELS),
        "targets": len(LANGUAGES),
    }


async def _handler(request: web.Request) -> web.Response:
    return web.json_response(get_stats())


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", _handler)
    app.router.add_get("/health", _handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HEALTH_PORT)
    await site.start()
    log.info("Health-check server listening on :%d", HEALTH_PORT)

#!/usr/bin/env python3
"""
main.py — Entry point. Orchestrates the polling loop.

You rarely need to edit this file. Instead edit:
  - config.py    → channels, languages, settings
  - scraper.py   → parsing logic
  - translator.py → translation backends, name fixes
  - publisher.py  → message format, Telegram API calls
  - state.py      → deduplication storage
  - health.py     → Fly.io health-check
"""

import asyncio
import logging
import sys
import time

import aiohttp

from bot.config import SOURCE_CHANNELS, LANGUAGES, POLL_INTERVAL
from bot.scraper import fetch_channel_posts
from bot.translator import translate
from bot.publisher import publish_post
from bot.state import load_seen, save_seen, has_any_state, load_content_hashes, save_content_hashes, content_hash
from bot import health

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("tg-aggregator")


async def process_cycle():
    """One polling cycle: fetch → translate → publish."""
    async with aiohttp.ClientSession() as session:
        # 1. Fetch all source channels
        all_posts = []
        for ch in SOURCE_CHANNELS:
            posts = await fetch_channel_posts(session, ch)
            all_posts.extend(posts)
            await asyncio.sleep(1)

        if not all_posts:
            log.info("No posts fetched this cycle")
            health.last_cycle_ts = time.time()
            return

        all_posts.sort(key=lambda p: p.post_id)

        # 2. For each language: translate & publish new posts (skip content dupes)
        for lang in LANGUAGES:
            seen = load_seen(lang.code)
            hashes = load_content_hashes(lang.code)
            new_count = 0

            for post in all_posts:
                if post.post_id in seen:
                    continue

                # Content dedup: skip if same text already published from another channel
                ch = content_hash(post.text_plain)
                if ch in hashes:
                    log.info("Skipping %s (duplicate content) for %s", post.post_id, lang.code)
                    seen.add(post.post_id)
                    save_seen(lang.code, seen)
                    continue

                log.info("Translating %s → %s", post.post_id, lang.code)
                try:
                    translated = await translate(session, post.text_html, lang)
                    await publish_post(session, post, translated, lang)
                    new_count += 1
                except Exception as e:
                    log.error("Failed %s for %s: %s",
                              post.post_id, lang.code, e)

                seen.add(post.post_id)
                hashes.add(ch)
                save_seen(lang.code, seen)
                save_content_hashes(lang.code, hashes)
                await asyncio.sleep(1)

            if new_count:
                log.info("Published %d new posts to %s", new_count, lang.chat_id)

    health.last_cycle_ts = time.time()


async def seed_initial_state():
    """First run: mark all existing posts as seen so we don't flood."""
    log.info("First run — marking existing posts as seen")
    async with aiohttp.ClientSession() as session:
        for ch in SOURCE_CHANNELS:
            posts = await fetch_channel_posts(session, ch)
            for lang in LANGUAGES:
                seen = load_seen(lang.code)
                hashes = load_content_hashes(lang.code)
                for p in posts:
                    seen.add(p.post_id)
                    hashes.add(content_hash(p.text_plain))
                save_seen(lang.code, seen)
                save_content_hashes(lang.code, hashes)
            await asyncio.sleep(1)
    log.info("Initial state saved. Only new posts from now on.")


async def main():
    log.info("Bot starting. Polling every %ds", POLL_INTERVAL)
    log.info("Sources: %s", ", ".join(f"@{c}" for c in SOURCE_CHANNELS))
    log.info("Targets: %s", ", ".join(l.chat_id for l in LANGUAGES))

    await health.start_health_server()

    if not has_any_state():
        await seed_initial_state()

    while True:
        try:
            await process_cycle()
        except Exception as e:
            log.error("Cycle error: %s", e, exc_info=True)

        log.info("Sleeping %ds until next cycle", POLL_INTERVAL)
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())

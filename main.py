#!/usr/bin/env python3
"""
main.py — Entry point. Orchestrates polling, translation, publishing.
"""

import asyncio
import logging
import sys
import time

import aiohttp

from bot.config import SOURCE_CHANNELS, LANGUAGES, POLL_INTERVAL
from bot.scraper import fetch_channel_posts, Post
from bot.translator import translate
from bot.publisher import publish_post, send_admin_alert
from bot.state import (
    load_seen, save_seen,
    load_published, save_published,
    load_content_hashes, save_content_hashes, content_hash,
    load_failed, mark_failed, get_retryable, clear_failed,
    has_any_state,
)
from bot import health

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("tg-aggregator")

# Track consecutive zero-fetch cycles for alerting
_zero_fetch_count = 0
_ZERO_FETCH_ALERT = 6  # alert after 6 cycles (1.5 hours) with 0 posts


async def process_post(session, post: Post, lang, seen, published, hashes):
    """Translate and publish one post. Returns (published_ok, is_dupe)."""
    ch = content_hash(post.text_plain)
    if ch in hashes:
        log.info("Skipping %s (duplicate content) for %s", post.post_id, lang.code)
        return False, True  # not published, but is dupe (don't retry)

    log.info("Translating %s → %s", post.post_id, lang.code)
    try:
        translated = await translate(session, post.text_html, lang)
        ok = await publish_post(session, post, translated, lang)
        if ok:
            hashes.add(ch)
        return ok, False
    except Exception as e:
        log.error("Failed %s for %s: %s", post.post_id, lang.code, e)
        return False, False


async def process_cycle():
    """One polling cycle: fetch → dedup → translate → publish → retry."""
    global _zero_fetch_count

    cycle_published = 0
    cycle_errors = 0

    async with aiohttp.ClientSession() as session:
        # 1. Fetch all source channels
        all_posts: list[Post] = []
        for ch in SOURCE_CHANNELS:
            posts = await fetch_channel_posts(session, ch)
            all_posts.extend(posts)
            await asyncio.sleep(1)

        if not all_posts:
            _zero_fetch_count += 1
            log.warning("No posts fetched this cycle (%d consecutive)", _zero_fetch_count)
            if _zero_fetch_count >= _ZERO_FETCH_ALERT:
                await send_admin_alert(
                    session,
                    f"⚠️ {_zero_fetch_count} consecutive cycles with 0 posts fetched.\n"
                    "Possible issue: t.me/s/ blocked or channels changed."
                )
                _zero_fetch_count = 0
            health.last_cycle_ts = time.time()
            health.last_cycle_published = 0
            health.last_cycle_errors = 0
            return
        else:
            _zero_fetch_count = 0

        # Build post lookup for retries
        post_map = {p.post_id: p for p in all_posts}
        all_posts.sort(key=lambda p: p.post_id)

        # 2. Process each language
        for lang in LANGUAGES:
            seen = load_seen(lang.code)
            published = load_published(lang.code)
            hashes = load_content_hashes(lang.code)

            # 2a. New posts
            for post in all_posts:
                if post.post_id in seen:
                    continue

                ok, is_dupe = await process_post(
                    session, post, lang, seen, published, hashes
                )

                seen.add(post.post_id)
                if ok:
                    published.add(post.post_id)
                    cycle_published += 1
                    clear_failed(lang.code, post.post_id)
                elif not is_dupe:
                    mark_failed(lang.code, post.post_id)
                    cycle_errors += 1

                save_seen(lang.code, seen)
                save_published(lang.code, published)
                save_content_hashes(lang.code, hashes)
                await asyncio.sleep(1)

            # 2b. Retry previously failed posts (if still in fetched batch)
            retryable = get_retryable(lang.code)
            for post_id in retryable:
                if post_id in published:
                    clear_failed(lang.code, post_id)
                    continue
                post = post_map.get(post_id)
                if not post:
                    continue  # post no longer in recent fetch, skip

                log.info("Retrying %s → %s", post_id, lang.code)
                ok, _ = await process_post(
                    session, post, lang, seen, published, hashes
                )
                if ok:
                    published.add(post_id)
                    save_published(lang.code, published)
                    save_content_hashes(lang.code, hashes)
                    clear_failed(lang.code, post_id)
                    cycle_published += 1
                else:
                    mark_failed(lang.code, post_id)
                    cycle_errors += 1
                await asyncio.sleep(1)

            if cycle_published:
                log.info("Published %d posts to %s", cycle_published, lang.chat_id)

    health.last_cycle_ts = time.time()
    health.last_cycle_published = cycle_published
    health.last_cycle_errors = cycle_errors
    health.total_published += cycle_published


async def seed_initial_state():
    """First run: mark all existing posts as seen."""
    log.info("First run — marking existing posts as seen")
    async with aiohttp.ClientSession() as session:
        for ch_name in SOURCE_CHANNELS:
            posts = await fetch_channel_posts(session, ch_name)
            for lang in LANGUAGES:
                seen = load_seen(lang.code)
                published = load_published(lang.code)
                hashes = load_content_hashes(lang.code)
                for p in posts:
                    seen.add(p.post_id)
                    published.add(p.post_id)  # mark as published so no retry
                    hashes.add(content_hash(p.text_plain))
                save_seen(lang.code, seen)
                save_published(lang.code, published)
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

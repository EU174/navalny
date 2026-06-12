#!/usr/bin/env python3
"""
main.py — T21: one-time republish marker, bot commands, polling loop.
"""

import asyncio
import logging
import sys
import time
from datetime import datetime, timezone

import aiohttp

from bot.config import SOURCE_CHANNELS, LANGUAGES, POLL_INTERVAL, BOT_TOKEN, ADMIN_CHAT_ID
from bot.scraper import fetch_channel_posts, Post
from bot.translator import (
    translate, set_alert_callback, get_deepl_usage,
    cleanup_cache, cache_stats, cache_hits, cache_misses,
)
from bot.publisher import publish_post, send_admin_alert, tg_request
from bot.state import (
    load_seen, save_seen,
    load_published, save_published,
    load_content_hashes, save_content_hashes, content_hash,
    mark_failed, get_retryable, clear_failed,
    has_any_state,
)
from bot import health

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("tg-aggregator")

_zero_fetch_count = 0
_ZERO_FETCH_ALERT = 6
# T26: Track last seen message_id per channel for gap detection
_last_seen_ids: dict[str, int] = {}
# T33: Track published count per channel (session lifetime)
_channel_published: dict[str, int] = {}

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
_last_update_id = 0


# ─── Bot commands ────────────────────────────────────────────────────────────

async def handle_commands(session):
    global _last_update_id
    if not ADMIN_CHAT_ID:
        return
    try:
        url = f"{TG_API}/getUpdates"
        params = {"offset": _last_update_id + 1, "timeout": 0, "limit": 10}
        async with session.get(url, params=params,
                               timeout=aiohttp.ClientTimeout(total=5)) as resp:
            data = await resp.json()
            if not data.get("ok"):
                return
            for update in data.get("result", []):
                _last_update_id = update["update_id"]
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip()
                if chat_id != str(ADMIN_CHAT_ID):
                    continue
                if text == "/status":
                    await cmd_status(session, chat_id)
                elif text == "/stats":
                    await cmd_stats(session, chat_id)
                elif text.startswith("/force "):
                    await cmd_force(session, chat_id, text[7:].strip())
                elif text == "/help":
                    await tg_request(session, "sendMessage", chat_id=chat_id,
                        text="Commands:\n/status — bot stats\n/stats — extended stats\n/force channel/id — republish\n/help")
    except Exception as e:
        log.debug("Command poll error: %s", e)


async def cmd_status(session, chat_id):
    stats = health.get_stats()
    text = (
        f"📊 <b>Bot Status</b>\n\n"
        f"Today: {stats['today_published']} published, {stats['today_errors']} errors\n"
        f"Total: {stats['total_published']} published\n"
        f"DeepL chars: {stats['deepl_chars_used']:,}\n\n"
        f"Last cycle: {stats['last_cycle_secs_ago']:.0f}s ago\n"
        f"Sources: {stats['sources']}, Targets: {stats['targets']}"
    )
    await tg_request(session, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")


async def cmd_stats(session, chat_id):
    """T33: Extended statistics — DeepL usage, cache hit rate, top channels."""
    import bot.translator as tr
    stats = health.get_stats()
    deepl = get_deepl_usage()
    cstats = cache_stats()

    # Cache hit rate
    total_lookups = tr.cache_hits + tr.cache_misses
    hit_rate = round(100 * tr.cache_hits / total_lookups, 1) if total_lookups else 0.0

    # Top channels by published count
    top = sorted(_channel_published.items(), key=lambda kv: kv[1], reverse=True)[:5]
    if top:
        top_lines = "\n".join(f"  • @{ch}: {n}" for ch, n in top)
    else:
        top_lines = "  (none yet this session)"

    text = (
        f"📈 <b>Extended Stats</b>\n\n"
        f"<b>DeepL ({deepl['month']})</b>\n"
        f"  {deepl['chars']:,} / {deepl['limit']:,} chars ({deepl['percent']}%)\n\n"
        f"<b>Translation cache</b>\n"
        f"  Hit rate: {hit_rate}% ({tr.cache_hits} hits / {tr.cache_misses} misses)\n"
        f"  Entries: {cstats['entries']} ({cstats['size_kb']} KB)\n\n"
        f"<b>Top channels (session)</b>\n{top_lines}\n\n"
        f"<b>Totals</b>\n"
        f"  Published: {stats['total_published']}\n"
        f"  Today: {stats['today_published']} pub, {stats['today_errors']} err"
    )
    await tg_request(session, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")


async def cmd_force(session, chat_id, post_id):
    if "/" not in post_id:
        await tg_request(session, "sendMessage", chat_id=chat_id,
                         text="Usage: /force channel/id\nExample: /force teamnavalny/28126")
        return
    channel = post_id.split("/")[0]
    await tg_request(session, "sendMessage", chat_id=chat_id, text=f"⏳ Fetching {post_id}...")
    posts = await fetch_channel_posts(session, channel)
    post = next((p for p in posts if p.post_id == post_id), None)
    if not post:
        await tg_request(session, "sendMessage", chat_id=chat_id,
                         text=f"❌ Not found in recent posts of @{channel}")
        return
    results = []
    for lang in LANGUAGES:
        try:
            translated = await translate(session, post.text_html, lang)
            if translated is None:
                results.append(f"{lang.code}: ❌ translation failed")
                continue
            ok = await publish_post(session, post, translated, lang)
            results.append(f"{lang.code}: {'✅' if ok else '❌'}")
            if ok:
                published = load_published(lang.code)
                published.add(post_id)
                save_published(lang.code, published)
        except Exception as e:
            results.append(f"{lang.code}: ❌ {e}")
    await tg_request(session, "sendMessage", chat_id=chat_id,
                     text=f"Force {post_id}:\n" + "\n".join(results))


# ─── Main loop ───────────────────────────────────────────────────────────────

async def process_post(session, post, lang, seen, published, hashes):
    ch = content_hash(post.text_plain)
    if ch in hashes:
        log.info("Skipping %s (dupe) for %s", post.post_id, lang.code)
        return False, True
    log.info("Translating %s → %s", post.post_id, lang.code)
    try:
        # T32: document-only post with little/no text — publish without translation
        if len(post.text_plain.strip()) < 10 and getattr(post, "documents", None):
            ok = await publish_post(session, post, "", lang)
            if ok:
                hashes.add(ch)
            return ok, False
        translated = await translate(session, post.text_html, lang)
        # T22: fail-closed — if translation returned None, don't publish
        if translated is None:
            log.warning("Translation failed for %s → %s, not publishing", post.post_id, lang.code)
            return False, False
        ok = await publish_post(session, post, translated, lang)
        if ok:
            hashes.add(ch)
        return ok, False
    except Exception as e:
        log.error("Failed %s for %s: %s", post.post_id, lang.code, e)
        return False, False


async def process_cycle():
    global _zero_fetch_count

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if health.today_date != today:
        health.today_date = today
        health.today_published = 0
        health.today_errors = 0

    cycle_published = 0
    cycle_errors = 0

    async with aiohttp.ClientSession() as session:
        await handle_commands(session)

        all_posts: list[Post] = []
        for ch in SOURCE_CHANNELS:
            posts = await fetch_channel_posts(session, ch)
            # T26: Gap detection
            if posts:
                max_id = max(p.message_id for p in posts)
                min_id = min(p.message_id for p in posts)
                prev_max = _last_seen_ids.get(ch)
                if prev_max is not None and min_id > prev_max + 1:
                    gap = min_id - prev_max - 1
                    log.warning("T26: Gap detected in @%s: %d posts may be missed (IDs %d-%d)",
                                ch, gap, prev_max + 1, min_id - 1)
                    if gap > 5:
                        await send_admin_alert(session,
                            f"⚠️ Gap in @{ch}: ~{gap} posts missed (IDs {prev_max+1}-{min_id-1})")
                _last_seen_ids[ch] = max_id
            all_posts.extend(posts)
            await asyncio.sleep(1)

        if not all_posts:
            _zero_fetch_count += 1
            log.warning("No posts fetched (%d consecutive)", _zero_fetch_count)
            if _zero_fetch_count >= _ZERO_FETCH_ALERT:
                await send_admin_alert(session,
                    f"⚠️ {_zero_fetch_count} cycles with 0 posts.")
                _zero_fetch_count = 0
            health.last_cycle_ts = time.time()
            health.last_cycle_published = 0
            health.last_cycle_errors = 0
            return
        _zero_fetch_count = 0

        post_map = {p.post_id: p for p in all_posts}
        all_posts.sort(key=lambda p: p.post_id)

        for lang in LANGUAGES:
            seen = load_seen(lang.code)
            published = load_published(lang.code)
            hashes = load_content_hashes(lang.code)

            for post in all_posts:
                if post.post_id in seen:
                    continue
                ok, is_dupe = await process_post(
                    session, post, lang, seen, published, hashes)
                seen.add(post.post_id)
                if ok:
                    published.add(post.post_id)
                    cycle_published += 1
                    _channel_published[post.channel] = _channel_published.get(post.channel, 0) + 1  # T33
                    clear_failed(lang.code, post.post_id)
                elif not is_dupe:
                    mark_failed(lang.code, post.post_id)
                    cycle_errors += 1
                save_seen(lang.code, seen)
                save_published(lang.code, published)
                save_content_hashes(lang.code, hashes)
                await asyncio.sleep(1)

            retryable = get_retryable(lang.code)
            for post_id in retryable:
                if post_id in published:
                    clear_failed(lang.code, post_id)
                    continue
                post = post_map.get(post_id)
                if not post:
                    continue
                log.info("Retrying %s → %s", post_id, lang.code)
                ok, _ = await process_post(
                    session, post, lang, seen, published, hashes)
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
                log.info("Published %d to %s", cycle_published, lang.chat_id)

    health.last_cycle_ts = time.time()
    health.last_cycle_published = cycle_published
    health.last_cycle_errors = cycle_errors
    health.total_published += cycle_published
    health.today_published += cycle_published
    health.today_errors += cycle_errors


async def seed_initial_state():
    """First run: mark all existing posts as seen (don't flood channels)."""
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
                    published.add(p.post_id)
                    hashes.add(content_hash(p.text_plain))
                save_seen(lang.code, seen)
                save_published(lang.code, published)
                save_content_hashes(lang.code, hashes)
            await asyncio.sleep(1)
    log.info("Initial state saved.")


async def main():
    log.info("Bot starting. Polling every %ds", POLL_INTERVAL)
    log.info("Sources: %s", ", ".join(f"@{c}" for c in SOURCE_CHANNELS))
    log.info("Targets: %s", ", ".join(l.chat_id for l in LANGUAGES))

    # T31: Wire DeepL 80%-limit alerts to admin
    async def _deepl_alert(message):
        async with aiohttp.ClientSession() as s:
            await send_admin_alert(s, message)
    set_alert_callback(_deepl_alert)

    # T34: Clean old cache entries on startup
    cleanup_cache(max_age_days=30)

    await health.start_health_server()

    if not has_any_state():
        await seed_initial_state()

    cycles = 0
    while True:
        try:
            await process_cycle()
        except Exception as e:
            log.error("Cycle error: %s", e, exc_info=True)
        # T34: Periodic cache cleanup every ~24h (96 cycles at 15-min interval)
        cycles += 1
        if cycles % 96 == 0:
            cleanup_cache(max_age_days=30)
        log.info("Sleeping %ds until next cycle", POLL_INTERVAL)
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())

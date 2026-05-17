"""
publisher.py — Send translated posts to Telegram channels.
Supports photos, albums, video references. Fallback to plain text on HTML errors.
Sends admin alerts on repeated failures.
"""

import asyncio
import logging
import re
from html import escape as html_escape
from typing import Optional

import aiohttp

from bot.config import (
    BOT_TOKEN, CAPTION_LIMIT, MESSAGE_LIMIT, ADMIN_CHAT_ID,
    LangConfig, CHANNEL_NAMES,
)
from bot.scraper import Post

log = logging.getLogger("tg-aggregator")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Track consecutive failures for alerting
_consecutive_failures = 0
_ALERT_THRESHOLD = 5


async def tg_request(
    session: aiohttp.ClientSession, method: str, **kwargs
) -> Optional[dict]:
    url = f"{TG_API}/{method}"
    try:
        async with session.post(url, json=kwargs,
                                timeout=aiohttp.ClientTimeout(total=30)) as resp:
            data = await resp.json()
            if not data.get("ok"):
                log.error("TG API %s failed: %s", method, data.get("description"))
                return None
            return data.get("result")
    except Exception as e:
        log.error("TG API %s error: %s", method, e)
        return None


async def send_admin_alert(session: aiohttp.ClientSession, message: str):
    """Send alert to admin's personal chat."""
    if not ADMIN_CHAT_ID:
        return
    try:
        await tg_request(
            session, "sendMessage",
            chat_id=ADMIN_CHAT_ID,
            text=f"⚠️ Bot Alert\n\n{message}",
            disable_web_page_preview=True,
        )
    except Exception:
        log.error("Failed to send admin alert")


async def track_failure(session: aiohttp.ClientSession, post_id: str, error: str):
    """Track failures and alert admin if threshold exceeded."""
    global _consecutive_failures
    _consecutive_failures += 1
    if _consecutive_failures >= _ALERT_THRESHOLD:
        await send_admin_alert(
            session,
            f"🔴 {_consecutive_failures} consecutive publish failures!\n"
            f"Last: {post_id}\nError: {error}"
        )
        _consecutive_failures = 0  # reset after alert


def reset_failure_counter():
    global _consecutive_failures
    _consecutive_failures = 0


def _sanitize_html(text: str) -> str:
    """Last-resort: if HTML is severely broken, strip all tags."""
    opens = len(re.findall(r"<[a-zA-Z]", text))
    closes = len(re.findall(r"</[a-zA-Z]", text))
    if abs(opens - closes) > 3:
        log.warning("HTML severely broken (opens=%d, closes=%d), stripping", opens, closes)
        text = re.sub(r"<[^>]+>", "", text)
    return text


def _build_header(post: Post, lang: LangConfig) -> str:
    """Channel name at the top of the post."""
    names = CHANNEL_NAMES.get(lang.code, {})
    display_name = names.get(post.channel, f"@{post.channel}")
    channel_url = f"https://t.me/{post.channel}"
    return f'<b><a href="{channel_url}">{html_escape(display_name)}</a></b>\n\n'


def _build_footer(post: Post, lang: LangConfig) -> str:
    """Source link at the bottom."""
    url = f"https://t.me/{post.post_id}"
    names = CHANNEL_NAMES.get(lang.code, {})
    display_name = names.get(post.channel, f"@{post.channel}")
    return f'\n\n{lang.source_label}: <a href="{url}">{html_escape(display_name)}</a>'


def _split_message(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text[:limit].rsplit("\n\n", 1)
        if len(cut) == 2 and len(cut[0]) > limit // 3:
            chunks.append(cut[0])
            text = cut[1]
        else:
            cut = text[:limit].rsplit("\n", 1)
            if len(cut) == 2 and len(cut[0]) > limit // 3:
                chunks.append(cut[0])
                text = cut[1]
            else:
                cut = text[:limit].rsplit(" ", 1)
                chunks.append(cut[0])
                text = cut[1] if len(cut) == 2 else ""
    return chunks


async def _send_text(session, chat_id: str, text: str) -> bool:
    """Send text message. Fallback to plain text if HTML fails. Returns success."""
    result = await tg_request(
        session, "sendMessage",
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    if result is not None:
        return True
    if "<" in text:
        log.info("Retrying as plain text (HTML parse failed)")
        plain = re.sub(r"<[^>]+>", "", text)
        result = await tg_request(
            session, "sendMessage",
            chat_id=chat_id,
            text=plain,
            disable_web_page_preview=True,
        )
        return result is not None
    return False


async def _send_photo(session, chat_id: str, photo_url: str,
                      caption: str) -> bool:
    """Send photo with caption. Returns success."""
    result = await tg_request(
        session, "sendPhoto",
        chat_id=chat_id,
        photo=photo_url,
        caption=caption,
        parse_mode="HTML",
    )
    if result is not None:
        return True
    # Retry caption without HTML
    if "<" in caption:
        plain = re.sub(r"<[^>]+>", "", caption)
        result = await tg_request(
            session, "sendPhoto",
            chat_id=chat_id,
            photo=photo_url,
            caption=plain,
        )
        if result is not None:
            return True
    return False


async def _send_album(session, chat_id: str, photos: list[str],
                      caption: str) -> bool:
    """Send media group (album). Caption on first photo."""
    media = []
    for i, url in enumerate(photos[:10]):  # TG limit: 10 media per group
        item = {"type": "photo", "media": url}
        if i == 0:
            item["caption"] = caption
            item["parse_mode"] = "HTML"
        media.append(item)

    result = await tg_request(
        session, "sendMediaGroup",
        chat_id=chat_id,
        media=media,
    )
    if result is not None:
        return True
    # Fallback: retry first photo with plain caption
    if "<" in caption:
        media[0]["caption"] = re.sub(r"<[^>]+>", "", caption)
        del media[0]["parse_mode"]
        result = await tg_request(
            session, "sendMediaGroup",
            chat_id=chat_id,
            media=media,
        )
        if result is not None:
            return True
    return False


async def publish_post(
    session: aiohttp.ClientSession,
    post: Post,
    translated_html: str,
    lang: LangConfig,
) -> bool:
    """Send one translated post. Returns True if published successfully."""
    translated_html = _sanitize_html(translated_html)
    header = _build_header(post, lang)
    footer = _build_footer(post, lang)
    full_text = header + translated_html + footer

    success = False

    # Album (multiple photos)
    if post.album_photos and len(post.album_photos) > 1:
        if len(full_text) <= CAPTION_LIMIT:
            success = await _send_album(session, lang.chat_id, post.album_photos, full_text)
        else:
            # Caption too long: send album with short caption, then text
            short = header + translated_html[:CAPTION_LIMIT - len(header) - len(footer) - 10]
            short = short.rsplit(" ", 1)[0] + "…" + footer
            success = await _send_album(session, lang.chat_id, post.album_photos, short)
            if success:
                await asyncio.sleep(0.5)
                remainder = f"<b>{lang.part2_label}</b>\n\n" + translated_html + footer
                for chunk in _split_message(remainder, MESSAGE_LIMIT):
                    await _send_text(session, lang.chat_id, chunk)
                    await asyncio.sleep(0.3)
        # Fallback: if album fails, try single photo
        if not success and post.album_photos:
            log.info("Album failed, trying single photo for %s", post.post_id)
            success = await _send_photo(session, lang.chat_id, post.album_photos[0],
                                        full_text if len(full_text) <= CAPTION_LIMIT else header + footer)

    # Single photo
    elif post.photo_url and post.video_url != "preview_only":
        if len(full_text) <= CAPTION_LIMIT:
            success = await _send_photo(session, lang.chat_id, post.photo_url, full_text)
            if not success:
                log.info("Photo failed, sending as text for %s", post.post_id)
                success = await _send_text(session, lang.chat_id, full_text)
        else:
            cutoff = CAPTION_LIMIT - len(header) - len(footer) - 10
            part1 = header + translated_html[:cutoff].rsplit(" ", 1)[0] + "…" + footer
            success = await _send_photo(session, lang.chat_id, post.photo_url, part1)
            if not success:
                success = await _send_text(session, lang.chat_id, part1)
            await asyncio.sleep(0.5)
            part2 = f"<b>{lang.part2_label}</b>\n\n" + translated_html[cutoff:] + footer
            for chunk in _split_message(part2, MESSAGE_LIMIT):
                await _send_text(session, lang.chat_id, chunk)
                await asyncio.sleep(0.3)
            success = True  # at least text part sent

    # Video post (can't forward video from preview, send as text with link)
    elif post.video_url:
        video_note = f'\n\n🎬 <a href="https://t.me/{post.post_id}">Video</a>'
        text_with_video = full_text + video_note
        for chunk in _split_message(text_with_video, MESSAGE_LIMIT):
            s = await _send_text(session, lang.chat_id, chunk)
            if s:
                success = True
            await asyncio.sleep(0.3)

    # Text only
    else:
        for chunk in _split_message(full_text, MESSAGE_LIMIT):
            s = await _send_text(session, lang.chat_id, chunk)
            if s:
                success = True
            await asyncio.sleep(0.3)

    if success:
        reset_failure_counter()
    else:
        await track_failure(session, post.post_id, "All send methods failed")

    return success

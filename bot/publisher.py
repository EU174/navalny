"""
publisher.py — Send translated posts to Telegram channels.

Strategy:
  1. If post has media (photo/video/album) → forwardMessage original, then send translation
  2. If text-only → send translated text with header + footer
  3. Fallback to plain text if HTML fails
  4. Admin alerts on repeated failures
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
    global _consecutive_failures
    _consecutive_failures += 1
    if _consecutive_failures >= _ALERT_THRESHOLD:
        await send_admin_alert(
            session,
            f"🔴 {_consecutive_failures} consecutive publish failures!\n"
            f"Last: {post_id}\nError: {error}"
        )
        _consecutive_failures = 0


def reset_failure_counter():
    global _consecutive_failures
    _consecutive_failures = 0


def _sanitize_html(text: str) -> str:
    """If HTML is severely broken, strip all tags."""
    opens = len(re.findall(r"<[a-zA-Z]", text))
    closes = len(re.findall(r"</[a-zA-Z]", text))
    if abs(opens - closes) > 3:
        log.warning("HTML severely broken (opens=%d, closes=%d), stripping", opens, closes)
        text = re.sub(r"<[^>]+>", "", text)
    return text


def _build_header(post: Post, lang: LangConfig) -> str:
    """Channel name at the top, bold with link to channel."""
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
    """Split at paragraph boundaries first, then line breaks, then spaces."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Try paragraph break
        cut = text[:limit].rsplit("\n\n", 1)
        if len(cut) == 2 and len(cut[0]) > limit // 3:
            chunks.append(cut[0])
            text = cut[1]
            continue
        # Try line break
        cut = text[:limit].rsplit("\n", 1)
        if len(cut) == 2 and len(cut[0]) > limit // 3:
            chunks.append(cut[0])
            text = cut[1]
            continue
        # Try space
        cut = text[:limit].rsplit(" ", 1)
        chunks.append(cut[0])
        text = cut[1] if len(cut) == 2 else ""
    return chunks


async def _send_text(session, chat_id: str, text: str) -> bool:
    """Send text message. Fallback to plain text if HTML fails."""
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


async def _forward_original(
    session: aiohttp.ClientSession,
    post: Post,
    chat_id: str,
) -> bool:
    """Forward original post from source channel (preserves all media)."""
    from_chat = f"@{post.channel}"
    result = await tg_request(
        session, "forwardMessage",
        chat_id=chat_id,
        from_chat_id=from_chat,
        message_id=post.message_id,
    )
    if result is not None:
        return True
    log.warning("forwardMessage failed for %s to %s", post.post_id, chat_id)
    return False


async def publish_post(
    session: aiohttp.ClientSession,
    post: Post,
    translated_html: str,
    lang: LangConfig,
) -> bool:
    """
    Publish strategy:
      - Media posts: forward original (keeps video/photo/album) + send translation below
      - Text-only: send translated text with header + footer
    """
    translated_html = _sanitize_html(translated_html)
    header = _build_header(post, lang)
    footer = _build_footer(post, lang)
    translation_text = header + translated_html + footer

    success = False

    if post.has_media:
        # Step 1: Forward original post (preserves all media)
        fwd_ok = await _forward_original(session, post, lang.chat_id)

        if fwd_ok:
            await asyncio.sleep(0.5)
            # Step 2: Send translation as reply-like text below
            for chunk in _split_message(translation_text, MESSAGE_LIMIT):
                s = await _send_text(session, lang.chat_id, chunk)
                if s:
                    success = True
                await asyncio.sleep(0.3)
        else:
            # Fallback: forward failed, send text-only with link to original
            log.info("Forward failed for %s, sending text with media link", post.post_id)
            media_link = f'\n\n🎬 <a href="https://t.me/{post.post_id}">Original post</a>'
            text_with_link = translation_text + media_link
            for chunk in _split_message(text_with_link, MESSAGE_LIMIT):
                s = await _send_text(session, lang.chat_id, chunk)
                if s:
                    success = True
                await asyncio.sleep(0.3)

    else:
        # Text-only post: just send translation
        for chunk in _split_message(translation_text, MESSAGE_LIMIT):
            s = await _send_text(session, lang.chat_id, chunk)
            if s:
                success = True
            await asyncio.sleep(0.3)

    if success:
        reset_failure_counter()
    else:
        await track_failure(session, post.post_id, "All send methods failed")

    return success

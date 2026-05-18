"""
publisher.py — T11: YouTube thumbnails, T12: subtitle links,
T13: copyMessage, T14: reply to copied media, alerts.
"""

import asyncio
import logging
import re
from html import escape as html_escape
from typing import Optional

import aiohttp

from bot.config import (
    BOT_TOKEN, MESSAGE_LIMIT, ADMIN_CHAT_ID,
    LangConfig, CHANNEL_NAMES,
)
from bot.scraper import Post

log = logging.getLogger("tg-aggregator")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

_consecutive_failures = 0
_ALERT_THRESHOLD = 5

# T12: YouTube subtitle language map
_YT_LANG_MAP = {"DE": "de", "EN-GB": "en", "FR": "fr"}


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
    opens = len(re.findall(r"<[a-zA-Z]", text))
    closes = len(re.findall(r"</[a-zA-Z]", text))
    if abs(opens - closes) > 3:
        log.warning("HTML severely broken, stripping")
        text = re.sub(r"<[^>]+>", "", text)
    return text


def _build_header(post: Post, lang: LangConfig) -> str:
    names = CHANNEL_NAMES.get(lang.code, {})
    display_name = names.get(post.channel, f"@{post.channel}")
    channel_url = f"https://t.me/{post.channel}"
    return f'<b><a href="{channel_url}">{html_escape(display_name)}</a></b>\n\n'


def _build_footer(post: Post, lang: LangConfig) -> str:
    url = f"https://t.me/{post.post_id}"
    names = CHANNEL_NAMES.get(lang.code, {})
    display_name = names.get(post.channel, f"@{post.channel}")
    return f'\n\n{lang.source_label}: <a href="{url}">{html_escape(display_name)}</a>'


def _apply_youtube_subtitle_link(text: str, youtube_id: Optional[str], lang: LangConfig) -> str:
    """T12: Replace YouTube URLs with subtitle-enabled versions."""
    if not youtube_id:
        return text
    yt_lang = _YT_LANG_MAP.get(lang.code, "en")
    new_url = f"https://www.youtube.com/watch?v={youtube_id}&cc_lang_pref={yt_lang}&cc_load_policy=1"
    # Replace short and long YouTube URLs
    text = re.sub(
        r"https?://(?:www\.)?(?:youtu\.be/|youtube\.com/watch\?v=)[a-zA-Z0-9_-]{11}[^\s<\"]*",
        new_url, text
    )
    return text


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
            continue
        cut = text[:limit].rsplit("\n", 1)
        if len(cut) == 2 and len(cut[0]) > limit // 3:
            chunks.append(cut[0])
            text = cut[1]
            continue
        cut = text[:limit].rsplit(" ", 1)
        chunks.append(cut[0])
        text = cut[1] if len(cut) == 2 else ""
    return chunks


async def _send_text(session, chat_id: str, text: str,
                     reply_to: Optional[int] = None) -> Optional[dict]:
    """Send text. Fallback to plain if HTML fails. Returns message result or None."""
    kwargs = dict(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    if reply_to:
        kwargs["reply_to_message_id"] = reply_to
        kwargs["allow_sending_without_reply"] = True

    result = await tg_request(session, "sendMessage", **kwargs)
    if result is not None:
        return result

    if "<" in text:
        log.info("Retrying as plain text (HTML parse failed)")
        kwargs["text"] = re.sub(r"<[^>]+>", "", text)
        kwargs.pop("parse_mode", None)
        result = await tg_request(session, "sendMessage", **kwargs)
        return result
    return None


async def _copy_original(
    session: aiohttp.ClientSession, post: Post, chat_id: str
) -> Optional[int]:
    """T13: Copy original post (no 'Forwarded from'). Returns new message_id."""
    result = await tg_request(
        session, "copyMessage",
        chat_id=chat_id,
        from_chat_id=f"@{post.channel}",
        message_id=post.message_id,
    )
    if result and "message_id" in result:
        return result["message_id"]
    log.warning("copyMessage failed for %s", post.post_id)
    return None


async def _send_yt_thumbnail(
    session, chat_id: str, youtube_id: str, caption: str,
    reply_to: Optional[int] = None
) -> Optional[dict]:
    """T11: Send YouTube thumbnail as photo."""
    thumb_url = f"https://img.youtube.com/vi/{youtube_id}/maxresdefault.jpg"
    kwargs = dict(
        chat_id=chat_id,
        photo=thumb_url,
        caption=caption[:1024],
        parse_mode="HTML",
    )
    if reply_to:
        kwargs["reply_to_message_id"] = reply_to
        kwargs["allow_sending_without_reply"] = True

    result = await tg_request(session, "sendPhoto", **kwargs)
    if result:
        return result
    # Try hqdefault if maxres doesn't exist
    kwargs["photo"] = f"https://img.youtube.com/vi/{youtube_id}/hqdefault.jpg"
    return await tg_request(session, "sendPhoto", **kwargs)


async def publish_post(
    session: aiohttp.ClientSession,
    post: Post,
    translated_html: str,
    lang: LangConfig,
) -> bool:
    """
    Publish strategy:
    - Media post: copyMessage (keeps video/photo) → reply with translation
    - YouTube post (no TG media): send YT thumbnail + translation
    - Text-only: send translated text
    """
    translated_html = _sanitize_html(translated_html)
    # T12: Apply YouTube subtitle links
    translated_html = _apply_youtube_subtitle_link(translated_html, post.youtube_id, lang)

    header = _build_header(post, lang)
    footer = _build_footer(post, lang)
    translation_text = header + translated_html + footer

    success = False

    if post.has_media:
        # T13: Copy original (preserves all media, no "Forwarded from")
        copied_msg_id = await _copy_original(session, post, lang.chat_id)

        if copied_msg_id:
            await asyncio.sleep(0.5)
            # T14: Reply to copied message with translation
            for chunk in _split_message(translation_text, MESSAGE_LIMIT):
                result = await _send_text(session, lang.chat_id, chunk,
                                          reply_to=copied_msg_id)
                if result:
                    success = True
                await asyncio.sleep(0.3)
        else:
            # Fallback: send text with link to original
            log.info("Copy failed for %s, sending text with link", post.post_id)
            link = f'\n\n📎 <a href="https://t.me/{post.post_id}">Original</a>'
            text_with_link = translation_text + link
            for chunk in _split_message(text_with_link, MESSAGE_LIMIT):
                result = await _send_text(session, lang.chat_id, chunk)
                if result:
                    success = True
                await asyncio.sleep(0.3)

    elif post.youtube_id:
        # T11: No TG media but has YouTube — send thumbnail + translation
        yt_lang = _YT_LANG_MAP.get(lang.code, "en")
        yt_url = f"https://www.youtube.com/watch?v={post.youtube_id}&cc_lang_pref={yt_lang}&cc_load_policy=1"

        # Send thumbnail
        short_caption = f'{header}▶️ <a href="{yt_url}">YouTube</a>{footer}'
        thumb_result = await _send_yt_thumbnail(
            session, lang.chat_id, post.youtube_id, short_caption
        )
        thumb_msg_id = thumb_result.get("message_id") if thumb_result else None

        if thumb_msg_id:
            await asyncio.sleep(0.5)
            # T14: Reply with full translation
            for chunk in _split_message(translation_text, MESSAGE_LIMIT):
                result = await _send_text(session, lang.chat_id, chunk,
                                          reply_to=thumb_msg_id)
                if result:
                    success = True
                await asyncio.sleep(0.3)
        else:
            # Fallback: just text
            for chunk in _split_message(translation_text, MESSAGE_LIMIT):
                result = await _send_text(session, lang.chat_id, chunk)
                if result:
                    success = True
                await asyncio.sleep(0.3)

    else:
        # Text-only post
        for chunk in _split_message(translation_text, MESSAGE_LIMIT):
            result = await _send_text(session, lang.chat_id, chunk)
            if result:
                success = True
            await asyncio.sleep(0.3)

    if success:
        reset_failure_counter()
    else:
        await track_failure(session, post.post_id, "All send methods failed")

    return success

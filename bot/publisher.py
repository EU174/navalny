"""
publisher.py — T19: no link preview anywhere, T20: photo+caption unified.
"""

import asyncio
import json
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

_consecutive_failures = 0
_ALERT_THRESHOLD = 5
_YT_LANG_MAP = {"DE": "de", "EN-GB": "en", "FR": "fr"}


async def tg_request(session, method, **kwargs) -> Optional[dict]:
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


async def tg_upload(session, method, file_field, file_data, filename, **params):
    url = f"{TG_API}/{method}"
    data = aiohttp.FormData()
    data.add_field(file_field, file_data, filename=filename, content_type="image/jpeg")
    for k, v in params.items():
        if v is not None:
            data.add_field(k, str(v))
    try:
        async with session.post(url, data=data,
                                timeout=aiohttp.ClientTimeout(total=60)) as resp:
            result = await resp.json()
            if not result.get("ok"):
                log.error("TG upload %s failed: %s", method, result.get("description"))
                return None
            return result.get("result")
    except Exception as e:
        log.error("TG upload %s error: %s", method, e)
        return None


async def download_image(session, url) -> Optional[bytes]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                log.warning("Download failed HTTP %d: %s", resp.status, url[:80])
                return None
            data = await resp.read()
            return data if len(data) > 1000 else None
    except Exception as e:
        log.error("Download error: %s", e)
        return None


async def send_admin_alert(session, message):
    if not ADMIN_CHAT_ID:
        return
    try:
        await tg_request(session, "sendMessage", chat_id=ADMIN_CHAT_ID,
                         text=f"⚠️ Bot Alert\n\n{message}", disable_web_page_preview=True)
    except Exception:
        pass


async def track_failure(session, post_id, error):
    global _consecutive_failures
    _consecutive_failures += 1
    if _consecutive_failures >= _ALERT_THRESHOLD:
        await send_admin_alert(session,
            f"🔴 {_consecutive_failures} failures!\nLast: {post_id}\nError: {error}")
        _consecutive_failures = 0


def reset_failure_counter():
    global _consecutive_failures
    _consecutive_failures = 0


def _sanitize_html(text):
    opens = len(re.findall(r"<[a-zA-Z]", text))
    closes = len(re.findall(r"</[a-zA-Z]", text))
    if abs(opens - closes) > 3:
        text = re.sub(r"<[^>]+>", "", text)
    return text


def _build_header(post, lang):
    names = CHANNEL_NAMES.get(lang.code, {})
    display_name = names.get(post.channel, f"@{post.channel}")
    channel_url = f"https://t.me/{post.channel}"
    return f'<b><a href="{channel_url}">{html_escape(display_name)}</a></b>\n\n'


def _build_footer(post, lang):
    url = f"https://t.me/{post.post_id}"
    names = CHANNEL_NAMES.get(lang.code, {})
    display_name = names.get(post.channel, f"@{post.channel}")
    return f'\n\n{lang.source_label}: <a href="{url}">{html_escape(display_name)}</a>'


def _apply_yt_subtitles(text, youtube_id, lang):
    if not youtube_id:
        return text
    yt_lang = _YT_LANG_MAP.get(lang.code, "en")
    new_url = f"https://www.youtube.com/watch?v={youtube_id}&cc_lang_pref={yt_lang}&cc_load_policy=1"
    text = re.sub(
        r"https?://(?:www\.)?(?:youtu\.be/|youtube\.com/watch\?v=)[a-zA-Z0-9_-]{11}[^\s<\"]*",
        new_url, text
    )
    return text


def _split_message(text, limit):
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        for sep in ["\n\n", "\n", " "]:
            cut = text[:limit].rsplit(sep, 1)
            if len(cut) == 2 and len(cut[0]) > limit // 3:
                chunks.append(cut[0])
                text = cut[1]
                break
        else:
            chunks.append(text[:limit])
            text = text[limit:]
    return chunks


# T19: ALL text sends have disable_web_page_preview=True
async def _send_text(session, chat_id, text, reply_to=None):
    kwargs = dict(chat_id=chat_id, text=text, parse_mode="HTML",
                  disable_web_page_preview=True)
    if reply_to:
        kwargs["reply_to_message_id"] = reply_to
        kwargs["allow_sending_without_reply"] = True
    result = await tg_request(session, "sendMessage", **kwargs)
    if result is not None:
        return result
    if "<" in text:
        log.info("Retrying as plain text")
        kwargs["text"] = re.sub(r"<[^>]+>", "", text)
        kwargs.pop("parse_mode", None)
        return await tg_request(session, "sendMessage", **kwargs)
    return None


async def _send_photo(session, chat_id, photo_data, caption, reply_to=None):
    """T20: Send photo with caption. Caption truncated to CAPTION_LIMIT."""
    params = {"chat_id": chat_id, "caption": caption[:CAPTION_LIMIT], "parse_mode": "HTML"}
    if reply_to:
        params["reply_to_message_id"] = str(reply_to)
        params["allow_sending_without_reply"] = "true"
    result = await tg_upload(session, "sendPhoto", "photo", photo_data, "photo.jpg", **params)
    if result:
        return result
    # Retry without HTML
    if "<" in caption:
        params["caption"] = re.sub(r"<[^>]+>", "", caption)[:CAPTION_LIMIT]
        params.pop("parse_mode", None)
        return await tg_upload(session, "sendPhoto", "photo", photo_data, "photo.jpg", **params)
    return None


async def _send_album(session, chat_id, photos_data, caption):
    """Send album. Caption on first photo."""
    url = f"{TG_API}/sendMediaGroup"
    form = aiohttp.FormData()
    media = []
    for i, photo_bytes in enumerate(photos_data[:10]):
        field = f"photo{i}"
        form.add_field(field, photo_bytes, filename=f"{field}.jpg", content_type="image/jpeg")
        item = {"type": "photo", "media": f"attach://{field}"}
        if i == 0:
            item["caption"] = caption[:CAPTION_LIMIT]
            item["parse_mode"] = "HTML"
        media.append(item)
    form.add_field("chat_id", str(chat_id))
    form.add_field("media", json.dumps(media))
    try:
        async with session.post(url, data=form,
                                timeout=aiohttp.ClientTimeout(total=60)) as resp:
            result = await resp.json()
            if not result.get("ok"):
                log.error("sendMediaGroup failed: %s", result.get("description"))
                return None
            msgs = result.get("result", [])
            return msgs[0] if msgs else None
    except Exception as e:
        log.error("sendMediaGroup error: %s", e)
        return None


async def publish_post(session, post, translated_html, lang) -> bool:
    """
    T20 strategy:
    - Photo(s) + text ≤1024: one message (photo + full caption)
    - Photo(s) + text >1024: photo + short caption, reply with full text
    - YouTube (no TG photo): YT thumbnail + caption
    - Video (TG): text + 🎬 link
    - Text only: just text
    """
    translated_html = _sanitize_html(translated_html)
    translated_html = _apply_yt_subtitles(translated_html, post.youtube_id, lang)

    header = _build_header(post, lang)
    footer = _build_footer(post, lang)
    full_text = header + translated_html + footer

    success = False

    # ── Download photos ──────────────────────────────────────────────────
    photos_data = []
    if post.photo_urls:
        for url in post.photo_urls:
            img = await download_image(session, url)
            if img:
                photos_data.append(img)
            await asyncio.sleep(0.2)

    # ── Album (multiple photos) ──────────────────────────────────────────
    if len(photos_data) > 1:
        if len(full_text) <= CAPTION_LIMIT:
            result = await _send_album(session, lang.chat_id, photos_data, full_text)
            if result:
                success = True
        else:
            # Album + short caption, then reply with full text
            short = header + footer
            result = await _send_album(session, lang.chat_id, photos_data, short)
            if result:
                msg_id = result.get("message_id")
                success = True
                await asyncio.sleep(0.5)
                for chunk in _split_message(full_text, MESSAGE_LIMIT):
                    await _send_text(session, lang.chat_id, chunk, reply_to=msg_id)
                    await asyncio.sleep(0.3)

        # Fallback to single photo if album fails
        if not success and photos_data:
            photos_data = [photos_data[0]]  # fall through to single photo

    # ── Single photo ─────────────────────────────────────────────────────
    if len(photos_data) == 1 and not success:
        if len(full_text) <= CAPTION_LIMIT:
            # T20: Everything in one message
            result = await _send_photo(session, lang.chat_id, photos_data[0], full_text)
            if result:
                success = True
        else:
            # Photo + short caption, then reply
            short = header + footer
            result = await _send_photo(session, lang.chat_id, photos_data[0], short)
            if result:
                msg_id = result.get("message_id")
                success = True
                await asyncio.sleep(0.5)
                for chunk in _split_message(full_text, MESSAGE_LIMIT):
                    await _send_text(session, lang.chat_id, chunk, reply_to=msg_id)
                    await asyncio.sleep(0.3)

    # ── YouTube thumbnail (no TG photos) ─────────────────────────────────
    if not success and not photos_data and post.youtube_id:
        yt_lang = _YT_LANG_MAP.get(lang.code, "en")
        yt_url = f"https://www.youtube.com/watch?v={post.youtube_id}&cc_lang_pref={yt_lang}&cc_load_policy=1"

        for quality in ["maxresdefault", "hqdefault"]:
            thumb_url = f"https://img.youtube.com/vi/{post.youtube_id}/{quality}.jpg"
            img = await download_image(session, thumb_url)
            if img:
                if len(full_text) <= CAPTION_LIMIT:
                    result = await _send_photo(session, lang.chat_id, img, full_text)
                    if result:
                        success = True
                else:
                    yt_caption = f'{header}▶️ <a href="{yt_url}">YouTube</a>{footer}'
                    result = await _send_photo(session, lang.chat_id, img, yt_caption)
                    if result:
                        msg_id = result.get("message_id")
                        success = True
                        await asyncio.sleep(0.5)
                        for chunk in _split_message(full_text, MESSAGE_LIMIT):
                            await _send_text(session, lang.chat_id, chunk, reply_to=msg_id)
                            await asyncio.sleep(0.3)
                break

    # ── Video (TG native) ────────────────────────────────────────────────
    if not success and not photos_data and post.has_video and not post.youtube_id:
        video_link = f'\n\n🎬 <a href="https://t.me/{post.post_id}">Video</a>'
        full_text += video_link

    # ── Text fallback ────────────────────────────────────────────────────
    if not success:
        for chunk in _split_message(full_text, MESSAGE_LIMIT):
            result = await _send_text(session, lang.chat_id, chunk)
            if result:
                success = True
            await asyncio.sleep(0.3)

    if success:
        reset_failure_counter()
    else:
        await track_failure(session, post.post_id, "All methods failed")

    return success

"""
publisher.py — Download photos + sendPhoto, YT thumbnails,
albums via sendMediaGroup, reply to media with translation.
No copyMessage (bot not admin of source channels).
"""

import asyncio
import io
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


async def tg_upload(
    session: aiohttp.ClientSession, method: str,
    file_field: str, file_data: bytes, filename: str,
    **params
) -> Optional[dict]:
    """Upload file via multipart form data."""
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


async def download_image(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    """Download image bytes from URL."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                log.warning("Download failed HTTP %d: %s", resp.status, url[:80])
                return None
            data = await resp.read()
            if len(data) < 1000:  # too small, probably error page
                return None
            return data
    except Exception as e:
        log.error("Download error: %s", e)
        return None


async def send_admin_alert(session: aiohttp.ClientSession, message: str):
    if not ADMIN_CHAT_ID:
        return
    try:
        await tg_request(session, "sendMessage", chat_id=ADMIN_CHAT_ID,
                         text=f"⚠️ Bot Alert\n\n{message}", disable_web_page_preview=True)
    except Exception:
        pass


async def track_failure(session: aiohttp.ClientSession, post_id: str, error: str):
    global _consecutive_failures
    _consecutive_failures += 1
    if _consecutive_failures >= _ALERT_THRESHOLD:
        await send_admin_alert(session,
            f"🔴 {_consecutive_failures} consecutive failures!\nLast: {post_id}\nError: {error}")
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
    if not youtube_id:
        return text
    yt_lang = _YT_LANG_MAP.get(lang.code, "en")
    new_url = f"https://www.youtube.com/watch?v={youtube_id}&cc_lang_pref={yt_lang}&cc_load_policy=1"
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


async def _send_text(session, chat_id: str, text: str,
                     reply_to: Optional[int] = None) -> Optional[dict]:
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


async def _send_photo_uploaded(
    session, chat_id: str, photo_data: bytes, caption: str,
    reply_to: Optional[int] = None
) -> Optional[dict]:
    """Send photo by uploading bytes."""
    params = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    if reply_to:
        params["reply_to_message_id"] = str(reply_to)
        params["allow_sending_without_reply"] = "true"
    result = await tg_upload(session, "sendPhoto", "photo", photo_data, "photo.jpg", **params)
    if result:
        return result
    # Retry without HTML
    if "<" in caption:
        params["caption"] = re.sub(r"<[^>]+>", "", caption)
        params.pop("parse_mode", None)
        return await tg_upload(session, "sendPhoto", "photo", photo_data, "photo.jpg", **params)
    return None


async def _send_album_uploaded(
    session, chat_id: str, photos_data: list[bytes], caption: str
) -> Optional[dict]:
    """Send album by uploading multiple photos."""
    url = f"{TG_API}/sendMediaGroup"
    data = aiohttp.FormData()
    media = []
    for i, photo_bytes in enumerate(photos_data[:10]):
        field_name = f"photo{i}"
        data.add_field(field_name, photo_bytes, filename=f"photo{i}.jpg", content_type="image/jpeg")
        item = {"type": "photo", "media": f"attach://{field_name}"}
        if i == 0:
            item["caption"] = caption[:CAPTION_LIMIT]
            item["parse_mode"] = "HTML"
        media.append(item)

    import json
    data.add_field("chat_id", str(chat_id))
    data.add_field("media", json.dumps(media))

    try:
        async with session.post(url, data=data,
                                timeout=aiohttp.ClientTimeout(total=60)) as resp:
            result = await resp.json()
            if not result.get("ok"):
                log.error("sendMediaGroup failed: %s", result.get("description"))
                return None
            # Returns list of messages, take first message_id
            msgs = result.get("result", [])
            return msgs[0] if msgs else None
    except Exception as e:
        log.error("sendMediaGroup error: %s", e)
        return None


async def publish_post(
    session: aiohttp.ClientSession,
    post: Post,
    translated_html: str,
    lang: LangConfig,
) -> bool:
    """
    Publish strategy:
    - Photos: download + upload via sendPhoto/sendMediaGroup, then translation as reply
    - YouTube: send thumbnail + translation as reply
    - Video (no YT): text + link to original
    - Text only: just send translation
    """
    translated_html = _sanitize_html(translated_html)
    translated_html = _apply_youtube_subtitle_link(translated_html, post.youtube_id, lang)

    header = _build_header(post, lang)
    footer = _build_footer(post, lang)
    translation_text = header + translated_html + footer

    success = False
    media_msg_id = None  # for reply

    # ── Album (multiple photos) ──────────────────────────────────────────
    if len(post.photo_urls) > 1:
        log.info("Downloading %d photos for album %s", len(post.photo_urls), post.post_id)
        photos_data = []
        for url in post.photo_urls:
            img = await download_image(session, url)
            if img:
                photos_data.append(img)
            await asyncio.sleep(0.2)

        if photos_data:
            short_caption = header + footer
            result = await _send_album_uploaded(session, lang.chat_id, photos_data, short_caption)
            if result:
                media_msg_id = result.get("message_id")
                success = True
            else:
                log.warning("Album upload failed for %s, trying single photo", post.post_id)
                # Fallback to first photo
                if photos_data:
                    result = await _send_photo_uploaded(
                        session, lang.chat_id, photos_data[0],
                        translation_text[:CAPTION_LIMIT]
                    )
                    if result:
                        media_msg_id = result.get("message_id")
                        success = True

    # ── Single photo ─────────────────────────────────────────────────────
    elif post.photo_urls:
        log.info("Downloading photo for %s", post.post_id)
        img = await download_image(session, post.photo_urls[0])
        if img:
            if len(translation_text) <= CAPTION_LIMIT:
                # Photo + full caption in one message
                result = await _send_photo_uploaded(
                    session, lang.chat_id, img, translation_text
                )
                if result:
                    success = True
                    # No need for reply, caption has everything
                    media_msg_id = None
            else:
                # Photo + short caption, then reply with full text
                short_caption = header + footer
                result = await _send_photo_uploaded(
                    session, lang.chat_id, img, short_caption
                )
                if result:
                    media_msg_id = result.get("message_id")
                    success = True
        else:
            log.warning("Photo download failed for %s", post.post_id)

    # ── YouTube (no TG photo) ────────────────────────────────────────────
    elif post.youtube_id and not post.photo_urls:
        yt_lang = _YT_LANG_MAP.get(lang.code, "en")
        yt_url = f"https://www.youtube.com/watch?v={post.youtube_id}&cc_lang_pref={yt_lang}&cc_load_policy=1"

        # Try maxresdefault, then hqdefault
        for quality in ["maxresdefault", "hqdefault"]:
            thumb_url = f"https://img.youtube.com/vi/{post.youtube_id}/{quality}.jpg"
            img = await download_image(session, thumb_url)
            if img:
                short_caption = f'{header}▶️ <a href="{yt_url}">YouTube</a>{footer}'
                result = await _send_photo_uploaded(
                    session, lang.chat_id, img, short_caption[:CAPTION_LIMIT]
                )
                if result:
                    media_msg_id = result.get("message_id")
                    success = True
                break

    # ── Video (TG native, no access to download) ────────────────────────
    elif post.has_video and not post.photo_urls:
        video_link = f'\n\n🎬 <a href="https://t.me/{post.post_id}">Video</a>'
        translation_text += video_link

    # ── Send translation text ────────────────────────────────────────────
    if media_msg_id:
        # Reply to media message
        await asyncio.sleep(0.5)
        for chunk in _split_message(translation_text, MESSAGE_LIMIT):
            result = await _send_text(session, lang.chat_id, chunk, reply_to=media_msg_id)
            if result:
                success = True
            await asyncio.sleep(0.3)
    elif not success:
        # No media sent, just text
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

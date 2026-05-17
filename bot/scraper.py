"""
scraper.py — Fetch posts from public Telegram channel previews.
Supports text, photos, videos, and albums.
"""

import re
import logging
from dataclasses import dataclass, field
from html import escape as html_escape
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup, NavigableString, Tag

from bot.config import MIN_POST_LENGTH

log = logging.getLogger("tg-aggregator")


@dataclass
class Post:
    channel: str
    post_id: str
    text_html: str
    text_plain: str
    photo_url: Optional[str] = None
    video_url: Optional[str] = None
    album_photos: list[str] = field(default_factory=list)


# ─── HTML conversion ─────────────────────────────────────────────────────────

def tg_html(el) -> str:
    """Convert BeautifulSoup element to Telegram-compatible HTML."""
    if isinstance(el, NavigableString):
        return html_escape(str(el))
    if not isinstance(el, Tag):
        return ""

    tag = el.name
    inner = "".join(tg_html(c) for c in el.children)

    if tag == "br":
        return "\n"
    if tag in ("b", "strong"):
        return f"<b>{inner}</b>"
    if tag in ("i", "em"):
        return f"<i>{inner}</i>"
    if tag == "u":
        return f"<u>{inner}</u>"
    if tag in ("s", "strike", "del"):
        return f"<s>{inner}</s>"
    if tag == "code":
        return f"<code>{inner}</code>"
    if tag == "pre":
        return f"<pre>{inner}</pre>"
    if tag == "a":
        href = el.get("href", "")
        return f'<a href="{html_escape(href)}">{inner}</a>' if href else inner
    if tag in ("p", "div", "blockquote"):
        return f"{inner}\n\n"
    return inner


def clean_html(text: str) -> str:
    """Strip unsupported tags and fix broken HTML from DeepL."""
    allowed = {"b", "strong", "i", "em", "u", "s", "strike", "del",
               "code", "pre", "a"}

    # 1. Remove non-allowed tags
    def _strip(m):
        tag_name = m.group(1).strip().split()[0].lower().lstrip("/")
        return m.group(0) if tag_name in allowed else ""
    text = re.sub(r"<(/?\s*[a-zA-Z][^>]*)>", _strip, text)

    # 2. Remove truncated tags (no closing >)
    text = re.sub(r"<[a-zA-Z][^>]*$", "", text)

    # 3. Remove empty tags
    text = re.sub(r"<(\w+)[^>]*>\s*</\1>", "", text)

    # 4. Remove <a> without href
    text = re.sub(r'<a(?:\s[^>]*)?>([^<]*)</a>', lambda m: (
        m.group(0) if 'href=' in m.group(0) else m.group(1)
    ), text)

    # 5. Balance unclosed/extra tags
    for tag in ["b", "i", "u", "s", "code", "pre"]:
        opens = len(re.findall(rf"<{tag}[\s>]", text, re.IGNORECASE))
        closes = len(re.findall(rf"</{tag}>", text, re.IGNORECASE))
        if opens > closes:
            for _ in range(opens - closes):
                text += f"</{tag}>"
        elif closes > opens:
            for _ in range(closes - opens):
                text = re.sub(rf"</{tag}>", "", text, count=1)

    # Balance <a> tags
    a_opens = len(re.findall(r"<a[\s>]", text, re.IGNORECASE))
    a_closes = len(re.findall(r"</a>", text, re.IGNORECASE))
    if a_opens > a_closes:
        for _ in range(a_opens - a_closes):
            text += "</a>"
    elif a_closes > a_opens:
        for _ in range(a_closes - a_opens):
            text = re.sub(r"</a>", "", text, count=1)

    # 6. Collapse multiple newlines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# ─── Channel scraper ─────────────────────────────────────────────────────────

def _extract_photo_url(el) -> Optional[str]:
    """Extract background-image URL from a style attribute."""
    if not el:
        return None
    style = el.get("style", "")
    m = re.search(r"url\(['\"]?(https://[^'\")\s]+)['\"]?\)", style)
    return m.group(1) if m else None


async def fetch_channel_posts(
    session: aiohttp.ClientSession, channel: str
) -> list[Post]:
    """Scrape t.me/s/<channel> and return list of Posts."""
    url = f"https://t.me/s/{channel}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                log.warning("HTTP %d for %s", resp.status, url)
                return []
            html = await resp.text()
    except Exception as e:
        log.error("Failed to fetch %s: %s", url, e)
        return []

    soup = BeautifulSoup(html, "html.parser")
    posts: list[Post] = []

    for msg_div in soup.select("div.tgme_widget_message_wrap"):
        msg = msg_div.select_one("div.tgme_widget_message")
        if not msg:
            continue

        data_post = msg.get("data-post", "")
        if "/" not in data_post:
            continue

        # Skip forwards
        if msg.select_one("a.tgme_widget_message_forwarded_from_name"):
            continue

        text_div = msg.select_one("div.tgme_widget_message_text")
        if not text_div:
            continue

        text_html = tg_html(text_div).strip()
        text_plain = text_div.get_text(separator="\n").strip()

        if len(text_plain) < MIN_POST_LENGTH:
            continue

        # Single photo
        photo_url = _extract_photo_url(
            msg.select_one("a.tgme_widget_message_photo_wrap")
        )

        # Album (multiple photos)
        album_photos = []
        for pw in msg.select("a.tgme_widget_message_photo_wrap"):
            u = _extract_photo_url(pw)
            if u:
                album_photos.append(u)
        # If single photo, don't duplicate in album
        if len(album_photos) <= 1:
            album_photos = []

        # Video
        video_url = None
        video_el = msg.select_one("video.tgme_widget_message_video")
        if video_el:
            video_url = video_el.get("src")
        # Also check for video wrap (thumbnail)
        if not video_url:
            video_wrap = msg.select_one("a.tgme_widget_message_video_wrap")
            if video_wrap:
                # Video exists but URL not directly available from preview
                # We'll note it and send text-only with link to original
                video_url = "preview_only"

        posts.append(Post(
            channel=channel,
            post_id=data_post,
            text_html=text_html,
            text_plain=text_plain,
            photo_url=photo_url if not album_photos else album_photos[0],
            video_url=video_url,
            album_photos=album_photos,
        ))

    log.info("Fetched %d posts from @%s", len(posts), channel)
    return posts

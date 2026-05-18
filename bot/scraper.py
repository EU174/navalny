"""
scraper.py — Fetch posts from public Telegram channel previews.
Detects media, extracts message_id, cleans embed artifacts.
"""

import re
import logging
from dataclasses import dataclass
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
    message_id: int
    text_html: str
    text_plain: str
    has_media: bool = False
    youtube_id: Optional[str] = None  # for T11/T12


# ─── HTML conversion ─────────────────────────────────────────────────────────

def tg_html(el) -> str:
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
    if tag in ("p", "div"):
        return f"{inner}\n\n"
    if tag == "blockquote":
        return f"{inner}\n\n"
    return inner


def _extract_youtube_id(text: str) -> Optional[str]:
    """Extract YouTube video ID from text."""
    m = re.search(
        r"(?:youtu\.be/|youtube\.com/watch\?v=|youtube\.com/embed/)([a-zA-Z0-9_-]{11})",
        text
    )
    return m.group(1) if m else None


def clean_html(text: str) -> str:
    """Strip unsupported tags, fix broken HTML, remove embed artifacts."""
    allowed = {"b", "strong", "i", "em", "u", "s", "strike", "del",
               "code", "pre", "a"}

    def _strip(m):
        tag_name = m.group(1).strip().split()[0].lower().lstrip("/")
        return m.group(0) if tag_name in allowed else ""
    text = re.sub(r"<(/?\s*[a-zA-Z][^>]*)>", _strip, text)
    text = re.sub(r"<[a-zA-Z][^>]*$", "", text)
    text = re.sub(r"<(\w+)[^>]*>\s*</\1>", "", text)
    text = re.sub(r'<a(?:\s[^>]*)?>([^<]*)</a>', lambda m: (
        m.group(0) if 'href=' in m.group(0) else m.group(1)
    ), text)

    for tag in ["b", "i", "u", "s", "code", "pre"]:
        opens = len(re.findall(rf"<{tag}[\s>]", text, re.IGNORECASE))
        closes = len(re.findall(rf"</{tag}>", text, re.IGNORECASE))
        if opens > closes:
            text += f"</{tag}>" * (opens - closes)
        elif closes > opens:
            for _ in range(closes - opens):
                text = re.sub(rf"</{tag}>", "", text, count=1)

    a_opens = len(re.findall(r"<a[\s>]", text, re.IGNORECASE))
    a_closes = len(re.findall(r"</a>", text, re.IGNORECASE))
    if a_opens > a_closes:
        text += "</a>" * (a_opens - a_closes)
    elif a_closes > a_opens:
        for _ in range(a_closes - a_opens):
            text = re.sub(r"</a>", "", text, count=1)

    # T10: Remove embed/link-preview artifacts
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        s = line.strip()
        # Skip standalone emoji-only lines (embed icons)
        if s and len(s) <= 3 and not s.isalnum():
            plain = re.sub(r"<[^>]+>", "", s)
            if all(ord(c) > 0x2000 or c in "❗️❤️🔥💪🎬📹📺🔴▶️⚡️" for c in plain.replace(" ", "")):
                continue
        # Skip "ChannelName | ButtonText" lines (embed buttons)
        plain_line = re.sub(r"<[^>]+>", "", s)
        if re.match(r"^.{1,40}\s*\|\s*.{1,30}$", plain_line):
            continue
        cleaned.append(line)
    text = "\n".join(cleaned)

    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ─── Channel scraper ─────────────────────────────────────────────────────────

async def fetch_channel_posts(
    session: aiohttp.ClientSession, channel: str
) -> list[Post]:
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
        try:
            message_id = int(data_post.split("/")[1])
        except (ValueError, IndexError):
            continue

        if msg.select_one("a.tgme_widget_message_forwarded_from_name"):
            continue

        # T10: Remove link preview blocks BEFORE extracting text
        for preview in msg.select("a.tgme_widget_message_link_preview"):
            preview.decompose()

        text_div = msg.select_one("div.tgme_widget_message_text")
        if not text_div:
            continue

        text_html = tg_html(text_div).strip()
        text_plain = text_div.get_text(separator="\n").strip()

        if len(text_plain) < MIN_POST_LENGTH:
            continue

        has_photo = bool(msg.select_one("a.tgme_widget_message_photo_wrap"))
        has_video = bool(
            msg.select_one("video.tgme_widget_message_video")
            or msg.select_one("a.tgme_widget_message_video_wrap")
            or msg.select_one("i.tgme_widget_message_video_thumb")
        )

        # T11: Extract YouTube ID
        youtube_id = _extract_youtube_id(text_plain)

        posts.append(Post(
            channel=channel,
            post_id=data_post,
            message_id=message_id,
            text_html=text_html,
            text_plain=text_plain,
            has_media=has_photo or has_video,
            youtube_id=youtube_id,
        ))

    log.info("Fetched %d posts from @%s", len(posts), channel)
    return posts

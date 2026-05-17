"""
scraper.py — Fetch posts from public Telegram channel previews.
Detects media (photo/video/album) for forwardMessage strategy.
Cleans up embed artifacts (YouTube buttons, link previews).
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
    post_id: str          # e.g. "teamnavalny/1234"
    message_id: int       # numeric part: 1234 (for forwardMessage)
    text_html: str        # Telegram-compatible HTML
    text_plain: str       # plain text
    has_media: bool = False  # True if post has photo/video/album


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
    if tag in ("p", "div"):
        return f"{inner}\n\n"
    if tag == "blockquote":
        return f"{inner}\n\n"
    return inner


def _clean_embed_artifacts(text: str) -> str:
    """Remove YouTube/link preview button-like artifacts from parsed text.
    These appear as 'Channel Name | Button Text' at the end of posts."""
    # Remove lines that look like embed buttons: "❗️\nChannel | Action"
    # or "ChannelName | Поддержать" / "| Support" etc.
    lines = text.split("\n")
    cleaned = []
    skip_rest = False
    for line in lines:
        stripped = line.strip()
        # Skip lines that are just emoji
        if stripped and all(c in "❗️❤️🔥💪🎬📹📺🔴▶️⚡️" or ord(c) > 0x1F000 for c in stripped.replace(" ", "")):
            continue
        # Skip "Channel | Action" button lines
        if re.match(r"^.{1,40}\s*\|\s*.{1,30}$", stripped) and not "<a " in stripped:
            skip_rest = True
            continue
        if skip_rest:
            continue
        cleaned.append(line)

    return "\n".join(cleaned)


def clean_html(text: str) -> str:
    """Strip unsupported tags, fix broken HTML, clean artifacts."""
    allowed = {"b", "strong", "i", "em", "u", "s", "strike", "del",
               "code", "pre", "a"}

    # 1. Remove non-allowed tags
    def _strip(m):
        tag_name = m.group(1).strip().split()[0].lower().lstrip("/")
        return m.group(0) if tag_name in allowed else ""
    text = re.sub(r"<(/?\s*[a-zA-Z][^>]*)>", _strip, text)

    # 2. Remove truncated tags
    text = re.sub(r"<[a-zA-Z][^>]*$", "", text)

    # 3. Remove empty tags
    text = re.sub(r"<(\w+)[^>]*>\s*</\1>", "", text)

    # 4. Remove <a> without href
    text = re.sub(r'<a(?:\s[^>]*)?>([^<]*)</a>', lambda m: (
        m.group(0) if 'href=' in m.group(0) else m.group(1)
    ), text)

    # 5. Balance tags
    for tag in ["b", "i", "u", "s", "code", "pre"]:
        opens = len(re.findall(rf"<{tag}[\s>]", text, re.IGNORECASE))
        closes = len(re.findall(rf"</{tag}>", text, re.IGNORECASE))
        if opens > closes:
            for _ in range(opens - closes):
                text += f"</{tag}>"
        elif closes > opens:
            for _ in range(closes - opens):
                text = re.sub(rf"</{tag}>", "", text, count=1)

    a_opens = len(re.findall(r"<a[\s>]", text, re.IGNORECASE))
    a_closes = len(re.findall(r"</a>", text, re.IGNORECASE))
    if a_opens > a_closes:
        for _ in range(a_opens - a_closes):
            text += "</a>"
    elif a_closes > a_opens:
        for _ in range(a_closes - a_opens):
            text = re.sub(r"</a>", "", text, count=1)

    # 6. Clean embed artifacts
    text = _clean_embed_artifacts(text)

    # 7. Normalize newlines: keep paragraph breaks, trim excess
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Ensure single \n stays (line breaks within paragraphs)
    # Don't collapse \n to space

    return text.strip()


# ─── Channel scraper ─────────────────────────────────────────────────────────

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

        # Extract numeric message_id
        try:
            message_id = int(data_post.split("/")[1])
        except (ValueError, IndexError):
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

        # Detect any media
        has_photo = bool(msg.select_one("a.tgme_widget_message_photo_wrap"))
        has_video = bool(
            msg.select_one("video.tgme_widget_message_video")
            or msg.select_one("a.tgme_widget_message_video_wrap")
            or msg.select_one("i.tgme_widget_message_video_thumb")
        )
        has_media = has_photo or has_video

        posts.append(Post(
            channel=channel,
            post_id=data_post,
            message_id=message_id,
            text_html=text_html,
            text_plain=text_plain,
            has_media=has_media,
        ))

    log.info("Fetched %d posts from @%s", len(posts), channel)
    return posts

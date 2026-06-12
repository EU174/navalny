"""
scraper.py — T18: aggressive embed artifact removal.
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
    message_id: int
    text_html: str
    text_plain: str
    photo_urls: list[str] = field(default_factory=list)
    has_video: bool = False
    youtube_id: Optional[str] = None
    # T32: documents (PDF etc.) attached to the post
    documents: list[dict] = field(default_factory=list)  # [{title, url}]


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
    if tag in ("p", "div", "blockquote"):
        return f"{inner}\n\n"
    return inner


def _extract_youtube_id(text: str) -> Optional[str]:
    m = re.search(
        r"(?:youtu\.be/|youtube\.com/watch\?v=|youtube\.com/embed/)([a-zA-Z0-9_-]{11})",
        text
    )
    return m.group(1) if m else None


def _extract_photo_url(el) -> Optional[str]:
    if not el:
        return None
    style = el.get("style", "")
    m = re.search(r"url\(['\"]?(https://[^'\")\s]+)['\"]?\)", style)
    return m.group(1) if m else None


def _is_junk_line(line: str) -> bool:
    """Check if a line is an embed artifact (emoji-only, channel name, button, etc.)."""
    s = line.strip()
    if not s:
        return False

    # Strip HTML tags for analysis
    plain = re.sub(r"<[^>]+>", "", s).strip()
    if not plain:
        return True  # empty after stripping tags

    # Single emoji or 1-3 char emoji-like
    if len(plain) <= 4:
        # Check if all chars are emoji/symbols (non-letter, non-digit)
        if not any(c.isalnum() for c in plain):
            return True

    # "ChannelName | ButtonText" pattern
    if re.match(r"^.{1,50}\s*\|\s*.{1,30}$", plain):
        return True

    # Known channel footer patterns (Russian)
    footer_patterns = [
        r"^Команда Навального$",
        r"^Навальный LIVE$",
        r"^Навальный$",
        r"^\|?\s*Поддержать\s*$",
        r"^\|?\s*Support\s*(us)?\s*$",
        r"^\|?\s*Подписаться\s*$",
        r"^\|?\s*Subscribe\s*$",
        r"^\|?\s*Soutenir\s*$",
        r"^\|?\s*Unterstützen\s*$",
    ]
    for pat in footer_patterns:
        if re.match(pat, plain, re.IGNORECASE):
            return True

    return False


def _clean_trailing_junk(text: str) -> str:
    """T18: Remove trailing embed artifacts aggressively.
    Walk backwards from end, remove junk lines until we hit real content."""
    lines = text.split("\n")

    # Find last line with real content (>10 chars of actual text, not just emoji/links)
    last_real = -1
    for i in range(len(lines) - 1, -1, -1):
        plain = re.sub(r"<[^>]+>", "", lines[i]).strip()
        if len(plain) > 10 and any(c.isalpha() for c in plain):
            # Check it's not a known footer
            if not _is_junk_line(lines[i]):
                last_real = i
                break

    if last_real >= 0:
        lines = lines[:last_real + 1]

    return "\n".join(lines)


def clean_html(text: str) -> str:
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

    # T18: Remove junk lines (emoji-only, channel names, buttons)
    lines = text.split("\n")
    cleaned = [line for line in lines if not _is_junk_line(line)]
    text = "\n".join(cleaned)

    # T18: Remove trailing junk
    text = _clean_trailing_junk(text)

    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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

        # Remove link preview blocks BEFORE text extraction
        for preview in msg.select("a.tgme_widget_message_link_preview"):
            preview.decompose()

        text_div = msg.select_one("div.tgme_widget_message_text")
        text_html = tg_html(text_div).strip() if text_div else ""
        text_plain = text_div.get_text(separator="\n").strip() if text_div else ""

        # T32: Detect document attachments (PDF etc.) — done early so
        # document-only posts with little text still pass the length gate.
        documents = []
        for doc in msg.select("a.tgme_widget_message_document_wrap"):
            href = doc.get("href", "")
            title_el = doc.select_one("div.tgme_widget_message_document_title")
            title = title_el.get_text(strip=True) if title_el else "Document"
            if href:
                documents.append({"title": title, "url": href})

        # Skip short posts UNLESS they carry a document attachment
        if len(text_plain) < MIN_POST_LENGTH and not documents:
            continue

        photo_urls = []
        for pw in msg.select("a.tgme_widget_message_photo_wrap"):
            u = _extract_photo_url(pw)
            if u:
                photo_urls.append(u)

        has_video = bool(
            msg.select_one("video.tgme_widget_message_video")
            or msg.select_one("a.tgme_widget_message_video_wrap")
            or msg.select_one("i.tgme_widget_message_video_thumb")
        )

        youtube_id = _extract_youtube_id(text_plain)

        posts.append(Post(
            channel=channel,
            post_id=data_post,
            message_id=message_id,
            text_html=text_html,
            text_plain=text_plain,
            photo_urls=photo_urls,
            has_video=has_video,
            youtube_id=youtube_id,
            documents=documents,  # T32
        ))

    log.info("Fetched %d posts from @%s", len(posts), channel)
    return posts

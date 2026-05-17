"""
scraper.py — Fetch posts from public Telegram channel previews.

Edit this file to:
  - Fix parsing if Telegram changes HTML structure
  - Add support for videos, documents, polls
  - Change forwarded-post detection
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


# ─── Data model ──────────────────────────────────────────────────────────────

@dataclass
class Post:
    channel: str
    post_id: str            # e.g. "teamnavalny/1234"
    text_html: str          # Telegram-compatible HTML
    text_plain: str         # plain text for length checks
    photo_url: Optional[str] = None


# ─── HTML conversion ────────────────────────────────────────────────────────

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
    """Strip tags not supported by Telegram."""
    allowed = {"b", "strong", "i", "em", "u", "s", "strike", "del",
               "code", "pre", "a"}
    def _strip(m):
        tag_name = m.group(1).strip().split()[0].lower().lstrip("/")
        return m.group(0) if tag_name in allowed else ""
    return re.sub(r"<(/?\s*[a-zA-Z][^>]*)>", _strip, text).strip()


# ─── Channel scraper ────────────────────────────────────────────────────────

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

        # Photo
        photo_url = None
        photo_wrap = msg.select_one("a.tgme_widget_message_photo_wrap")
        if photo_wrap:
            style = photo_wrap.get("style", "")
            m = re.search(r"url\(['\"]?(https://[^'\")\s]+)['\"]?\)", style)
            if m:
                photo_url = m.group(1)

        posts.append(Post(
            channel=channel,
            post_id=data_post,
            text_html=text_html,
            text_plain=text_plain,
            photo_url=photo_url,
        ))

    log.info("Fetched %d posts from @%s", len(posts), channel)
    return posts

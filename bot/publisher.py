"""
publisher.py — Send translated posts to Telegram channels.

Edit this file to:
  - Change message format / footer style
  - Add video/document forwarding
  - Adjust caption splitting logic
  - Add inline keyboards or reactions
"""

import asyncio
import logging
from html import escape as html_escape
from typing import Optional

import aiohttp

from bot.config import BOT_TOKEN, CAPTION_LIMIT, MESSAGE_LIMIT, LangConfig, CHANNEL_NAMES
from bot.scraper import Post

log = logging.getLogger("tg-aggregator")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


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


def _build_footer(post: Post, lang: LangConfig) -> str:
    """Build footer: localized channel name as hyperlink to original post."""
    url = f"https://t.me/{post.post_id}"
    # Get localized channel name, fallback to @channel
    names = CHANNEL_NAMES.get(lang.code, {})
    display_name = names.get(post.channel, f"@{post.channel}")
    return f'\n\n{lang.source_label}: <a href="{url}">{html_escape(display_name)}</a>'


def _split_message(text: str, limit: int) -> list[str]:
    """Split long text at paragraph or word boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text[:limit].rsplit("\n", 1)
        if len(cut) == 2 and len(cut[0]) > limit // 2:
            chunks.append(cut[0])
            text = cut[1]
        else:
            cut = text[:limit].rsplit(" ", 1)
            chunks.append(cut[0])
            text = cut[1] if len(cut) == 2 else ""
    return chunks


async def publish_post(
    session: aiohttp.ClientSession,
    post: Post,
    translated_html: str,
    lang: LangConfig,
):
    """Send one translated post to a target channel."""
    footer = _build_footer(post, lang)
    full_text = translated_html + footer

    if post.photo_url:
        if len(full_text) <= CAPTION_LIMIT:
            await tg_request(
                session, "sendPhoto",
                chat_id=lang.chat_id,
                photo=post.photo_url,
                caption=full_text,
                parse_mode="HTML",
            )
        else:
            # Photo + truncated caption, then continuation
            cutoff = CAPTION_LIMIT - len(footer) - 10
            part1 = translated_html[:cutoff].rsplit(" ", 1)[0] + "…" + footer

            await tg_request(
                session, "sendPhoto",
                chat_id=lang.chat_id,
                photo=post.photo_url,
                caption=part1,
                parse_mode="HTML",
            )
            await asyncio.sleep(0.5)

            part2 = f"<b>{lang.part2_label}</b>\n\n" + translated_html[cutoff:] + footer
            for chunk in _split_message(part2, MESSAGE_LIMIT):
                await tg_request(
                    session, "sendMessage",
                    chat_id=lang.chat_id,
                    text=chunk,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                await asyncio.sleep(0.3)
    else:
        for chunk in _split_message(full_text, MESSAGE_LIMIT):
            await tg_request(
                session, "sendMessage",
                chat_id=lang.chat_id,
                text=chunk,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await asyncio.sleep(0.3)

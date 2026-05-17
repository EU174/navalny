"""
translator.py — DeepL (primary) + Google Translate (fallback).

Edit this file to:
  - Switch to DeepL Pro API (change URL)
  - Add another fallback (LibreTranslate, etc.)
  - Tweak name transliteration rules
  - Add glossary / do-not-translate lists
"""

import logging
from typing import Optional

import aiohttp

from bot.config import DEEPL_API_KEY, LangConfig
from bot.scraper import clean_html

log = logging.getLogger("tg-aggregator")


async def translate_deepl(
    session: aiohttp.ClientSession, text: str, target_lang: str
) -> Optional[str]:
    """DeepL Free API. Returns None on any failure."""
    if not DEEPL_API_KEY:
        return None

    # Free: api-free.deepl.com  |  Pro: api.deepl.com
    url = "https://api-free.deepl.com/v2/translate"
    payload = {
        "text": [text],
        "source_lang": "RU",
        "target_lang": target_lang,
        "tag_handling": "html",
        "ignore_tags": ["a"],
    }
    headers = {"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"}
    try:
        async with session.post(url, json=payload, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.warning("DeepL HTTP %d: %s", resp.status, body[:300])
                return None
            data = await resp.json()
            return data["translations"][0]["text"]
    except Exception as e:
        log.error("DeepL error: %s", e)
        return None


async def translate_google(
    session: aiohttp.ClientSession, text: str, target_lang: str
) -> Optional[str]:
    """Google Translate (unofficial, no API key). Fallback only."""
    gl_map = {"DE": "de", "EN-GB": "en", "FR": "fr"}
    tl = gl_map.get(target_lang, target_lang.lower().split("-")[0])
    url = "https://translate.googleapis.com/translate_a/single"
    params = {"client": "gtx", "sl": "ru", "tl": tl, "dt": "t", "q": text}
    try:
        async with session.get(url, params=params,
                               timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            return "".join(seg[0] for seg in data[0] if seg[0])
    except Exception as e:
        log.error("Google Translate error: %s", e)
        return None


def apply_name_fixes(text: str, lang: LangConfig) -> str:
    """Post-translation name transliteration corrections."""
    for src, dst in lang.name_fixes.items():
        text = text.replace(src, dst)
    return text


async def translate(
    session: aiohttp.ClientSession, text: str, lang: LangConfig
) -> str:
    """Translate text with fallback chain: DeepL → Google → original."""
    translated = await translate_deepl(session, text, lang.code)

    if translated is None:
        log.info("DeepL unavailable, trying Google Translate for %s", lang.code)
        translated = await translate_google(session, text, lang.code)

    if translated is None:
        log.error("All translation backends failed for %s", lang.code)
        return text  # return original as last resort

    translated = apply_name_fixes(translated, lang)
    return clean_html(translated)

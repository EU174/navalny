"""
translator.py — DeepL (primary) + Google Translate (fallback).
Glossary protection: wraps terms and hashtags so they aren't translated.
"""

import logging
import re
from typing import Optional

import aiohttp

from bot.config import DEEPL_API_KEY, GLOSSARY, LangConfig
from bot.scraper import clean_html

log = logging.getLogger("tg-aggregator")

# Placeholder markers for glossary terms
_GLOSS_PREFIX = "\u2063GLOSS"  # invisible separator + GLOSS
_GLOSS_SUFFIX = "SSOLG\u2063"


def _protect_glossary(text: str) -> tuple[str, dict]:
    """Wrap glossary terms and hashtags in markers so DeepL skips them."""
    replacements = {}
    counter = 0

    # Protect hashtags first (#протест, #FreeNavalny, etc.)
    def _replace_hashtag(m):
        nonlocal counter
        key = f"{_GLOSS_PREFIX}{counter}{_GLOSS_SUFFIX}"
        replacements[key] = m.group(0)
        counter += 1
        return key
    text = re.sub(r"#\w+", _replace_hashtag, text)

    # Protect glossary terms (longest first to avoid partial matches)
    for term in sorted(GLOSSARY, key=len, reverse=True):
        if term in text:
            key = f"{_GLOSS_PREFIX}{counter}{_GLOSS_SUFFIX}"
            replacements[key] = term
            text = text.replace(term, key)
            counter += 1

    return text, replacements


def _restore_glossary(text: str, replacements: dict) -> str:
    """Restore original terms from markers."""
    for key, original in replacements.items():
        text = text.replace(key, original)
    # Clean up any leftover markers
    text = re.sub(rf"{re.escape(_GLOSS_PREFIX)}\d+{re.escape(_GLOSS_SUFFIX)}", "", text)
    return text


async def translate_deepl(
    session: aiohttp.ClientSession, text: str, target_lang: str
) -> Optional[str]:
    """DeepL Free API. Returns None on any failure."""
    if not DEEPL_API_KEY:
        return None

    url = "https://api-free.deepl.com/v2/translate"
    payload = {
        "text": [text],
        "source_lang": "RU",
        "target_lang": target_lang,
        "tag_handling": "html",
        "ignore_tags": ["a", "code", "pre"],
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
    """Translate with glossary protection and fallback chain."""
    # Protect glossary terms and hashtags
    protected_text, replacements = _protect_glossary(text)

    translated = await translate_deepl(session, protected_text, lang.code)

    if translated is None:
        log.info("DeepL unavailable, trying Google Translate for %s", lang.code)
        # Strip HTML for Google (it doesn't handle tags well)
        plain = re.sub(r"<[^>]+>", "", protected_text)
        translated = await translate_google(session, plain, lang.code)

    if translated is None:
        log.error("All translation backends failed for %s", lang.code)
        return text  # return original as last resort

    # Restore glossary terms
    translated = _restore_glossary(translated, replacements)

    # Apply name transliteration fixes
    translated = apply_name_fixes(translated, lang)

    return clean_html(translated)

"""
translator.py — DeepL + Google fallback. Glossary protection. Translation cache.
"""

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Optional

import aiohttp

from bot.config import DEEPL_API_KEY, GLOSSARY, DATA_DIR, LangConfig
from bot.scraper import clean_html

log = logging.getLogger("tg-aggregator")

_GLOSS_PREFIX = "\u2063GLOSS"
_GLOSS_SUFFIX = "SSOLG\u2063"

# T15: Translation cache dir
_CACHE_DIR = DATA_DIR / "translation_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Track DeepL character usage
deepl_chars_used: int = 0


def _cache_key(text: str, lang_code: str) -> str:
    h = hashlib.md5(f"{lang_code}:{text}".encode()).hexdigest()
    return h


def _cache_get(text: str, lang_code: str) -> Optional[str]:
    key = _cache_key(text, lang_code)
    p = _CACHE_DIR / f"{key}.txt"
    if p.exists():
        log.debug("Translation cache hit for %s", lang_code)
        return p.read_text()
    return None


def _cache_set(text: str, lang_code: str, translated: str):
    key = _cache_key(text, lang_code)
    p = _CACHE_DIR / f"{key}.txt"
    p.write_text(translated)


def _protect_glossary(text: str) -> tuple[str, dict]:
    replacements = {}
    counter = 0

    def _replace_hashtag(m):
        nonlocal counter
        key = f"{_GLOSS_PREFIX}{counter}{_GLOSS_SUFFIX}"
        replacements[key] = m.group(0)
        counter += 1
        return key
    text = re.sub(r"#\w+", _replace_hashtag, text)

    for term in sorted(GLOSSARY, key=len, reverse=True):
        if term in text:
            key = f"{_GLOSS_PREFIX}{counter}{_GLOSS_SUFFIX}"
            replacements[key] = term
            text = text.replace(term, key)
            counter += 1

    return text, replacements


def _restore_glossary(text: str, replacements: dict) -> str:
    for key, original in replacements.items():
        text = text.replace(key, original)
    text = re.sub(rf"{re.escape(_GLOSS_PREFIX)}\d+{re.escape(_GLOSS_SUFFIX)}", "", text)
    return text


async def translate_deepl(
    session: aiohttp.ClientSession, text: str, target_lang: str
) -> Optional[str]:
    global deepl_chars_used
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
            deepl_chars_used += len(text)
            return data["translations"][0]["text"]
    except Exception as e:
        log.error("DeepL error: %s", e)
        return None


async def translate_google(
    session: aiohttp.ClientSession, text: str, target_lang: str
) -> Optional[str]:
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
    for src, dst in lang.name_fixes.items():
        text = text.replace(src, dst)
    return text


async def translate(
    session: aiohttp.ClientSession, text: str, lang: LangConfig
) -> str:
    """Translate with cache, glossary protection, and fallback chain."""
    # T15: Check cache first
    cached = _cache_get(text, lang.code)
    if cached is not None:
        log.info("Using cached translation for %s", lang.code)
        return cached

    protected_text, replacements = _protect_glossary(text)

    translated = await translate_deepl(session, protected_text, lang.code)

    if translated is None:
        log.info("DeepL unavailable, trying Google Translate for %s", lang.code)
        plain = re.sub(r"<[^>]+>", "", protected_text)
        translated = await translate_google(session, plain, lang.code)

    if translated is None:
        log.error("All translation backends failed for %s", lang.code)
        return None  # T22: fail-closed — never publish untranslated original

    translated = _restore_glossary(translated, replacements)
    translated = apply_name_fixes(translated, lang)
    result = clean_html(translated)

    # T15: Save to cache
    _cache_set(text, lang.code, result)

    return result

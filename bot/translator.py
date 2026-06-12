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

# T30: Persistent DeepL usage counter
_DEEPL_COUNTER_FILE = DATA_DIR / "deepl_usage.json"
# DeepL Free monthly limit
DEEPL_FREE_LIMIT = 500_000
# T31: Alert threshold (80% of limit)
DEEPL_ALERT_THRESHOLD = int(DEEPL_FREE_LIMIT * 0.8)


def _load_deepl_usage() -> dict:
    """T30: Load persisted DeepL usage. Resets counter at start of each month."""
    import datetime
    current_month = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
    if _DEEPL_COUNTER_FILE.exists():
        try:
            data = json.loads(_DEEPL_COUNTER_FILE.read_text())
            # Reset if month changed (DeepL Free quota resets monthly)
            if data.get("month") != current_month:
                return {"month": current_month, "chars": 0, "alerted": False}
            return data
        except Exception:
            log.warning("Corrupt deepl_usage.json, resetting")
    return {"month": current_month, "chars": 0, "alerted": False}


def _save_deepl_usage(data: dict):
    """T30: Persist DeepL usage atomically."""
    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=_DEEPL_COUNTER_FILE.parent, suffix=".tmp")
    try:
        os.write(fd, json.dumps(data).encode())
        os.close(fd)
        os.rename(tmp, str(_DEEPL_COUNTER_FILE))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# T30: Load persisted usage at module import
_usage = _load_deepl_usage()
deepl_chars_used: int = _usage["chars"]

# T31: Callback for limit alerts (set by main.py)
_alert_callback = None

# T33: Cache hit/miss tracking for stats
cache_hits: int = 0
cache_misses: int = 0


def set_alert_callback(callback):
    """T31: Register a callback(message) invoked when DeepL usage crosses 80%."""
    global _alert_callback
    _alert_callback = callback


def get_deepl_usage() -> dict:
    """Return current DeepL usage info for stats."""
    return {
        "chars": deepl_chars_used,
        "limit": DEEPL_FREE_LIMIT,
        "percent": round(100 * deepl_chars_used / DEEPL_FREE_LIMIT, 1),
        "month": _usage["month"],
    }


async def _record_deepl_usage(chars: int):
    """T30/T31: Increment persistent counter and alert at 80%."""
    global deepl_chars_used, _usage
    deepl_chars_used += chars
    _usage["chars"] = deepl_chars_used
    _save_deepl_usage(_usage)
    # T31: Alert once when crossing 80%
    if deepl_chars_used >= DEEPL_ALERT_THRESHOLD and not _usage.get("alerted"):
        _usage["alerted"] = True
        _save_deepl_usage(_usage)
        if _alert_callback:
            pct = round(100 * deepl_chars_used / DEEPL_FREE_LIMIT, 1)
            try:
                await _alert_callback(
                    f"🟡 DeepL usage at {pct}% "
                    f"({deepl_chars_used:,}/{DEEPL_FREE_LIMIT:,} chars this month). "
                    f"Approaching free-tier limit; Google fallback will kick in at 100%."
                )
            except Exception as e:
                log.error("DeepL alert callback failed: %s", e)


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


def cleanup_cache(max_age_days: int = 30) -> int:
    """T34: Remove cache files older than max_age_days. Returns count removed."""
    import time
    cutoff = time.time() - (max_age_days * 86400)
    removed = 0
    try:
        for f in _CACHE_DIR.glob("*.txt"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
    except Exception as e:
        log.error("Cache cleanup error: %s", e)
    if removed:
        log.info("T34: Cleaned %d cache entries older than %d days", removed, max_age_days)
    return removed


def cache_stats() -> dict:
    """T33: Return cache statistics (file count, total size)."""
    try:
        files = list(_CACHE_DIR.glob("*.txt"))
        total_size = sum(f.stat().st_size for f in files)
        return {"entries": len(files), "size_kb": round(total_size / 1024, 1)}
    except Exception:
        return {"entries": 0, "size_kb": 0.0}


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
            # T30: persistent usage tracking
            await _record_deepl_usage(len(text))
            return data["translations"][0]["text"]
    except Exception as e:
        log.error("DeepL error: %s", e)
        return None


async def translate_google(
    session: aiohttp.ClientSession, text: str, target_lang: str
) -> Optional[str]:
    gl_map = {"DE": "de", "EN-GB": "en"}
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
) -> Optional[str]:
    """Translate with cache, glossary protection, and fallback chain."""
    global cache_hits, cache_misses
    # T15: Check cache first
    cached = _cache_get(text, lang.code)
    if cached is not None:
        cache_hits += 1  # T33
        log.info("Using cached translation for %s", lang.code)
        return cached
    cache_misses += 1  # T33

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

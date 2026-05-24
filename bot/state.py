"""
state.py — Deduplication, retry tracking, content hashing.
"""

import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from bot.config import DATA_DIR, MAX_RETRIES

log = logging.getLogger("tg-aggregator")

MAX_IDS = 5000


def _path(prefix: str, lang_code: str) -> Path:
    return DATA_DIR / f"{prefix}_{lang_code.lower()}.json"


def _atomic_write(path: Path, content: str):
    """T23: Write to temp file then rename — atomic on POSIX."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, content.encode())
        os.close(fd)
        os.rename(tmp, str(path))
    except Exception:
        os.close(fd) if not os.get_inheritable(fd) else None
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_set(prefix: str, lang_code: str) -> set[str]:
    p = _path(prefix, lang_code)
    if p.exists():
        try:
            return set(json.loads(p.read_text()))
        except Exception:
            log.warning("Corrupt %s file for %s, resetting", prefix, lang_code)
    return set()


def _save_set(prefix: str, lang_code: str, data: set[str]):
    p = _path(prefix, lang_code)
    _atomic_write(p, json.dumps(sorted(data)[-MAX_IDS:]))


def _load_dict(prefix: str, lang_code: str) -> dict:
    p = _path(prefix, lang_code)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            log.warning("Corrupt %s file for %s, resetting", prefix, lang_code)
    return {}


def _save_dict(prefix: str, lang_code: str, data: dict):
    p = _path(prefix, lang_code)
    if len(data) > MAX_IDS:
        keys = sorted(data.keys())[-MAX_IDS:]
        data = {k: data[k] for k in keys}
    _atomic_write(p, json.dumps(data))


def load_seen(lang_code: str) -> set[str]:
    return _load_set("seen", lang_code)

def save_seen(lang_code: str, seen: set[str]):
    _save_set("seen", lang_code, seen)

def load_published(lang_code: str) -> set[str]:
    return _load_set("published", lang_code)

def save_published(lang_code: str, published: set[str]):
    _save_set("published", lang_code, published)

def load_content_hashes(lang_code: str) -> set[str]:
    return _load_set("hashes", lang_code)

def save_content_hashes(lang_code: str, hashes: set[str]):
    _save_set("hashes", lang_code, hashes)

def content_hash(text: str) -> str:
    normalized = re.sub(r'\s+', ' ', text.strip().lower())
    return hashlib.md5(normalized.encode()).hexdigest()

def load_failed(lang_code: str) -> dict[str, int]:
    return _load_dict("failed", lang_code)

def save_failed(lang_code: str, failed: dict[str, int]):
    _save_dict("failed", lang_code, failed)

def mark_failed(lang_code: str, post_id: str):
    failed = load_failed(lang_code)
    failed[post_id] = failed.get(post_id, 0) + 1
    save_failed(lang_code, failed)

def get_retryable(lang_code: str) -> list[str]:
    failed = load_failed(lang_code)
    return [pid for pid, count in failed.items() if count < MAX_RETRIES]

def clear_failed(lang_code: str, post_id: str):
    failed = load_failed(lang_code)
    failed.pop(post_id, None)
    save_failed(lang_code, failed)

def has_any_state() -> bool:
    if not DATA_DIR.exists():
        return False
    return any(f.name.startswith("seen_") for f in DATA_DIR.iterdir())

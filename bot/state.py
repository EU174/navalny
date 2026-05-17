"""
state.py — Deduplication state (JSON files on disk).

Two layers of dedup:
  1. By post_id (per language) — skip already-processed posts
  2. By content hash (per language) — skip duplicate text across channels

Edit this file to:
  - Switch to SQLite for faster lookups
  - Add Redis support for multi-instance setups
  - Change retention (currently keeps last 5000 entries)
"""

import hashlib
import json
import logging
import re
from pathlib import Path

from bot.config import DATA_DIR

log = logging.getLogger("tg-aggregator")

MAX_IDS = 5000  # keep last N entries per language


def _state_path(lang_code: str) -> Path:
    return DATA_DIR / f"seen_{lang_code.lower()}.json"


def _hashes_path(lang_code: str) -> Path:
    return DATA_DIR / f"hashes_{lang_code.lower()}.json"


def load_seen(lang_code: str) -> set[str]:
    p = _state_path(lang_code)
    if p.exists():
        try:
            return set(json.loads(p.read_text()))
        except Exception:
            log.warning("Corrupt state file %s, resetting", p)
    return set()


def save_seen(lang_code: str, seen: set[str]):
    p = _state_path(lang_code)
    p.write_text(json.dumps(sorted(seen)[-MAX_IDS:]))


def load_content_hashes(lang_code: str) -> set[str]:
    p = _hashes_path(lang_code)
    if p.exists():
        try:
            return set(json.loads(p.read_text()))
        except Exception:
            log.warning("Corrupt hashes file %s, resetting", p)
    return set()


def save_content_hashes(lang_code: str, hashes: set[str]):
    p = _hashes_path(lang_code)
    p.write_text(json.dumps(sorted(hashes)[-MAX_IDS:]))


def content_hash(text: str) -> str:
    """Generate a hash of the post text, ignoring whitespace differences."""
    normalized = re.sub(r'\s+', ' ', text.strip().lower())
    return hashlib.md5(normalized.encode()).hexdigest()


def has_any_state() -> bool:
    """True if at least one state file exists (not first run)."""
    return any(
        (DATA_DIR / f).exists()
        for f in DATA_DIR.iterdir()
        if f.name.startswith("seen_")
    ) if DATA_DIR.exists() else False

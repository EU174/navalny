"""
T27: Minimal test suite for critical functions.
Run: python3.11 -m pytest tests/ -v
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ─── Setup: patch DATA_DIR before importing bot modules ──────────────────────

@pytest.fixture(autouse=True)
def tmp_data_dir(tmp_path):
    """Use a temp directory for all state/cache operations."""
    with patch("bot.config.DATA_DIR", tmp_path):
        # Re-patch state module's DATA_DIR
        import bot.state as state_mod
        state_mod.DATA_DIR = tmp_path
        # Re-patch translator cache dir
        import bot.translator as trans_mod
        cache_dir = tmp_path / "translation_cache"
        cache_dir.mkdir(exist_ok=True)
        trans_mod._CACHE_DIR = cache_dir
        yield tmp_path


# ─── T27a: clean_html tests ─────────────────────────────────────────────────

class TestCleanHtml:
    def test_keeps_supported_tags(self):
        from bot.scraper import clean_html
        html = '<b>bold</b> <i>italic</i> <a href="url">link</a>'
        result = clean_html(html)
        assert "<b>" in result
        assert "<i>" in result
        assert "<a " in result

    def test_strips_unsupported_tags(self):
        from bot.scraper import clean_html
        html = '<div>text</div><span>more</span><p>para</p>'
        result = clean_html(html)
        assert "<div>" not in result
        assert "<span>" not in result
        assert "text" in result

    def test_removes_empty_tags(self):
        from bot.scraper import clean_html
        html = 'text <b></b> more'
        result = clean_html(html)
        assert "<b></b>" not in result

    def test_normalizes_newlines(self):
        from bot.scraper import clean_html
        html = 'line1\n\n\n\n\nline2'
        result = clean_html(html)
        assert '\n\n\n' not in result

    def test_removes_junk_lines(self):
        from bot.scraper import clean_html
        html = 'Real content here\n\n| Поддержать'
        result = clean_html(html)
        assert "Поддержать" not in result


# ─── T27b: _split_message tests (T25: HTML-aware) ───────────────────────────

class TestSplitMessage:
    def test_short_message_unchanged(self):
        from bot.publisher import _split_message
        result = _split_message("hello world", 100)
        assert result == ["hello world"]

    def test_splits_long_message(self):
        from bot.publisher import _split_message
        text = "word " * 1000
        chunks = _split_message(text.strip(), 100)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 100

    def test_html_tags_balanced(self):
        from bot.publisher import _split_message
        text = "<b>" + "word " * 200 + "</b>"
        chunks = _split_message(text, 100)
        for chunk in chunks:
            opens = chunk.count("<b>")
            closes = chunk.count("</b>")
            assert opens == closes, f"Unbalanced tags in chunk: {chunk[:60]}..."

    def test_nested_tags(self):
        from bot.publisher import _split_message
        text = "<b><i>" + "word " * 200 + "</i></b>"
        chunks = _split_message(text, 100)
        for chunk in chunks:
            b_opens = chunk.count("<b>")
            b_closes = chunk.count("</b>")
            i_opens = chunk.count("<i>")
            i_closes = chunk.count("</i>")
            assert b_opens == b_closes
            assert i_opens == i_closes


# ─── T27c: glossary protection tests ────────────────────────────────────────

class TestGlossary:
    def test_protect_and_restore(self):
        from bot.translator import _protect_glossary, _restore_glossary
        text = "Navalny spoke about FBK"
        protected, replacements = _protect_glossary(text)
        assert "Navalny" not in protected
        assert "FBK" not in protected
        restored = _restore_glossary(protected, replacements)
        assert "Navalny" in restored
        assert "FBK" in restored

    def test_hashtags_protected(self):
        from bot.translator import _protect_glossary, _restore_glossary
        text = "Post about #FreeNavalny"
        protected, replacements = _protect_glossary(text)
        assert "#FreeNavalny" not in protected
        restored = _restore_glossary(protected, replacements)
        assert "#FreeNavalny" in restored


# ─── T27d: state tests (T23: atomic writes) ─────────────────────────────────

class TestState:
    def test_save_and_load_seen(self, tmp_data_dir):
        from bot.state import load_seen, save_seen
        save_seen("de", {"post1", "post2"})
        loaded = load_seen("de")
        assert loaded == {"post1", "post2"}

    def test_corrupt_file_resets(self, tmp_data_dir):
        from bot.state import load_seen
        p = tmp_data_dir / "seen_de.json"
        p.write_text("{{corrupt json")
        loaded = load_seen("de")
        assert loaded == set()

    def test_max_ids_limit(self, tmp_data_dir):
        from bot.state import save_seen, load_seen, MAX_IDS
        big_set = {f"post{i}" for i in range(MAX_IDS + 500)}
        save_seen("de", big_set)
        loaded = load_seen("de")
        assert len(loaded) <= MAX_IDS

    def test_content_hash_consistent(self):
        from bot.state import content_hash
        h1 = content_hash("Hello  World ")
        h2 = content_hash("hello world")
        assert h1 == h2

    def test_failed_tracking(self, tmp_data_dir):
        from bot.state import mark_failed, get_retryable, clear_failed
        mark_failed("de", "post1")
        mark_failed("de", "post1")
        retryable = get_retryable("de")
        assert "post1" in retryable
        clear_failed("de", "post1")
        retryable = get_retryable("de")
        assert "post1" not in retryable


# ─── T22: fail-closed translation test ──────────────────────────────────────

class TestFailClosed:
    def test_translate_returns_none_on_failure(self):
        """Verify translate returns None (not original text) when all backends fail."""
        import asyncio
        from bot.translator import translate
        from bot.config import LangConfig

        lang = LangConfig(
            code="DE",
            chat_id="@test",
            source_label="Quelle",
            part2_label="Teil 2",
            channel_name="Test",
        )

        async def _test():
            import aiohttp
            async with aiohttp.ClientSession() as session:
                # With no DEEPL key and broken Google, should return None
                with patch("bot.translator.DEEPL_API_KEY", ""):
                    with patch("bot.translator.translate_google", return_value=None):
                        result = await translate(session, "тестовый текст", lang)
                        assert result is None, f"Expected None, got: {result}"

        asyncio.run(_test())

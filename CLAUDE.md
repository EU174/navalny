# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram aggregator that **scrapes** several Russian-language Navalny-team channels via their public web preview, **translates** new posts to German / English / French, and **republishes** them to one target channel per language. There is no Telegram user-client or login: source posts are read anonymously from the `https://t.me/s/<channel>` HTML preview; publishing goes through the Bot API (the bot must be an admin of each target channel).

Code is annotated with task markers like `T22`, `T25` in comments and commit messages — these are historical work-item tags, not a framework. Follow the existing convention if extending tracked work, but they carry no runtime meaning.

## Commands

```bash
pip install -r requirements.txt              # deps: aiohttp, beautifulsoup4, lxml (Python 3.11)

# Run locally — requires env vars (see below). Without DATA_DIR it writes to /data.
TELEGRAM_BOT_TOKEN=... DATA_DIR=./data python -u main.py

python3.11 -m pytest tests/ -v                                          # full suite
python3.11 -m pytest tests/test_bot.py::TestCleanHtml -v                 # one class
python3.11 -m pytest tests/test_bot.py::TestState::test_max_ids_limit -v # one test
```

Tests patch `DATA_DIR` to a tmp dir via an autouse fixture, so they never touch real state. `TestFailClosed` and `TestGlossary` hit no network. **Importing any `bot.*` module requires `TELEGRAM_BOT_TOKEN` to be set** — `bot/config.py` reads it at import time with `os.environ[...]` (hard fail if missing).

### Environment variables

| Var | Required | Default | Purpose |
|-----|----------|---------|---------|
| `TELEGRAM_BOT_TOKEN` | **yes** | — | Bot API token (read at import time) |
| `DEEPL_API_KEY` | no | `""` | DeepL free API; absent → Google fallback only |
| `ADMIN_CHAT_ID` | no | `""` | TG user id for alerts + `/status` `/force` commands |
| `POLL_INTERVAL` | no | `900` | Seconds between cycles |
| `TG_CHAT_DE` / `TG_CHAT_EN` / `TG_CHAT_FR` | no | `@navalnydeutsch` etc. | Target channels |
| `DATA_DIR` | no | `/data` | State + translation cache root |
| `PORT` | no | `8080` | Health server port |

## Architecture

One async process (`main.py`) running a poll loop. Each cycle: scrape all source channels → for each target language, translate and publish each unseen post → persist state. A small aiohttp health server runs alongside.

**Pipeline:** `scraper.py` → `translator.py` → `publisher.py`, with `state.py` providing dedup/retry persistence and `health.py` exposing stats. `config.py` holds all channel/language/glossary tables.

### Scraping (`bot/scraper.py`)
- Parses `t.me/s/<channel>` HTML with BeautifulSoup. **Forwarded posts and link-preview blocks are dropped** before text extraction; posts under `MIN_POST_LENGTH` (50) chars are skipped.
- `tg_html()` converts the message DOM to Telegram's restricted HTML subset; `clean_html()` then balances tags and strips "junk" lines (emoji-only, channel footers, "Subscribe/Support" buttons) — see `_is_junk_line` / `_clean_trailing_junk`. This is heuristic and the main source of formatting bugs.
- A `Post` carries both `text_html` (for translation) and `text_plain` (for dedup hashing), plus `photo_urls`, `has_video`, `youtube_id`, and the numeric `message_id`.

### Translation (`bot/translator.py`)
- **Fail-closed:** `translate()` returns `None` when every backend fails. Callers must treat `None` as "do not publish" — never fall back to posting the untranslated original. `TestFailClosed` guards this.
- Order: cache → **glossary protection** → DeepL (`tag_handling=html`) → Google fallback (plaintext) → restore glossary → `apply_name_fixes` → `clean_html`. Results cached at `DATA_DIR/translation_cache/<md5(lang:text)>.txt`.
- Glossary protection wraps hashtags and `GLOSSARY` terms in invisible sentinel tokens (`⁣GLOSS…`) so the translator leaves them untouched, then restores them after. `NAME_FIXES[lang]` is a plain ordered `str.replace` pass applied *after* translation to transliterate names (e.g. Navalny↔Nawalny), fix org names, and catch Russian leftovers — order matters since replacements are sequential.

### Publishing (`bot/publisher.py`)
- `publish_post()` is a **media-aware cascade**, tried in order until one succeeds: album (>1 photo) → single photo → YouTube thumbnail → native-video text link → plain text. Photos are downloaded and re-uploaded as multipart (not passed by URL).
- Two Telegram length limits drive branching: `CAPTION_LIMIT` (1024, photo captions) and `MESSAGE_LIMIT` (4096, text). When text exceeds a caption, the media goes out with a short caption and the full body follows as **reply chunks**; success requires *all* chunks to deliver (`_send_reply_chunks` → T24).
- `_split_message()` is HTML-aware: it closes open tags at a chunk boundary and reopens them in the next chunk. `tg_request()` retries on `429` honoring `retry_after`; text/photo sends retry once as plain text on HTML failure. **All sends set `disable_web_page_preview=True`.**

### State & dedup (`bot/state.py`)
- Per-language JSON files in `DATA_DIR`: `seen_<lang>.json` (post ids processed), `published_<lang>.json` (actually sent), `hashes_<lang>.json` (content hashes), `failed_<lang>.json` (`{post_id: retry_count}`).
- **`seen` ≠ `published`:** a post is marked `seen` once handled at all (even if a duplicate or a failure); `published` only on successful send; `failed` tracks retry counts up to `MAX_RETRIES` (3). `content_hash` (whitespace/case-normalized md5) dedupes identical content **across channels**.
- Writes are atomic (temp file + `os.rename`) and capped at `MAX_IDS` (5000, newest kept). Corrupt files reset to empty rather than crashing. State is saved after **every** post for crash safety, so the loop is resumable mid-cycle.

### Loop behavior (`main.py`)
- **First run** (`has_any_state()` false): `seed_initial_state()` marks all currently-visible posts as seen/published so the bot doesn't flood channels with backlog on deploy.
- Gap detection compares `message_id` ranges across cycles; a gap >5 alerts the admin (T26). Six consecutive zero-fetch cycles also alert.
- Admin commands are polled via `getUpdates` at the top of each cycle: `/status`, `/force <channel>/<id>` (manual republish), `/help`.

## Deployment

Production runs on an **Oracle VM under systemd** (service name `navalny-bot`). `.github/workflows/deploy-oracle.yml` SSHes in on push to `main` and does `git pull && systemctl restart navalny-bot`.

`Dockerfile` and `fly.toml` are **legacy** from a removed Fly.io deployment (see `git log`) and are not part of the current deploy path; don't assume they're live.

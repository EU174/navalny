"""
config.py — Settings, channels, languages, glossary.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

# ─── Env vars ────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DEEPL_API_KEY = os.environ.get("DEEPL_API_KEY", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")  # your TG user id for alerts

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "900"))  # seconds
HEALTH_PORT = int(os.environ.get("PORT", "8080"))

MIN_POST_LENGTH = 50
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
MAX_RETRIES = 3  # retry failed posts up to N times

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ─── Source channels ─────────────────────────────────────────────────────────

SOURCE_CHANNELS = [
    "leonid_volkov",
    "yulia_navalnaya_channel",
    "teamnavalny",
    "anti_edro",
    "navalnylivechannel",
    "lawfbk",
    "mariapevchikh",
]

# ─── Glossary: never translate these terms ───────────────────────────────────
# These get wrapped in <keep> tags before translation and unwrapped after.

GLOSSARY = [
    # People
    "Навальный", "Навальная", "Навального", "Навальной", "Навальному",
    "Navalny", "Navalnaya", "Nawalny", "Nawalnaja",
    "Волков", "Волкова", "Волкову",
    "Певчих",
    "Жданов", "Жданова",
    "Шаведдинов", "Шаведдинова",
    # Organizations
    "ФБК", "FBK", "АСФ", "ACF",
    "Единая Россия", "ЕдРо",
    "Команда Навального", "Team Navalny",
    # Hashtags (pattern handled separately in translator.py)
]

# ─── Channel display names per language ──────────────────────────────────────

CHANNEL_NAMES = {
    "DE": {
        "leonid_volkov":           "Leonid Wolkow",
        "yulia_navalnaya_channel": "Julia Nawalnaja",
        "teamnavalny":             "Team Nawalny",
        "anti_edro":               "Anti-Einiges-Russland",
        "navalnylivechannel":      "Nawalny LIVE",
        "lawfbk":                  "FBK Juristen",
        "mariapevchikh":           "Maria Pewtschich",
    },
    "EN-GB": {
        "leonid_volkov":           "Leonid Volkov",
        "yulia_navalnaya_channel": "Yulia Navalnaya",
        "teamnavalny":             "Team Navalny",
        "anti_edro":               "Anti-United Russia",
        "navalnylivechannel":      "Navalny LIVE",
        "lawfbk":                  "FBK Lawyers",
        "mariapevchikh":           "Maria Pevchikh",
    },
    "FR": {
        "leonid_volkov":           "Leonid Volkov",
        "yulia_navalnaya_channel": "Ioulia Navalnaïa",
        "teamnavalny":             "Équipe Navalny",
        "anti_edro":               "Anti-Russie unie",
        "navalnylivechannel":      "Navalny LIVE",
        "lawfbk":                  "FBK Juristes",
        "mariapevchikh":           "Maria Pevchikh",
    },
}

# ─── Target languages ────────────────────────────────────────────────────────

@dataclass
class LangConfig:
    code: str
    chat_id: str
    source_label: str
    part2_label: str
    channel_name: str
    name_fixes: dict = field(default_factory=dict)

LANGUAGES = [
    LangConfig(
        code="DE",
        chat_id=os.environ.get("TG_CHAT_DE", "@navalnydeutsch"),
        source_label="Quelle",
        part2_label="Teil 2",
        channel_name="Nawalny Deutsch",
        name_fixes={
            "Navalny": "Nawalny", "navalny": "nawalny",
            "Navalnaya": "Nawalnaja", "navalnaya": "nawalnaja",
            "Navalniy": "Nawalny",
            "Навальный": "Nawalny", "Навальная": "Nawalnaja",
        },
    ),
    LangConfig(
        code="EN-GB",
        chat_id=os.environ.get("TG_CHAT_EN", "@navalnyenglish"),
        source_label="Source",
        part2_label="Part 2",
        channel_name="Navalny English",
        name_fixes={
            "Nawalny": "Navalny", "nawalny": "navalny",
            "Nawalnaja": "Navalnaya", "nawalnaja": "navalnaya",
            "Навальный": "Navalny", "Навальная": "Navalnaya",
        },
    ),
    LangConfig(
        code="FR",
        chat_id=os.environ.get("TG_CHAT_FR", "@navalnyfrancais"),
        source_label="Source\u00a0",
        part2_label="Partie 2",
        channel_name="Navalny Français",
        name_fixes={
            "Nawalny": "Navalny", "nawalny": "navalny",
            "Nawalnaja": "Navalnaya", "nawalnaja": "navalnaya",
            "Навальный": "Navalny", "Навальная": "Navalnaya",
        },
    ),
]

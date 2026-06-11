"""
config.py — Settings, channels, languages, glossary.
"""

import os
from dataclasses import dataclass
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
# Only terms that should pass through unchanged in ALL languages.
# Organizations and names that need per-language translation go in name_fixes.

GLOSSARY = [
    # Hashtags handled separately in translator.py
    # People names that should stay as-is (nominative Latin forms)
    "Navalny", "Navalnaya", "Nawalny", "Nawalnaja",
    "FBK", "ACF",
    "Team Navalny",
]

# ─── Post-translation replacements per language ──────────────────────────────
# Applied AFTER DeepL translates. Covers:
# - Name transliteration (Navalny↔Nawalny)
# - Russian leftovers DeepL didn't translate
# - Organization names that need specific translations

NAME_FIXES = {
    "DE": {
        # Navalny family — all case forms
        "Navalny": "Nawalny", "navalny": "nawalny",
        "Navalnaya": "Nawalnaja", "navalnaya": "nawalnaja",
        "Navalniy": "Nawalny",
        "Навальный": "Nawalny", "Навальная": "Nawalnaja",
        "Навального": "Nawalnys", "Навальной": "Nawalnaja",
        "Навальному": "Nawalny",
        # Volkov
        "Волков": "Wolkow", "Волкова": "Wolkow", "Волкову": "Wolkow",
        # Pevchikh
        "Певчих": "Pewtschich",
        # Zhdanov
        "Жданов": "Schdanow", "Жданова": "Schdanow",
        # Shaveddinov
        "Шаведдинов": "Schaweddinow", "Шаведдинова": "Schaweddinow",
        # Yulia
        "Юлия Навальная": "Julia Nawalnaja", "Юлии Навальной": "Julia Nawalnaja",
        # Organizations
        "Единая Россия": "Einiges Russland", "Единой России": "Einiges Russland",
        '"Единая Россия"': '\u201EEiniges Russland\u201C',
        "«Единая Россия»": "\u201EEiniges Russland\u201C",
        "ЕдРо": "Einiges Russland",
        "ФБК": "Stiftung für Korruptionsbekämpfung",
        "Фонд борьбы с коррупцией": "Stiftung für Korruptionsbekämpfung",
        "Фонд Борьбы с Коррупцией": "Stiftung für Korruptionsbekämpfung",
        "Команда Навального": "Team Nawalny", "Команды Навального": "Team Nawalny",
        "АСФ": "ASF",
    },
    "EN-GB": {
        # Navalny family
        "Nawalny": "Navalny", "nawalny": "navalny",
        "Nawalnaja": "Navalnaya", "nawalnaja": "navalnaya",
        "Навальный": "Navalny", "Навальная": "Navalnaya",
        "Навального": "Navalny's", "Навальной": "Navalnaya",
        "Навальному": "Navalny",
        # Volkov
        "Волков": "Volkov", "Волкова": "Volkov", "Волкову": "Volkov",
        # Pevchikh
        "Певчих": "Pevchikh",
        # Zhdanov
        "Жданов": "Zhdanov", "Жданова": "Zhdanov",
        # Shaveddinov
        "Шаведдинов": "Shaveddinov", "Шаведдинова": "Shaveddinov",
        # Yulia
        "Юлия Навальная": "Yulia Navalnaya", "Юлии Навальной": "Yulia Navalnaya",
        # Organizations
        "Единая Россия": "United Russia", "Единой России": "United Russia",
        '"Единая Россия"': '"United Russia"',
        "«Единая Россия»": '"United Russia"',
        "ЕдРо": "United Russia",
        "ФБК": "Anti-Corruption Foundation",
        "Фонд борьбы с коррупцией": "Anti-Corruption Foundation",
        "Фонд Борьбы с Коррупцией": "Anti-Corruption Foundation",
        "Команда Навального": "Team Navalny", "Команды Навального": "Team Navalny",
        "АСФ": "ACF",
    },
    "FR": {
        # Navalny family
        "Nawalny": "Navalny", "nawalny": "navalny",
        "Nawalnaja": "Navalnaya", "nawalnaja": "navalnaya",
        "Навальный": "Navalny", "Навальная": "Navalnaya",
        "Навального": "Navalny", "Навальной": "Navalnaya",
        "Навальному": "Navalny",
        # Volkov
        "Волков": "Volkov", "Волкова": "Volkov", "Волкову": "Volkov",
        # Pevchikh
        "Певчих": "Pevchikh",
        # Zhdanov
        "Жданов": "Jdanov", "Жданова": "Jdanov",
        # Shaveddinov
        "Шаведдинов": "Chaveddinov", "Шаведдинова": "Chaveddinov",
        # Yulia
        "Юлия Навальная": "Ioulia Navalnaya", "Юлии Навальной": "Ioulia Navalnaya",
        # Organizations
        "Единая Россия": "Russie unie", "Единой России": "Russie unie",
        '"Единая Россия"': "\u00ab\u00a0Russie unie\u00a0\u00bb",
        "«Единая Россия»": "\u00ab\u00a0Russie unie\u00a0\u00bb",
        "ЕдРо": "Russie unie",
        "ФБК": "Fondation anti-corruption",
        "Фонд борьбы с коррупцией": "Fondation anti-corruption",
        "Фонд Борьбы с Коррупцией": "Fondation anti-corruption",
        "Команда Навального": "Équipe Navalny", "Команды Навального": "Équipe Navalny",
        "АСФ": "FCA",
    },
}

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

    @property
    def name_fixes(self) -> dict:
        return NAME_FIXES.get(self.code, {})

LANGUAGES = [
    LangConfig(
        code="DE",
        chat_id=os.environ.get("TG_CHAT_DE", "@navalnydeutsch"),
        source_label="Quelle",
        part2_label="Teil 2",
        channel_name="Nawalny Deutsch",
    ),
    LangConfig(
        code="EN-GB",
        chat_id=os.environ.get("TG_CHAT_EN", "@navalnyenglish"),
        source_label="Source",
        part2_label="Part 2",
        channel_name="Navalny English",
    ),
        source_label="Source\u00a0",
        part2_label="Partie 2",
        channel_name="Navalny Français",
    ),
]

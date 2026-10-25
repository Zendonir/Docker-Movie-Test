"""Sprachkennungen (ISO 639-2/B, wie sie mkvmerge verwendet) und deutsche Bezeichnungen."""

from __future__ import annotations

# ISO 639-1 und 639-2/T -> 639-2/B
_ALIASES = {
    "de": "ger", "deu": "ger", "en": "eng", "fr": "fre", "fra": "fre", "es": "spa", "it": "ita", "nl": "dut",
    "nld": "dut", "ja": "jpn", "zh": "chi", "zho": "chi", "ru": "rus", "pl": "pol", "pt": "por", "tr": "tur",
    "sv": "swe", "da": "dan", "no": "nor", "nb": "nob", "fi": "fin", "cs": "cze", "ces": "cze", "hu": "hun",
    "el": "gre", "ell": "gre", "ko": "kor", "ar": "ara", "he": "heb", "hi": "hin", "th": "tha", "uk": "ukr",
    "ro": "rum", "ron": "rum", "sk": "slo", "slk": "slo", "hr": "hrv", "sr": "srp", "bg": "bul", "is": "ice",
    "isl": "ice", "fa": "per", "fas": "per", "ms": "may", "msa": "may", "id": "ind", "vi": "vie", "ca": "cat",
    "et": "est", "lv": "lav", "lt": "lit", "sl": "slv", "ga": "gle", "cy": "wel", "cym": "wel", "eu": "baq",
    "eus": "baq", "gsw": "gsw", "la": "lat", "": "und", "zxx": "zxx", "mul": "mul",
}

NAMES = {
    "ger": "Deutsch", "eng": "Englisch", "fre": "Französisch", "spa": "Spanisch", "ita": "Italienisch",
    "dut": "Niederländisch", "jpn": "Japanisch", "chi": "Chinesisch", "rus": "Russisch", "pol": "Polnisch",
    "por": "Portugiesisch", "tur": "Türkisch", "swe": "Schwedisch", "dan": "Dänisch", "nor": "Norwegisch",
    "nob": "Norwegisch (Bokmål)", "fin": "Finnisch", "cze": "Tschechisch", "hun": "Ungarisch", "gre": "Griechisch",
    "kor": "Koreanisch", "ara": "Arabisch", "heb": "Hebräisch", "hin": "Hindi", "tha": "Thailändisch",
    "ukr": "Ukrainisch", "rum": "Rumänisch", "slo": "Slowakisch", "hrv": "Kroatisch", "srp": "Serbisch",
    "bul": "Bulgarisch", "ice": "Isländisch", "per": "Persisch", "ind": "Indonesisch", "vie": "Vietnamesisch",
    "cat": "Katalanisch", "est": "Estnisch", "lav": "Lettisch", "lit": "Litauisch", "slv": "Slowenisch",
    "gsw": "Schweizerdeutsch", "lat": "Latein", "und": "Unbestimmt", "zxx": "Kein Sprachinhalt", "mul": "Mehrsprachig",
}


def normalize_language(code: str | None) -> str:
    """Normalisiert z. B. 'de', 'deu', 'de-DE', 'GER' zu 'ger'."""
    if not code:
        return "und"
    c = code.strip().lower().replace("_", "-").split("-")[0]
    if not c.isalpha() or not 2 <= len(c) <= 3:
        return "und"
    return _ALIASES.get(c, c)


def language_label(code: str) -> str:
    return NAMES.get(code, code.upper())

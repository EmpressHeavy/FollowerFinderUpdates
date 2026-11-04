"""Bundled lightweight language detector (v3.5.32). Standard library only; no download, no network.

Used by translation.py when X gives no usable per-post `lang` tag. Two passes:
  1. Script: Unicode blocks decide non-Latin languages (Japanese kana, Korean Hangul, Chinese Han, Arabic/Persian/Urdu,
     Hebrew, Greek, Cyrillic Russian/Ukrainian, Thai, Devanagari Hindi, Bengali).
  2. Stopwords: for Latin-script text, counts common function words for en, es, fr, de, pt, it, nl, tr, id, pl, sv, tl,
     vi and picks the clear winner.
Returns an ISO 639-1 code or None when the text is too short or unclear. If the optional `langdetect` package is
installed, detect_best() asks it only when the bundled detector returns None.
"""
from __future__ import annotations

import re
from collections import Counter

_URL = re.compile(r"https?://\S+|www\.\S+|[@#]\w+")
_WORD = re.compile(r"[^\W\d_]+", re.U)
MIN_LETTERS = 3

STOPWORDS = {
    "en": "the and is are was were to of in that it for you with on this have not be at but they what just".split(),
    "es": "el la los las de que y en un una es por para con no se lo del al pero muy como más está esto".split(),
    "fr": "le la les de des et est un une que qui pas pour dans ce sur avec je vous mais du au nous très".split(),
    "de": "der die das und ist nicht ein eine ich zu mit den sie es auf für von sich auch dem wir aber".split(),
    "pt": "o a os as de que e do da em um uma não para com é por mais mas se no na você muito está".split(),
    "it": "il la di che e è un una per non sono in del della con ma come anche questo ho mi gli".split(),
    "nl": "de het een en van is dat niet ik je op te met zijn voor maar ook er wat aan wel".split(),
    "tr": "ve bir bu da de için ne ile çok ben mi değil gibi daha ama var olan sen o".split(),
    "id": "yang dan di ini itu tidak saya dengan untuk ada aku dari ke juga kamu bisa sudah akan".split(),
    "pl": "i w nie na się z że to jest do jak co ale tak jestem już mnie dla po".split(),
    "sv": "och att det som en är på för inte jag med har till av om vi så men den".split(),
    "tl": "ang ng sa mga na at ay hindi ko ako mo ka siya kami namin po lang".split(),
    "vi": "và của là không có những được cho một người này với các đã tôi trong".split(),
}
_SW = {lang: set(ws) for lang, ws in STOPWORDS.items()}
_UK_ONLY = set("іїєґ")
_FA_UR = {"fa": set("پچژگ"), "ur": set("ٹڈڑںے")}


def _script_counts(text: str) -> Counter:
    c: Counter = Counter()
    for ch in text:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF:
            c["kana"] += 1
        elif 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF:
            c["hangul"] += 1
        elif 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            c["han"] += 1
        elif 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F:
            c["arabic"] += 1
        elif 0x0590 <= o <= 0x05FF:
            c["hebrew"] += 1
        elif 0x0370 <= o <= 0x03FF:
            c["greek"] += 1
        elif 0x0400 <= o <= 0x04FF:
            c["cyrillic"] += 1
        elif 0x0E00 <= o <= 0x0E7F:
            c["thai"] += 1
        elif 0x0900 <= o <= 0x097F:
            c["devanagari"] += 1
        elif 0x0980 <= o <= 0x09FF:
            c["bengali"] += 1
        elif ch.isalpha():
            c["latin"] += 1
    return c


def detect(text: str | None) -> str | None:
    """Best-guess language code for one text, or None when unsure."""
    t = _URL.sub(" ", text or "")
    sc = _script_counts(t)
    letters = sum(sc.values())
    if letters < MIN_LETTERS:
        return None
    script, n = sc.most_common(1)[0]
    if script != "latin" and n / letters >= 0.3:
        if script == "kana" or (script == "han" and sc["kana"]):
            return "ja"
        if script == "hangul":
            return "ko"
        if script == "han":
            return "zh"
        if script == "arabic":
            for lang, chars in _FA_UR.items():
                if any(ch in chars for ch in t):
                    return lang
            return "ar"
        return {"hebrew": "he", "greek": "el", "thai": "th", "devanagari": "hi", "bengali": "bn",
                "cyrillic": "uk" if any(ch in _UK_ONLY for ch in t.lower()) else "ru"}.get(script)
    words = [w.lower() for w in _WORD.findall(t)]
    if not words:
        return None
    scores = {lang: sum(1 for w in words if w in sw) for lang, sw in _SW.items()}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    (best, s1), (_, s2) = ranked[0], ranked[1]
    if s1 == 0 or s1 == s2:
        return None
    if len(words) >= 4 and s1 < 2:  # a single shared word in a longer text is not enough
        return None
    return best


def detect_best(text: str | None) -> str | None:
    """Bundled detector first; the optional `langdetect` package only when it is installed and we were unsure."""
    code = detect(text)
    if code or not (text or "").strip():
        return code
    try:
        from langdetect import detect as _ld  # type: ignore
        return (_ld(text) or "").split("-")[0].lower() or None
    except Exception:  # noqa: BLE001  (not installed, or langdetect could not decide)
        return None

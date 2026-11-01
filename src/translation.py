"""Foreign-language candidates: translated by X itself, using the box browser (v3.5.23).

If most of a candidate's posts are in a language the owner doesn't read (pref read_languages, default ['en']), Follower
Finder's browser agent opens a few of their recent posts on X, signed in to the owner's X account (the same browser
mechanism used for follows). It clicks X's own "Translate post" control and copies the translated text. The card's short
profile then reads "Translated by X from <Language>: <text>". The owner doesn't click anything.

If nothing was captured (button missing, error, X warning or login wall), the card shows the neutral line
"Translation not available for this account today."

This module makes no network calls and uses no outside translation service. The browser step is the only source of
translated text.

Language: X's per-post `lang` field. Undetermined codes (und, zxx, qme, qht, qam, qst, art) are ignored. If no post
has a usable lang, the local `langdetect` package is used only when it is installed. "Mostly" means at least
min_foreign_share (0.6) of lang-tagged posts are in one language.
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable

TRANSLATION_CONFIG = {
    "min_foreign_share": 0.6,
    "max_posts": 3,        # post URLs per account for the browser step
    "max_chars": 450,      # card summary length
}
IGNORE = {"und", "zxx", "qme", "qht", "qam", "qst", "art"}
NOT_AVAILABLE = "Translation not available for this account today."
LANG_NAMES = {
    "ar": "Arabic", "bn": "Bengali", "de": "German", "el": "Greek", "en": "English", "es": "Spanish", "fa": "Persian",
    "fi": "Finnish", "fr": "French", "he": "Hebrew", "iw": "Hebrew", "hi": "Hindi", "id": "Indonesian", "in": "Indonesian",
    "it": "Italian", "ja": "Japanese", "ko": "Korean", "nl": "Dutch", "no": "Norwegian", "pl": "Polish",
    "pt": "Portuguese", "ru": "Russian", "sv": "Swedish", "th": "Thai", "tl": "Tagalog", "tr": "Turkish",
    "uk": "Ukrainian", "ur": "Urdu", "vi": "Vietnamese", "zh": "Chinese",
}

BROWSER_TRANSLATE_STEPS = (
    "Browser step: translate posts with X's own Translate post (box browser, signed in to X; read only).\n"
    "For each request from translation.translation_requests(), and for each URL in it:\n"
    "1. Open the URL (https://x.com/<handle>/status/<id>) in the box browser.\n"
    "2. Under the post text, click 'Translate post'.\n"
    "3. Wait until the translated text appears under the original.\n"
    "4. Copy ONLY the translated text: not the original, not the 'Translated from ... by ...' label, no replies or UI text.\n"
    "5. NEVER like, reply, repost, quote, bookmark, follow or DM. Don't click anything else on the page.\n"
    "6. STOP the whole step on any X warning, rate-limit notice, captcha or login wall, and keep what was captured so far.\n"
    "If 'Translate post' is missing or fails for a post, skip it.\n"
    "Then call translation.apply_browser_translations(candidate, captured_texts) for every requested account. An empty "
    "list gives the neutral 'not available' line."
)


def _base(code: str | None) -> str:
    return (code or "").split("-")[0].lower()


def language_name(code: str) -> str:
    return LANG_NAMES.get(_base(code), code)


def dominant_language(posts: Iterable[dict], cfg: dict | None = None) -> tuple[str | None, float]:
    posts = list(posts)
    langs = [_base(p.get("lang")) for p in posts if _base(p.get("lang")) and _base(p.get("lang")) not in IGNORE]
    if not langs:
        try:  # optional local fallback, only if already installed
            from langdetect import detect  # type: ignore
            langs = [_base(detect(p["text"])) for p in posts if (p.get("text") or "").strip()]
        except Exception:  # noqa: BLE001
            return None, 0.0
    if not langs:
        return None, 0.0
    lang, n = Counter(langs).most_common(1)[0]
    return lang, round(n / len(langs), 2)


def _read(prefs) -> set[str]:
    langs = prefs.get("read_languages") if isinstance(prefs, dict) else prefs
    return {_base(x) for x in (langs or [])} or {"en"}


def foreign_language(posts: list[dict], prefs, cfg: dict | None = None) -> tuple[str, float] | None:
    c = {**TRANSLATION_CONFIG, **(cfg or {})}
    lang, share = dominant_language(posts, c)
    if not lang or lang in _read(prefs) or share < c["min_foreign_share"]:
        return None
    return lang, share


def translation_requests(candidates: list[dict], prefs, cfg: dict | None = None) -> list[dict]:
    """candidates: profile dicts with 'handle', 'user_id', 'posts' (X posts with id/text/lang, newest first) and an
    optional 'pinned_tweet_id'. prefs: {'read_languages': [...]} or a list of codes. Returns one request per
    mostly-foreign account: {user_id, handle, lang, share, urls}, with up to max_posts recent non-pinned post URLs in
    that language. Accounts without such posts are left out."""
    c = {**TRANSLATION_CONFIG, **(cfg or {})}
    out = []
    for cand in candidates:
        posts = cand.get("posts") or []
        fl = foreign_language(posts, prefs, c)
        if not fl:
            continue
        h = (cand.get("handle") or "").lstrip("@").strip()
        pin = str(cand.get("pinned_tweet_id") or "")
        ids = [str(p["id"]) for p in posts if p.get("id") and _base(p.get("lang")) == fl[0]
               and not p.get("pinned") and str(p["id"]) != pin][: c["max_posts"]]
        if h and ids:
            out.append({"user_id": cand.get("user_id"), "handle": h, "lang": fl[0], "share": fl[1],
                        "urls": [f"https://x.com/{h}/status/{i}" for i in ids]})
    return out


def apply_browser_translations(candidate: dict, captured: list[str] | None, *, lang: str | None = None,
                               cfg: dict | None = None) -> dict:
    """Adds the card line. 'captured' is what the browser step copied from X's Translate post. Text identical to an
    original post is dropped, so untranslated text is never shown as translated. Nothing usable gives NOT_AVAILABLE."""
    c = {**TRANSLATION_CONFIG, **(cfg or {})}
    posts = candidate.get("posts") or []
    if lang is None:
        fl = foreign_language(posts, {"read_languages": candidate.get("read_languages") or ["en"]}, c) \
            if "read_languages" in candidate else (dominant_language(posts, c)[0], 1.0)
        lang = fl[0] if fl else None
    originals = {" ".join((p.get("text") or "").split()) for p in posts}
    texts = [" ".join(t.split()) for t in (captured or []) if t and t.strip()]
    texts = [t for t in texts if t not in originals]
    if texts:
        s = " | ".join(texts)
        if len(s) > c["max_chars"]:
            s = s[: c["max_chars"] - 1].rstrip() + "…"
        t = {"lang": lang, "text": s, "translated_by": "x", "available": True}
        line = f"Translated by X from {language_name(lang or '')}: {s}"
    else:
        t = {"lang": lang, "text": None, "translated_by": None, "available": False}
        line = NOT_AVAILABLE
    candidate["translation"] = t
    candidate.setdefault("card_extras", []).append(line)
    return candidate

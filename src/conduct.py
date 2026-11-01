"""Account health / conduct signal for the daily verified review (v3.5.27).

Why: following an account ties your account to its behaviour. Accounts that troll, harass, catfish, use heavy
profanity, chase men or women in a sexual or provocative way, or break other X rules can hurt your account health by
association. The check is gender-neutral: the same rules apply whoever the account is and whoever it targets.

What is read: ONLY what was already fetched for the deep dive (the candidate's own posts AND its replies/quotes, i.e.
the user timeline plus the search from:candidate results), plus the bio and the connection_status field X returns on
the follower object. Reposts are skipped (not the candidate's own words). No extra X calls. Nothing is invented: with
no posts the posts part is "Not available"; a flag needs a pattern match in real text.

Flags (labels shown on the card):
  - "You muted this account"   connection_status contains "muting" (X API). Unknown -> nothing shown.
  - "catfishing signals"       romance-bait bio, "DM me"/move-to-WhatsApp/Telegram lures, sugar/romance-scam
                               wording (allowance, pay your bills, gift cards, lonely widow(er)...). Strong -> Skip.
  - "sexualized pursuit"       replies chasing men or women sexually / provocatively ("send pics", "dm me baby",
                               "you look so sexy"...), adult-content promotion in replies. Strong -> Skip.
  - "trolling/harassment"      insults or attacks aimed at someone ("you're an idiot", "shut up", "kys"...).
  - "profanity"                swear words in their own posts/replies (share of items).
  - "platform-rule risks"      adult links (onlyfans/fansly/nsfw/18+), violent threats, follow-for-follow or
                               engagement farming. (Crypto shilling is already scored by the deep dive; counted here as info only.)
  - tone "negative"            many hostile / negative items vs positive ones (positive / neutral / negative).

Points (negative only, added to the deep-dive score) and caps are in CONDUCT_CONFIG. Strong flags force Skip.
Reuses Follower Finder detectors: classify.handle_bio_screen bio hint words (dm me, sugar, onlyfans, whatsapp,
telegram...) and verified_deep_dive.item_kind/content_signals. Impersonation is already a hard Skip upstream.
"""
from __future__ import annotations

import re
from typing import Any

try:
    from . import classify as cl
    from . import verified_deep_dive as dd
except ImportError:  # top-level import
    import classify as cl  # type: ignore
    import verified_deep_dive as dd  # type: ignore

NA = "Not available"
NO_ISSUES = "No issues found"

CONDUCT_CONFIG: dict[str, Any] = {
    "muted": {"pts": -8.0, "max_rec": "Maybe"},          # you muted them: never Follow back, usually Skip
    "catfish": {"per_hit": -3.0, "cap": -8.0, "skip_at": 2},   # >= skip_at distinct signals (bio+posts) -> Skip
    "sexual_pursuit": {"per_hit": -3.0, "cap": -8.0, "skip_at": 1},  # any sexualized pursuit reply -> Skip
    "harassment": {"per_hit": -2.0, "cap": -6.0, "skip_at": 3},
    "profanity": {"per_hit": -0.5, "cap": -3.0, "heavy_share": 0.3, "heavy_pts": -3.0},
    "platform_risk": {"per_hit": -1.5, "cap": -5.0, "skip_at": 3},
    "tone": {"negative_share": 0.4, "min_items": 3, "negative_pts": -2.0},
}
FLAG_ORDER = ("You muted this account", "catfishing signals", "sexualized pursuit", "trolling/harassment",
              "platform-rule risks", "profanity", "negative tone")

_R = lambda p: re.compile(p, re.I)  # noqa: E731
CATFISH_BIO = _R(r"\b(single (and|&) (looking|ready)|looking for (love|my soulmate|a (serious|real) (man|woman|partner|relationship))|"
                 r"soul ?mate|lonely|widow(er)?|sugar ?(daddy|mommy|mummy|baby)|seeking (a )?(man|woman|gentleman|lady)|"
                 r"god ?fearing (man|woman)|honest (man|woman) only|no time ?wasters)\b")
DM_LURE = _R(r"(\b(dm|inbox|message) me\b|check (your|my) (dm|inbox)|text me on|add me on (whatsapp|telegram|snap|kik|hangouts?)|"
             r"\b(whatsapp|telegram|kik|snapchat|hangouts?)\b.{0,20}(me|\+?\d{6,}))")
ROMANCE_SCAM = _R(r"\b(sugar ?(daddy|mommy|mummy|baby)|weekly allowance|pay (your|ur) bills|gift ?cards?|"
                  r"send (you|u) money|spoil (you|u)|looking for (a )?serious relationship|i('m| am) (a )?(widow|widower|military|soldier|engineer) (on|in) (a |an )?(deployment|oil rig|offshore))\b")
SEXUAL_PURSUIT = _R(r"(send (me )?(pics|nudes|a pic)|\bnudes\b|dm me (baby|babe|sexy|handsome|beautiful|daddy|mami)|"
                    r"you('re| are)? (so |looking )?(sexy|hot|fine) (af|as hell|babe|baby|mama|daddy)|hey (sexy|baby|babe|beautiful|handsome)\b|"
                    r"\b(wanna|want to) (hook ?up|smash|have fun with me)|\bsit on (my|your) face\b|"
                    r"\b(horny|sexting|hookup)\b|come (to|in) my (dm|inbox)|link in (my )?bio.{0,10}(🔞|18\+|spicy))")
HARASS = _R(r"(\byou('re| are)? (an? )?(idiot|moron|loser|clown|retard\w*|pathetic|stupid|trash|garbage|worthless|ugly)\b|"
            r"\bshut (the \w+ )?up\b|\bkys\b|kill yourself|\bnobody (asked|cares)\b|\bstfu\b|\bgo die\b|\bdelete your account\b|"
            r"\bcry (more|harder)\b|\bseethe\b)")
PROFANITY = _R(r"\b(f+u+c+k+\w*|sh[i1]t\w*|bitch\w*|asshole\w*|bastard\w*|dick(head)?s?|cunt\w*|motherf\w*|wtf|"
               r"bullshit|damn|piss(ed)?|crap)\b")
PLATFORM_RISK = _R(r"(onlyfans|fansly|\bnsfw\b|🔞|\b18\+|\bf4f\b|follow ?for ?follow|follow ?back ?(train|=)|"
                   r"\bi('ll| will) (kill|hurt|find) you\b|\b(shoot|stab|beat) (you|him|her|them) up\b)")
POSITIVE = _R(r"\b(thanks?|thank you|love (this|it)|great|awesome|amazing|congrats|congratulations|well done|"
               r"nice|cool|helpful|beautiful work|proud|excited|happy|appreciate)\b|[🙏🎉❤️💯🙌]")
NEGATIVE = _R(r"\b(hate|disgusting|terrible|awful|worst|garbage|trash|sick of|pathetic|ridiculous|clown|idiot|"
              r"stupid|scam(mer)?s?|liar|fraud|shut up)\b")


def _texts(posts: list[dict] | None) -> list[dict]:
    out = []
    for p in posts or []:
        k = p.get("_kind") or dd.item_kind(p)
        if k == "repost":
            continue
        t = re.sub(r"https?://\S+", " ", p.get("text") or "")
        out.append({"kind": k, "text": t, "raw": p})
    return out


def is_muted(user: dict) -> bool | None:
    cs = user.get("connection_status")
    if cs is None:
        return None
    return "muting" in [str(x).lower() for x in cs]


def assess(user: dict, posts: list[dict] | None, *, cfg: dict | None = None, muted: bool | None = None) -> dict:
    c = {**CONDUCT_CONFIG, **(cfg or {})}
    bio = user.get("description") or ""
    items = _texts(posts)
    muted = is_muted(user) if muted is None else muted
    flags: dict[str, dict] = {}

    def add(name, n, pts, skip=False, cap_rec=None, note=None):
        flags[name] = {"count": n, "pts": pts, "skip": skip, "max_rec": cap_rec, "note": note}

    if muted:
        add("You muted this account", 1, c["muted"]["pts"], cap_rec=c["muted"]["max_rec"])

    # catfishing: bio (reuse Follower Finder bio screen) + post lures
    bio_words = cl.handle_bio_screen(cl.Candidate(user_id=str(user.get("id")), username=user.get("username", ""),
                                                  name=user.get("name") or "", verified=True, bio=bio))["bio_words"]
    cat = set()
    if CATFISH_BIO.search(bio): cat.add("romance-bait bio")
    if DM_LURE.search(bio) or any(w in bio_words for w in ("dm me", "dm for", "whatsapp", "telegram")): cat.add("'DM me' lure in bio")
    if ROMANCE_SCAM.search(bio) or "sugar" in bio_words: cat.add("sugar/romance-scam wording in bio")
    lure_n = sum(1 for i in items if DM_LURE.search(i["text"]))
    scam_n = sum(1 for i in items if ROMANCE_SCAM.search(i["text"]))
    if lure_n: cat.add(f"'DM me' lures in {lure_n} post(s)")
    if scam_n: cat.add(f"sugar/romance-scam wording in {scam_n} post(s)")
    if cat:
        C = c["catfish"]
        add("catfishing signals", len(cat), max(C["cap"], len(cat) * C["per_hit"]), skip=len(cat) >= C["skip_at"],
            note=", ".join(sorted(cat)))

    sp = [i for i in items if SEXUAL_PURSUIT.search(i["text"])]
    if sp:
        S = c["sexual_pursuit"]
        add("sexualized pursuit", len(sp), max(S["cap"], len(sp) * S["per_hit"]), skip=len(sp) >= S["skip_at"],
            note=f"{len(sp)} post(s)/repl(ies)")
    hz = [i for i in items if HARASS.search(i["text"])]
    if hz:
        H = c["harassment"]
        add("trolling/harassment", len(hz), max(H["cap"], len(hz) * H["per_hit"]), skip=len(hz) >= H["skip_at"],
            note=f"{len(hz)} of {len(items)} items")
    pr = [i for i in items if PLATFORM_RISK.search(i["text"])]
    if PLATFORM_RISK.search(bio) or "onlyfans" in bio_words:
        pr = pr + [{"text": bio}]
    crypto = sum(1 for i in items if dd.content_signals(i["raw"]).get("quality") == "low_crypto")
    n_pr = len(pr)
    if n_pr:
        P = c["platform_risk"]
        add("platform-rule risks", n_pr, max(P["cap"], n_pr * P["per_hit"]), skip=n_pr >= P["skip_at"],
            note="adult links, threats or follow-for-follow")
    pf = [i for i in items if PROFANITY.search(i["text"])]
    if pf:
        F = c["profanity"]
        share = len(pf) / max(1, len(items))
        pts = F["heavy_pts"] if share >= F["heavy_share"] else max(F["cap"], len(pf) * F["per_hit"])
        add("profanity", len(pf), pts, note=f"{len(pf)} of {len(items)} items")

    # tone
    tone = NA
    if items:
        pos = sum(1 for i in items if POSITIVE.search(i["text"]))
        neg = sum(1 for i in items if NEGATIVE.search(i["text"]) or HARASS.search(i["text"]))
        T = c["tone"]
        if len(items) >= T["min_items"] and neg / len(items) >= T["negative_share"] and neg > pos:
            tone = "negative"
            add("negative tone", neg, T["negative_pts"], note=f"{neg} of {len(items)} items negative")
        elif neg > pos:
            tone = "negative"   # shown, but too few items / too small a share to cost points
        else:
            tone = "positive" if pos > neg else "neutral"
    ordered = [f for f in FLAG_ORDER if f in flags]
    return {"available": bool(items), "n_items": len(items), "tone": tone, "muted": muted, "flags": {f: flags[f] for f in ordered},
            "points": round(sum(flags[f]["pts"] for f in ordered), 2), "skip": any(flags[f]["skip"] for f in ordered),
            "crypto_items": crypto, "top": ordered[0] if ordered else None}


def card_line(a: dict) -> str:
    parts = []
    for name, f in a["flags"].items():
        parts.append(name if name == "You muted this account" else f"{name} ({f['note']})" if f.get("note") else name)
    if not a["available"]:
        return "Account health: " + ("; ".join(parts) + "; posts " + NA if parts else NA)
    body = "; ".join(parts) if parts else NO_ISSUES
    return f"Account health: {body} · tone {a['tone']}"


_RANK = {"Skip": 0, "Maybe": 1, "Follow back": 2}


def apply(score: dict, a: dict, *, cfg: dict | None = None) -> dict:
    """Fold the conduct result into a deep-dive score dict (in place) and return it."""
    score["conduct"] = a
    if not a["flags"]:
        return score
    if score.get("score") is not None:
        score["score"] = round(score["score"] + a["points"], 2)
    score.setdefault("factors", []).extend((f"account health: {n}", f["pts"]) for n, f in a["flags"].items())
    from_dd = (cfg or dd.DEEP_DIVE_CONFIG)["thresholds"]
    rec = score.get("rec") or "Maybe"
    if score.get("score") is not None and not score.get("hard_skip"):
        s = score["score"]
        new = "Follow back" if s >= from_dd["follow_back"] else "Maybe" if s >= from_dd["maybe"] else "Skip"
        if _RANK[new] < _RANK.get(rec, 1):
            rec = new
    for f in a["flags"].values():
        if f.get("max_rec") and _RANK[rec] > _RANK[f["max_rec"]]:
            rec = f["max_rec"]
    if a["skip"]:
        rec = "Skip"
    score["rec"] = rec
    score["reason"] = f"Account health: {a['top']}; " + (score.get("reason") or "")
    return score


BROWSER_MUTE_CHECK_STEPS = (
    "Browser step: muted check (box browser, signed in to X; read only). Only used when X did not return "
    "connection_status for an account.\n"
    "For each account:\n"
    "1. Open https://x.com/<handle>.\n"
    "2. Read the profile only. If it shows \"You muted @<handle>\" / an Unmute control, report muted=True; if the "
    "profile loads normally without that, report muted=False.\n"
    "Never click anything (no Unmute, Follow, Block or Report). STOP on any X warning, captcha or login wall; "
    "unchecked accounts are reported as None and nothing is shown for them."
)

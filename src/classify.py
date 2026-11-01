"""Two-stage X follower classifier (offline on collected records).

Stage 1: cheap screen on list/profile fields → KEEP early or escalate.
Stage 2: deep signals (About country, 30d fingerprint, detailed inactivity).
Never removes accounts. Ambiguous → REVIEW.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal

try:
    from .protections import apply_protections
    from .product_config import ProtectionPolicy
    from .stage1_data import stage1_field_status
except ImportError:  # pragma: no cover
    from protections import apply_protections
    from product_config import ProtectionPolicy
    from stage1_data import stage1_field_status


Stage = Literal["protected", "stage1_keep", "stage2"]
FinalLabel = Literal[
    "PROTECTED / DO NOT REMOVE",
    "ALWAYS KEEP / DO NOT REMOVE",
    "KEEP",
    "REVIEW",
    "BOT-LIKE / REMOVE CANDIDATE",
    "INACTIVE / REMOVE CANDIDATE",
    "BOT-LIKE + INACTIVE / REMOVE CANDIDATE",
    "NOT EVALUATED",
]

# --- Inactivity without an activity sample (2026-10-16) -----------------------
# Defaults; overridable via config "inactivity" block (see config/default_user_config.json).
DEFAULT_INACTIVITY_CONFIG: dict[str, Any] = {
    "use_profile_fields_without_sample": True,  # master switch for the no-sample rules
    "use_profile_last_post_date": True,
    "zero_posts_min_account_age_days": 365,
    "low_post_rate_per_year": 1.0,
    "low_post_rate_min_account_age_days": 730,
}
LAST_POST_DATE_KEYS = (
    "last_post_at", "last_tweet_at", "most_recent_post_at", "status_created_at",
    "last_status_at", "last_activity_at", "last_activity",
)


def _parse_date(value: Any):
    import datetime as _dt
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text or text.lower() in ("not available", "none", "null"):
        return None
    try:
        d = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:  # X legacy format: "Wed Oct 10 20:19:24 +0000 2018"
            d = _dt.datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d


def fallback_enabled(cfg: dict[str, Any] | None) -> bool:
    return bool({**DEFAULT_INACTIVITY_CONFIG, **(cfg or {})}.get("use_profile_fields_without_sample", True))


def has_activity_sample(c: "Candidate") -> bool:
    """True when a usable 30-day activity sample exists (it then takes precedence)."""
    a = c.activity
    return not a.insufficient and bool(a.sample_size) and all(
        v is not None for v in (a.original, a.replies, a.quotes, a.reposts)
    )


def inactivity_evidence(
    c: "Candidate",
    threshold: int = 90,
    cfg: dict[str, Any] | None = None,
    as_of: Any = None,
) -> dict[str, Any]:
    """Evidence-based dormancy read from fields already in scan/profile data.

    Returns {"days": int|None, "days_source": str, "strong": bool, "weak": [..], "signals": [..]}.
    strong = a dated last-post/last-activity at or beyond the threshold (direct evidence).
    weak   = supporting hints (zero posts on an old account, very low lifetime post rate);
             never enough for INACTIVE on their own -> REVIEW. Missing fields are never invented.
    """
    import datetime as _dt
    cfg = {**DEFAULT_INACTIVITY_CONFIG, **(cfg or {})}
    signals: list[str] = []
    days, source = None, "Not available"
    if c.days_inactive is not None:
        days, source = c.days_inactive, "days_inactive"
    elif c.days_inactive_cheap is not None:
        days, source = c.days_inactive_cheap, "days_inactive_cheap"
    elif cfg.get("use_profile_last_post_date", True):
        ref = _parse_date(as_of) or _dt.datetime.now(_dt.timezone.utc)
        for key in LAST_POST_DATE_KEYS:
            raw = (c.raw or {}).get(key) if key != "last_activity_at" else (c.last_activity_at or (c.raw or {}).get(key))
            d = _parse_date(raw)
            if d is not None:
                delta = (ref - d).days
                if delta >= 0:  # future dates are bad data: ignore, never invent
                    days, source = delta, key
                    break
    strong = days is not None and days >= threshold
    if days is None:
        signals.append("Last post date: Not available")
    else:
        signals.append(f"Days since last post/activity: {days} (source: {source})")
    weak: list[str] = []
    age, posts = c.account_age_days, c.post_count
    if posts is not None and posts == 0 and age is not None and age >= int(cfg["zero_posts_min_account_age_days"]):
        weak.append(f"Zero posts on an account ≥ {cfg['zero_posts_min_account_age_days']}d old")
    elif (posts is not None and age is not None and posts > 0
          and age >= int(cfg["low_post_rate_min_account_age_days"])
          and posts / (age / 365.0) < float(cfg["low_post_rate_per_year"])):
        weak.append(f"Very low lifetime post rate (< {cfg['low_post_rate_per_year']}/year)")
    if posts is None:
        signals.append("Post count: Not available")
    if age is None:
        signals.append("Account age: Not available")
    signals.extend(weak)
    return {"days": days, "days_source": source, "strong": strong, "weak": weak, "signals": signals}


@dataclass
class ActivityFingerprint:
    original: int | None = None
    replies: int | None = None
    quotes: int | None = None
    reposts: int | None = None
    sample_size: int | None = None
    window_days: int = 30
    insufficient: bool = False

    def as_percents(self) -> dict[str, Any]:
        if self.insufficient or any(
            p is None for p in (self.original, self.replies, self.quotes, self.reposts)
        ):
            return {
                "original": "Not available",
                "replies": "Not available",
                "quotes": "Not available",
                "reposts": "Not available",
                "sampled": bool(self.sample_size),
                "sample_size": self.sample_size,
                "insufficient": True,
            }
        total = int(self.original) + int(self.replies) + int(self.quotes) + int(self.reposts)
        if total <= 0:
            return {
                "original": 0,
                "replies": 0,
                "quotes": 0,
                "reposts": 0,
                "sampled": bool(self.sample_size),
                "sample_size": self.sample_size,
                "insufficient": False,
            }
        return {
            "original": round(100 * int(self.original) / total),
            "replies": round(100 * int(self.replies) / total),
            "quotes": round(100 * int(self.quotes) / total),
            "reposts": round(100 * int(self.reposts) / total),
            "sampled": bool(self.sample_size),
            "sample_size": self.sample_size,
            "insufficient": False,
        }


try:
    from .about_account import NA, about_from_record, location_mismatch
except ImportError:  # pragma: no cover
    from about_account import NA, about_from_record, location_mismatch

MISMATCH_WEIGHT = 1  # based-in vs connected-via store country: moderate, never proof alone
USERNAME_CHANGES_SIGNAL = 3  # this many username changes or more is a signal (+1)
USERNAME_CHANGES_STRONG = 6  # +2 from here
ID_VERIFIED_KEEP_WEIGHT = 3  # ID verified: strong keep (cancels this much and never bot-like)

@dataclass
class Candidate:
    user_id: str
    username: str
    name: str
    verified: bool = False
    followed_by_me: bool = False
    follow_relationship_source: str = "not_available"
    always_keep: bool = False
    protected: bool = False  # X protected/locked account flag from list scrape
    # Stage-1 cheap fields
    created_at: str | None = None
    account_age_days: int | None = None
    followers_count: int | None = None
    following_count: int | None = None
    post_count: int | None = None
    bio: str | None = None
    profile_image_url: str | None = None
    has_default_avatar: bool | None = None
    profile_complete: bool | None = None  # bio + non-default avatar heuristic
    bio_location: str | None = None
    days_inactive_cheap: int | None = None  # from list/timeline peek if known
    last_activity_at: str | None = None
    # Stage-2 deep fields (may be unset until inspected)
    account_country: str | None = None  # X About "Account based in" only
    account_country_unavailable: bool = False
    # v3.5.8 X "About this account" page (src/about_account.py); every value "Not available" unless read
    about: dict[str, Any] = field(default_factory=dict)
    days_inactive: int | None = None
    activity: ActivityFingerprint = field(default_factory=ActivityFingerprint)
    deep_inspected: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


def _ratio(followers: int | None, following: int | None) -> float | None:
    if followers is None or following is None or following <= 0:
        return None
    return followers / following


CELEBRITY_TOKENS = (
    "elon", "musk", "obama", "trump", "beyonce", "rihanna", "cristiano", "ronaldo", "justinbieber", "bieber",
    "taylorswift", "taylor swift", "billgates", "bill gates", "oprah", "kimkardashian", "mrbeast", "keanu",
)
# "manager"-style scams: an account posing as a celebrity's manager/team/assistant/fan desk.
IMPERSONATION_SCAM_WORDS = (
    "manager", "management", "managing team", "team", "assistant", "booking", "agent", "fan page", "fanpage",
    "fan club", "private", "support", "official", "real", "backup", "personal",
)


def _norm_name(x: Any) -> str:
    return "".join(ch for ch in str(x or "").lower() if ch.isalnum())


class FriendIndex:
    """Fast lookup of accounts the owner follows, for the impersonation flag."""

    def __init__(self, friends: Any = None):
        self.by_handle: dict[str, str] = {}
        self.by_name: dict[str, list[tuple[str, str]]] = {}
        for f in friends or ():
            fh = _norm_name(f.get("handle") or f.get("username")) if isinstance(f, dict) else _norm_name(f)
            fn = _norm_name(f.get("name")) if isinstance(f, dict) else ""
            fid = str((f.get("user_id") if isinstance(f, dict) else "") or "")
            if fh:
                self.by_handle[fh] = fid
                if fn and len(fn) >= 4:
                    self.by_name.setdefault(fn, []).append((fh, fid))

    def match(self, uh: str, un: str, uid: str) -> str | None:
        for fh, fid in self.by_name.get(un, ()):
            if fh != uh and not (fid and uid and fid == uid):
                return fh
        n = len(uh)
        for i in range(n):
            for j in range(i + 4, n + 1):
                sub = uh[i:j]
                if sub != uh and sub in self.by_handle:
                    fid = self.by_handle[sub]
                    if not (fid and uid and fid == uid):
                        return sub
        return None


def _impersonation_signals(name: str, username: str, bio: str | None = None,
                           friends: Any = None, user_id: str | None = None) -> list[str]:
    """Impersonation flag (owner + beta feedback 2026-10-20). Two kinds:
    - posing as a celebrity, e.g. a "manager"/"team"/"fan page" scam (celebrity token + scam word in name/handle/bio);
    - posing as someone the owner follows (friends): same display name or the friend's handle inside a different
      handle, with a different X user ID.
    Digits in a handle are NOT an impersonation signal on their own (number-style handles are only a weak hint)."""
    signals: list[str] = []
    blob = f"{name} {username} {bio or ''}".lower()
    if any(c in blob for c in CELEBRITY_TOKENS) and any(w in blob for w in IMPERSONATION_SCAM_WORDS):
        signals.append("Possible celebrity impersonation (e.g. a 'manager' or fan-page scam)")
    uh, un = _norm_name(username), _norm_name(name)
    idx = friends if isinstance(friends, FriendIndex) else FriendIndex(friends)
    hit = idx.match(uh, un, str(user_id or ""))
    if hit:
        signals.append(f"Possible impersonation of someone you follow (@{hit})")
    return signals


NUMBER_HANDLE_MIN_DIGITS = 4
BIO_HINT_WORDS = ("crypto", "forex", "invest", "trading", "dm me", "dm for", "giveaway", "onlyfans", "promo",
                  "sugar", "whatsapp", "telegram", "earn", "profit")


def handle_bio_screen(c: "Candidate") -> dict[str, Any]:
    """First-pass screen ONLY (beta feedback 2026-10-20). Produces weak hints for the card text; nothing here
    is proof or a counted independent signal. A number-style handle (e.g. name + 8 digits, X's default style)
    is common for real people too."""
    digits = sum(ch.isdigit() for ch in (c.username or ""))
    hints: list[str] = []
    number_style = digits >= NUMBER_HANDLE_MIN_DIGITS
    if number_style:
        hints.append("Hint only: number-style handle (common for real people too; never proof)")
    bio = (c.bio or "").lower()
    words = [w for w in BIO_HINT_WORDS if w in bio]
    if words:
        hints.append("Hint only: bio mentions " + ", ".join(words))
    return {"number_style_handle": number_style, "bio_words": words, "hints": hints}


def _first_int(raw: dict, *keys) -> int | None:
    for k in keys:
        v = (raw or {}).get(k)
        if v is None or isinstance(v, bool):
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


def person_check(c: "Candidate") -> dict[str, Any]:
    """Steps (b) and (c) of the check order (beta feedback 2026-10-20):
    (b) real replies to OTHER people -> real person / keep;
    (c) the numbers: no/almost no posts, very low followers, high following = bot signs;
        real or verified followers -> real person.
    Only accounts failing (b) AND (c) may go on a removal card. Missing data is never invented."""
    raw = c.raw or {}
    replies = _first_int(raw, "replies_to_others", "real_replies_to_others", "replies_to_others_count")
    real_f = _first_int(raw, "known_followers_count", "real_followers_count", "verified_followers_count",
                        "followers_you_know")
    if raw.get("followed_by_verified") is True:
        real_f = max(real_f or 0, 1)
    signs: list[str] = []
    if c.post_count is not None and c.post_count <= 5:
        signs.append("No or almost no posts")
    if c.followers_count is not None and c.followers_count <= 20:
        signs.append("Very low followers")
    fol, fwg = c.followers_count, c.following_count
    if fwg is not None and (fwg >= 500 or (fol is not None and fwg >= 200 and fwg > 5 * max(fol, 1))):
        signs.append("High following")
    return {
        "real_replies": bool(replies and replies > 0),
        "replies_to_others": replies if replies is not None else "Not available",
        "real_followers": bool(real_f and real_f > 0),
        "real_or_verified_followers": real_f if real_f is not None else "Not available",
        "bot_number_signs": signs,
        # all three counts known: only then can "no bot signs" clear an account (missing data stays REVIEW)
        "numbers_complete": None not in (c.post_count, c.followers_count, c.following_count),
    }


def _profile_incomplete(c: Candidate) -> bool:
    if c.profile_complete is False:
        return True
    if c.has_default_avatar is True and not (c.bio and c.bio.strip()):
        return True
    if c.bio is not None and not c.bio.strip() and c.has_default_avatar is True:
        return True
    return False


def stage1_screen(
    c: Candidate,
    inactivity_days_threshold: int = 90,
    policy: ProtectionPolicy | None = None,
    inactivity_cfg: dict[str, Any] | None = None,
    as_of: Any = None,
    friends: Any = None,
) -> dict[str, Any]:
    """Cheap screen. Protections run BEFORE bot-risk scoring.

    Returns disposition: protected | stage1_keep | stage1_deferred | escalate (stage2).
    """
    policy = policy or ProtectionPolicy(inactivity_days_threshold=inactivity_days_threshold)
    if inactivity_days_threshold != policy.inactivity_days_threshold:
        # caller threshold wins for this screen
        policy.inactivity_days_threshold = inactivity_days_threshold

    gated = apply_protections(c, policy)
    if gated is not None:
        return gated.as_stage1_result()

    signals: list[str] = ["Unverified"]
    suspicion = 0
    # Evidence-only note (no score impact): where the follow relationship came from.
    _src = getattr(c, "follow_relationship_source", "not_available")
    signals.append(
        "You follow: Not available (relationship evidence missing)"
        if _src == "not_available"
        else f"You follow: {'Yes' if c.followed_by_me else 'No'} (source: {_src})"
    )

    ratio = _ratio(c.followers_count, c.following_count)
    if ratio is not None and c.following_count and c.following_count >= 1500 and ratio < 0.05:
        suspicion += 2
        signals.append("Extreme following ratio")
    elif ratio is not None and c.following_count and c.following_count >= 800 and ratio < 0.1:
        suspicion += 1
        signals.append("High following vs followers")

    if c.followers_count is not None and c.followers_count <= 20 and (c.following_count or 0) >= 500:
        suspicion += 1
        signals.append("Very low followers with large following")

    if c.account_age_days is not None and c.account_age_days < 30:
        suspicion += 1
        signals.append("Very new account (<30d)")
    elif c.account_age_days is not None and c.account_age_days < 90 and (c.post_count or 0) < 5:
        suspicion += 1
        signals.append("Young account with almost no posts")

    if c.post_count is not None and c.post_count == 0:
        suspicion += 1
        signals.append("Zero posts")

    if _profile_incomplete(c):
        suspicion += 1
        signals.append("Sparse/incomplete profile")

    # (a) handle/bio first-pass screen: weak hints only, never counted (beta feedback 2026-10-20)
    screen = handle_bio_screen(c)
    signals.extend(screen["hints"])

    imp = _impersonation_signals(c.name, c.username, c.bio, friends, c.user_id)
    signals.extend(imp)
    if imp:
        suspicion += 2

    cheap_inactive = c.days_inactive_cheap
    if cheap_inactive is not None and cheap_inactive >= inactivity_days_threshold:
        suspicion += 1
        signals.append(f"Possibly inactive ≥ {inactivity_days_threshold}d (cheap peek)")

    dormancy_escalate = False
    if fallback_enabled(inactivity_cfg) and not has_activity_sample(c):
        ev = inactivity_evidence(c, inactivity_days_threshold, inactivity_cfg, as_of)
        if ev["strong"] or ev["weak"]:
            dormancy_escalate = True
            signals.append("Possible dormancy without activity sample — escalate for inactivity check")

    # List-only scrapes often omit counts/age. Missing Stage-1-critical fields must
    # not be treated as "normal" — defer/escalate for cheap enrichment (never invent).
    field_probe = {
        "followers_count": c.followers_count,
        "following_count": c.following_count,
        "account_age_days": c.account_age_days,
        "created_at": c.created_at,
        "post_count": c.post_count,
    }
    field_status = stage1_field_status(field_probe)
    insufficient_stage1_fields = not field_status["stage1_ready"]
    if insufficient_stage1_fields:
        signals.append(
            "Insufficient Stage 1 fields — STAGE1_DEFER_MISSING_FIELDS: "
            + ", ".join(field_status["missing_fields"])
        )

    # Normal-looking with enough cheap fields → KEEP without Stage 2
    if suspicion == 0 and not insufficient_stage1_fields and not dormancy_escalate:
        return {
            "stage": "stage1_keep",
            "label": "KEEP",
            "escalate": False,
            "suspicion": 0,
            "signals": signals + ["Cleared Stage 1 cheap screen"],
            "stage1_status": field_status["status"],
        }

    if insufficient_stage1_fields and suspicion == 0:
        return {
            "stage": "stage1_deferred",
            "label": None,
            "escalate": True,
            "suspicion": 0,
            "signals": signals,
            "stage1_status": field_status["status"],
            "missing_fields": field_status["missing_fields"],
        }

    return {
        "stage": "stage2",
        "label": None,
        "escalate": True,
        "suspicion": suspicion,
        "signals": signals,
        "stage1_status": field_status.get("status"),
    }


def stage2_classify(
    c: Candidate,
    stage1: dict[str, Any],
    inactivity_days_threshold: int = 90,
    policy: ProtectionPolicy | None = None,
    inactivity_cfg: dict[str, Any] | None = None,
    as_of: Any = None,
) -> dict[str, Any]:
    """Deep classification after expensive inspection fields are filled when possible.

    Stage 2 principles:
    - About "Account based in" only when X provides it (never invent / no nationality inference
      from name/pic/language).
    - Country is a configurable signal but never alone = bot.
    - 30-day fingerprint ratios; bot-like vs inactive are distinct labels.
    """
    policy = policy or ProtectionPolicy(inactivity_days_threshold=inactivity_days_threshold)
    signals = list(stage1.get("signals") or [])
    bot_score = int(stage1.get("suspicion") or 0)

    # v3.5.8 About this account. Location alone is never a judgment (no nationality inference): a watched
    # country only adds weight when other bot signals are already present, and a based-in / connected-via
    # mismatch is a moderate signal (+1), never proof on its own.
    about = about_from_record({"about": c.about}) if c.about else about_from_record({})
    if about.get("account_based_in") != NA and (c.account_country is None or c.account_country_unavailable):
        c.account_country = str(about["account_based_in"])
        c.account_country_unavailable = False
    other_signals = bot_score
    watched = {w.strip().lower() for w in (policy.watched_countries or [])}
    if c.account_country_unavailable or c.account_country is None:
        country_display = "Not available"
        signals.append("Account based in: Not available")
    else:
        country_display = c.account_country
        signals.append(f"X account country: {c.account_country}")
        if c.account_country.strip().lower() in watched and other_signals >= 1:
            bot_score += int(policy.country_signal_weight or 1)
            signals.append("Country is one signal among many (not decisive alone)")
    mismatch = location_mismatch({**about, "account_based_in": c.account_country or about.get("account_based_in")})
    if mismatch:
        bot_score += MISMATCH_WEIGHT
        signals.append(f"About: based in {mismatch['based_in']} but connected via {mismatch['connected_via']} "
                       "(possible VPN or bot; one signal, not proof)")
    uc = about.get("username_changes")
    if isinstance(uc, int) and not isinstance(uc, bool) and uc >= USERNAME_CHANGES_SIGNAL:
        bot_score += 2 if uc >= USERNAME_CHANGES_STRONG else 1
        signals.append(f"About: {uc} username changes")
    id_verified = about.get("id_verified") == "Yes"
    if id_verified:
        bot_score = max(0, bot_score - ID_VERIFIED_KEEP_WEIGHT)
        signals.append("About: ID verified (strong keep signal)")

    perc = c.activity.as_percents()
    activity_insufficient = bool(perc.get("insufficient"))
    if activity_insufficient:
        signals.append("30-day activity: Not available / insufficient")
    else:
        if isinstance(perc.get("reposts"), int) and perc["reposts"] >= 90:
            bot_score += 2
            signals.append("Very high repost ratio")
        elif isinstance(perc.get("reposts"), int) and perc["reposts"] >= 75:
            bot_score += 1
            signals.append("High repost ratio")
        if isinstance(perc.get("original"), int) and isinstance(perc.get("replies"), int):
            if perc["original"] + perc["replies"] <= 8 and isinstance(perc.get("reposts"), int) and perc["reposts"] >= 70:
                bot_score += 1
                signals.append("Almost no conversational activity")
        if perc.get("sampled"):
            signals.append(f"Activity stats are SAMPLE (n={perc.get('sample_size')})")

    sample_present = has_activity_sample(c)
    dormancy_weak: list[str] = []
    if sample_present or not fallback_enabled(inactivity_cfg):
        # Activity sample takes precedence: original days-based behaviour, unchanged.
        days = c.days_inactive if c.days_inactive is not None else c.days_inactive_cheap
        inactive = days is not None and days >= inactivity_days_threshold
    else:
        ev = inactivity_evidence(c, inactivity_days_threshold, inactivity_cfg, as_of)
        days = ev["days"]
        inactive = bool(ev["strong"])  # only a dated last post/activity counts as strong
        dormancy_weak = list(ev["weak"])
        for sig in ev["signals"]:
            if sig not in signals:
                signals.append(sig)
        if not inactive and dormancy_weak:
            signals.append("Dormancy hints only (not enough for INACTIVE) — Needs your look")
    if inactive:
        signals.append(f"Inactive ≥ {inactivity_days_threshold}d")

    bot_like = bot_score >= 3
    if any("impersonation" in s.lower() for s in signals) and bot_score >= 2:
        bot_like = True
    if mismatch and not bot_like and bot_score <= MISMATCH_WEIGHT:
        bot_like = False  # a mismatch alone is never enough: at most REVIEW
    if id_verified:
        bot_like = False

    if bot_like and inactive:
        label: FinalLabel = "BOT-LIKE + INACTIVE / REMOVE CANDIDATE"
    elif bot_like:
        label = "BOT-LIKE / REMOVE CANDIDATE"
    elif inactive:
        label = "INACTIVE / REMOVE CANDIDATE"
    elif bot_score >= 1 or activity_insufficient or dormancy_weak:
        # Ambiguous or missing deep data → REVIEW, not pretend certainty
        label = "REVIEW"
    else:
        label = "KEEP"

    return {
        "stage": "stage2",
        "label": label,
        "bot_like": bot_like,
        "inactive": inactive,
        "candidate": label.endswith("REMOVE CANDIDATE") or label == "REVIEW",
        "bot_score": bot_score,
        "signals": signals,
        "activity_percents": perc,
        "activity_insufficient": activity_insufficient,
        "account_country_display": country_display,
        "account_country_unavailable": country_display == "Not available",
        "about": about,
        "about_location_mismatch": bool(mismatch),
        "id_verified": id_verified,
        "last_activity": c.last_activity_at or "Not available",
        "days_inactive": days if days is not None else "Not available",
        "deep_inspected": c.deep_inspected,
        "inactivity_basis": "activity_sample" if sample_present else "profile_fields",
        "dormancy_hints": dormancy_weak,
    }


def classify(
    c: Candidate,
    inactivity_days_threshold: int = 90,
    policy: ProtectionPolicy | None = None,
    inactivity_cfg: dict[str, Any] | None = None,
    as_of: Any = None,
    friends: Any = None,
) -> dict[str, Any]:
    """Check order (beta feedback 2026-10-20): protections first; then (a) handle/bio screen (weak hints only),
    (b) real replies to other people => real person / keep, (c) the numbers (posts, followers, following;
    real or verified followers => real person). Only accounts failing (b) AND (c) go on a removal card.
    Inactivity (dated evidence) and the impersonation flag are separate reasons; country stays one counted
    signal among many. friends = accounts the owner follows ({handle, name, user_id}) for the impersonation flag."""
    policy = policy or ProtectionPolicy(inactivity_days_threshold=inactivity_days_threshold)
    s1 = stage1_screen(c, inactivity_days_threshold=inactivity_days_threshold, policy=policy,
                       inactivity_cfg=inactivity_cfg, as_of=as_of, friends=friends)
    if not s1.get("escalate"):
        return {
            **s1,
            "bot_like": False,
            "inactive": False,
            "candidate": False,
            "activity_percents": c.activity.as_percents(),
            "activity_insufficient": False,
            "account_country_display": c.account_country or "Not available",
            "account_country_unavailable": c.account_country is None,
            "about": about_from_record({"about": c.about}) if c.about else about_from_record({}),
            "last_activity": c.last_activity_at or "Not available",
            "days_inactive": c.days_inactive_cheap if c.days_inactive_cheap is not None else "Not available",
            "deep_inspected": False,
            "stage1": s1,
            "removal_card_eligible": False,
            "impersonation_flag": any("impersonation" in x.lower() for x in s1.get("signals") or []),
            "person_check": person_check(c),
        }
    # If escalated but deep fields not filled, still classify conservatively as REVIEW
    if not c.deep_inspected:
        if not (c.about or {}).get("about_read"):
            c.account_country_unavailable = True
        c.activity.insufficient = True
    s2 = stage2_classify(c, s1, inactivity_days_threshold=inactivity_days_threshold, policy=policy,
                         inactivity_cfg=inactivity_cfg, as_of=as_of)
    s2 = _apply_person_check(c, s2)
    # Hard verified guard (v3.5.13): if deep inspection revealed Verified (e.g. on the About page), the
    # account is protected and never a suspect, whatever it scored.
    gated = apply_protections(c, policy)
    if gated is not None and gated.gate == "verified":
        out = gated.as_stage1_result()
        out.update({"stage1": s1, "removal_card_eligible": False, "deep_inspected": c.deep_inspected,
                    "about": s2.get("about"), "person_check": s2.get("person_check")})
        return out
    s2["stage1"] = s1
    s2["escalate"] = True
    if s1.get("stage1_status"):
        s2["stage1_status"] = s1["stage1_status"]
    if s1.get("missing_fields"):
        s2["missing_fields"] = s1["missing_fields"]
    if s1.get("stage") == "stage1_deferred":
        s2["stage1_deferred"] = True
    return s2


BOT_LABELS = ("BOT-LIKE / REMOVE CANDIDATE", "BOT-LIKE + INACTIVE / REMOVE CANDIDATE", "REVIEW")


def _apply_person_check(c: Candidate, r: dict[str, Any]) -> dict[str, Any]:
    """Apply steps (b)/(c) to a stage-2 result. Never adds a removal reason; only takes bot reasons away."""
    ev = person_check(c)
    r["person_check"] = ev
    sig = r.setdefault("signals", [])
    impersonation = any("impersonation" in s_.lower() for s_ in sig)
    r["impersonation_flag"] = impersonation
    label = r.get("label")
    if label in BOT_LABELS and not impersonation:
        reason = None
        if ev["real_replies"]:
            reason = "Real person: replies to other people"
        elif ev["real_followers"]:
            reason = "Real person: followed by real or verified accounts"
        elif not ev["bot_number_signs"] and ev["numbers_complete"]:
            reason = "Numbers look like a person (no bot signs in posts/followers/following)"
        if reason:
            sig.append(reason + " - not a bot candidate")
            r["bot_like"] = False
            new = "INACTIVE / REMOVE CANDIDATE" if r.get("inactive") else "KEEP"
            r["label"] = new
            r["candidate"] = new.endswith("REMOVE CANDIDATE")
    elif ev["bot_number_signs"] and label in BOT_LABELS:
        sig.append("Bot signs in the numbers: " + ", ".join(ev["bot_number_signs"]))
    r["removal_card_eligible"] = bool(r.get("candidate"))
    return r


def demo() -> None:
    samples = [
        Candidate(
            user_id="1", username="ElonMusk298472", name="Elon Musk Manager",
            followers_count=8, following_count=3821, post_count=400,
            account_age_days=40, has_default_avatar=False, bio="CEO",
            deep_inspected=True, account_country="Nigeria",
            activity=ActivityFingerprint(2, 2, 0, 96, sample_size=100),
            days_inactive=2,
        ),
        Candidate(
            user_id="2", username="normal_person", name="Normal Person",
            followers_count=350, following_count=320, post_count=1200,
            account_age_days=2000, bio="hello", has_default_avatar=False,
            days_inactive_cheap=3,
        ),
        Candidate(
            user_id="3", username="friend_unverified", name="Real Friend",
            followed_by_me=True, days_inactive_cheap=400,
        ),
        Candidate(
            user_id="4", username="quiet_acct", name="Quiet",
            followers_count=80, following_count=90, post_count=40,
            account_age_days=800, bio="hi", has_default_avatar=False,
            days_inactive_cheap=200, deep_inspected=True,
            account_country_unavailable=True,
            activity=ActivityFingerprint(insufficient=True),
            days_inactive=200,
        ),
    ]
    for s in samples:
        r = classify(s)
        print(s.username, "->", r.get("stage"), r.get("label"), "escalate" if r.get("escalate") else "")


if __name__ == "__main__":
    demo()

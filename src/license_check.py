"""Subscription gate for this Follower Finder install.

The license server learns an install id, the product code, and Stripe status.
This file never sends an X handle, password, token, cookie, or follower profile.
Records and the X login are not deleted when a check fails.

The shared server (Bot Terminator's license Worker) accepts a check for product
"ff". A subscription stored as "both" is applied there: billing.covers treats
product "both" as covering "ff" and "bt". This client only sends "ff".
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
ACTIONS = ("scan", "recommend", "follow", "write")
STATE_NAME = "install_license.json"
PUBLIC_SUBSCRIBE = "https://empressheavy.com/subscribe/"


def package_version() -> str:
    """First line of the package VERSION file. Fallback matches the current template."""
    try:
        line = (PACKAGE / "VERSION").read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        line = ""
    return line if re.fullmatch(r"v\d+(?:\.\d+){1,3}", line or "") else "v3.5.37"


def user_agent() -> str:
    """Cloudflare error 1010 blocks urllib's default User-Agent. Send this on every license call."""
    return f"FollowerFinder/{package_version()}"


class LicenseError(RuntimeError):
    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


def _config_path(root: Path) -> Path:
    local = root / "config" / "license.json"
    if local.is_file():
        return local
    return PACKAGE / "config" / "license.json"


def load_config(root: Path | None = None) -> dict:
    root = Path(root) if root is not None else PACKAGE
    cfg = json.loads(_config_path(root).read_text(encoding="utf-8"))
    url = os.environ.get("FOLLOWER_FINDER_LICENSE_URL", "").strip()
    if url:
        cfg["base_url"] = url
    grace = os.environ.get("FOLLOWER_FINDER_LICENSE_GRACE_HOURS", "").strip()
    if grace:
        cfg["grace_hours"] = float(grace)
    return cfg


def _state_root(root: Path) -> Path:
    """updater.apply replaces HOME/app and leaves HOME alone. Keep the install id in HOME."""
    if root.name == "app":
        return root.parent
    return root


def state_path(root: Path | None = None) -> Path:
    root = Path(root) if root is not None else PACKAGE
    return _state_root(root) / "data" / STATE_NAME


def load_state(root: Path | None = None) -> dict:
    path = state_path(root)
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def save_state(state: dict, root: Path | None = None) -> None:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = {
        "install_id": state.get("install_id") or "",
        "last_active_at": state.get("last_active_at"),
        "last_renew_url": state.get("last_renew_url") or "",
        "last_price": state.get("last_price"),
    }
    path.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _request_headers() -> dict:
    return {"content-type": "application/json", "User-Agent": user_agent()}


def _post(url: str, payload: dict, timeout: float, transport):
    body = json.dumps(payload).encode("utf-8")
    headers = _request_headers()
    if transport is not None:
        status, raw = transport(url, payload)
        return status, raw
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def subscribe_link(base: str, install_id: str = "", product: str = "ff", server_url: str = "") -> str:
    """Pay link for this install, or the public subscribe page when the install id is unknown.

    The license server's renew_url omits the product. Checkout needs product=ff so a Follower Finder
    install is not sent through the Bot Terminator price.
    """
    product = (product or "ff").strip() or "ff"
    install_id = (install_id or "").strip()
    base = (base or "").rstrip("/")
    if server_url and not (base and install_id):
        parsed = urllib.parse.urlparse(server_url)
        query = urllib.parse.parse_qs(parsed.query)
        if not install_id:
            install_id = (query.get("install_id") or [""])[0]
        if not base and parsed.scheme in ("https", "http") and parsed.netloc:
            base = f"{parsed.scheme}://{parsed.netloc}"
    if base and install_id:
        query = urllib.parse.urlencode([("install_id", install_id), ("product", product)])
        return f"{base}/pay?{query}"
    return PUBLIC_SUBSCRIBE


def _message(kind: str, *, price: dict | None, renew_url: str, grace_hours: float) -> str:
    label = (price or {}).get("label") or "Follower Finder"
    display = (price or {}).get("display") or ""
    priced = f" ({display})" if display else ""
    link = f" You can renew here: {renew_url}" if renew_url else ""
    if kind == "inactive":
        return (f"Your {label} subscription{priced} isn't active, so I'm not going to scan or analyze followers, "
                f"suggest accounts to follow, or follow, unfollow, or change anything on X right now. "
                f"Your records are still here, and I haven't changed your X login.{link}")
    if kind == "grace_over":
        return (f"I couldn't confirm your {label} subscription, and it's been more than {grace_hours:g} hours "
                f"since the last confirmation, so I'm holding off on scans, suggestions, follows, and other "
                f"changes on X. Your records are still here, and I haven't changed your X login.{link}")
    return (f"I couldn't reach the subscription check for {label}, and this install hasn't been confirmed yet, "
            f"so I'm holding off on scans, suggestions, follows, and other changes on X. Your records are still "
            f"here, and I haven't changed your X login.{link}")


def evaluate(payload: dict | None, *, now: datetime, last_active_at, grace_hours: float,
             unreachable: bool, renew_url: str = "", price: dict | None = None) -> dict:
    """Decide whether an action may proceed. Unreachable uses the grace window. Inactive does not."""
    if not unreachable and payload is not None:
        if payload.get("active") is True:
            return {"allow": True, "reason": "active", "renew_url": payload.get("renew_url") or renew_url,
                    "price": payload.get("price") or price}
        return {"allow": False, "reason": "inactive",
                "message": _message("inactive", price=payload.get("price") or price,
                                    renew_url=payload.get("renew_url") or renew_url, grace_hours=grace_hours)}
    last = _parse_time(last_active_at)
    if last is not None and now - last <= timedelta(hours=float(grace_hours)):
        return {"allow": True, "reason": "grace", "renew_url": renew_url, "price": price}
    kind = "grace_over" if last is not None else "never"
    return {"allow": False, "reason": kind,
            "message": _message(kind, price=price, renew_url=renew_url, grace_hours=grace_hours)}


def register(*, root=None, transport=None, config=None) -> dict:
    cfg = config or load_config(root)
    base = str(cfg.get("base_url") or "").rstrip("/")
    page = subscribe_link("", "", str(cfg.get("product") or "ff"))
    if not base:
        raise LicenseError(_message("never", price=None, renew_url=page, grace_hours=float(cfg.get("grace_hours") or 72)),
                           reason="unconfigured")
    try:
        status, raw = _post(f"{base}/register", {}, float(cfg.get("timeout_seconds") or 10), transport)
    except Exception:
        raise LicenseError(_message("never", price=None, renew_url=page, grace_hours=float(cfg.get("grace_hours") or 72)),
                           reason="unreachable") from None
    if status != 201:
        raise LicenseError(_message("never", price=None, renew_url=page, grace_hours=float(cfg.get("grace_hours") or 72)),
                           reason="unreachable")
    body = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
    install_id = str(body.get("install_id") or "")
    if not install_id:
        raise LicenseError(_message("never", price=None, renew_url=page, grace_hours=float(cfg.get("grace_hours") or 72)),
                           reason="unreachable")
    state = load_state(root)
    state["install_id"] = install_id
    save_state(state, root)
    return {"install_id": install_id}


def require(action: str, *, root=None, now: datetime | None = None, transport=None) -> dict:
    if action not in ACTIONS:
        raise LicenseError(f"Unknown license action {action}.", reason="bad_action")
    cfg = load_config(root)
    grace = float(cfg.get("grace_hours") or 72)
    base = str(cfg.get("base_url") or "").rstrip("/")
    product = str(cfg.get("product") or "ff")
    state = load_state(root)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    def _link(server_url: str = "") -> str:
        return subscribe_link(base, str(state.get("install_id") or ""), product,
                              server_url or str(state.get("last_renew_url") or ""))

    if not base:
        decision = evaluate(None, now=now, last_active_at=state.get("last_active_at"), grace_hours=grace,
                            unreachable=True, renew_url=_link(), price=state.get("last_price"))
        if not decision["allow"]:
            raise LicenseError(decision["message"], reason="unconfigured")
        return decision
    if not state.get("install_id"):
        try:
            registered = register(root=root, transport=transport, config=cfg)
        except LicenseError:
            decision = evaluate(None, now=now, last_active_at=None, grace_hours=grace, unreachable=True,
                                renew_url=_link())
            raise LicenseError(decision["message"], reason=decision["reason"]) from None
        state["install_id"] = registered["install_id"]
    url = f"{base}/check"
    # "both" is not sent. The server marks an ff check active when the stored product is ff or both.
    payload = {"install_id": state["install_id"], "product": product}
    try:
        status, raw = _post(url, payload, float(cfg.get("timeout_seconds") or 10), transport)
        body = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        if status != 200 or not isinstance(body, dict):
            raise ValueError("bad check response")
        unreachable = False
    except Exception:
        body = None
        unreachable = True
    link = _link(str(body.get("renew_url") or "") if isinstance(body, dict) else "")
    if isinstance(body, dict):
        body = dict(body)
        body["renew_url"] = link
    decision = evaluate(body, now=now, last_active_at=state.get("last_active_at"), grace_hours=grace,
                        unreachable=unreachable, renew_url=link, price=state.get("last_price"))
    if not unreachable and isinstance(body, dict):
        state["last_renew_url"] = link
        state["last_price"] = body.get("price")
        state["last_active_at"] = now.isoformat() if body.get("active") is True else None
        save_state(state, root)
    if not decision["allow"]:
        raise LicenseError(decision["message"], reason=decision["reason"])
    return decision


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else ""
    try:
        if cmd == "register":
            print(json.dumps(register()))
            return 0
        if cmd == "require" and len(args) > 1:
            require(args[1])
            print("subscription active")
            return 0
    except LicenseError as exc:
        print(str(exc))
        return 2
    print("usage: python -m src.license_check register | require scan|recommend|follow|write", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

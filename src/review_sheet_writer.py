"""Write review-sheet rows to the owner's own Google Sheet, or to a CSV for manual import.

Rows come from populate_review_sheet.build_rows (In review). Nothing here touches X.

Default path: the Google Sheets CONNECTOR in the user's own Grok Bot. See sheet_connector.py and the README
section "Writing the review sheet". The direct Google API below is an ADVANCED FALLBACK (sheets.backend=google_api).

Google API fallback (tested against a mocked API and a connector bridge, not a real Google login):
- Install the optional extras: `pip install -r requirements-sheets.txt`
  (google-api-python-client, google-auth, google-auth-oauthlib). Imported lazily, only when this path is used.
- Recommended: the owner signs in to THEIR OWN Google account (OAuth), so the sheet is created in their own
  Drive. Set FOLLOWER_FINDER_GOOGLE_OAUTH_CLIENT to the path of an OAuth "Desktop app" client JSON the owner
  created in their own Google Cloud project; the first call opens a browser sign-in and the token is cached
  at data/users/<user_key>/google_token.json (0600). Pass user_key=... so the token stays per user.
- Alternative: FOLLOWER_FINDER_GOOGLE_CREDENTIALS = path to a service-account JSON key. A service account has
  its own Drive, so use it only with spreadsheet_id= of a sheet in your Drive shared with its email.
- Nothing is bundled: no client secrets, keys or tokens ship with the package.
- The sheet ID, once created, belongs in the owner's own config (data/users/<key>/config.json:
  review_sheet_id) — never in shared code.

Offline fallback: write_rows_csv() writes the same header + rows to a CSV the owner can import into Sheets
(File > Import > Upload).
"""
from __future__ import annotations

import csv
import re
import os
from pathlib import Path
from typing import Any, Callable, Sequence

CREDENTIALS_ENV = "FOLLOWER_FINDER_GOOGLE_CREDENTIALS"      # service-account key (alternative)
OAUTH_CLIENT_ENV = "FOLLOWER_FINDER_GOOGLE_OAUTH_CLIENT"    # owner's own OAuth desktop client (recommended)
SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
REVIEW_QUEUE_TAB = "In review"
REVIEW_QUEUE_HEADER = [
    "Select", "Review ID", "Display name", "X user ID", "Handle", "Classification", "Reason", "Protection",
    "Follows you", "Batch", "Status", "Eligible", "Is bot", "Review date", "Decision", "Decided at", "Source", "Notes",
]


def _csv_cell(v):
    return "" if v is None else str(v)


def write_rows_csv(rows: Sequence[Sequence[Any]], path: str | Path, *, header: Sequence[str] = REVIEW_QUEUE_HEADER,
                   dedupe_key: Callable[[Sequence[Any]], Any] | None = None) -> Path:
    """Offline fallback: UTF-8 CSV, APPEND-ONLY (owner rule 2026-10-16 7:50 PM PT).

    New file: header + rows. Existing file: rows are appended after the last existing row; existing rows are never
    rewritten and the header is not repeated. dedupe_key (optional) skips rows whose key is already in the file.
    Formulas such as =HYPERLINK are kept as text and evaluate when imported into Google Sheets."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    seen = set()
    if not new and dedupe_key is not None:
        with open(path, newline="", encoding="utf-8") as f:
            seen = {dedupe_key(r) for i, r in enumerate(csv.reader(f)) if i > 0}
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(list(header))
        for r in rows:
            cells = [_csv_cell(v) if v is None else v for v in r]
            if dedupe_key is not None:
                k = dedupe_key([_csv_cell(v) for v in r])
                if k in seen:
                    continue
                seen.add(k)
            w.writerow(cells)
    try:
        os.chmod(path, 0o644)  # CSV export is a report: 0644 (readable report)
    except OSError:  # pragma: no cover
        pass
    return path


def _oauth_service(client_path: str, user_key: str | None, root=None):
    try:
        from google.oauth2.credentials import Credentials  # type: ignore
        from google.auth.transport.requests import Request  # type: ignore
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError("Google Sheets support is optional: pip install -r requirements-sheets.txt") from exc
    if not user_key:
        raise RuntimeError("user_key is required for Google sign-in (the token is stored per user)")
    try:
        from storage import user_data_root
    except ImportError:  # pragma: no cover
        from .storage import user_data_root
    token = user_data_root(user_key, root) / "google_token.json"
    creds = Credentials.from_authorized_user_file(str(token), list(SCOPES)) if token.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        creds = InstalledAppFlow.from_client_secrets_file(client_path, list(SCOPES)).run_local_server(port=0)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(token, 0o600)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _build_service(credentials_path: str | Path | None = None, *, user_key: str | None = None, root=None):
    """Create a Sheets API client from the owner's own credentials (lazy optional import).
    Prefers the owner's own Google sign-in (OAuth); falls back to a service-account key."""
    oauth = os.environ.get(OAUTH_CLIENT_ENV)
    if oauth and not credentials_path:
        return _oauth_service(oauth, user_key, root)
    cred = credentials_path or os.environ.get(CREDENTIALS_ENV)
    if not cred:
        raise RuntimeError(f"No Google credentials: set {OAUTH_CLIENT_ENV} (sign in to your own Google account; "
                           f"recommended) or {CREDENTIALS_ENV} (service-account key), or use write_rows_csv "
                           "for the offline CSV path.")
    try:
        from google.oauth2 import service_account  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError("Google Sheets support is optional: pip install -r requirements-sheets.txt") from exc
    creds = service_account.Credentials.from_service_account_file(str(cred), scopes=list(SCOPES))
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def create_or_open_sheet(*, spreadsheet_id: str | None = None, title: str = "Follower Finder review",
                         service=None, credentials_path: str | Path | None = None, user_key: str | None = None,
                         root=None) -> str:
    """Return a spreadsheet ID: the given one (verified to open), or a newly created sheet with a In review tab
    (created in the signed-in owner's own Drive when using OAuth)."""
    service = service or _build_service(credentials_path, user_key=user_key, root=root)
    sheets = service.spreadsheets()
    if spreadsheet_id:
        sheets.get(spreadsheetId=spreadsheet_id).execute()
        return spreadsheet_id
    body = {"properties": {"title": title}, "sheets": [{"properties": {"title": REVIEW_QUEUE_TAB}}]}
    return sheets.create(body=body, fields="spreadsheetId").execute()["spreadsheetId"]


def existing_rows(service, spreadsheet_id: str, tab: str = REVIEW_QUEUE_TAB) -> list[list[Any]]:
    """All rows currently in the tab (A:R). The API trims trailing empty rows, so len() is the last used row."""
    resp = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=f"'{tab}'!A:R").execute()
    vals = resp.get("values", []) if isinstance(resp, dict) else []
    return vals if isinstance(vals, list) else []


def next_empty_row(service, spreadsheet_id: str, tab: str = REVIEW_QUEUE_TAB) -> int:
    return len(existing_rows(service, spreadsheet_id, tab)) + 1


def _start_row(updated_range: str) -> int | None:
    m = re.search(r"!\$?[A-Z]+\$?(\d+)", updated_range or "")
    return int(m.group(1)) if m else None


def write_rows_to_sheet(rows, *, spreadsheet_id: str, first_row: int = 4,
                        tab: str = REVIEW_QUEUE_TAB, write_header: bool = True, service=None,
                        credentials_path: str | Path | None = None, user_key: str | None = None,
                        root=None, dedupe_key: Callable[[Sequence[Any]], Any] | None = None) -> dict[str, Any]:
    """Append rows with Google's ATOMIC spreadsheets.values.append (owner rule 2026-10-16 7:57 PM PT).

    The write is a single values.append (USER_ENTERED, insertDataOption=INSERT_ROWS) anchored on the table
    ('<tab>'!A<first_row-1>:R), so Sheets itself puts the rows after the table's last row: no fixed start row,
    nothing overwritten even if the sheet changed since we looked. A read happens first only to detect an empty
    tab (header + rows are then appended together, giving header on row first_row-1 and data from first_row)
    and for dedupe_key (skips rows whose key already appears in the tab). rows may be a list or a
    callable(start_row) -> rows (kept for compatibility; formulas are position-independent now).
    start_row in the result comes from the append response's updates.updatedRange."""
    service = service or _build_service(credentials_path, user_key=user_key, root=root)
    existing = existing_rows(service, spreadsheet_id, tab)
    header = (not existing) and write_header and first_row > 1
    anchor = first_row - 1 if first_row > 1 and (header or existing) else first_row
    guess = len(existing) + 1 if existing else first_row
    rows = list(rows(guess) if callable(rows) else rows)
    if dedupe_key is not None:
        seen = {dedupe_key([_csv_cell(v) for v in r]) for r in existing}
        kept = []
        for r in rows:
            k = dedupe_key([_csv_cell(v) for v in r])
            if k not in seen:
                seen.add(k); kept.append(r)
        rows = kept
    values = ([list(REVIEW_QUEUE_HEADER)] if header and rows else []) + [["" if v is None else v for v in r] for r in rows]
    resp, start = {}, None
    if values:
        resp = service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id, range=f"'{tab}'!A{anchor}:R", valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS", body={"values": values}).execute()
        start = _start_row(((resp or {}).get("updates") or {}).get("updatedRange", "")) if isinstance(resp, dict) else None
        if start is not None and header and rows:
            start += 1  # first data row is below the header we appended with it
    return {"spreadsheet_id": spreadsheet_id, "rows_written": len(rows), "start_row": start,
            "header_written": bool(header and rows), "response": resp}

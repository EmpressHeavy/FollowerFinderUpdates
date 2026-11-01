"""v3.5.5: full-fidelity Sheet import + the standard GitHub records mirror (readable JSON, one file per Sheet tab).

1. import_sheet(): copy EVERY tab of the owner's old Google Sheet into terminator.db (tables sheet_tabs + sheet_rows),
   keeping every column (original order and names, duplicates included), every row and every link formula
   (=HYPERLINK(...)). Read-only on the Sheet: the bot reads it twice with read_range (displayed values and
   valueRenderOption=FORMULA) and passes both here. Idempotent (append-only, UNIQUE(tab,row_num)).
2. export_mirror(): write records/ (pretty JSON) that mirrors the Sheet tab by tab plus every DB table, with
   cross references by X user ID, handle and snapshot ID, an index.json and a README.md. This is what the
   GitHub backup pushes (records.push_github).
3. sheet_values_from_mirror() / restore_from_mirror(): read records/ back, rebuild the Sheet cell grid (formulas
   included) or the whole terminator.db.
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

try:
    from . import records as rec
except ImportError:  # pragma: no cover
    import records as rec

FORMAT = "bot-terminator-records/1"
MIRROR_DIR = "records"
DEFAULT_HEADER_ROW = 1
HEADER_ROWS = {"REVIEW QUEUE": 3}  # REVIEW QUEUE has 2 control/notice rows above its header (frozen rows = 3)
_URL = re.compile(r'https?://[^\s"\')]+')
_HYPER = re.compile(r'=HYPERLINK\(\s*"([^"]+)"\s*(?:[,;]\s*"([^"]*)")?\s*\)', re.I)
_SNAP = re.compile(r"\bsnap(?:shot)?_[A-Za-z0-9_]+")
_HANDLE = re.compile(r"(?<![\w/])@([A-Za-z0-9_]{1,15})\b")


def slug(tab: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", tab.lower()).strip("_") or "tab"


def _parse(content: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(content or "")))


def _ranges(v) -> dict[str, list[list[str]]]:
    """Accept read_range output ({"ranges":[{sheet,content}]}) or {tab: rows|csv-text}."""
    if isinstance(v, dict) and "ranges" in v:
        return {r["sheet"]: _parse(r.get("content", "")) for r in v["ranges"]}
    return {k: (_parse(x) if isinstance(x, str) else [list(map(str, r)) for r in x]) for k, x in (v or {}).items()}


def unique_columns(headers: list[str]) -> list[str]:
    """JSON keys for the columns: the exact header text; a repeated or blank header gets ' (2)' / 'Column N'."""
    out, seen = [], {}
    for i, h in enumerate(headers):
        h = (h or "").strip() or f"Column {i + 1}"
        seen[h] = seen.get(h, 0) + 1
        out.append(h if seen[h] == 1 else f"{h} ({seen[h]})")
    return out


def import_sheet(user_key, displayed, formulas=None, *, tab_order=None, header_rows=None, root=None) -> dict:
    """Store every tab. Returns {tab: {"sheet_rows": n_data_rows, "stored": n, "added": n}} for count checks."""
    disp, form = _ranges(displayed), _ranges(formulas or {})
    hdr = dict(HEADER_ROWS, **(header_rows or {}))
    order = list(tab_order or disp)
    now = rec._now(); out = {}
    c = rec.connect(user_key, root)
    try:
        for pos, tab in enumerate(order):
            rows = disp.get(tab, []); frows = form.get(tab, [])
            h = hdr.get(tab, DEFAULT_HEADER_ROW)
            headers = rows[h - 1] if len(rows) >= h else []
            data = rows[h:]
            while data and not any(x.strip() for x in data[-1]):
                data.pop()
            c.execute("INSERT OR IGNORE INTO sheet_tabs(tab,position,header_row,headers,preamble,row_count,imported_at) "
                      "VALUES(?,?,?,?,?,?,?)", (tab, pos, h, json.dumps(headers, ensure_ascii=False),
                                                json.dumps(rows[:h - 1], ensure_ascii=False), len(data), now))
            added = 0
            for i, r in enumerate(data):
                rn = h + 1 + i
                fr = frows[rn - 1] if len(frows) >= rn else []
                fmap = {str(j): f for j, f in enumerate(fr) if f and f.startswith("=") and (j >= len(r) or f != r[j])}
                added += c.execute("INSERT OR IGNORE INTO sheet_rows(tab,row_num,cells,formulas,imported_at) VALUES(?,?,?,?,?)",
                                   (tab, rn, json.dumps(r, ensure_ascii=False),
                                    json.dumps(fmap, ensure_ascii=False) if fmap else None, now)).rowcount
            stored = c.execute("SELECT count(*) FROM sheet_rows WHERE tab=?", (tab,)).fetchone()[0]
            out[tab] = {"sheet_rows": len(data), "stored": stored, "added": added, "header_row": h,
                        "columns": len(headers), "match": stored == len(data)}
        rec._audit(c, "sheet_import", json.dumps({t: v["sheet_rows"] for t, v in out.items()}), now)
        c.commit()
    finally:
        c.close()
    return out


def tab_counts(user_key, root=None) -> dict:
    c = rec.connect(user_key, root)
    try:
        return dict(c.execute("SELECT tab, count(*) FROM sheet_rows GROUP BY tab").fetchall())
    finally:
        c.close()


# ---------- derived cross-reference fields ----------
def _links(cells: list[str], fmap: dict) -> list[str]:
    urls = []
    for j, v in enumerate(cells):
        f = fmap.get(str(j)) or ""
        m = _HYPER.search(f)
        for u in ([m.group(1)] if m else []) + _URL.findall(v or ""):
            if u not in urls:
                urls.append(u)
    return urls


def _refs(record: dict, links: list[str]) -> dict:
    uid = handle = None
    for k, v in record.items():
        kl = k.lower()
        if uid is None and ("user id" in kl or kl in ("owner id",)) and re.fullmatch(r"\d{3,25}", (v or "").strip()):
            uid = v.strip()
        if handle is None and any(w in kl for w in ("handle", "account", "x profile")):
            m = _HANDLE.search(v or "") or re.search(r"x\.com/([A-Za-z0-9_]{1,15})\b", v or "")
            if m:
                handle = m.group(1)
    if handle is None:
        for u in links:
            m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:$|[/?#])", u)
            if m and m.group(1).lower() not in ("i", "home", "search"):
                handle = m.group(1); break
    snaps = sorted({s for v in record.values() for s in _SNAP.findall(v or "")})
    review = next((v for k, v in record.items() if k.lower() == "review id" and v), None)
    entry = next((v for k, v in record.items() if k.lower() == "entry id" and v), None)
    return {k: v for k, v in {"x_user_id": uid, "handle": handle, "x_url": f"https://x.com/{handle}" if handle else None,
                              "snapshot_ids": snaps or None, "review_id": review, "entry_id": entry}.items() if v}


def _dump(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


README = """# Follower Finder records (private backup)

This folder is the readable backup of one owner's Follower Finder records. It mirrors the old Google Sheet tab by
tab and holds every table of `terminator.db`. Everything is pretty-printed JSON. Follower Finder reads this folder to
restore `terminator.db` or to rebuild the Sheet (`sheet_mirror.restore_from_mirror` / `sheet_values_from_mirror`).
Format: `{fmt}`. Keep this repository PRIVATE.

## Sheet tabs -> files

| Sheet tab | File | Rows | What it holds |
|---|---|---|---|
{tab_rows}

Each tab file has `columns` (the exact header text, in Sheet order), `column_keys` (the JSON key used for each
column; a repeated header gets " (2)"), `header_row`, `preamble` (rows above the header, e.g. the REVIEW QUEUE
notice) and `rows`. Each row has `row` (its row number in the Sheet), `values` (column key -> cell text as
displayed), `formulas` (cells that held a formula such as `=HYPERLINK("https://x.com/...","@...")`, so links are
kept) and `refs` (derived for lookups: `x_user_id`, `handle`, `x_url`, `snapshot_ids`, `review_id`, `entry_id`).

## Database tables -> files

| Table | File | Rows |
|---|---|---|
{db_rows}

`removals` / `graveyard` = every termination (the Graveyard), `allowlist` = Always Keep, `scans` + `candidates` +
`scan_metrics` = each scan's frozen snapshot and its flagged accounts, `audit_log` = what happened when.

## How the files relate

- **Account**: `xref/accounts.json` is keyed by X user ID (or `@handle` when the ID is unknown). Each entry lists
  every place that account appears: `{{"file", "row"}}` for Sheet tabs and `{{"file", "id"}}` for tables.
- **Snapshot**: `xref/snapshots.json` is keyed by snapshot ID (`snap_...`, the frozen scan a decision was made on)
  and lists the scan, its candidates and every Sheet row that mentions it.
- Review IDs (`BT-...`) tie REVIEW QUEUE, CHAT SELECTIONS, KEEPERS, PROTECTED and GRAVEYARD rows together;
  entry IDs (`TH-....`) point at the owner's termination history.
- `index.json` lists every file with its row count, so a restore can check nothing is missing.
"""


def export_mirror(user_key, dest, root=None) -> dict:
    """Write <dest>/records/... Returns the index."""
    base = Path(dest) / MIRROR_DIR
    c = rec.connect(user_key, root)
    accounts, snaps, files = {}, {}, {}
    try:
        def acct(key, ref):
            if key:
                accounts.setdefault(key, []).append(ref)
        tabs = c.execute("SELECT tab,position,header_row,headers,preamble,row_count FROM sheet_tabs ORDER BY position").fetchall()
        tab_rows = []
        for tab, pos, h, headers, preamble, n in tabs:
            hd = json.loads(headers or "[]"); keys = unique_columns(hd)
            fn = f"sheet/{pos + 1:02d}_{slug(tab)}.json"
            rows = []
            for rn, cells, fm in c.execute("SELECT row_num,cells,formulas FROM sheet_rows WHERE tab=? ORDER BY row_num", (tab,)):
                cl = json.loads(cells); fmap = json.loads(fm) if fm else {}
                width = max(len(keys), len(cl))
                ks = keys + [f"Column {i + 1}" for i in range(len(keys), width)]
                values = {ks[i]: (cl[i] if i < len(cl) else "") for i in range(width)}
                formulas = {ks[int(j)]: f for j, f in fmap.items()}
                links = _links(cl, fmap); refs = _refs(values, links)
                if links:
                    refs["links"] = links
                rows.append({"row": rn, "values": values, **({"formulas": formulas} if formulas else {}), "refs": refs})
                acct(refs.get("x_user_id") or (("@" + refs["handle"].lower()) if refs.get("handle") else None),
                     {"file": fn, "row": rn, "tab": tab, "handle": refs.get("handle")})
                for s in refs.get("snapshot_ids", []):
                    snaps.setdefault(s, {"sheet_rows": [], "candidates": []})["sheet_rows"].append({"file": fn, "row": rn})
            _dump(base / fn, {"tab": tab, "position": pos, "header_row": h, "columns": hd, "column_keys": keys,
                              "preamble": json.loads(preamble or "[]"), "row_count": len(rows), "rows": rows})
            files[fn] = {"tab": tab, "rows": len(rows)}
            tab_rows.append(f"| {tab} | `{fn}` | {len(rows)} | {_TAB_NOTES.get(tab, 'tab copied as is')} |")
        db_rows = []
        for t, cols in rec.TABLES.items():
            if t.startswith("sheet_"):
                continue
            q = c.execute(f"SELECT rowid,{','.join(cols)} FROM {t} ORDER BY rowid").fetchall()
            out = []
            for r in q:
                d = {"id": r[0], **dict(zip(cols, r[1:]))}
                if d.get("handle"):
                    d["x_url"] = "https://x.com/" + str(d["handle"]).lstrip("@")
                out.append(d)
                key = d.get("user_id") or (("@" + str(d["handle"]).lstrip("@").lower()) if d.get("handle") else None)
                acct(key, {"file": f"db/{t}.json", "id": r[0], "handle": d.get("handle")})
                sid = d.get("scan_id") or (d.get("id") if t == "scans" else None)
                if t == "scans":
                    snaps.setdefault(d["id"], {"sheet_rows": [], "candidates": []})["scan"] = {"file": "db/scans.json", "id": r[0]}
                elif t == "candidates":
                    snaps.setdefault(sid, {"sheet_rows": [], "candidates": []})["candidates"].append(
                        {"file": "db/candidates.json", "id": r[0], "handle": d["handle"], "label": d["label"]})
            _dump(base / f"db/{t}.json", {"table": t, "columns": cols, "row_count": len(out), "rows": out})
            files[f"db/{t}.json"] = {"table": t, "rows": len(out)}
            db_rows.append(f"| {t} | `db/{t}.json` | {len(out)} |")
    finally:
        c.close()
    _dump(base / "xref/accounts.json", dict(sorted(accounts.items())))
    _dump(base / "xref/snapshots.json", dict(sorted(snaps.items())))
    files["xref/accounts.json"] = {"accounts": len(accounts)}
    files["xref/snapshots.json"] = {"snapshots": len(snaps)}
    index = {"format": FORMAT, "exported_at": rec._now(), "files": files,
             "keys": {"account": "X user ID, else @handle (lowercase)", "snapshot": "snapshot ID (snap_...)",
                      "review": "Review ID (BT-...)", "entry": "termination history entry (TH-....)"}}
    _dump(base / "index.json", index)
    (base / "README.md").write_text(README.format(fmt=FORMAT, tab_rows="\n".join(tab_rows) or "| (no Sheet imported) | | | |",
                                                  db_rows="\n".join(db_rows)), encoding="utf-8")
    return index


_TAB_NOTES = {
    "REVIEW QUEUE": "accounts waiting for the owner's decision",
    "CHAT SELECTIONS": "every chat card answer (what was shown, what was picked)",
    "BATCH LOG": "one line per scan batch and what happened to it",
    "Re-follow Watch": "removed accounts that followed again",
    "PROTECTED": "accounts never touched (followed, verified, Always Keep)",
    "KEEPERS": "accounts the owner or classifier kept",
    "GRAVEYARD": "every terminated account with evidence",
    "List Graveyard": "lists the owner was removed from",
}


def sheet_values_from_mirror(src) -> dict[str, list[list[str]]]:
    """Rebuild each tab's cell grid (preamble + header + rows, formulas restored) from records/."""
    base = Path(src) / MIRROR_DIR if (Path(src) / MIRROR_DIR).exists() else Path(src)
    out = {}
    for p in sorted((base / "sheet").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        grid = [list(r) for r in d["preamble"]] + [list(d["columns"])]
        for r in d["rows"]:
            while len(grid) < r["row"] - 1:
                grid.append([])
            cells = list(r["values"].values())
            for k, f in (r.get("formulas") or {}).items():
                cells[list(r["values"]).index(k)] = f
            while cells and cells[-1] == "" and len(cells) > len(d["columns"]):
                cells.pop()
            grid.append(cells)
        out[d["tab"]] = grid
    return out


def restore_from_mirror(user_key, src, root=None) -> dict:
    """Rebuild/merge terminator.db from records/ (idempotent)."""
    base = Path(src) / MIRROR_DIR if (Path(src) / MIRROR_DIR).exists() else Path(src)
    added = {}
    c = rec.connect(user_key, root)
    try:
        for t in rec.TABLES:
            p = base / "db" / f"{t}.json"
            if p.exists():
                rows = json.loads(p.read_text(encoding="utf-8"))["rows"]
                added[t] = rec._import_rows(c, t, [{k: ("" if v is None else str(v)) for k, v in r.items()} for r in rows])
        c.commit()
    finally:
        c.close()
    disp, form, hdr, order = {}, {}, {}, []
    for p in sorted((base / "sheet").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        grid_d = [list(r) for r in d["preamble"]] + [list(d["columns"])]
        grid_f = [list(r) for r in grid_d]
        for r in d["rows"]:
            vals = list(r["values"].values())
            while vals and vals[-1] == "" and len(vals) > len(d["columns"]):
                vals.pop()
            fs = list(vals)
            for k, f in (r.get("formulas") or {}).items():
                fs[list(r["values"]).index(k)] = f
            while len(grid_d) < r["row"] - 1:
                grid_d.append([]); grid_f.append([])
            grid_d.append(vals); grid_f.append(fs)
        disp[d["tab"]], form[d["tab"]], hdr[d["tab"]] = grid_d, grid_f, d["header_row"]; order.append(d["tab"])
    if order:
        res = import_sheet(user_key, disp, form, tab_order=order, header_rows=hdr, root=root)
        added["sheet_rows"] = sum(v["added"] for v in res.values())
    return {"added": added, "counts": rec.counts(user_key, root), "tab_counts": tab_counts(user_key, root)}


def verify_mirror(user_key, src, root=None) -> dict:
    """Compare records/ with terminator.db: per table / per tab row counts must match."""
    base = Path(src) / MIRROR_DIR if (Path(src) / MIRROR_DIR).exists() else Path(src)
    idx = json.loads((base / "index.json").read_text(encoding="utf-8"))
    db = rec.counts(user_key, root); tabs = tab_counts(user_key, root)
    diffs = []
    for fn, meta in idx["files"].items():
        if "table" in meta and db.get(meta["table"]) is not None and db[meta["table"]] != meta["rows"]:
            diffs.append((fn, meta["rows"], db[meta["table"]]))
        if "tab" in meta and tabs.get(meta["tab"], 0) != meta["rows"]:
            diffs.append((fn, meta["rows"], tabs.get(meta["tab"], 0)))
    return {"ok": not diffs, "diffs": diffs, "db": db, "tabs": tabs}

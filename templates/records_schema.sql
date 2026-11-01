-- Follower Finder records (v3.5.0): the main local store for one owner's scans, removals, graveyard and allowlist.
-- One file per owner: data/users/<user_key>/terminator.db. Starts empty. History tables are append-only
-- (triggers refuse UPDATE/DELETE); a change is a new row (e.g. allowlist op='remove', removals action='restored').
CREATE TABLE IF NOT EXISTS followers(user_id TEXT PRIMARY KEY, handle TEXT, first_seen TEXT, score REAL, reasons TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS removals(id INTEGER PRIMARY KEY, user_id TEXT, handle TEXT,
  action TEXT NOT NULL CHECK(action IN ('removed','blocked','listed','restored')), at TEXT NOT NULL, batch TEXT, result TEXT,
  UNIQUE(user_id, handle, action, at));
CREATE TABLE IF NOT EXISTS graveyard(id INTEGER PRIMARY KEY, user_id TEXT, handle TEXT, added_at TEXT NOT NULL, reason TEXT,
  event TEXT DEFAULT 'REMOVED', UNIQUE(user_id, handle, added_at, event));
CREATE TABLE IF NOT EXISTS allowlist(id INTEGER PRIMARY KEY, user_id TEXT, handle TEXT, added_at TEXT NOT NULL,
  op TEXT NOT NULL DEFAULT 'add' CHECK(op IN ('add','remove')), UNIQUE(user_id, handle, added_at, op));
CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY, at TEXT NOT NULL, count INTEGER);
CREATE TABLE IF NOT EXISTS candidates(id INTEGER PRIMARY KEY, scan_id TEXT NOT NULL, user_id TEXT NOT NULL, handle TEXT NOT NULL,
  label TEXT, reasons TEXT, score REAL, UNIQUE(scan_id, user_id, handle));
CREATE TABLE IF NOT EXISTS scan_metrics(id INTEGER PRIMARY KEY, scan_id TEXT NOT NULL, metric TEXT NOT NULL, value TEXT,
  UNIQUE(scan_id, metric));
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY, at TEXT NOT NULL, event TEXT NOT NULL, detail TEXT,
  UNIQUE(at, event, detail));
-- v3.5.5: full-fidelity copy of the owner's old Google Sheet (every tab, every column, every row, link formulas kept).
CREATE TABLE IF NOT EXISTS sheet_tabs(id INTEGER PRIMARY KEY, tab TEXT NOT NULL, position INTEGER, header_row INTEGER,
  headers TEXT, preamble TEXT, row_count INTEGER, imported_at TEXT NOT NULL, UNIQUE(tab));
CREATE TABLE IF NOT EXISTS sheet_rows(id INTEGER PRIMARY KEY, tab TEXT NOT NULL, row_num INTEGER NOT NULL, cells TEXT NOT NULL,
  formulas TEXT, imported_at TEXT NOT NULL, UNIQUE(tab, row_num));
CREATE VIEW IF NOT EXISTS current_allowlist AS
  SELECT a.user_id, a.handle, a.added_at FROM allowlist a
  WHERE a.op='add' AND a.id = (SELECT max(b.id) FROM allowlist b WHERE coalesce(b.user_id,'')||'|'||lower(coalesce(b.handle,''))
                                = coalesce(a.user_id,'')||'|'||lower(coalesce(a.handle,'')));
CREATE TRIGGER IF NOT EXISTS removals_no_update BEFORE UPDATE ON removals BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS removals_no_delete BEFORE DELETE ON removals BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS graveyard_no_update BEFORE UPDATE ON graveyard BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS graveyard_no_delete BEFORE DELETE ON graveyard BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS allowlist_no_update BEFORE UPDATE ON allowlist BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS allowlist_no_delete BEFORE DELETE ON allowlist BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS scans_no_update BEFORE UPDATE ON scans BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS scans_no_delete BEFORE DELETE ON scans BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS candidates_no_update BEFORE UPDATE ON candidates BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS candidates_no_delete BEFORE DELETE ON candidates BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS scan_metrics_no_update BEFORE UPDATE ON scan_metrics BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS scan_metrics_no_delete BEFORE DELETE ON scan_metrics BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS sheet_tabs_no_update BEFORE UPDATE ON sheet_tabs BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS sheet_tabs_no_delete BEFORE DELETE ON sheet_tabs BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS sheet_rows_no_update BEFORE UPDATE ON sheet_rows BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS sheet_rows_no_delete BEFORE DELETE ON sheet_rows BEGIN SELECT RAISE(ABORT, 'append-only'); END;
-- v3.5.17: Verified Follower Review monthly recheck queue. NOT append-only (rows are pushed out / resolved in place).
-- One row per (owner, account). Accounts the deep dive found inactive (>90 days / no real activity, or only an old
-- pinned post) that the owner did not pick are rechecked ~30 days later instead of being discarded.
CREATE TABLE IF NOT EXISTS recheck_queue(owner_key TEXT NOT NULL, account_key TEXT NOT NULL, user_id TEXT, handle TEXT NOT NULL,
  skipped_at TEXT NOT NULL, recheck_due TEXT NOT NULL,
  reason TEXT NOT NULL CHECK(reason IN ('inactive_90d','pinned_only','no_activity')),
  recheck_count INTEGER NOT NULL DEFAULT 0, last_checked_at TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','resolved')), resolved_reason TEXT, user_json TEXT,
  PRIMARY KEY(owner_key, account_key));
CREATE INDEX IF NOT EXISTS recheck_queue_due ON recheck_queue(owner_key, status, recheck_due);
-- v3.5.19: verified follower sightings for the silent drive-by drop. One row per (owner, follower). Not append-only.
-- status: present (in the latest follower list) | gone (left after >= window) | drive_by (left within the window).
CREATE TABLE IF NOT EXISTS verified_follower_seen(owner_key TEXT NOT NULL, user_id TEXT NOT NULL, handle TEXT,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'present' CHECK(status IN ('present','gone','drive_by')), drive_by_at TEXT,
  PRIMARY KEY(owner_key, user_id));
-- v3.5.25 blocked-by cache (append-only; latest row per account wins; rechecked every BLOCK_RECHECK_DAYS)
CREATE TABLE IF NOT EXISTS blocked_by(owner_key TEXT NOT NULL, account_key TEXT NOT NULL, handle TEXT,
  detected_at TEXT NOT NULL, blocked INTEGER NOT NULL CHECK(blocked IN (0,1)), source TEXT NOT NULL,
  PRIMARY KEY(owner_key, account_key, detected_at));
CREATE TRIGGER IF NOT EXISTS blocked_by_no_update BEFORE UPDATE ON blocked_by BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS blocked_by_no_delete BEFORE DELETE ON blocked_by BEGIN SELECT RAISE(ABORT, 'append-only'); END;

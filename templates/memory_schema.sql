-- Follower Finder per-user memory (v3.3.0; v3.4.0 adds lessons, rule/capability status, fact.subject). Generic schema; every user's brain.db starts EMPTY.
-- Lives at data/users/<user_key>/memory/brain.db on the user's own bot computer. Never store secrets here.
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS entity(id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL UNIQUE, aliases TEXT, created_at TEXT DEFAULT (date('now','localtime')));
CREATE TABLE IF NOT EXISTS person(entity_id INTEGER PRIMARY KEY REFERENCES entity(id), relation TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS bot(entity_id INTEGER PRIMARY KEY REFERENCES entity(id), owner TEXT, purpose TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS account_platform(id INTEGER PRIMARY KEY, entity_id INTEGER REFERENCES entity(id), name TEXT NOT NULL UNIQUE, connector_status TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS quest(id INTEGER PRIMARY KEY, title TEXT NOT NULL, status TEXT DEFAULT 'open', related_entity INTEGER REFERENCES entity(id), created TEXT, due TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS project(id INTEGER PRIMARY KEY, title TEXT NOT NULL UNIQUE, status TEXT, waiting_on TEXT, notes TEXT, updated_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS rule(id INTEGER PRIMARY KEY, scope TEXT, category TEXT, text TEXT NOT NULL, set_by TEXT, set_on TEXT, superseded_by INTEGER REFERENCES rule(id), status TEXT DEFAULT 'current', reason TEXT, changed_on TEXT);
CREATE TABLE IF NOT EXISTS capability(id INTEGER PRIMARY KEY, bot_id INTEGER REFERENCES entity(id), description TEXT NOT NULL, learned_on TEXT, source TEXT, superseded_by INTEGER REFERENCES capability(id), status TEXT DEFAULT 'current', reason TEXT, changed_on TEXT);
CREATE TABLE IF NOT EXISTS routine(id INTEGER PRIMARY KEY, bot_id INTEGER REFERENCES entity(id), schedule TEXT, description TEXT NOT NULL, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS fact(id INTEGER PRIMARY KEY, subject_id INTEGER NOT NULL REFERENCES entity(id), subject TEXT, predicate TEXT NOT NULL, object_id INTEGER REFERENCES entity(id), value TEXT, valid_from TEXT, valid_to TEXT, source TEXT, recorded_by_bot TEXT, recorded_at TEXT DEFAULT (datetime('now','localtime')));
CREATE INDEX IF NOT EXISTS fact_cur ON fact(subject_id,predicate) WHERE valid_to IS NULL;
CREATE TABLE IF NOT EXISTS event_log(id INTEGER PRIMARY KEY, ts TEXT DEFAULT (datetime('now','localtime')), bot_id INTEGER REFERENCES entity(id), entity_id INTEGER REFERENCES entity(id), kind TEXT, summary TEXT NOT NULL);
-- lesson: same columns the Hive Mind client uses (topic, wrong, right, uses, src, status, added, last). Nothing is deleted:
-- outdated lessons get status 'avoid', outdated rules/capabilities get status 'superseded'.
CREATE TABLE IF NOT EXISTS lesson(id INTEGER PRIMARY KEY, bot TEXT, topic TEXT, wrong TEXT, right TEXT, uses INTEGER DEFAULT 1, src TEXT DEFAULT 'local', status TEXT DEFAULT 'current', added TEXT, last TEXT, ref_table TEXT, ref_id INTEGER);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(src, ref, body, tokenize='porter unicode61');
-- v3.5.28 unfollow-back tracker. tracked_follow: one row per account the owner follows through Follower Finder (or seeded
-- from mutual verified followers); its check columns are updated by the monthly recheck. unfollow_back_log is append-only
-- (every follow recorded, recheck result, card pick and unfollow). flagged_account is permanent (no update/delete).
CREATE TABLE IF NOT EXISTS tracked_follow(user_id TEXT PRIMARY KEY, handle TEXT NOT NULL, followed_at TEXT, source TEXT NOT NULL, last_checked TEXT, still_follows_user INTEGER, status TEXT NOT NULL DEFAULT 'tracking');
CREATE TABLE IF NOT EXISTS unfollow_back_log(id INTEGER PRIMARY KEY, ts TEXT DEFAULT (datetime('now','localtime')), event TEXT NOT NULL, run_id TEXT, user_id TEXT, handle TEXT, detail TEXT);
CREATE TRIGGER IF NOT EXISTS unfollow_back_log_no_update BEFORE UPDATE ON unfollow_back_log BEGIN SELECT RAISE(ABORT, 'unfollow_back_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS unfollow_back_log_no_delete BEFORE DELETE ON unfollow_back_log BEGIN SELECT RAISE(ABORT, 'unfollow_back_log is append-only'); END;
CREATE TABLE IF NOT EXISTS flagged_account(user_id TEXT PRIMARY KEY, handle TEXT NOT NULL, followed_at TEXT, unfollowed_user_at TEXT, user_unfollowed_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS flagged_account_no_update BEFORE UPDATE ON flagged_account BEGIN SELECT RAISE(ABORT, 'flags are permanent'); END;
CREATE TRIGGER IF NOT EXISTS flagged_account_no_delete BEFORE DELETE ON flagged_account BEGIN SELECT RAISE(ABORT, 'flags are permanent'); END;

#!/usr/bin/env python3
"""hive: opt-in, anonymized lesson AND owner-feedback sharing (Hive Mind) for ~/memory/brain.db. stdlib only.
v3.5.11: kind field (lesson | correction | request | declined | broken | workaround); feedback kinds are eligible after
1 use (lessons after 2); one weekly batch plus at most one urgent 'broken' batch per day; `auto --dry-run` never
touches the network.
v3.5.14: one-shot owner feedback `hive feedback --bot B --category bug|request|praise|other "text" [--dry-run]`: sends
ONLY that one item as a `direct` single-item payload, with its own small allowance (DPD accepted per UTC day, separate
from the weekly/urgent batches), so a reworded retry after HTTP 422 can go right away. Dry run prints the exact bytes.
v3.5.13: template version bump only (verified hard pre-filter).
v3.5.12: template version bump only (the review tool fix is in tools/hive_review.py)."""
import sqlite3, sys, os, re, json, hashlib, secrets, datetime, urllib.request, urllib.error
DB = os.environ.get('MEM_DB', os.path.expanduser('~/memory/brain.db'))
URL = 'https://hive-mind-intake.empressheavy.workers.dev'
TV = 'v3.5.15'
CODES = {'dg': 'DOGE', 'bt': 'Bot Terminator', 'pc': 'Playlist Converter', 'ff': 'Follower Finder'}
N2C = {v.lower(): k for k, v in CODES.items()}
KINDS = ('lesson', 'correction', 'request', 'declined', 'broken', 'workaround')
FB_CATS = {'bug': 'broken', 'request': 'request', 'praise': 'praise', 'other': 'other'}  # category -> intake kind
DPD = 3  # direct one-shot feedback: accepted sends per UTC day (intake enforces the same)
CAP, FLEN, MAXB, UCAP = 20, 150, 8192, 5
SCHEMA = """CREATE TABLE IF NOT EXISTS lesson(id INTEGER PRIMARY KEY,topic TEXT,wrong TEXT,right TEXT,uses INTEGER DEFAULT 1,src TEXT DEFAULT 'local',status TEXT DEFAULT 'current',added TEXT,last TEXT,kind TEXT DEFAULT 'lesson');
CREATE TABLE IF NOT EXISTS hive_state(k TEXT PRIMARY KEY,v TEXT);
CREATE TABLE IF NOT EXISTS hive_sent(id INTEGER PRIMARY KEY,wk TEXT,sent_at TEXT,lines INTEGER,status TEXT,body TEXT);
CREATE TABLE IF NOT EXISTS hive_sent_h(h TEXT PRIMARY KEY,sent_at TEXT);"""
def con():
    c = sqlite3.connect(DB, isolation_level=None); c.execute('PRAGMA busy_timeout=30000'); c.executescript(SCHEMA)
    if 'kind' not in [r[1] for r in c.execute('PRAGMA table_info(lesson)')]: c.execute("ALTER TABLE lesson ADD COLUMN kind TEXT DEFAULT 'lesson'")
    return c
def get(c, k, d=None):
    r = c.execute('SELECT v FROM hive_state WHERE k=?', (k,)).fetchone(); return r[0] if r else d
def put(c, k, v): c.execute('INSERT OR REPLACE INTO hive_state(k,v) VALUES(?,?)', (k, v))
def now(): return datetime.datetime.now().isoformat(timespec='seconds')
def today(): return datetime.date.today().isoformat()
def wk():
    y, w, _ = datetime.date.today().isocalendar(); return '%d-W%02d' % (y, w)
def norm(s): return ' '.join(re.sub(r'[^a-z0-9<>]+', ' ', str(s).lower()).split())
def hsh(b, k, t, w, r): return hashlib.sha1(('%s|%s|%s|%s|%s' % (b, k, t, norm(w), norm(r))).encode()).hexdigest()[:12]
def bucket(u): return 3 if u >= 10 else 2 if u >= 5 else 1
def rid(c): return hashlib.sha256(('hive-v1|%s|%s' % (get(c, 'salt'), get(c, 'install_id'))).encode()).hexdigest()[:16]
SECRET = re.compile(r'gh[opsur]_[A-Za-z0-9]{20,}|\b[0-9]{16}\b|\b[0-9]{4}[- ][0-9]{4}[- ][0-9]{4}[- ][0-9]{4}\b|\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b|password["\' ]*[:=] *["\']?[^ ",}]{3,}', re.I)
SUBS = [
    (r'[\w.+-]+@[\w-]+(\.[\w-]+)+', '<EMAIL>'),
    (r'\b(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|net|org|io|ai|co|app|dev|me|gg|ly|tv|us)(/\S*)?\b', '<URL>'),
    (r'\b\d{1,3}(?:\.\d{1,3}){3}\b|\b[0-9a-f]{1,4}(?::[0-9a-f]{0,4}){4,7}\b', '<IP>'),
    (r'(?:\+?\d{1,2}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b', '<PHONE>'),
    (r'(?<![\w<])@\w{2,}', '<HANDLE>'),
    (r'\b\d{1,6}\s+(?:[A-Z][a-z]+\s){1,3}(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Dr|Drive|Ln|Lane|Ct|Way|Pl|Hwy)\b\.?', '<ADDRESS>'),
    (r'(?:/home|/Users|/workspace|/cursor|/root|/tmp|~|[A-Za-z]:\\Users)[/\\][^\s,;)]*', '<PATH>'),
    (r'\b(?:sk|pk|rk|xai|AKIA|AIza|ya29|eyJ)[-_A-Za-z0-9.]{12,}|\b[A-Za-z0-9_\-]{32,}\b', '<KEY>')]
SUBS = [(re.compile(p, re.I), t) for p, t in SUBS]
RISKY = re.compile(r'password|passcode|\bpin\b|\bssn\b|social security|secret|token|api.?key|\b\d{5,}\b|card ending|routing|account (no|number)|@|https?:|www\.', re.I)
# the intake's own checks (same pattern in intake/worker.js); anything matching is dropped before sending
INTAKE = re.compile(r'[\w.+-]+@[\w-]+(\.[\w-]+)+|(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b|\b(?:https?://|www\.)\S+|[?&](?:token|key|api_key|access_token|auth|sig|signature|code|secret)=|pass(?:word|wd|code)?\s*["\']?\s*[:=]|\bpwd\s*[:=]|\b(?:\d[ -]?){13,19}\b', re.I)
def names(c):
    ns = set()
    try: rows = c.execute("SELECT subject,value FROM fact WHERE valid_to IS NULL").fetchall()
    except sqlite3.OperationalError: rows = []  # no fact table (bare hive install): nothing extra to mask
    for s, v in rows:
        for x in (s, v):
            x = str(x or '').strip()
            if 4 <= len(x) <= 80 and x.lower() not in ('user', 'owner', 'bot', 'global') and x.lower() not in N2C: ns.add(x)
    return sorted(ns, key=len, reverse=True)
def clean(s, ns):
    s = ' '.join(str(s or '').split())
    if not s or SECRET.search(s): return None
    for rx, t in SUBS: s = rx.sub(t, s)
    for n in ns: s = re.sub(r'(?<![\w<])' + re.escape(n) + r'(?![\w>])', '<NAME>', s, flags=re.I)
    if RISKY.search(s) or INTAKE.search(s): return None
    return s[:FLEN]
def build(c, urgent=False):
    b = get(c, 'bot'); ns = names(c); r_ = rid(c); w_ = wk(); out, seen, drop = [], set(), 0
    q = ("SELECT kind,topic,wrong,right,uses FROM lesson WHERE status='current' AND src<>'hive' AND "
         "((COALESCE(kind,'lesson')='lesson' AND uses>=2) OR (COALESCE(kind,'lesson')<>'lesson' AND uses>=1)) "
         "ORDER BY kind='broken' DESC, COALESCE(kind,'lesson')<>'lesson' DESC, uses DESC, id DESC")
    for k, t, w, r, u in c.execute(q).fetchall():
        k = k if k in KINDS else 'lesson'
        if urgent and k != 'broken': continue
        t2, w2, r2 = clean(t or 'general', ns), clean(w, ns), clean(r, ns)
        if None in (t2, w2, r2): drop += 1; continue
        d = {'v': 2, 'b': b, 'k': k, 't': t2, 'w': w2, 'r': r2, 'n': bucket(u), 'tv': TV, 'rid': r_, 'h': hsh(b, k, t2, w2, r2), 'wk': w_}
        if d['h'] in seen: drop += 1; continue
        if urgent and c.execute('SELECT 1 FROM hive_sent_h WHERE h=?', (d['h'],)).fetchone(): continue
        trial = out + [d]
        if len(json.dumps({'batch': trial}, separators=(',', ':')).encode()) > MAXB: break
        seen.add(d['h']); out = trial
        if len(out) >= (UCAP if urgent else CAP): break
    return out, drop
def payload(recs, urgent):
    p = {'batch': recs}
    if urgent: p['urgent'] = True
    return json.dumps(p, separators=(',', ':')).encode()
def log(c, recs, st):
    body = '\n'.join(json.dumps(d, separators=(',', ':')) for d in recs)
    c.execute('INSERT INTO hive_sent(wk,sent_at,lines,status,body) VALUES(?,?,?,?,?)', (wk(), now(), len(recs), st, body))
def send(c, recs, urgent=False):
    req = urllib.request.Request(get(c, 'url', URL), data=payload(recs, urgent), headers={'content-type': 'application/json', 'user-agent': 'hive-mind/2'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: code, msg = r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as e: code, msg = e.code, e.read().decode()[:200]
    except Exception as e: code, msg = 0, str(e)[:200]
    st = ('ok' if code == 200 else 'rejected %s %s' % (code, msg)) + (' (urgent)' if urgent else '')
    log(c, recs, st)
    if urgent: put(c, 'last_urgent_day', today())  # one urgent attempt per day, accepted or not
    else: put(c, 'last_sent_wk', wk())  # one weekly attempt, accepted or not; never retried another way
    if code == 200:
        for d in recs: c.execute('INSERT OR IGNORE INTO hive_sent_h(h,sent_at) VALUES(?,?)', (d['h'], now()))
    print('sent %d line(s): HTTP %s' % (len(recs), code) if code == 200 else 'not sent: HTTP %s %s' % (code, msg))
def auto(c, dry):
    ok = get(c, 'enabled') == '1' and get(c, 'bot') in CODES
    if dry:  # offline: build exactly what would go out, log it as dry-run, no network, no state change
        if get(c, 'bot') not in CODES: sys.exit('hive auto --dry-run: run `hive on --bot dg|bt|pc|ff` first')
        urgent = get(c, 'last_sent_wk') == wk()
        recs, drop = build(c, urgent=urgent)
        log(c, recs, 'dry-run%s (not sent; %d dropped)' % (' urgent' if urgent else '', drop))
        sys.stdout.buffer.write(payload(recs, urgent) + b'\n'); return
    if not ok: return
    if get(c, 'last_sent_wk') != wk():
        recs, _ = build(c)
        if recs: send(c, recs)
    elif get(c, 'last_urgent_day') != today():
        recs, _ = build(c, urgent=True)
        if recs: send(c, recs, urgent=True)
def add(c, k, t, w, r):
    x = c.execute("SELECT id FROM lesson WHERE COALESCE(kind,'lesson')=? AND topic=? AND wrong=? AND right=?", (k, t, w, r)).fetchone()
    if x: c.execute('UPDATE lesson SET uses=uses+1,last=? WHERE id=?', (now(), x[0])); print('%s %d used again' % (k, x[0]))
    else: c.execute('INSERT INTO lesson(topic,wrong,right,added,last,kind) VALUES(?,?,?,?,?,?)', (t, w, r, now(), now(), k)); print('%s added' % k)
def utcday(): return datetime.datetime.now(datetime.timezone.utc).date().isoformat()
def direct(c, a):
    a = list(a)
    def opt(k):
        if k in a:
            i = a.index(k); v = a[i + 1] if i + 1 < len(a) else None; del a[i:i + 2]; return v
    b = opt('--bot'); b = N2C.get((b or '').lower(), b) or get(c, 'bot'); cat = (opt('--category') or '').lower()
    dry = '--dry-run' in a; text = ' '.join(x for x in a if x != '--dry-run').strip()
    if b not in CODES or cat not in FB_CATS or not text: sys.exit('hive ' + FB_USAGE)
    t = clean(text, names(c))
    if t is None or len(' '.join(text.split())) > FLEN:
        print('not sent: looks private or too long (max %d chars, no names, handles, emails, links, numbers, secrets); reword and retry' % FLEN); sys.exit(3)
    if not get(c, 'install_id'): put(c, 'install_id', secrets.token_hex(16)); put(c, 'salt', secrets.token_hex(8))
    if not get(c, 'bot'): put(c, 'bot', b)
    k = FB_CATS[cat]; tp = 'owner feedback: ' + cat; w = 'direct owner feedback'
    d = {'v': 2, 'b': b, 'k': k, 't': tp, 'w': w, 'r': t, 'n': 1, 'tv': TV, 'rid': rid(c), 'h': hsh(b, k, tp, w, t), 'wk': wk()}
    body = json.dumps({'batch': [d], 'direct': True}, separators=(',', ':')).encode()
    if dry: log(c, [d], 'dry-run direct (not sent)'); print(body.decode()); return
    if c.execute('SELECT 1 FROM hive_sent_h WHERE h=?', (d['h'],)).fetchone(): print('already sent: this exact feedback went out before'); return
    day = utcday(); n = int(get(c, 'direct_n', '0')) if get(c, 'direct_day') == day else 0
    if n >= DPD: print('not sent: daily limit of %d direct feedback sends reached; try again tomorrow' % DPD); sys.exit(4)
    req = urllib.request.Request(get(c, 'url', URL), data=body, headers={'content-type': 'application/json', 'user-agent': 'hive-mind/2'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: code, msg = r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as e: code, msg = e.code, e.read().decode()[:200]
    except Exception as e: code, msg = 0, str(e)[:200]
    log(c, [d], ('ok' if code == 200 else 'rejected %s %s' % (code, msg)) + ' (direct)')
    if code == 200:
        put(c, 'direct_day', day); put(c, 'direct_n', str(n + 1))
        c.execute('INSERT OR IGNORE INTO hive_sent_h(h,sent_at) VALUES(?,?)', (d['h'], now())); print('sent 1 line: HTTP 200'); return
    print('not sent: HTTP %s %s' % (code, msg) + ('; reword without private details and retry' if code == 422 else '')); sys.exit(2)
FB_USAGE = 'feedback --bot dg|bt|pc|ff --category bug|request|praise|other "text" [--dry-run]'
USAGE = ('hive lesson add TOPIC WRONG RIGHT | feedback add correction|request|declined|broken|workaround TOPIC WHAT_I_DID_OR_OFFERED WHAT_OWNER_WANTED'
         ' | ' + FB_USAGE + ' | on --bot dg|bt|pc|ff | off | status | preview | auto [--dry-run] | shared [N] | import FILE')
def main(a):
    c = con(); cmd = a[0] if a else ''
    if cmd == 'lesson' and len(a) >= 5 and a[1] == 'add': add(c, 'lesson', *a[2:5])
    elif cmd == 'feedback' and len(a) >= 6 and a[1] == 'add' and a[2] in KINDS[1:]: add(c, a[2], *a[3:6])
    elif cmd == 'feedback' and a[1:2] != ['add']: direct(c, a[1:])
    elif cmd == 'on':
        b = a[a.index('--bot') + 1] if '--bot' in a else get(c, 'bot'); b = N2C.get((b or '').lower(), b)
        if b not in CODES: sys.exit('hive on --bot dg|bt|pc|ff')
        if not get(c, 'install_id'): put(c, 'install_id', secrets.token_hex(16)); put(c, 'salt', secrets.token_hex(8))
        put(c, 'bot', b); put(c, 'enabled', '1'); print('hive ON for %s (anonymous id %s)' % (b, rid(c)))
    elif cmd == 'off': put(c, 'enabled', '0'); print('hive OFF; nothing will be sent')
    elif cmd == 'status': print('enabled=%s bot=%s rid=%s last_sent_wk=%s last_urgent_day=%s tv=%s' % (get(c, 'enabled', '0'), get(c, 'bot'), rid(c) if get(c, 'install_id') else '-', get(c, 'last_sent_wk', '-'), get(c, 'last_urgent_day', '-'), TV))
    elif cmd == 'preview':
        if get(c, 'enabled') != '1': sys.exit('hive is off')
        recs, drop = build(c)
        for d in recs: print(json.dumps(d, separators=(',', ':')))
        print('# %d line(s), %d dropped%s' % (len(recs), drop, '; already sent this week' if get(c, 'last_sent_wk') == wk() else ''), file=sys.stderr)
    elif cmd == 'auto': auto(c, '--dry-run' in a)
    elif cmd == 'shared':
        rows = c.execute('SELECT id,wk,sent_at,lines,status,body FROM hive_sent ORDER BY id DESC LIMIT ?', (int(a[1]) if len(a) > 1 else 10,)).fetchall()
        for r in rows: print('--- batch %d | week %s | %s | %d line(s) | %s\n%s' % r)
        if not rows: print('(nothing has been shared yet)')
    elif cmd == 'import' and len(a) > 1:
        n = 0
        for l in open(a[1]):
            if not l.strip(): continue
            d = json.loads(l); k = d.get('k', 'lesson') if d.get('k') in KINDS else 'lesson'
            if not c.execute("SELECT 1 FROM lesson WHERE topic=? AND wrong=? AND right=?", (d['t'], d['w'], d['r'])).fetchone():
                c.execute("INSERT INTO lesson(topic,wrong,right,uses,src,added,last,kind) VALUES(?,?,?,0,'hive',?,?,?)", (d['t'], d['w'], d['r'], now(), now(), k)); n += 1
        print('imported %d hive item(s)' % n)
    else: sys.exit(USAGE)
if __name__ == '__main__': main(sys.argv[1:])

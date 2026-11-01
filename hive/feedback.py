#!/usr/bin/env python3
"""feedback: send ONE end-user feedback message (bug | feature | praise | other, optional 1-5 rating) to the bot
owner's intake (POST /feedback). stdlib only. Run `send` ONLY after the end user explicitly agreed to send that exact
message: `preview` prints the exact payload, `send --yes` posts that same payload and logs it locally (feedback_sent
table in the hive brain.db). Install id + salt come from hive_state (shared with hive.py). Role is 'main' only when a
MAIN_BOT marker file sits next to this script; published copies have no marker and report 'clone'.
  feedback.py preview --kind bug --text "..." [--rating 4]
  feedback.py send --yes --kind bug --text "..." [--rating 4]
  feedback.py log | selftest"""
import os, sys, re, json, sqlite3, secrets, argparse, datetime, urllib.request, urllib.error
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import hive
URL = os.environ.get('FEEDBACK_URL', hive.URL.rstrip('/') + '/feedback')
MARKER = os.path.join(HERE, 'MAIN_BOT')
KINDS, FB_LEN, FB_MAXB = ('bug', 'feature', 'praise', 'other'), 500, 2048
# same rejections as the worker: hive's intake pattern + handles, token-like strings, bare domains
EXTRA = re.compile(r'(?<!\w)@\w{2,}|\b(?:sk|pk|rk|xai|AKIA|AIza|ya29|eyJ|gh[opsur]_)[-_A-Za-z0-9.]{12,}|\b[A-Za-z0-9_\-]{32,}\b|\b[\w-]+\.(?:com|net|org|io|ai|co|app|dev|me|gg|ly|tv|us)(?:/\S*)?\b', re.I)
LOG = 'CREATE TABLE IF NOT EXISTS feedback_sent(id INTEGER PRIMARY KEY,sent_at TEXT,status TEXT,body TEXT)'

def role(): return 'main' if os.path.exists(MARKER) else 'clone'
def problem(text):
    if not text.strip(): return 'empty message'
    if len(text) > FB_LEN: return 'over %d characters' % FB_LEN
    if hive.INTAKE.search(text) or EXTRA.search(text) or hive.SECRET.search(text):
        return 'looks like personal data (email, phone, link, handle, token, card or password); please reword'
    return None
def build(c, kind, text, rating=None):
    if kind not in KINDS: raise ValueError('kind must be one of ' + '|'.join(KINDS))
    if rating is not None and rating not in (1, 2, 3, 4, 5): raise ValueError('rating must be 1-5')
    text = ' '.join(str(text).split()); p = problem(text)
    if p: raise ValueError(p)
    if not hive.get(c, 'install_id'): hive.put(c, 'install_id', secrets.token_hex(16)); hive.put(c, 'salt', secrets.token_hex(8))
    d = {'v': 1, 'b': hive.get(c, 'bot') or 'ff', 'kind': kind, 'text': text, 'tv': hive.TV, 'rid': hive.rid(c), 'role': role()}
    if rating is not None: d['rating'] = rating
    body = json.dumps(d, separators=(',', ':'))
    if len(body.encode()) > FB_MAXB: raise ValueError('payload over 2KB')
    return body
def send(c, body, post=None):
    c.execute(LOG)
    def real(b):
        rq = urllib.request.Request(URL, data=b.encode(), headers={'content-type': 'application/json', 'user-agent': 'hive-feedback'}, method='POST')
        try:
            with urllib.request.urlopen(rq, timeout=20) as r: return r.status
        except urllib.error.HTTPError as e: return e.code
        except Exception: return 0
    st = (post or real)(body)
    c.execute('INSERT INTO feedback_sent(sent_at,status,body) VALUES(?,?,?)', (hive.now(), str(st), body))
    return st
MSG = {200: 'sent, thank you', 422: 'rejected as personal data; nothing stored', 429: 'daily feedback limit reached; try tomorrow', 0: 'network error; not sent'}

def selftest():
    import tempfile
    global MARKER
    d = tempfile.mkdtemp(); hive.DB = os.path.join(d, 'brain.db'); c = hive.con(); ok = True
    def chk(n, cond): nonlocal ok; ok &= bool(cond); print(('ok   ' if cond else 'FAIL ') + n)
    MARKER = os.path.join(d, 'MAIN_BOT')
    b = json.loads(build(c, 'feature', 'Sort  by follower count', 4))
    chk('clone without marker', b['role'] == 'clone')
    chk('exact keys', sorted(b) == sorted(['v', 'b', 'kind', 'text', 'tv', 'rid', 'role', 'rating']) and b['text'] == 'Sort by follower count')
    chk('rid is 16 hex and stable', re.fullmatch(r'[0-9a-f]{16}', b['rid']) and json.loads(build(c, 'bug', 'x'))['rid'] == b['rid'])
    chk('no rating key when omitted', 'rating' not in json.loads(build(c, 'bug', 'x')))
    open(MARKER, 'w').close(); chk('main with marker', json.loads(build(c, 'praise', 'great'))['role'] == 'main')
    for n, t in [('email', 'a@b.co'), ('phone', '555-123-4567'), ('link', 'https://x.y'), ('domain', 'example.com'), ('handle', 'hi @bob'), ('token', 'ghp_' + 'a' * 30), ('long', 'x' * 40), ('too long', 'a' * 501)]:
        try: build(c, 'bug', t); chk('reject ' + n, False)
        except ValueError: chk('reject ' + n, True)
    for k, r in [('rant', None), ('bug', 6), ('bug', 0)]:
        try: build(c, k, 'x', r); chk('reject %s/%s' % (k, r), False)
        except ValueError: chk('reject %s/%s' % (k, r), True)
    seen = []; st = send(c, build(c, 'bug', 'crash on scan', 2), post=lambda body: seen.append(body) or 200)
    row = c.execute('SELECT status,body FROM feedback_sent').fetchall()
    chk('fake send logged exactly', st == 200 and row == [('200', seen[0])])
    print('FEEDBACK CLIENT SELFTEST', 'OK' if ok else 'FAILED'); return 0 if ok else 1

def main(a):
    ap = argparse.ArgumentParser(prog='feedback.py'); ap.add_argument('cmd', choices=['preview', 'send', 'log', 'selftest'])
    ap.add_argument('--kind'); ap.add_argument('--text'); ap.add_argument('--rating', type=int); ap.add_argument('--yes', action='store_true')
    o = ap.parse_args(a)
    if o.cmd == 'selftest': return selftest()
    c = hive.con()
    if o.cmd == 'log':
        c.execute(LOG)
        for r in c.execute('SELECT sent_at,status,body FROM feedback_sent ORDER BY id'): print(*r, sep='\t')
        return 0
    try: body = build(c, o.kind, o.text or '', o.rating)
    except ValueError as e: print('not sendable:', e); return 2
    print('Exact payload that would be sent to', URL); print(body)
    if o.cmd == 'preview': return 0
    if not o.yes: print('not sent: pass --yes only after the end user agreed to this exact payload'); return 3
    st = send(c, body); print('status', st, '-', MSG.get(st, 'not accepted')); return 0 if st == 200 else 1

if __name__ == '__main__': sys.exit(main(sys.argv[1:]))

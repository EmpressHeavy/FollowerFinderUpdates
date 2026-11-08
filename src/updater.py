"""Follower Finder self-updater (v3.5.35).

Updates come ONLY from https://github.com/EmpressHeavy/FollowerFinderUpdates (branch main).
Layout installed under HOME (default ~/follower_finder, test installs pass their own folder):
  HOME/app/        code + SKILL.md + VERSION (replaced on update)
  HOME/snapshots/  previous app/ copies (rollback)
Everything else in HOME (brain.db, backup/, hive.py, ff_feedback.py, logs) is owner data and is never touched.

CLI:  python3 updater.py check|apply|selftest [--home DIR]
check prints JSON {status: current|newer|error, installed, remote}; apply installs a newer build
(snapshot first, roll back on any failure) and prints JSON {status: updated|current|rolled_back|error}.
"""
import io, json, os, re, shutil, sys, tarfile, tempfile, time, urllib.request

REPO = "EmpressHeavy/FollowerFinderUpdates"
VERSION_URL = "https://raw.githubusercontent.com/%s/main/VERSION" % REPO
TARBALL_URL = "https://codeload.github.com/%s/tar.gz/refs/heads/main" % REPO
APP_ITEMS = ("SKILL.md", "VERSION", "README.md", "CHANGELOG.md", "src", "hive", "templates", "tools", "config", "routines")
_VER = re.compile(r"^v?(\d+(?:\.\d+){0,3})$")


def parse_version(s):
    m = _VER.match((s or "").strip())
    if not m:
        raise ValueError("bad version: %r" % s)
    return tuple(int(x) for x in m.group(1).split("."))


def is_newer(remote, installed):
    r = parse_version(remote)
    try:
        i = parse_version(installed)
    except ValueError:
        return True  # nothing valid installed -> take the published build
    n = max(len(r), len(i))
    return r + (0,) * (n - len(r)) > i + (0,) * (n - len(i))


def _fetch(url, timeout=30):
    if not (url.startswith("https://raw.githubusercontent.com/%s/" % REPO) or url.startswith("https://codeload.github.com/%s/" % REPO)):
        raise ValueError("refusing non-official source")
    req = urllib.request.Request(url, headers={"user-agent": "ff-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.geturl().split("/")[2] not in ("raw.githubusercontent.com", "codeload.github.com"):
            raise ValueError("redirected off GitHub")
        return r.read()


def installed_version(home):
    p = os.path.join(home, "app", "VERSION")
    return open(p).read().strip() if os.path.exists(p) else ""


def check(home, fetch=_fetch):
    inst = installed_version(home)
    try:
        remote = fetch(VERSION_URL).decode().strip()
        parse_version(remote)
    except Exception as e:
        return {"status": "error", "installed": inst, "error": str(e)[:200]}
    return {"status": "newer" if is_newer(remote, inst) else "current", "installed": inst, "remote": remote}


def _safe_extract(blob, dest):
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
        for m in t.getmembers():
            p = os.path.normpath(m.name)
            if p.startswith("..") or os.path.isabs(p) or m.issym() or m.islnk():
                raise ValueError("unsafe path in archive: %s" % m.name)
        t.extractall(dest)  # members validated above
    tops = [d for d in os.listdir(dest) if os.path.isdir(os.path.join(dest, d))]
    if len(tops) != 1:
        raise ValueError("unexpected archive layout")
    return os.path.join(dest, tops[0])


def apply(home, fetch=_fetch):
    st = check(home, fetch)
    if st["status"] != "newer":
        return st
    app = os.path.join(home, "app")
    snap = None
    os.makedirs(home, exist_ok=True)
    if os.path.isdir(app):
        snap = os.path.join(home, "snapshots", "%s-%d" % (st["installed"] or "none", int(time.time())))
        os.makedirs(os.path.dirname(snap), exist_ok=True)
        shutil.copytree(app, snap)
    tmp = tempfile.mkdtemp(prefix="ff-update-")
    try:
        root = _safe_extract(fetch(TARBALL_URL, timeout=120), tmp)
        got = open(os.path.join(root, "VERSION")).read().strip()
        if parse_version(got) != parse_version(st["remote"]):
            raise ValueError("archive VERSION %s != %s" % (got, st["remote"]))
        if not os.path.exists(os.path.join(root, "SKILL.md")):
            raise ValueError("archive has no SKILL.md")
        new = app + ".new"
        shutil.rmtree(new, ignore_errors=True)
        os.makedirs(new)
        for item in APP_ITEMS:
            s = os.path.join(root, item)
            if os.path.isdir(s):
                shutil.copytree(s, os.path.join(new, item))
            elif os.path.isfile(s):
                shutil.copy2(s, os.path.join(new, item))
        shutil.rmtree(app, ignore_errors=True)
        os.replace(new, app)
        return {"status": "updated", "from": st["installed"], "to": got, "snapshot": snap,
                "skill": os.path.join(app, "SKILL.md")}
    except Exception as e:
        shutil.rmtree(app + ".new", ignore_errors=True)
        if snap:
            shutil.rmtree(app, ignore_errors=True)
            shutil.copytree(snap, app)
        return {"status": "rolled_back", "installed": st["installed"], "remote": st["remote"], "error": str(e)[:200]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    home = os.path.expanduser("~/follower_finder")
    if "--home" in a:
        i = a.index("--home"); home = os.path.expanduser(a[i + 1]); del a[i:i + 2]
    cmd = a[0] if a else "check"
    if cmd == "selftest":
        assert is_newer("v3.5.30", "v3.5.29.1") and not is_newer("v3.5.29.1", "v3.5.29.1") and is_newer("3.6", "")
        print("OK"); return 0
    out = check(home) if cmd == "check" else apply(home) if cmd == "apply" else None
    if out is None:
        print("usage: updater.py check|apply|selftest [--home DIR]"); return 2
    print(json.dumps(out)); return 0 if out["status"] in ("current", "newer", "updated") else 1


if __name__ == "__main__":
    sys.exit(main())

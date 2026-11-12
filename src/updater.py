"""Follower Finder self-updater (v3.5.39).

Updates come ONLY from https://github.com/EmpressHeavy/FollowerFinderUpdates (branch main).
Layout installed under HOME (default ~/follower_finder, test installs pass their own folder):
  HOME/app/        code + SKILL.md + VERSION (replaced on update)
  HOME/app/data/   per-user state (verified_review, memory/brain.db, and the rest). Never deleted.
  HOME/app/memory and HOME/app/*.db are per-user state too, when they live in the app tree.
  HOME/snapshots/  previous app/ copies (rollback)
  HOME/user-data-backups/  copy of that per-user state, taken before the code is swapped
Everything else in HOME (brain.db, backup/, hive.py, ff_feedback.py, logs, data/install_license.json) is owner data and is never touched.
A release that ships files under data/ only adds paths that are not already there. Existing user files are not
overwritten, truncated, or removed. If the swap fails, the previous app snapshot (including that user data) is put back.

CLI:  python3 updater.py check|apply|selftest [--home DIR]
check prints JSON {status: current|newer|error, installed, remote}; apply installs a newer build
(snapshot first, roll back on any failure) and prints JSON {status: updated|current|rolled_back|error}.
"""
import io, json, os, re, shutil, sys, tarfile, tempfile, time, urllib.request

REPO = "EmpressHeavy/FollowerFinderUpdates"
VERSION_URL = "https://raw.githubusercontent.com/%s/main/VERSION" % REPO
TARBALL_URL = "https://codeload.github.com/%s/tar.gz/refs/heads/main" % REPO
APP_ITEMS = ("SKILL.md", "VERSION", "README.md", "CHANGELOG.md", "src", "hive", "templates", "tools", "config", "routines")
# Names directly under app/ that are the owner's state, not the published code.
USER_STATE_DIRS = ("data", "memory")
_VER = re.compile(r"^v?(\d+(?:\.\d+){0,3})$")


def _is_user_state_name(name):
    return name in USER_STATE_DIRS or name.endswith(".db")


def user_state_entries(app):
    """Directory and database names under app/ that an update must keep. Symlinks are skipped."""
    if not app or not os.path.isdir(app):
        return []
    found = []
    for name in USER_STATE_DIRS:
        p = os.path.join(app, name)
        if os.path.isdir(p) and not os.path.islink(p):
            found.append(name)
    for name in sorted(os.listdir(app)):
        if not name.endswith(".db"):
            continue
        p = os.path.join(app, name)
        if os.path.isfile(p) and not os.path.islink(p):
            found.append(name)
    return found


def _walk_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            if not os.path.islink(p) and os.path.isfile(p):
                yield p


def user_state_bytes(root, names):
    """Relative path -> exact file bytes for the preserved names under root."""
    out = {}
    for name in names:
        src = os.path.join(root, name)
        if os.path.isfile(src) and not os.path.islink(src):
            with open(src, "rb") as f:
                out[name] = f.read()
        elif os.path.isdir(src) and not os.path.islink(src):
            for p in _walk_files(src):
                rel = os.path.relpath(p, root)
                with open(p, "rb") as f:
                    out[rel] = f.read()
    return out


def _copy_user_state(app, dest, names):
    os.makedirs(dest, exist_ok=True)
    for name in names:
        src = os.path.join(app, name)
        dst = os.path.join(dest, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def _restore_user_state(backup, app, names):
    """Put the backup back. Refuses to overwrite a path that is already there."""
    for name in names:
        src = os.path.join(backup, name)
        dst = os.path.join(app, name)
        if os.path.lexists(dst):
            raise RuntimeError("refusing to overwrite user data at %s" % name)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def merge_seeds(src, dst):
    """Copy files from a release data/ tree that the install does not already have.
    An existing file is never opened for write, replaced, or truncated."""
    if not os.path.isdir(src):
        return 0
    added = 0
    os.makedirs(dst, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        rel = os.path.relpath(dirpath, src)
        target = dst if rel == os.curdir else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for fn in filenames:
            s = os.path.join(dirpath, fn)
            d = os.path.join(target, fn)
            if os.path.islink(s) or os.path.lexists(d):
                continue
            shutil.copy2(s, d)
            added += 1
    return added


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
    user_backup = None
    preserved = []
    try:
        root = _safe_extract(fetch(TARBALL_URL, timeout=120), tmp)
        got = open(os.path.join(root, "VERSION")).read().strip()
        if parse_version(got) != parse_version(st["remote"]):
            raise ValueError("archive VERSION %s != %s" % (got, st["remote"]))
        if not os.path.exists(os.path.join(root, "SKILL.md")):
            raise ValueError("archive has no SKILL.md")
        preserved = user_state_entries(app)
        original = user_state_bytes(app, preserved) if preserved else {}
        if preserved:
            user_backup = os.path.join(home, "user-data-backups", "%s-%d" % (st["installed"] or "none", int(time.time())))
            _copy_user_state(app, user_backup, preserved)
            if user_state_bytes(user_backup, preserved) != original:
                raise RuntimeError("user-data backup does not match the install")
        new = app + ".new"
        shutil.rmtree(new, ignore_errors=True)
        os.makedirs(new)
        for item in APP_ITEMS:
            if _is_user_state_name(item):
                continue
            s = os.path.join(root, item)
            if os.path.isdir(s):
                shutil.copytree(s, os.path.join(new, item))
            elif os.path.isfile(s):
                shutil.copy2(s, os.path.join(new, item))
        if os.path.isdir(app):
            shutil.rmtree(app)
        os.replace(new, app)
        if preserved:
            _restore_user_state(user_backup, app, preserved)
            if user_state_bytes(app, preserved) != original:
                raise RuntimeError("user data changed during update")
        release_data = os.path.join(root, "data")
        if os.path.isdir(release_data):
            merge_seeds(release_data, os.path.join(app, "data"))
            if preserved:
                restored = user_state_bytes(app, preserved)
                for rel, blob in original.items():
                    if restored.get(rel) != blob:
                        raise RuntimeError("seed files overwrote user data at %s" % rel)
        return {"status": "updated", "from": st["installed"], "to": got, "snapshot": snap,
                "user_data_backup": user_backup, "skill": os.path.join(app, "SKILL.md")}
    except Exception as e:
        shutil.rmtree(app + ".new", ignore_errors=True)
        if snap:
            shutil.rmtree(app, ignore_errors=True)
            shutil.copytree(snap, app)
        return {"status": "rolled_back", "installed": st["installed"], "remote": st["remote"],
                "user_data_backup": user_backup, "error": str(e)[:200]}
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

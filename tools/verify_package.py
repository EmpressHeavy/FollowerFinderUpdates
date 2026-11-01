#!/usr/bin/env python3
"""Verify a Follower Finder checkout against an expected fingerprint.

Fingerprint = SHA-256 of the text made of one line per tracked file (`git ls-files`), sorted by path:
    "<sha256 of file bytes>  <relative path>\\n"
The expected value is NOT stored here; pass it in (it comes from the getting-started skill / template).

    python tools/verify_package.py                 # print the fingerprint
    python tools/verify_package.py <expected>      # exit 0 if it matches, 1 if not
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path


def fingerprint(repo: Path) -> str:
    files = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], check=True,
                           capture_output=True).stdout.decode().split("\0")
    lines = []
    for rel in sorted(f for f in files if f):
        lines.append(f"{hashlib.sha256((repo / rel).read_bytes()).hexdigest()}  {rel}\n")
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def main(argv: list[str]) -> int:
    repo = Path(__file__).resolve().parents[1]
    fp = fingerprint(repo)
    if len(argv) < 2:
        print(fp)
        return 0
    if fp == argv[1].strip().lower():
        print(f"OK  fingerprint matches: {fp}")
        return 0
    print(f"MISMATCH  expected {argv[1].strip()} but this checkout is {fp}. Do not install it.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

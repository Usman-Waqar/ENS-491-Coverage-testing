#!/usr/bin/env python3
"""Fetch the subject system (inih) at the commit pinned in csm.yaml into ../subject_system.

  python3 setup_subject.py            # works on Linux, macOS, WSL and Windows (needs git)

The checkout lives NEXT TO this repository (system.local_path in csm.yaml), not inside it, so
the third-party code is never committed here.
"""
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
model = yaml.safe_load((HERE / "csm.yaml").read_text())
repo, commit = model["system"]["repository"], model["system"]["commit"]
dest = (HERE / model["system"]["local_path"]).resolve()


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


if (dest / ".git").exists():
    head = git("rev-parse", "HEAD", cwd=dest)
    if head == commit:
        print(f"{dest} already at {commit[:12]}")
        sys.exit(0)
    sys.exit(f"{dest} exists at {head[:12]}, expected {commit[:12]}; move it away or run "
             f"'git -C \"{dest}\" checkout {commit}'")
if dest.exists() and any(dest.iterdir()):
    sys.exit(f"{dest} exists and is not a git checkout; move it away first")
dest.mkdir(parents=True, exist_ok=True)
git("init", "-q", cwd=dest)
git("remote", "add", "origin", repo, cwd=dest)
git("fetch", "-q", "--depth", "1", "origin", commit, cwd=dest)
git("checkout", "-q", "FETCH_HEAD", cwd=dest)
print(f"fetched {repo} @ {commit[:12]} into {dest}")

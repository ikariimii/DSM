"""Helpers for root tests that read older revisions from git (QA-057 item 3): skip cleanly with a reason when the
tree is not a git checkout (e.g. a `git archive` export) or the revision is not present (e.g. a shallow clone)."""
import os
import subprocess

import pytest


def repo_root_or_none(root):
    """The git top level if `root` itself is the top of a git work tree, else None (an export nested in some other
    repository does not count)."""
    try:
        r = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    except FileNotFoundError:
        return None
    if r.returncode != 0 or os.path.realpath(r.stdout.strip()) != os.path.realpath(root):
        return None
    return r.stdout.strip()


def require_git(root, why):
    if repo_root_or_none(root) is None:
        pytest.skip(f"no git repository at {root} (e.g. a `git archive` export); needed for {why}")


def git_show_or_skip(root, rev, path):
    require_git(root, f"{rev}:{path}")
    r = subprocess.run(["git", "-C", root, "show", f"{rev}:{path}"], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"{rev}:{path} not available in this clone ({r.stderr.strip()})")
    return r.stdout

"""Git worktree helpers and repo config lookup."""

from __future__ import annotations

import os
import subprocess

from sprout_worktree_db.paths import clean_env


def run(cmd, cwd=None, env=None, timeout=900):
    res = subprocess.run(
        cmd,
        cwd=cwd,
        env=env or clean_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return res.returncode, res.stdout.strip(), res.stderr.strip()


def git(args: list[str], cwd: str) -> str | None:
    try:
        rc, out, _ = run(["git", "-C", cwd, *args], timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return out if rc == 0 else None


def main_repo_of(worktree: str) -> str | None:
    """First `worktree list --porcelain` entry is the main checkout."""
    porcelain = git(["worktree", "list", "--porcelain"], worktree)
    if not porcelain:
        return None
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            return line[len("worktree ") :].strip()
    return None


def git_worktree_paths(repo_path: str) -> set[str]:
    porcelain = git(["worktree", "list", "--porcelain"], repo_path)
    if not porcelain:
        return set()
    paths: set[str] = set()
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            paths.add(os.path.realpath(line[len("worktree ") :].strip()))
    return paths


def branch_of(worktree: str) -> str | None:
    out = git(["rev-parse", "--abbrev-ref", "HEAD"], worktree)
    return out.strip() if out else None


def repo_config(cfg: dict, worktree: str) -> dict | None:
    main = main_repo_of(worktree) or ""
    for repo in cfg.get("repos", []):
        if main and os.path.realpath(main) == os.path.realpath(
            os.path.expanduser(repo["main_repo"])
        ):
            return repo
    worktree_real = os.path.realpath(worktree)
    for repo in cfg.get("repos", []):
        root = os.path.realpath(os.path.expanduser(repo["main_repo"]))
        if worktree_real.startswith(root + os.sep):
            return repo
        wt_root = repo.get("worktrees_root")
        if wt_root:
            wt_root = os.path.realpath(os.path.expanduser(wt_root))
            if worktree_real == wt_root or worktree_real.startswith(
                wt_root + os.sep
            ):
                return repo
    return None

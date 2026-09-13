"""Shared fixtures for sprout_worktree_db unit tests."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "sprout-worktree-db"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sprout_worktree_db.models import RepoConfig  # noqa: E402


def repo(name: str = "myapp", **kwargs) -> RepoConfig:
    return RepoConfig(
        name=name,
        main_repo=kwargs.pop("main_repo", "/tmp/main"),
        env_files=kwargs.pop("env_files", (".env",)),
        **kwargs,
    )

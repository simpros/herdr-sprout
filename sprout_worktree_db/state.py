"""State load/save and worktree key picking."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from sprout_worktree_db.paths import LEGACY_CONFIG_DIR, state_path


def normalize_key(raw: str) -> str | None:
    """Mirror sprout's normalizeWorktreeKey: lowercase, -collapse, max 40."""
    key = re.sub(r"[^a-z0-9-]+", "-", raw.lower())
    key = re.sub(r"-+", "-", key).strip("-")
    if len(key) > 40:
        key = key[:40].rstrip("-")
    return key or None


def object_name(key: str) -> str:
    return "sprout_wt_" + key.replace("-", "_")


def key_from_object(obj: str) -> str | None:
    if not obj.startswith("sprout_wt_"):
        return None
    return obj[len("sprout_wt_") :].replace("_", "-")


def stable_suffix(worktree: str) -> str:
    """Process-stable 5-hex digest of the realpath (not PYTHONHASHSEED)."""
    digest = hashlib.sha1(os.path.realpath(worktree).encode()).hexdigest()
    return digest[:5]


def load_state() -> dict:
    path = state_path()
    if not path.exists():
        legacy = LEGACY_CONFIG_DIR / "worktree-db-state.json"
        if legacy.exists():
            path = legacy
        else:
            return {"worktrees": {}}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"worktrees": {}}
    data.setdefault("worktrees", {})
    return data


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-")
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def pick_key(state: dict, worktree: str, repo: dict) -> str:
    """Basename-derived key; stable digest suffix if another live holder owns it.

    Prefer including the repo name when the basename alone would collide across
    repos sharing one Postgres. State remains the source of truth for object
    names during GC — never re-derive disambiguated keys from basename alone.
    """
    basename = normalize_key(Path(worktree).name)
    repo_slug = normalize_key(str(repo.get("name") or ""))
    # Prefer repo-qualified slug when it fits; falls back to basename.
    if basename and repo_slug:
        qualified = normalize_key(f"{repo_slug}-{basename}")
        base = qualified or basename
    else:
        base = basename or repo_slug
    if not base:
        raise SystemExit(f"cannot derive slug from worktree path: {worktree}")

    key = base
    holder = next(
        (
            p
            for p, rec in state["worktrees"].items()
            if rec.get("key") == key and p != worktree
        ),
        None,
    )
    if holder and os.path.exists(holder):
        key = f"{base[:34]}-{stable_suffix(worktree)}"
        if len(key) > 40:
            key = f"{base[:34]}-{stable_suffix(worktree)}"[:40]
    return key

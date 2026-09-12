"""State load/save and worktree key minting / release."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sprout_worktree_db.gitutil import repo_config
from sprout_worktree_db.models import (
    PluginConfig,
    PluginState,
    RepoConfig,
    WorktreeRecord,
)
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


def mint_key(worktree: str, repo: RepoConfig) -> str:
    """Always content-addressed: `{repo}-{basename}-{path-digest}` (max 40).

    No collision branching — first provision and re-provision agree when the
    path is unchanged. Callers must prefer an existing state row's key before
    minting (see resolve_key).
    """
    basename = normalize_key(Path(worktree).name)
    repo_slug = normalize_key(repo.name)
    if basename and repo_slug:
        qualified = normalize_key(f"{repo_slug}-{basename}")
        base = qualified or basename
    else:
        base = basename or repo_slug
    if not base:
        raise SystemExit(f"cannot derive slug from worktree path: {worktree}")
    suffix = stable_suffix(worktree)
    # Reserve 6 chars for "-xxxxx"; truncate base so the full key fits in 40.
    key = f"{base[:34]}-{suffix}"
    if len(key) > 40:
        key = key[:40].rstrip("-")
    return key


def resolve_key(
    state: PluginState,
    worktree: str,
    repo: RepoConfig,
    requested: str | None = None,
) -> str:
    """State owns the slug once claimed; --key only seeds a first mint."""
    existing = state.worktrees.get(worktree)
    if existing and existing.key:
        if requested:
            normalized = normalize_key(requested)
            if requested != existing.key and normalized != existing.key:
                raise SystemExit(
                    f"{worktree} already claimed as {existing.key!r}; "
                    f"drop/forget first, or omit --key (got {requested!r})"
                )
        return existing.key
    if requested:
        key = normalize_key(requested)
        if not key:
            raise SystemExit(f"invalid --key: {requested!r}")
        return key
    return mint_key(worktree, repo)


def _read_state_file() -> PluginState:
    path = state_path()
    if not path.exists():
        legacy = LEGACY_CONFIG_DIR / "worktree-db-state.json"
        if legacy.exists():
            path = legacy
        else:
            return PluginState()
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return PluginState()
    if not isinstance(data, dict):
        return PluginState()
    return PluginState.from_dict(data)


def load_state() -> PluginState:
    return _read_state_file()


def save_state(state: PluginState) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-")
    with os.fdopen(fd, "w") as fh:
        json.dump(state.to_dict(), fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


@contextmanager
def locked_state() -> Iterator[PluginState]:
    """Exclusive lock around load → mutate → save of state.json."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_fd:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        state = _read_state_file()
        yield state
        save_state(state)


def claim_key(
    worktree: str,
    repo: RepoConfig,
    *,
    mode: str,
    requested: str | None = None,
) -> tuple[str, WorktreeRecord | None]:
    """Atomically resolve + persist a key claim before slow sprout work.

    Returns (key, previous_record). Re-provision reuses the stored key so
    passwords/objects stay stable even if mint rules change.
    """
    with locked_state() as state:
        previous = state.worktrees.get(worktree)
        key = resolve_key(state, worktree, repo, requested)
        # Keep prior object/created_at when reclaiming the same key so a crash
        # mid-provision does not orphan the row's identity.
        if previous and previous.key == key:
            claim = WorktreeRecord(
                key=key,
                repo=repo.name,
                mode="preview" if mode == "preview" else "dedicated",
                object=previous.object,
                created_at=previous.created_at,
                env_files=list(previous.env_files),
                steps=list(previous.steps),
                pr_id=previous.pr_id,
                preview_url=previous.preview_url,
            )
        else:
            claim = WorktreeRecord(
                key=key,
                repo=repo.name,
                mode="preview" if mode == "preview" else "dedicated",
                object="",
                created_at=datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
            )
        state.worktrees[worktree] = claim
        return key, previous


def release_key(
    cfg: PluginConfig,
    worktree: str | None,
    *,
    requested: str | None = None,
    only_if_key: str | None = None,
    only_if_object: str | None = None,
) -> tuple[str | None, bool]:
    """Inverse of claim_key: resolve + forget under lock, then caller drops.

    Returns (key, skip_drop). ``key`` is None when ``only_if_*`` guards fail
    (a concurrent re-provision replaced the claim — caller must not drop).

    ``requested`` is the drop/forget escape hatch: forget every row with that
    key and return it for ``drop_key``. Provision must not use this path.
    """
    with locked_state() as state:
        wt = os.path.realpath(worktree) if worktree else None
        record = state.worktrees.get(wt) if wt else None

        if only_if_key is not None or only_if_object is not None:
            if record is None:
                # Already forgotten; still allow orphan drop via planned key.
                return only_if_key, False
            if only_if_key is not None and record.key != only_if_key:
                return None, True
            if (
                only_if_object is not None
                and record.object
                and record.object != only_if_object
            ):
                return None, True
            key = record.key
            shared = record.mode == "preview"
            assert wt is not None
            state.worktrees.pop(wt, None)
            return key, shared

        if requested:
            key = normalize_key(requested)
            if not key:
                raise SystemExit(f"invalid --key: {requested!r}")
            shared = False
            for path, rec in list(state.worktrees.items()):
                if rec.key == key:
                    shared = shared or rec.mode == "preview"
                    state.worktrees.pop(path, None)
            return key, shared

        if record and wt:
            key = record.key
            shared = record.mode == "preview"
            state.worktrees.pop(wt, None)
            return key, shared

        if not wt:
            raise SystemExit("drop needs --worktree or --key")

        repo = repo_config(cfg, wt)
        if repo:
            # Honest recovery when state was wiped but path+repo still known.
            return mint_key(wt, repo), False

        raise SystemExit(
            f"no state row for {wt}; pass --key "
            "(or ensure repo config matches so the slug can be reminted)"
        )

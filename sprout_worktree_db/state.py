"""State load/save and worktree key minting / drop leases."""

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
    DropLease,
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
        text = path.read_text()
    except OSError as exc:
        raise SystemExit(f"cannot read state.json: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"corrupt state.json: invalid JSON ({exc}); "
            "fix or remove the file — refusing to wipe claims"
        ) from exc
    if not isinstance(data, dict):
        raise SystemExit(
            "corrupt state.json: root must be an object; "
            "refusing to wipe claims"
        )
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
        if previous and previous.status == "dropping":
            raise SystemExit(
                f"{worktree}: drop in progress for key {previous.key!r}"
            )
        key = resolve_key(state, worktree, repo, requested)
        if key in state.dropping:
            raise SystemExit(f"key {key!r}: drop in progress")
        mode_lit = "preview" if mode == "preview" else "dedicated"
        if previous is None:
            claim = WorktreeRecord(
                key=key,
                repo=repo.name,
                mode=mode_lit,
                object="",
                created_at=datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
            )
        else:
            # resolve_key always returns previous.key when a row exists.
            claim = WorktreeRecord(
                key=previous.key,
                repo=repo.name,
                mode=mode_lit,
                object=previous.object,
                created_at=previous.created_at,
                env_files=list(previous.env_files),
                steps=list(previous.steps),
                pr_id=previous.pr_id,
                preview_url=previous.preview_url,
            )
        state.worktrees[worktree] = claim
        return key, previous


def finalize_claim(
    worktree: str, key: str, record: WorktreeRecord
) -> None:
    """Compare-and-swap provision result onto a non-dropping claim."""
    with locked_state() as state:
        claimed = state.worktrees.get(worktree)
        if claimed is None:
            raise RuntimeError(
                f"key claim lost for {worktree}: row removed during provision "
                f"(provisioned {key!r})"
            )
        if claimed.status == "dropping":
            raise RuntimeError(
                f"key claim lost for {worktree}: drop in progress for "
                f"{claimed.key!r} (provisioned {key!r})"
            )
        if claimed.key != key:
            raise RuntimeError(
                f"key claim lost for {worktree}: held {claimed.key!r}, "
                f"provisioned {key!r}"
            )
        if claimed.created_at:
            record.created_at = claimed.created_at
        record.status = "ready"
        state.worktrees[worktree] = record


def _reserve_dropping(
    state: PluginState,
    key: str,
    worktree: str | None,
    record: WorktreeRecord | None,
) -> DropLease:
    """Mark slug reserved; keep the worktree row until finish_drop."""
    skip_postgres = bool(record and record.mode == "preview")
    obj = ""
    if record:
        obj = record.object or (object_name(key) if not skip_postgres else "")
        record.status = "dropping"
        if worktree:
            state.worktrees[worktree] = record
    state.dropping[key] = worktree or ""
    return DropLease(
        key=key,
        worktree=worktree,
        object_name=obj,
        skip_postgres=skip_postgres,
    )


def begin_drop(
    cfg: PluginConfig,
    worktree: str | None = None,
    *,
    requested: str | None = None,
    expect_key: str | None = None,
    expect_object: str | None = None,
) -> DropLease | None:
    """Reserve the slug under lock before slow Postgres drop.

    Returns None when ``expect_*`` identity no longer matches (concurrent
    re-provision replaced the claim — caller must not drop).
    """
    with locked_state() as state:
        wt = os.path.realpath(worktree) if worktree else None
        record = state.worktrees.get(wt) if wt else None

        if expect_key is not None or expect_object is not None:
            if record is None:
                # Already forgotten; still allow orphan drop via planned key.
                if expect_key and expect_key in state.dropping:
                    return DropLease(
                        key=expect_key,
                        worktree=wt,
                        object_name=expect_object or object_name(expect_key),
                        skip_postgres=False,
                    )
                if expect_key:
                    state.dropping[expect_key] = wt or ""
                    return DropLease(
                        key=expect_key,
                        worktree=wt,
                        object_name=expect_object or object_name(expect_key),
                        skip_postgres=False,
                    )
                return None
            if expect_key is not None and record.key != expect_key:
                return None
            if (
                expect_object is not None
                and record.object
                and record.object != expect_object
            ):
                return None
            assert wt is not None
            if record.status == "dropping" and record.key in state.dropping:
                return DropLease(
                    key=record.key,
                    worktree=wt,
                    object_name=record.object or object_name(record.key),
                    skip_postgres=record.mode == "preview",
                )
            return _reserve_dropping(state, record.key, wt, record)

        if requested:
            key = normalize_key(requested)
            if not key:
                raise SystemExit(f"invalid --key: {requested!r}")
            # Forget every row with this key; reserve slug until finish.
            matched = [
                (path, rec)
                for path, rec in list(state.worktrees.items())
                if rec.key == key
            ]
            skip = False
            obj = object_name(key)
            for path, rec in matched:
                skip = skip or rec.mode == "preview"
                rec.status = "dropping"
                state.worktrees[path] = rec
                if rec.object:
                    obj = rec.object
            state.dropping[key] = matched[0][0] if matched else (wt or "")
            return DropLease(
                key=key,
                worktree=matched[0][0] if matched else wt,
                object_name=obj,
                skip_postgres=skip,
            )

        if record and wt:
            if record.status == "dropping" and record.key in state.dropping:
                return DropLease(
                    key=record.key,
                    worktree=wt,
                    object_name=record.object or object_name(record.key),
                    skip_postgres=record.mode == "preview",
                )
            return _reserve_dropping(state, record.key, wt, record)

        if not wt:
            raise SystemExit("drop needs --worktree or --key")

        repo = repo_config(cfg, wt)
        if repo:
            # Honest recovery when state was wiped but path+repo still known.
            key = mint_key(wt, repo)
            if key in state.dropping:
                raise SystemExit(f"key {key!r}: drop in progress")
            state.dropping[key] = wt
            return DropLease(
                key=key,
                worktree=wt,
                object_name=object_name(key),
                skip_postgres=False,
            )

        raise SystemExit(
            f"no state row for {wt}; pass --key "
            "(or ensure repo config matches so the slug can be reminted)"
        )


def finish_drop(lease: DropLease) -> None:
    """Pop the dropping claim only if the lease still owns the slug."""
    with locked_state() as state:
        if lease.worktree:
            rec = state.worktrees.get(lease.worktree)
            if (
                rec
                and rec.status == "dropping"
                and rec.key == lease.key
            ):
                state.worktrees.pop(lease.worktree, None)
            # Also clear any other rows marked dropping for this key
            # (drop --key may have reserved several paths).
            for path, other in list(state.worktrees.items()):
                if other.key == lease.key and other.status == "dropping":
                    state.worktrees.pop(path, None)
        else:
            for path, other in list(state.worktrees.items()):
                if other.key == lease.key and other.status == "dropping":
                    state.worktrees.pop(path, None)
        state.dropping.pop(lease.key, None)


def abort_drop(lease: DropLease) -> None:
    """Clear dropping reservation after a failed Postgres drop (keep claim)."""
    with locked_state() as state:
        if lease.worktree:
            rec = state.worktrees.get(lease.worktree)
            if (
                rec
                and rec.status == "dropping"
                and rec.key == lease.key
            ):
                rec.status = "ready"
                state.worktrees[lease.worktree] = rec
        for path, other in list(state.worktrees.items()):
            if other.key == lease.key and other.status == "dropping":
                other.status = "ready"
                state.worktrees[path] = other
        state.dropping.pop(lease.key, None)

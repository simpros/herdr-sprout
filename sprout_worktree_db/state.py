"""State load/save and lease reclaim (persistence only).

Slug math lives in :mod:`sprout_worktree_db.keys`; lease lifecycles
(provision session, drop resolve/reserve/finish/abort) live in
:mod:`sprout_worktree_db.leases`. This module owns lock + file + TTL
reclaim plus read helpers (``claim_status``, ``path_for_key``).
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sprout_worktree_db.models import (
    PluginState,
    SlugLease,
    WorktreeRecord,
)
from sprout_worktree_db.paths import state_path

# Crash mid-op leaves leases[K] forever without reclaim. Expired leases
# are abort-equivalent under lock so automation can recover.
LEASE_TTL_SECONDS = 3600


def _read_state_file() -> PluginState:
    """Read-only parse of state.json (never persists).

    Legacy shapes are normalized in memory by
    :func:`models.normalize_state_dict`; the canonical form is persisted
    lazily by the next :func:`locked_state` mutation — never from this
    lock-free read path, so concurrent status/GC readers cannot tear
    the write. Legacy *path* migration (``~/.config/sprout/...`` → herdr
    state dir) happens once in :func:`paths.state_path`, shared with
    config/secrets.
    """
    path = state_path()
    if not path.exists():
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
    state = PluginState.from_dict(data)
    return state


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
    """Exclusive lock around load → mutate → save of state.json.

    The exit save also persists any in-memory legacy normalization in
    canonical form, so unlocked readers stay read-only. The save runs
    even when the critical section refuses (``SystemExit`` /
    ``RuntimeError``): reclaim and other in-lock mutations are
    abort-equivalent, and a refused op must not roll back prior reclaim
    — the exclusive flock already serializes writers.
    """
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_fd:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        state = _read_state_file()
        try:
            yield state
        finally:
            save_state(state)


def claim_status(state: PluginState, rec: WorktreeRecord) -> str:
    """Effective claim status — lease membership is authoritative."""
    lease = state.leases.get(rec.key)
    if lease is None:
        return "ready"
    return "provisioning" if lease.op == "provision" else "dropping"


def _lease_age_seconds(res: SlugLease, now: datetime) -> float | None:
    """Seconds since reserved_at, or None if unparseable. Missing → expired."""
    if not res.reserved_at:
        return None  # legacy / missing → reclaimable
    try:
        reserved = datetime.fromisoformat(res.reserved_at)
    except ValueError:
        return None
    if reserved.tzinfo is None:
        reserved = reserved.replace(tzinfo=timezone.utc)
    return (now - reserved).total_seconds()


def _lease_expired(
    lease: SlugLease, now: datetime, ttl_seconds: int
) -> bool:
    """Single TTL predicate — missing/unparseable age counts as expired."""
    age = _lease_age_seconds(lease, now)
    return age is None or age >= ttl_seconds


def reclaim_expired_leases(
    state: PluginState,
    *,
    force_all: bool = False,
    ttl_seconds: int = LEASE_TTL_SECONDS,
) -> list[str]:
    """Pop expired (or all, when force_all) lease entries. Returns cleared keys.

    Abort-equivalent: claims stay in worktrees; only the reservation clears.
    """
    now = datetime.now(timezone.utc)
    cleared: list[str] = []
    for key, res in list(state.leases.items()):
        if not force_all and not _lease_expired(res, now, ttl_seconds):
            continue
        state.leases.pop(key, None)
        cleared.append(key)
    return cleared


def expired_lease_keys(
    state: PluginState,
    *,
    ttl_seconds: int = LEASE_TTL_SECONDS,
    all_leases: bool = False,
) -> list[str]:
    """Keys whose leases are expired (or all, when all_leases)."""
    if all_leases:
        return list(state.leases)
    now = datetime.now(timezone.utc)
    return [
        key
        for key, res in state.leases.items()
        if _lease_expired(res, now, ttl_seconds)
    ]


def path_for_key(state: PluginState, key: str) -> str | None:
    """The unique claim path for ``key``, or None (one key → one path)."""
    for path, rec in state.worktrees.items():
        if rec.key == key:
            return path
    return None

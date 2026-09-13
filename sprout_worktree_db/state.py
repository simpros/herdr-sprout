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
from sprout_worktree_db.paths import LEGACY_CONFIG_DIR, state_path

# Crash mid-op leaves leases[K] forever without reclaim. Expired leases
# are abort-equivalent under lock so automation can recover.
LEASE_TTL_SECONDS = 3600


def _is_legacy_shape(data: dict) -> bool:
    """True when the raw JSON still carries a pre-leases schema."""
    if "dropping" in data:
        return True
    leases = data.get("leases")
    if isinstance(leases, dict):
        for value in leases.values():
            if isinstance(value, str):
                return True
            if isinstance(value, dict) and (
                "skip_postgres" in value or "op" not in value
            ):
                return True
    return False


def _read_state_file() -> PluginState:
    primary = state_path()
    path = primary
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
    state = PluginState.from_dict(data)
    if path != primary or _is_legacy_shape(data):
        # One-shot migrate: persist the canonical form so later loads
        # parse only the modern schema (legacy file left in place).
        try:
            save_state(state)
        except OSError:
            pass
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
    """Exclusive lock around load → mutate → save of state.json."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_fd:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        state = _read_state_file()
        yield state
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

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
    DropPlan,
    DropReservation,
    PluginConfig,
    PluginState,
    RepoConfig,
    WorktreeRecord,
)
from sprout_worktree_db.paths import LEGACY_CONFIG_DIR, log, state_path

# Crash mid-drop leaves dropping[K] forever without reclaim. Expired leases
# are abort-equivalent under lock so automation can recover.
LEASE_TTL_SECONDS = 3600


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


def claim_status(state: PluginState, rec: WorktreeRecord) -> str:
    """Effective claim status — dropping membership is authoritative."""
    return "dropping" if rec.key in state.dropping else "ready"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lease_age_seconds(res: DropReservation, now: datetime) -> float | None:
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


def reclaim_expired_leases(
    state: PluginState,
    *,
    force_keys: set[str] | None = None,
    ttl_seconds: int = LEASE_TTL_SECONDS,
) -> list[str]:
    """Pop expired (or force_keys) dropping entries. Returns cleared keys.

    Abort-equivalent: claims stay in worktrees; only the reservation clears.
    """
    now = datetime.now(timezone.utc)
    cleared: list[str] = []
    for key, res in list(state.dropping.items()):
        if force_keys is not None:
            if key not in force_keys:
                continue
        else:
            age = _lease_age_seconds(res, now)
            if age is not None and age < ttl_seconds:
                continue
        state.dropping.pop(key, None)
        cleared.append(key)
    return cleared


def expired_lease_keys(
    state: PluginState,
    *,
    ttl_seconds: int = LEASE_TTL_SECONDS,
    all_leases: bool = False,
) -> list[str]:
    """Keys whose drop leases are expired (or all, when all_leases)."""
    if all_leases:
        return list(state.dropping)
    now = datetime.now(timezone.utc)
    out: list[str] = []
    for key, res in state.dropping.items():
        age = _lease_age_seconds(res, now)
        if age is None or age >= ttl_seconds:
            out.append(key)
    return out


def paths_for_key(state: PluginState, key: str) -> tuple[str, ...]:
    """Every state path that currently holds ``key`` (authoritative forget-set)."""
    return tuple(p for p, r in state.worktrees.items() if r.key == key)


def _path_is_live(path: str, live_paths: set[str]) -> bool:
    real = os.path.realpath(path) if path else ""
    return bool(path and (os.path.exists(path) or real in live_paths))


def key_has_live_sibling(
    state: PluginState,
    key: str,
    *,
    except_path: str | None,
    live_paths: set[str],
) -> bool:
    """True if another path still holds ``key`` and exists on disk / in live set."""
    for path, rec in state.worktrees.items():
        if rec.key != key:
            continue
        if except_path is not None and path == except_path:
            continue
        if _path_is_live(path, live_paths):
            return True
    return False


def prune_gone_sibling_rows(
    state: PluginState, live_paths: set[str]
) -> list[str]:
    """Forget gone paths whose key is still held by a live sibling (no postgres).

    Does not take a drop lease — the live claim owns the slug.
    """
    pruned: list[str] = []
    for path, rec in list(state.worktrees.items()):
        if not rec.key or _path_is_live(path, live_paths):
            continue
        if key_has_live_sibling(
            state, rec.key, except_path=path, live_paths=live_paths
        ):
            state.worktrees.pop(path, None)
            pruned.append(path)
    return pruned


def claim_key(
    worktree: str,
    repo: RepoConfig,
    *,
    mode: str,
    requested: str | None = None,
) -> tuple[str, WorktreeRecord | None]:
    """Atomically resolve + persist a key claim before slow sprout work.

    Returns (key, previous_record). Re-provision reuses the stored key so
    passwords/objects stay stable even if mint rules change. One key maps to
    at most one worktree path.
    """
    with locked_state() as state:
        reclaim_expired_leases(state)
        previous = state.worktrees.get(worktree)
        if previous and previous.key in state.dropping:
            raise SystemExit(
                f"{worktree}: drop in progress for key {previous.key!r}"
            )
        key = resolve_key(state, worktree, repo, requested)
        if key in state.dropping:
            raise SystemExit(f"key {key!r}: drop in progress")
        holder = next(
            (
                p
                for p, r in state.worktrees.items()
                if r.key == key and p != worktree
            ),
            None,
        )
        if holder:
            raise SystemExit(
                f"key {key!r} already claimed by {holder}; "
                "drop that worktree first"
            )
        mode_lit = "preview" if mode == "preview" else "dedicated"
        if previous is None:
            claim = WorktreeRecord(
                key=key,
                repo=repo.name,
                mode=mode_lit,
                object="",
                created_at=_now_iso(),
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
        reclaim_expired_leases(state)
        claimed = state.worktrees.get(worktree)
        if claimed is None:
            raise RuntimeError(
                f"key claim lost for {worktree}: row removed during provision "
                f"(provisioned {key!r})"
            )
        if claimed.key in state.dropping:
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
        state.worktrees[worktree] = record


def _forget_set(
    state: PluginState, key: str, extra: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Authoritative forget-set: every path holding key, plus remint extras."""
    paths = list(paths_for_key(state, key))
    for p in extra:
        if p and p not in paths:
            paths.append(p)
    return tuple(paths)


def _reserve(
    state: PluginState,
    key: str,
    *,
    worktrees: tuple[str, ...],
    object_name: str,
    skip_postgres: bool,
) -> DropLease | None:
    """Exclusive slug reservation. Returns None if already reserved."""
    if key in state.dropping:
        return None
    lease_id = state.next_lease_id
    state.next_lease_id = lease_id + 1
    forget = _forget_set(state, key, worktrees)
    state.dropping[key] = DropReservation(
        lease_id=lease_id,
        worktrees=forget,
        object_name=object_name,
        skip_postgres=skip_postgres,
        reserved_at=_now_iso(),
    )
    return DropLease(
        lease_id=lease_id,
        key=key,
        worktrees=forget,
        object_name=object_name,
        skip_postgres=skip_postgres,
    )


def _resolve_and_reserve(
    state: PluginState,
    cfg: PluginConfig,
    worktree: str | None,
    requested: str | None,
) -> DropLease:
    """Single drop entrypoint: by --key, by worktree row, or remint recovery."""
    wt = os.path.realpath(worktree) if worktree else None

    if requested:
        key = normalize_key(requested)
        if not key:
            raise SystemExit(f"invalid --key: {requested!r}")
        matched = [
            (path, rec)
            for path, rec in state.worktrees.items()
            if rec.key == key
        ]
        skip = any(rec.mode == "preview" for _, rec in matched)
        obj = object_name(key)
        for _, rec in matched:
            if rec.object:
                obj = rec.object
                break
        extra = (wt,) if wt else ()
        lease = _reserve(
            state,
            key,
            worktrees=extra,
            object_name=obj,
            skip_postgres=skip,
        )
        if lease is None:
            raise SystemExit(f"key {key!r}: drop in progress")
        return lease

    if wt:
        record = state.worktrees.get(wt)
        if record:
            obj = record.object or (
                "" if record.mode == "preview" else object_name(record.key)
            )
            lease = _reserve(
                state,
                record.key,
                worktrees=(wt,),
                object_name=obj,
                skip_postgres=record.mode == "preview",
            )
            if lease is None:
                raise SystemExit(
                    f"key {record.key!r}: drop in progress"
                )
            return lease

        repo = repo_config(cfg, wt)
        if repo:
            key = mint_key(wt, repo)
            lease = _reserve(
                state,
                key,
                worktrees=(wt,),
                object_name=object_name(key),
                skip_postgres=False,
            )
            if lease is None:
                raise SystemExit(f"key {key!r}: drop in progress")
            return lease

        raise SystemExit(
            f"no state row for {wt}; pass --key "
            "(or ensure repo config matches so the slug can be reminted)"
        )

    raise SystemExit("drop needs --worktree or --key")


def begin_drop(
    cfg: PluginConfig,
    worktree: str | None = None,
    *,
    requested: str | None = None,
    force: bool = False,
) -> DropLease:
    """Reserve the slug under lock before slow Postgres drop (exclusive)."""
    with locked_state() as state:
        if force:
            force_keys: set[str] = set()
            if requested:
                nk = normalize_key(requested)
                if nk:
                    force_keys.add(nk)
            wt = os.path.realpath(worktree) if worktree else None
            if wt and wt in state.worktrees:
                force_keys.add(state.worktrees[wt].key)
            if force_keys:
                cleared = reclaim_expired_leases(state, force_keys=force_keys)
                for key in cleared:
                    log(f"drop --force: reclaimed lease for {key!r}")
            else:
                reclaim_expired_leases(state)
        else:
            cleared = reclaim_expired_leases(state)
            for key in cleared:
                log(f"reclaimed expired drop lease for {key!r}")
        return _resolve_and_reserve(state, cfg, worktree, requested)


def reserve_from_plan(state: PluginState, plan: DropPlan) -> DropLease | None:
    """Reserve under an already-held lock (GC). None if slug busy / gone."""
    if plan.state_path:
        rec = state.worktrees.get(plan.state_path)
        if rec is None:
            return None
        obj = rec.object or plan.object_name or (
            object_name(rec.key) if rec.key else ""
        )
        return _reserve(
            state,
            rec.key,
            worktrees=(plan.state_path,),
            object_name=obj,
            skip_postgres=plan.skip_drop or rec.mode == "preview",
        )
    if not plan.key:
        return None
    return _reserve(
        state,
        plan.key,
        worktrees=(),
        object_name=plan.object_name or object_name(plan.key),
        skip_postgres=plan.skip_drop,
    )


def _clear_lease(state: PluginState, lease: DropLease, *, restore: bool) -> None:
    reserved = state.dropping.get(lease.key)
    if reserved is None or reserved.lease_id != lease.lease_id:
        return
    if not restore:
        # Reservation owns the forget-set — no same-key scan.
        for path in reserved.worktrees:
            state.worktrees.pop(path, None)
    state.dropping.pop(lease.key, None)


def finish_drop(lease: DropLease) -> None:
    """Pop the claim only if this lease still owns the slug."""
    with locked_state() as state:
        _clear_lease(state, lease, restore=False)


def abort_drop(lease: DropLease) -> None:
    """Clear reservation after a failed Postgres drop (keep claim)."""
    with locked_state() as state:
        _clear_lease(state, lease, restore=True)

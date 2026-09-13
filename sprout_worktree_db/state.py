"""State load/save and worktree key minting / slug leases."""

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
    DropOp,
    LeaseOp,
    Mode,
    PluginConfig,
    PluginState,
    RepoConfig,
    SlugLease,
    WorktreeRecord,
)
from sprout_worktree_db.paths import LEGACY_CONFIG_DIR, log, state_path

# Crash mid-op leaves leases[K] forever without reclaim. Expired leases
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


def postgres_target(
    rec: WorktreeRecord | None, key: str
) -> tuple[str, bool]:
    """(object_name, touch_postgres) — single rule for drop + GC."""
    if rec is None:
        return object_name(key), True
    if rec.mode == "preview":
        return (rec.object or ""), False
    return (rec.object or object_name(rec.key)), True


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
    """Effective claim status — lease membership is authoritative."""
    lease = state.leases.get(rec.key)
    if lease is None:
        return "ready"
    return "provisioning" if lease.op == "provision" else "dropping"


def _busy_msg(key: str, lease: SlugLease) -> str:
    return f"key {key!r}: {lease.op} in progress"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        if not force_all:
            age = _lease_age_seconds(res, now)
            if age is not None and age < ttl_seconds:
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
    out: list[str] = []
    for key, res in state.leases.items():
        age = _lease_age_seconds(res, now)
        if age is None or age >= ttl_seconds:
            out.append(key)
    return out


def path_for_key(state: PluginState, key: str) -> str | None:
    """The unique claim path for ``key``, or None (one key → one path)."""
    for path, rec in state.worktrees.items():
        if rec.key == key:
            return path
    return None


def claim_key(
    worktree: str,
    repo: RepoConfig,
    *,
    mode: Mode,
    requested: str | None = None,
) -> tuple[str, int]:
    """Atomically resolve + persist a provision lease before slow sprout work.

    Returns ``(key, lease_id)``. Re-provision reuses the stored key so
    passwords/objects stay stable. One key maps to at most one worktree path.

    Claim reserves the key under an exclusive provision lease. On re-claim the
    existing row is left untouched until ``finalize_claim`` writes mode/object.
    In-place mode changes are refused — drop first so dedicated ``sprout_wt_*``
    teardown stays on the drop/GC path (no dual-object-under-one-key).
    """
    with locked_state() as state:
        reclaim_expired_leases(state)
        previous = state.worktrees.get(worktree)
        if previous and previous.key in state.leases:
            raise SystemExit(_busy_msg(previous.key, state.leases[previous.key]))
        if previous is not None and previous.mode != mode:
            raise SystemExit(
                f"{worktree} is {previous.mode}; "
                f"drop first, then re-run for {mode}"
            )
        key = resolve_key(state, worktree, repo, requested)
        if key in state.leases:
            raise SystemExit(_busy_msg(key, state.leases[key]))
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
        if previous is None:
            state.worktrees[worktree] = WorktreeRecord(
                key=key,
                repo=repo.name,
                mode=mode,
                object="",
                created_at=_now_iso(),
            )
        lease = _mint_lease(
            state,
            key,
            op="provision",
            worktrees=(worktree,),
            object_name="",
            touch_postgres=False,
        )
        return key, lease.lease_id


def finalize_claim(
    worktree: str, key: str, lease_id: int, record: WorktreeRecord
) -> None:
    """Compare-and-swap provision result; keep the provision lease.

    The lease stays until ``release_claim`` so concurrent drop/GC cannot tear
    down the object between finalize and deferred env merge / steps.
    """
    with locked_state() as state:
        reclaim_expired_leases(state)
        claimed = state.worktrees.get(worktree)
        if claimed is None:
            raise RuntimeError(
                f"key claim lost for {worktree}: row removed during provision "
                f"(provisioned {key!r})"
            )
        lease = state.leases.get(key)
        if (
            lease is None
            or lease.lease_id != lease_id
            or lease.op != "provision"
        ):
            raise RuntimeError(
                f"key claim lost for {worktree}: provision lease gone "
                f"(provisioned {key!r})"
            )
        if claimed.key != key:
            raise RuntimeError(
                f"key claim lost for {worktree}: held {claimed.key!r}, "
                f"provisioned {key!r}"
            )
        if claimed.created_at:
            record.created_at = claimed.created_at
        state.worktrees[worktree] = record


def _pop_provision_lease(key: str, lease_id: int) -> None:
    """Lease-id-fenced pop of a provision lease (no-op when fenced out)."""
    with locked_state() as state:
        lease = state.leases.get(key)
        if (
            lease is None
            or lease.lease_id != lease_id
            or lease.op != "provision"
        ):
            return
        state.leases.pop(key, None)


def abort_claim(worktree: str, key: str, lease_id: int) -> None:
    """Clear a provision lease after failed sprout work; keep the claim row."""
    _pop_provision_lease(key, lease_id)


def release_claim(worktree: str, key: str, lease_id: int) -> None:
    """Lease-id-fenced pop after env merge + steps (or failure thereof)."""
    _pop_provision_lease(key, lease_id)


def update_claim_steps(
    worktree: str, key: str, lease_id: int, steps: list[dict]
) -> None:
    """Persist step results while this provision lease still owns the slug."""
    with locked_state() as state:
        claimed = state.worktrees.get(worktree)
        if claimed is None or claimed.key != key:
            return
        lease = state.leases.get(key)
        if (
            lease is None
            or lease.lease_id != lease_id
            or lease.op != "provision"
        ):
            return
        claimed.steps = list(steps)


def _forget_set(
    state: PluginState, key: str, extra: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Forget-set: the unique claim path for key, plus remint extras."""
    claim = path_for_key(state, key)
    return tuple(dict.fromkeys(p for p in (claim, *extra) if p))


def _mint_lease(
    state: PluginState,
    key: str,
    *,
    op: LeaseOp,
    worktrees: tuple[str, ...],
    object_name: str,
    touch_postgres: bool,
) -> SlugLease:
    lease_id = state.next_lease_id
    state.next_lease_id = lease_id + 1
    forget = _forget_set(state, key, worktrees) if op == "drop" else worktrees
    lease = SlugLease(
        lease_id=lease_id,
        key=key,
        op=op,
        worktrees=forget,
        object_name=object_name,
        touch_postgres=touch_postgres,
        reserved_at=_now_iso(),
    )
    state.leases[key] = lease
    return lease


def _reserve(
    state: PluginState,
    key: str,
    *,
    worktrees: tuple[str, ...],
    object_name: str,
    touch_postgres: bool,
    steal: bool = False,
) -> SlugLease | None:
    """Exclusive drop-slug reservation. Returns None if busy (unless steal).

    ``steal`` only clears a stuck *drop* lease. In-flight provision leases
    stay exclusive until TTL expiry or ``gc --reclaim-leases``.
    """
    if key in state.leases:
        existing = state.leases[key]
        if not steal or existing.op != "drop":
            return None
        stolen = state.leases.pop(key, None)
        if stolen:
            log(f"stole drop lease for {key!r}")
    return _mint_lease(
        state,
        key,
        op="drop",
        worktrees=worktrees,
        object_name=object_name,
        touch_postgres=touch_postgres,
    )


def _drop_target(
    state: PluginState,
    cfg: PluginConfig,
    worktree: str | None,
    requested: str | None,
) -> DropOp:
    """Resolve drop identity: by --key, by worktree row, or remint recovery."""
    wt = os.path.realpath(worktree) if worktree else None

    if requested:
        key = normalize_key(requested)
        if not key:
            raise SystemExit(f"invalid --key: {requested!r}")
        path = path_for_key(state, key)
        rec = state.worktrees[path] if path else None
        obj, touch = postgres_target(rec, key)
        return DropOp(
            key=key,
            object_name=obj,
            touch_postgres=touch,
            paths=(wt,) if wt else (),
        )

    if wt:
        record = state.worktrees.get(wt)
        if record:
            obj, touch = postgres_target(record, record.key)
            return DropOp(
                key=record.key,
                object_name=obj,
                touch_postgres=touch,
                paths=(wt,),
            )
        repo = repo_config(cfg, wt)
        if repo:
            key = mint_key(wt, repo)
            obj, touch = postgres_target(None, key)
            return DropOp(
                key=key,
                object_name=obj,
                touch_postgres=touch,
                paths=(wt,),
            )
        raise SystemExit(
            f"no state row for {wt}; pass --key "
            "(or ensure repo config matches so the slug can be reminted)"
        )

    raise SystemExit("drop needs --worktree or --key")


def _resolve_and_reserve(
    state: PluginState,
    cfg: PluginConfig,
    worktree: str | None,
    requested: str | None,
    *,
    touch_postgres: bool | None = None,
    steal: bool = False,
) -> SlugLease:
    """Resolve once, then reserve (single busy-exit)."""
    t = _drop_target(state, cfg, worktree, requested)
    touch = t.touch_postgres if touch_postgres is None else touch_postgres
    lease = _reserve(
        state,
        t.key,
        worktrees=t.paths,
        object_name=t.object_name,
        touch_postgres=touch,
        steal=steal,
    )
    if lease is None:
        existing = state.leases.get(t.key)
        if existing:
            raise SystemExit(_busy_msg(t.key, existing))
        raise SystemExit(f"key {t.key!r}: drop in progress")
    return lease


def begin_drop(
    cfg: PluginConfig,
    worktree: str | None = None,
    *,
    requested: str | None = None,
    force: bool = False,
    forget_only: bool = False,
) -> SlugLease:
    """Reserve the slug under lock before slow Postgres drop (exclusive).

    ``force`` steals a stuck *drop* lease for the target slug (after TTL
    reclaim), so remint recovery works without guessing keys up front.
    Provision leases are not stolen — use TTL or ``gc --reclaim-leases``.
    ``forget_only`` encodes ``touch_postgres=False`` on the lease.
    """
    with locked_state() as state:
        for key in reclaim_expired_leases(state):
            log(f"reclaimed expired lease for {key!r}")
        touch_override = False if forget_only else None
        return _resolve_and_reserve(
            state,
            cfg,
            worktree,
            requested,
            touch_postgres=touch_override,
            steal=force,
        )


def reserve_from_plan(state: PluginState, plan: DropOp) -> SlugLease | None:
    """Reserve under an already-held lock (GC). None if slug busy / gone.

    Planner owns ``object_name`` and ``touch_postgres``; lease copies both.
    """
    if plan.state_path and plan.state_path not in state.worktrees:
        return None
    if not plan.key:
        return None
    return _reserve(
        state,
        plan.key,
        worktrees=plan.paths,
        object_name=plan.object_name,
        touch_postgres=plan.touch_postgres,
    )


def _clear_lease(
    state: PluginState, lease: SlugLease, *, restore: bool
) -> None:
    reserved = state.leases.get(lease.key)
    if reserved is None or reserved.lease_id != lease.lease_id:
        return
    if not restore and lease.op == "drop":
        for path in reserved.worktrees:
            state.worktrees.pop(path, None)
    state.leases.pop(lease.key, None)


def finish_drop(lease: SlugLease) -> None:
    """Pop the claim only if this drop lease still owns the slug."""
    with locked_state() as state:
        _clear_lease(state, lease, restore=False)


def abort_drop(lease: SlugLease) -> None:
    """Clear reservation after a failed Postgres drop (keep claim)."""
    with locked_state() as state:
        _clear_lease(state, lease, restore=True)

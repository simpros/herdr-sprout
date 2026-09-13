"""Exclusive slug-lease lifecycles (provision session + drop leases).

``state.py`` owns persistence; this module owns every lease mutation:

- provision: :func:`claim_provision` / :class:`ProvisionLease` is the only
  entrypoint (claim → finalize → steps, always released on exit);
- drop: resolve / reserve / finish / abort for CLI drop and GC.

One key maps to at most one lease; the lease-id fence is checked once per
mutation (``_provision_lease_valid`` / ``_clear_lease``).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

from sprout_worktree_db.gitutil import repo_config
from sprout_worktree_db.keys import (
    mint_key,
    normalize_key,
    postgres_target,
    resolve_key,
)
from sprout_worktree_db.models import (
    DropOp,
    LeaseOp,
    Mode,
    PluginConfig,
    PluginState,
    RepoConfig,
    SlugLease,
    StepRecord,
    WorktreeRecord,
)
from sprout_worktree_db.paths import log
from sprout_worktree_db.state import (
    locked_state,
    path_for_key,
    reclaim_expired_leases,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _busy_msg(key: str, lease: SlugLease) -> str:
    return f"key {key!r}: {lease.op} in progress"


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


def _claim_key(
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
    existing row is left untouched until finalization writes mode/object.
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


def _provision_lease_valid(
    state: PluginState, key: str, lease_id: int
) -> bool:
    """Single lease-id fence for provision leases (one check, all callers)."""
    lease = state.leases.get(key)
    return (
        lease is not None
        and lease.lease_id == lease_id
        and lease.op == "provision"
    )


def _finalize_claim(
    worktree: str, key: str, lease_id: int, record: WorktreeRecord
) -> None:
    """Compare-and-swap provision result; keep the provision lease.

    The lease stays until release so concurrent drop/GC cannot tear
    down the object between finalize and deferred env merge / steps.
    Raises on a lost lease: env must never merge against a stale claim.
    """
    with locked_state() as state:
        reclaim_expired_leases(state)
        claimed = state.worktrees.get(worktree)
        if claimed is None:
            raise RuntimeError(
                f"key claim lost for {worktree}: row removed during provision "
                f"(provisioned {key!r})"
            )
        if not _provision_lease_valid(state, key, lease_id):
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
    """Lease-id-fenced pop of a provision lease (no-op when fenced out).

    Single canonical pop: failure and success both clear only the lease and
    keep the claim row — the row is CAS-written by finalization, never by
    the lease pop.
    """
    with locked_state() as state:
        if not _provision_lease_valid(state, key, lease_id):
            return
        state.leases.pop(key, None)


@dataclass
class ProvisionLease:
    """Session handle for one provision lease — the only provision API::

        with claim_provision(worktree, repo, mode=...) as lease:
            injection = provision_or_attach(...)
            lease.finalize(record)
            lease.record_steps(steps)

    ``__exit__`` always clears the provision lease; the claim row is kept
    either way.
    """

    worktree: str
    key: str
    lease_id: int

    def finalize(self, record: WorktreeRecord) -> None:
        _finalize_claim(self.worktree, self.key, self.lease_id, record)

    def record_steps(self, steps: list[StepRecord]) -> None:
        _update_claim_steps(self.worktree, self.key, self.lease_id, steps)

    def release(self) -> None:
        _pop_provision_lease(self.key, self.lease_id)


@contextmanager
def claim_provision(
    worktree: str,
    repo: RepoConfig,
    *,
    mode: Mode,
    requested: str | None = None,
) -> Iterator[ProvisionLease]:
    """Claim a provision lease and yield a session handle (always released)."""
    key, lease_id = _claim_key(worktree, repo, mode=mode, requested=requested)
    try:
        yield ProvisionLease(worktree=worktree, key=key, lease_id=lease_id)
    finally:
        _pop_provision_lease(key, lease_id)


def _update_claim_steps(
    worktree: str, key: str, lease_id: int, steps: list[StepRecord]
) -> None:
    """Persist step results while this provision lease still owns the slug.

    Best-effort by design (unlike finalization which raises): steps run
    after disk truth is already CAS-committed, so a fenced-out steps write is
    a silent no-op rather than a provision failure.
    """
    with locked_state() as state:
        claimed = state.worktrees.get(worktree)
        if claimed is None or claimed.key != key:
            return
        if not _provision_lease_valid(state, key, lease_id):
            return
        claimed.steps = list(steps)


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
    *,
    forget_only: bool = False,
) -> DropOp:
    """Resolve drop identity: by --key, by worktree row, or remint recovery.

    ``forget_only`` is encoded here, once, as ``touch_postgres=False`` — the
    op owns the flag from creation; no caller overrides it afterwards.
    """
    wt = os.path.realpath(worktree) if worktree else None

    def _bake(key: str, obj: str, touch: bool, paths: tuple[str, ...]) -> DropOp:
        return DropOp(
            key=key,
            object_name=obj,
            touch_postgres=False if forget_only else touch,
            paths=paths,
        )

    if requested:
        key = normalize_key(requested)
        if not key:
            raise SystemExit(f"invalid --key: {requested!r}")
        path = path_for_key(state, key)
        rec = state.worktrees[path] if path else None
        obj, touch = postgres_target(rec, key)
        return _bake(key, obj, touch, (wt,) if wt else ())

    if wt:
        record = state.worktrees.get(wt)
        if record:
            obj, touch = postgres_target(record, record.key)
            return _bake(record.key, obj, touch, (wt,))
        repo = repo_config(cfg, wt)
        if repo:
            key = mint_key(wt, repo)
            obj, touch = postgres_target(None, key)
            return _bake(key, obj, touch, (wt,))
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
    forget_only: bool = False,
    steal: bool = False,
) -> SlugLease:
    """Resolve once, then reserve (single busy-exit).

    ``touch_postgres`` comes from the resolved :class:`DropOp` only — there
    is no override side channel.
    """
    t = _drop_target(
        state, cfg, worktree, requested, forget_only=forget_only
    )
    lease = _reserve(
        state,
        t.key,
        worktrees=t.paths,
        object_name=t.object_name,
        touch_postgres=t.touch_postgres,
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
        return _resolve_and_reserve(
            state,
            cfg,
            worktree,
            requested,
            forget_only=forget_only,
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

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

from sprout_worktree_db.errors import BusyError, ConfigError, SproutError
from sprout_worktree_db.keys import (
    normalize_key,
    postgres_target,
    resolve_key,
)
from sprout_worktree_db.models import (
    DropOp,
    LeaseOp,
    Mode,
    PluginState,
    RepoConfig,
    SlugLease,
    StepResult,
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


def _mint_lease(
    state: PluginState,
    key: str,
    *,
    op: LeaseOp,
    worktrees: tuple[str, ...],
    object_name: str = "",
    touch_postgres: bool = False,
) -> SlugLease:
    """Single lease constructor — one id sequence, no op switchboard.

    ``worktrees`` is authoritative: each resolve branch emits the exact
    forget-set (no hint + expand). Provision passes the in-flight path;
    ``--key`` passes ``(path_for_key(key),)`` or ``()``; row passes
    ``(wt,)``; remint passes ``(wt,)`` as a busy-fence only.
    """
    lease_id = state.next_lease_id
    state.next_lease_id = lease_id + 1
    lease = SlugLease(
        lease_id=lease_id,
        key=key,
        op=op,
        worktrees=worktrees,
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

    Lease-only claim: no ``worktrees`` row is written here. The lease's
    ``worktrees=(worktree,)`` is the in-flight reservation; :func:`_finalize_claim`
    inserts the real row. After TTL reclaim a leftover ``sprout_wt_*`` is a
    normal pathless postgres orphan — GC already plans those, so there is no
    ``object=""`` phase sentinel and no in-process orphan cleanup.

    Returns ``(key, lease_id)``. Re-provision reuses the finalized row's key
    so passwords/objects stay stable. One key maps to at most one worktree
    path. In-place mode changes are refused — drop first so dedicated
    ``sprout_wt_*`` teardown stays on the drop/GC path.
    """
    with locked_state() as state:
        reclaim_expired_leases(state)
        for res in state.leases.values():
            if worktree in res.worktrees:
                raise BusyError(_busy_msg(res.key, res))
        previous = state.worktrees.get(worktree)
        if previous and previous.key in state.leases:
            raise BusyError(_busy_msg(previous.key, state.leases[previous.key]))
        if previous is not None and previous.mode != mode:
            raise BusyError(
                f"{worktree} is {previous.mode}; "
                f"drop first, then re-run for {mode}"
            )
        key = resolve_key(state, worktree, repo, requested)
        if key in state.leases:
            raise BusyError(_busy_msg(key, state.leases[key]))
        holder = path_for_key(state, key)
        if holder is not None and holder != worktree:
            raise BusyError(
                f"key {key!r} already claimed by {holder}; "
                "drop that worktree first"
            )
        lease = _mint_lease(
            state,
            key,
            op="provision",
            worktrees=(worktree,),
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

    Lease-only claim wrote no row, so this inserts the real claim row.
    The lease stays until release so concurrent drop/GC cannot tear
    down the object between finalize and deferred env merge / steps.
    Raises on a lost lease: env must never merge against a stale claim.
    """
    with locked_state() as state:
        reclaim_expired_leases(state)
        if not _provision_lease_valid(state, key, lease_id):
            raise SproutError(
                f"key claim lost for {worktree}: provision lease gone "
                f"(provisioned {key!r})"
            )
        if record.key != key:
            raise SproutError(
                f"key claim lost for {worktree}: held {key!r}, "
                f"provisioned {record.key!r}"
            )
        existing = state.worktrees.get(worktree)
        if existing is not None and existing.key != key:
            raise SproutError(
                f"key claim lost for {worktree}: held {existing.key!r}, "
                f"provisioned {key!r}"
            )
        holder = path_for_key(state, key)
        if holder is not None and holder != worktree:
            raise SproutError(
                f"key {key!r} already claimed by {holder}; "
                "drop that worktree first"
            )
        if existing is not None and existing.created_at:
            record.created_at = existing.created_at
        state.worktrees[worktree] = record


def _pop_provision_lease(key: str, lease_id: int) -> None:
    """Lease-id-fenced pop of a provision lease (no-op when fenced out).

    Single canonical pop: failure and success both clear only the lease.
    The claim row is written by finalization only — a run that never
    finalized leaves no row, so a leftover ``sprout_wt_*`` is a normal
    pathless postgres orphan for GC (never a placeholder row).
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

    ``__exit__`` always clears the provision lease. The claim row is
    inserted by ``finalize`` only — a run that never finalized leaves no
    row behind.
    """

    worktree: str
    key: str
    lease_id: int

    def finalize(self, record: WorktreeRecord) -> None:
        _finalize_claim(self.worktree, self.key, self.lease_id, record)

    def record_steps(self, steps: list[StepResult]) -> None:
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
    key, lease_id = _claim_key(
        worktree, repo, mode=mode, requested=requested
    )
    try:
        yield ProvisionLease(
            worktree=worktree,
            key=key,
            lease_id=lease_id,
        )
    finally:
        _pop_provision_lease(key, lease_id)


def _update_claim_steps(
    worktree: str, key: str, lease_id: int, steps: list[StepResult]
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

    ``worktrees`` is stored verbatim — the authoritative forget-set from
    the resolver. No claim ∪ extras merge: callers emit the exact set.

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
    *,
    worktree: str | None,
    requested_key: str | None,
    remint_key: str | None,
    forget_only: bool = False,
) -> DropOp:
    """Resolve drop identity: by --key, by worktree row, or remint recovery.

    Pure state lookup — no I/O. Remint inputs (``remint_key``) are resolved
    by the caller *before* taking the lock (see ``drop.remint_key_for``).
    ``forget_only`` is encoded here, once, as ``touch_postgres=False`` —
    the op owns the flag from creation; no caller overrides it afterwards.

    ``DropOp.paths`` is authoritative (no second expansion in ``_reserve``):

    - ``--key``: ``(path_for_key(key),)`` when claimed, else ``()``.
      Passing both flags requires agreement (fail closed) so a
      key-resolved drop can never pop a foreign claim via ``finish_drop``.
    - row: ``(wt,)`` — the claimed path itself.
    - remint: ``(wt,)`` busy-fence only; the slug must be unclaimed or
      this raises (same ownership rule as provision claim).
    """
    wt = worktree

    def _bake(key: str, obj: str, touch: bool, paths: tuple[str, ...]) -> DropOp:
        return DropOp(
            key=key,
            object_name=obj,
            touch_postgres=False if forget_only else touch,
            paths=paths,
        )

    if requested_key is not None:
        path = path_for_key(state, requested_key)
        rec = state.worktrees[path] if path else None
        if wt is not None and path != wt:
            raise ConfigError(
                f"drop --key {requested_key!r} does not own {wt} "
                f"(owns {path!r}); pass only one of --key / --worktree"
            )
        obj, touch = postgres_target(rec, requested_key)
        return _bake(requested_key, obj, touch, (path,) if path else ())

    if wt is not None:
        record = state.worktrees.get(wt)
        if record:
            obj, touch = postgres_target(record, record.key)
            return _bake(record.key, obj, touch, (wt,))
        for res in state.leases.values():
            if wt in res.worktrees:
                raise BusyError(_busy_msg(res.key, res))
        if remint_key is not None:
            holder = path_for_key(state, remint_key)
            if holder is not None:
                raise BusyError(
                    f"key {remint_key!r} already claimed by {holder}; "
                    "drop that worktree first"
                )
            obj, touch = postgres_target(None, remint_key)
            return _bake(remint_key, obj, touch, (wt,))
        raise ConfigError(
            f"no state row for {wt}; pass --key "
            "(or ensure repo config matches so the slug can be reminted)"
        )

    raise ConfigError("drop needs --worktree or --key")


def _resolve_and_reserve(
    state: PluginState,
    *,
    worktree: str | None,
    requested_key: str | None,
    remint_key: str | None,
    forget_only: bool = False,
    steal: bool = False,
) -> SlugLease:
    """Resolve once, then reserve (single busy-exit).

    ``touch_postgres`` comes from the resolved :class:`DropOp` only — there
    is no override side channel.
    """
    t = _drop_target(
        state,
        worktree=worktree,
        requested_key=requested_key,
        remint_key=remint_key,
        forget_only=forget_only,
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
        # _reserve only refuses while a lease holds the slug, so the row
        # must still be there — no second fallback raise.
        raise BusyError(_busy_msg(t.key, state.leases[t.key]))
    return lease


def begin_drop(
    worktree: str | None = None,
    *,
    requested: str | None = None,
    force: bool = False,
    forget_only: bool = False,
    remint_key: str | None = None,
) -> SlugLease:
    """Reserve the slug under lock before slow Postgres drop (exclusive).

    ``force`` steals a stuck *drop* lease for the target slug (after TTL
    reclaim), so remint recovery works without guessing keys up front.
    Provision leases are not stolen — use TTL or ``gc --reclaim-leases``.
    ``forget_only`` encodes ``touch_postgres=False`` on the lease.

    Pure mutation layer: remint inputs arrive via ``remint_key`` (resolved
    by ``drop.remint_key_for`` outside the flock — ``repo_config`` shells
    out to ``git worktree list``, which must never run while holding the
    exclusive state flock). ``_drop_target`` under the lock is then a pure
    state lookup; a row created concurrently still wins because the lock
    re-checks state.
    """
    wt = os.path.realpath(worktree) if worktree else None
    requested_key: str | None = None
    if requested is not None:
        requested_key = normalize_key(requested)
        if not requested_key:
            raise ConfigError(f"invalid --key: {requested!r}")
    if wt is None and requested_key is None:
        raise ConfigError("drop needs --worktree or --key")
    with locked_state() as state:
        for key in reclaim_expired_leases(state):
            log(f"reclaimed expired lease for {key!r}")
        return _resolve_and_reserve(
            state,
            worktree=wt,
            requested_key=requested_key,
            remint_key=remint_key,
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
    """Fenced clear of a *drop* lease.

    ``finish_drop`` (``restore=False``) also pops the claim; ``abort_drop``
    (``restore=True``) keeps it. No ``op`` branch: every lease cleared here
    is a drop lease by construction (provision leases pop via
    ``_pop_provision_lease``).
    """
    reserved = state.leases.get(lease.key)
    if reserved is None or reserved.lease_id != lease.lease_id:
        return
    if not restore:
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

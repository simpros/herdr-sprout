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

from sprout_worktree_db.errors import BusyError, ConfigError
from sprout_worktree_db.gitutil import repo_config
from sprout_worktree_db.keys import (
    mint_key,
    normalize_key,
    postgres_target,
    resolve_key,
)
from sprout_worktree_db.models import (
    DropOp,
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


def _mint_provision_lease(
    state: PluginState,
    key: str,
    *,
    worktrees: tuple[str, ...],
) -> SlugLease:
    """Mint a provision lease (no forget-set: the claim row is the set)."""
    lease_id = state.next_lease_id
    state.next_lease_id = lease_id + 1
    lease = SlugLease(
        lease_id=lease_id,
        key=key,
        op="provision",
        worktrees=worktrees,
        object_name="",
        touch_postgres=False,
        reserved_at=_now_iso(),
    )
    state.leases[key] = lease
    return lease


def _mint_drop_lease(
    state: PluginState,
    key: str,
    *,
    worktrees: tuple[str, ...],
    object_name: str,
    touch_postgres: bool,
) -> SlugLease:
    """Mint a drop lease (drop-only forget-set: claim path + remint extras)."""
    lease_id = state.next_lease_id
    state.next_lease_id = lease_id + 1
    lease = SlugLease(
        lease_id=lease_id,
        key=key,
        op="drop",
        worktrees=_forget_set(state, key, worktrees),
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
) -> tuple[str, int, str]:
    """Atomically resolve + persist a provision lease before slow sprout work.

    Returns ``(key, lease_id, prior_object)``. Re-provision reuses the stored
    key so passwords/objects stay stable. One key maps to at most one
    worktree path. ``prior_object`` is the pre-claim object (``""`` for a
    fresh claim) so provision can best-effort clean an orphaned dedicated
    DB only when this run created it — never a live DB that predates us.

    Claim reserves the key under an exclusive provision lease. On re-claim the
    existing row is left untouched until finalization writes mode/object.
    In-place mode changes are refused — drop first so dedicated ``sprout_wt_*``
    teardown stays on the drop/GC path (no dual-object-under-one-key).
    """
    with locked_state() as state:
        reclaim_expired_leases(state)
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
        holder = next(
            (
                p
                for p, r in state.worktrees.items()
                if r.key == key and p != worktree
            ),
            None,
        )
        if holder:
            raise BusyError(
                f"key {key!r} already claimed by {holder}; "
                "drop that worktree first"
            )
        prior_object = previous.object if previous is not None else ""
        if previous is None:
            state.worktrees[worktree] = WorktreeRecord(
                key=key,
                repo=repo.name,
                mode=mode,
                object="",
                created_at=_now_iso(),
            )
        lease = _mint_provision_lease(
            state,
            key,
            worktrees=(worktree,),
        )
        return key, lease.lease_id, prior_object


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
    either way. ``prior_object`` is the object that predated this claim
    (``""`` when fresh) — provision uses it to scope orphan cleanup.
    """

    worktree: str
    key: str
    lease_id: int
    prior_object: str = ""

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
    key, lease_id, prior_object = _claim_key(
        worktree, repo, mode=mode, requested=requested
    )
    try:
        yield ProvisionLease(
            worktree=worktree,
            key=key,
            lease_id=lease_id,
            prior_object=prior_object,
        )
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
    return _mint_drop_lease(
        state,
        key,
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
    by the caller *before* taking the lock because ``repo_config`` runs
    ``git worktree list``. ``forget_only`` is encoded here, once, as
    ``touch_postgres=False`` — the op owns the flag from creation; no
    caller overrides it afterwards.
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
        obj, touch = postgres_target(rec, requested_key)
        return _bake(requested_key, obj, touch, (wt,) if wt else ())

    if wt is not None:
        record = state.worktrees.get(wt)
        if record:
            obj, touch = postgres_target(record, record.key)
            return _bake(record.key, obj, touch, (wt,))
        if remint_key is not None:
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

    Remint inputs are resolved *before* the lock: ``repo_config`` shells
    out to ``git worktree list``, which must never run while holding the
    exclusive state flock (a hung git would stall every other writer).
    ``_drop_target`` under the lock is then a pure state lookup; a row
    created concurrently still wins because the lock re-checks state.
    """
    wt = os.path.realpath(worktree) if worktree else None
    requested_key: str | None = None
    if requested is not None:
        requested_key = normalize_key(requested)
        if not requested_key:
            raise ConfigError(f"invalid --key: {requested!r}")
    if wt is None and requested_key is None:
        raise ConfigError("drop needs --worktree or --key")
    remint_key: str | None = None
    if wt is not None and requested_key is None:
        repo = repo_config(cfg, wt)
        if repo is not None:
            remint_key = mint_key(wt, repo)
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

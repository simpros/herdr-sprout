"""Drop orchestration (CLI drop + GC share this executor)."""

from __future__ import annotations

import json
import os

from sprout_worktree_db.models import DropRequest, PluginConfig, Secrets, SlugLease
from sprout_worktree_db.gitutil import repo_config
from sprout_worktree_db.keys import mint_key
from sprout_worktree_db.paths import log
from sprout_worktree_db.sprout import drop_key
from sprout_worktree_db.state import load_state
from sprout_worktree_db.leases import abort_drop, begin_drop, finish_drop


def execute_drop_lease(
    cfg: PluginConfig,
    secrets: Secrets,
    lease: SlugLease,
    *,
    reraise: bool,
) -> bool:
    """One lease lifecycle: skip / drop_key → finish, or abort on failure.

    ``lease.touch_postgres`` is authoritative (forget-only baked in at begin).
    Returns True when Postgres drop ran successfully.
    """
    if not lease.touch_postgres:
        finish_drop(lease)
        return False
    try:
        drop_key(cfg, secrets, lease.key)
        finish_drop(lease)
        return True
    except Exception as exc:
        # Broad on purpose: PluginError subclasses Exception (never
        # SystemExit) so every expected sprout failure lands here and
        # aborts the lease instead of leaving it stuck.
        abort_drop(lease)
        if reraise:
            raise
        log(f"drop failed for {lease.key}: {exc}")
        return False


def remint_key_for(cfg: PluginConfig, wt: str | None) -> str | None:
    """Recovery-only remint resolved outside the state flock.

    ``repo_config`` shells out to ``git worktree list`` — a hung git must
    never stall every other provision/drop/gc writer. Fast path: a tracked
    row needs no remint, so no git runs at all (lock-free read only; the
    lock in :func:`leases.begin_drop` re-checks state, so a concurrently
    created row still wins and the unused remint is ignored).
    """
    if wt is None:
        return None
    if load_state().worktrees.get(wt) is not None:
        return None
    repo = repo_config(cfg, wt)
    if repo is None:
        return None
    return mint_key(wt, repo)


def do_drop(cfg: PluginConfig, secrets: Secrets, req: DropRequest) -> dict:
    """Begin drop lease → Postgres drop → finish (inverse of claim)."""
    worktree = os.path.realpath(req.worktree) if req.worktree else None
    remint_key = (
        remint_key_for(cfg, worktree)
        if (worktree is not None and not req.key)
        else None
    )
    lease = begin_drop(
        worktree,
        requested=req.key,
        force=req.force,
        forget_only=req.forget_only,
        remint_key=remint_key,
    )

    if not lease.touch_postgres:
        reason = (
            "--forget-only"
            if req.forget_only
            else "leaving shared/preview DB intact"
        )
        log(f"forgetting claim for {lease.key}; {reason}")
    dropped = execute_drop_lease(cfg, secrets, lease, reraise=True)
    if dropped:
        log(f"dropped {lease.object_name or lease.key}")
    print(
        json.dumps(
            {
                "ok": True,
                "key": lease.key,
                "dropped": dropped,
                "forgot_only": not lease.touch_postgres,
            },
            indent=2,
        )
    )
    return {"key": lease.key}

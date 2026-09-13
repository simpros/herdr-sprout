"""Unified orphan planning and GC."""

from __future__ import annotations

import json
import os
import shutil

from sprout_worktree_db.gitutil import git_worktree_paths, run
from sprout_worktree_db.models import DropOp, PluginConfig, PluginState, SlugLease
from sprout_worktree_db.paths import log, require_admin_url
from sprout_worktree_db.provision import execute_drop_lease
from sprout_worktree_db.state import (
    expired_lease_keys,
    key_from_object,
    load_state,
    locked_state,
    path_for_key,
    postgres_target,
    reclaim_expired_leases,
    reserve_from_plan,
)


def live_worktree_paths_for_config(cfg: PluginConfig) -> set[str]:
    """Union of `git worktree list` paths across configured repos."""
    live: set[str] = set()
    for repo in cfg.repos:
        main = os.path.expanduser(repo.main_repo)
        live |= git_worktree_paths(main)
    return live


def live_objects_from_state(
    state: PluginState, live_paths: set[str]
) -> set[str]:
    """Object names still claimed by an existing worktree path in state.

    State is the only source of truth for object names — never guess from
    basename alone. Active drop leases that will touch Postgres also count
    as live so concurrent GC does not double-plan them. Forget-only /
    preview / provision leases hold the slug only.
    """
    live: set[str] = set()
    for path, rec in state.worktrees.items():
        real = os.path.realpath(path) if path else ""
        if not (os.path.exists(path) or real in live_paths):
            continue
        if rec.key in state.leases:
            continue
        obj, touch = postgres_target(rec, rec.key)
        if touch and obj:
            live.add(obj)
    for key, res in state.leases.items():
        if res.op != "drop" or not res.touch_postgres:
            continue
        if res.object_name:
            live.add(res.object_name)
    return live


def plan_orphans(
    state: PluginState,
    live_paths: set[str],
    postgres_names: list[str] | None = None,
) -> list[DropOp]:
    """Pure planning: state rows / postgres names with no live worktree.

    Rows / keys already under a lease are skipped (exclusive lease).
    One key → one path is load-enforced; no sibling dialect.
    """
    plans: list[DropOp] = []
    seen_objects: set[str] = set()

    for path, rec in list(state.worktrees.items()):
        if rec.key in state.leases:
            continue
        real = os.path.realpath(path) if path else ""
        path_gone = not os.path.exists(path) and real not in live_paths
        if not path_gone:
            continue
        obj, touch = postgres_target(rec, rec.key)
        reason = (
            "stale preview state"
            if not touch
            else f"worktree gone ({path})"
        )
        plans.append(
            DropOp(
                key=rec.key,
                object_name=obj,
                reason=reason,
                paths=(path,),
                touch_postgres=touch,
            )
        )
        if touch and obj:
            seen_objects.add(obj)

    live_objects = live_objects_from_state(state, live_paths)
    # Objects we already planned to drop should not count as live.
    live_objects -= seen_objects

    if postgres_names is not None:
        for obj in postgres_names:
            if not obj.startswith("sprout_wt_"):
                continue
            if obj in live_objects or obj in seen_objects:
                continue
            key = key_from_object(obj)
            if not key:
                continue
            if key in state.leases:
                continue
            # A live claim owns the slug — never lease it as a pathless
            # postgres orphan (that would finish_drop the claim). Leftover
            # dedicated DBs after a mode change are operator/drop territory.
            if path_for_key(state, key) is not None:
                continue
            plans.append(
                DropOp(
                    key=key,
                    object_name=obj,
                    reason="postgres orphan (no state / live worktree)",
                    paths=(),
                    touch_postgres=True,
                )
            )
            seen_objects.add(obj)

    return plans


def list_postgres_worktree_dbs(secrets: dict[str, str]) -> list[str] | None:
    """List sprout_wt_* databases via psql if available."""
    admin_url = secrets.get("SPROUT_WORKTREE_ADMIN_URL", "").strip()
    if not admin_url:
        return None
    psql = shutil.which("psql")
    if not psql:
        return None
    rc, out, err = run(
        [
            psql,
            admin_url,
            "-v",
            "ON_ERROR_STOP=1",
            "-tAc",
            r"select datname from pg_database where datname like 'sprout_wt\_%'",
        ],
        timeout=60,
    )
    if rc != 0:
        log(f"gc: psql list failed: {err or out}")
        return None
    return [line.strip() for line in out.splitlines() if line.strip()]


def reserve_orphan_leases(
    live_paths: set[str],
    postgres_names: list[str] | None,
    *,
    reclaim_leases: bool = False,
) -> tuple[list[DropOp], list[tuple[DropOp, SlugLease]]]:
    """Under one lock: reclaim leases, plan orphans, reserve."""
    with locked_state() as state:
        if reclaim_leases:
            for key in reclaim_expired_leases(state, force_all=True):
                log(f"gc --reclaim-leases: cleared {key!r}")
        else:
            for key in reclaim_expired_leases(state):
                log(f"gc: reclaimed expired lease for {key!r}")
        plans = plan_orphans(state, live_paths, postgres_names)
        reserved: list[tuple[DropOp, SlugLease]] = []
        for plan in plans:
            lease = reserve_from_plan(state, plan)
            if lease is None:
                log(
                    f"gc: skip {plan.object_name or plan.key}; "
                    "drop already in progress or claim gone"
                )
                continue
            reserved.append((plan, lease))
        return plans, reserved


def apply_drop_leases(
    cfg: PluginConfig,
    secrets: dict,
    reserved: list[tuple[DropOp, SlugLease]],
) -> list[str]:
    """Execute reserved drop leases; return object names that were dropped."""
    dropped: list[str] = []
    for plan, lease in reserved:
        log(f"gc: {plan.reason} -> {lease.object_name or lease.key}")
        ok = execute_drop_lease(cfg, secrets, lease, reraise=False)
        if ok:
            log(f"gc: dropped {lease.object_name or lease.key}")
            if lease.object_name:
                dropped.append(lease.object_name)
    return dropped


def gc(
    cfg: PluginConfig,
    secrets: dict,
    dry_run: bool = False,
    *,
    reclaim_leases: bool = False,
) -> int:
    """Drop sprout_wt_* objects with no live worktree behind them."""
    require_admin_url(secrets)
    live_paths = live_worktree_paths_for_config(cfg)
    postgres = list_postgres_worktree_dbs(secrets)

    if dry_run:
        st = load_state()
        for key in expired_lease_keys(st, all_leases=reclaim_leases):
            log(f"gc: would reclaim drop lease {key!r}")
        plans = plan_orphans(st, live_paths, postgres)
        for plan in plans:
            log(f"gc: {plan.reason} -> {plan.object_name or plan.key}")
        dropped: list[str] = []
    else:
        plans, reserved = reserve_orphan_leases(
            live_paths, postgres, reclaim_leases=reclaim_leases
        )
        dropped = apply_drop_leases(cfg, secrets, reserved)

    if postgres is None:
        log(
            "gc: skipping postgres scan (install psql on PATH for orphan scan); "
            f"plans={len(plans)}"
        )

    live_objects = sorted(live_objects_from_state(load_state(), live_paths))
    orphans = [
        p.object_name
        for p in plans
        if p.touch_postgres and p.object_name
    ]
    print(
        json.dumps(
            {
                "ok": True,
                "dry_run": dry_run,
                "postgres_scan": postgres is not None,
                "live_objects": live_objects,
                "orphans": orphans,
                "dropped": [] if dry_run else dropped,
            },
            indent=2,
        )
    )
    return 0

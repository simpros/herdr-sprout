"""Unified orphan planning and GC."""

from __future__ import annotations

import json
import os
import shutil

from sprout_worktree_db.gitutil import git_worktree_paths, run
from sprout_worktree_db.models import DropLease, DropPlan, PluginConfig, PluginState
from sprout_worktree_db.paths import log, require_admin_url
from sprout_worktree_db.state import (
    abort_drop,
    expired_lease_keys,
    finish_drop,
    key_from_object,
    key_has_live_sibling,
    load_state,
    locked_state,
    object_name,
    prune_gone_sibling_rows,
    reclaim_expired_leases,
    reserve_from_plan,
)
from sprout_worktree_db.sprout import drop_key


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
    basename alone. Slugs under an active drop lease also count as live so
    concurrent GC does not double-plan them.
    """
    live: set[str] = set()
    for path, rec in state.worktrees.items():
        real = os.path.realpath(path) if path else ""
        if not (os.path.exists(path) or real in live_paths):
            continue
        if rec.key in state.dropping:
            continue
        if rec.object and str(rec.object).startswith("sprout_wt_"):
            live.add(str(rec.object))
        elif rec.key and rec.mode != "preview":
            live.add(object_name(rec.key))
    for key, res in state.dropping.items():
        if res.object_name and res.object_name.startswith("sprout_wt_"):
            live.add(res.object_name)
        else:
            live.add(object_name(key))
    return live


def plan_orphans(
    state: PluginState,
    live_paths: set[str],
    postgres_names: list[str] | None = None,
) -> list[DropPlan]:
    """Pure planning: state rows / postgres names with no live worktree.

    Rows / keys already under a drop lease are skipped (exclusive lease).
    """
    plans: list[DropPlan] = []
    seen_objects: set[str] = set()

    for path, rec in list(state.worktrees.items()):
        if rec.key in state.dropping:
            continue
        real = os.path.realpath(path) if path else ""
        path_gone = not os.path.exists(path) and real not in live_paths
        if not path_gone:
            continue
        # Live sibling still owns the slug — prune row elsewhere; never drop DB.
        if rec.key and key_has_live_sibling(
            state, rec.key, except_path=path, live_paths=live_paths
        ):
            continue
        if rec.mode == "preview":
            plans.append(
                DropPlan(
                    key=rec.key,
                    object_name=rec.object,
                    reason="stale preview state",
                    state_path=path,
                    skip_drop=True,
                )
            )
            continue
        if not rec.key:
            plans.append(
                DropPlan(
                    key="",
                    object_name=rec.object,
                    reason="stale state missing key",
                    state_path=path,
                    skip_drop=True,
                )
            )
            continue
        obj = rec.object or object_name(rec.key)
        plans.append(
            DropPlan(
                key=rec.key,
                object_name=obj,
                reason=f"worktree gone ({path})",
                state_path=path,
            )
        )
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
            if key in state.dropping:
                continue
            plans.append(
                DropPlan(
                    key=key,
                    object_name=obj,
                    reason="postgres orphan (no state / live worktree)",
                    state_path=None,
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
) -> tuple[list[DropPlan], list[tuple[DropPlan, DropLease]]]:
    """Under one lock: reclaim expired leases, prune siblings, plan, reserve."""
    with locked_state() as state:
        for key in reclaim_expired_leases(state):
            log(f"gc: reclaimed expired drop lease for {key!r}")
        for path in prune_gone_sibling_rows(state, live_paths):
            log(f"gc: forgot gone sibling row {path} (live path still holds key)")
        plans = plan_orphans(state, live_paths, postgres_names)
        reserved: list[tuple[DropPlan, DropLease]] = []
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
    reserved: list[tuple[DropPlan, DropLease]],
) -> list[str]:
    """Execute reserved drop leases; return object names that were dropped."""
    dropped: list[str] = []
    for plan, lease in reserved:
        log(f"gc: {plan.reason} -> {plan.object_name or plan.key}")
        try:
            if plan.skip_drop or lease.skip_postgres:
                finish_drop(lease)
            else:
                try:
                    drop_key(cfg, secrets, lease.key)
                except Exception as exc:
                    abort_drop(lease)
                    log(f"gc: drop failed for {lease.key}: {exc}")
                    continue
                finish_drop(lease)
                log(
                    f"gc: dropped {plan.object_name or object_name(lease.key)}"
                )
                if plan.object_name:
                    dropped.append(plan.object_name)
        except Exception as exc:
            abort_drop(lease)
            log(f"gc: drop failed for {lease.key}: {exc}")
            continue
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

    if reclaim_leases and not dry_run:
        with locked_state() as state:
            # Force-clear every stuck reservation (TTL or not).
            cleared = reclaim_expired_leases(
                state, force_keys=set(state.dropping)
            )
            for key in cleared:
                log(f"gc --reclaim-leases: cleared {key!r}")

    if dry_run:
        st = load_state()
        for key in expired_lease_keys(st, all_leases=reclaim_leases):
            log(f"gc: would reclaim drop lease {key!r}")
        plans = plan_orphans(st, live_paths, postgres)
        for plan in plans:
            log(f"gc: {plan.reason} -> {plan.object_name or plan.key}")
        dropped: list[str] = []
    else:
        plans, reserved = reserve_orphan_leases(live_paths, postgres)
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
        if not p.skip_drop and p.object_name
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

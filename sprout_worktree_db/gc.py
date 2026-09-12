"""Unified orphan planning and GC."""

from __future__ import annotations

import json
import os
import shutil

from sprout_worktree_db.gitutil import git_worktree_paths, run
from sprout_worktree_db.models import DropPlan
from sprout_worktree_db.paths import log, require_admin_url
from sprout_worktree_db.state import key_from_object, load_state, object_name, save_state
from sprout_worktree_db.sprout import drop_key


def live_worktree_paths_for_config(cfg: dict) -> set[str]:
    """Union of `git worktree list` paths across configured repos."""
    live: set[str] = set()
    for repo in cfg.get("repos", []):
        main = os.path.expanduser(repo["main_repo"])
        live |= git_worktree_paths(main)
    return live


def live_objects_from_state(state: dict, live_paths: set[str]) -> set[str]:
    """Object names still claimed by an existing worktree path in state.

    State is the only source of truth for disambiguated keys — never guess
    from basename alone.
    """
    live: set[str] = set()
    for path, rec in state.get("worktrees", {}).items():
        real = os.path.realpath(path) if path else ""
        if not (os.path.exists(path) or real in live_paths):
            continue
        obj = rec.get("object")
        if obj and str(obj).startswith("sprout_wt_"):
            live.add(str(obj))
        elif rec.get("key") and rec.get("mode", "dedicated") != "preview":
            live.add(object_name(str(rec["key"])))
    return live


def plan_orphans(
    state: dict,
    live_paths: set[str],
    postgres_names: list[str] | None = None,
) -> list[DropPlan]:
    """Pure planning: state rows / postgres names with no live worktree."""
    plans: list[DropPlan] = []
    seen_objects: set[str] = set()

    for path, rec in list(state.get("worktrees", {}).items()):
        real = os.path.realpath(path) if path else ""
        path_gone = not os.path.exists(path) and real not in live_paths
        if not path_gone:
            continue
        if rec.get("mode") == "preview":
            plans.append(
                DropPlan(
                    key=str(rec.get("key") or ""),
                    object_name=str(rec.get("object") or ""),
                    reason="stale preview state",
                    state_path=path,
                    skip_drop=True,
                )
            )
            continue
        key = rec.get("key")
        if not key:
            plans.append(
                DropPlan(
                    key="",
                    object_name=str(rec.get("object") or ""),
                    reason="stale state missing key",
                    state_path=path,
                    skip_drop=True,
                )
            )
            continue
        obj = str(rec.get("object") or object_name(str(key)))
        plans.append(
            DropPlan(
                key=str(key),
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


def apply_drop_plans(
    cfg: dict,
    secrets: dict,
    plans: list[DropPlan],
    *,
    dry_run: bool = False,
) -> list[str]:
    """Execute drop plans; return object names that were (or would be) dropped."""
    dropped: list[str] = []
    state = load_state()
    for plan in plans:
        log(f"gc: {plan.reason} -> {plan.object_name or plan.key}")
        if dry_run:
            if not plan.skip_drop and plan.object_name:
                dropped.append(plan.object_name)
            continue
        if not plan.skip_drop and plan.key:
            try:
                drop_key(cfg, secrets, plan.key)
                log(f"gc: dropped {plan.object_name or object_name(plan.key)}")
                if plan.object_name:
                    dropped.append(plan.object_name)
            except Exception as exc:
                log(f"gc: drop failed for {plan.key}: {exc}")
                continue
        if plan.state_path:
            state["worktrees"].pop(plan.state_path, None)
    if not dry_run:
        # Also forget any state rows whose object was dropped as a postgres orphan.
        for path, rec in list(state["worktrees"].items()):
            if rec.get("object") in dropped:
                state["worktrees"].pop(path, None)
        save_state(state)
    return dropped


def gc(cfg: dict, secrets: dict, dry_run: bool = False) -> int:
    """Drop sprout_wt_* objects with no live worktree behind them."""
    require_admin_url(secrets)
    state = load_state()
    live_paths = live_worktree_paths_for_config(cfg)
    postgres = list_postgres_worktree_dbs(secrets)
    plans = plan_orphans(state, live_paths, postgres)

    if postgres is None:
        log(
            "gc: skipping postgres scan (install psql on PATH for orphan scan); "
            f"plans={len(plans)}"
        )

    dropped = apply_drop_plans(cfg, secrets, plans, dry_run=dry_run)
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

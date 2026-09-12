"""Provision / drop orchestration."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from sprout_worktree_db.gitutil import repo_config
from sprout_worktree_db.models import (
    DropLease,
    DropRequest,
    PluginConfig,
    ProvisionRequest,
    WorktreeRecord,
)
from sprout_worktree_db.paths import config_path, log
from sprout_worktree_db.state import (
    abort_drop,
    begin_drop,
    claim_key,
    finalize_claim,
    finish_drop,
)
from sprout_worktree_db.steps import run_steps
from sprout_worktree_db.sprout import attach_preview, drop_key, provision_dedicated


def do_provision(
    cfg: PluginConfig, secrets: dict, req: ProvisionRequest
) -> dict:
    worktree = os.path.realpath(req.worktree)
    repo = repo_config(cfg, worktree)
    if not repo:
        raise SystemExit(
            f"{worktree}: no repo config "
            f"(add a repos[] entry with matching main_repo to {config_path()})"
        )

    key, _previous = claim_key(
        worktree, repo, mode=req.mode, requested=req.key
    )
    log(
        f"provision [{req.mode}] worktree={worktree} key={key} repo={repo.name}"
    )

    if req.mode == "preview":
        injection = attach_preview(cfg, secrets, repo, worktree)
    else:
        injection = provision_dedicated(cfg, secrets, repo, worktree, key)

    steps = (
        run_steps(cfg, secrets, repo, worktree)
        if (req.with_steps and repo.steps)
        else []
    )
    record = WorktreeRecord(
        key=key,
        repo=repo.name,
        mode=req.mode,
        object=injection.object_name,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        env_files=[str(p) for p in injection.env_files],
        steps=steps,
        pr_id=injection.pr_id,
        preview_url=injection.preview_url,
    )
    finalize_claim(worktree, key, record)

    payload = record.to_dict()
    log(
        f"provisioned {injection.object_name} into "
        f"{', '.join(str(p) for p in injection.env_files)}"
    )
    print(json.dumps({"ok": True, **payload}, indent=2))
    return payload


def execute_drop_lease(
    cfg: PluginConfig,
    secrets: dict,
    lease: DropLease,
    *,
    skip_postgres: bool,
    reraise: bool,
) -> bool:
    """One lease lifecycle: skip / drop_key → finish, or abort on failure.

    Returns True when Postgres drop ran successfully.
    """
    if skip_postgres:
        finish_drop(lease)
        return False
    try:
        drop_key(cfg, secrets, lease.key)
        finish_drop(lease)
        return True
    except Exception as exc:
        abort_drop(lease)
        if reraise:
            raise
        log(f"drop failed for {lease.key}: {exc}")
        return False


def do_drop(cfg: PluginConfig, secrets: dict, req: DropRequest) -> dict:
    """Begin drop lease → Postgres drop → finish (inverse of claim)."""
    worktree = os.path.realpath(req.worktree) if req.worktree else None
    lease = begin_drop(
        cfg, worktree, requested=req.key, force=req.force
    )

    skip_postgres = req.forget_only or lease.skip_postgres
    if skip_postgres:
        reason = (
            "leaving shared/preview DB intact"
            if lease.skip_postgres
            else "--forget-only"
        )
        log(f"forgetting claim for {lease.key}; {reason}")
    dropped = execute_drop_lease(
        cfg,
        secrets,
        lease,
        skip_postgres=skip_postgres,
        reraise=True,
    )
    if dropped:
        log(f"dropped {lease.object_name or lease.key}")
    print(
        json.dumps(
            {
                "ok": True,
                "key": lease.key,
                "dropped": dropped,
                "forgot_only": skip_postgres,
            },
            indent=2,
        )
    )
    return {"key": lease.key}

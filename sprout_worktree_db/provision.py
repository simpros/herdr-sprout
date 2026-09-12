"""Provision / drop orchestration."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from sprout_worktree_db.gitutil import repo_config
from sprout_worktree_db.models import (
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
    object_name,
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


def do_drop(cfg: PluginConfig, secrets: dict, req: DropRequest) -> dict:
    """Begin drop lease → Postgres drop → finish (inverse of claim)."""
    worktree = os.path.realpath(req.worktree) if req.worktree else None
    lease = begin_drop(cfg, worktree, requested=req.key)

    dropped = False
    skip_postgres = req.forget_only or lease.skip_postgres
    if skip_postgres:
        reason = (
            "leaving shared/preview DB intact"
            if lease.skip_postgres
            else "--forget-only"
        )
        log(f"forgetting claim for {lease.key}; {reason}")
    else:
        try:
            drop_key(cfg, secrets, lease.key)
        except Exception:
            abort_drop(lease)
            raise
        dropped = True
        log(f"dropped {object_name(lease.key)}")

    finish_drop(lease)
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

"""Provision orchestration (straight-line session over a provision lease)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from sprout_worktree_db.envfile import merge_env_file
from sprout_worktree_db.gitutil import repo_config
from sprout_worktree_db.leases import claim_provision
from sprout_worktree_db.models import (
    EnvInjection,
    PluginConfig,
    ProvisionRequest,
    Secrets,
    WorktreeRecord,
)
from sprout_worktree_db.paths import config_path, log
from sprout_worktree_db.steps import run_steps
from sprout_worktree_db.sprout import attach_preview, provision_dedicated


def _apply_pending_env(injection: EnvInjection) -> None:
    for env_file in injection.env_files:
        merge_env_file(env_file, injection.pending_env)


def do_provision(
    cfg: PluginConfig, secrets: Secrets, req: ProvisionRequest
) -> dict:
    worktree = os.path.realpath(req.worktree)
    if not os.path.isdir(worktree):
        raise SystemExit(f"{worktree}: worktree path is not a directory")
    repo = repo_config(cfg, worktree)
    if not repo:
        raise SystemExit(
            f"{worktree}: no repo config "
            f"(add a repos[] entry with matching main_repo to {config_path()})"
        )

    with claim_provision(
        worktree, repo, mode=req.mode, requested=req.key
    ) as lease:
        log(
            f"provision [{req.mode}] worktree={worktree} "
            f"key={lease.key} repo={repo.name}"
        )
        if req.mode == "preview":
            injection = attach_preview(cfg, secrets, repo, worktree)
        else:
            injection = provision_dedicated(
                cfg, secrets, repo, worktree, lease.key
            )

        record = WorktreeRecord(
            key=lease.key,
            repo=repo.name,
            mode=req.mode,
            object=injection.object_name,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            env_files=[str(p) for p in injection.env_files],
            steps=[],
            pr_id=injection.pr_id,
            preview_url=injection.preview_url,
        )
        # Finalize gates disk truth: env + steps only after the claim sticks.
        # The provision lease is held through env/steps so drop/GC cannot race.
        lease.finalize(record)

        _apply_pending_env(injection)
        steps = (
            run_steps(cfg, secrets, repo, worktree)
            if (req.with_steps and repo.steps)
            else []
        )
        if steps:
            record.steps = steps
            lease.record_steps(steps)

    payload = record.to_dict()
    log(
        f"provisioned {injection.object_name} into "
        f"{', '.join(str(p) for p in injection.env_files)}"
    )
    print(json.dumps({"ok": True, **payload}, indent=2))
    return payload

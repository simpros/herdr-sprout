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
from sprout_worktree_db.state import claim_key, locked_state, object_name, release_key
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
    with locked_state() as state:
        claimed = state.worktrees.get(worktree)
        if claimed and claimed.key != key:
            raise RuntimeError(
                f"key claim lost for {worktree}: held {claimed.key!r}, "
                f"provisioned {key!r}"
            )
        if claimed and claimed.created_at:
            record.created_at = claimed.created_at
        state.worktrees[worktree] = record

    payload = record.to_dict()
    log(
        f"provisioned {injection.object_name} into "
        f"{', '.join(str(p) for p in injection.env_files)}"
    )
    print(json.dumps({"ok": True, **payload}, indent=2))
    return payload


def do_drop(cfg: PluginConfig, secrets: dict, req: DropRequest) -> dict:
    """Release state claim under lock, then drop Postgres (inverse of claim)."""
    worktree = os.path.realpath(req.worktree) if req.worktree else None
    key, skip_drop = release_key(cfg, worktree, requested=req.key)
    if key is None:
        raise SystemExit("drop could not resolve a key")
    if skip_drop:
        log(
            f"worktree {worktree} shares preview DB; refusing to drop"
        )
    elif not req.forget_only:
        drop_key(cfg, secrets, key)
        log(f"dropped {object_name(key)}")
    print(
        json.dumps(
            {
                "ok": True,
                "key": key,
                "dropped": not skip_drop and not req.forget_only,
                "refused_shared_preview": skip_drop,
            },
            indent=2,
        )
    )
    return {"key": key}

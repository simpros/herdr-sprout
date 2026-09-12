"""Provision / drop orchestration."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from sprout_worktree_db.gitutil import repo_config
from sprout_worktree_db.models import (
    DropRequest,
    PluginState,
    ProvisionRequest,
    WorktreeRecord,
)
from sprout_worktree_db.paths import config_path, log
from sprout_worktree_db.state import (
    claim_key,
    locked_state,
    load_state,
    mint_key,
    object_name,
)
from sprout_worktree_db.steps import run_steps
from sprout_worktree_db.sprout import attach_preview, drop_key, provision_dedicated


def do_provision(
    cfg: dict, secrets: dict, req: ProvisionRequest
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
        f"provision [{req.mode}] worktree={worktree} key={key} repo={repo['name']}"
    )

    if req.mode == "preview":
        injection = attach_preview(cfg, secrets, repo, worktree)
    else:
        injection = provision_dedicated(cfg, secrets, repo, worktree, key)

    steps = (
        run_steps(cfg, secrets, repo, worktree)
        if (req.with_steps and repo.get("steps"))
        else []
    )
    record = WorktreeRecord(
        key=key,
        repo=repo["name"],
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


def do_drop(cfg: dict, secrets: dict, req: DropRequest) -> dict:
    state = load_state()
    worktree = os.path.realpath(req.worktree) if req.worktree else None
    record = state.worktrees.get(worktree) if worktree else None

    key = _resolve_drop_key(cfg, state, worktree, record, req.key)
    shared = bool(record and record.mode == "preview")
    if shared:
        log(
            f"worktree {worktree} shares preview DB {record.object}; "
            "refusing to drop"
        )
    elif not req.forget_only:
        drop_key(cfg, secrets, key)
        log(f"dropped {object_name(key)}")
    if record and worktree:
        with locked_state() as locked:
            locked.worktrees.pop(worktree, None)
    print(
        json.dumps(
            {
                "ok": True,
                "key": key,
                "dropped": not shared and not req.forget_only,
                "refused_shared_preview": shared,
            },
            indent=2,
        )
    )
    return {"key": key}


def _resolve_drop_key(
    cfg: dict,
    state: PluginState,
    worktree: str | None,
    record: WorktreeRecord | None,
    requested: str | None,
) -> str:
    """Same identity rules as provision; fail closed without state or --key."""
    if requested:
        return requested
    if record:
        return record.key
    if not worktree:
        raise SystemExit("drop needs --worktree or --key")
    repo = repo_config(cfg, worktree)
    if repo:
        # Honest recovery when state was wiped but path+repo still known:
        # remint with the same always-suffix rule as provision.
        return mint_key(worktree, repo)
    raise SystemExit(
        f"no state row for {worktree}; pass --key "
        "(or ensure repo config matches so the slug can be reminted)"
    )

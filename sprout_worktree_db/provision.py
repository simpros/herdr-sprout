"""Provision / drop orchestration."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sprout_worktree_db.gitutil import repo_config
from sprout_worktree_db.models import DropRequest, ProvisionRequest, WorktreeRecord
from sprout_worktree_db.paths import config_path, log
from sprout_worktree_db.state import load_state, normalize_key, object_name, pick_key, save_state
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

    state = load_state()
    key = req.key or pick_key(state, worktree, repo)
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
        shared_with_preview=injection.shared_with_preview,
        pr_id=injection.pr_id,
        preview_url=injection.preview_url,
    )
    state = load_state()
    state["worktrees"][worktree] = record.to_dict()
    save_state(state)
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
    record = state["worktrees"].get(worktree) if worktree else None
    key = req.key
    if record:
        key = record["key"]
    if not key and worktree:
        key = normalize_key(Path(worktree).name or "")
    if not key:
        raise SystemExit("drop needs --worktree or --key")
    shared = bool(record and record.get("mode") == "preview")
    if shared:
        log(
            f"worktree {worktree} shares preview DB {record.get('object')}; "
            "refusing to drop"
        )
    elif not req.forget_only:
        drop_key(cfg, secrets, key)
        log(f"dropped {object_name(key)}")
    if record and worktree:
        state["worktrees"].pop(worktree, None)
        save_state(state)
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

"""Provision orchestration (straight-line session over a provision lease)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from sprout_worktree_db.envfile import merge_env_file
from sprout_worktree_db.errors import ConfigError
from sprout_worktree_db.gitutil import repo_config
from sprout_worktree_db.leases import ProvisionLease, claim_provision
from sprout_worktree_db.models import (
    EnvInjection,
    PluginConfig,
    ProvisionRequest,
    Secrets,
    WorktreeRecord,
)
from sprout_worktree_db.paths import config_path, log
from sprout_worktree_db.steps import run_steps
from sprout_worktree_db.sprout import (
    attach_preview,
    drop_key,
    provision_dedicated,
)


def _apply_pending_env(injection: EnvInjection) -> None:
    for env_file in injection.env_files:
        merge_env_file(env_file, injection.pending_env)


def _drop_orphan_on_finalize_failure(
    cfg: PluginConfig,
    secrets: Secrets,
    req: ProvisionRequest,
    lease: ProvisionLease,
    injection: EnvInjection | None,
    finalized: bool,
) -> None:
    """Best-effort cleanup of the designed orphan gap.

    Crash (or a lost lease) between a successful dedicated ``sprout``
    provision and ``finalize`` leaves a real Postgres DB behind an
    ``object=""`` claim row that GC will not plan (the claim path still
    exists). When this run created the object — dedicated mode, sprout
    reported success, finalize never ran, and no object predates this
    claim — drop it best-effort. Skipped otherwise: preview objects are
    shared, post-finalize claims are GC territory, and re-provisions must
    never tear down a live DB that predates the run. Never raises.
    """
    if finalized or injection is None:
        return
    if req.mode != "dedicated" or lease.prior_object:
        return
    try:
        drop_key(cfg, secrets, lease.key)
    except Exception as exc:
        log(f"provision orphan cleanup failed for {lease.key}: {exc}")
    else:
        log(f"provision orphan cleanup: dropped {injection.object_name}")


def do_provision(
    cfg: PluginConfig, secrets: Secrets, req: ProvisionRequest
) -> dict:
    worktree = os.path.realpath(req.worktree)
    if not os.path.isdir(worktree):
        raise ConfigError(f"{worktree}: worktree path is not a directory")
    repo = repo_config(cfg, worktree)
    if not repo:
        raise ConfigError(
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
        injection: EnvInjection | None = None
        finalized = False
        try:
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
                created_at=datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                env_files=[str(p) for p in injection.env_files],
                steps=[],
                pr_id=injection.pr_id,
                preview_url=injection.preview_url,
            )
            # Finalize gates disk truth: env + steps only after the claim sticks.
            # The provision lease is held through env/steps so drop/GC cannot race.
            lease.finalize(record)
            finalized = True

            _apply_pending_env(injection)
            steps = (
                run_steps(cfg, secrets, repo, worktree)
                if (req.with_steps and repo.steps)
                else []
            )
            if steps:
                record.steps = steps
                lease.record_steps(steps)
        except BaseException:
            _drop_orphan_on_finalize_failure(
                cfg, secrets, req, lease, injection, finalized
            )
            raise

    payload = record.to_dict()
    log(
        f"provisioned {injection.object_name} into "
        f"{', '.join(str(p) for p in injection.env_files)}"
    )
    print(json.dumps({"ok": True, **payload}, indent=2))
    return payload

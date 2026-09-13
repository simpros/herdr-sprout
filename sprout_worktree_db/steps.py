"""Post-provision step runner."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path

from sprout_worktree_db.gitutil import run
from sprout_worktree_db.models import (
    PluginConfig,
    RepoConfig,
    Secrets,
    StepResult,
    StepsStatus,
)
from sprout_worktree_db.paths import clean_env, log, require_admin_url
from sprout_worktree_db.sprout import target_name


def steps_status(steps: list[StepResult]) -> StepsStatus | None:
    """Ternary status: ok | skipped | failed (None when no steps ran)."""
    if not steps:
        return None
    if any(s.skipped for s in steps):
        return "skipped"
    if all(s.ok for s in steps):
        return "ok"
    return "failed"


def run_steps(
    cfg: PluginConfig, secrets: Secrets, repo: RepoConfig, worktree: str
) -> list[StepResult]:
    results: list[StepResult] = []
    bun = cfg.bun or shutil.which("bun") or "bun"
    if repo.requires_node_modules and not (
        Path(worktree) / "node_modules"
    ).exists():
        log(
            "steps skipped: node_modules missing "
            "(install deps, then re-run provision)"
        )
        # Skips are not success — status must surface steps_status: skipped.
        return [StepResult(step="all", ok=False, skipped="node_modules missing")]
    # Repo scripts shell out to `bun` by name; hooks often lack ~/.bun/bin.
    bun_dir = str(Path(bun).parent) if bun else ""
    step_env = clean_env(
        {
            "PATH": (
                (bun_dir + os.pathsep) if bun_dir else ""
            )
            + os.environ.get("PATH", ""),
        }
    )
    admin = urllib.parse.urlsplit(require_admin_url(secrets))
    for spec in repo.steps:
        cmd = [bun if part == "{bun}" else part for part in spec.cmd]
        env = dict(step_env)
        if spec.as_admin:
            # Role DDL needs CREATEROLE; the worktree owner role lacks it.
            # Drive overrides only through renames + optional spec.admin_env.
            user_key = target_name(repo, "PGUSER")
            pass_key = target_name(repo, "PGPASSWORD")
            env[user_key] = urllib.parse.unquote(admin.username or "")
            env[pass_key] = urllib.parse.unquote(admin.password or "")
            for k, v in spec.admin_env.items():
                env[str(k)] = str(v)
        started = time.time()
        try:
            rc, out, err = run(cmd, cwd=worktree, env=env, timeout=900)
        except subprocess.TimeoutExpired:
            results.append(
                StepResult(step=" ".join(cmd), ok=False, error="timeout")
            )
            log(f"step TIMEOUT: {' '.join(cmd)}")
            break
        elapsed = round(time.time() - started, 1)
        results.append(
            StepResult(
                step=" ".join(cmd),
                ok=rc == 0,
                seconds=elapsed,
                error=(err or out)[-600:] if rc != 0 else None,
            )
        )
        log(
            f"step {'ok' if rc == 0 else 'FAILED'} ({elapsed}s): {' '.join(cmd)}"
        )
        if rc != 0:
            break
    return results

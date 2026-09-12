"""Post-provision step runner."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path

from sprout_worktree_db.gitutil import run
from sprout_worktree_db.paths import clean_env, log, require_admin_url
from sprout_worktree_db.sprout import target_name


def run_steps(cfg: dict, secrets: dict, repo: dict, worktree: str) -> list[dict]:
    results: list[dict] = []
    bun = cfg.get("bun") or shutil.which("bun") or "bun"
    if repo.get("requires_node_modules") and not (
        Path(worktree) / "node_modules"
    ).exists():
        log(
            "steps skipped: node_modules missing "
            "(install deps, then re-run provision)"
        )
        return [{"step": "all", "ok": True, "skipped": "node_modules missing"}]
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
    for raw_step in repo.get("steps", []):
        step = {"cmd": raw_step} if isinstance(raw_step, list) else raw_step
        cmd = [bun if part == "{bun}" else part for part in step["cmd"]]
        env = dict(step_env)
        if step.get("as_admin"):
            # Role DDL needs CREATEROLE; the worktree owner role lacks it.
            # Drive overrides only through renames + optional step.admin_env.
            user_key = target_name(repo, "PGUSER")
            pass_key = target_name(repo, "PGPASSWORD")
            env[user_key] = urllib.parse.unquote(admin.username or "")
            env[pass_key] = urllib.parse.unquote(admin.password or "")
            for k, v in (step.get("admin_env") or {}).items():
                env[str(k)] = str(v)
        started = time.time()
        try:
            rc, out, err = run(cmd, cwd=worktree, env=env, timeout=900)
        except subprocess.TimeoutExpired:
            results.append(
                {"step": " ".join(cmd), "ok": False, "error": "timeout"}
            )
            log(f"step TIMEOUT: {' '.join(cmd)}")
            break
        elapsed = round(time.time() - started, 1)
        results.append(
            {"step": " ".join(cmd), "ok": rc == 0, "seconds": elapsed}
        )
        log(
            f"step {'ok' if rc == 0 else 'FAILED'} ({elapsed}s): {' '.join(cmd)}"
        )
        if rc != 0:
            results[-1]["error"] = (err or out)[-600:]
            break
    return results

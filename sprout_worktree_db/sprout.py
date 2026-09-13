"""sprout CLI wrappers: provision / drop / list."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import urllib.parse
from pathlib import Path

from sprout_worktree_db.envfile import read_env_values
from sprout_worktree_db.errors import ConfigError, SproutError
from sprout_worktree_db.gitutil import branch_of, run
from sprout_worktree_db.models import (
    EnvInjection,
    PluginConfig,
    RepoConfig,
    Secrets,
)
from sprout_worktree_db.paths import clean_env, log, require_admin_url


def resolve_sprout_cli(cfg: PluginConfig) -> str:
    configured = cfg.cli
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("sprout")
    if found:
        return found
    raise ConfigError(
        "sprout CLI not found (set config.cli or install sprout on PATH; "
        "glibc hosts may need a source build — see README)"
    )


def sprout_cli(
    cfg: PluginConfig, secrets: Secrets, args: list[str]
) -> tuple[int, str, str]:
    cmd = [resolve_sprout_cli(cfg), *args]
    env = clean_env(
        {
            "SPROUT_URL": secrets.get("SPROUT_URL", ""),
            "SPROUT_TOKEN": secrets.get("SPROUT_ADMIN_TOKEN", ""),
        }
    )
    return run(cmd, env=env)


def target_name(repo: RepoConfig, logical: str) -> str:
    return repo.renames.get(logical, logical)


def track_keys_for(repo: RepoConfig) -> set[str]:
    return set(repo.renames.values()) | {
        "PGHOST",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGPASSWORD",
        "DATABASE_URL",
    }


def provision_dedicated(
    cfg: PluginConfig,
    secrets: Secrets,
    repo: RepoConfig,
    worktree: str,
    key: str,
) -> EnvInjection:
    """Provision once via sprout into a scratch env; defer worktree merges."""
    admin_url = require_admin_url(secrets)
    renames: list[str] = []
    for logical, target in repo.renames.items():
        renames += ["--rename", f"{logical}={target}"]
    env_files = [Path(worktree) / rel for rel in repo.env_files]
    track_keys = track_keys_for(repo)

    # System temp: credentials must not land in the worktree before finalize.
    fd, scratch_name = tempfile.mkstemp(
        prefix="sprout-provision-", suffix=".env"
    )
    os.close(fd)
    scratch = Path(scratch_name)
    try:
        rc, out, err = sprout_cli(
            cfg,
            secrets,
            [
                "worktree-db",
                "provision",
                "--slug",
                key,
                "--env-file",
                str(scratch),
                "--admin-url",
                admin_url,
                *renames,
            ],
        )
        if rc != 0:
            raise SproutError(
                f"sprout worktree-db provision failed ({rc}): {err or out}"
            )
        conn = json.loads(out)
        written = read_env_values(scratch, track_keys)
        values = {k: v for k, v in written.items() if k in track_keys}
        return EnvInjection(
            object_name=conn["object_name"],
            env_files=tuple(env_files),
            pending_env=values,
        )
    finally:
        scratch.unlink(missing_ok=True)


def attach_preview(
    cfg: PluginConfig, secrets: Secrets, repo: RepoConfig, worktree: str
) -> EnvInjection:
    """Point the worktree at the PR preview's database instead of a fresh one."""
    branch = branch_of(worktree)
    if not branch:
        raise SproutError("cannot resolve branch of worktree")
    pr = resolve_pr(repo, branch)
    if not pr:
        raise SproutError(f"no open MR/PR found for branch {branch}")
    rc, out, err = sprout_cli(cfg, secrets, ["list"])
    if rc != 0:
        raise SproutError(f"sprout list failed: {err or out}")
    previews = json.loads(out).get("previews", [])
    canonical = repo.canonical_repo_id
    if not canonical:
        raise SproutError(
            f"repos[] entry {repo.name!r} missing canonical_repo_id "
            "(required for attach-preview)"
        )
    # Canonical only (plus optional .git suffix) — no silent slug fallback.
    target = next(
        (
            p
            for p in previews
            if p["pr_id"] == pr
            and p["canonical_repo_id"] in (canonical, f"{canonical}.git")
        ),
        None,
    )
    if not target:
        raise SproutError(
            f"no sprout preview registered for {repo.name} PR/MR {pr}"
        )
    owner = secrets.get("SPROUT_PREVIEW_OWNER_URL", "").strip()
    if not owner:
        raise SproutError(
            "SPROUT_PREVIEW_OWNER_URL required for attach-preview"
        )
    admin = urllib.parse.urlsplit(owner)
    host = secrets.get("SPROUT_PG_HOST") or admin.hostname or "127.0.0.1"
    port = secrets.get("SPROUT_PG_PORT") or str(admin.port or 5432)
    values = {
        target_name(repo, "PGHOST"): host,
        target_name(repo, "PGPORT"): port,
        target_name(repo, "PGDATABASE"): target["db_name"],
        target_name(repo, "PGUSER"): urllib.parse.unquote(admin.username or ""),
        target_name(repo, "PGPASSWORD"): urllib.parse.unquote(
            admin.password or ""
        ),
    }
    env_files = [Path(worktree) / rel for rel in repo.env_files]
    return EnvInjection(
        object_name=target["db_name"],
        env_files=tuple(env_files),
        pending_env=values,
        pr_id=pr,
        preview_url=target.get("hostname"),
    )


def resolve_pr(repo: RepoConfig, branch: str) -> int | None:
    """Open MR/PR number for a branch, via glab/gh."""
    canonical = repo.canonical_repo_id
    if not canonical:
        log(
            f"cannot resolve PR: repos[] entry {repo.name!r} "
            "missing canonical_repo_id"
        )
        return None
    forge = repo.forge
    slug = canonical.split("://", 1)[-1]
    if forge == "gitlab":
        # GitLab's API wants the project path WITHOUT the host.
        host, _, project = slug.partition("/")
        if not project:
            log(f"cannot resolve PR: unexpected canonical repo id {canonical}")
            return None
        extra = {} if host == "gitlab.com" else {"GITLAB_HOST": host}
        rc, out, err = run(
            [
                "glab",
                "api",
                f"projects/{urllib.parse.quote(project, safe='')}/merge_requests"
                f"?source_branch={urllib.parse.quote(branch, safe='')}"
                f"&state=opened",
            ],
            env=clean_env(extra),
            timeout=60,
        )
        if rc != 0:
            log(f"cannot resolve PR for {branch}: {err or out}")
            return None
        data = json.loads(out or "[]")
        return int(data[0]["iid"]) if data else None
    rc, out, err = run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            slug,
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number",
        ],
        timeout=60,
    )
    if rc != 0:
        log(f"cannot resolve PR for {branch}: {err or out}")
        return None
    data = json.loads(out or "[]")
    return int(data[0]["number"]) if data else None


def drop_key(cfg: PluginConfig, secrets: Secrets, key: str) -> None:
    rc, out, err = sprout_cli(
        cfg,
        secrets,
        [
            "worktree-db",
            "drop",
            "--slug",
            key,
            "--admin-url",
            require_admin_url(secrets),
        ],
    )
    if rc != 0:
        raise SproutError(
            f"sprout worktree-db drop failed ({rc}): {err or out}"
        )

"""Config / state / secrets path resolution and logging."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sprout_worktree_db import PLUGIN_ID
from sprout_worktree_db.models import PluginConfig

LEGACY_CONFIG_DIR = Path.home() / ".config" / "sprout"

# Bun's SQL client falls back to libpq-style env vars; a worktree env file
# sourced into the shell once made `drop` fail with "cannot drop the currently
# open database". Always hand the CLI a clean PG environment.
PG_ENV_KEYS = (
    "PGHOST",
    "PGPORT",
    "PGUSER",
    "PGPASSWORD",
    "PGDATABASE",
    "PGAPPUSER",
    "PGAPPPASSWORD",
    "DATABASE_URL",
    "PGSERVICE",
    "PGOPTIONS",
    "PGSSLMODE",
)


def config_dir() -> Path:
    if env := os.environ.get("HERDR_PLUGIN_CONFIG_DIR"):
        return Path(env)
    return Path.home() / ".config" / "herdr" / "plugins" / "config" / PLUGIN_ID


def state_dir() -> Path:
    if env := os.environ.get("HERDR_PLUGIN_STATE_DIR"):
        return Path(env)
    return Path.home() / ".local" / "state" / "herdr" / "plugins" / PLUGIN_ID


def _migrate_legacy_file(primary: Path, legacy: Path, kind: str) -> Path:
    """Copy a legacy file into place once; fall back to legacy on failure.

    The legacy file is left in place — never destroy user data on a
    best-effort migration.
    """
    try:
        primary.parent.mkdir(parents=True, exist_ok=True)
        primary.write_bytes(legacy.read_bytes())
    except OSError as exc:
        log(
            f"using legacy {kind} {legacy} (migrate to "
            f"{primary} to silence this warning: {exc})"
        )
        return legacy
    log(f"migrated legacy {kind} {legacy} -> {primary}")
    return primary


def config_path() -> Path:
    primary = config_dir() / "config.json"
    if primary.exists():
        return primary
    legacy = LEGACY_CONFIG_DIR / "worktree-db.json"
    if legacy.exists():
        return _migrate_legacy_file(primary, legacy, "config")
    return primary


def secrets_path() -> Path:
    primary = config_dir() / "secrets.env"
    if primary.exists():
        return primary
    legacy = LEGACY_CONFIG_DIR / "worktree-db.env"
    if legacy.exists():
        return _migrate_legacy_file(primary, legacy, "secrets")
    return primary


def state_path() -> Path:
    return state_dir() / "state.json"


def log_path() -> Path:
    return state_dir() / "plugin.log"


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{stamp} {msg}"
    print(line, flush=True)
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_config() -> PluginConfig:
    path = config_path()
    if not path.exists():
        raise SystemExit(
            f"missing config {config_dir() / 'config.json'} "
            f"(see README; herdr plugin config-dir {PLUGIN_ID})"
        )
    with path.open() as fh:
        data = json.load(fh)
    return PluginConfig.from_dict(data)


def load_secrets() -> dict[str, str]:
    out: dict[str, str] = {}
    path = secrets_path()
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def require_admin_url(secrets: dict[str, str]) -> str:
    url = secrets.get("SPROUT_WORKTREE_ADMIN_URL", "").strip()
    if not url:
        raise SystemExit(
            f"SPROUT_WORKTREE_ADMIN_URL missing in {secrets_path()} "
            "(CREATEROLE admin DSN required)"
        )
    return url


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in PG_ENV_KEYS}
    if extra:
        env.update(extra)
    return env

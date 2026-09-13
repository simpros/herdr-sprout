"""Typed contracts for provision / drop / GC / config."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from sprout_worktree_db.errors import ConfigError, CorruptStateError


Mode = Literal["dedicated", "preview"]
StepsStatus = Literal["ok", "skipped", "failed"]
LeaseOp = Literal["provision", "drop"]

# Secrets flow end-to-end as string mappings (parsed once in paths.py).
Secrets = Mapping[str, str]


@dataclass(frozen=True)
class StepSpec:
    """One configured post-provision step (parsed once in ``RepoConfig``).

    ``cmd`` is the full argv (``{bun}`` is expanded to the bun binary at
    run time). ``as_admin`` re-issues owner credentials via ``admin_env``
    overrides; the runner reads these fields directly — no ``.get`` soup.
    """

    cmd: tuple[str, ...]
    as_admin: bool = False
    admin_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: object, repo_name: str) -> StepSpec:
        if isinstance(raw, list):
            parts = raw
            as_admin = False
            admin_env: dict[str, str] = {}
        elif isinstance(raw, dict) and "cmd" in raw:
            parts = raw["cmd"]
            as_admin = bool(raw.get("as_admin"))
            extra = raw.get("admin_env") or {}
            if not isinstance(extra, dict):
                raise ConfigError(
                    f"repos[] entry {repo_name!r}: "
                    "'admin_env' must be an object"
                )
            admin_env = {str(k): str(v) for k, v in extra.items()}
        else:
            raise ConfigError(
                f"repos[] entry {repo_name!r}: each step needs 'cmd'"
            )
        if not isinstance(parts, list) or not parts:
            raise ConfigError(
                f"repos[] entry {repo_name!r}: each step needs 'cmd'"
            )
        return cls(
            cmd=tuple(str(p) for p in parts),
            as_admin=as_admin,
            admin_env=admin_env,
        )


@dataclass(frozen=True)
class StepResult:
    """One executed step row (persisted on the claim, shown in status).

    ``step`` is the joined command (``"all"`` for the node_modules skip).
    Exactly one of ``error`` / ``skipped`` is set on non-ok rows.
    """

    step: str
    ok: bool
    seconds: float | None = None
    error: str | None = None
    skipped: str | None = None

    def to_dict(self) -> dict:
        data: dict = {"step": self.step, "ok": self.ok}
        if self.seconds is not None:
            data["seconds"] = self.seconds
        if self.error is not None:
            data["error"] = self.error
        if self.skipped is not None:
            data["skipped"] = self.skipped
        return data

    @classmethod
    def from_dict(cls, data: dict) -> StepResult:
        if not isinstance(data, dict):
            raise CorruptStateError(
                "corrupt state.json: step rows must be objects"
            )
        return cls(
            step=str(data.get("step", "")),
            ok=bool(data.get("ok")),
            seconds=(
                float(data["seconds"])
                if data.get("seconds") is not None
                else None
            ),
            error=(
                str(data["error"]) if data.get("error") is not None else None
            ),
            skipped=(
                str(data["skipped"])
                if data.get("skipped") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class EnvInjection:
    """Connection credentials for worktree env files.

    ``pending_env`` is always set after a successful injection. Callers must
    merge only after the provision lease finalizes (while the lease is still
    held) so a failed finalize cannot leave env pointing at a new DSN while
    state still describes the prior claim, and concurrent drop cannot race
    the deferred merge.
    """

    object_name: str
    env_files: tuple[Path, ...]
    pending_env: dict[str, str]
    pr_id: int | None = None
    preview_url: str | None = None


@dataclass(frozen=True)
class RepoConfig:
    """One repos[] entry — validated at config load."""

    name: str
    main_repo: str
    env_files: tuple[str, ...]
    renames: dict[str, str] = field(default_factory=dict)
    steps: tuple[StepSpec, ...] = field(default_factory=tuple)
    requires_node_modules: bool = False
    worktrees_root: str | None = None
    canonical_repo_id: str | None = None
    forge: str = "gitlab"

    @classmethod
    def from_dict(cls, data: dict) -> RepoConfig:
        if not isinstance(data, dict):
            raise ConfigError("repos[] entries must be objects")
        name = str(data.get("name") or "").strip()
        main_repo = str(data.get("main_repo") or "").strip()
        raw_env = data.get("env_files")
        if not name:
            raise ConfigError("repos[] entry missing required 'name'")
        if not main_repo:
            raise ConfigError(
                f"repos[] entry {name!r} missing required 'main_repo'"
            )
        if not isinstance(raw_env, list) or not raw_env:
            raise ConfigError(
                f"repos[] entry {name!r} requires non-empty 'env_files'"
            )
        env_files = tuple(str(p) for p in raw_env if str(p).strip())
        if not env_files:
            raise ConfigError(
                f"repos[] entry {name!r} requires non-empty 'env_files'"
            )
        renames = data.get("renames") or {}
        if not isinstance(renames, dict):
            raise ConfigError(
                f"repos[] entry {name!r}: 'renames' must be an object"
            )
        raw_steps = data.get("steps") or []
        if not isinstance(raw_steps, list):
            raise ConfigError(
                f"repos[] entry {name!r}: 'steps' must be an array"
            )
        steps: list[StepSpec] = []
        for raw in raw_steps:
            steps.append(StepSpec.from_raw(raw, name))
        forge = str(data.get("forge") or "gitlab")
        return cls(
            name=name,
            main_repo=main_repo,
            env_files=env_files,
            renames={str(k): str(v) for k, v in renames.items()},
            steps=tuple(steps),
            requires_node_modules=bool(data.get("requires_node_modules")),
            worktrees_root=(
                str(data["worktrees_root"])
                if data.get("worktrees_root")
                else None
            ),
            canonical_repo_id=(
                str(data["canonical_repo_id"])
                if data.get("canonical_repo_id")
                else None
            ),
            forge=forge,
        )


@dataclass(frozen=True)
class PluginConfig:
    """Top-level plugin config.json."""

    repos: tuple[RepoConfig, ...]
    auto_provision_on_create: bool = False
    cli: str | None = None
    bun: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> PluginConfig:
        if not isinstance(data, dict):
            raise ConfigError("config.json must be a JSON object")
        raw_repos = data.get("repos")
        if not isinstance(raw_repos, list) or not raw_repos:
            raise ConfigError("config.json requires a non-empty 'repos' array")
        repos = tuple(RepoConfig.from_dict(r) for r in raw_repos)
        cli = data.get("cli")
        bun = data.get("bun")
        return cls(
            repos=repos,
            auto_provision_on_create=bool(
                data.get("auto_provision_on_create", False)
            ),
            cli=str(cli) if cli else None,
            bun=str(bun) if bun else None,
        )


@dataclass
class WorktreeRecord:
    key: str
    repo: str
    mode: Mode
    object: str
    created_at: str
    env_files: list[str] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)
    pr_id: int | None = None
    preview_url: str | None = None

    def to_dict(self) -> dict:
        data: dict = {
            "key": self.key,
            "repo": self.repo,
            "mode": self.mode,
            "object": self.object,
            "created_at": self.created_at,
            "env_files": list(self.env_files),
            "steps": [s.to_dict() for s in self.steps],
        }
        if self.pr_id is not None:
            data["pr_id"] = self.pr_id
        if self.preview_url is not None:
            data["preview_url"] = self.preview_url
        return data

    @classmethod
    def from_dict(cls, data: dict) -> WorktreeRecord:
        # Legacy "status" field is ignored — derived from lease membership.
        if not data.get("key"):
            raise CorruptStateError(
                "corrupt state.json: worktree row missing 'key' "
                "(fix or remove the row)"
            )
        raw_mode = data.get("mode")
        if raw_mode not in ("dedicated", "preview"):
            raise CorruptStateError(
                f"corrupt state.json: worktree {data.get('key')!r} has "
                f"invalid mode {raw_mode!r} (expected 'dedicated'/'preview')"
            )
        mode: Mode = "preview" if raw_mode == "preview" else "dedicated"
        obj = str(data.get("object") or "")
        if mode == "dedicated" and not obj:
            raise CorruptStateError(
                f"corrupt state.json: dedicated claim {data.get('key')!r} "
                "missing 'object' (fix or remove the row)"
            )
        raw_steps = data.get("steps") or []
        if not isinstance(raw_steps, list):
            raise CorruptStateError(
                "corrupt state.json: step rows must be a list"
            )
        steps: list[StepResult] = []
        for s in raw_steps:
            if not isinstance(s, dict):
                raise CorruptStateError(
                    "corrupt state.json: step rows must be objects"
                )
            steps.append(StepResult.from_dict(s))
        return cls(
            key=str(data["key"]),
            repo=str(data.get("repo", "")),
            mode=mode,
            object=obj,
            created_at=str(data.get("created_at", "")),
            env_files=list(data.get("env_files") or []),
            steps=steps,
            pr_id=data.get("pr_id"),
            preview_url=data.get("preview_url"),
        )


@dataclass(frozen=True)
class DropOp:
    """One drop *plan* for resolve / reserve / execute (not a persisted lease).

    ``touch_postgres`` is authoritative: ``--forget-only`` is encoded when the
    op is created (False), not re-OR'd at execute time. ``paths`` is the
    forget-set hint (claim path and/or remint extras); reserve expands via
    ``path_for_key``. ``reason`` is GC log context only.

    Deliberately distinct from :class:`SlugLease`: plans are pre-lock input
    (GC builds them lock-free, then reserves under an already-held lock),
    leases are the persisted exclusive reservation. One call style per op —
    session CM for provision, begin/finish/abort for drop — because GC's
    reserve-under-held-lock cannot be served by a session CM.
    """

    key: str
    object_name: str
    touch_postgres: bool
    paths: tuple[str, ...] = ()
    reason: str = ""

    @property
    def state_path(self) -> str | None:
        return self.paths[0] if self.paths else None


@dataclass(frozen=True)
class SlugLease:
    """Exclusive slug reservation — persisted under ``leases[key]``.

    ``op`` is ``provision`` or ``drop``. ``worktrees`` is the authoritative
    forget-set for drop. ``key`` is the map key when stored; ``to_dict`` omits
    it. Executors use the returned handle (with key).
    """

    lease_id: int
    key: str
    op: LeaseOp
    worktrees: tuple[str, ...]
    object_name: str = ""
    touch_postgres: bool = True
    reserved_at: str = ""  # ISO timestamp; missing → treat as expired

    def to_dict(self) -> dict:
        data = {
            "lease_id": self.lease_id,
            "op": self.op,
            "worktrees": list(self.worktrees),
            "object_name": self.object_name,
            "touch_postgres": self.touch_postgres,
        }
        if self.reserved_at:
            data["reserved_at"] = self.reserved_at
        return data

    @classmethod
    def from_dict(cls, key: str, data: dict) -> SlugLease:
        """Parse the canonical lease object shape (strict, no legacy).

        Pre-0.1.0 shapes (string values, ``skip_postgres``, missing
        ``op``, top-level ``dropping``) are corrupt, not migrated — see
        :meth:`PluginState.from_dict`. ``touch_postgres`` is required;
        absence is corrupt, not defaulted.
        """
        if not isinstance(data, dict):
            raise CorruptStateError(
                f"corrupt state.json: leases[{key!r}] must be an object"
            )
        raw_wts = data.get("worktrees") or []
        if not isinstance(raw_wts, list):
            raise CorruptStateError(
                f"corrupt state.json: leases[{key!r}].worktrees must be a list"
            )
        try:
            lease_id = int(data.get("lease_id", 0))
        except (TypeError, ValueError) as exc:
            raise CorruptStateError(
                f"corrupt state.json: leases[{key!r}].lease_id invalid"
            ) from exc
        raw_op = data.get("op")
        if raw_op not in ("provision", "drop"):
            raise CorruptStateError(
                f"corrupt state.json: leases[{key!r}].op must be "
                "'provision' or 'drop'"
            )
        op: LeaseOp = "provision" if raw_op == "provision" else "drop"
        if "touch_postgres" not in data:
            raise CorruptStateError(
                f"corrupt state.json: leases[{key!r}] missing "
                "'touch_postgres' (fix or remove the row)"
            )
        touch = bool(data["touch_postgres"])
        return cls(
            lease_id=lease_id,
            key=key,
            op=op,
            worktrees=tuple(str(p) for p in raw_wts if str(p)),
            object_name=str(data.get("object_name") or ""),
            touch_postgres=touch,
            reserved_at=str(data.get("reserved_at") or ""),
        )


@dataclass
class PluginState:
    worktrees: dict[str, WorktreeRecord] = field(default_factory=dict)
    # key → exclusive SlugLease (provision | drop)
    leases: dict[str, SlugLease] = field(default_factory=dict)
    next_lease_id: int = 1

    def to_dict(self) -> dict:
        data: dict = {
            "worktrees": {
                path: rec.to_dict() for path, rec in self.worktrees.items()
            }
        }
        if self.leases:
            data["leases"] = {
                key: res.to_dict() for key, res in self.leases.items()
            }
        if self.next_lease_id != 1:
            data["next_lease_id"] = self.next_lease_id
        return data

    @classmethod
    def from_dict(cls, data: dict) -> PluginState:
        """Parse the canonical 0.1.0 schema (strict, no legacy migration).

        Pre-0.1.0 shapes (``dropping``, string leases, ``skip_postgres``,
        missing ``op``) fail closed as corrupt: for an official publish the
        dual-schema tax on every load costs more than a one-time manual
        fix of dev-era state files.
        """
        if "dropping" in data:
            raise CorruptStateError(
                "corrupt state.json: 'dropping' is a pre-0.1.0 key "
                "(canonical schema uses 'leases'); rename it to 'leases' "
                "with an 'op: drop' + 'touch_postgres' per entry, or "
                "remove it — refusing to guess"
            )
        raw = data.get("worktrees") or {}
        if not isinstance(raw, dict):
            raise CorruptStateError(
                "corrupt state.json: 'worktrees' must be an object"
            )
        worktrees: dict[str, WorktreeRecord] = {}
        by_key: dict[str, str] = {}
        for path, rec in raw.items():
            if not isinstance(rec, dict):
                raise CorruptStateError(
                    f"corrupt state.json: worktree {path!r} is not an object"
                )
            if not rec.get("key"):
                raise CorruptStateError(
                    f"corrupt state.json: worktree {path!r} missing 'key' "
                    "(fix or remove the row)"
                )
            path_s = str(path)
            record = WorktreeRecord.from_dict(rec)
            holder = by_key.get(record.key)
            if holder is not None:
                raise CorruptStateError(
                    f"corrupt state.json: key {record.key!r} claimed by both "
                    f"{holder!r} and {path_s!r}; drop/forget one row "
                    "(one key → one path)"
                )
            by_key[record.key] = path_s
            worktrees[path_s] = record
        # Only ``leases`` remains — no ``dropping`` merge, no soft defaults.
        raw_leases = data.get("leases")
        if raw_leases is None:
            raw_leases = {}
        if not isinstance(raw_leases, dict):
            raise CorruptStateError(
                "corrupt state.json: 'leases' must be an object"
            )
        leases = {
            str(k): SlugLease.from_dict(str(k), v)
            for k, v in raw_leases.items()
        }
        try:
            next_id = int(data.get("next_lease_id") or 1)
        except (TypeError, ValueError) as exc:
            raise CorruptStateError(
                "corrupt state.json: next_lease_id must be an integer"
            ) from exc
        if next_id < 1:
            next_id = 1
        # Advance past any recovered lease ids.
        for res in leases.values():
            if res.lease_id >= next_id:
                next_id = res.lease_id + 1
        return cls(worktrees=worktrees, leases=leases, next_lease_id=next_id)


@dataclass(frozen=True)
class ProvisionRequest:
    worktree: str
    mode: Mode = "dedicated"
    key: str | None = None
    with_steps: bool = True


@dataclass(frozen=True)
class DropRequest:
    worktree: str | None = None
    key: str | None = None
    forget_only: bool = False
    force: bool = False

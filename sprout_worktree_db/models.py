"""Typed contracts for provision / drop / GC / config."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


Mode = Literal["dedicated", "preview"]
StepsStatus = Literal["ok", "skipped", "failed"]
LeaseOp = Literal["provision", "drop"]

# Secrets flow end-to-end as string mappings (parsed once in paths.py).
Secrets = Mapping[str, str]
# Step rows are small JSON-ish dicts (cmd / ok / error / skipped / seconds).
StepRecord = dict[str, object]


@dataclass(frozen=True)
class EnvInjection:
    """Connection credentials for worktree env files.

    ``pending_env`` is always set after a successful injection. Callers must
    merge only after ``finalize_claim`` (while the provision lease is still
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
    steps: tuple[StepRecord, ...] = field(default_factory=tuple)
    requires_node_modules: bool = False
    worktrees_root: str | None = None
    canonical_repo_id: str | None = None
    forge: str = "gitlab"

    @classmethod
    def from_dict(cls, data: dict) -> RepoConfig:
        if not isinstance(data, dict):
            raise SystemExit("repos[] entries must be objects")
        name = str(data.get("name") or "").strip()
        main_repo = str(data.get("main_repo") or "").strip()
        raw_env = data.get("env_files")
        if not name:
            raise SystemExit("repos[] entry missing required 'name'")
        if not main_repo:
            raise SystemExit(
                f"repos[] entry {name!r} missing required 'main_repo'"
            )
        if not isinstance(raw_env, list) or not raw_env:
            raise SystemExit(
                f"repos[] entry {name!r} requires non-empty 'env_files'"
            )
        env_files = tuple(str(p) for p in raw_env if str(p).strip())
        if not env_files:
            raise SystemExit(
                f"repos[] entry {name!r} requires non-empty 'env_files'"
            )
        renames = data.get("renames") or {}
        if not isinstance(renames, dict):
            raise SystemExit(
                f"repos[] entry {name!r}: 'renames' must be an object"
            )
        raw_steps = data.get("steps") or []
        if not isinstance(raw_steps, list):
            raise SystemExit(
                f"repos[] entry {name!r}: 'steps' must be an array"
            )
        steps: list[StepRecord] = []
        for raw in raw_steps:
            if isinstance(raw, list):
                steps.append({"cmd": raw})
            elif isinstance(raw, dict) and "cmd" in raw:
                steps.append(dict(raw))
            else:
                raise SystemExit(
                    f"repos[] entry {name!r}: each step needs 'cmd'"
                )
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
            raise SystemExit("config.json must be a JSON object")
        raw_repos = data.get("repos")
        if not isinstance(raw_repos, list) or not raw_repos:
            raise SystemExit("config.json requires a non-empty 'repos' array")
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
    steps: list[StepRecord] = field(default_factory=list)
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
            "steps": list(self.steps),
        }
        if self.pr_id is not None:
            data["pr_id"] = self.pr_id
        if self.preview_url is not None:
            data["preview_url"] = self.preview_url
        return data

    @classmethod
    def from_dict(cls, data: dict) -> WorktreeRecord:
        # Legacy "status" field is ignored — derived from lease membership.
        return cls(
            key=str(data["key"]),
            repo=str(data.get("repo", "")),
            mode="preview" if data.get("mode") == "preview" else "dedicated",
            object=str(data.get("object", "")),
            created_at=str(data.get("created_at", "")),
            env_files=list(data.get("env_files") or []),
            steps=list(data.get("steps") or []),
            pr_id=data.get("pr_id"),
            preview_url=data.get("preview_url"),
        )


@dataclass(frozen=True)
class DropOp:
    """One drop identity for plan / resolve / reserve / execute.

    ``touch_postgres`` is authoritative: ``--forget-only`` is encoded when the
    op is created (False), not re-OR'd at execute time. ``paths`` is the
    forget-set hint (claim path and/or remint extras); reserve expands via
    ``path_for_key``.
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
    def from_dict(cls, key: str, data: dict | str) -> SlugLease:
        # Legacy: dropping[key] = worktree path string
        if isinstance(data, str):
            return cls(
                lease_id=0,
                key=key,
                op="drop",
                worktrees=(data,) if data else (),
                object_name="",
                touch_postgres=True,
                reserved_at="",
            )
        if not isinstance(data, dict):
            raise SystemExit(
                f"corrupt state.json: leases[{key!r}] must be an object"
            )
        raw_wts = data.get("worktrees") or []
        if not isinstance(raw_wts, list):
            raise SystemExit(
                f"corrupt state.json: leases[{key!r}].worktrees must be a list"
            )
        try:
            lease_id = int(data.get("lease_id", 0))
        except (TypeError, ValueError) as exc:
            raise SystemExit(
                f"corrupt state.json: leases[{key!r}].lease_id invalid"
            ) from exc
        raw_op = data.get("op") or "drop"
        if raw_op not in ("provision", "drop"):
            raise SystemExit(
                f"corrupt state.json: leases[{key!r}].op must be "
                "'provision' or 'drop'"
            )
        op: LeaseOp = "provision" if raw_op == "provision" else "drop"
        if "touch_postgres" in data:
            touch = bool(data["touch_postgres"])
        elif "skip_postgres" in data:
            # Legacy invert
            touch = not bool(data["skip_postgres"])
        else:
            touch = raw_op == "drop"
        return cls(
            lease_id=lease_id,
            key=key,
            op=op,
            worktrees=tuple(str(p) for p in raw_wts if str(p)),
            object_name=str(data.get("object_name") or ""),
            touch_postgres=touch,
            reserved_at=str(data.get("reserved_at") or ""),
        )


def migrate_legacy_state(raw: dict) -> dict:
    """One-shot normalize of legacy shapes into the canonical schema.

    - ``dropping`` (all op=drop) → ``leases``.
    - string lease values (``dropping[key] = worktree path``) → lease dicts.
    - ``skip_postgres`` → inverted ``touch_postgres``.

    Dataclass constructors parse only the modern schema after this runs;
    ``SlugLease.from_dict`` keeps its legacy branches as a second fence for
    direct callers.
    """
    if not isinstance(raw, dict):
        return raw
    data = dict(raw)
    leases = data.get("leases")
    if leases is None:
        legacy = data.get("dropping") or {}
        if isinstance(legacy, dict):
            converted: dict = {}
            for key, val in legacy.items():
                if isinstance(val, str):
                    converted[key] = {
                        "lease_id": 0,
                        "op": "drop",
                        "worktrees": [val] if val else [],
                        "object_name": "",
                        "touch_postgres": True,
                    }
                elif isinstance(val, dict):
                    entry = dict(val)
                    entry.setdefault("op", "drop")
                    if "touch_postgres" not in entry and "skip_postgres" in entry:
                        entry["touch_postgres"] = not bool(
                            entry.pop("skip_postgres")
                        )
                    converted[key] = entry
                else:
                    converted[key] = val
            data["leases"] = converted
            data.pop("dropping", None)
    return data


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
        data = migrate_legacy_state(data)
        raw = data.get("worktrees") or {}
        if not isinstance(raw, dict):
            raise SystemExit("corrupt state.json: 'worktrees' must be an object")
        worktrees: dict[str, WorktreeRecord] = {}
        by_key: dict[str, str] = {}
        for path, rec in raw.items():
            if not isinstance(rec, dict):
                raise SystemExit(
                    f"corrupt state.json: worktree {path!r} is not an object"
                )
            if not rec.get("key"):
                raise SystemExit(
                    f"corrupt state.json: worktree {path!r} missing 'key' "
                    "(fix or remove the row)"
                )
            path_s = str(path)
            record = WorktreeRecord.from_dict(rec)
            holder = by_key.get(record.key)
            if holder is not None:
                raise SystemExit(
                    f"corrupt state.json: key {record.key!r} claimed by both "
                    f"{holder!r} and {path_s!r}; drop/forget one row "
                    "(one key → one path)"
                )
            by_key[record.key] = path_s
            worktrees[path_s] = record
        # Prefer ``leases``; legacy ``dropping`` already migrated above.
        raw_leases = data.get("leases") or {}
        if raw_leases and not isinstance(raw_leases, dict):
            raise SystemExit(
                "corrupt state.json: 'leases'/'dropping' must be an object"
            )
        leases = {
            str(k): SlugLease.from_dict(str(k), v)
            for k, v in (raw_leases or {}).items()
        }
        try:
            next_id = int(data.get("next_lease_id") or 1)
        except (TypeError, ValueError) as exc:
            raise SystemExit(
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

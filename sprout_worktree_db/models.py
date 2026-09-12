"""Typed contracts for provision / drop / GC / config."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


Mode = Literal["dedicated", "preview"]
StepsStatus = Literal["ok", "skipped", "failed"]


@dataclass(frozen=True)
class EnvInjection:
    """Result of writing connection credentials into worktree env files."""

    object_name: str
    env_files: tuple[Path, ...]
    pr_id: int | None = None
    preview_url: str | None = None


@dataclass(frozen=True)
class RepoConfig:
    """One repos[] entry — validated at config load."""

    name: str
    main_repo: str
    env_files: tuple[str, ...]
    renames: dict[str, str] = field(default_factory=dict)
    steps: tuple[dict, ...] = field(default_factory=tuple)
    requires_node_modules: bool = False
    worktrees_root: str | None = None
    canonical_repo_id: str | None = None
    slug: str | None = None
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
        steps: list[dict] = []
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
            slug=str(data["slug"]) if data.get("slug") else None,
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
    steps: list[dict] = field(default_factory=list)
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


@dataclass
class PluginState:
    worktrees: dict[str, WorktreeRecord] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "worktrees": {
                path: rec.to_dict() for path, rec in self.worktrees.items()
            }
        }

    @classmethod
    def from_dict(cls, data: dict) -> PluginState:
        raw = data.get("worktrees") or {}
        if not isinstance(raw, dict):
            raise SystemExit("corrupt state.json: 'worktrees' must be an object")
        worktrees: dict[str, WorktreeRecord] = {}
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
            worktrees[str(path)] = WorktreeRecord.from_dict(rec)
        return cls(worktrees=worktrees)


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


@dataclass(frozen=True)
class DropPlan:
    """One planned teardown: drop Postgres object and optionally forget state."""

    key: str
    object_name: str
    reason: str
    state_path: str | None = None
    skip_drop: bool = False  # preview / forget-only

"""Typed contracts for provision / drop / GC."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


Mode = Literal["dedicated", "preview"]


@dataclass(frozen=True)
class EnvInjection:
    """Result of writing connection credentials into worktree env files."""

    object_name: str
    env_files: tuple[Path, ...]
    expected: dict[Path, dict[str, str]]
    shared_with_preview: bool = False
    pr_id: int | None = None
    preview_url: str | None = None


@dataclass
class WorktreeRecord:
    key: str
    repo: str
    mode: Mode
    object: str
    created_at: str
    env_files: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    shared_with_preview: bool = False
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
        if self.shared_with_preview:
            data["shared_with_preview"] = True
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
            shared_with_preview=bool(data.get("shared_with_preview")),
            pr_id=data.get("pr_id"),
            preview_url=data.get("preview_url"),
        )


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

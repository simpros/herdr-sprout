"""Resolve worktree path from herdr ≥ 0.8 event / context payloads."""

from __future__ import annotations

import json
import os
from typing import Any


def _path_from_worktree_obj(wt: Any) -> str | None:
    if not isinstance(wt, dict):
        return None
    # herdr ≥ 0.8 workspace worktree: prefer checkout_path, then path.
    for key in ("checkout_path", "path"):
        val = wt.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def resolve_worktree_path(
    event: dict | None = None,
    context: dict | None = None,
    env: dict[str, str] | None = None,
) -> str | None:
    """Extract worktree checkout path from the supported herdr ≥ 0.8 contract.

    Documented paths (first match wins):
      1. HERDR_WORKTREE env
      2. event.data.worktree.path          (worktree.created / removed)
      3. event.data.workspace.worktree.checkout_path | path
      4. context.worktree.checkout_path | path  (HERDR_PLUGIN_CONTEXT_JSON)

    Fails closed (returns None) when none of these are present.
    """
    environ = env if env is not None else os.environ
    direct = environ.get("HERDR_WORKTREE")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    if event is None:
        payload = environ.get("HERDR_PLUGIN_EVENT_JSON") or ""
        try:
            event = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            event = {}

    if context is None:
        ctx_raw = environ.get("HERDR_PLUGIN_CONTEXT_JSON") or ""
        try:
            context = json.loads(ctx_raw) if ctx_raw else {}
        except json.JSONDecodeError:
            context = {}

    if not isinstance(event, dict):
        event = {}
    if not isinstance(context, dict):
        context = {}

    inner = event.get("data") if isinstance(event.get("data"), dict) else event
    if isinstance(inner, dict):
        path = _path_from_worktree_obj(inner.get("worktree"))
        if path:
            return path
        workspace = inner.get("workspace")
        if isinstance(workspace, dict):
            path = _path_from_worktree_obj(workspace.get("worktree"))
            if path:
                return path

    path = _path_from_worktree_obj(context.get("worktree"))
    if path:
        return path
    return None


def event_worktree_path() -> str | None:
    """Convenience wrapper reading from process environment."""
    return resolve_worktree_path()

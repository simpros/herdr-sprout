"""Argparse + hook/action dispatch."""

from __future__ import annotations

import argparse
import json
import os

from sprout_worktree_db.event import event_worktree_path
from sprout_worktree_db.gc import gc
from sprout_worktree_db.gitutil import repo_config
from sprout_worktree_db.models import DropRequest, ProvisionRequest
from sprout_worktree_db.paths import load_config, load_secrets, log
from sprout_worktree_db.provision import do_drop, do_provision
from sprout_worktree_db.state import load_state

_DOC = """Per-herdr-worktree Postgres databases via sprout.

Dedicated mode (default): provisions `sprout_wt_<key>` (own DB + login role)
and injects the connection into the worktree's env files so parallel agents
stop sharing one dev database.

Preview mode (`attach-preview`): points the worktree at the database that the
branch's PR/MR preview deployment already uses (`sprout_<slug>_pr<id>`)
instead of creating a new one.

Modes:
  hook <created|removed>   driven by the herdr plugin event payload
  action <provision|…>     herdr action entrypoints (worktree from context)
  provision                provision + inject for a worktree (explicit path)
  attach-preview           reuse the PR's preview database
  drop                     drop the worktree database + role
  gc [--dry-run]           drop sprout_wt_* objects whose worktree is gone
  status                   list tracked worktrees and their databases

Config:  $HERDR_PLUGIN_CONFIG_DIR/config.json
Secrets: $HERDR_PLUGIN_CONFIG_DIR/secrets.env  (mode 0600)
State:   $HERDR_PLUGIN_STATE_DIR/state.json
"""


def status() -> int:
    state = load_state()
    rows = []
    for path, rec in sorted(state.worktrees.items()):
        steps = rec.steps
        if not steps:
            steps_ok = None
        elif any(s.get("skipped") for s in steps):
            steps_ok = False
        else:
            steps_ok = all(s.get("ok") for s in steps)
        rows.append(
            {
                "worktree": path,
                "exists": os.path.exists(path),
                "key": rec.key,
                "mode": rec.mode,
                "database": rec.object,
                "steps_ok": steps_ok,
            }
        )
    print(json.dumps({"worktrees": rows}, indent=2))
    return 0


def hook(event: str) -> int:
    try:
        cfg = load_config()
    except SystemExit as exc:
        log(f"hook {event}: {exc}")
        return 0
    secrets = load_secrets()
    path = event_worktree_path()
    if not path:
        log(f"hook {event}: no worktree path in event payload, skipping")
        return 0

    try:
        if event == "created":
            # Opt-in: competing .env-copy plugins have no ordering guarantee.
            # Default off — enable via config.auto_provision_on_create, or call
            # the ordered `provision` action after setup copies (see README).
            if not cfg.get("auto_provision_on_create", False):
                log(
                    "hook created: auto_provision_on_create disabled "
                    "(set true in config.json, or run ordered provision action)"
                )
                return 0
            if not repo_config(cfg, path):
                log(f"hook created: no repo config for {path}, skipping")
                return 0
            do_provision(
                cfg, secrets, ProvisionRequest(worktree=path, with_steps=True)
            )
        else:
            tracked = os.path.realpath(path) in load_state().worktrees
            if not tracked and not repo_config(cfg, path):
                log(f"hook removed: {path} not tracked, skipping")
                return 0
            do_drop(cfg, secrets, DropRequest(worktree=path))
    except SystemExit as exc:
        log(f"hook {event}: {exc}")
        return 1
    except Exception as exc:
        log(f"hook {event} FAILED: {type(exc).__name__}: {exc}")
        return 1
    return 0


def _provision_from_args(
    cfg: dict,
    secrets: dict,
    worktree: str,
    *,
    mode: str = "dedicated",
    key: str | None = None,
    with_steps: bool = True,
) -> int:
    do_provision(
        cfg,
        secrets,
        ProvisionRequest(
            worktree=worktree,
            mode="preview" if mode == "preview" else "dedicated",
            key=key,
            with_steps=with_steps,
        ),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sprout-worktree-db", description=_DOC
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    p_hook = sub.add_parser("hook", help="herdr plugin event entrypoint")
    p_hook.add_argument("event", choices=["created", "removed"])

    p_action = sub.add_parser("action", help="herdr action entrypoint")
    p_action.add_argument(
        "name", choices=["provision", "drop", "attach-preview"]
    )
    p_action.add_argument(
        "--no-steps",
        action="store_true",
        help="skip repo post-steps (migrate/bootstrap)",
    )

    p_prov = sub.add_parser(
        "provision", help="provision + inject a dedicated DB"
    )
    p_prov.add_argument("--worktree", required=True)
    p_prov.add_argument("--key")
    p_prov.add_argument(
        "--no-steps",
        action="store_true",
        help="skip repo post-steps (migrate/bootstrap)",
    )

    p_prev = sub.add_parser(
        "attach-preview", help="reuse the PR/MR preview database"
    )
    p_prev.add_argument("--worktree", required=True)
    p_prev.add_argument("--key")

    p_drop = sub.add_parser("drop", help="drop the worktree DB + role")
    p_drop.add_argument("--worktree")
    p_drop.add_argument("--key")
    p_drop.add_argument("--forget-only", action="store_true")

    p_gc = sub.add_parser(
        "gc", help="drop worktree DBs whose worktree is gone"
    )
    p_gc.add_argument("--dry-run", action="store_true")

    sub.add_parser("status", help="list tracked worktrees")

    args = parser.parse_args(argv)

    if args.mode == "hook":
        return hook(args.event)

    cfg = load_config()
    secrets = load_secrets()

    if args.mode == "action":
        path = event_worktree_path()
        if not path:
            raise SystemExit(
                "no worktree in HERDR_PLUGIN_CONTEXT_JSON / event payload"
            )
        if args.name == "provision":
            return _provision_from_args(
                cfg,
                secrets,
                path,
                with_steps=not args.no_steps,
            )
        if args.name == "drop":
            do_drop(cfg, secrets, DropRequest(worktree=path))
            return 0
        return _provision_from_args(
            cfg, secrets, path, mode="preview", with_steps=False
        )

    if args.mode == "provision":
        return _provision_from_args(
            cfg,
            secrets,
            args.worktree,
            key=args.key,
            with_steps=not args.no_steps,
        )
    if args.mode == "attach-preview":
        return _provision_from_args(
            cfg,
            secrets,
            args.worktree,
            mode="preview",
            key=args.key,
            with_steps=False,
        )
    if args.mode == "drop":
        do_drop(
            cfg,
            secrets,
            DropRequest(
                worktree=args.worktree,
                key=args.key,
                forget_only=args.forget_only,
            ),
        )
        return 0
    if args.mode == "gc":
        return gc(cfg, secrets, dry_run=args.dry_run)
    if args.mode == "status":
        return status()
    return 2

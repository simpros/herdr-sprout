#!/usr/bin/env python3
"""Canonical state-schema tests (strict 0.1.0 shape, no legacy migration)."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from _helpers import ROOT  # noqa: F401 — ROOT ensures sys.path

from sprout_worktree_db import state
from sprout_worktree_db.errors import (
    BusyError,
    ConfigError,
    CorruptStateError,
    PluginError,
    SproutError,
)
from sprout_worktree_db.models import PluginState, SlugLease


def _modern_lease(**overrides):
    base = {
        "lease_id": 1,
        "op": "drop",
        "worktrees": ["/wt"],
        "object_name": "sprout_wt_x",
        "touch_postgres": True,
        "reserved_at": "2099-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


class StateSchemaTest(unittest.TestCase):
    def test_duplicate_key_rejected_on_load(self):
        """One key → one path: corrupt multi-path state fails closed."""
        with self.assertRaises(SystemExit) as ctx:
            PluginState.from_dict(
                {
                    "worktrees": {
                        "/a": {
                            "key": "dup",
                            "repo": "app",
                            "mode": "dedicated",
                            "object": "sprout_wt_dup",
                            "created_at": "",
                        },
                        "/b": {
                            "key": "dup",
                            "repo": "app",
                            "mode": "dedicated",
                            "object": "sprout_wt_dup",
                            "created_at": "",
                        },
                    }
                }
            )
        self.assertIn("claimed by both", str(ctx.exception))

    def test_dropping_key_rejected(self):
        """Pre-0.1.0 `dropping` fails closed — no silent merge, no drop."""
        with self.assertRaises(CorruptStateError) as ctx:
            PluginState.from_dict(
                {
                    "worktrees": {},
                    "leases": {},
                    "dropping": {"k": "/wt"},
                }
            )
        self.assertIn("dropping", str(ctx.exception))

    def test_string_lease_rejected(self):
        with self.assertRaises(CorruptStateError) as ctx:
            PluginState.from_dict({"worktrees": {}, "leases": {"a": "/wt"}})
        self.assertIn("must be an object", str(ctx.exception))

    def test_missing_op_rejected(self):
        """A provision lease missing `op` must not be rewritten as a drop."""
        lease = _modern_lease()
        del lease["op"]
        with self.assertRaises(CorruptStateError) as ctx:
            PluginState.from_dict({"worktrees": {}, "leases": {"b": lease}})
        self.assertIn(".op", str(ctx.exception))

    def test_invalid_op_rejected(self):
        lease = _modern_lease(op="forget")
        with self.assertRaises(CorruptStateError):
            PluginState.from_dict({"worktrees": {}, "leases": {"b": lease}})

    def test_skip_postgres_rejected_not_inverted(self):
        """`skip_postgres` is not a schema field anymore — no silent invert."""
        lease = _modern_lease(skip_postgres=True)
        del lease["touch_postgres"]
        with self.assertRaises(CorruptStateError) as ctx:
            PluginState.from_dict({"worktrees": {}, "leases": {"c": lease}})
        self.assertIn("touch_postgres", str(ctx.exception))

    def test_modern_missing_touch_is_corrupt(self):
        with self.assertRaises(SystemExit):
            PluginState.from_dict(
                {
                    "worktrees": {},
                    "leases": {
                        "x": {
                            "lease_id": 1,
                            "op": "drop",
                            "worktrees": [],
                        }
                    },
                }
            )

    def test_leases_not_object_rejected(self):
        with self.assertRaises(CorruptStateError):
            PluginState.from_dict({"worktrees": {}, "leases": ["x"]})

    def test_canonical_round_trip(self):
        st = PluginState.from_dict(
            {
                "worktrees": {
                    "/wt": {
                        "key": "app-x",
                        "repo": "app",
                        "mode": "dedicated",
                        "object": "sprout_wt_app_x",
                        "created_at": "",
                    }
                },
                "leases": {"app-x": _modern_lease()},
                "next_lease_id": 2,
            }
        )
        st2 = PluginState.from_dict(st.to_dict())
        self.assertEqual(st2.leases["app-x"].touch_postgres, True)
        self.assertEqual(st2.next_lease_id, 2)

    def test_locked_state_persists_reclaim_on_refusal(self):
        """Reclaim survives a refused op: lock exit always saves."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                key = "app-stale-abc12"
                st = PluginState(
                    worktrees={},
                    leases={
                        key: SlugLease(
                            lease_id=1,
                            key=key,
                            op="drop",
                            worktrees=(),
                            object_name="sprout_wt_app_stale_abc12",
                            reserved_at="2000-01-01T00:00:00+00:00",
                        )
                    },
                    next_lease_id=2,
                )
                state.save_state(st)
                with self.assertRaises(SystemExit):
                    with state.locked_state() as locked:
                        cleared = state.reclaim_expired_leases(locked)
                        self.assertIn(key, cleared)
                        raise SystemExit("busy: drop in progress")
                self.assertNotIn(key, state.load_state().leases)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_legacy_state_path_migrates_once(self):
        """state.json resolves through the shared legacy file migrator."""
        import sprout_worktree_db.paths as paths

        with tempfile.TemporaryDirectory() as tmp:
            primary_dir = Path(tmp) / "primary"
            legacy_dir = Path(tmp) / "legacy"
            primary_dir.mkdir()
            legacy_dir.mkdir()
            legacy_file = legacy_dir / "worktree-db-state.json"
            legacy_file.write_text(json.dumps({"worktrees": {}}))
            with (
                mock.patch.object(
                    paths, "state_dir", return_value=primary_dir
                ),
                mock.patch.object(
                    paths, "LEGACY_CONFIG_DIR", legacy_dir
                ),
            ):
                # Getters stay pure — migration runs at the CLI boundary,
                # and only when HERDR_PLUGIN_STATE_DIR is the default.
                self.assertEqual(
                    paths.state_path(), primary_dir / "state.json"
                )
                self.assertFalse((primary_dir / "state.json").exists())
                saved = os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)
                try:
                    paths.migrate_legacy_files()
                finally:
                    if saved is not None:
                        os.environ["HERDR_PLUGIN_STATE_DIR"] = saved
                self.assertTrue((primary_dir / "state.json").exists())
                # Second migrate is a no-op (primary wins, legacy kept).
                saved = os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)
                try:
                    paths.migrate_legacy_files()
                finally:
                    if saved is not None:
                        os.environ["HERDR_PLUGIN_STATE_DIR"] = saved
                self.assertTrue(legacy_file.exists())

    def test_override_dir_is_sole_source(self):
        """Explicit HERDR_PLUGIN_*_DIR never imports the home legacy tree."""
        import sprout_worktree_db.paths as paths

        with tempfile.TemporaryDirectory() as tmp:
            legacy_dir = Path(tmp) / "legacy"
            legacy_dir.mkdir()
            (legacy_dir / "worktree-db-state.json").write_text(
                json.dumps({"worktrees": {}})
            )
            override = Path(tmp) / "override"
            override.mkdir()
            with (
                mock.patch.object(
                    paths, "LEGACY_CONFIG_DIR", legacy_dir
                ),
                mock.patch.dict(
                    os.environ, {"HERDR_PLUGIN_STATE_DIR": str(override)}
                ),
            ):
                self.assertEqual(paths.state_path(), override / "state.json")
                paths.migrate_legacy_files()
                self.assertFalse((override / "state.json").exists())


class ErrorHierarchyTest(unittest.TestCase):
    def test_domain_errors_stay_system_exit_compatible(self):
        """PluginError subclasses SystemExit: old catches keep working."""
        for cls in (BusyError, ConfigError, CorruptStateError, SproutError):
            self.assertTrue(issubclass(cls, PluginError))
            with self.assertRaises(SystemExit):
                raise cls("boom")

    def test_domain_errors_distinguishable_from_interpreter_abort(self):
        with self.assertRaises(PluginError):
            raise BusyError("key 'x': drop in progress")
        # A bare interpreter abort (e.g. argparse exit 2) is NOT a PluginError.
        caught_plugin_error = False
        try:
            raise SystemExit(2)
        except PluginError:
            caught_plugin_error = True
        except SystemExit:
            pass
        self.assertFalse(caught_plugin_error)

    def test_busy_message_preserved(self):
        try:
            raise BusyError("key 'x': drop in progress")
        except SystemExit as exc:
            self.assertIn("in progress", str(exc))


class MainMapsPluginErrorTest(unittest.TestCase):
    def test_corrupt_state_maps_to_exit_1(self):
        """CLI maps PluginError to `error:` on stderr + exit 1 (not a traceback)."""
        from sprout_worktree_db.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "cfg"
            cfg_dir.mkdir()
            (cfg_dir / "config.json").write_text(
                json.dumps(
                    {
                        "repos": [
                            {
                                "name": "a",
                                "main_repo": "/tmp/x",
                                "env_files": [".env"],
                            }
                        ]
                    }
                )
            )
            (Path(tmp) / "state.json").write_text(
                json.dumps({"worktrees": {}, "leases": {"a": "/wt"}})
            )
            os.environ["HERDR_PLUGIN_CONFIG_DIR"] = str(cfg_dir)
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                err = io.StringIO()
                with redirect_stderr(err):
                    rc = main(["status"])
                self.assertEqual(rc, 1)
                self.assertIn("must be an object", err.getvalue())
            finally:
                del os.environ["HERDR_PLUGIN_CONFIG_DIR"]
                del os.environ["HERDR_PLUGIN_STATE_DIR"]


if __name__ == "__main__":
    unittest.main()

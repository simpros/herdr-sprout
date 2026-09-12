#!/usr/bin/env python3
"""Unit tests for sprout_worktree_db (no Postgres required)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "sprout-worktree-db"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sprout_worktree_db import envfile, event, gc as gc_mod, state  # noqa: E402
from sprout_worktree_db.models import PluginState, WorktreeRecord  # noqa: E402
from sprout_worktree_db.state import (  # noqa: E402
    mint_key,
    normalize_key,
    object_name,
    resolve_key,
    stable_suffix,
)
from sprout_worktree_db import steps as steps_mod  # noqa: E402


class NormalizeKeyTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(normalize_key("My_Feature Branch!"), "my-feature-branch")
        self.assertEqual(normalize_key("---Foo---"), "foo")
        self.assertEqual(normalize_key("a--b"), "a-b")

    def test_max_len(self):
        long = "a" * 50
        key = normalize_key(long)
        assert key is not None
        self.assertLessEqual(len(key), 40)

    def test_empty(self):
        self.assertIsNone(normalize_key(""))
        self.assertIsNone(normalize_key("!!!"))

    def test_object_name(self):
        self.assertEqual(object_name("my-feature"), "sprout_wt_my_feature")


class MergeEnvFileTest(unittest.TestCase):
    def test_atomic_merge_and_preserve(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("KEEP=1\nPGHOST=old\n# comment\n")
            envfile.merge_env_file(path, {"PGHOST": "new", "PGPORT": "5432"})
            text = path.read_text()
            self.assertIn("KEEP=1\n", text)
            self.assertIn("PGHOST=new\n", text)
            self.assertIn("PGPORT=5432\n", text)
            self.assertIn("# comment\n", text)
            self.assertNotIn("PGHOST=old", text)


class MintKeyTest(unittest.TestCase):
    def test_always_includes_stable_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            wt = str(Path(tmp) / "feature")
            key = mint_key(wt, {"name": "myapp"})
            self.assertEqual(key, f"myapp-feature-{stable_suffix(wt)}")
            self.assertLessEqual(len(key), 40)
            # Same path → same key; no collision branching.
            self.assertEqual(key, mint_key(wt, {"name": "myapp"}))

    def test_distinct_paths_distinct_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = str(Path(tmp) / "a" / "feature")
            b = str(Path(tmp) / "b" / "feature")
            Path(a).parent.mkdir(parents=True)
            Path(b).parent.mkdir(parents=True)
            self.assertNotEqual(
                mint_key(a, {"name": "repo"}),
                mint_key(b, {"name": "repo"}),
            )

    def test_resolve_prefers_existing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            wt = str(Path(tmp) / "feature")
            st = PluginState(
                worktrees={
                    wt: WorktreeRecord(
                        key="legacy-bare-key",
                        repo="myapp",
                        mode="dedicated",
                        object="sprout_wt_legacy_bare_key",
                        created_at="2020-01-01T00:00:00+00:00",
                    )
                }
            )
            self.assertEqual(
                resolve_key(st, wt, {"name": "myapp"}),
                "legacy-bare-key",
            )
            self.assertEqual(
                resolve_key(st, wt, {"name": "myapp"}, requested="forced"),
                "forced",
            )


class PlanOrphansTest(unittest.TestCase):
    def test_stale_state_and_postgres_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "live"
            live.mkdir()
            gone = str(Path(tmp) / "gone")
            state_data = PluginState(
                worktrees={
                    str(live): WorktreeRecord(
                        key="app-live",
                        repo="app",
                        mode="dedicated",
                        object="sprout_wt_app_live",
                        created_at="",
                    ),
                    gone: WorktreeRecord(
                        key="app-gone",
                        repo="app",
                        mode="dedicated",
                        object="sprout_wt_app_gone",
                        created_at="",
                    ),
                    str(Path(tmp) / "preview-gone"): WorktreeRecord(
                        key="app-prev",
                        repo="app",
                        mode="preview",
                        object="sprout_shared_pr1",
                        created_at="",
                    ),
                }
            )
            live_paths = {os.path.realpath(str(live))}
            postgres = [
                "sprout_wt_app_live",
                "sprout_wt_app_gone",
                "sprout_wt_orphan_only",
            ]
            plans = gc_mod.plan_orphans(state_data, live_paths, postgres)
            by_obj = {p.object_name: p for p in plans}
            self.assertIn("sprout_wt_app_gone", by_obj)
            self.assertFalse(by_obj["sprout_wt_app_gone"].skip_drop)
            self.assertIn("sprout_wt_orphan_only", by_obj)
            preview = next(p for p in plans if p.skip_drop and p.state_path)
            self.assertTrue(preview.skip_drop)
            self.assertNotIn("sprout_wt_app_live", by_obj)

    def test_live_objects_from_state_not_basename(self):
        """Disambiguated keys must come from state, not Path.name guesses."""
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "feature"
            wt.mkdir()
            state_data = PluginState(
                worktrees={
                    str(wt): WorktreeRecord(
                        key="repo-feature-abc12",
                        repo="repo",
                        mode="dedicated",
                        object="sprout_wt_repo_feature_abc12",
                        created_at="",
                    )
                }
            )
            live = gc_mod.live_objects_from_state(
                state_data, {os.path.realpath(str(wt))}
            )
            self.assertEqual(live, {"sprout_wt_repo_feature_abc12"})
            self.assertNotIn("sprout_wt_feature", live)


class EventPathTest(unittest.TestCase):
    def test_herdr_worktree_env(self):
        path = event.resolve_worktree_path(
            event={}, context={}, env={"HERDR_WORKTREE": "/wt/a"}
        )
        self.assertEqual(path, "/wt/a")

    def test_event_data_worktree_path(self):
        path = event.resolve_worktree_path(
            event={"data": {"worktree": {"path": "/wt/b"}}},
            context={},
            env={},
        )
        self.assertEqual(path, "/wt/b")

    def test_workspace_checkout_path(self):
        path = event.resolve_worktree_path(
            event={
                "data": {
                    "workspace": {
                        "worktree": {"checkout_path": "/wt/c"}
                    }
                }
            },
            context={},
            env={},
        )
        self.assertEqual(path, "/wt/c")

    def test_context_fallback(self):
        path = event.resolve_worktree_path(
            event={},
            context={"worktree": {"path": "/wt/d"}},
            env={},
        )
        self.assertEqual(path, "/wt/d")

    def test_fail_closed(self):
        self.assertIsNone(
            event.resolve_worktree_path(event={}, context={}, env={})
        )


class StepsSkipTest(unittest.TestCase):
    def test_missing_node_modules_is_not_ok(self):
        results = steps_mod.run_steps(
            {},
            {"SPROUT_WORKTREE_ADMIN_URL": "postgres://u:p@h/db"},
            {"requires_node_modules": True, "steps": [["echo", "hi"]]},
            "/tmp/nonexistent-worktree-xyz",
        )
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertIn("skipped", results[0])


class DropKeyResolveTest(unittest.TestCase):
    def test_fail_closed_without_state_or_repo(self):
        from sprout_worktree_db.provision import _resolve_drop_key

        with self.assertRaises(SystemExit) as ctx:
            _resolve_drop_key(
                {"repos": []},
                PluginState(),
                "/tmp/ghost-wt",
                None,
                None,
            )
        self.assertIn("--key", str(ctx.exception))

    def test_remint_when_repo_known(self):
        from sprout_worktree_db.provision import _resolve_drop_key

        with tempfile.TemporaryDirectory() as tmp:
            wt = str(Path(tmp) / "feature")
            Path(wt).mkdir()
            main = str(Path(tmp) / "main")
            Path(main).mkdir()
            cfg = {
                "repos": [
                    {
                        "name": "myapp",
                        "main_repo": main,
                        "env_files": [".env"],
                    }
                ]
            }
            # repo_config matches via worktrees under main — stub it.
            with mock.patch(
                "sprout_worktree_db.provision.repo_config",
                return_value={"name": "myapp", "env_files": [".env"]},
            ):
                key = _resolve_drop_key(cfg, PluginState(), wt, None, None)
            self.assertEqual(key, mint_key(wt, {"name": "myapp"}))


class ConfigDirTest(unittest.TestCase):
    def test_env_override(self):
        from sprout_worktree_db import paths

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_CONFIG_DIR"] = tmp
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp + "-state"
            try:
                self.assertEqual(paths.config_dir(), Path(tmp))
                self.assertEqual(paths.state_dir(), Path(tmp + "-state"))
            finally:
                del os.environ["HERDR_PLUGIN_CONFIG_DIR"]
                del os.environ["HERDR_PLUGIN_STATE_DIR"]


class ManifestTest(unittest.TestCase):
    def test_manifest_exists(self):
        text = (ROOT / "herdr-plugin.toml").read_text()
        self.assertIn('id = "sprout.worktree-db"', text)
        self.assertIn('on = "worktree.created"', text)
        self.assertIn('on = "worktree.removed"', text)
        self.assertIn('id = "provision"', text)


class CliHelpTest(unittest.TestCase):
    def test_help(self):
        rc = subprocess.run(
            [sys.executable, str(SCRIPT), "-h"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(rc.returncode, 0)
        self.assertIn("provision", rc.stdout)
        self.assertNotIn("--settle", rc.stdout)


class PackageLayoutTest(unittest.TestCase):
    def test_bin_is_thin_shim(self):
        text = SCRIPT.read_text()
        self.assertLess(len(text.splitlines()), 40)
        self.assertIn("sprout_worktree_db.cli", text)

    def test_no_settle_in_package(self):
        for path in (ROOT / "sprout_worktree_db").rglob("*.py"):
            src = path.read_text()
            self.assertNotIn("reassert_env_files", src, msg=str(path))
            self.assertNotIn("DEFAULT_SETTLE", src, msg=str(path))
            self.assertNotIn("time.sleep", src, msg=str(path))

    def test_no_expected_on_env_injection(self):
        from sprout_worktree_db.models import EnvInjection

        self.assertNotIn("expected", EnvInjection.__dataclass_fields__)
        self.assertNotIn(
            "shared_with_preview", EnvInjection.__dataclass_fields__
        )


if __name__ == "__main__":
    unittest.main()

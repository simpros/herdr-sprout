#!/usr/bin/env python3
"""Unit tests for sprout_worktree_db (no Postgres required)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "sprout-worktree-db"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sprout_worktree_db import envfile, event, gc as gc_mod, state  # noqa: E402
from sprout_worktree_db.state import normalize_key, object_name, pick_key, stable_suffix  # noqa: E402


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


class PickKeyTest(unittest.TestCase):
    def test_repo_qualified_basename(self):
        key = pick_key({"worktrees": {}}, "/tmp/feature", {"name": "myapp"})
        self.assertEqual(key, "myapp-feature")

    def test_stable_collision_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            holder = Path(tmp) / "holder"
            holder.mkdir()
            wt = str(Path(tmp) / "feature")
            state_data = {
                "worktrees": {
                    str(holder): {"key": "repo-feature"},
                }
            }
            key = pick_key(state_data, wt, {"name": "repo"})
            self.assertTrue(key.startswith("repo-feature-"))
            self.assertEqual(key, f"repo-feature-{stable_suffix(wt)}")
            # Same path → same suffix across processes / hash seeds.
            self.assertEqual(
                key, pick_key(state_data, wt, {"name": "repo"})
            )


class PlanOrphansTest(unittest.TestCase):
    def test_stale_state_and_postgres_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "live"
            live.mkdir()
            gone = str(Path(tmp) / "gone")
            state_data = {
                "worktrees": {
                    str(live): {
                        "key": "app-live",
                        "object": "sprout_wt_app_live",
                        "mode": "dedicated",
                    },
                    gone: {
                        "key": "app-gone",
                        "object": "sprout_wt_app_gone",
                        "mode": "dedicated",
                    },
                    str(Path(tmp) / "preview-gone"): {
                        "key": "app-prev",
                        "object": "sprout_shared_pr1",
                        "mode": "preview",
                    },
                }
            }
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
            # Live worktree's DB must not be planned as orphan.
            self.assertNotIn("sprout_wt_app_live", by_obj)

    def test_live_objects_from_state_not_basename(self):
        """Disambiguated keys must come from state, not Path.name guesses."""
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "feature"
            wt.mkdir()
            state_data = {
                "worktrees": {
                    str(wt): {
                        "key": "repo-feature-abc12",
                        "object": "sprout_wt_repo_feature_abc12",
                        "mode": "dedicated",
                    }
                }
            }
            live = gc_mod.live_objects_from_state(
                state_data, {os.path.realpath(str(wt))}
            )
            self.assertEqual(live, {"sprout_wt_repo_feature_abc12"})
            # Basename-only guess would be sprout_wt_feature — must not appear.
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


if __name__ == "__main__":
    unittest.main()

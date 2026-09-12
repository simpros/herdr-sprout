#!/usr/bin/env python3
"""Unit tests for sprout-worktree-db helpers (no Postgres required)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "sprout-worktree-db"


def load_mod():
    spec = importlib.util.spec_from_file_location(
        "sprout_worktree_db",
        SCRIPT,
        loader=importlib.machinery.SourceFileLoader(
            "sprout_worktree_db", str(SCRIPT)
        ),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sprout_worktree_db"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = load_mod()


class NormalizeKeyTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(mod.normalize_key("My_Feature Branch!"), "my-feature-branch")
        self.assertEqual(mod.normalize_key("---Foo---"), "foo")
        self.assertEqual(mod.normalize_key("a--b"), "a-b")

    def test_max_len(self):
        long = "a" * 50
        key = mod.normalize_key(long)
        assert key is not None
        self.assertLessEqual(len(key), 40)

    def test_empty(self):
        self.assertIsNone(mod.normalize_key(""))
        self.assertIsNone(mod.normalize_key("!!!"))

    def test_object_name(self):
        self.assertEqual(mod.object_name("my-feature"), "sprout_wt_my_feature")


class MergeEnvFileTest(unittest.TestCase):
    def test_atomic_merge_and_preserve(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("KEEP=1\nPGHOST=old\n# comment\n")
            mod.merge_env_file(path, {"PGHOST": "new", "PGPORT": "5432"})
            text = path.read_text()
            self.assertIn("KEEP=1\n", text)
            self.assertIn("PGHOST=new\n", text)
            self.assertIn("PGPORT=5432\n", text)
            self.assertIn("# comment\n", text)
            self.assertNotIn("PGHOST=old", text)

    def test_reassert_detects_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("DATABASE_HOST=wt\nOTHER=1\n")
            expected = {str(path): {"DATABASE_HOST": "wt"}}
            # Simulate competing copy after a tiny settle of 0.
            path.write_text("DATABASE_HOST=shared\nOTHER=1\n")
            mod.reassert_env_files([path], expected, settle=0.01)
            self.assertIn("DATABASE_HOST=wt\n", path.read_text())


class PickKeyTest(unittest.TestCase):
    def test_disambiguates_collision(self):
        state = {
            "worktrees": {
                "/other/wt-a": {"key": "wt-a"},
            }
        }
        # Create a live holder path so collision triggers.
        with tempfile.TemporaryDirectory() as tmp:
            holder = Path(tmp) / "holder"
            holder.mkdir()
            state["worktrees"][str(holder)] = {"key": "feature"}
            key = mod.pick_key(
                state,
                str(Path(tmp) / "feature"),
                {"name": "repo"},
            )
            self.assertTrue(key.startswith("feature-"))
            self.assertNotEqual(key, "feature")


class ConfigDirTest(unittest.TestCase):
    def test_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_CONFIG_DIR"] = tmp
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp + "-state"
            try:
                self.assertEqual(mod.config_dir(), Path(tmp))
                self.assertEqual(mod.state_dir(), Path(tmp + "-state"))
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
        import subprocess

        rc = subprocess.run(
            [sys.executable, str(SCRIPT), "-h"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(rc.returncode, 0)
        self.assertIn("provision", rc.stdout)


if __name__ == "__main__":
    unittest.main()

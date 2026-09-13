#!/usr/bin/env python3
"""Unit tests for sprout_worktree_db (no Postgres required)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _helpers import ROOT, SCRIPT, repo

from sprout_worktree_db import envfile, state  # noqa: E402
from sprout_worktree_db.models import (  # noqa: E402
    PluginConfig,
    PluginState,
    RepoConfig,
    WorktreeRecord,
)
from sprout_worktree_db.keys import (  # noqa: E402
    mint_key,
    normalize_key,
    object_name,
    postgres_target,
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

    def test_postgres_target_unified(self):
        """--key and --worktree must agree for the same claim."""
        preview = WorktreeRecord(
            key="app-prev",
            repo="app",
            mode="preview",
            object="",
            created_at="",
        )
        self.assertEqual(postgres_target(preview, "app-prev"), ("", False))
        dedicated = WorktreeRecord(
            key="app-feat",
            repo="app",
            mode="dedicated",
            object="",
            created_at="",
        )
        self.assertEqual(
            postgres_target(dedicated, "app-feat"),
            ("sprout_wt_app_feat", True),
        )
        self.assertEqual(
            postgres_target(None, "remint-key"),
            ("sprout_wt_remint_key", True),
        )


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
            key = mint_key(wt, repo("myapp"))
            self.assertEqual(key, f"myapp-feature-{stable_suffix(wt)}")
            self.assertLessEqual(len(key), 40)
            # Same path → same key; no collision branching.
            self.assertEqual(key, mint_key(wt, repo("myapp")))

    def test_distinct_paths_distinct_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = str(Path(tmp) / "a" / "feature")
            b = str(Path(tmp) / "b" / "feature")
            Path(a).parent.mkdir(parents=True)
            Path(b).parent.mkdir(parents=True)
            self.assertNotEqual(
                mint_key(a, repo("repo")),
                mint_key(b, repo("repo")),
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
                resolve_key(st, wt, repo("myapp")),
                "legacy-bare-key",
            )
            # Matching --key after normalize is accepted.
            self.assertEqual(
                resolve_key(st, wt, repo("myapp"), requested="Legacy-Bare-Key"),
                "legacy-bare-key",
            )

    def test_resolve_rejects_conflicting_requested(self):
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
            with self.assertRaises(SystemExit) as ctx:
                resolve_key(st, wt, repo("myapp"), requested="forced")
            self.assertIn("already claimed", str(ctx.exception))

    def test_resolve_normalizes_first_mint_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            wt = str(Path(tmp) / "feature")
            self.assertEqual(
                resolve_key(PluginState(), wt, repo("myapp"), requested="Foo_Bar"),
                "foo-bar",
            )


class StepsSkipTest(unittest.TestCase):
    def test_missing_node_modules_is_not_ok(self):
        results = steps_mod.run_steps(
            PluginConfig(repos=(repo(requires_node_modules=True, steps=(
                {"cmd": ["echo", "hi"]},
            )),)),
            {"SPROUT_WORKTREE_ADMIN_URL": "postgres://u:p@h/db"},
            repo(requires_node_modules=True, steps=({"cmd": ["echo", "hi"]},)),
            "/tmp/nonexistent-worktree-xyz",
        )
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertIn("skipped", results[0])
        self.assertEqual(steps_mod.steps_status(results), "skipped")

    def test_steps_status_ternary(self):
        self.assertIsNone(steps_mod.steps_status([]))
        self.assertEqual(
            steps_mod.steps_status([{"ok": True}]),
            "ok",
        )
        self.assertEqual(
            steps_mod.steps_status([{"ok": False, "error": "boom"}]),
            "failed",
        )
        self.assertEqual(
            steps_mod.steps_status(
                [{"ok": False, "skipped": "node_modules missing"}]
            ),
            "skipped",
        )


class PluginConfigTest(unittest.TestCase):
    def test_requires_env_files(self):
        with self.assertRaises(SystemExit) as ctx:
            RepoConfig.from_dict(
                {"name": "x", "main_repo": "/r", "env_files": []}
            )
        self.assertIn("env_files", str(ctx.exception))

    def test_requires_name_and_main(self):
        with self.assertRaises(SystemExit):
            RepoConfig.from_dict({"main_repo": "/r", "env_files": [".env"]})
        with self.assertRaises(SystemExit):
            RepoConfig.from_dict({"name": "x", "env_files": [".env"]})

    def test_corrupt_state_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            PluginState.from_dict(
                {"worktrees": {"/wt": {"object": "sprout_wt_x"}}}
            )
        self.assertIn("missing 'key'", str(ctx.exception))

    def test_unreadable_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                path = Path(tmp) / "state.json"
                path.write_text("{")
                with self.assertRaises(SystemExit) as ctx:
                    state.load_state()
                self.assertIn("corrupt state.json", str(ctx.exception))
                # File must be unchanged (no empty wipe via locked_state).
                self.assertEqual(path.read_text(), "{")
                with self.assertRaises(SystemExit):
                    with state.locked_state():
                        pass
                self.assertEqual(path.read_text(), "{")
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_non_object_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                path = Path(tmp) / "state.json"
                path.write_text("[1,2,3]\n")
                with self.assertRaises(SystemExit) as ctx:
                    state.load_state()
                self.assertIn("root must be an object", str(ctx.exception))
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]


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
        self.assertIn("pending_env", EnvInjection.__dataclass_fields__)
        # Required field: constructing without pending_env must fail.
        with self.assertRaises(TypeError):
            EnvInjection(object_name="x", env_files=())  # type: ignore[call-arg]

    def test_no_drop_lease_alias(self):
        import sprout_worktree_db.models as models

        self.assertFalse(hasattr(models, "DropLease"))

    def test_dedicated_scratch_outside_worktree(self):
        """Scratch env must use system temp, not the checkout."""
        from sprout_worktree_db.models import PluginConfig
        from sprout_worktree_db.sprout import provision_dedicated

        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "feature"
            wt.mkdir()
            seen: list[str] = []

            def fake_cli(cfg, secrets, argv):
                env_idx = argv.index("--env-file") + 1
                scratch = Path(argv[env_idx])
                seen.append(str(scratch))
                self.assertTrue(scratch.exists())
                # Must not live under the worktree.
                self.assertFalse(
                    str(scratch.resolve()).startswith(str(wt.resolve()))
                )
                scratch.write_text("DATABASE_URL=postgres://x\n")
                return 0, json.dumps({"object_name": "sprout_wt_k"}), ""

            cfg = PluginConfig(repos=(repo("myapp"),))
            with mock.patch(
                "sprout_worktree_db.sprout.sprout_cli", side_effect=fake_cli
            ), mock.patch(
                "sprout_worktree_db.sprout.require_admin_url",
                return_value="postgres://admin",
            ):
                inj = provision_dedicated(
                    cfg, {}, repo("myapp"), str(wt), "myapp-feature-abc12"
                )
            self.assertEqual(len(seen), 1)
            self.assertFalse(Path(seen[0]).exists())  # cleaned up
            self.assertEqual(inj.pending_env.get("DATABASE_URL"), "postgres://x")
            leftovers = list(wt.glob(".sprout-provision-*"))
            self.assertEqual(leftovers, [])

    def test_attach_preview_requires_canonical_not_slug(self):
        """Slug-only match must not attach when canonical differs."""
        from sprout_worktree_db.models import PluginConfig
        from sprout_worktree_db.sprout import attach_preview

        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "feature"
            wt.mkdir()
            previews = {
                "previews": [
                    {
                        "pr_id": 7,
                        "canonical_repo_id": "other/repo",
                        "slug": "myapp",
                        "db_name": "sprout_other_pr7",
                        "hostname": "https://preview.example",
                    }
                ]
            }
            cfg = PluginConfig(
                repos=(
                    repo(
                        "myapp",
                        canonical_repo_id="org/myapp",
                    ),
                )
            )
            with mock.patch(
                "sprout_worktree_db.sprout.branch_of", return_value="feat"
            ), mock.patch(
                "sprout_worktree_db.sprout.resolve_pr", return_value=7
            ), mock.patch(
                "sprout_worktree_db.sprout.sprout_cli",
                return_value=(0, json.dumps(previews), ""),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    attach_preview(
                        cfg,
                        {"SPROUT_PREVIEW_OWNER_URL": "postgres://u:p@h/db"},
                        repo(
                            "myapp",
                            canonical_repo_id="org/myapp",
                        ),
                        str(wt),
                    )
            self.assertIn("no sprout preview", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

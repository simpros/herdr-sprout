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
from sprout_worktree_db.models import (  # noqa: E402
    PluginConfig,
    PluginState,
    RepoConfig,
    WorktreeRecord,
)
from sprout_worktree_db.state import (  # noqa: E402
    mint_key,
    normalize_key,
    object_name,
    resolve_key,
    stable_suffix,
)
from sprout_worktree_db import steps as steps_mod  # noqa: E402


def _repo(name: str = "myapp", **kwargs) -> RepoConfig:
    return RepoConfig(
        name=name,
        main_repo=kwargs.pop("main_repo", "/tmp/main"),
        env_files=kwargs.pop("env_files", (".env",)),
        **kwargs,
    )


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
            key = mint_key(wt, _repo("myapp"))
            self.assertEqual(key, f"myapp-feature-{stable_suffix(wt)}")
            self.assertLessEqual(len(key), 40)
            # Same path → same key; no collision branching.
            self.assertEqual(key, mint_key(wt, _repo("myapp")))

    def test_distinct_paths_distinct_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = str(Path(tmp) / "a" / "feature")
            b = str(Path(tmp) / "b" / "feature")
            Path(a).parent.mkdir(parents=True)
            Path(b).parent.mkdir(parents=True)
            self.assertNotEqual(
                mint_key(a, _repo("repo")),
                mint_key(b, _repo("repo")),
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
                resolve_key(st, wt, _repo("myapp")),
                "legacy-bare-key",
            )
            # Matching --key after normalize is accepted.
            self.assertEqual(
                resolve_key(st, wt, _repo("myapp"), requested="Legacy-Bare-Key"),
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
                resolve_key(st, wt, _repo("myapp"), requested="forced")
            self.assertIn("already claimed", str(ctx.exception))

    def test_resolve_normalizes_first_mint_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            wt = str(Path(tmp) / "feature")
            self.assertEqual(
                resolve_key(PluginState(), wt, _repo("myapp"), requested="Foo_Bar"),
                "foo-bar",
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
            PluginConfig(repos=(_repo(requires_node_modules=True, steps=(
                {"cmd": ["echo", "hi"]},
            )),)),
            {"SPROUT_WORKTREE_ADMIN_URL": "postgres://u:p@h/db"},
            _repo(requires_node_modules=True, steps=({"cmd": ["echo", "hi"]},)),
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


class DropLeaseTest(unittest.TestCase):
    def test_fail_closed_without_state_or_repo(self):
        cfg = PluginConfig(repos=())
        with self.assertRaises(SystemExit) as ctx:
            state.begin_drop(cfg, "/tmp/ghost-wt", requested=None)
        self.assertIn("--key", str(ctx.exception))

    def test_remint_when_repo_known(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = str(Path(tmp) / "feature")
                Path(wt).mkdir()
                main = str(Path(tmp) / "main")
                Path(main).mkdir()
                cfg = PluginConfig(
                    repos=(_repo("myapp", main_repo=main, env_files=(".env",)),)
                )
                with mock.patch(
                    "sprout_worktree_db.state.repo_config",
                    return_value=_repo("myapp"),
                ):
                    lease = state.begin_drop(cfg, wt)
                self.assertEqual(lease.key, mint_key(wt, _repo("myapp")))
                self.assertFalse(lease.skip_postgres)
                self.assertIn(lease.key, state.load_state().dropping)
                state.finish_drop(lease)
                self.assertNotIn(lease.key, state.load_state().dropping)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_begin_reserves_until_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = str(Path(tmp) / "feature")
                Path(wt).mkdir()
                st = PluginState(
                    worktrees={
                        wt: WorktreeRecord(
                            key="app-feature-abc12",
                            repo="app",
                            mode="dedicated",
                            object="sprout_wt_app_feature_abc12",
                            created_at="",
                        )
                    }
                )
                state.save_state(st)
                cfg = PluginConfig(repos=(_repo("app"),))
                lease = state.begin_drop(cfg, wt)
                self.assertEqual(lease.key, "app-feature-abc12")
                mid = state.load_state()
                self.assertEqual(
                    state.claim_status(mid, mid.worktrees[wt]), "dropping"
                )
                self.assertIn("app-feature-abc12", mid.dropping)
                self.assertEqual(
                    mid.dropping["app-feature-abc12"].lease_id, lease.lease_id
                )
                self.assertEqual(lease.worktrees, (wt,))
                # Concurrent claim must fail closed while lease is held.
                with self.assertRaises(SystemExit) as ctx:
                    state.claim_key(wt, _repo("app"), mode="dedicated")
                self.assertIn("drop in progress", str(ctx.exception))
                # Second begin_drop must not mint another lease for the same slug.
                with self.assertRaises(SystemExit) as ctx2:
                    state.begin_drop(cfg, wt)
                self.assertIn("drop in progress", str(ctx2.exception))
                state.finish_drop(lease)
                self.assertNotIn(wt, state.load_state().worktrees)
                self.assertNotIn("app-feature-abc12", state.load_state().dropping)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_stale_finish_ignored_after_abort(self):
        """Exclusive lease: abort then a second owner's finish must not wipe."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = str(Path(tmp) / "feature")
                Path(wt).mkdir()
                st = PluginState(
                    worktrees={
                        wt: WorktreeRecord(
                            key="app-feature-abc12",
                            repo="app",
                            mode="dedicated",
                            object="sprout_wt_app_feature_abc12",
                            created_at="",
                        )
                    }
                )
                state.save_state(st)
                cfg = PluginConfig(repos=(_repo("app"),))
                lease1 = state.begin_drop(cfg, wt)
                state.abort_drop(lease1)
                after_abort = state.load_state()
                self.assertEqual(
                    state.claim_status(
                        after_abort, after_abort.worktrees[wt]
                    ),
                    "ready",
                )
                self.assertNotIn("app-feature-abc12", after_abort.dropping)
                # Stale finish from the aborted lease must be a no-op.
                state.finish_drop(lease1)
                self.assertIn(wt, state.load_state().worktrees)
                # A fresh lease can proceed.
                lease2 = state.begin_drop(cfg, wt)
                self.assertNotEqual(lease1.lease_id, lease2.lease_id)
                state.finish_drop(lease2)
                self.assertNotIn(wt, state.load_state().worktrees)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_plan_skips_dropping_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            gone = str(Path(tmp) / "gone")
            from sprout_worktree_db.models import DropLease

            st = PluginState(
                worktrees={
                    gone: WorktreeRecord(
                        key="app-gone",
                        repo="app",
                        mode="dedicated",
                        object="sprout_wt_app_gone",
                        created_at="",
                    )
                },
                dropping={
                    "app-gone": DropLease(
                        lease_id=1,
                        key="app-gone",
                        worktrees=(gone,),
                        object_name="sprout_wt_app_gone",
                        reserved_at="2099-01-01T00:00:00+00:00",
                    )
                },
            )
            plans = gc_mod.plan_orphans(st, set(), ["sprout_wt_app_gone"])
            self.assertEqual(plans, [])

    def test_abort_restores_ready_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = str(Path(tmp) / "feature")
                Path(wt).mkdir()
                st = PluginState(
                    worktrees={
                        wt: WorktreeRecord(
                            key="app-feature-abc12",
                            repo="app",
                            mode="dedicated",
                            object="sprout_wt_app_feature_abc12",
                            created_at="",
                        )
                    }
                )
                state.save_state(st)
                cfg = PluginConfig(repos=(_repo("app"),))
                lease = state.begin_drop(cfg, wt)
                state.abort_drop(lease)
                after = state.load_state()
                self.assertEqual(
                    state.claim_status(after, after.worktrees[wt]), "ready"
                )
                self.assertNotIn("app-feature-abc12", after.dropping)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_finalize_refuses_dropping_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = str(Path(tmp) / "feature")
                Path(wt).mkdir()
                from sprout_worktree_db.models import DropLease

                st = PluginState(
                    worktrees={
                        wt: WorktreeRecord(
                            key="app-feature-abc12",
                            repo="app",
                            mode="dedicated",
                            object="sprout_wt_app_feature_abc12",
                            created_at="2020-01-01T00:00:00+00:00",
                        )
                    },
                    dropping={
                        "app-feature-abc12": DropLease(
                            lease_id=1,
                            key="app-feature-abc12",
                            worktrees=(wt,),
                            object_name="sprout_wt_app_feature_abc12",
                            reserved_at="2099-01-01T00:00:00+00:00",
                        )
                    },
                )
                state.save_state(st)
                with self.assertRaises(RuntimeError) as ctx:
                    state.finalize_claim(
                        wt,
                        "app-feature-abc12",
                        WorktreeRecord(
                            key="app-feature-abc12",
                            repo="app",
                            mode="dedicated",
                            object="sprout_wt_app_feature_abc12",
                            created_at="",
                        ),
                    )
                self.assertIn("drop in progress", str(ctx.exception))
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_claim_rejects_duplicate_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                a = str(Path(tmp) / "a")
                b = str(Path(tmp) / "b")
                Path(a).mkdir()
                Path(b).mkdir()
                st = PluginState(
                    worktrees={
                        a: WorktreeRecord(
                            key="shared-key",
                            repo="app",
                            mode="dedicated",
                            object="sprout_wt_shared_key",
                            created_at="",
                        )
                    }
                )
                state.save_state(st)
                with self.assertRaises(SystemExit) as ctx:
                    state.claim_key(
                        b, _repo("app"), mode="dedicated", requested="shared-key"
                    )
                self.assertIn("already claimed", str(ctx.exception))
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

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

    def test_expired_lease_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = str(Path(tmp) / "feature")
                Path(wt).mkdir()
                from sprout_worktree_db.models import DropLease

                st = PluginState(
                    worktrees={
                        wt: WorktreeRecord(
                            key="app-feature-abc12",
                            repo="app",
                            mode="dedicated",
                            object="sprout_wt_app_feature_abc12",
                            created_at="",
                        )
                    },
                    dropping={
                        "app-feature-abc12": DropLease(
                            lease_id=1,
                            key="app-feature-abc12",
                            worktrees=(wt,),
                            object_name="sprout_wt_app_feature_abc12",
                            reserved_at="2000-01-01T00:00:00+00:00",
                        )
                    },
                    next_lease_id=2,
                )
                state.save_state(st)
                cfg = PluginConfig(repos=(_repo("app"),))
                # TTL reclaim lets a new drop proceed.
                lease = state.begin_drop(cfg, wt)
                self.assertEqual(lease.lease_id, 2)
                self.assertIn("app-feature-abc12", state.load_state().dropping)
                state.finish_drop(lease)
                self.assertNotIn(wt, state.load_state().worktrees)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_force_steals_fresh_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = str(Path(tmp) / "feature")
                Path(wt).mkdir()
                from sprout_worktree_db.models import DropLease

                st = PluginState(
                    worktrees={
                        wt: WorktreeRecord(
                            key="app-feature-abc12",
                            repo="app",
                            mode="dedicated",
                            object="sprout_wt_app_feature_abc12",
                            created_at="",
                        )
                    },
                    dropping={
                        "app-feature-abc12": DropLease(
                            lease_id=1,
                            key="app-feature-abc12",
                            worktrees=(wt,),
                            object_name="sprout_wt_app_feature_abc12",
                            reserved_at="2099-01-01T00:00:00+00:00",
                        )
                    },
                    next_lease_id=2,
                )
                state.save_state(st)
                cfg = PluginConfig(repos=(_repo("app"),))
                with self.assertRaises(SystemExit):
                    state.begin_drop(cfg, wt)
                lease = state.begin_drop(cfg, wt, force=True)
                self.assertEqual(lease.lease_id, 2)
                state.finish_drop(lease)
                self.assertNotIn(wt, state.load_state().worktrees)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_force_steals_on_remint_without_state_row(self):
        """--force remint steals the reminted slug (no force_keys preamble)."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = str(Path(tmp) / "feature")
                Path(wt).mkdir()
                from sprout_worktree_db.models import DropLease

                key = mint_key(wt, _repo("myapp"))
                # Stuck lease only — no worktrees row (remint recovery path).
                st = PluginState(
                    dropping={
                        key: DropLease(
                            lease_id=1,
                            key=key,
                            worktrees=(),
                            object_name=object_name(key),
                            reserved_at="2099-01-01T00:00:00+00:00",
                        )
                    },
                    next_lease_id=2,
                )
                state.save_state(st)
                cfg = PluginConfig(repos=(_repo("myapp"),))
                with mock.patch(
                    "sprout_worktree_db.state.repo_config",
                    return_value=_repo("myapp"),
                ):
                    with self.assertRaises(SystemExit) as ctx:
                        state.begin_drop(cfg, wt)
                    self.assertIn("drop in progress", str(ctx.exception))
                    lease = state.begin_drop(cfg, wt, force=True)
                self.assertEqual(lease.key, key)
                self.assertEqual(lease.lease_id, 2)
                state.finish_drop(lease)
                self.assertNotIn(key, state.load_state().dropping)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]


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


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Drop lease tests (begin/finish/abort + GC reserve)."""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from _helpers import ROOT, repo  # noqa: F401 — ROOT ensures sys.path

from sprout_worktree_db import state
from sprout_worktree_db.errors import PluginError, SproutError
from sprout_worktree_db.keys import mint_key, object_name
from sprout_worktree_db.leases import (
    abort_drop,
    begin_drop,
    claim_provision,
    finish_drop,
)
from sprout_worktree_db.models import (
    PluginConfig,
    PluginState,
    SlugLease,
    WorktreeRecord,
)


class DropLeaseTest(unittest.TestCase):
    def test_fail_closed_without_state_or_repo(self):
        cfg = PluginConfig(repos=())
        with self.assertRaises(PluginError) as ctx:
            begin_drop(cfg, "/tmp/ghost-wt", requested=None)
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
                    repos=(repo("myapp", main_repo=main, env_files=(".env",)),)
                )
                with mock.patch(
                    "sprout_worktree_db.leases.repo_config",
                    return_value=repo("myapp"),
                ):
                    lease = begin_drop(cfg, wt)
                self.assertEqual(lease.key, mint_key(wt, repo("myapp")))
                self.assertTrue(lease.touch_postgres)
                self.assertEqual(lease.op, "drop")
                self.assertIn(lease.key, state.load_state().leases)
                finish_drop(lease)
                self.assertNotIn(lease.key, state.load_state().leases)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_preview_key_and_worktree_agree(self):
        """Empty preview object: --key and --worktree use the same rule."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = str(Path(tmp) / "feature")
                Path(wt).mkdir()
                st = PluginState(
                    worktrees={
                        wt: WorktreeRecord(
                            key="app-prev",
                            repo="app",
                            mode="preview",
                            object="",
                            created_at="",
                        )
                    }
                )
                state.save_state(st)
                cfg = PluginConfig(repos=(repo("app"),))
                by_wt = begin_drop(cfg, wt)
                self.assertEqual(by_wt.object_name, "")
                self.assertFalse(by_wt.touch_postgres)
                abort_drop(by_wt)
                by_key = begin_drop(cfg, requested="app-prev")
                self.assertEqual(by_key.object_name, "")
                self.assertFalse(by_key.touch_postgres)
                finish_drop(by_key)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_forget_only_encodes_touch_postgres(self):
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
                cfg = PluginConfig(repos=(repo("app"),))
                lease = begin_drop(cfg, wt, forget_only=True)
                self.assertFalse(lease.touch_postgres)
                finish_drop(lease)
                self.assertNotIn(wt, state.load_state().worktrees)
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
                cfg = PluginConfig(repos=(repo("app"),))
                lease = begin_drop(cfg, wt)
                self.assertEqual(lease.key, "app-feature-abc12")
                mid = state.load_state()
                self.assertEqual(
                    state.claim_status(mid, mid.worktrees[wt]), "dropping"
                )
                self.assertIn("app-feature-abc12", mid.leases)
                self.assertEqual(
                    mid.leases["app-feature-abc12"].lease_id, lease.lease_id
                )
                self.assertEqual(lease.worktrees, (wt,))
                # Concurrent provision must fail closed while lease is held.
                with self.assertRaises(PluginError) as ctx:
                    with claim_provision(wt, repo("app"), mode="dedicated"):
                        pass
                self.assertIn("in progress", str(ctx.exception))
                # Second begin_drop must not mint another lease for the same slug.
                with self.assertRaises(PluginError) as ctx2:
                    begin_drop(cfg, wt)
                self.assertIn("in progress", str(ctx2.exception))
                finish_drop(lease)
                self.assertNotIn(wt, state.load_state().worktrees)
                self.assertNotIn("app-feature-abc12", state.load_state().leases)
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
                cfg = PluginConfig(repos=(repo("app"),))
                lease1 = begin_drop(cfg, wt)
                abort_drop(lease1)
                after_abort = state.load_state()
                self.assertEqual(
                    state.claim_status(
                        after_abort, after_abort.worktrees[wt]
                    ),
                    "ready",
                )
                self.assertNotIn("app-feature-abc12", after_abort.leases)
                # Stale finish from the aborted lease must be a no-op.
                finish_drop(lease1)
                self.assertIn(wt, state.load_state().worktrees)
                # A fresh lease can proceed.
                lease2 = begin_drop(cfg, wt)
                self.assertNotEqual(lease1.lease_id, lease2.lease_id)
                finish_drop(lease2)
                self.assertNotIn(wt, state.load_state().worktrees)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

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
                cfg = PluginConfig(repos=(repo("app"),))
                lease = begin_drop(cfg, wt)
                abort_drop(lease)
                after = state.load_state()
                self.assertEqual(
                    state.claim_status(after, after.worktrees[wt]), "ready"
                )
                self.assertNotIn("app-feature-abc12", after.leases)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_expired_lease_reclaimed(self):
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
                    },
                    leases={
                        "app-feature-abc12": SlugLease(
                            lease_id=1,
                            key="app-feature-abc12",
                            op="drop",
                            worktrees=(wt,),
                            object_name="sprout_wt_app_feature_abc12",
                            reserved_at="2000-01-01T00:00:00+00:00",
                        )
                    },
                    next_lease_id=2,
                )
                state.save_state(st)
                cfg = PluginConfig(repos=(repo("app"),))
                # TTL reclaim lets a new drop proceed.
                lease = begin_drop(cfg, wt)
                self.assertEqual(lease.lease_id, 2)
                self.assertIn("app-feature-abc12", state.load_state().leases)
                finish_drop(lease)
                self.assertNotIn(wt, state.load_state().worktrees)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_force_steals_fresh_lease(self):
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
                    },
                    leases={
                        "app-feature-abc12": SlugLease(
                            lease_id=1,
                            key="app-feature-abc12",
                            op="drop",
                            worktrees=(wt,),
                            object_name="sprout_wt_app_feature_abc12",
                            reserved_at="2099-01-01T00:00:00+00:00",
                        )
                    },
                    next_lease_id=2,
                )
                state.save_state(st)
                cfg = PluginConfig(repos=(repo("app"),))
                with self.assertRaises(PluginError):
                    begin_drop(cfg, wt)
                lease = begin_drop(cfg, wt, force=True)
                self.assertEqual(lease.lease_id, 2)
                finish_drop(lease)
                self.assertNotIn(wt, state.load_state().worktrees)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_force_does_not_steal_provision_lease(self):
        """--force only steals stuck drop leases; provision stays exclusive."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = str(Path(tmp) / "feature")
                Path(wt).mkdir()
                key = "app-feature-abc12"
                st = PluginState(
                    worktrees={
                        wt: WorktreeRecord(
                            key=key,
                            repo="app",
                            mode="dedicated",
                            object="sprout_wt_app_feature_abc12",
                            created_at="",
                        )
                    },
                    leases={
                        key: SlugLease(
                            lease_id=1,
                            key=key,
                            op="provision",
                            worktrees=(wt,),
                            object_name="sprout_wt_app_feature_abc12",
                            touch_postgres=False,
                            reserved_at="2099-01-01T00:00:00+00:00",
                        )
                    },
                    next_lease_id=2,
                )
                state.save_state(st)
                cfg = PluginConfig(repos=(repo("app"),))
                with self.assertRaises(PluginError) as ctx:
                    begin_drop(cfg, wt, force=True)
                self.assertIn("provision in progress", str(ctx.exception))
                self.assertIn(key, state.load_state().leases)
                self.assertEqual(state.load_state().leases[key].op, "provision")
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_force_steals_on_remint_without_state_row(self):
        """--force remint steals the reminted slug (no force_keys preamble)."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = str(Path(tmp) / "feature")
                Path(wt).mkdir()

                key = mint_key(wt, repo("myapp"))
                # Stuck lease only — no worktrees row (remint recovery path).
                st = PluginState(
                    leases={
                        key: SlugLease(
                            lease_id=1,
                            key=key,
                            op="drop",
                            worktrees=(),
                            object_name=object_name(key),
                            reserved_at="2099-01-01T00:00:00+00:00",
                        )
                    },
                    next_lease_id=2,
                )
                state.save_state(st)
                cfg = PluginConfig(repos=(repo("myapp"),))
                with mock.patch(
                    "sprout_worktree_db.leases.repo_config",
                    return_value=repo("myapp"),
                ):
                    with self.assertRaises(PluginError) as ctx:
                        begin_drop(cfg, wt)
                    self.assertIn("in progress", str(ctx.exception))
                    lease = begin_drop(cfg, wt, force=True)
                self.assertEqual(lease.key, key)
                self.assertEqual(lease.lease_id, 2)
                finish_drop(lease)
                self.assertNotIn(key, state.load_state().leases)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_git_resolution_holds_no_lock(self):
        """Remint `git` runs before the flock, never inside it."""
        import sprout_worktree_db.leases as leases_mod

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = str(Path(tmp) / "feature")
                Path(wt).mkdir()
                state.save_state(PluginState())
                cfg = PluginConfig(repos=(repo("myapp"),))
                calls: list[str] = []
                real_locked = leases_mod.locked_state

                @contextmanager
                def tracked_lock():
                    calls.append("lock-enter")
                    with real_locked() as st:
                        yield st
                    calls.append("lock-exit")

                def fake_repo_config(cfg_arg, wt_arg):
                    calls.append("repo_config")
                    return repo("myapp")

                with (
                    mock.patch.object(
                        leases_mod, "locked_state", tracked_lock
                    ),
                    mock.patch.object(
                        leases_mod, "repo_config", fake_repo_config
                    ),
                ):
                    lease = begin_drop(cfg, wt)
                self.assertEqual(
                    calls[0], "repo_config", f"git first, lock after: {calls}"
                )
                self.assertIn("lock-enter", calls)
                abort_drop(lease)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_fast_path_skips_git_when_row_present(self):
        """Row present: remint is recovery-only, so no git runs at all."""
        import sprout_worktree_db.leases as leases_mod

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = str(Path(tmp) / "feature")
                Path(wt).mkdir()
                state.save_state(
                    PluginState(
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
                )
                cfg = PluginConfig(repos=(repo("app"),))
                calls: list[str] = []
                real_locked = leases_mod.locked_state

                @contextmanager
                def tracked_lock():
                    calls.append("lock-enter")
                    with real_locked() as st:
                        yield st
                    calls.append("lock-exit")

                def fake_repo_config(cfg_arg, wt_arg):
                    calls.append("repo_config")
                    return repo("app")

                with (
                    mock.patch.object(
                        leases_mod, "locked_state", tracked_lock
                    ),
                    mock.patch.object(
                        leases_mod, "repo_config", fake_repo_config
                    ),
                ):
                    lease = begin_drop(cfg, wt)
                # Resolved key comes from the row, not the remint.
                self.assertEqual(lease.key, "app-feature-abc12")
                # Recovery-only remint: the fast path never shells git.
                self.assertNotIn(
                    "repo_config", calls, f"git must not run: {calls}"
                )
                abort_drop(lease)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]


class ExecuteDropLeaseAbortTest(unittest.TestCase):
    """SproutError must abort the lease — never leave it stuck.

    Regression for PluginError(SystemExit) punching through the
    ``except Exception`` net in ``execute_drop_lease``.
    """

    def _setup(self, tmp: str):
        from sprout_worktree_db.drop import execute_drop_lease

        os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
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
        cfg = PluginConfig(repos=(repo("app"),))
        return cfg, wt, execute_drop_lease

    def test_sprout_failure_aborts_and_reraises(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg, wt, execute_drop_lease = self._setup(tmp)
            try:
                lease = begin_drop(cfg, wt)
                with mock.patch(
                    "sprout_worktree_db.drop.drop_key",
                    side_effect=SproutError("boom"),
                ):
                    with self.assertRaises(SproutError):
                        execute_drop_lease(cfg, {}, lease, reraise=True)
                after = state.load_state()
                self.assertNotIn(lease.key, after.leases)
                self.assertIn(wt, after.worktrees)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_sprout_failure_aborts_and_soft_fails(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg, wt, execute_drop_lease = self._setup(tmp)
            try:
                lease = begin_drop(cfg, wt)
                with mock.patch(
                    "sprout_worktree_db.drop.drop_key",
                    side_effect=SproutError("boom"),
                ):
                    ok = execute_drop_lease(cfg, {}, lease, reraise=False)
                self.assertFalse(ok)
                after = state.load_state()
                self.assertNotIn(lease.key, after.leases)
                self.assertIn(wt, after.worktrees)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]


if __name__ == "__main__":
    unittest.main()

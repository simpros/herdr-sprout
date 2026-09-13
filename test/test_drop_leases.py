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
            begin_drop("/tmp/ghost-wt", requested=None)
        self.assertIn("--key", str(ctx.exception))

    def test_remint_when_repo_known(self):
        from sprout_worktree_db.drop import remint_key_for

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
                    "sprout_worktree_db.drop.repo_config",
                    return_value=repo("myapp"),
                ):
                    remint = remint_key_for(cfg, wt)
                self.assertEqual(remint, mint_key(wt, repo("myapp")))
                lease = begin_drop(wt, remint_key=remint)
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
                by_wt = begin_drop(wt)
                self.assertEqual(by_wt.object_name, "")
                self.assertFalse(by_wt.touch_postgres)
                abort_drop(by_wt)
                by_key = begin_drop(requested="app-prev")
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
                lease = begin_drop(wt, forget_only=True)
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
                lease = begin_drop(wt)
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
                    begin_drop(wt)
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
                lease1 = begin_drop(wt)
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
                lease2 = begin_drop(wt)
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
                lease = begin_drop(wt)
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
                lease = begin_drop(wt)
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
                    begin_drop(wt)
                lease = begin_drop(wt, force=True)
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
                    begin_drop(wt, force=True)
                self.assertIn("provision in progress", str(ctx.exception))
                self.assertIn(key, state.load_state().leases)
                self.assertEqual(state.load_state().leases[key].op, "provision")
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_force_steals_on_remint_without_state_row(self):
        """--force remint steals the reminted slug (no force_keys preamble)."""
        from sprout_worktree_db.drop import remint_key_for

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
                    "sprout_worktree_db.drop.repo_config",
                    return_value=repo("myapp"),
                ):
                    remint = remint_key_for(cfg, wt)
                    with self.assertRaises(PluginError) as ctx:
                        begin_drop(wt, remint_key=remint)
                    self.assertIn("in progress", str(ctx.exception))
                    lease = begin_drop(wt, force=True, remint_key=remint)
                self.assertEqual(lease.key, key)
                self.assertEqual(lease.lease_id, 2)
                finish_drop(lease)
                self.assertNotIn(key, state.load_state().leases)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_git_resolution_holds_no_lock(self):
        """Remint `git` runs before the flock, never inside it."""
        import sprout_worktree_db.drop as drop_mod
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
                        drop_mod, "repo_config", fake_repo_config
                    ),
                ):
                    remint = drop_mod.remint_key_for(cfg, wt)
                    self.assertEqual(calls, ["repo_config"])
                    lease = begin_drop(wt, remint_key=remint)
                self.assertEqual(
                    calls[0], "repo_config", f"git first, lock after: {calls}"
                )
                self.assertIn("lock-enter", calls)
                abort_drop(lease)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_fast_path_skips_git_when_row_present(self):
        """Row present: remint is recovery-only, so no git runs at all."""
        import sprout_worktree_db.drop as drop_mod

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

                def fake_repo_config(cfg_arg, wt_arg):
                    raise AssertionError("git must not run on the fast path")

                with mock.patch.object(
                    drop_mod, "repo_config", fake_repo_config
                ):
                    remint = drop_mod.remint_key_for(cfg, wt)
                self.assertIsNone(remint)
                lease = begin_drop(wt, remint_key=remint)
                # Resolved key comes from the row, not the remint.
                self.assertEqual(lease.key, "app-feature-abc12")
                abort_drop(lease)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_key_and_worktree_agree_resolves_key_only(self):
        """Both flags agreeing: forget-set is the key's claim only."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = os.path.realpath(str(Path(tmp) / "feature"))
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
                lease = begin_drop(
                    wt, requested="app-feature-abc12", forget_only=True
                )
                self.assertEqual(lease.key, "app-feature-abc12")
                self.assertEqual(lease.worktrees, (wt,))
                finish_drop(lease)
                self.assertNotIn(wt, state.load_state().worktrees)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_key_with_foreign_worktree_refused(self):
        """--key + unrelated --worktree: fail closed, keep both claims."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                a = os.path.realpath(str(Path(tmp) / "a"))
                b = os.path.realpath(str(Path(tmp) / "b"))
                Path(a).mkdir()
                Path(b).mkdir()
                st = PluginState(
                    worktrees={
                        a: WorktreeRecord(
                            key="key-a",
                            repo="app",
                            mode="dedicated",
                            object="sprout_wt_key_a",
                            created_at="",
                        ),
                        b: WorktreeRecord(
                            key="key-b",
                            repo="app",
                            mode="dedicated",
                            object="sprout_wt_key_b",
                            created_at="",
                        ),
                    }
                )
                state.save_state(st)
                with self.assertRaises(PluginError) as ctx:
                    begin_drop(b, requested="key-a", forget_only=True)
                self.assertIn("does not own", str(ctx.exception))
                after = state.load_state()
                self.assertIn(a, after.worktrees)
                self.assertIn(b, after.worktrees)
                self.assertNotIn("key-a", after.leases)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_orphan_key_with_tracked_worktree_refused(self):
        """Orphan --key + tracked --worktree: fail closed, keep the claim."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                b = os.path.realpath(str(Path(tmp) / "b"))
                Path(b).mkdir()
                st = PluginState(
                    worktrees={
                        b: WorktreeRecord(
                            key="key-b",
                            repo="app",
                            mode="dedicated",
                            object="sprout_wt_key_b",
                            created_at="",
                        )
                    }
                )
                state.save_state(st)
                with self.assertRaises(PluginError):
                    begin_drop(b, requested="key-a")
                after = state.load_state()
                self.assertIn(b, after.worktrees)
                self.assertNotIn("key-a", after.leases)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_remint_claimed_elsewhere_refused(self):
        """Remint of B for a slug A owns: refuse, keep A's claim.

        Regression: remint used to merge the foreign claim into the
        forget-set via ``_forget_set`` so ``finish_drop`` wiped A.
        ``DropOp.paths`` is now authoritative and the resolver fails
        closed on ownership (same rule as provision claim).
        """
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                a = os.path.realpath(str(Path(tmp) / "a"))
                b = os.path.realpath(str(Path(tmp) / "b"))
                Path(a).mkdir()
                Path(b).mkdir()
                remint = mint_key(b, repo("myapp"))
                st = PluginState(
                    worktrees={
                        a: WorktreeRecord(
                            key=remint,
                            repo="myapp",
                            mode="dedicated",
                            object=object_name(remint),
                            created_at="",
                        )
                    }
                )
                state.save_state(st)
                with self.assertRaises(PluginError) as ctx:
                    begin_drop(b, remint_key=remint, forget_only=True)
                self.assertIn("already claimed", str(ctx.exception))
                after = state.load_state()
                self.assertIn(a, after.worktrees)
                self.assertNotIn(remint, after.leases)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_key_resolved_forget_set_is_claim_only(self):
        """--key alone: lease worktrees is the claim path, never empty."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                a = os.path.realpath(str(Path(tmp) / "a"))
                Path(a).mkdir()
                st = PluginState(
                    worktrees={
                        a: WorktreeRecord(
                            key="key-a",
                            repo="app",
                            mode="dedicated",
                            object="sprout_wt_key_a",
                            created_at="",
                        )
                    }
                )
                state.save_state(st)
                lease = begin_drop(requested="key-a", forget_only=True)
                self.assertEqual(lease.key, "key-a")
                self.assertEqual(lease.worktrees, (a,))
                finish_drop(lease)
                self.assertNotIn(a, state.load_state().worktrees)
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
                lease = begin_drop(wt)
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
                lease = begin_drop(wt)
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

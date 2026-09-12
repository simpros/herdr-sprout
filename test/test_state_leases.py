#!/usr/bin/env python3
"""Claim / drop-lease state tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _helpers import ROOT, repo  # noqa: F401 — ROOT ensures sys.path

from sprout_worktree_db import state
from sprout_worktree_db.models import DropLease, PluginConfig, PluginState, WorktreeRecord
from sprout_worktree_db.state import mint_key, object_name


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
                    repos=(repo("myapp", main_repo=main, env_files=(".env",)),)
                )
                with mock.patch(
                    "sprout_worktree_db.state.repo_config",
                    return_value=repo("myapp"),
                ):
                    lease = state.begin_drop(cfg, wt)
                self.assertEqual(lease.key, mint_key(wt, repo("myapp")))
                self.assertFalse(lease.skip_postgres)
                self.assertIn(lease.key, state.load_state().dropping)
                state.finish_drop(lease)
                self.assertNotIn(lease.key, state.load_state().dropping)
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
                by_wt = state.begin_drop(cfg, wt)
                self.assertEqual(by_wt.object_name, "")
                self.assertTrue(by_wt.skip_postgres)
                state.abort_drop(by_wt)
                by_key = state.begin_drop(cfg, requested="app-prev")
                self.assertEqual(by_key.object_name, "")
                self.assertTrue(by_key.skip_postgres)
                state.finish_drop(by_key)
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
                    state.claim_key(wt, repo("app"), mode="dedicated")
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
                cfg = PluginConfig(repos=(repo("app"),))
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
                        b, repo("app"), mode="dedicated", requested="shared-key"
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
                cfg = PluginConfig(repos=(repo("app"),))
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
                cfg = PluginConfig(repos=(repo("app"),))
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

                key = mint_key(wt, repo("myapp"))
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
                cfg = PluginConfig(repos=(repo("myapp"),))
                with mock.patch(
                    "sprout_worktree_db.state.repo_config",
                    return_value=repo("myapp"),
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

    def test_claim_refuses_mode_change(self):
        """In-place dedicated↔preview is refused; drop first."""
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
                            created_at="2020-01-01T00:00:00+00:00",
                        )
                    }
                )
                state.save_state(st)
                with self.assertRaises(SystemExit) as ctx:
                    state.claim_key(wt, repo("app"), mode="preview")
                self.assertIn("drop first", str(ctx.exception))
                mid = state.load_state()
                rec = mid.worktrees[wt]
                self.assertEqual(rec.mode, "dedicated")
                self.assertEqual(rec.object, "sprout_wt_app_feature_abc12")
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_claim_same_mode_leaves_row_until_finalize(self):
        """Same-mode re-claim reserves the key without rewriting the row."""
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
                            created_at="2020-01-01T00:00:00+00:00",
                        )
                    }
                )
                state.save_state(st)
                key = state.claim_key(wt, repo("app"), mode="dedicated")
                self.assertEqual(key, "app-feature-abc12")
                mid = state.load_state()
                rec = mid.worktrees[wt]
                self.assertEqual(rec.mode, "dedicated")
                self.assertEqual(rec.object, "sprout_wt_app_feature_abc12")
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]


if __name__ == "__main__":
    unittest.main()

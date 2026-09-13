#!/usr/bin/env python3
"""Claim / slug-lease state tests (session API + drop leases)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _helpers import ROOT, repo  # noqa: F401 — ROOT ensures sys.path

from sprout_worktree_db import state
from sprout_worktree_db.keys import mint_key, object_name
from sprout_worktree_db.leases import (
    ProvisionLease,
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


class SlugLeaseTest(unittest.TestCase):
    def test_fail_closed_without_state_or_repo(self):
        cfg = PluginConfig(repos=())
        with self.assertRaises(SystemExit) as ctx:
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
                with self.assertRaises(SystemExit) as ctx:
                    with claim_provision(wt, repo("app"), mode="dedicated"):
                        pass
                self.assertIn("in progress", str(ctx.exception))
                # Second begin_drop must not mint another lease for the same slug.
                with self.assertRaises(SystemExit) as ctx2:
                    begin_drop(cfg, wt)
                self.assertIn("in progress", str(ctx2.exception))
                finish_drop(lease)
                self.assertNotIn(wt, state.load_state().worktrees)
                self.assertNotIn("app-feature-abc12", state.load_state().leases)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_provision_lease_blocks_drop(self):
        """Exclusive leasing: drop refuses while provision holds the slug."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = str(Path(tmp) / "feature")
                Path(wt).mkdir()
                with claim_provision(wt, repo("app"), mode="dedicated") as lease:
                    key = lease.key
                    mid = state.load_state()
                    self.assertEqual(
                        state.claim_status(mid, mid.worktrees[wt]),
                        "provisioning",
                    )
                    self.assertEqual(mid.leases[key].op, "provision")
                    cfg = PluginConfig(repos=(repo("app"),))
                    with self.assertRaises(SystemExit) as ctx:
                        begin_drop(cfg, wt)
                    self.assertIn("provision in progress", str(ctx.exception))
                # Session exit always releases the lease (abort ≡ release).
                self.assertNotIn(key, state.load_state().leases)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_session_releases_on_error(self):
        """Exception inside the session still clears the lease, keeps row."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = str(Path(tmp) / "feature")
                Path(wt).mkdir()
                with self.assertRaises(RuntimeError):
                    with claim_provision(
                        wt, repo("app"), mode="dedicated"
                    ) as lease:
                        key = lease.key
                        raise RuntimeError("sprout boom")
                after = state.load_state()
                self.assertNotIn(key, after.leases)
                self.assertIn(wt, after.worktrees)
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

    def test_finalize_refuses_drop_lease(self):
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
                )
                state.save_state(st)
                # A stale handle (wrong lease_id, wrong op) must fail closed.
                stale = ProvisionLease(
                    worktree=wt, key="app-feature-abc12", lease_id=99
                )
                with self.assertRaises(RuntimeError) as ctx:
                    stale.finalize(
                        WorktreeRecord(
                            key="app-feature-abc12",
                            repo="app",
                            mode="dedicated",
                            object="sprout_wt_app_feature_abc12",
                            created_at="",
                        ),
                    )
                self.assertIn("provision lease gone", str(ctx.exception))
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
                    with claim_provision(
                        b,
                        repo("app"),
                        mode="dedicated",
                        requested="shared-key",
                    ):
                        pass
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

    def test_legacy_dropping_migrates_on_load(self):
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
                "dropping": {
                    "app-x": {
                        "lease_id": 3,
                        "worktrees": ["/wt"],
                        "object_name": "sprout_wt_app_x",
                        "skip_postgres": False,
                        "reserved_at": "2099-01-01T00:00:00+00:00",
                    }
                },
            }
        )
        self.assertIn("app-x", st.leases)
        self.assertEqual(st.leases["app-x"].op, "drop")
        self.assertTrue(st.leases["app-x"].touch_postgres)
        self.assertNotIn("dropping", st.to_dict())
        self.assertIn("leases", st.to_dict())

    def test_legacy_shape_writes_back_canonical(self):
        """Legacy persists lazily: unlocked reads stay read-only."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                path = Path(tmp) / "state.json"
                path.write_text(
                    json.dumps(
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
                            "dropping": {
                                "app-x": "/wt",
                            },
                        }
                    )
                )
                st = state.load_state()
                self.assertIn("app-x", st.leases)
                self.assertEqual(st.leases["app-x"].op, "drop")
                # Unlocked load must not persist (no lock-free write-back).
                raw = json.loads(path.read_text())
                self.assertIn("dropping", raw)
                # Next locked mutation persists the canonical form.
                with state.locked_state():
                    pass
                raw2 = json.loads(path.read_text())
                self.assertNotIn("dropping", raw2)
                self.assertIn("leases", raw2)
                st2 = state.load_state()
                self.assertIn("app-x", st2.leases)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_legacy_string_lease_under_leases(self):
        st = PluginState.from_dict(
            {"worktrees": {}, "leases": {"a": "/wt"}}
        )
        self.assertEqual(st.leases["a"].op, "drop")
        self.assertEqual(st.leases["a"].worktrees, ("/wt",))
        self.assertTrue(st.leases["a"].touch_postgres)

    def test_legacy_missing_op_under_leases(self):
        st = PluginState.from_dict(
            {
                "worktrees": {},
                "leases": {
                    "b": {
                        "lease_id": 1,
                        "worktrees": ["/wt"],
                        "touch_postgres": True,
                    }
                },
            }
        )
        self.assertEqual(st.leases["b"].op, "drop")

    def test_legacy_skip_postgres_inverts_on_write_back(self):
        st = PluginState.from_dict(
            {
                "worktrees": {},
                "leases": {
                    "c": {
                        "lease_id": 1,
                        "op": "drop",
                        "worktrees": ["/wt"],
                        "skip_postgres": True,
                    }
                },
            }
        )
        self.assertFalse(st.leases["c"].touch_postgres)
        data = st.to_dict()
        self.assertFalse(data["leases"]["c"]["touch_postgres"])
        self.assertNotIn("skip_postgres", data["leases"]["c"])

    def test_legacy_empty_leases_merges_dropping(self):
        st = PluginState.from_dict(
            {
                "worktrees": {},
                "leases": {},
                "dropping": {"k": "/wt"},
            }
        )
        self.assertIn("k", st.leases)
        self.assertNotIn("dropping", st.to_dict())

    def test_legacy_leases_wins_on_conflict(self):
        st = PluginState.from_dict(
            {
                "worktrees": {},
                "leases": {
                    "k": {
                        "lease_id": 9,
                        "op": "drop",
                        "worktrees": ["/a"],
                        "touch_postgres": False,
                    }
                },
                "dropping": {"k": "/b"},
            }
        )
        self.assertEqual(st.leases["k"].lease_id, 9)
        self.assertEqual(st.leases["k"].worktrees, ("/a",))

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
                with self.assertRaises(SystemExit):
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
                            object="",
                            created_at="",
                        )
                    },
                    leases={
                        key: SlugLease(
                            lease_id=1,
                            key=key,
                            op="provision",
                            worktrees=(wt,),
                            object_name="",
                            touch_postgres=False,
                            reserved_at="2099-01-01T00:00:00+00:00",
                        )
                    },
                    next_lease_id=2,
                )
                state.save_state(st)
                cfg = PluginConfig(repos=(repo("app"),))
                with self.assertRaises(SystemExit) as ctx:
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
                    with self.assertRaises(SystemExit) as ctx:
                        begin_drop(cfg, wt)
                    self.assertIn("in progress", str(ctx.exception))
                    lease = begin_drop(cfg, wt, force=True)
                self.assertEqual(lease.key, key)
                self.assertEqual(lease.lease_id, 2)
                finish_drop(lease)
                self.assertNotIn(key, state.load_state().leases)
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
                    with claim_provision(wt, repo("app"), mode="preview"):
                        pass
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
                with claim_provision(wt, repo("app"), mode="dedicated") as lease:
                    self.assertEqual(lease.key, "app-feature-abc12")
                    mid = state.load_state()
                    rec = mid.worktrees[wt]
                    self.assertEqual(rec.mode, "dedicated")
                    self.assertEqual(rec.object, "sprout_wt_app_feature_abc12")
                    self.assertEqual(mid.leases[lease.key].op, "provision")
                self.assertNotIn(lease.key, state.load_state().leases)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_finalize_keeps_lease_until_release(self):
        """Provision lease fences env/steps; drop blocked until released."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = str(Path(tmp) / "feature")
                Path(wt).mkdir()
                with claim_provision(
                    wt, repo("app"), mode="dedicated"
                ) as lease:
                    key = lease.key
                    record = WorktreeRecord(
                        key=key,
                        repo="app",
                        mode="dedicated",
                        object=object_name(key),
                        created_at="",
                    )
                    lease.finalize(record)
                    mid = state.load_state()
                    self.assertEqual(
                        mid.worktrees[wt].object, object_name(key)
                    )
                    self.assertEqual(mid.leases[key].op, "provision")
                    self.assertEqual(
                        state.claim_status(mid, mid.worktrees[wt]),
                        "provisioning",
                    )
                    cfg = PluginConfig(repos=(repo("app"),))
                    with self.assertRaises(SystemExit) as ctx:
                        begin_drop(cfg, wt)
                    self.assertIn("provision in progress", str(ctx.exception))

                    steps = [{"ok": True, "cmd": "migrate"}]
                    lease.record_steps(steps)
                    self.assertEqual(
                        state.load_state().worktrees[wt].steps, steps
                    )

                # Session exit released the lease; claim is ready.
                after = state.load_state()
                self.assertNotIn(key, after.leases)
                self.assertEqual(
                    state.claim_status(after, after.worktrees[wt]), "ready"
                )
                # Drop can proceed once the lease is gone.
                lease = begin_drop(cfg, wt)
                finish_drop(lease)
                self.assertNotIn(wt, state.load_state().worktrees)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_record_steps_rejects_wrong_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = str(Path(tmp) / "feature")
                Path(wt).mkdir()
                with claim_provision(
                    wt, repo("app"), mode="dedicated"
                ) as lease:
                    key = lease.key
                    lease.finalize(
                        WorktreeRecord(
                            key=key,
                            repo="app",
                            mode="dedicated",
                            object=object_name(key),
                            created_at="",
                        ),
                    )
                    # A fenced-out handle is a silent no-op, not a failure.
                    stale = ProvisionLease(
                        worktree=wt, key=key, lease_id=lease.lease_id + 1
                    )
                    stale.record_steps([{"ok": True}])
                    self.assertEqual(state.load_state().worktrees[wt].steps, [])
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]


if __name__ == "__main__":
    unittest.main()

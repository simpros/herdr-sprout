#!/usr/bin/env python3
"""Provision session / claim tests (claim_provision is the only entrypoint)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _helpers import ROOT, repo  # noqa: F401 — ROOT ensures sys.path

from sprout_worktree_db import state
from sprout_worktree_db.errors import PluginError
from sprout_worktree_db.keys import object_name
from sprout_worktree_db.leases import (
    ProvisionLease,
    begin_drop,
    claim_provision,
    finish_drop,
)
from sprout_worktree_db.models import (
    EnvInjection,
    PluginConfig,
    PluginState,
    ProvisionRequest,
    SlugLease,
    StepResult,
    WorktreeRecord,
)
from sprout_worktree_db.provision import do_provision


class ProvisionLeaseTest(unittest.TestCase):
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
                    # Lease-only claim: no row until finalize; the lease
                    # itself is the in-flight reservation.
                    self.assertNotIn(wt, mid.worktrees)
                    self.assertEqual(mid.leases[key].op, "provision")
                    self.assertEqual(mid.leases[key].worktrees, (wt,))
                    cfg = PluginConfig(repos=(repo("app"),))
                    with self.assertRaises(PluginError) as ctx:
                        begin_drop(cfg, wt)
                    self.assertIn("provision in progress", str(ctx.exception))
                # Session exit always releases the lease (abort ≡ release).
                self.assertNotIn(key, state.load_state().leases)
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_session_releases_on_error(self):
        """Exception inside the session still clears the lease, writes no row."""
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
                # Lease-only claim: no finalize ran, so no row exists.
                # A leftover sprout_wt_* is a pathless postgres orphan for GC.
                self.assertNotIn(wt, after.worktrees)
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
                with self.assertRaises(PluginError) as ctx:
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
                with self.assertRaises(PluginError) as ctx:
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
                with self.assertRaises(PluginError) as ctx:
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
                    with self.assertRaises(PluginError) as ctx:
                        begin_drop(cfg, wt)
                    self.assertIn("provision in progress", str(ctx.exception))

                    steps = [StepResult(step="migrate", ok=True)]
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
                    stale.record_steps([StepResult(step="x", ok=True)])
                    self.assertEqual(state.load_state().worktrees[wt].steps, [])
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_reprovision_reuses_finalized_key(self):
        """Fresh claims mint; re-provisions reuse the finalized row's key."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = tmp
            try:
                wt = str(Path(tmp) / "feature")
                Path(wt).mkdir()
                with claim_provision(
                    wt, repo("app"), mode="dedicated"
                ) as lease:
                    key = lease.key
                    # Lease-only claim: no row until finalize.
                    self.assertNotIn(wt, state.load_state().worktrees)
                    lease.finalize(
                        WorktreeRecord(
                            key=key,
                            repo="app",
                            mode="dedicated",
                            object=object_name(key),
                            created_at="",
                        )
                    )
                self.assertIn(wt, state.load_state().worktrees)
                with claim_provision(
                    wt, repo("app"), mode="dedicated"
                ) as lease2:
                    self.assertEqual(lease2.key, key)
                    # Finalized row stays until the second finalize.
                    self.assertEqual(
                        state.load_state().worktrees[wt].object,
                        object_name(key),
                    )
            finally:
                del os.environ["HERDR_PLUGIN_STATE_DIR"]


class ProvisionOrphanCleanupTest(unittest.TestCase):
    """Lease-only claim: crash-before-finalize leaves no row for GC to miss.

    There is no in-process orphan drop — a leftover ``sprout_wt_*`` after
    TTL reclaim is a normal pathless postgres orphan that
    :func:`gc.plan_orphans` already plans.
    """

    def _setup(self, tmp: str):
        wt = str(Path(tmp) / "wt")
        Path(wt).mkdir()
        cfg = PluginConfig(
            repos=(repo("myapp", main_repo=tmp, env_files=(".env",)),)
        )
        os.environ["HERDR_PLUGIN_STATE_DIR"] = str(Path(tmp) / "state")
        return cfg, wt

    def _teardown(self):
        del os.environ["HERDR_PLUGIN_STATE_DIR"]

    def test_finalize_failure_writes_no_row(self):
        """Lost lease at finalize: no row, no hidden placeholder."""
        from sprout_worktree_db.errors import SproutError

        with tempfile.TemporaryDirectory() as tmp:
            cfg, wt = self._setup(tmp)
            try:
                inj = EnvInjection(
                    object_name="sprout_wt_new",
                    env_files=(),
                    pending_env={},
                )
                with (
                    mock.patch(
                        "sprout_worktree_db.provision.provision_dedicated",
                        return_value=inj,
                    ),
                    mock.patch(
                        "sprout_worktree_db.leases._finalize_claim",
                        side_effect=SproutError("lease lost"),
                    ),
                ):
                    with self.assertRaises(PluginError):
                        do_provision(
                            cfg,
                            {},
                            ProvisionRequest(
                                worktree=wt,
                                mode="dedicated",
                                with_steps=False,
                            ),
                        )
                # No placeholder row — GC sees a pathless postgres orphan.
                self.assertNotIn(wt, state.load_state().worktrees)
            finally:
                self._teardown()

    def test_sprout_failure_writes_no_row(self):
        """Sprout itself failed: nothing to clean, nothing persisted."""
        from sprout_worktree_db.errors import SproutError

        with tempfile.TemporaryDirectory() as tmp:
            cfg, wt = self._setup(tmp)
            try:
                with mock.patch(
                    "sprout_worktree_db.provision.provision_dedicated",
                    side_effect=SproutError("sprout boom"),
                ):
                    with self.assertRaises(PluginError):
                        do_provision(
                            cfg,
                            {},
                            ProvisionRequest(
                                worktree=wt,
                                mode="dedicated",
                                with_steps=False,
                            ),
                        )
                self.assertNotIn(wt, state.load_state().worktrees)
            finally:
                self._teardown()

    def test_post_finalize_failure_keeps_claim(self):
        """Env merge after finalize: claim has the object, GC owns orphans."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg, wt = self._setup(tmp)
            try:
                inj = EnvInjection(
                    object_name="sprout_wt_new",
                    env_files=(),
                    pending_env={},
                )
                with (
                    mock.patch(
                        "sprout_worktree_db.provision.provision_dedicated",
                        return_value=inj,
                    ),
                    mock.patch(
                        "sprout_worktree_db.provision._apply_pending_env",
                        side_effect=RuntimeError("env boom"),
                    ),
                ):
                    with self.assertRaises(RuntimeError):
                        do_provision(
                            cfg,
                            {},
                            ProvisionRequest(
                                worktree=wt,
                                mode="dedicated",
                                with_steps=False,
                            ),
                        )
                self.assertEqual(
                    state.load_state().worktrees[wt].object, "sprout_wt_new"
                )
            finally:
                self._teardown()

    def test_crash_before_finalize_is_gc_plannable(self):
        """No row + leftover DB ⇒ GC plans a pathless postgres orphan."""
        from sprout_worktree_db import gc as gc_mod

        st = PluginState(worktrees={})
        plans = gc_mod.plan_orphans(st, set(), ["sprout_wt_new"])
        self.assertEqual(
            [p.object_name for p in plans], ["sprout_wt_new"]
        )


if __name__ == "__main__":
    unittest.main()

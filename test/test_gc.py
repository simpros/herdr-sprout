#!/usr/bin/env python3
"""GC / orphan planning tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from _helpers import ROOT  # noqa: F401 — ensures sys.path

from sprout_worktree_db import gc as gc_mod
from sprout_worktree_db.models import DropLease, PluginState, WorktreeRecord


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
            self.assertFalse(by_obj["sprout_wt_app_gone"].skip_postgres)
            self.assertIn("sprout_wt_orphan_only", by_obj)
            preview = next(p for p in plans if p.skip_postgres and p.state_path)
            self.assertTrue(preview.skip_postgres)
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

    def test_live_objects_skips_forget_only_leases(self):
        """Preview/forget leases must not invent a sprout_wt_* live name."""
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "feature"
            wt.mkdir()
            state_data = PluginState(
                worktrees={
                    str(wt): WorktreeRecord(
                        key="app-prev",
                        repo="app",
                        mode="preview",
                        object="sprout_shared_pr1",
                        created_at="",
                    )
                },
                dropping={
                    "app-prev": DropLease(
                        lease_id=1,
                        key="app-prev",
                        worktrees=(str(wt),),
                        object_name="",
                        skip_postgres=True,
                        reserved_at="2020-01-01T00:00:00+00:00",
                    )
                },
            )
            live = gc_mod.live_objects_from_state(
                state_data, {os.path.realpath(str(wt))}
            )
            self.assertEqual(live, set())
            # Real orphan with same key shape must still be planable.
            plans = gc_mod.plan_orphans(
                state_data,
                {os.path.realpath(str(wt))},
                ["sprout_wt_app_prev"],
            )
            # key app-prev is in dropping → postgres orphan skipped by lease
            self.assertEqual(plans, [])
            # Different orphan key still appears.
            plans2 = gc_mod.plan_orphans(
                PluginState(dropping=state_data.dropping),
                set(),
                ["sprout_wt_other_orphan"],
            )
            self.assertEqual(
                [p.object_name for p in plans2], ["sprout_wt_other_orphan"]
            )

    def test_plan_skips_dropping_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            gone = str(Path(tmp) / "gone")
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


if __name__ == "__main__":
    unittest.main()

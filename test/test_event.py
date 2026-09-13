#!/usr/bin/env python3
"""Event path resolution tests."""

from __future__ import annotations

import unittest

from _helpers import ROOT  # noqa: F401 — ensures sys.path

from sprout_worktree_db import event


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


if __name__ == "__main__":
    unittest.main()

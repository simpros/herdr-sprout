"""Domain error hierarchy (typed alternative to interpreter abort).

Every expected failure in this package — bad config, lease contention,
corrupt state — raises a :class:`PluginError` subclass instead of a bare
``SystemExit`` or ``RuntimeError`` so callers can distinguish domain errors
from an interpreter abort (``except PluginError`` vs ``except SystemExit``).

``PluginError`` subclasses ``Exception`` on purpose: the shared drop
executor aborts leases under ``except Exception``, and a ``SystemExit``
base would punch through that net and leave exclusive leases stuck.
``cli.main`` / ``hook`` map ``PluginError`` explicitly to exit codes —
that mapping is the real error boundary.
"""

from __future__ import annotations


class PluginError(Exception):
    """Base for all expected domain failures (CLI maps to exit 1)."""


class BusyError(PluginError):
    """Exclusive-lease contention: another op holds the slug.

    Callers that hit this should back off / report busy, not wipe state.
    """


class ConfigError(PluginError):
    """Bad config, secrets, CLI input, or repo/path resolution."""


class CorruptStateError(PluginError):
    """``state.json`` shape violations — fail closed, never wipe claims."""


class SproutError(PluginError):
    """Expected operational failure (sprout CLI, attach, drop, lost lease).

    Every foreseeable provision/drop failure raises this instead of a bare
    ``RuntimeError`` so CLI ``main`` maps it to a one-line ``exit 1``
    (and hooks log cleanly). Keep ``RuntimeError`` for genuine
    programming bugs only.
    """

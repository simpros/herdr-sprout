"""Domain error hierarchy (typed alternative to bare ``SystemExit``).

Every expected failure in this package — bad config, lease contention,
corrupt state — raises a :class:`PluginError` subclass instead of a bare
``SystemExit`` so callers can distinguish domain errors from an
interpreter abort (``except PluginError`` vs ``except SystemExit``).

``PluginError`` subclasses ``SystemExit`` on purpose: a ``PluginError("msg")``
still exits 1 with the message on stderr when it propagates out of the CLI,
and existing ``except SystemExit`` handling keeps working. New code should
catch ``PluginError`` (or a subclass) explicitly.
"""

from __future__ import annotations


class PluginError(SystemExit):
    """Base for all expected domain failures (exit 1 with a message)."""


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

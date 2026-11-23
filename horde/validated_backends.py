# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Determine which text workers run a validated backend, using rules that admins store in the database.

A text request with ``validated_backends`` set goes only to workers whose reported software matches a rule. A worker
that does not match any rule earns 30% of the text reward. The rules replace the ``LLM_VALIDATED_BACKENDS`` set that
was hard-coded in ``horde/bridge_reference.py``. The five entries of that set became the seed rules.

This module holds the matching logic and the process's snapshot of the rules. It does not import any database model, so
the worker classes can import it. [`horde.database.validated_backends`][horde.database.validated_backends] reads and
writes the stored rules, and importing that module sets the loader of the process's registry.

- [`ValidatedBackendRule`][horde.validated_backends.ValidatedBackendRule]: an immutable copy of one rule, with the
  matching logic.
- [`ValidatedBackendRegistry`][horde.validated_backends.ValidatedBackendRegistry]: the process-local snapshot of the
  rules, read on the hot path and refreshed every [`REGISTRY_REFRESH_SECONDS`][horde.validated_backends.REGISTRY_REFRESH_SECONDS].
- [`validated_backend_registry`][horde.validated_backends.validated_backend_registry]: the process's registry.
- [`LEGACY_VALIDATED_BACKEND_SEEDS`][horde.validated_backends.LEGACY_VALIDATED_BACKEND_SEEDS]: the initial rules of a
  new table.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import semver
from sqlalchemy.exc import SQLAlchemyError

from horde.logger import logger
from horde.worker_software import WorkerSoftware

__all__ = [
    "LEGACY_VALIDATED_BACKEND_SEEDS",
    "REGISTRY_REFRESH_SECONDS",
    "UNVALIDATED_BACKEND_REWARD_MULTIPLIER",
    "RuleLoader",
    "ValidatedBackendFields",
    "ValidatedBackendRegistry",
    "ValidatedBackendReturnCode",
    "ValidatedBackendRule",
    "meets_minimum_version",
    "parse_minimum_version",
    "validated_backend_registry",
]

REGISTRY_REFRESH_SECONDS: Final = 15.0
"""How long a process uses its snapshot of the rules before reading them again.

An admin change drops the snapshot at once in the process that handled the change. Other processes apply the change
within this many seconds.
"""

UNVALIDATED_BACKEND_REWARD_MULTIPLIER: Final = 0.3
"""The share of the text generation and uptime rewards earned by a worker whose software does not match any rule."""


class ValidatedBackendReturnCode:
    """Group the return codes of the validated backend admin API."""

    EXISTS: Final = "ValidatedBackendExists"
    """A rule for the same bridge and backend already exists."""
    INVALID: Final = "ValidatedBackendInvalid"
    """A field of the rule body is invalid or unknown."""
    NOT_FOUND: Final = "ValidatedBackendNotFound"
    """The id does not match any rule."""


@dataclass(frozen=True, kw_only=True)
class ValidatedBackendFields:
    """Represents the admin-editable fields of a rule, as validated by the API before they reach the database."""

    bridge_name: str
    """The required bridge name."""
    backend_name: str | None
    """The required backend name, or None to match a worker that does not report a backend."""
    min_bridge_version: str | None
    """The lowest accepted bridge version, or None for any."""
    min_backend_version: str | None
    """The lowest accepted backend version, or None for any."""
    description: str | None
    """The admin's note."""


@dataclass(frozen=True, kw_only=True)
class ValidatedBackendRule:
    """Represents a copy of one rule for reads on the hot path, detached from the database session."""

    bridge_name: str
    """The required bridge name."""
    backend_name: str | None = None
    """The required backend name, or None to match a worker that does not report a backend."""
    min_bridge_version: str | None = None
    """The lowest accepted bridge version, or None for any."""
    min_backend_version: str | None = None
    """The lowest accepted backend version, or None for any."""

    def matches(self, software: WorkerSoftware) -> bool:
        """Return whether a worker's reported software satisfies the rule.

        Args:
            software: The worker's reported bridge and backend.

        Returns:
            True when the names match without regard to case and every minimum version is met.
        """
        if software.bridge_name.casefold() != self.bridge_name.casefold():
            return False
        if not self._backend_name_matches(software.backend_name):
            return False
        if self.min_bridge_version is not None and not meets_minimum_version(software.bridge_version, self.min_bridge_version):
            return False
        if self.min_backend_version is not None and not meets_minimum_version(software.backend_version, self.min_backend_version):
            return False
        return True

    def _backend_name_matches(self, reported_backend_name: str | None) -> bool:
        """Return whether the reported backend name matches the rule's backend name. None matches only None."""
        if self.backend_name is None or reported_backend_name is None:
            return self.backend_name is None and reported_backend_name is None
        return reported_backend_name.casefold() == self.backend_name.casefold()


LEGACY_VALIDATED_BACKEND_SEEDS: Final[tuple[ValidatedBackendFields, ...]] = (
    ValidatedBackendFields(
        bridge_name="AI Horde Worker",
        backend_name=None,
        min_bridge_version=None,
        min_backend_version=None,
        description="Seeded from LLM_VALIDATED_BACKENDS: the AI Horde Worker scribe before it reported a backend.",
    ),
    ValidatedBackendFields(
        bridge_name="AI Horde Worker",
        backend_name="aphrodite",
        min_bridge_version=None,
        min_backend_version=None,
        description="Seeded from LLM_VALIDATED_BACKENDS: aphrodite-engine through its OpenAI or KoboldAI API.",
    ),
    ValidatedBackendFields(
        bridge_name="KoboldCppEmbedWorker",
        backend_name=None,
        min_bridge_version=None,
        min_backend_version=None,
        description="Seeded from LLM_VALIDATED_BACKENDS: koboldcpp's embedded worker.",
    ),
    ValidatedBackendFields(
        bridge_name="TabbyAPI",
        backend_name=None,
        min_bridge_version=None,
        min_backend_version=None,
        description="Seeded from LLM_VALIDATED_BACKENDS: TabbyAPI's embedded worker.",
    ),
)
"""The initial rules of a new ``validated_backends`` table, equivalent to the set hard-coded in ``bridge_reference``.

The legacy entries ``AI Horde Worker~aphrodite~oai`` and ``AI Horde Worker~aphrodite~kai`` became one rule, because the
API dialect is reported separately from the backend and is not part of a rule.
"""


def parse_minimum_version(version: str) -> semver.Version:
    """Parse a version as a loose semantic version, so ``24`` is parsed as ``24.0.0``.

    Args:
        version: The version string.

    Returns:
        The parsed version.

    Raises:
        ValueError: The string is not a loose semantic version.
    """
    try:
        return semver.Version.parse(version, optional_minor_and_patch=True)
    except TypeError as type_error:
        raise ValueError(f"'{version}' is not a version") from type_error


def meets_minimum_version(reported_version: str | None, minimum_version: str) -> bool:
    """Return whether a reported version is at least a rule's minimum.

    Args:
        reported_version: The worker's reported version, or None.
        minimum_version: The rule's minimum, already validated by
            [`parse_minimum_version`][horde.validated_backends.parse_minimum_version].

    Returns:
        True when the reported version is at least the minimum. False when the worker did not report a version or the
        reported version does not parse.
    """
    if reported_version is None:
        return False
    try:
        parsed_reported_version = parse_minimum_version(reported_version)
    except ValueError:
        return False
    return parsed_reported_version >= parse_minimum_version(minimum_version)


type RuleLoader = Callable[[], tuple["ValidatedBackendRule", ...]]
"""A function that reads the current rules and raises ``SQLAlchemyError`` when the database cannot be read."""


class ValidatedBackendRegistry:
    """Hold a process-local snapshot of the validated backend rules and match worker software against it.

    The pop query, the eligibility check and the text reward calculation query the registry for every candidate
    worker, so the snapshot keeps those reads off the database. Once the snapshot is older than the refresh interval,
    the next call reads the rules again, inside the caller's application context and database session.

    Thread Safety:
        Only one thread refreshes at a time, under a lock. A reader takes the current tuple of rules without the lock,
        because the tuple is not mutated.
    """

    def __init__(
        self,
        *,
        refresh_seconds: float = REGISTRY_REFRESH_SECONDS,
        load_rules: RuleLoader | None = None,
    ) -> None:
        """Create an empty registry, which reads the rules on first use.

        Args:
            refresh_seconds: How long a snapshot is used before the rules are read again.
            load_rules: The function that reads the rules, or None until one is set with
                [`use_loader`][horde.validated_backends.ValidatedBackendRegistry.use_loader].
        """
        self._refresh_seconds: float = refresh_seconds
        self._load_rules: RuleLoader | None = load_rules
        self._rules: tuple[ValidatedBackendRule, ...] | None = None
        self._loaded_at: float = 0.0
        self._refresh_lock: threading.Lock = threading.Lock()

    def get_rules(self) -> tuple[ValidatedBackendRule, ...]:
        """Return the current rules, reading them from the database when the snapshot is missing or stale.

        Returns:
            The rules, in no particular order.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: The first read failed. A failed refresh of an existing snapshot is logged
                and the previous snapshot is kept for another interval.
            RuntimeError: A loader was not set.
        """
        current_rules = self._rules
        snapshot_is_fresh = current_rules is not None and time.monotonic() - self._loaded_at < self._refresh_seconds
        if snapshot_is_fresh and current_rules is not None:
            return current_rules
        with self._refresh_lock:
            # Another thread may have refreshed the snapshot while this thread waited for the lock.
            if self._rules is not None and time.monotonic() - self._loaded_at < self._refresh_seconds:
                return self._rules
            return self._refresh_locked()

    def is_validated(self, software: WorkerSoftware) -> bool:
        """Return whether a worker's reported software matches any rule.

        Args:
            software: The worker's reported bridge and backend.

        Returns:
            True when a rule matches.
        """
        return any(rule.matches(software) for rule in self.get_rules())

    def use_loader(self, load_rules: RuleLoader) -> None:
        """Mutate the registry to read the rules with the given loader, and drop the current snapshot.

        Args:
            load_rules: The function that reads the rules.
        """
        with self._refresh_lock:
            self._load_rules = load_rules
            self._rules = None
            self._loaded_at = 0.0

    def invalidate(self) -> None:
        """Mutate the registry to drop the snapshot, so the next call reads the rules from the database."""
        with self._refresh_lock:
            self._rules = None
            self._loaded_at = 0.0

    def _refresh_locked(self) -> tuple[ValidatedBackendRule, ...]:
        """Read the rules into a new snapshot. The caller must hold the refresh lock.

        Raises:
            RuntimeError: A loader was not set, because ``horde.database.validated_backends`` was not imported.
            sqlalchemy.exc.SQLAlchemyError: The read failed before any snapshot was loaded.
        """
        if self._load_rules is None:
            raise RuntimeError("The validated backend registry does not have a loader. Import horde.database.validated_backends to set it.")
        try:
            loaded_rules = self._load_rules()
        except SQLAlchemyError as refresh_error:
            if self._rules is None:
                raise
            logger.error(f"Could not refresh the validated backend rules, keeping the previous snapshot: {refresh_error}")
            self._loaded_at = time.monotonic()
            return self._rules
        self._rules = loaded_rules
        self._loaded_at = time.monotonic()
        return loaded_rules


validated_backend_registry: Final = ValidatedBackendRegistry()
"""The process's registry, read by the pop query, the eligibility check and the text reward calculation."""

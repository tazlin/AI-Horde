# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read and write the stored validated backend rules.

Importing this module sets [`load_stored_rules`][horde.database.validated_backends.load_stored_rules] as the loader of
the process's [`validated_backend_registry`][horde.validated_backends.validated_backend_registry]. ``horde.classes``
imports this module, so the registry works in any process that imports the worker models.

- [`seed_validated_backends`][horde.database.validated_backends.seed_validated_backends]: store the seed rules.
- [`list_validated_backends`][horde.database.validated_backends.list_validated_backends] and
  [`find_validated_backend`][horde.database.validated_backends.find_validated_backend]: read the rules.
- [`create_validated_backend`][horde.database.validated_backends.create_validated_backend],
  [`update_validated_backend`][horde.database.validated_backends.update_validated_backend] and
  [`delete_validated_backend`][horde.database.validated_backends.delete_validated_backend]: the admin writes.
  Each write refreshes this process's registry snapshot at once. Other processes load the change within the refresh
  interval.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from horde import exceptions as e
from horde.classes.base.validated_backend import ValidatedBackend
from horde.flask import SQLITE_MODE, db
from horde.validated_backends import (
    LEGACY_VALIDATED_BACKEND_SEEDS,
    ValidatedBackendFields,
    ValidatedBackendReturnCode,
    ValidatedBackendRule,
    validated_backend_registry,
)

__all__ = [
    "create_validated_backend",
    "delete_validated_backend",
    "find_validated_backend",
    "list_validated_backends",
    "load_stored_rules",
    "seed_validated_backends",
    "update_validated_backend",
]


def _rule_from_row(row: ValidatedBackend) -> ValidatedBackendRule:
    """Convert a stored rule into the detached form held in the registry snapshot."""
    return ValidatedBackendRule(
        bridge_name=row.bridge_name,
        backend_name=row.backend_name,
        min_bridge_version=row.min_bridge_version,
        min_backend_version=row.min_backend_version,
    )


def load_stored_rules() -> tuple[ValidatedBackendRule, ...]:
    """Return every stored rule, read in the caller's application context and session."""
    rows = db.session.execute(select(ValidatedBackend)).scalars().all()
    return tuple(_rule_from_row(row) for row in rows)


def seed_validated_backends(seeds: Iterable[ValidatedBackendFields] = LEGACY_VALIDATED_BACKEND_SEEDS) -> None:
    """Add the seed rules that are not stored yet, then commit.

    ``init_db`` calls this only when it creates the table, so rules deleted by an admin are not restored on the next
    boot.

    Args:
        seeds: The rules to add.
    """
    for seed in seeds:
        if _find_rule_by_identity(bridge_name=seed.bridge_name, backend_name=seed.backend_name) is not None:
            continue
        db.session.add(_new_row(seed, created_by_user_id=None))
    db.session.commit()
    validated_backend_registry.invalidate()


def list_validated_backends() -> list[ValidatedBackend]:
    """Return every stored rule, ordered by bridge and backend name."""
    statement = select(ValidatedBackend).order_by(
        func.lower(ValidatedBackend.bridge_name),
        func.lower(func.coalesce(ValidatedBackend.backend_name, "")),
    )
    return list(db.session.execute(statement).scalars().all())


def find_validated_backend(rule_id: str) -> ValidatedBackend:
    """Return the stored rule with the given id.

    Args:
        rule_id: The rule's id as sent by the client.

    Returns:
        The rule.

    Raises:
        horde.exceptions.NotFound: With ``ValidatedBackendNotFound`` when the id is not a UUID or does not match a
            stored rule.
    """
    not_found_message = f"A validated backend rule with the id '{rule_id}' does not exist."
    try:
        parsed_rule_id = uuid.UUID(rule_id)
    except ValueError as invalid_id_error:
        raise e.NotFound(not_found_message, rc=ValidatedBackendReturnCode.NOT_FOUND) from invalid_id_error
    found_rule = db.session.get(ValidatedBackend, parsed_rule_id if not SQLITE_MODE else str(parsed_rule_id))
    if found_rule is None:
        raise e.NotFound(not_found_message, rc=ValidatedBackendReturnCode.NOT_FOUND)
    return found_rule


def create_validated_backend(fields: ValidatedBackendFields, *, created_by_user_id: int) -> ValidatedBackend:
    """Create a rule and commit it.

    Args:
        fields: The validated fields.
        created_by_user_id: The admin's user id.

    Returns:
        The stored rule.

    Raises:
        horde.exceptions.BadRequest: With ``ValidatedBackendExists`` when a rule for the bridge and backend already
            exists.
    """
    _refuse_duplicate_identity(fields, existing_rule_id=None)
    new_rule = _new_row(fields, created_by_user_id=created_by_user_id)
    db.session.add(new_rule)
    _commit_rule_write(fields)
    return new_rule


def update_validated_backend(rule: ValidatedBackend, fields: ValidatedBackendFields) -> ValidatedBackend:
    """Mutate a stored rule to match the given fields, and commit it.

    Args:
        rule: The rule to change.
        fields: The validated fields. Every field is applied.

    Returns:
        The changed rule.

    Raises:
        horde.exceptions.BadRequest: With ``ValidatedBackendExists`` when another rule has the new bridge and backend.
    """
    _refuse_duplicate_identity(fields, existing_rule_id=rule.id)
    rule.bridge_name = fields.bridge_name
    rule.backend_name = fields.backend_name
    rule.min_bridge_version = fields.min_bridge_version
    rule.min_backend_version = fields.min_backend_version
    rule.description = fields.description
    _commit_rule_write(fields)
    return rule


def delete_validated_backend(rule: ValidatedBackend) -> None:
    """Delete a stored rule and commit.

    Args:
        rule: The rule to delete.
    """
    db.session.delete(rule)
    db.session.commit()
    validated_backend_registry.invalidate()


def _new_row(fields: ValidatedBackendFields, *, created_by_user_id: int | None) -> ValidatedBackend:
    """Create an unsaved row from validated fields."""
    return ValidatedBackend(
        bridge_name=fields.bridge_name,
        backend_name=fields.backend_name,
        min_bridge_version=fields.min_bridge_version,
        min_backend_version=fields.min_backend_version,
        description=fields.description,
        created_by_user_id=created_by_user_id,
    )


def _find_rule_by_identity(*, bridge_name: str, backend_name: str | None) -> ValidatedBackend | None:
    """Return the stored rule for a bridge and backend pair, compared case-insensitively as in the unique index."""
    statement = select(ValidatedBackend).where(
        func.lower(ValidatedBackend.bridge_name) == bridge_name.lower(),
        func.lower(func.coalesce(ValidatedBackend.backend_name, "")) == (backend_name or "").lower(),
    )
    return db.session.execute(statement).scalars().first()


def _refuse_duplicate_identity(fields: ValidatedBackendFields, *, existing_rule_id: uuid.UUID | str | None) -> None:
    """Validate that another rule does not have the same bridge and backend.

    Raises:
        horde.exceptions.BadRequest: With ``ValidatedBackendExists`` when another rule has them.
    """
    duplicate_rule = _find_rule_by_identity(bridge_name=fields.bridge_name, backend_name=fields.backend_name)
    if duplicate_rule is None or str(duplicate_rule.id) == str(existing_rule_id):
        return
    raise e.BadRequest(
        f"A validated backend rule for bridge '{fields.bridge_name}' and backend '{fields.backend_name}' already exists.",
        rc=ValidatedBackendReturnCode.EXISTS,
    )


def _commit_rule_write(fields: ValidatedBackendFields) -> None:
    """Commit a created or changed rule and refresh this process's registry snapshot.

    Raises:
        horde.exceptions.BadRequest: With ``ValidatedBackendExists`` when a concurrent write stored the same bridge
            and backend first, so the unique index rejects this commit.
    """
    try:
        db.session.commit()
    except IntegrityError as integrity_error:
        db.session.rollback()
        raise e.BadRequest(
            f"A validated backend rule for bridge '{fields.bridge_name}' and backend '{fields.backend_name}' already exists.",
            rc=ValidatedBackendReturnCode.EXISTS,
        ) from integrity_error
    validated_backend_registry.invalidate()


validated_backend_registry.use_loader(load_stored_rules)

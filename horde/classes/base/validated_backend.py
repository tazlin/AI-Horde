# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Define the database table of the validated backend rules.

- [`ValidatedBackend`][horde.classes.base.validated_backend.ValidatedBackend]: one rule, a bridge with or without a
  backend. Workers that match a rule serve ``validated_backends`` requests and earn the full text reward.
- [`ValidatedBackendColumnLimits`][horde.classes.base.validated_backend.ValidatedBackendColumnLimits]: the column
  lengths, which the admin API also validates.

The registry that reads these rows on the hot path is [`horde.validated_backends`][horde.validated_backends].
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped

from horde.classes.base.sql_constructs import UtcNow
from horde.flask import SQLITE_MODE, db
from horde.utils import get_db_uuid
from horde.worker_software import SoftwareReportLimits

__all__ = [
    "ValidatedBackend",
    "ValidatedBackendColumnLimits",
]


class ValidatedBackendColumnLimits:
    """Group the column lengths of the ``validated_backends`` table."""

    MAX_NAME_LENGTH: Final = SoftwareReportLimits.MAX_NAME_LENGTH
    """The longest bridge or backend name, equal to the limit for a worker's report."""
    MAX_VERSION_LENGTH: Final = SoftwareReportLimits.MAX_VERSION_LENGTH
    """The longest minimum version, equal to the limit for a worker's report."""
    MAX_DESCRIPTION_LENGTH: Final = 500
    """The longest admin note."""


class ValidatedBackend(db.Model):
    """Represents one validated backend rule.

    A worker matches the rule when it reports the rule's bridge name and, if the rule has a backend name, that backend
    name. A rule without a backend matches only a worker that does not report a backend, such as a bridge embedded in
    its backend. Names are compared without regard to case. A minimum version, when set, has to be met by the reported
    version, compared as a loose semantic version. A worker that does not report the version does not meet the minimum.
    """

    __tablename__ = "validated_backends"

    id: Mapped[uuid.UUID | str] = db.Column(
        UUID(as_uuid=True) if not SQLITE_MODE else db.String(36),
        primary_key=True,
        default=get_db_uuid,
    )
    """The rule's id."""
    bridge_name: Mapped[str] = db.Column(db.String(ValidatedBackendColumnLimits.MAX_NAME_LENGTH), nullable=False)
    """The required bridge name."""
    backend_name: Mapped[str | None] = db.Column(db.String(ValidatedBackendColumnLimits.MAX_NAME_LENGTH), nullable=True)
    """The required backend name, or None to match a worker that does not report a backend."""
    min_bridge_version: Mapped[str | None] = db.Column(db.String(ValidatedBackendColumnLimits.MAX_VERSION_LENGTH))
    """The lowest accepted bridge version, or None for any version."""
    min_backend_version: Mapped[str | None] = db.Column(db.String(ValidatedBackendColumnLimits.MAX_VERSION_LENGTH))
    """The lowest accepted backend version, or None for any version, including a missing one."""
    description: Mapped[str | None] = db.Column(db.String(ValidatedBackendColumnLimits.MAX_DESCRIPTION_LENGTH))
    """The admin's note on why the rule exists."""
    created_by_user_id: Mapped[int | None] = db.Column(db.Integer, nullable=True)
    """The user id of the admin who created the rule, or None for a seeded rule."""
    created: Mapped[datetime] = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, server_default=UtcNow())
    """When the rule was created, in naive UTC."""
    updated: Mapped[datetime] = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default=UtcNow(),
    )
    """When the rule last changed, in naive UTC."""


# Allow one rule per bridge and backend pair, compared without regard to case. COALESCE maps a missing backend to an
# empty string, so two rules without a backend conflict. A plain unique constraint treats NULL values as distinct.
db.Index(
    "uq_validated_backends_identity",
    func.lower(ValidatedBackend.bridge_name),
    func.lower(func.coalesce(ValidatedBackend.backend_name, "")),
    unique=True,
)

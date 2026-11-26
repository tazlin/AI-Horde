# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Store which accounts were used from which networks.

A link row pairs an account with a network pseudonym (``CounterMeasures.ip_subject_key``). It does not hold the
address. The pseudonym is an HMAC of the address with the deployment secret, so the table cannot be read back into
addresses without that secret. It matches the pseudonyms already shown on prompt moderation evidence. Rows older than the
retention window are deleted by a timed job.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Mapped

from horde.classes.base.prompt_moderation import IP_SUBJECT_KEY_CHARACTERS
from horde.classes.base.sql_constructs import UtcNow
from horde.flask import db

__all__ = [
    "IpSubjectAccountLink",
]


class IpSubjectAccountLink(db.Model):
    """Represents one account seen on one network within the retention window."""

    __tablename__ = "ip_subject_account_links"
    __table_args__ = (
        db.UniqueConstraint("subject_key", "user_id", name="uq_ip_subject_account_link"),
        db.Index("ix_ip_subject_account_links_user_id", "user_id"),
        db.Index("ix_ip_subject_account_links_last_seen", "last_seen"),
    )

    id: Mapped[int] = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)
    subject_key: Mapped[str] = db.Column(db.String(IP_SUBJECT_KEY_CHARACTERS), nullable=False)
    """The network pseudonym (``CounterMeasures.ip_subject_key``)."""
    user_id: Mapped[int] = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    """The account seen on the network. Deleting the account deletes its links."""
    first_seen: Mapped[datetime] = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, server_default=UtcNow())
    """When the pair was first recorded in the current retention window."""
    last_seen: Mapped[datetime] = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, server_default=UtcNow())
    """When the pair was last recorded. The retention job deletes the row once this is older than the window."""

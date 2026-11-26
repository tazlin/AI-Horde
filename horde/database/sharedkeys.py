# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Change shared key policies and statuses, report filtered prompts, and count a key's waiting work.

The callers commit.
"""

from __future__ import annotations

from datetime import datetime

import logfire
from sqlalchemy import func

from horde.access_control import SharedKeyActor, SharedKeyLimits, SharedKeyPolicyInput, SharedKeyStatus
from horde.classes.base.user import UserSharedKey
from horde.classes.base.waiting_prompt import WaitingPrompt
from horde.flask import db
from horde.metrics import sharedkey_filtered_prompts, sharedkey_status_changes

__all__ = [
    "FILTERED_PROMPT_EVENT",
    "STATUS_CHANGED_EVENT",
    "apply_policy_update",
    "change_sharedkey_status",
    "count_sharedkey_waiting",
    "report_sharedkey_filtered_prompt",
]

FILTERED_PROMPT_EVENT: str = "horde.sharedkey.filtered_prompt"
"""The structured event emitted for each filtered prompt sent with a shared key."""

STATUS_CHANGED_EVENT: str = "horde.sharedkey.status_changed"
"""The structured event emitted for each suspension or reactivation of a shared key."""


def _clip_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    stripped_reason = reason.strip()
    if not stripped_reason:
        return None
    return stripped_reason[: SharedKeyLimits.MAX_STATUS_REASON_CHARACTERS]


def change_sharedkey_status(
    sharedkey: UserSharedKey,
    status: SharedKeyStatus,
    *,
    actor: SharedKeyActor,
    actor_user_id: int | None,
    reason: str | None,
) -> bool:
    """Set the key's status and emit ``STATUS_CHANGED_EVENT``.

    The event holds the key and owner IDs, the actor, the old and new status, and whether a reason was given. It does
    not hold the reason, which the key's owner and moderators read from the key's details.

    Args:
        sharedkey: The key.
        status: The new status.
        actor: Who made the change.
        actor_user_id: The account that made the change.
        reason: Why the change was made.

    Returns:
        True when the status changed. False when the key already had the status. Nothing is emitted then.
    """
    previous_status = sharedkey.get_status()
    if previous_status is status:
        return False
    sharedkey.status = status.value
    sharedkey.status_reason = _clip_reason(reason)
    sharedkey.status_changed = datetime.utcnow()
    sharedkey_status_changes.add(1, {"horde.actor": actor.value, "horde.status": status.value})
    logfire.info(
        STATUS_CHANGED_EVENT,
        sharedkey_id=str(sharedkey.id),
        owner_id=sharedkey.user_id,
        actor=actor.value,
        actor_user_id=actor_user_id,
        old_status=previous_status.value,
        new_status=status.value,
        has_reason=sharedkey.status_reason is not None,
    )
    return True


def apply_policy_update(sharedkey: UserSharedKey, policy: SharedKeyPolicyInput, *, actor_user_id: int) -> None:
    """Apply the policy fields that the owner sent. Fields left out of the request keep their values.

    Args:
        sharedkey: The key.
        policy: The owner's policy fields, already checked by
            [`check_owner_policy`][horde.access_control.check_owner_policy].
        actor_user_id: The owner's account ID.
    """
    for field_name, value in policy.model_dump(mode="json", exclude_unset=True, exclude={"status"}).items():
        setattr(sharedkey, field_name, value)
    if policy.status is not None:
        change_sharedkey_status(sharedkey, policy.status, actor=SharedKeyActor.OWNER, actor_user_id=actor_user_id, reason=None)


def report_sharedkey_filtered_prompt(sharedkey: UserSharedKey) -> None:
    """Emit ``FILTERED_PROMPT_EVENT`` for a filtered prompt sent with the key. The key's status does not change.

    The event holds the key and owner IDs. It does not hold the prompt or an address.

    Args:
        sharedkey: The key the refused prompt was sent with.
    """
    sharedkey_filtered_prompts.add(1)
    logfire.info(FILTERED_PROMPT_EVENT, sharedkey_id=str(sharedkey.id), owner_id=sharedkey.user_id)


def count_sharedkey_waiting(sharedkey_id: str) -> int:
    """Return the generations waiting under the key, across image and text requests.

    Args:
        sharedkey_id: The key's ID.

    Returns:
        The sum of the remaining generations of the key's active, unexpired requests.

    Performance:
        One aggregate over the ``sharedkey_id`` index of ``waiting_prompts``.
    """
    waiting_total = (
        db.session.query(func.coalesce(func.sum(WaitingPrompt.n), 0))
        .filter(
            WaitingPrompt.sharedkey_id == sharedkey_id,
            WaitingPrompt.active.is_(True),
            WaitingPrompt.faulted.is_(False),
            WaitingPrompt.terminal_outcome.is_(None),
            WaitingPrompt.expiry > datetime.utcnow(),
            WaitingPrompt.n >= 1,
        )
        .scalar()
    )
    return int(waiting_total)

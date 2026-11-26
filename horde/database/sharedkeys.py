# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Change shared key policies.

The callers commit.
"""

from __future__ import annotations

from horde.access_control import SharedKeyPolicyInput
from horde.classes.base.user import UserSharedKey

__all__ = [
    "apply_policy_update",
]


def apply_policy_update(sharedkey: UserSharedKey, policy: SharedKeyPolicyInput) -> None:
    """Apply the policy fields that the owner sent. Fields left out of the request keep their values.

    Args:
        sharedkey: The key.
        policy: The owner's policy fields, already checked by
            [`check_owner_policy`][horde.access_control.check_owner_policy].
    """
    for field_name, value in policy.model_dump(mode="json", exclude_unset=True).items():
        setattr(sharedkey, field_name, value)

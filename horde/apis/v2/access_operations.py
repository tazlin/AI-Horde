# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Expose moderator shared key suspension.

Every resource here requires a moderator ``apikey``. Every response has ``Cache-Control: private, no-store``, because
the payloads hold account identity and key policies.
"""

from __future__ import annotations

from typing import Any

from flask_restx import Resource, fields, reqparse

from horde import exceptions as e
from horde.access_control import MODERATOR_SETTABLE_STATUSES, SharedKeyActor, SharedKeyLimits, SharedKeyStatus
from horde.apis.v2.base import api, check_for_mod, models
from horde.apis.v2.moderation import PRIVATE_HEADERS, _add_moderator_credentials, _moderator_limits
from horde.classes.base.user import UserSharedKey
from horde.database import functions as database
from horde.database.sharedkeys import change_sharedkey_status
from horde.flask import db

__all__ = [
    "OperationsSharedKey",
]

_moderator_sharedkey_model = api.model(
    "ModeratorSharedKey",
    {
        "owner_id": fields.Integer(description="The key's owner."),
        "sharedkey": fields.Nested(models.response_model_sharedkey_details, description="The key, with its private policy."),
    },
)
_moderator_sharedkey_status_input = api.model(
    "ModeratorSharedKeyStatusInput",
    {
        "status": fields.String(
            required=True,
            enum=sorted(status.value for status in MODERATOR_SETTABLE_STATUSES),
            description="suspended_by_moderator stops the key until a moderator reactivates it. active lifts any suspension.",
        ),
        "reason": fields.String(
            required=True,
            min_length=1,
            max_length=SharedKeyLimits.MAX_STATUS_REASON_CHARACTERS,
            description="The reason for the change. It is shown to the key's owner in the key's details.",
        ),
    },
)


def _find_sharedkey_or_raise(sharedkey_id: str) -> UserSharedKey:
    sharedkey = database.find_sharedkey(str(sharedkey_id))
    if sharedkey is None:
        raise e.InvalidAPIKey("Shared Key Not Found.", keytype="Shared")
    return sharedkey


class OperationsSharedKey(Resource):
    """Let moderators inspect, suspend and reactivate any shared key."""

    decorators = _moderator_limits()

    get_parser = reqparse.RequestParser()
    _add_moderator_credentials(get_parser)

    @api.expect(get_parser)
    @api.marshal_with(_moderator_sharedkey_model, code=200, description="The key and its private policy")
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(403, "Access Denied", models.response_model_error)
    def get(self, sharedkey_id: str = "") -> tuple[dict[str, Any], int, dict[str, str]]:
        """Return a shared key with its private policy.

        Args:
            sharedkey_id: The key's ID.

        Returns:
            The key and its owner.
        """
        self.args = self.get_parser.parse_args()
        check_for_mod(self.args.apikey, "GET OperationsSharedKey")
        return self._payload(_find_sharedkey_or_raise(sharedkey_id))

    patch_parser = reqparse.RequestParser()
    _add_moderator_credentials(patch_parser)
    patch_parser.add_argument("status", type=str, required=True, location="json", help="The new status.")
    patch_parser.add_argument("reason", type=str, required=True, location="json", help="The reason for the change.")

    @api.expect(patch_parser, _moderator_sharedkey_status_input)
    @api.marshal_with(_moderator_sharedkey_model, code=200, description="The key after the change")
    @api.response(400, "Validation Error", models.response_model_error)
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(403, "Access Denied", models.response_model_error)
    def patch(self, sharedkey_id: str = "") -> tuple[dict[str, Any], int, dict[str, str]]:
        """Suspend a shared key, or lift any suspension of it, and store the moderator's reason on the key.

        Args:
            sharedkey_id: The key's ID.

        Returns:
            The key and its owner.

        Raises:
            e.BadRequest: The status is not one that a moderator sets, or the reason is empty or too long.
        """
        self.args = self.patch_parser.parse_args()
        moderator = check_for_mod(self.args.apikey, "PATCH OperationsSharedKey")
        sharedkey = _find_sharedkey_or_raise(sharedkey_id)
        # A StrEnum member equals its value, so the membership test also refuses text that is not a status.
        if self.args.status not in MODERATOR_SETTABLE_STATUSES:
            raise e.BadRequest("status must be suspended_by_moderator or active.", rc="InvalidSharedKeyPolicy")
        reason = (self.args.reason or "").strip()
        if not reason or len(reason) > SharedKeyLimits.MAX_STATUS_REASON_CHARACTERS:
            raise e.BadRequest(
                f"reason must be 1 to {SharedKeyLimits.MAX_STATUS_REASON_CHARACTERS} characters.",
                rc="InvalidModerationNote",
            )
        change_sharedkey_status(
            sharedkey,
            SharedKeyStatus(self.args.status),
            actor=SharedKeyActor.MODERATOR,
            actor_user_id=moderator.id,
            reason=reason,
        )
        db.session.commit()
        return self._payload(sharedkey)

    @staticmethod
    def _payload(sharedkey: UserSharedKey) -> tuple[dict[str, Any], int, dict[str, str]]:
        payload = {
            "owner_id": sharedkey.user_id,
            "sharedkey": sharedkey.get_details(include_private_policy=True),
        }
        return payload, 200, PRIVATE_HEADERS

# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Expose moderator shared key suspension and linked-account review.

Every resource here requires a moderator ``apikey``. Every response has ``Cache-Control: private, no-store``, because
the payloads hold account identity, network pseudonyms and key policies.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from flask_restx import Resource, fields, reqparse

from horde import exceptions as e
from horde.access_control import MODERATOR_SETTABLE_STATUSES, SharedKeyActor, SharedKeyLimits, SharedKeyStatus
from horde.account_links import list_account_links, list_subject_accounts
from horde.apis.v2.base import api, check_for_mod, models
from horde.apis.v2.moderation import PRIVATE_HEADERS, _add_moderator_credentials, _assert_ip_subject_key, _moderator_limits
from horde.classes.base.user import User, UserSharedKey
from horde.database import functions as database
from horde.database.sharedkeys import change_sharedkey_status
from horde.flask import db

__all__ = [
    "OperationsAccountLinks",
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
_linked_account_model = api.model(
    "LinkedAccount",
    {
        "user_id": fields.Integer(),
        "username": fields.String(description="The account's unique alias."),
        "first_seen": fields.DateTime(dt_format="iso8601"),
        "last_seen": fields.DateTime(dt_format="iso8601"),
        "flagged": fields.Boolean(),
        "deleted": fields.Boolean(),
        "trusted": fields.Boolean(),
    },
)
_linked_network_model = api.model(
    "LinkedNetwork",
    {
        "subject_key": fields.String(description="The network pseudonym. It matches the ip_subject_key of prompt evidence."),
        "first_seen": fields.DateTime(dt_format="iso8601"),
        "last_seen": fields.DateTime(dt_format="iso8601"),
        "other_accounts": fields.List(fields.Nested(_linked_account_model)),
    },
)
_account_links_model = api.model(
    "AccountLinks",
    {
        "user_id": fields.Integer(allow_null=True, description="The account looked up, when one was given."),
        "networks": fields.List(
            fields.Nested(_linked_network_model),
            description="The account's networks in the last 30 days, the networks shared with other accounts first.",
        ),
        "accounts": fields.List(
            fields.Nested(_linked_account_model),
            description="The accounts seen on the given network pseudonym in the last 30 days.",
        ),
    },
)


def _find_sharedkey_or_raise(sharedkey_id: str) -> UserSharedKey:
    sharedkey = database.find_sharedkey(str(sharedkey_id))
    if sharedkey is None:
        raise e.InvalidAPIKey("Shared Key Not Found.", keytype="Shared")
    return sharedkey


def _find_user_or_raise(user_id: int) -> User:
    user = database.find_user_by_id(user_id)
    if user is None:
        raise e.UserNotFound(str(user_id))
    return user


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


class OperationsAccountLinks(Resource):
    """Let moderators see which accounts share networks."""

    decorators = _moderator_limits()

    get_parser = reqparse.RequestParser()
    _add_moderator_credentials(get_parser)
    get_parser.add_argument("user_id", type=int, required=False, location="args", help="List this account's networks.")
    get_parser.add_argument(
        "ip_subject_key",
        type=str,
        required=False,
        location="args",
        help="List the accounts seen on this network pseudonym, as shown on prompt evidence.",
    )

    @api.expect(get_parser)
    @api.marshal_with(_account_links_model, code=200, description="Linked accounts")
    @api.response(400, "Validation Error", models.response_model_error)
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(403, "Access Denied", models.response_model_error)
    @api.response(404, "User Not Found", models.response_model_error)
    def get(self) -> tuple[dict[str, Any], int, dict[str, str]]:
        """Return an account's networks with the other accounts on each, or the accounts on one network.

        Returns:
            The listing. Exactly one of user_id and ip_subject_key must be given.

        Raises:
            e.BadRequest: Neither or both filters were given, or the pseudonym is malformed.
        """
        self.args = self.get_parser.parse_args()
        check_for_mod(self.args.apikey, "GET OperationsAccountLinks")
        subject_key = _assert_ip_subject_key(self.args.ip_subject_key)
        has_user_filter = self.args.user_id is not None
        has_subject_filter = subject_key is not None
        if has_user_filter == has_subject_filter:
            raise e.BadRequest("Give exactly one of user_id and ip_subject_key.", rc="InvalidAccountLinkFilter")
        if has_user_filter:
            _find_user_or_raise(self.args.user_id)
            networks = [asdict(network) for network in list_account_links(self.args.user_id)]
            return {"user_id": self.args.user_id, "networks": networks, "accounts": []}, 200, PRIVATE_HEADERS
        accounts = [asdict(account) for account in list_subject_accounts(subject_key)]
        return {"user_id": None, "networks": [], "accounts": accounts}, 200, PRIVATE_HEADERS

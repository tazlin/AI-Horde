# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Provide the API endpoints of the validated backend registry. Anyone can read the rules, and admins can change them.

A text request with ``validated_backends`` set to true goes only to workers whose reported software matches a rule. A
text worker that does not match any rule earns reduced kudos. See [`horde.validated_backends`][horde.validated_backends]
for the matching logic and ``docs/decisions/0043-keep-validated-text-backends-in-an-admin-registry.md`` for the
design.

- [`ValidatedBackends`][horde.apis.v2.validated_backends.ValidatedBackends]: list the rules, or create one.
- [`SingleValidatedBackend`][horde.apis.v2.validated_backends.SingleValidatedBackend]: read, change or delete a rule.

A write needs the ``apikey`` of a user listed in the ``ADMINS`` environment variable.
"""

from __future__ import annotations

from typing import Annotated, Any

from flask import request
from flask_restx import Resource, fields, reqparse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, ValidationError

import horde.apis.limiter_api as lim
from horde import exceptions as e
from horde.apis.models.v2 import UTCDateTime
from horde.apis.v2.base import api, check_for_admin, models
from horde.classes.base.validated_backend import ValidatedBackend, ValidatedBackendColumnLimits
from horde.database.validated_backends import (
    create_validated_backend,
    delete_validated_backend,
    find_validated_backend,
    list_validated_backends,
    update_validated_backend,
)
from horde.limiter import limiter
from horde.validated_backends import ValidatedBackendFields, ValidatedBackendReturnCode, parse_minimum_version
from horde.worker_software import LEGACY_AGENT_FIELD_SEPARATOR, LEGACY_NAME_SEPARATOR

__all__ = [
    "SingleValidatedBackend",
    "ValidatedBackendInput",
    "ValidatedBackends",
]


def _refuse_reserved_characters(name: str) -> str:
    """Validate that a rule's name does not contain ``:`` or ``~``, which are not allowed in a worker's reported names.

    Raises:
        ValueError: The name contains ``:`` or ``~``, so a worker cannot report it.
    """
    if LEGACY_AGENT_FIELD_SEPARATOR in name or LEGACY_NAME_SEPARATOR in name:
        raise ValueError(f"must not contain '{LEGACY_AGENT_FIELD_SEPARATOR}' or '{LEGACY_NAME_SEPARATOR}'")
    return name


def _refuse_unparsable_version(version: str) -> str:
    """Validate that a minimum version is a loose semantic version, such as ``24`` or ``0.3.2``.

    Raises:
        ValueError: The version cannot be parsed.
    """
    parse_minimum_version(version)
    return version


RuleName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=ValidatedBackendColumnLimits.MAX_NAME_LENGTH),
    AfterValidator(_refuse_reserved_characters),
]
"""A bridge or backend name matched by a rule."""

RuleVersion = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=ValidatedBackendColumnLimits.MAX_VERSION_LENGTH),
    AfterValidator(_refuse_unparsable_version),
]
"""A minimum version required by a rule."""


class ValidatedBackendInput(BaseModel):
    """Represents the body of a create or change request.

    A create request needs ``bridge_name``. A change request changes only the fields present in the body. A JSON null
    clears an optional field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bridge_name: RuleName | None = None
    """The bridge name reported by a matching worker."""
    backend_name: RuleName | None = None
    """The backend name reported by a matching worker. Absent or null for a bridge that is built into its backend."""
    min_bridge_version: RuleVersion | None = None
    """The lowest accepted bridge version."""
    min_backend_version: RuleVersion | None = None
    """The lowest accepted backend version."""
    description: str | None = Field(default=None, max_length=ValidatedBackendColumnLimits.MAX_DESCRIPTION_LENGTH)
    """The admin's note on why the rule exists."""

    def to_fields(self, *, current_rule: ValidatedBackend | None = None) -> ValidatedBackendFields:
        """Return the rule's fields, using the current rule's value for each field that is absent from the body.

        Args:
            current_rule: The rule to change, or None for a create request.

        Returns:
            The complete rule fields.

        Raises:
            horde.exceptions.BadRequest: With ``ValidatedBackendInvalid`` when the resulting rule does not have a bridge
                name.
        """
        sent_fields = self.model_fields_set

        def resolve(field_name: str) -> Any:
            if field_name in sent_fields or current_rule is None:
                return getattr(self, field_name)
            return getattr(current_rule, field_name)

        bridge_name = resolve("bridge_name")
        if not bridge_name:
            raise e.BadRequest("A validated backend rule needs a bridge_name.", rc=ValidatedBackendReturnCode.INVALID)
        return ValidatedBackendFields(
            bridge_name=bridge_name,
            backend_name=resolve("backend_name"),
            min_bridge_version=resolve("min_bridge_version"),
            min_backend_version=resolve("min_backend_version"),
            description=resolve("description"),
        )


def _parse_rule_body() -> ValidatedBackendInput:
    """Parse the JSON body of a create or change request.

    Raises:
        horde.exceptions.BadRequest: With ``ValidatedBackendInvalid`` when the body is not a JSON object or a field is
            invalid.
    """
    raw_body = request.get_json(silent=True)
    if not isinstance(raw_body, dict):
        raise e.BadRequest("The body has to be a JSON object.", rc=ValidatedBackendReturnCode.INVALID)
    try:
        return ValidatedBackendInput.model_validate(raw_body)
    except ValidationError as validation_error:
        refused_fields = "; ".join(
            f"{'.'.join(str(location) for location in error['loc'])}: {error['msg']}" for error in validation_error.errors()
        )
        raise e.BadRequest(
            f"The validated backend rule is invalid: {refused_fields}",
            rc=ValidatedBackendReturnCode.INVALID,
        ) from validation_error


def _registry_limits() -> list[Any]:
    """Return the rate limits of the registry endpoints.

    A client gets 60 reads a minute per IP address and path, and 30 writes a minute per API key.
    """
    return [
        limiter.limit("60/minute", key_func=lim.get_request_path, methods=["GET"]),
        limiter.limit("30/minute", key_func=lim.get_request_api_key_per_method, methods=["POST", "PATCH", "DELETE"]),
    ]


def _add_admin_credentials(parser: reqparse.RequestParser) -> None:
    """Add the headers required for a write: the admin API key and the client agent."""
    parser.add_argument("apikey", type=str, required=True, help="An admin API key.", location="headers")
    parser.add_argument(
        "Client-Agent",
        default="unknown:0:unknown",
        type=str,
        required=False,
        help="The client name and version.",
        location="headers",
    )


_input_model_validated_backend = api.model(
    "ValidatedBackendInput",
    {
        "bridge_name": fields.String(
            example="AI Horde Worker",
            description="The bridge name reported by a matching worker. Required to create a rule.",
            max_length=ValidatedBackendColumnLimits.MAX_NAME_LENGTH,
        ),
        "backend_name": fields.String(
            example="aphrodite",
            description="The backend name reported by a matching worker. Null for a bridge that is built into its backend.",
            max_length=ValidatedBackendColumnLimits.MAX_NAME_LENGTH,
        ),
        "min_bridge_version": fields.String(
            example="25.0.0",
            description="The lowest accepted bridge version, compared as a loose semantic version. Null when any version is accepted.",
            max_length=ValidatedBackendColumnLimits.MAX_VERSION_LENGTH,
        ),
        "min_backend_version": fields.String(
            example="0.6.0",
            description=(
                "The lowest accepted backend version, compared as a loose semantic version. Null when any version is accepted. "
                "A worker that does not report a backend version does not meet this minimum."
            ),
            max_length=ValidatedBackendColumnLimits.MAX_VERSION_LENGTH,
        ),
        "description": fields.String(
            description="The admin's note on why the rule exists.",
            max_length=ValidatedBackendColumnLimits.MAX_DESCRIPTION_LENGTH,
        ),
    },
)

_response_model_validated_backend = api.model(
    "ValidatedBackend",
    {
        "id": fields.String(description="The rule's id."),
        "bridge_name": fields.String(
            description="The bridge name reported by a matching worker. The comparison ignores case.",
        ),
        "backend_name": fields.String(
            description=(
                "The backend name reported by a matching worker. The comparison ignores case. Null when the rule "
                "matches only workers that do not report a backend."
            ),
        ),
        "min_bridge_version": fields.String(
            description="The lowest accepted bridge version, or null when any version is accepted.",
        ),
        "min_backend_version": fields.String(
            description="The lowest accepted backend version, or null when any version is accepted.",
        ),
        "description": fields.String(description="The admin's note on why the rule exists."),
        "created": UTCDateTime(description="When the rule was created."),
        "updated": UTCDateTime(description="When the rule was last changed."),
    },
)


def _rule_details(rule: ValidatedBackend) -> dict[str, Any]:
    """Convert a stored rule to the ``ValidatedBackend`` response model."""
    return {
        "id": str(rule.id),
        "bridge_name": rule.bridge_name,
        "backend_name": rule.backend_name,
        "min_bridge_version": rule.min_bridge_version,
        "min_backend_version": rule.min_backend_version,
        "description": rule.description,
        "created": rule.created,
        "updated": rule.updated,
    }


class ValidatedBackends(Resource):
    """List the validated backend rules, or create one."""

    decorators = _registry_limits()

    post_parser = reqparse.RequestParser()
    _add_admin_credentials(post_parser)

    @api.marshal_with(_response_model_validated_backend, code=200, description="Every rule", as_list=True)
    def get(self) -> tuple[list[dict[str, Any]], int]:
        """List the validated backend rules.

        A text worker runs a validated backend when its reported software matches one of these rules.

        Returns:
            Every rule, ordered by bridge and backend name.
        """
        return [_rule_details(rule) for rule in list_validated_backends()], 200

    @api.expect(post_parser, _input_model_validated_backend)
    @api.marshal_with(_response_model_validated_backend, code=201, description="The stored rule")
    @api.response(400, "Validation Error", models.response_model_error)
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(403, "Access Denied", models.response_model_error)
    def post(self) -> tuple[dict[str, Any], int]:
        """Create a validated backend rule. Admins only.

        Returns:
            The stored rule.

        Raises:
            horde.exceptions.NotAdmin: The user of the API key is not an admin.
            horde.exceptions.BadRequest: The body is invalid, or a rule for the bridge and backend already exists.
        """
        self.args = self.post_parser.parse_args()
        admin = check_for_admin(self.args.apikey, "POST ValidatedBackends")
        rule_fields = _parse_rule_body().to_fields()
        stored_rule = create_validated_backend(rule_fields, created_by_user_id=admin.id)
        return _rule_details(stored_rule), 201


class SingleValidatedBackend(Resource):
    """Read, change or delete one validated backend rule."""

    decorators = _registry_limits()

    write_parser = reqparse.RequestParser()
    _add_admin_credentials(write_parser)

    @api.marshal_with(_response_model_validated_backend, code=200, description="The rule")
    @api.response(404, "Validated Backend Not Found", models.response_model_error)
    def get(self, rule_id: str) -> tuple[dict[str, Any], int]:
        """Read one validated backend rule.

        Args:
            rule_id: The rule's id.

        Returns:
            The rule.
        """
        return _rule_details(find_validated_backend(rule_id)), 200

    @api.expect(write_parser, _input_model_validated_backend)
    @api.marshal_with(_response_model_validated_backend, code=200, description="The changed rule")
    @api.response(400, "Validation Error", models.response_model_error)
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(403, "Access Denied", models.response_model_error)
    @api.response(404, "Validated Backend Not Found", models.response_model_error)
    def patch(self, rule_id: str) -> tuple[dict[str, Any], int]:
        """Change a validated backend rule. Admins only.

        Only the fields present in the request body change. A JSON null clears an optional field.

        Args:
            rule_id: The rule's id.

        Returns:
            The changed rule.
        """
        self.args = self.write_parser.parse_args()
        check_for_admin(self.args.apikey, "PATCH SingleValidatedBackend")
        current_rule = find_validated_backend(rule_id)
        rule_fields = _parse_rule_body().to_fields(current_rule=current_rule)
        changed_rule = update_validated_backend(current_rule, rule_fields)
        return _rule_details(changed_rule), 200

    @api.expect(write_parser)
    @api.response(200, "Deleted")
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(403, "Access Denied", models.response_model_error)
    @api.response(404, "Validated Backend Not Found", models.response_model_error)
    def delete(self, rule_id: str) -> tuple[dict[str, str], int]:
        """Delete a validated backend rule. Admins only.

        Args:
            rule_id: The rule's id.

        Returns:
            The deleted rule's id.
        """
        self.args = self.write_parser.parse_args()
        check_for_admin(self.args.apikey, "DELETE SingleValidatedBackend")
        delete_validated_backend(find_validated_backend(rule_id))
        return {"deleted_id": rule_id}, 200

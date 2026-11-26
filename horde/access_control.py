# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Decide what a request made with a shared key may do.

A request made with a shared key runs as the key's owner, with only the owner roles granted to the key. The owner's
restrictions always apply. The rules here are the single place where the effective roles of a request are decided.

Public members:

- [`SharedKeyRole`][horde.access_control.SharedKeyRole]: owner roles that may be granted to a key.
- [`RequestPrincipal`][horde.access_control.RequestPrincipal]: the effective roles of one request.
- [`SharedKeyPolicyInput`][horde.access_control.SharedKeyPolicyInput],
  [`parse_policy_input`][horde.access_control.parse_policy_input] and
  [`check_owner_policy`][horde.access_control.check_owner_policy]: owner input validation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from horde import exceptions as e
from horde.metrics import sharedkey_legacy_requests
from horde.patreon import patrons

if TYPE_CHECKING:
    from horde.classes.base.user import User, UserSharedKey

__all__ = [
    "RequestPrincipal",
    "SharedKeyPolicyInput",
    "SharedKeyRole",
    "check_owner_policy",
    "owner_grantable_roles",
    "parse_policy_input",
    "record_legacy_sharedkey_request",
]


class SharedKeyRole(StrEnum):
    """Represents an owner role that the owner may grant to a shared key.

    A key acts with a role only when the key was granted it and the owner holds it at the time of the request.
    The moderator role cannot be granted.
    """

    TRUSTED = "trusted"
    """The trusted and supporter allowances: batching control, extra source images, the raid-mode network check
    bypass and the rate-limit allowlist."""
    EDUCATION = "education"
    """The education allowances: the replacement filter and the rate-limit allowlist.

    The education restrictions (forced SFW and trusted workers) apply to every key of an education account whether or
    not this role is granted."""
    SPECIAL = "special"
    """Special models and the ``special`` payload."""
    SERVICE = "service"
    """Setting ``proxied_account`` and the rate-limit allowlist. Only a service account can grant it.

    A key without a role list does not act with this role."""


class RequestPrincipal:
    """Hold the effective roles of one request, made with a personal key or a shared key.

    For a personal key the effective roles are the account's roles. For a shared key each allowance role is the owner's
    role only when the key was granted it. The moderator role is not effective through a shared key.
    Restrictions (flagged, deleted, education restrictions) are read from the account directly and are not affected.

    Thread Safety:
        Instances are built per request and are not shared between threads.
    """

    def __init__(self, *, user: User, sharedkey: UserSharedKey | None = None) -> None:
        """Create the principal of a request.

        Args:
            user: The account the request acts for. For a shared key this is the key's owner, or the anonymous account
                when an empty key was downgraded.
            sharedkey: The shared key used for the request, if any.
        """
        self._user = user
        self._sharedkey = sharedkey
        self._granted_roles: frozenset[SharedKeyRole] | None = None
        if sharedkey is not None:
            key_roles = sharedkey.get_roles()
            # A key created before roles existed does not have a role list. Requests through it get every
            # allowance role that its owner holds.
            if key_roles is not None:
                self._granted_roles = frozenset(key_roles)

    @property
    def user(self) -> User:
        """Return the account the request acts for."""
        return self._user

    @property
    def uses_sharedkey(self) -> bool:
        """Return whether the request was made with a shared key."""
        return self._sharedkey is not None

    @property
    def uses_legacy_sharedkey(self) -> bool:
        """Return whether the request was made with a shared key without a role list."""
        return self._sharedkey is not None and self._granted_roles is None

    def key_allows(self, role: SharedKeyRole) -> bool:
        """Return whether the request's key passes the role on, without reading the owner's roles.

        A personal key and a key without a role list pass every role on.
        """
        if self._granted_roles is None:
            return True
        return role in self._granted_roles

    @property
    def trusted(self) -> bool:
        """Return whether the request has the trusted allowances."""
        return self._user.trusted and self.key_allows(SharedKeyRole.TRUSTED)

    @property
    def is_patron(self) -> bool:
        """Return whether the request has the supporter allowances, which a key gets with the trusted role."""
        return self.key_allows(SharedKeyRole.TRUSTED) and patrons.is_patron(self._user.id)

    @property
    def education(self) -> bool:
        """Return whether the request has the education allowances."""
        return self._user.education and self.key_allows(SharedKeyRole.EDUCATION)

    @property
    def special(self) -> bool:
        """Return whether the request may use special models and the special payload."""
        return self._user.special and self.key_allows(SharedKeyRole.SPECIAL)

    @property
    def moderator(self) -> bool:
        """Return whether the request has the moderator exemptions. A request through a shared key does not have them."""
        return self._user.moderator and not self.uses_sharedkey

    @property
    def service(self) -> bool:
        """Return whether the request may act for a proxied account.

        A request through a shared key may only when the key was granted the service role.
        """
        if not self._user.service:
            return False
        if self._sharedkey is None:
            return True
        return self._granted_roles is not None and SharedKeyRole.SERVICE in self._granted_roles


def record_legacy_sharedkey_request(principal: RequestPrincipal) -> None:
    """Count a request through a shared key without a role list.

    The count shows how many requests still act with every allowance role of the key's owner. Other requests are not
    counted.

    Args:
        principal: The request's principal.
    """
    if principal.uses_legacy_sharedkey:
        sharedkey_legacy_requests.add(1)


def owner_grantable_roles(owner: User) -> frozenset[SharedKeyRole]:
    """Return the roles that the owner holds and may therefore grant to a key.

    Args:
        owner: The key's owner.

    Returns:
        The roles among [`SharedKeyRole`][horde.access_control.SharedKeyRole] that the owner holds. Supporter status
        counts as holding the trusted role.
    """
    held_roles: set[SharedKeyRole] = set()
    if owner.trusted or patrons.is_patron(owner.id):
        held_roles.add(SharedKeyRole.TRUSTED)
    if owner.education:
        held_roles.add(SharedKeyRole.EDUCATION)
    if owner.special:
        held_roles.add(SharedKeyRole.SPECIAL)
    if owner.service:
        held_roles.add(SharedKeyRole.SERVICE)
    return frozenset(held_roles)


class SharedKeyPolicyInput(BaseModel):
    """Represents the policy fields that an owner sent when creating or updating a shared key.

    A field left out of the request is not in ``model_fields_set``, so an update changes only the fields sent. Repeated
    entries are kept once, in the order sent.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", use_attribute_docstrings=True)

    roles: list[SharedKeyRole] | None = None
    """The owner roles granted to the key. The owner must hold each of them."""

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, data: Any) -> Any:
        # The other fields of the shared key endpoints are parsed by reqparse, which reads a null as not sent. The
        # policy fields do the same.
        if not isinstance(data, dict):
            return data
        return {name: value for name, value in data.items() if value is not None}

    @field_validator("roles")
    @classmethod
    def _dedupe_roles(cls, roles: list[SharedKeyRole] | None) -> list[SharedKeyRole] | None:
        return None if roles is None else list(dict.fromkeys(roles))


def parse_policy_input(body: object) -> SharedKeyPolicyInput:
    """Parse the policy fields of a shared key request body. The body's other fields are ignored.

    Args:
        body: The decoded JSON body, or None when the request did not have one.

    Returns:
        The policy fields sent with the request.

    Raises:
        e.BadRequest: With ``InvalidSharedKeyPolicy``, listing every malformed or out-of-range value.
    """
    try:
        return SharedKeyPolicyInput.model_validate({} if body is None else body)
    except ValidationError as error:
        raise e.BadRequest(f"The shared key policy is not valid. {e.validation_reasons(error)}.", rc="InvalidSharedKeyPolicy") from None


def check_owner_policy(policy: SharedKeyPolicyInput, *, owner: User) -> None:
    """Refuse a policy that grants a role the owner does not hold.

    Args:
        policy: The policy fields sent by the owner.
        owner: The key's owner.

    Raises:
        e.Forbidden: The owner tried to grant a role they do not hold.
    """
    if policy.roles is not None:
        missing_roles = set(policy.roles) - owner_grantable_roles(owner)
        if missing_roles:
            missing_text = ", ".join(sorted(missing_roles))
            raise e.Forbidden(
                f"You can only grant a shared key roles that your account holds. Missing: {missing_text}.",
                rc="SharedKeyRoleNotHeld",
            )

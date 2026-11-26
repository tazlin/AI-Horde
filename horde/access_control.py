# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Decide what a request made with a shared key may do.

A request made with a shared key runs as the key's owner, with only the owner roles and request types granted to
the key. The owner's restrictions always apply. The rules here are the single place where the effective roles of a request are decided.

Public members:

- [`SharedKeyScope`][horde.access_control.SharedKeyScope]: request types a key may submit.
- [`SharedKeyRole`][horde.access_control.SharedKeyRole]: owner roles that may be granted to a key.
- [`SharedKeyStatus`][horde.access_control.SharedKeyStatus]: whether a key is usable, and who suspended it.
- [`RequestPrincipal`][horde.access_control.RequestPrincipal]: the effective roles of one request.
- [`SharedKeyPolicyInput`][horde.access_control.SharedKeyPolicyInput],
  [`parse_policy_input`][horde.access_control.parse_policy_input] and
  [`check_owner_policy`][horde.access_control.check_owner_policy]: owner input validation.
- [`check_sharedkey_access`][horde.access_control.check_sharedkey_access]: the per-request checks that do not need
  Redis or the database.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError, field_validator, model_validator

from horde import exceptions as e
from horde.metrics import sharedkey_legacy_requests, sharedkey_refusals
from horde.patreon import patrons

if TYPE_CHECKING:
    from horde.classes.base.user import User, UserSharedKey

__all__ = [
    "ALL_SCOPES",
    "GENTYPE_SCOPES",
    "MODERATOR_SETTABLE_STATUSES",
    "OWNER_SETTABLE_STATUSES",
    "RequestPrincipal",
    "SharedKeyAccessRequest",
    "SharedKeyActor",
    "SharedKeyLimits",
    "SharedKeyPolicyInput",
    "SharedKeyRefusal",
    "SharedKeyRole",
    "SharedKeyScope",
    "SharedKeyStatus",
    "check_owner_policy",
    "check_sharedkey_access",
    "normalize_networks",
    "owner_grantable_roles",
    "parse_policy_input",
    "record_legacy_sharedkey_request",
    "refuse_sharedkey_request",
]


class SharedKeyScope(StrEnum):
    """Represents a request type that a shared key may submit."""

    IMAGE_GENERATION = "image_generation"
    """Image generation requests (``/generate/async``)."""
    TEXT_GENERATION = "text_generation"
    """Text generation and chat requests (``/generate/text/async`` and ``/generate/chat/async``)."""


ALL_SCOPES: tuple[SharedKeyScope, ...] = tuple(SharedKeyScope)
"""The scopes of a new key when the owner does not choose any, and of every key created before scopes existed."""

GENTYPE_SCOPES: dict[str, SharedKeyScope] = {
    "image": SharedKeyScope.IMAGE_GENERATION,
    "text": SharedKeyScope.TEXT_GENERATION,
}
"""The scope needed by each ``GenerateTemplate.gentype``."""


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


class SharedKeyStatus(StrEnum):
    """Represents whether a shared key is usable, and who suspended it."""

    ACTIVE = "active"
    """The key is usable."""
    SUSPENDED_BY_OWNER = "suspended_by_owner"
    """The owner suspended the key. The owner can reactivate it."""
    SUSPENDED_BY_MODERATOR = "suspended_by_moderator"
    """A moderator suspended the key. Only a moderator can reactivate it."""


OWNER_SETTABLE_STATUSES: frozenset[SharedKeyStatus] = frozenset({SharedKeyStatus.ACTIVE, SharedKeyStatus.SUSPENDED_BY_OWNER})
"""The statuses that the owner may set through the shared key endpoints."""
MODERATOR_SETTABLE_STATUSES: frozenset[SharedKeyStatus] = frozenset({SharedKeyStatus.ACTIVE, SharedKeyStatus.SUSPENDED_BY_MODERATOR})
"""The statuses that a moderator may set. A moderator's reactivation lifts any suspension."""


class SharedKeyActor(StrEnum):
    """Represents who changed a key's status."""

    OWNER = "owner"
    MODERATOR = "moderator"


class SharedKeyRefusal(StrEnum):
    """Represents the return codes of a request refused by a shared key's policy."""

    SUSPENDED = "SharedKeySuspended"
    SCOPE_DENIED = "SharedKeyScopeDenied"
    NETWORK_DENIED = "SharedKeyNetworkDenied"
    MODEL_DENIED = "SharedKeyModelDenied"
    CONCURRENCY_EXCEEDED = "SharedKeyConcurrencyExceeded"


class SharedKeyLimits:
    """Group the bounds on a shared key's policy."""

    MAX_ALLOWED_MODELS: int = 100
    """The most models an allowlist may hold."""
    MAX_MODEL_NAME_CHARACTERS: int = 255
    """The longest model name an allowlist may hold."""
    MAX_ALLOWED_NETWORKS: int = 20
    """The most CIDR ranges a network allowlist may hold."""
    MAX_CONCURRENCY: int = 500
    """The highest per-key concurrency an owner may set. The owner's concurrency limit still applies."""
    MAX_STATUS_REASON_CHARACTERS: int = 255
    """The most characters kept from the reason for a status change, which is the width of the ``status_reason``
    column."""
    UNLIMITED: int = -1
    """The value of a numeric limit that does not limit anything."""


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


def normalize_networks(raw_networks: Sequence[str]) -> list[str]:
    """Parse a network allowlist into normalized CIDR text.

    A bare address becomes a single-address network. An IPv4-mapped IPv6 network becomes its IPv4 network, so that
    the allowlist matches IPv4 clients reported by dual-stack sockets.

    Args:
        raw_networks: CIDR ranges or addresses, as the owner sent them.

    Returns:
        The networks in normalized form, without duplicates, in the order sent.

    Raises:
        ValueError: A value is not an address or a network, or there are too many networks.
    """
    if len(raw_networks) > SharedKeyLimits.MAX_ALLOWED_NETWORKS:
        raise ValueError(f"A shared key can have at most {SharedKeyLimits.MAX_ALLOWED_NETWORKS} allowed networks")
    normalized_networks: list[str] = []
    for raw_network in raw_networks:
        try:
            network = ipaddress.ip_network(str(raw_network).strip(), strict=False)
        except ValueError as err:
            raise ValueError(f"'{raw_network}' is not an IP address or a CIDR range") from err
        if isinstance(network, ipaddress.IPv6Network) and network.network_address.ipv4_mapped is not None:
            mapped_prefix = max(network.prefixlen - 96, 0)
            network = ipaddress.ip_network(f"{network.network_address.ipv4_mapped}/{mapped_prefix}", strict=False)
        normalized_text = str(network)
        if normalized_text not in normalized_networks:
            normalized_networks.append(normalized_text)
    return normalized_networks


def _network_contains_subject(network_text: str, ip_subject: str) -> bool:
    network = ipaddress.ip_network(network_text)
    if "/" in ip_subject:
        subject_network = ipaddress.ip_network(ip_subject)
        if subject_network.version != network.version:
            return False
        return subject_network.subnet_of(network)  # type: ignore[arg-type]
    subject_address = ipaddress.ip_address(ip_subject)
    if subject_address.version != network.version:
        return False
    return subject_address in network


def _is_subject_allowed(allowed_networks: Sequence[str], ip_subject: str | None) -> bool:
    """Return whether the request's IP subject is inside one of the allowed networks.

    An IPv6 subject is a /64 network, so it is allowed only when the whole /64 is inside an allowed network. A request
    without an IP subject (a free-text proxy origin) is not allowed by any allowlist.
    """
    if ip_subject is None:
        return False
    return any(_network_contains_subject(network_text, ip_subject) for network_text in allowed_networks)


_CLEARABLE_POLICY_FIELDS: frozenset[str] = frozenset({"allowed_models", "allowed_networks"})
"""The policy fields that an explicit null clears. A null in any other policy field counts as not sent."""

_ModelName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=SharedKeyLimits.MAX_MODEL_NAME_CHARACTERS),
]


class SharedKeyPolicyInput(BaseModel):
    """Represents the policy fields that an owner sent when creating or updating a shared key.

    A field left out of the request is not in ``model_fields_set``, so an update changes only the fields sent. Repeated
    entries are kept once, in the order sent. Model names are kept as typed, because the model reference changes over
    time and a key can be prepared for a model before workers serve it.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", use_attribute_docstrings=True)

    scopes: list[SharedKeyScope] | None = None
    """The request types that the key may submit. At least one."""
    roles: list[SharedKeyRole] | None = None
    """The owner roles granted to the key. The owner must hold each of them."""
    allowed_models: list[_ModelName] | None = None
    """The models that the key may request. An explicit null allows any model."""
    max_concurrency: int | None = None
    """The most generations allowed to wait under the key, or -1 when only the owner's limit applies."""
    allowed_networks: list[str] | None = None
    """The CIDR ranges that the key may be used from, normalized. An explicit null allows any network."""
    status: SharedKeyStatus | None = None
    """The key's new status. The owner may set only ``OWNER_SETTABLE_STATUSES``."""

    @model_validator(mode="before")
    @classmethod
    def _drop_unclearable_nulls(cls, data: Any) -> Any:
        # The other fields of the shared key endpoints are parsed by reqparse, which reads a null as not sent. The
        # policy fields do the same, except the allowlists, where a null is how an owner allows everything again.
        if not isinstance(data, dict):
            return data
        return {name: value for name, value in data.items() if value is not None or name in _CLEARABLE_POLICY_FIELDS}

    @field_validator("scopes")
    @classmethod
    def _check_scopes(cls, scopes: list[SharedKeyScope] | None) -> list[SharedKeyScope] | None:
        if scopes is not None and not scopes:
            raise ValueError("A shared key needs at least one scope")
        return None if scopes is None else list(dict.fromkeys(scopes))

    @field_validator("roles")
    @classmethod
    def _dedupe_roles(cls, roles: list[SharedKeyRole] | None) -> list[SharedKeyRole] | None:
        return None if roles is None else list(dict.fromkeys(roles))

    @field_validator("allowed_models")
    @classmethod
    def _check_models(cls, models: list[str] | None) -> list[str] | None:
        if models is None:
            return None
        if not models:
            raise ValueError("allowed_models cannot be empty. Send null, or leave it out, to allow every model")
        if len(models) > SharedKeyLimits.MAX_ALLOWED_MODELS:
            raise ValueError(f"A shared key can allow at most {SharedKeyLimits.MAX_ALLOWED_MODELS} models")
        return list(dict.fromkeys(models))

    @field_validator("max_concurrency")
    @classmethod
    def _check_concurrency(cls, max_concurrency: int | None) -> int | None:
        if max_concurrency is None or max_concurrency == SharedKeyLimits.UNLIMITED:
            return max_concurrency
        if not 1 <= max_concurrency <= SharedKeyLimits.MAX_CONCURRENCY:
            raise ValueError(f"max_concurrency must be -1 or between 1 and {SharedKeyLimits.MAX_CONCURRENCY}")
        return max_concurrency

    @field_validator("allowed_networks")
    @classmethod
    def _check_networks(cls, networks: list[str] | None) -> list[str] | None:
        if networks is None:
            return None
        if not networks:
            raise ValueError("allowed_networks cannot be empty. Send null, or leave it out, to allow every network")
        return normalize_networks(networks)


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


def check_owner_policy(policy: SharedKeyPolicyInput, *, owner: User, current_status: SharedKeyStatus | None) -> None:
    """Refuse a policy that grants a role the owner does not hold, or sets a status that the owner may not set.

    Args:
        policy: The policy fields sent by the owner.
        owner: The key's owner.
        current_status: The key's status, or None when the key is being created.

    Raises:
        e.Forbidden: The owner tried to grant a role they do not hold, to set a status reserved for moderators, or to
            lift a moderator's suspension.
    """
    if policy.roles is not None:
        missing_roles = set(policy.roles) - owner_grantable_roles(owner)
        if missing_roles:
            missing_text = ", ".join(sorted(missing_roles))
            raise e.Forbidden(
                f"You can only grant a shared key roles that your account holds. Missing: {missing_text}.",
                rc="SharedKeyRoleNotHeld",
            )
    if policy.status is None:
        return
    if policy.status not in OWNER_SETTABLE_STATUSES:
        raise e.Forbidden("Only a moderator can set this status.", rc="SharedKeyStatusForbidden")
    if current_status is SharedKeyStatus.SUSPENDED_BY_MODERATOR and policy.status is not current_status:
        raise e.Forbidden(
            "A moderator suspended this shared key. Please contact the moderators to reactivate it.",
            rc="SharedKeyModeratorSuspension",
        )


@dataclass(frozen=True)
class SharedKeyAccessRequest:
    """Represents the parts of a generation request that a shared key's policy is checked against."""

    gentype: str
    """The ``GenerateTemplate.gentype`` of the request."""
    models: list[str]
    """The models the request will run on, after any style was applied."""
    ip_subject: str | None
    """The request's IP subject (``CounterMeasures.ip_subject``), or None when the origin was not an address."""
    via_style: bool
    """Whether the key was supplied by the request's style rather than by the caller."""


def refuse_sharedkey_request(message: str, refusal: SharedKeyRefusal) -> e.Forbidden:
    """Count a request refused by a shared key's policy, and return the exception to raise.

    Args:
        message: The message for the client.
        refusal: The refusal's return code.

    Returns:
        The exception, with the refusal as its return code.
    """
    sharedkey_refusals.add(1, {"horde.rc": refusal.value})
    return e.Forbidden(message, rc=refusal.value)


def check_sharedkey_access(sharedkey: UserSharedKey, access_request: SharedKeyAccessRequest) -> None:
    """Refuse a request that the key's scopes, network allowlist or model allowlist does not allow.

    The key's status is checked before this, by ``UserSharedKey.is_valid``. The model allowlist is skipped when the
    key was supplied by its style, because the style's owner chose the style's models, as for the per-job size limits.
    The scopes and network allowlist always apply.

    Args:
        sharedkey: The key used for the request.
        access_request: The request's checked parts.

    Raises:
        e.Forbidden: The policy does not allow the request. The return code is a
            [`SharedKeyRefusal`][horde.access_control.SharedKeyRefusal] value.
    """
    required_scope = GENTYPE_SCOPES.get(access_request.gentype)
    if required_scope is None or required_scope not in sharedkey.get_scopes():
        raise refuse_sharedkey_request(
            f"This shared key cannot be used for {access_request.gentype} requests.",
            SharedKeyRefusal.SCOPE_DENIED,
        )
    allowed_networks = sharedkey.get_allowed_networks()
    if allowed_networks is not None and not _is_subject_allowed(allowed_networks, access_request.ip_subject):
        raise refuse_sharedkey_request("This shared key cannot be used from your network.", SharedKeyRefusal.NETWORK_DENIED)
    allowed_models = sharedkey.get_allowed_models()
    if access_request.via_style or allowed_models is None:
        return
    denied_models = [model_name for model_name in access_request.models if model_name not in allowed_models]
    if denied_models:
        raise refuse_sharedkey_request(
            f"This shared key cannot be used with these models: {', '.join(denied_models)}.",
            SharedKeyRefusal.MODEL_DENIED,
        )

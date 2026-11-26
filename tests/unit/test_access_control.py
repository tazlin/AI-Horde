# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Test the rules for what a request made with a shared key may do, without the database.

An owner hands a shared key to people outside the owner's control, such as a class or a community bot. The rules keep
the owner's privileges off the key unless the owner granted them. A key loses a role when its owner loses it, and an
owner cannot grant a role that the owner does not hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from horde import access_control
from horde import exceptions as e
from horde.access_control import (
    RequestPrincipal,
    SharedKeyRole,
    check_owner_policy,
    parse_policy_input,
    record_legacy_sharedkey_request,
)
from horde.patreon import patrons

pytestmark = pytest.mark.unit


@dataclass
class StubOwner:
    """Represents the account fields read by the access rules, without a database row."""

    id: int = 41
    trusted: bool = False
    education: bool = False
    special: bool = False
    moderator: bool = False
    service: bool = False


@dataclass
class StubSharedKey:
    """Represents the shared key fields read by the access rules, without a database row."""

    id: str = "6f1c9d0e-0000-4000-8000-000000000001"
    roles: list[SharedKeyRole] | None = field(default_factory=list)

    def get_roles(self) -> list[SharedKeyRole] | None:
        return self.roles


@pytest.fixture
def no_patrons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every account a non-supporter."""
    monkeypatch.setattr(patrons, "patrons", {})


def _rc_of(exc_info: pytest.ExceptionInfo[e.Forbidden] | pytest.ExceptionInfo[e.BadRequest]) -> str:
    return exc_info.value.rc


def _owner_check(body: dict[str, Any], owner: StubOwner) -> None:
    check_owner_policy(parse_policy_input(body), owner=owner)


class TestEffectiveRoles:
    """A request through a shared key gets only the owner's allowances that the owner granted to the key.

    It does not get the moderator exemptions.
    """

    @pytest.mark.usefixtures("no_patrons")
    def test_a_new_key_of_a_privileged_owner_has_none_of_the_owners_allowances(self) -> None:
        """A new key does not have any role, so handing it out shares only the kudos."""
        owner = StubOwner(trusted=True, education=True, special=True)
        principal = RequestPrincipal(user=owner, sharedkey=StubSharedKey(roles=[]))

        assert (principal.trusted, principal.education, principal.special, principal.is_patron) == (False, False, False, False)

    @pytest.mark.usefixtures("no_patrons")
    def test_a_granted_role_lapses_as_soon_as_the_owner_loses_it(self) -> None:
        """A moderator who removes an owner's trust removes it from every key of the owner at once, without changing each key."""
        owner = StubOwner(trusted=True)
        principal = RequestPrincipal(user=owner, sharedkey=StubSharedKey(roles=[SharedKeyRole.TRUSTED]))
        assert principal.trusted

        owner.trusted = False

        assert not principal.trusted

    @pytest.mark.parametrize("roles", [None, [], list(SharedKeyRole)])
    def test_a_key_never_acts_with_the_moderator_role(self, roles: list[SharedKeyRole] | None) -> None:
        """Moderator exemptions stay with the owner, whatever roles the key was granted."""
        owner = StubOwner(moderator=True, service=True, trusted=True)
        principal = RequestPrincipal(user=owner, sharedkey=StubSharedKey(roles=roles))

        assert not principal.moderator

    def test_a_key_granted_the_service_role_may_proxy_for_the_service_account(self) -> None:
        """A service's backend can hold a separate key and still attribute work to the service's users."""
        owner = StubOwner(service=True)

        assert RequestPrincipal(user=owner, sharedkey=StubSharedKey(roles=[SharedKeyRole.SERVICE])).service

    @pytest.mark.parametrize("roles", [None, [], [SharedKeyRole.TRUSTED, SharedKeyRole.EDUCATION, SharedKeyRole.SPECIAL]])
    def test_a_key_without_the_service_grant_may_not_proxy(self, roles: list[SharedKeyRole] | None) -> None:
        """Without the grant, a holder of the key cannot attribute requests to other people.

        A key without a role list does not proxy either, so a key created before 6.1.0 proxies only after the migration
        or the owner grants the role.
        """
        owner = StubOwner(service=True, trusted=True, education=True, special=True)

        assert not RequestPrincipal(user=owner, sharedkey=StubSharedKey(roles=roles)).service

    def test_the_service_grant_lapses_when_the_owner_stops_being_a_service_account(self) -> None:
        owner = StubOwner(service=True)
        principal = RequestPrincipal(user=owner, sharedkey=StubSharedKey(roles=[SharedKeyRole.SERVICE]))

        owner.service = False

        assert not principal.service

    @pytest.mark.usefixtures("no_patrons")
    def test_a_key_made_before_roles_existed_keeps_working_as_it_did(self) -> None:
        """Existing integrations must not break on deploy, so a key without a role list keeps its owner's allowances."""
        owner = StubOwner(trusted=True, special=True)
        principal = RequestPrincipal(user=owner, sharedkey=StubSharedKey(roles=None))

        assert principal.trusted
        assert principal.special
        assert not principal.moderator

    def test_a_personal_key_acts_with_every_role_of_its_account(self) -> None:
        owner = StubOwner(trusted=True, moderator=True, service=True)
        principal = RequestPrincipal(user=owner)

        assert (principal.trusted, principal.moderator, principal.service) == (True, True, True)

    def test_supporter_allowances_travel_only_with_the_trusted_grant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Supporters get the same allowances as trusted users, so the trusted grant also controls them on a key."""
        owner = StubOwner(trusted=False)
        monkeypatch.setattr(patrons, "patrons", {owner.id: {"entitlement_amount": 5}})

        assert RequestPrincipal(user=owner, sharedkey=StubSharedKey(roles=[SharedKeyRole.TRUSTED])).is_patron
        assert not RequestPrincipal(user=owner, sharedkey=StubSharedKey(roles=[])).is_patron

    def test_the_principal_acts_for_the_owner_so_owner_restrictions_still_apply(self) -> None:
        """An education account's forced SFW is read from the account, so leaving the role out of the grant does not lift it."""
        owner = StubOwner(education=True)
        principal = RequestPrincipal(user=owner, sharedkey=StubSharedKey(roles=[]))

        assert not principal.education
        assert principal.user.education


class _Counter:
    """Record the amounts added to a metric counter."""

    def __init__(self) -> None:
        self.total = 0

    def add(self, amount: int) -> None:
        self.total += amount


class TestLegacyKeyCount:
    """The count shows how many requests still go through keys without a role list."""

    @pytest.fixture
    def legacy_count(self, monkeypatch: pytest.MonkeyPatch) -> _Counter:
        counter = _Counter()
        monkeypatch.setattr(access_control, "sharedkey_legacy_requests", counter)
        return counter

    def test_a_request_through_a_key_without_a_role_list_is_counted_once(self, legacy_count: _Counter) -> None:
        owner = StubOwner(trusted=True, special=True, moderator=True, service=True)

        record_legacy_sharedkey_request(RequestPrincipal(user=owner, sharedkey=StubSharedKey(roles=None)))

        assert legacy_count.total == 1

    @pytest.mark.parametrize("sharedkey", [None, StubSharedKey(roles=[]), StubSharedKey(roles=[SharedKeyRole.TRUSTED])])
    def test_personal_keys_and_keys_with_a_role_list_are_not_counted(self, legacy_count: _Counter, sharedkey: StubSharedKey | None) -> None:
        record_legacy_sharedkey_request(RequestPrincipal(user=StubOwner(trusted=True), sharedkey=sharedkey))

        assert legacy_count.total == 0


class TestOwnerPolicyValidation:
    """The owner can widen a key only up to the roles that the owner holds."""

    @pytest.mark.usefixtures("no_patrons")
    def test_an_owner_cannot_grant_a_role_they_do_not_hold(self) -> None:
        with pytest.raises(e.Forbidden) as refused:
            _owner_check({"roles": ["trusted"]}, StubOwner())

        assert _rc_of(refused) == "SharedKeyRoleNotHeld"

    @pytest.mark.usefixtures("no_patrons")
    def test_only_a_service_account_may_grant_the_service_role(self) -> None:
        with pytest.raises(e.Forbidden) as refused:
            _owner_check({"roles": ["service"]}, StubOwner(trusted=True))
        _owner_check({"roles": ["service"]}, StubOwner(service=True))

        assert _rc_of(refused) == "SharedKeyRoleNotHeld"

    def test_a_supporter_may_grant_the_trusted_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        owner = StubOwner()
        monkeypatch.setattr(patrons, "patrons", {owner.id: {"entitlement_amount": 5}})

        _owner_check({"roles": ["trusted"]}, owner)

    def test_a_null_role_list_counts_as_not_sent(self) -> None:
        """The other shared key fields read null as not sent, so a null cannot remove a key's role list."""
        policy = parse_policy_input({"roles": None})

        assert policy.model_fields_set == set()

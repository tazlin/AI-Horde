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
    ALL_SCOPES,
    RequestPrincipal,
    SharedKeyAccessRequest,
    SharedKeyLimits,
    SharedKeyRefusal,
    SharedKeyRole,
    SharedKeyScope,
    SharedKeyStatus,
    _is_subject_allowed,
    check_owner_policy,
    check_sharedkey_access,
    normalize_networks,
    parse_policy_input,
    record_legacy_sharedkey_request,
)
from horde.countermeasures import CounterMeasures
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
    scopes: list[SharedKeyScope] = field(default_factory=lambda: list(ALL_SCOPES))
    allowed_networks: list[str] | None = None
    allowed_models: list[str] | None = None

    def get_roles(self) -> list[SharedKeyRole] | None:
        return self.roles

    def get_scopes(self) -> list[SharedKeyScope]:
        return self.scopes

    def get_allowed_networks(self) -> list[str] | None:
        return self.allowed_networks

    def get_allowed_models(self) -> list[str] | None:
        return self.allowed_models


@pytest.fixture
def no_patrons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every account a non-supporter."""
    monkeypatch.setattr(patrons, "patrons", {})


def _request(
    *, gentype: str = "image", models: list[str] | None = None, ip_subject: str | None = "198.51.100.7", via_style: bool = False
) -> SharedKeyAccessRequest:
    return SharedKeyAccessRequest(gentype=gentype, models=models or ["stable_diffusion"], ip_subject=ip_subject, via_style=via_style)


def _rc_of(exc_info: pytest.ExceptionInfo[e.Forbidden] | pytest.ExceptionInfo[e.BadRequest]) -> str:
    return exc_info.value.rc


def _owner_check(body: dict[str, Any], owner: StubOwner, current_status: SharedKeyStatus | None = None) -> None:
    check_owner_policy(parse_policy_input(body), owner=owner, current_status=current_status)


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
    """The owner can limit a key freely, and can widen it only up to the roles that the owner holds."""

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

    def test_an_owner_cannot_impersonate_a_moderator(self) -> None:
        """A moderator's suspension is only meaningful when the owner cannot set it or lift it."""
        with pytest.raises(e.Forbidden) as refused:
            _owner_check({"status": "suspended_by_moderator"}, StubOwner(), SharedKeyStatus.ACTIVE)

        assert _rc_of(refused) == "SharedKeyStatusForbidden"

    @pytest.mark.parametrize("status", ["active", "suspended_by_owner"])
    def test_an_owner_cannot_lift_a_moderators_suspension(self, status: str) -> None:
        with pytest.raises(e.Forbidden) as refused:
            _owner_check({"status": status}, StubOwner(), SharedKeyStatus.SUSPENDED_BY_MODERATOR)

        assert _rc_of(refused) == "SharedKeyModeratorSuspension"

    @pytest.mark.parametrize(
        "body",
        [{"scopes": []}, {"allowed_models": []}, {"allowed_networks": []}],
        ids=["no scopes", "no models", "no networks"],
    )
    def test_an_empty_allowlist_is_refused_rather_than_read_as_lock_everything_or_allow_everything(self, body: dict[str, Any]) -> None:
        """Clients read an empty list in opposite ways, so the owner must send null or at least one value."""
        with pytest.raises(e.BadRequest) as refused:
            parse_policy_input(body)

        assert _rc_of(refused) == "InvalidSharedKeyPolicy"

    @pytest.mark.parametrize(
        ("value", "accepted"),
        [
            (SharedKeyLimits.UNLIMITED, True),
            (0, False),
            (1, True),
            (SharedKeyLimits.MAX_CONCURRENCY, True),
            (SharedKeyLimits.MAX_CONCURRENCY + 1, False),
            (-2, False),
        ],
    )
    def test_concurrency_is_unlimited_or_a_positive_bound(self, value: int, accepted: bool) -> None:
        """A limit of zero would make the key unusable. An owner who wants that should suspend the key."""
        if accepted:
            assert parse_policy_input({"max_concurrency": value}).max_concurrency == value
        else:
            with pytest.raises(e.BadRequest):
                parse_policy_input({"max_concurrency": value})

    def test_unknown_scopes_are_refused_with_the_allowed_values(self) -> None:
        with pytest.raises(e.BadRequest) as refused:
            parse_policy_input({"scopes": ["interrogation"]})

        assert "image_generation" in str(refused.value.specific)

    def test_repeated_entries_are_kept_once_in_the_order_sent(self) -> None:
        policy = parse_policy_input(
            {"scopes": ["text_generation", "image_generation", "text_generation"], "allowed_models": ["b", " a ", "b"]}
        )

        assert policy.scopes == [SharedKeyScope.TEXT_GENERATION, SharedKeyScope.IMAGE_GENERATION]
        assert policy.allowed_models == ["b", "a"]

    def test_an_explicit_null_clears_an_allowlist_and_a_missing_field_keeps_it(self) -> None:
        """An update changes only the fields sent. Null is how an owner allows every model or network again."""
        cleared = parse_policy_input({"allowed_models": None, "allowed_networks": None})
        untouched = parse_policy_input({"kudos": 10, "name": "renamed"})

        assert cleared.model_dump(exclude_unset=True) == {"allowed_models": None, "allowed_networks": None}
        assert untouched.model_fields_set == set()

    def test_a_null_in_a_field_that_cannot_be_cleared_counts_as_not_sent(self) -> None:
        """The other shared key fields read null as not sent, so a null cannot remove a key's scopes or role list."""
        policy = parse_policy_input({"scopes": None, "roles": None, "max_concurrency": None, "status": None})

        assert policy.model_fields_set == set()


class TestNetworkAllowlist:
    """An allowlist limits a key to the networks where its owner expects it to be used, such as a school or a server."""

    def test_networks_are_normalized_so_equal_ranges_compare_equal(self) -> None:
        assert normalize_networks(["192.0.2.77/24", "198.51.100.9", "::ffff:203.0.113.0/120", "192.0.2.0/24"]) == [
            "192.0.2.0/24",
            "198.51.100.9/32",
            "203.0.113.0/24",
        ]

    def test_more_networks_than_the_limit_are_refused(self) -> None:
        networks = [f"10.0.{index}.0/24" for index in range(SharedKeyLimits.MAX_ALLOWED_NETWORKS + 1)]

        with pytest.raises(e.BadRequest) as refused:
            parse_policy_input({"allowed_networks": networks})

        assert _rc_of(refused) == "InvalidSharedKeyPolicy"

    def test_text_that_is_not_a_network_is_refused(self) -> None:
        with pytest.raises(e.BadRequest) as refused:
            parse_policy_input({"allowed_networks": ["school.example"]})

        assert "school.example" in refused.value.specific

    @pytest.mark.parametrize(
        ("allowed", "ipaddr", "expected"),
        [
            (["192.0.2.0/24"], "192.0.2.200", True),
            (["192.0.2.0/24"], "192.0.3.1", False),
            (["192.0.2.0/24"], "::ffff:192.0.2.5", True),
            (["2001:db8::/48"], "2001:db8:0:7::1", True),
            # A /64 subject only partly inside an /80 allowlist would let the rest of the /64 in.
            (["2001:db8:0:7::/80"], "2001:db8:0:7::1", False),
            (["192.0.2.0/24"], "2001:db8::1", False),
        ],
    )
    def test_a_request_is_allowed_only_when_its_whole_subject_is_inside_an_allowed_network(
        self,
        allowed: list[str],
        ipaddr: str,
        expected: bool,
    ) -> None:
        assert _is_subject_allowed(allowed, CounterMeasures.ip_subject(ipaddr)) is expected

    def test_a_request_without_an_address_is_never_inside_an_allowlist(self) -> None:
        """A free-text proxy origin cannot be matched to a network, so a key with a network allowlist refuses the request."""
        assert not _is_subject_allowed(["0.0.0.0/0"], None)


class TestRequestChecks:
    """The per-request checks return a code that an integration can act on."""

    @pytest.mark.parametrize(
        ("sharedkey", "access_request", "refusal"),
        [
            (StubSharedKey(scopes=[SharedKeyScope.TEXT_GENERATION]), _request(gentype="image"), SharedKeyRefusal.SCOPE_DENIED),
            (StubSharedKey(allowed_networks=["192.0.2.0/24"]), _request(ip_subject="198.51.100.7"), SharedKeyRefusal.NETWORK_DENIED),
            (StubSharedKey(allowed_models=["model_a"]), _request(models=["model_a", "model_b"]), SharedKeyRefusal.MODEL_DENIED),
        ],
        ids=["scope", "network", "model"],
    )
    def test_each_policy_refuses_with_its_own_return_code(
        self,
        sharedkey: StubSharedKey,
        access_request: SharedKeyAccessRequest,
        refusal: SharedKeyRefusal,
    ) -> None:
        with pytest.raises(e.Forbidden) as refused:
            check_sharedkey_access(sharedkey, access_request)

        assert _rc_of(refused) == refusal.value

    def test_a_model_refusal_names_only_the_models_the_key_does_not_allow(self) -> None:
        with pytest.raises(e.Forbidden) as refused:
            check_sharedkey_access(StubSharedKey(allowed_models=["model_a"]), _request(models=["model_a", "model_b"]))

        assert "model_b" in refused.value.specific
        assert "model_a" not in refused.value.specific

    def test_a_style_chooses_its_own_models_but_not_where_the_key_is_used(self) -> None:
        """The style's author chose the style's models. The owner's network allowlist still applies to the key."""
        pinned_and_limited = StubSharedKey(allowed_models=["model_a"], allowed_networks=["192.0.2.0/24"])

        check_sharedkey_access(pinned_and_limited, _request(models=["model_b"], ip_subject="192.0.2.9", via_style=True))
        with pytest.raises(e.Forbidden) as refused:
            check_sharedkey_access(pinned_and_limited, _request(models=["model_b"], ip_subject="198.51.100.7", via_style=True))
        assert _rc_of(refused) == SharedKeyRefusal.NETWORK_DENIED.value


class TestSuspendedKey:
    """A suspended key is refused before any other check, so its holders see why the key stopped working."""

    @pytest.mark.parametrize("status", [SharedKeyStatus.SUSPENDED_BY_OWNER, SharedKeyStatus.SUSPENDED_BY_MODERATOR])
    def test_a_suspended_key_is_reported_as_suspended_even_when_it_is_also_empty(self, status: SharedKeyStatus) -> None:
        """A suspended key without kudos is refused rather than downgraded to the anonymous account."""
        from horde.classes.base.user import UserSharedKey

        is_valid, _, rc = UserSharedKey(status=status.value, kudos=0).is_valid()

        assert (is_valid, rc) == (False, SharedKeyRefusal.SUSPENDED.value)

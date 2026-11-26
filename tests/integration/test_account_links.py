# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Test the linked-account protections against sockpuppet accounts, from the side of a farmer, a household, a classroom and a moderator.

Two accounts are linked when both were seen on one network in the last 30 days. The tests check that a transfer between
linked accounts goes through and is reported with an event, and that a registration from a suspicious network needs a
higher reCAPTCHA score instead of a moderator. They also check that moderators can see the links without being
notified, and that an address is not stored, shown or reported.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from flask import Flask
from flask.testing import FlaskClient

from tests.fixture_types import ApiUser, MakeApiUser
from tests.integration.test_request_parameters import IMAGE_REQUEST

AGENT: str = "aihorde_ci_client:1.0:(test)ci"
TEST_IP_SUBJECT_SECRET: bytes = b"account link integration secret"
"""The secret for network pseudonyms. The test environment does not set one."""
LINKS_PATH: str = "/api/v2/operations/moderation/account_links"


class _Recorder:
    """Record the calls of one counter or of the structured event function."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def add(self, amount: float, attributes: dict[str, str] | None = None) -> None:
        self.calls.append((amount, attributes or {}))

    def info(self, name: str, **attributes: Any) -> None:
        self.calls.append((name, attributes))


@dataclass
class Telemetry:
    """The structured events, transfer metrics and moderator notices emitted during one test."""

    event_recorder: _Recorder = field(default_factory=_Recorder)
    transfer_recorder: _Recorder = field(default_factory=_Recorder)
    notices: list[str] = field(default_factory=list)

    def events(self, name: str) -> list[dict[str, Any]]:
        """Return the attributes of each event with the name, in order."""
        return [attributes for event_name, attributes in self.event_recorder.calls if event_name == name]

    def transfer_link_statuses(self) -> list[str]:
        """Return the ``horde.link_status`` attribute of each completed transfer, in order."""
        return [attributes["horde.link_status"] for _, attributes in self.transfer_recorder.calls]


@pytest.fixture(autouse=True)
def telemetry(monkeypatch: pytest.MonkeyPatch) -> Iterator[Telemetry]:
    """Disable rate limits, set the pseudonym secret, turn reCAPTCHA off, and record events, metrics and notices."""
    from horde import account_links, countermeasures
    from horde.apis.v2 import base
    from horde.classes.base import user
    from horde.database import functions
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    recorded = Telemetry()
    monkeypatch.setattr(countermeasures, "IP_SUBJECT_KEY_SECRET", TEST_IP_SUBJECT_SECRET)
    monkeypatch.setattr(base, "upload_prompt", lambda *_: None)
    monkeypatch.setattr(base, "send_problem_user_notification", recorded.notices.append)
    monkeypatch.setattr(user, "send_problem_user_notification", recorded.notices.append)
    monkeypatch.setattr(account_links, "logfire", SimpleNamespace(info=recorded.event_recorder.info))
    monkeypatch.setattr(functions, "kudos_transfers_completed", recorded.transfer_recorder)
    monkeypatch.delenv("RECAPTCHA_SECRET_KEY", raising=False)
    monkeypatch.delenv("RECAPTCHA_MIN_SCORE", raising=False)
    monkeypatch.delenv("RECAPTCHA_LINKED_NETWORK_MIN_SCORE", raising=False)
    yield recorded
    limiter.enabled = previous


def _headers(api_key: str) -> dict[str, str]:
    return {"apikey": api_key, "Client-Agent": AGENT}


def _unique_ip() -> str:
    raw = uuid.uuid4().bytes
    return f"10.{raw[0]}.{raw[1]}.{raw[2]}"


def _generate_from(client: FlaskClient, account: ApiUser, ipaddr: str) -> None:
    """Submit and cancel one image request from the address, which records the account on the address's network."""
    response = client.post(
        "/api/v2/generate/async", json=IMAGE_REQUEST, headers=_headers(account.api_key), environ_base={"REMOTE_ADDR": ipaddr}
    )
    assert response.status_code == 202, response.get_data(as_text=True)
    client.delete(f"/api/v2/generate/status/{response.get_json()['id']}", headers=_headers(account.api_key))


def _transfer(
    client: FlaskClient,
    sender: ApiUser,
    receiver: str,
    *,
    ipaddr: str | None = None,
    amount: int = 100,
    idempotency_key: str | None = None,
):
    headers = _headers(sender.api_key)
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return client.post(
        "/api/v2/kudos/transfer",
        json={"username": receiver, "amount": amount},
        headers=headers,
        environ_base={"REMOTE_ADDR": ipaddr or _unique_ip()},
    )


def _subject_key(ipaddr: str) -> str:
    from horde.countermeasures import CounterMeasures

    subject_key = CounterMeasures.ip_subject_key(CounterMeasures.ip_subject(ipaddr))
    assert subject_key is not None
    return subject_key


def _link_rows(app: Flask, user_id: int) -> list[Any]:
    from horde.classes.base.account_link import IpSubjectAccountLink
    from horde.flask import db

    with app.app_context():
        rows = db.session.query(IpSubjectAccountLink).filter_by(user_id=user_id).all()
        for row in rows:
            db.session.expunge(row)
        return rows


def _set_roles(app: Flask, user_id: int, **roles: bool) -> None:
    from horde.classes.base.user import User
    from horde.flask import db

    with app.app_context():
        user = db.session.get(User, user_id)
        for role_name, value in roles.items():
            getattr(user, f"set_{role_name}")(value)
        db.session.commit()
        user.refresh_cache()


@pytest.fixture
def funded(make_api_user: MakeApiUser, settle_kudos: Callable[[], int]) -> Callable[..., ApiUser]:
    """Return a factory of accounts with settled kudos to generate and transfer with."""

    def _make(**roles: bool) -> ApiUser:
        account = make_api_user(kudos=10_000, **roles)
        settle_kudos()
        return account

    return _make


class TestKudosCashOut:
    """Farmed kudos are cashed out by moving them from fresh accounts to one main account on the same network.

    A shared network also holds households, schools and carrier NAT, so the transfer goes through. A moderator can look
    at the reported transfers when they choose to, and nobody is notified.
    """

    def test_a_transfer_between_accounts_seen_on_one_network_goes_through_and_is_reported(
        self, client: FlaskClient, funded, telemetry: Telemetry
    ) -> None:
        from horde.account_links import LINKED_TRANSFER_EVENT

        farm, main = funded(), funded()
        shared_ip = _unique_ip()
        _generate_from(client, farm, shared_ip)
        _generate_from(client, main, shared_ip)

        response = _transfer(client, farm, main.alias, amount=100)

        assert response.status_code == 200, response.get_data(as_text=True)
        [event] = telemetry.events(LINKED_TRANSFER_EVENT)
        assert (event["source_user_id"], event["dest_user_id"]) == (farm.id, main.id)
        assert event["amount"] == 100
        assert event["shared_networks"] == 1
        assert event["policy"] == "kudos_transfer"
        assert shared_ip not in repr(event)
        assert telemetry.transfer_link_statuses() == ["linked"]
        assert telemetry.notices == []

    def test_sending_to_the_main_accounts_shared_key_is_reported_against_the_keys_owner(
        self, client: FlaskClient, funded, telemetry: Telemetry
    ) -> None:
        """A transfer to a shared key credits the key's owner, so the owner is the receiver in the report."""
        from horde.account_links import LINKED_TRANSFER_EVENT

        farm, main = funded(), funded()
        shared_ip = _unique_ip()
        _generate_from(client, farm, shared_ip)
        _generate_from(client, main, shared_ip)
        key = client.put("/api/v2/sharedkeys", json={"kudos": 10}, headers=_headers(main.api_key)).get_json()["id"]

        response = _transfer(client, farm, key)

        assert response.status_code == 200, response.get_data(as_text=True)
        assert [event["dest_user_id"] for event in telemetry.events(LINKED_TRANSFER_EVENT)] == [main.id]

    def test_the_transfer_itself_links_the_sender_to_its_network(self, client: FlaskClient, funded, telemetry: Telemetry) -> None:
        """A farm that sends transfers without generating is reported at the second sending account."""
        from horde.account_links import LINKED_TRANSFER_EVENT

        first_farm, second_farm, main = funded(), funded(), funded()
        farm_ip = _unique_ip()
        assert _transfer(client, first_farm, main.alias, ipaddr=farm_ip).status_code == 200
        _generate_from(client, second_farm, farm_ip)

        response = _transfer(client, second_farm, first_farm.alias, ipaddr=_unique_ip())

        assert response.status_code == 200, response.get_data(as_text=True)
        assert [(event["source_user_id"], event["dest_user_id"]) for event in telemetry.events(LINKED_TRANSFER_EVENT)] == [
            (second_farm.id, first_farm.id)
        ]

    def test_accounts_on_different_networks_are_not_reported(self, client: FlaskClient, funded, telemetry: Telemetry) -> None:
        from horde.account_links import LINKED_TRANSFER_EVENT

        sender, receiver = funded(), funded()
        _generate_from(client, sender, _unique_ip())
        _generate_from(client, receiver, _unique_ip())

        assert _transfer(client, sender, receiver.alias).status_code == 200
        assert telemetry.events(LINKED_TRANSFER_EVENT) == []
        assert telemetry.transfer_link_statuses() == ["unlinked"]

    def test_a_trusted_sender_and_an_education_receiver_are_not_reported(
        self, app: Flask, client: FlaskClient, funded, telemetry: Telemetry
    ) -> None:
        """Trusted users have a track record, and classrooms share one network and pool kudos on purpose."""
        from horde.account_links import LINKED_TRANSFER_EVENT

        trusted, ordinary, classroom = funded(trusted=True), funded(), funded()
        _set_roles(app, classroom.id, education=True)
        shared_ip = _unique_ip()
        for account in (trusted, ordinary, classroom):
            _generate_from(client, account, shared_ip)

        from_trusted = _transfer(client, trusted, ordinary.alias)
        to_classroom = _transfer(client, ordinary, classroom.alias)

        assert from_trusted.status_code == 200, from_trusted.get_data(as_text=True)
        assert to_classroom.status_code == 200, to_classroom.get_data(as_text=True)
        assert telemetry.events(LINKED_TRANSFER_EVENT) == []
        assert telemetry.transfer_link_statuses() == ["exempt", "exempt"]

    def test_a_retried_transfer_is_reported_once(self, client: FlaskClient, funded, telemetry: Telemetry) -> None:
        """A client that retries with the same Idempotency-Key moves the kudos once, so a reviewer sees one transfer."""
        from horde.account_links import LINKED_TRANSFER_EVENT

        farm, main = funded(), funded()
        shared_ip = _unique_ip()
        _generate_from(client, farm, shared_ip)
        _generate_from(client, main, shared_ip)
        retry_key = str(uuid.uuid4())

        first = _transfer(client, farm, main.alias, idempotency_key=retry_key)
        retry = _transfer(client, farm, main.alias, idempotency_key=retry_key)

        assert first.status_code == 200, first.get_data(as_text=True)
        assert retry.status_code == 200, retry.get_data(as_text=True)
        assert len(telemetry.events(LINKED_TRANSFER_EVENT)) == 1
        assert telemetry.transfer_link_statuses() == ["linked"]

    def test_a_refused_transfer_is_not_counted_as_completed(self, client: FlaskClient, funded, telemetry: Telemetry) -> None:
        """The completed-transfer volume must not include transfers that did not move any kudos."""
        sender, receiver = funded(), funded()

        response = _transfer(client, sender, receiver.alias, amount=10_000_000)

        assert response.status_code == 400, response.get_data(as_text=True)
        assert telemetry.transfer_link_statuses() == []

    def test_links_older_than_the_retention_window_no_longer_count(
        self, app: Flask, client: FlaskClient, funded, telemetry: Telemetry
    ) -> None:
        """A student who lived in a dorm a year ago is not tied to everyone who lived there since."""
        from horde.account_links import purge_expired_account_links
        from horde.classes.base.account_link import IpSubjectAccountLink
        from horde.flask import db

        sender, receiver = funded(), funded()
        old_ip = _unique_ip()
        _generate_from(client, sender, old_ip)
        _generate_from(client, receiver, old_ip)
        with app.app_context():
            db.session.query(IpSubjectAccountLink).filter(IpSubjectAccountLink.user_id.in_([sender.id, receiver.id])).update(
                {"last_seen": datetime.utcnow() - timedelta(days=31)},
                synchronize_session=False,
            )
            db.session.commit()

        before_purge = _transfer(client, sender, receiver.alias)
        with app.app_context():
            purged = purge_expired_account_links()
            db.session.commit()

        assert before_purge.status_code == 200, before_purge.get_data(as_text=True)
        assert telemetry.transfer_link_statuses() == ["unlinked"]
        assert purged >= 2
        assert _subject_key(old_ip) not in {row.subject_key for row in _link_rows(app, sender.id)}


class TestModeratorListing:
    """Moderators can see which accounts share networks. Networks are shown by pseudonym and not by address."""

    def test_an_accounts_listing_shows_the_other_accounts_on_its_networks_and_no_address(
        self,
        client: FlaskClient,
        funded,
        make_api_user: MakeApiUser,
    ) -> None:
        moderator = make_api_user(moderator=True)
        suspect, puppet, bystander = funded(), funded(), funded()
        shared_ip, lonely_ip = _unique_ip(), _unique_ip()
        _generate_from(client, suspect, lonely_ip)
        _generate_from(client, suspect, shared_ip)
        _generate_from(client, puppet, shared_ip)
        _generate_from(client, bystander, _unique_ip())

        response = client.get(LINKS_PATH, query_string={"user_id": suspect.id}, headers=_headers(moderator.api_key))

        assert response.status_code == 200, response.get_data(as_text=True)
        assert response.headers["Cache-Control"] == "private, no-store"
        networks = response.get_json()["networks"]
        assert [network["subject_key"] for network in networks] == [_subject_key(shared_ip), _subject_key(lonely_ip)]
        assert [account["user_id"] for account in networks[0]["other_accounts"]] == [puppet.id]
        assert networks[1]["other_accounts"] == []
        text = response.get_data(as_text=True)
        assert shared_ip not in text
        assert lonely_ip not in text

    def test_a_network_listing_shows_every_account_on_it(self, client: FlaskClient, funded, make_api_user: MakeApiUser) -> None:
        """A moderator with a pseudonym from prompt evidence can list the other accounts used from that network."""
        moderator = make_api_user(moderator=True)
        first, second = funded(), funded()
        shared_ip = _unique_ip()
        _generate_from(client, first, shared_ip)
        _generate_from(client, second, shared_ip)

        response = client.get(LINKS_PATH, query_string={"ip_subject_key": _subject_key(shared_ip)}, headers=_headers(moderator.api_key))

        assert response.status_code == 200, response.get_data(as_text=True)
        assert {account["user_id"] for account in response.get_json()["accounts"]} == {first.id, second.id}

    @pytest.mark.parametrize("query", [{}, {"user_id": 1, "ip_subject_key": "a" * 64}], ids=["neither", "both"])
    def test_the_listing_takes_exactly_one_filter(self, client: FlaskClient, make_api_user: MakeApiUser, query: dict[str, Any]) -> None:
        moderator = make_api_user(moderator=True)

        response = client.get(LINKS_PATH, query_string=query, headers=_headers(moderator.api_key))

        assert response.status_code == 400, response.get_data(as_text=True)
        assert response.get_json()["rc"] == "InvalidAccountLinkFilter"

    def test_only_moderators_can_list_links(self, client: FlaskClient, make_api_user: MakeApiUser) -> None:
        ordinary = make_api_user()

        listing = client.get(LINKS_PATH, query_string={"user_id": ordinary.id}, headers=_headers(ordinary.api_key))

        assert listing.get_json()["rc"] == "NotModerator"


class TestWhatIsRecorded:
    """Link an account to a network only from personal-key requests that are evidence about the person.

    Requests made with a shared key or by a service account do not link the account.
    """

    def test_a_shared_key_holders_network_is_not_linked_to_the_keys_owner(self, app: Flask, client: FlaskClient, funded) -> None:
        """An owner who hands a key to a class must not be linked to every pupil's home."""
        owner = funded()
        key = client.put("/api/v2/sharedkeys", json={"kudos": 1000}, headers=_headers(owner.api_key)).get_json()["id"]
        response = client.post(
            "/api/v2/generate/async", json=IMAGE_REQUEST, headers=_headers(key), environ_base={"REMOTE_ADDR": _unique_ip()}
        )
        client.delete(f"/api/v2/generate/status/{response.get_json()['id']}", headers=_headers(owner.api_key))

        assert response.status_code == 202
        assert _link_rows(app, owner.id) == []

    def test_a_service_account_is_not_linked_to_its_servers(self, app: Flask, client: FlaskClient, funded) -> None:
        """A service submits requests for many people from its servers, so the network does not show who owns the account."""
        service = funded()
        _set_roles(app, service.id, service=True)

        _generate_from(client, service, _unique_ip())

        assert _link_rows(app, service.id) == []

    def test_repeated_generations_update_one_row_at_most_once_an_hour(self, app: Flask, client: FlaskClient, funded) -> None:
        """Busy accounts must not turn every generation into a database write."""
        account = funded()
        ipaddr = _unique_ip()

        for _ in range(3):
            _generate_from(client, account, ipaddr)

        [row] = _link_rows(app, account.id)
        assert row.subject_key == _subject_key(ipaddr)
        assert row.last_seen == row.first_seen

    def test_without_redis_generations_are_not_recorded_and_transfers_are(
        self, app: Flask, client: FlaskClient, funded: Callable[..., ApiUser], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without the hourly throttle every generation would write to the database, so generations skip the record."""
        from horde import account_links

        monkeypatch.setattr(account_links, "hr", SimpleNamespace(horde_r=None))
        sender, receiver = funded(), funded()
        ipaddr = _unique_ip()

        _generate_from(client, sender, ipaddr)
        generation_rows = _link_rows(app, sender.id)
        transfer = _transfer(client, sender, receiver.alias, ipaddr=ipaddr)

        assert generation_rows == []
        assert transfer.status_code == 200, transfer.get_data(as_text=True)
        assert [row.subject_key for row in _link_rows(app, sender.id)] == [_subject_key(ipaddr)]

    def test_without_a_deployment_secret_nothing_is_recorded_or_reported(
        self,
        app: Flask,
        client: FlaskClient,
        funded,
        monkeypatch: pytest.MonkeyPatch,
        telemetry: Telemetry,
    ) -> None:
        from horde import countermeasures

        monkeypatch.setattr(countermeasures, "IP_SUBJECT_KEY_SECRET", None)
        sender, receiver = funded(), funded()
        shared_ip = _unique_ip()
        _generate_from(client, sender, shared_ip)
        _generate_from(client, receiver, shared_ip)

        assert _link_rows(app, sender.id) == []
        assert _transfer(client, sender, receiver.alias).status_code == 200
        assert telemetry.transfer_link_statuses() == ["unlinked"]


class _SiteVerify:
    """Stand in for Google's verification endpoint and record the data sent to it."""

    def __init__(self, *, success: bool = True, score: float | None = 0.9, action: str = "submit", error: Exception | None = None) -> None:
        self.result: dict[str, Any] = {"success": success, "action": action}
        if score is not None:
            self.result["score"] = score
        self.error = error
        self.sent: list[dict[str, str]] = []

    def __call__(self, url: str, data: dict[str, str], timeout: float) -> SimpleNamespace:
        self.sent.append(data)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: self.result)


class TestRegistration:
    """Free pseudonymous accounts are what a farm is built from, and the usual way to come back after a ban.

    The same network signals also come from schools, offices and carrier NAT. A registrant from such a network needs a
    higher reCAPTCHA score or an OAuth login, and a moderator does not have to act.
    """

    @staticmethod
    def _register(client: FlaskClient, ipaddr: str, *, username: str | None = None, token: str | None = "token"):
        form = {"username": username or f"newcomer{uuid.uuid4().hex[:6]}"}
        if token is not None:
            form["g-recaptcha-response"] = token
        return client.post("/register", data=form, environ_base={"REMOTE_ADDR": ipaddr})

    @staticmethod
    def _accounts_registered_from(app: Flask, ipaddr: str) -> list[int]:
        """Return the pseudonymous accounts seen on the address's network. The other accounts in these tests use OAuth IDs."""
        from horde.classes.base.account_link import IpSubjectAccountLink
        from horde.classes.base.user import User
        from horde.flask import db

        with app.app_context():
            users = (
                db.session.query(User)
                .join(IpSubjectAccountLink, IpSubjectAccountLink.user_id == User.id)
                .filter(IpSubjectAccountLink.subject_key == _subject_key(ipaddr))
                .all()
            )
            return sorted(user.id for user in users if user.is_pseudonymous())

    @staticmethod
    def _suspicions(app: Flask, user_id: int) -> list[int]:
        from horde.classes.base.user import User
        from horde.flask import db

        with app.app_context():
            return [suspicion.suspicion_id for suspicion in db.session.get(User, user_id).suspicions]

    @staticmethod
    def _enable_recaptcha(monkeypatch: pytest.MonkeyPatch, site_verify: _SiteVerify) -> None:
        from horde import recaptcha

        monkeypatch.setenv("RECAPTCHA_SECRET_KEY", "test secret")
        monkeypatch.setattr(recaptcha.requests, "post", site_verify)

    @staticmethod
    def _banned_network(app: Flask, client: FlaskClient, funded) -> tuple[str, int]:
        """Return a network that a flagged account generated from, and the account's ID."""
        banned = funded()
        ban_ip = _unique_ip()
        _generate_from(client, banned, ban_ip)
        _set_roles(app, banned.id, flagged=True)
        return ban_ip, banned.id

    def test_without_recaptcha_a_busy_network_keeps_registering_and_the_extra_accounts_are_reported(
        self, app: Flask, client: FlaskClient, telemetry: Telemetry
    ) -> None:
        """Without reCAPTCHA there is no check that a person can pass, so nobody is turned away."""
        from horde.account_links import REGISTRATION_EVENT

        farm_ip = _unique_ip()

        responses = [self._register(client, farm_ip) for _ in range(4)]

        assert all(response.status_code == 200 for response in responses)
        registered = self._accounts_registered_from(app, farm_ip)
        assert len(registered) == 4
        [event] = telemetry.events(REGISTRATION_EVENT)
        assert event["signals"] == ["registration_limit"]
        assert event["outcome"] == "advisory"
        assert event["recent_pseudonymous_registrations"] == 3
        assert event["user_id"] == max(registered)
        assert farm_ip not in repr(event)

    def test_without_recaptcha_a_registration_from_a_flagged_accounts_network_is_marked_and_nobody_is_notified(
        self, app: Flask, client: FlaskClient, funded, telemetry: Telemetry
    ) -> None:
        from horde.account_links import REGISTRATION_EVENT
        from horde.suspicions import Suspicions

        ban_ip, banned_id = self._banned_network(app, client, funded)

        response = self._register(client, ban_ip)

        assert response.status_code == 200
        [newcomer_id] = self._accounts_registered_from(app, ban_ip)
        assert Suspicions.LINKED_TO_RESTRICTED_ACCOUNT.value in self._suspicions(app, newcomer_id)
        [event] = telemetry.events(REGISTRATION_EVENT)
        assert (event["outcome"], event["linked_restricted_user_ids"], event["user_id"]) == ("advisory", [banned_id], newcomer_id)
        assert telemetry.notices == []

    def test_a_person_on_a_flagged_accounts_network_registers_with_a_high_recaptcha_score(
        self, app: Flask, client: FlaskClient, funded, telemetry: Telemetry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A student whose school network was used by a banned account gets in without a moderator."""
        from horde.account_links import REGISTRATION_EVENT

        ban_ip, _ = self._banned_network(app, client, funded)
        self._enable_recaptcha(monkeypatch, _SiteVerify(score=0.9))

        response = self._register(client, ban_ip)

        assert response.status_code == 200
        assert len(self._accounts_registered_from(app, ban_ip)) == 1
        [event] = telemetry.events(REGISTRATION_EVENT)
        assert (event["outcome"], event["recaptcha_score"], event["required_score"]) == ("verified", 0.9, 0.7)

    def test_a_middling_score_registers_on_a_clean_network_but_must_log_in_on_a_flagged_one(
        self, app: Flask, client: FlaskClient, funded, telemetry: Telemetry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The higher score applies only where a network has review signals, and OAuth stays open as the way in.

        The refused registrant sees the form again with the chosen name, the reason and the sign-in option.
        """
        from horde.account_links import REGISTRATION_EVENT, REGISTRATION_REFUSED_MESSAGE

        ban_ip, _ = self._banned_network(app, client, funded)
        clean_ip = _unique_ip()
        self._enable_recaptcha(monkeypatch, _SiteVerify(score=0.5))

        on_clean_network = self._register(client, clean_ip)
        on_flagged_network = self._register(client, ban_ip, username="flaggednetworkname")

        assert len(self._accounts_registered_from(app, clean_ip)) == 1
        refused_page = on_flagged_network.get_data(as_text=True)
        assert on_flagged_network.status_code == 403
        assert REGISTRATION_REFUSED_MESSAGE in refused_page
        assert "sign in with one of the providers above" in refused_page
        assert 'value="flaggednetworkname"' in refused_page
        assert self._accounts_registered_from(app, ban_ip) == []
        [event] = telemetry.events(REGISTRATION_EVENT)
        assert (event["outcome"], event["user_id"]) == ("refused", None)
        assert on_clean_network.status_code == 200

    def test_an_operator_can_lower_the_score_for_flagged_networks(
        self, app: Flask, client: FlaskClient, funded, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator who sees too many people turned away can lower the threshold without a release."""
        ban_ip, _ = self._banned_network(app, client, funded)
        self._enable_recaptcha(monkeypatch, _SiteVerify(score=0.5))
        monkeypatch.setenv("RECAPTCHA_LINKED_NETWORK_MIN_SCORE", "0.5")

        self._register(client, ban_ip)

        assert len(self._accounts_registered_from(app, ban_ip)) == 1

    @pytest.mark.parametrize(
        ("site_verify", "outcome", "status"),
        [
            pytest.param(_SiteVerify(score=0.3), "low_score", 400, id="score-below-the-minimum"),
            pytest.param(_SiteVerify(action="login"), "wrong_action", 400, id="token-for-another-action"),
            pytest.param(_SiteVerify(success=False), "failed", 400, id="invalid-token"),
            pytest.param(_SiteVerify(error=requests.ConnectionError("down")), "unavailable", 503, id="google-unreachable"),
        ],
    )
    def test_a_token_that_does_not_pass_registers_nothing_and_shows_why(
        self,
        app: Flask,
        client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
        site_verify: _SiteVerify,
        outcome: str,
        status: int,
    ) -> None:
        """The check fails safe, so no account is made. The page tells the person what to do next, and keeps the name.

        An outage at Google returns 503, so a monitor can tell it apart from a refused person.
        """
        from horde.recaptcha import RecaptchaOutcome, refusal_message

        clean_ip = _unique_ip()
        self._enable_recaptcha(monkeypatch, site_verify)

        response = self._register(client, clean_ip, username="retryname")

        page = response.get_data(as_text=True)
        assert response.status_code == status
        assert refusal_message(RecaptchaOutcome(outcome)) in page
        assert 'value="retryname"' in page
        assert self._accounts_registered_from(app, clean_ip) == []

    def test_a_form_sent_without_a_token_asks_the_person_to_allow_scripts(
        self, app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A browser that blocks the reCAPTCHA script sends the form without a token. Google is not called for it."""
        from horde.recaptcha import RecaptchaOutcome, refusal_message

        site_verify = _SiteVerify()
        self._enable_recaptcha(monkeypatch, site_verify)
        clean_ip = _unique_ip()

        response = self._register(client, clean_ip, token=None)

        assert response.status_code == 400
        assert refusal_message(RecaptchaOutcome.MISSING_TOKEN) in response.get_data(as_text=True)
        assert site_verify.sent == []
        assert self._accounts_registered_from(app, clean_ip) == []

    def test_the_registrants_address_is_not_sent_to_google(self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
        site_verify = _SiteVerify()
        self._enable_recaptcha(monkeypatch, site_verify)

        self._register(client, _unique_ip())

        assert [set(sent) for sent in site_verify.sent] == [{"response", "secret"}]

    def test_an_oauth_registration_on_a_flagged_accounts_network_needs_no_score_and_is_marked(
        self,
        app: Flask,
        client: FlaskClient,
        funded,
        telemetry: Telemetry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An OAuth login ties the account to an outside identity, so it is the way in for a registrant below the score."""
        from horde import routes
        from horde.account_links import REGISTRATION_EVENT
        from horde.classes.base.user import User
        from horde.flask import db
        from horde.suspicions import Suspicions

        ban_ip, banned_id = self._banned_network(app, client, funded)
        self._enable_recaptcha(monkeypatch, _SiteVerify(score=0.5))
        oauth_id = f"g_{uuid.uuid4().hex[:12]}"
        monkeypatch.setattr(routes, "get_oauth_id", lambda: oauth_id)

        response = self._register(client, ban_ip)

        assert response.status_code == 200
        with app.app_context():
            newcomer_id = db.session.query(User).filter_by(oauth_id=oauth_id).one().id
        assert Suspicions.LINKED_TO_RESTRICTED_ACCOUNT.value in self._suspicions(app, newcomer_id)
        [event] = telemetry.events(REGISTRATION_EVENT)
        assert (event["policy"], event["outcome"], event["linked_restricted_user_ids"]) == ("oauth_registration", "flagged", [banned_id])
        assert ban_ip not in repr(event)
        assert telemetry.notices == []


WORKER_BRIDGE_AGENT: str = "AI Horde Worker reGen:9.0.1-citests:https://github.com/Haidra-Org/horde-worker-reGen"
"""The bridge agent of the worker in the generation tests."""
WORKER_MODELS: list[str] = IMAGE_REQUEST["models"]
"""The models of the worker and of its requests. Worker owners in these tests are customizers, so the model reference
does not need to list them."""


SURCHARGED_STYLE: dict[str, Any] = {
    "name": "linked surcharge test style",
    "info": "a style used by other accounts, which pay its author a surcharge",
    "public": True,
    "prompt": "{p}, as a test###{np}",
    "nsfw": False,
    "params": {"width": 512, "height": 512, "steps": 8, "cfg_scale": 1.5, "sampler_name": "k_euler_a"},
    "models": IMAGE_REQUEST["models"],
}
"""A public image style. A request by another account with this style pays the author a surcharge.

Style names are unique on the horde, so each test adds a suffix to the name."""


class TestStyleSurcharge:
    """A style surcharge moves kudos from the requester to the style's author, so a farm can cash out through a style.

    The surcharge is reported in the same way as a transfer between linked accounts. The request is not affected.
    """

    @staticmethod
    def _create_style(client: FlaskClient, author: ApiUser) -> str:
        style = {**SURCHARGED_STYLE, "name": f"{SURCHARGED_STYLE['name']} {uuid.uuid4().hex[:8]}"}
        response = client.post("/api/v2/styles/image", json=style, headers=_headers(author.api_key))
        assert response.status_code == 200, response.get_data(as_text=True)
        return response.get_json()["id"]

    @staticmethod
    def _styled_request(client: FlaskClient, requester: ApiUser, style_id: str, ipaddr: str) -> None:
        response = client.post(
            "/api/v2/generate/async",
            json={**IMAGE_REQUEST, "style": style_id},
            headers=_headers(requester.api_key),
            environ_base={"REMOTE_ADDR": ipaddr},
        )
        assert response.status_code == 202, response.get_data(as_text=True)
        client.delete(f"/api/v2/generate/status/{response.get_json()['id']}", headers=_headers(requester.api_key))

    def test_a_surcharge_paid_to_an_author_on_the_requesters_network_is_reported(
        self, client: FlaskClient, funded, telemetry: Telemetry
    ) -> None:
        from horde.account_links import LINKED_TRANSFER_EVENT

        author, farm = funded(customizer=True), funded()
        shared_ip = _unique_ip()
        _generate_from(client, author, shared_ip)
        style_id = self._create_style(client, author)

        self._styled_request(client, farm, style_id, shared_ip)

        [event] = telemetry.events(LINKED_TRANSFER_EVENT)
        assert event["policy"] == "style_surcharge"
        assert (event["source_user_id"], event["dest_user_id"]) == (farm.id, author.id)
        assert shared_ip not in repr(event)

    def test_a_surcharge_paid_to_an_author_on_another_network_is_not_reported(
        self, client: FlaskClient, funded, telemetry: Telemetry
    ) -> None:
        from horde.account_links import LINKED_TRANSFER_EVENT

        author, requester = funded(customizer=True), funded()
        _generate_from(client, author, _unique_ip())
        style_id = self._create_style(client, author)

        self._styled_request(client, requester, style_id, _unique_ip())

        assert telemetry.events(LINKED_TRANSFER_EVENT) == []

    def test_an_author_using_their_own_style_pays_no_surcharge_and_is_not_reported(
        self, client: FlaskClient, funded, telemetry: Telemetry
    ) -> None:
        from horde.account_links import LINKED_TRANSFER_EVENT

        author = funded(customizer=True)
        author_ip = _unique_ip()
        _generate_from(client, author, author_ip)
        style_id = self._create_style(client, author)

        self._styled_request(client, author, style_id, author_ip)

        assert telemetry.events(LINKED_TRANSFER_EVENT) == []


class TestSharedNetworkGeneration:
    """A worker that fulfils requests sent from its own network can earn kudos from accounts that its owner controls.

    Such generations are reported for review, and the generation and its kudos are not affected.
    """

    @staticmethod
    def _fulfil(client: FlaskClient, requester: ApiUser, request_ip: str, worker_owner: ApiUser, worker_ip: str) -> float:
        """Send one image request, fulfil it with a new worker of ``worker_owner`` and return the worker's reward."""
        request_body = {**IMAGE_REQUEST, "params": {**IMAGE_REQUEST["params"], "n": 1}, "models": WORKER_MODELS, "trusted_workers": False}
        request = client.post(
            "/api/v2/generate/async", json=request_body, headers=_headers(requester.api_key), environ_base={"REMOTE_ADDR": request_ip}
        )
        assert request.status_code == 202, request.get_data(as_text=True)
        request_id = request.get_json()["id"]
        pop_body = {
            "name": f"shared network worker {uuid.uuid4().hex[:8]}",
            "models": WORKER_MODELS,
            "bridge_agent": WORKER_BRIDGE_AGENT,
            "nsfw": True,
            "max_pixels": 4194304,
            "amount": 1,
        }
        try:
            pop = client.post(
                "/api/v2/generate/pop", json=pop_body, headers=_headers(worker_owner.api_key), environ_base={"REMOTE_ADDR": worker_ip}
            )
            assert pop.status_code == 200, pop.get_data(as_text=True)
            job_id = pop.get_json()["id"]
            assert job_id is not None, pop.get_json()
            submit = client.post(
                "/api/v2/generate/submit",
                json={"id": job_id, "generation": "R2", "state": "ok", "seed": 0},
                headers=_headers(worker_owner.api_key),
                environ_base={"REMOTE_ADDR": worker_ip},
            )
            assert submit.status_code == 200, submit.get_data(as_text=True)
            return float(submit.get_json()["reward"])
        finally:
            client.delete(f"/api/v2/generate/status/{request_id}", headers=_headers(requester.api_key))

    def test_a_generation_requested_from_the_workers_network_is_reported_with_its_kudos(
        self, client: FlaskClient, funded, telemetry: Telemetry
    ) -> None:
        from horde.account_links import SHARED_NETWORK_GENERATION_EVENT

        requester, worker_owner = funded(), funded(customizer=True)
        shared_ip = _unique_ip()

        reward = self._fulfil(client, requester, shared_ip, worker_owner, shared_ip)

        [event] = telemetry.events(SHARED_NETWORK_GENERATION_EVENT)
        assert (event["requester_id"], event["worker_owner_id"], event["requester_kind"]) == (requester.id, worker_owner.id, "registered")
        assert event["kudos"] == pytest.approx(reward)
        assert shared_ip not in repr(event)

    def test_a_generation_requested_from_another_network_is_not_reported(self, client: FlaskClient, funded, telemetry: Telemetry) -> None:
        from horde.account_links import SHARED_NETWORK_GENERATION_EVENT

        requester, worker_owner = funded(), funded(customizer=True)

        self._fulfil(client, requester, _unique_ip(), worker_owner, _unique_ip())

        assert telemetry.events(SHARED_NETWORK_GENERATION_EVENT) == []

    def test_an_owner_generating_with_their_own_worker_is_not_reported(self, client: FlaskClient, funded, telemetry: Telemetry) -> None:
        """An owner who tests their own worker does not earn kudos from another account, so there is nothing to review."""
        from horde.account_links import SHARED_NETWORK_GENERATION_EVENT

        owner = funded(customizer=True)
        shared_ip = _unique_ip()

        self._fulfil(client, owner, shared_ip, owner, shared_ip)

        assert telemetry.events(SHARED_NETWORK_GENERATION_EVENT) == []

# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Test shared key roles, limits and suspensions through the API, from the side of owners, holders and moderators.

Each test performs an action of an owner, a key holder or a moderator, and checks the result seen by the others. The
generation requests run against an empty queue, so a 202 response only means that the key's policy allowed the request.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient

from tests.fixture_types import ApiUser, MakeApiUser
from tests.integration._fake_chat_worker import CHAT_MESSAGES, CHAT_MODEL, FakeChatWorker, cancel_request, take_worker_offline
from tests.integration.test_request_parameters import IMAGE_REQUEST, TEXT_REQUEST

AGENT: str = "aihorde_ci_client:1.0:(test)ci"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Disable rate limits and outside notices, and use a fake Redis for the address suspicion cache."""
    import fakeredis

    from horde import countermeasures
    from horde.apis.v2 import base
    from horde.classes.base import user
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    monkeypatch.setattr(countermeasures, "ip_s_r", fakeredis.FakeStrictRedis())
    monkeypatch.setattr(base, "upload_prompt", lambda *_: None)
    monkeypatch.setattr(base, "send_problem_user_notification", lambda *_: None)
    monkeypatch.setattr(user, "send_problem_user_notification", lambda *_: None)
    yield
    limiter.enabled = previous


def _headers(api_key: str) -> dict[str, str]:
    return {"apikey": api_key, "Client-Agent": AGENT}


def _unique_ip() -> str:
    raw = uuid.uuid4().bytes
    return f"10.{raw[0]}.{raw[1]}.{raw[2]}"


def _create_key(client: FlaskClient, owner: ApiUser, **policy: Any) -> str:
    body = {"kudos": 5000, "name": f"key {uuid.uuid4().hex[:6]}", **policy}
    response = client.put("/api/v2/sharedkeys", json=body, headers=_headers(owner.api_key))
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["id"]


def _patch_key(client: FlaskClient, owner: ApiUser, key_id: str, **changes: Any):
    return client.patch(f"/api/v2/sharedkeys/{key_id}", json=changes, headers=_headers(owner.api_key))


FILTERED: dict[str, Any] = {"replacement_filter": False}
"""Request overrides that turn off the replacement filter.

With these overrides, a prompt marked as suspicious by the patched checker is refused and not rewritten.
"""


def _submit(
    client: FlaskClient,
    api_key: str,
    *,
    ipaddr: str | None = None,
    body: dict[str, Any] | None = None,
    text: bool = False,
    **overrides: Any,
):
    request_body = copy.deepcopy(body or (TEXT_REQUEST if text else IMAGE_REQUEST))
    request_body.update(overrides)
    path = "/api/v2/generate/text/async" if text else "/api/v2/generate/async"
    return client.post(path, json=request_body, headers=_headers(api_key), environ_base={"REMOTE_ADDR": ipaddr or _unique_ip()})


def _cancel(client: FlaskClient, api_key: str, response, *, text: bool = False) -> None:
    if response.status_code != 202:
        return
    path = "/api/v2/generate/text/status" if text else "/api/v2/generate/status"
    client.delete(f"{path}/{response.get_json()['id']}", headers=_headers(api_key))


def _assert_refused(response, status: int, rc: str) -> None:
    assert response.status_code == status, response.get_data(as_text=True)
    assert response.get_json()["rc"] == rc, response.get_json()


def _set_roles(app: Flask, user_id: int, **roles: bool) -> None:
    from horde.classes.base.user import User
    from horde.flask import db

    with app.app_context():
        user = db.session.get(User, user_id)
        for role_name, value in roles.items():
            getattr(user, f"set_{role_name}")(value)
        db.session.commit()
        user.refresh_cache()


class _EventRecorder:
    """Record the structured events emitted by the shared key module."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, name: str, **attributes: Any) -> None:
        self.events.append((name, attributes))

    def named(self, name: str) -> list[dict[str, Any]]:
        return [attributes for event_name, attributes in self.events if event_name == name]


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> _EventRecorder:
    """Record the structured events of the shared key module in place of sending them to logfire."""
    from horde.database import sharedkeys

    recorder = _EventRecorder()
    monkeypatch.setattr(sharedkeys, "logfire", recorder)
    return recorder


@pytest.fixture
def funded(make_api_user: MakeApiUser, settle_kudos: Callable[[], int]) -> Callable[..., ApiUser]:
    """Return a factory of accounts with settled kudos, so only the key's policy can refuse a request."""

    def _make(**roles: bool) -> ApiUser:
        account = make_api_user(kudos=100_000, **roles)
        settle_kudos()
        return account

    return _make


BATCHED_REQUEST: dict[str, Any] = {**IMAGE_REQUEST, "disable_batching": True}
"""An image request with two images and batching disabled, which only trusted accounts and supporters may send."""


class TestOwnerAllowancesStayWithTheOwner:
    """A trusted or moderator owner who shares a key shares the kudos. The owner's standing stays with the owner."""

    def test_a_trusted_owners_new_key_cannot_use_trusted_allowances_until_granted(self, client: FlaskClient, funded) -> None:
        owner = funded(trusted=True)
        key_id = _create_key(client, owner)

        as_owner = _submit(client, owner.api_key, body=BATCHED_REQUEST)
        as_key = _submit(client, key_id, body=BATCHED_REQUEST)
        _cancel(client, owner.api_key, as_owner)

        assert as_owner.status_code == 202, as_owner.get_data(as_text=True)
        _assert_refused(as_key, 400, "RequiresTrust")

        assert _patch_key(client, owner, key_id, roles=["trusted"]).status_code == 200
        granted = _submit(client, key_id, body=BATCHED_REQUEST)
        _cancel(client, owner.api_key, granted)
        assert granted.status_code == 202, granted.get_data(as_text=True)

    def test_a_granted_role_ends_the_moment_a_moderator_removes_it_from_the_owner(self, app: Flask, client: FlaskClient, funded) -> None:
        owner = funded(trusted=True)
        key_id = _create_key(client, owner, roles=["trusted"])

        _set_roles(app, owner.id, trusted=False)
        response = _submit(client, key_id, body=BATCHED_REQUEST)

        _assert_refused(response, 400, "RequiresTrust")

    def test_an_owner_cannot_grant_a_role_their_account_lacks(self, client: FlaskClient, funded) -> None:
        owner = funded()

        response = client.put("/api/v2/sharedkeys", json={"kudos": 10, "roles": ["trusted"]}, headers=_headers(owner.api_key))

        _assert_refused(response, 403, "SharedKeyRoleNotHeld")

    def test_a_moderators_key_is_timed_out_for_a_filtered_prompt_like_anyone_else(
        self,
        client: FlaskClient,
        funded,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Moderators are exempt from the timeout so that they can test the filter.

        Requests through a key handed out by a moderator are not such tests.
        """
        from horde import countermeasures
        from tests.integration.test_prompt_moderation import _patch_checker

        owner = funded(moderator=True)
        key_id = _create_key(client, owner)
        _patch_checker(monkeypatch, 2)
        key_ip, moderator_ip = _unique_ip(), _unique_ip()

        _assert_refused(_submit(client, key_id, ipaddr=key_ip, **FILTERED), 400, "CorruptPrompt")
        _assert_refused(_submit(client, owner.api_key, ipaddr=moderator_ip, **FILTERED), 400, "CorruptPrompt")

        assert countermeasures.ip_s_r.get(key_ip) is not None
        assert countermeasures.ip_s_r.get(moderator_ip) is None

    def test_a_service_accounts_key_speaks_for_proxied_accounts_only_with_the_service_grant(
        self, app: Flask, client: FlaskClient, funded
    ) -> None:
        """A service's backend may hold a key granted the service role. A key handed to anyone else stays ungranted, so
        its holder cannot attribute requests to the service's users."""
        owner = funded()
        _set_roles(app, owner.id, service=True)
        ungranted_key_id = _create_key(client, owner)
        granted_key_id = _create_key(client, owner, roles=["service"])

        ungranted = _submit(client, ungranted_key_id, proxied_account="someone_else")
        granted = _submit(client, granted_key_id, proxied_account="someone_else")
        _cancel(client, owner.api_key, granted)

        _assert_refused(ungranted, 400, "OnlyServiceAccountProxy")
        assert granted.status_code == 202, granted.get_data(as_text=True)

    def test_only_a_service_account_can_grant_the_service_role(self, client: FlaskClient, funded) -> None:
        owner = funded(trusted=True)

        response = client.put("/api/v2/sharedkeys", json={"kudos": 10, "roles": ["service"]}, headers=_headers(owner.api_key))

        _assert_refused(response, 403, "SharedKeyRoleNotHeld")

    def test_a_key_made_before_roles_existed_keeps_its_owners_allowances(self, app: Flask, client: FlaskClient, funded) -> None:
        """Integrations that use existing keys must keep working on the day this ships."""
        from horde.classes.base.user import UserSharedKey
        from horde.flask import db

        owner = funded(trusted=True)
        key_id = _create_key(client, owner)
        with app.app_context():
            db.session.get(UserSharedKey, uuid.UUID(key_id)).roles = None
            db.session.commit()

        response = _submit(client, key_id, body=BATCHED_REQUEST)
        _cancel(client, owner.api_key, response)

        assert response.status_code == 202, response.get_data(as_text=True)
        assert client.get(f"/api/v2/sharedkeys/{key_id}").get_json().get("roles") is None


class TestOwnerLimits:
    """An owner limits a key so that a leak or a misbehaving holder costs little."""

    def test_a_text_only_key_cannot_fund_images(self, client: FlaskClient, funded) -> None:
        owner = funded()
        key_id = _create_key(client, owner, scopes=["text_generation"])

        image = _submit(client, key_id)
        text = _submit(client, key_id, text=True)
        _cancel(client, owner.api_key, text, text=True)

        _assert_refused(image, 403, "SharedKeyScopeDenied")
        assert text.status_code == 202, text.get_data(as_text=True)

    def test_a_model_allowlist_refuses_other_models(self, client: FlaskClient, funded) -> None:
        owner = funded()
        key_id = _create_key(client, owner, allowed_models=["some_other_model"])

        _assert_refused(_submit(client, key_id), 403, "SharedKeyModelDenied")

        assert _patch_key(client, owner, key_id, allowed_models=None).status_code == 200
        reopened = _submit(client, key_id)
        _cancel(client, owner.api_key, reopened)
        assert reopened.status_code == 202, reopened.get_data(as_text=True)

    def test_a_key_pinned_to_a_school_network_refuses_requests_from_elsewhere(self, client: FlaskClient, funded) -> None:
        owner = funded()
        key_id = _create_key(client, owner, allowed_networks=["192.0.2.0/24"])

        inside = _submit(client, key_id, ipaddr="192.0.2.40")
        outside = _submit(client, key_id, ipaddr="198.51.100.40")
        _cancel(client, owner.api_key, inside)

        assert inside.status_code == 202, inside.get_data(as_text=True)
        _assert_refused(outside, 403, "SharedKeyNetworkDenied")

    def test_a_concurrency_limit_caps_waiting_generations_until_some_finish(self, client: FlaskClient, funded) -> None:
        owner = funded()
        key_id = _create_key(client, owner, max_concurrency=2)

        first = _submit(client, key_id)
        assert first.status_code == 202, first.get_data(as_text=True)
        over = _submit(client, key_id)
        _assert_refused(over, 403, "SharedKeyConcurrencyExceeded")

        _cancel(client, owner.api_key, first)
        after_cancel = _submit(client, key_id)
        _cancel(client, owner.api_key, after_cancel)
        assert after_cancel.status_code == 202, after_cancel.get_data(as_text=True)

    def test_the_network_allowlist_is_shown_to_the_owner_only(self, client: FlaskClient, funded, make_api_user: MakeApiUser) -> None:
        """An allowlist can reveal the networks used by a school or a business."""
        owner = funded()
        key_id = _create_key(client, owner, allowed_networks=["192.0.2.0/24"])

        as_owner = client.get(f"/api/v2/sharedkeys/{key_id}", headers=_headers(owner.api_key))
        anonymous = client.get(f"/api/v2/sharedkeys/{key_id}")
        as_holder = client.get(f"/api/v2/sharedkeys/{key_id}", headers=_headers(key_id))

        assert as_owner.get_json()["allowed_networks"] == ["192.0.2.0/24"]
        assert "192.0.2" not in anonymous.get_data(as_text=True)
        assert "192.0.2" not in as_holder.get_data(as_text=True)


class _RefusalCounts:
    """Record the return codes counted by the shared key refusal metric."""

    def __init__(self) -> None:
        self.return_codes: list[str] = []

    def add(self, amount: int, attributes: dict[str, str]) -> None:
        self.return_codes.extend([attributes["horde.rc"]] * amount)


class TestSuspension:
    """Owners and moderators can stop a key without deleting it, so the key stays for review."""

    def test_an_owner_suspends_and_reactivates_a_key(
        self,
        client: FlaskClient,
        funded,
        monkeypatch: pytest.MonkeyPatch,
        recorded: _EventRecorder,
    ) -> None:
        from horde import access_control
        from horde.database.sharedkeys import STATUS_CHANGED_EVENT

        refusals = _RefusalCounts()
        monkeypatch.setattr(access_control, "sharedkey_refusals", refusals)
        owner = funded()
        key_id = _create_key(client, owner)

        assert _patch_key(client, owner, key_id, status="suspended_by_owner").status_code == 200
        suspended = _submit(client, key_id)
        assert _patch_key(client, owner, key_id, status="active").status_code == 200
        reactivated = _submit(client, key_id)
        _cancel(client, owner.api_key, reactivated)

        _assert_refused(suspended, 403, "SharedKeySuspended")
        assert reactivated.status_code == 202, reactivated.get_data(as_text=True)
        assert refusals.return_codes == ["SharedKeySuspended"]
        changes = [(event["actor"], event["old_status"], event["new_status"]) for event in recorded.named(STATUS_CHANGED_EVENT)]
        assert changes == [("owner", "active", "suspended_by_owner"), ("owner", "suspended_by_owner", "active")]

    def test_a_key_created_suspended_is_not_reported_as_a_status_change(
        self, client: FlaskClient, funded, recorded: _EventRecorder
    ) -> None:
        from horde.database.sharedkeys import STATUS_CHANGED_EVENT

        owner = funded()
        key_id = _create_key(client, owner, status="suspended_by_owner")

        _assert_refused(_submit(client, key_id), 403, "SharedKeySuspended")
        assert recorded.named(STATUS_CHANGED_EVENT) == []

    def test_an_owner_cannot_lift_a_moderators_suspension_but_a_moderator_can(
        self,
        client: FlaskClient,
        funded,
        make_api_user: MakeApiUser,
        recorded: _EventRecorder,
    ) -> None:
        from horde.database.sharedkeys import STATUS_CHANGED_EVENT

        owner = funded()
        moderator = make_api_user(moderator=True)
        key_id = _create_key(client, owner)
        moderation_path = f"/api/v2/operations/moderation/sharedkeys/{key_id}"

        suspended = client.patch(
            moderation_path,
            json={"status": "suspended_by_moderator", "reason": "Used to post spam prompts"},
            headers=_headers(moderator.api_key),
        )
        owner_attempt = _patch_key(client, owner, key_id, status="active")
        renamed = _patch_key(client, owner, key_id, name="still mine")
        while_suspended = _submit(client, key_id)
        reinstated = client.patch(
            moderation_path, json={"status": "active", "reason": "Owner explained"}, headers=_headers(moderator.api_key)
        )

        assert suspended.status_code == 200, suspended.get_data(as_text=True)
        _assert_refused(owner_attempt, 403, "SharedKeyModeratorSuspension")
        assert renamed.status_code == 200, renamed.get_data(as_text=True)
        _assert_refused(while_suspended, 403, "SharedKeySuspended")
        assert reinstated.status_code == 200, reinstated.get_data(as_text=True)
        owner_view = client.get(f"/api/v2/sharedkeys/{key_id}", headers=_headers(owner.api_key)).get_json()
        assert (owner_view["status"], owner_view["status_reason"]) == ("active", "Owner explained")
        changes = [
            (event["actor"], event["actor_user_id"], event["new_status"], event["has_reason"])
            for event in recorded.named(STATUS_CHANGED_EVENT)
        ]
        assert changes == [
            ("moderator", moderator.id, "suspended_by_moderator", True),
            ("moderator", moderator.id, "active", True),
        ]
        assert all("reason" not in event for event in recorded.named(STATUS_CHANGED_EVENT)), "the event must not hold the reason"

    def test_moderator_actions_need_a_moderator_and_a_reason(self, client: FlaskClient, funded, make_api_user: MakeApiUser) -> None:
        owner = funded()
        moderator = make_api_user(moderator=True)
        key_id = _create_key(client, owner)
        path = f"/api/v2/operations/moderation/sharedkeys/{key_id}"

        by_owner = client.patch(path, json={"status": "suspended_by_moderator", "reason": "x"}, headers=_headers(owner.api_key))
        without_reason = client.patch(path, json={"status": "suspended_by_moderator", "reason": "  "}, headers=_headers(moderator.api_key))
        as_owner_status = client.patch(path, json={"status": "suspended_by_owner", "reason": "x"}, headers=_headers(moderator.api_key))
        unknown_status = client.patch(path, json={"status": "deleted", "reason": "x"}, headers=_headers(moderator.api_key))

        _assert_refused(by_owner, 403, "NotModerator")
        assert without_reason.status_code == 400
        _assert_refused(as_owner_status, 400, "InvalidSharedKeyPolicy")
        _assert_refused(unknown_status, 400, "InvalidSharedKeyPolicy")


def _corrupt_prompt_suspicions(app: Flask, user_id: int) -> int:
    from horde.classes.base.user import UserSuspicions
    from horde.flask import db
    from horde.suspicions import Suspicions

    with app.app_context():
        return db.session.query(UserSuspicions).filter_by(user_id=user_id, suspicion_id=int(Suspicions.CORRUPT_PROMPT)).count()


class TestFilteredPrompts:
    """Filtered prompts sent with a key are reported for review. The key stays usable for its other holders."""

    def test_filtered_prompts_time_out_the_holder_without_suspending_the_key_or_suspecting_the_owner(
        self,
        app: Flask,
        client: FlaskClient,
        funded,
        monkeypatch: pytest.MonkeyPatch,
        recorded: _EventRecorder,
    ) -> None:
        """A classroom key reaches many students, so one student's filtered prompts must not cut off the rest of the class.

        The student is not the owner, so the prompts time out the student's address and are not held against the owner.
        """
        from horde import countermeasures
        from horde.database.sharedkeys import FILTERED_PROMPT_EVENT
        from tests.integration.test_prompt_moderation import _patch_checker

        owner = funded()
        key_id = _create_key(client, owner)
        _patch_checker(monkeypatch, 2)
        holder_ip = _unique_ip()

        for _ in range(3):
            _assert_refused(_submit(client, key_id, ipaddr=holder_ip, **FILTERED), 400, "CorruptPrompt")
        _patch_checker(monkeypatch, 0)
        after_filtered = _submit(client, key_id)
        _cancel(client, owner.api_key, after_filtered)

        assert after_filtered.status_code == 202, after_filtered.get_data(as_text=True)
        details = client.get(f"/api/v2/sharedkeys/{key_id}", headers=_headers(owner.api_key)).get_json()
        assert details["status"] == "active"
        assert countermeasures.ip_s_r.get(holder_ip) is not None
        assert _corrupt_prompt_suspicions(app, owner.id) == 0
        reported = recorded.named(FILTERED_PROMPT_EVENT)
        assert reported == [{"sharedkey_id": key_id, "owner_id": owner.id}] * 3

    def test_the_owners_own_filtered_prompts_are_held_against_the_owner_and_not_reported_for_their_keys(
        self,
        app: Flask,
        client: FlaskClient,
        funded,
        monkeypatch: pytest.MonkeyPatch,
        recorded: _EventRecorder,
    ) -> None:
        from tests.integration.test_prompt_moderation import _patch_checker

        owner = funded()
        _create_key(client, owner)
        _patch_checker(monkeypatch, 2)

        for _ in range(3):
            _assert_refused(_submit(client, owner.api_key, **FILTERED), 400, "CorruptPrompt")

        assert _corrupt_prompt_suspicions(app, owner.id) == 1
        assert recorded.events == []


class TestStyleFundedKeys:
    """A style's key pays for the requests of the style's users. When the key's policy refuses a request, the caller's key pays instead."""

    @pytest.fixture
    def style_with_key(self, client: FlaskClient, funded) -> Callable[..., tuple[ApiUser, str, str]]:
        from tests.integration.test_image_styles import style_body

        created: list[tuple[ApiUser, str]] = []

        def _make(**policy: Any) -> tuple[ApiUser, str, str]:
            owner = funded(customizer=True)
            key_id = _create_key(client, owner, **policy)
            body = style_body(f"funded {uuid.uuid4().hex[:8]}", sharedkey=key_id, models=["stable_diffusion"], public=True)
            response = client.post("/api/v2/styles/image", json=body, headers=_headers(owner.api_key))
            assert response.status_code == 200, response.get_data(as_text=True)
            style_id = response.get_json()["id"]
            created.append((owner, style_id))
            return owner, key_id, style_id

        yield _make
        for owner, style_id in created:
            client.delete(f"/api/v2/styles/image/{style_id}", headers=_headers(owner.api_key))

    def _funding_key(self, app: Flask, request_id: str) -> str | None:
        from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
        from horde.flask import db

        with app.app_context():
            waiting_prompt = db.session.get(ImageWaitingPrompt, uuid.UUID(request_id))
            return None if waiting_prompt.sharedkey_id is None else str(waiting_prompt.sharedkey_id)

    @pytest.mark.parametrize(
        ("policy", "funds"),
        [
            ({}, True),
            ({"scopes": ["text_generation"]}, False),
            ({"allowed_networks": ["192.0.2.0/24"]}, False),
            ({"allowed_models": ["only_this_model"]}, True),
        ],
        ids=["open key", "text-only key", "pinned elsewhere", "model allowlist ignored for its style"],
    )
    def test_a_refusing_style_key_leaves_the_caller_paying_rather_than_failing(
        self,
        app: Flask,
        client: FlaskClient,
        funded,
        style_with_key,
        policy: dict[str, Any],
        funds: bool,
    ) -> None:
        _, key_id, style_id = style_with_key(**policy)
        caller = funded()

        response = _submit(client, caller.api_key, ipaddr="198.51.100.20", style=style_id)
        try:
            assert response.status_code == 202, response.get_data(as_text=True)
            funding_key = self._funding_key(app, response.get_json()["id"])
        finally:
            _cancel(client, caller.api_key, response)

        assert (funding_key == key_id) is funds

    def test_a_suspended_style_key_leaves_the_caller_paying(self, app: Flask, client: FlaskClient, funded, style_with_key) -> None:
        owner, key_id, style_id = style_with_key()
        assert _patch_key(client, owner, key_id, status="suspended_by_owner").status_code == 200
        caller = funded()

        response = _submit(client, caller.api_key, style=style_id)
        try:
            assert response.status_code == 202, response.get_data(as_text=True)
            assert self._funding_key(app, response.get_json()["id"]) is None
        finally:
            _cancel(client, caller.api_key, response)


CHAT_FRONT_PASSKEY: str = "chat front integration passkey"
"""The proxy passkey of the chat front in these tests."""

CHAT_FRONT_ADDRESS: str = "198.51.100.77"
"""The address of the chat front. Every chat request in these tests arrives from it."""


class TestChatFront:
    """The OpenAI-compatible chat front sends each end user's chat through the Horde with the end user's key.

    The front forwards the end user's address, so a shared key used behind the front is held to its network allowlist
    per end user. Checking the front's address would put every holder on one network.
    """

    @pytest.fixture
    def chat_worker(self, app: Flask, client: FlaskClient, funded) -> Iterator[FakeChatWorker]:
        """A chat worker that is online for the test and taken offline afterwards."""
        worker_owner = funded()
        worker = FakeChatWorker(
            client=client,
            headers=_headers(worker_owner.api_key),
            name=f"CICD Shared Key Chat {uuid.uuid4().hex[:8]}",
            models=[CHAT_MODEL],
        )
        worker.pop()
        yield worker
        take_worker_offline(app, worker.name)

    @pytest.fixture
    def front_headers(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
        """Register the chat front's passkey and return the header that proves a request came from the front."""
        from horde.apis import request_utils
        from horde.database.classes import CachedPasskeys

        passkeys = CachedPasskeys.__new__(CachedPasskeys)
        passkeys.passkeys = {1: CHAT_FRONT_PASSKEY}
        monkeypatch.setattr(request_utils, "get_cached_passkeys", lambda: passkeys)
        return {"Proxy-Authorization": CHAT_FRONT_PASSKEY}

    @staticmethod
    def _chat(client: FlaskClient, key_id: str, front_headers: dict[str, str], end_user_ip: str):
        body = {"model": CHAT_MODEL, "messages": CHAT_MESSAGES, "temperature": 0.5}
        headers = {**_headers(key_id), **front_headers, "Proxied-For": end_user_ip}
        return client.post("/api/v2/generate/chat/async", json=body, headers=headers, environ_base={"REMOTE_ADDR": CHAT_FRONT_ADDRESS})

    def test_a_key_pinned_to_a_school_network_accepts_chats_only_from_its_students(
        self, client: FlaskClient, funded, chat_worker: FakeChatWorker, front_headers: dict[str, str]
    ) -> None:
        """The front's address is outside the allowlist, so a chat is accepted only because of the forwarded address."""
        owner = funded()
        key_id = _create_key(client, owner, allowed_networks=["192.0.2.0/24"])

        student = self._chat(client, key_id, front_headers, "192.0.2.40")
        outsider = self._chat(client, key_id, front_headers, "203.0.113.40")
        if student.status_code == 202:
            cancel_request(client, _headers(owner.api_key), "chat", student.get_json()["id"])

        assert student.status_code == 202, student.get_data(as_text=True)
        _assert_refused(outsider, 403, "SharedKeyNetworkDenied")

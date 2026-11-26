# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Test shared key roles through the API, from the side of owners and key holders.

Each test performs an action of an owner or a key holder, and checks the result seen by the other. The generation
requests run against an empty queue, so a 202 response only means that the request was allowed.
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


def _corrupt_prompt_suspicions(app: Flask, user_id: int) -> int:
    from horde.classes.base.user import UserSuspicions
    from horde.flask import db
    from horde.suspicions import Suspicions

    with app.app_context():
        return db.session.query(UserSuspicions).filter_by(user_id=user_id, suspicion_id=int(Suspicions.CORRUPT_PROMPT)).count()


class TestFilteredPrompts:
    """Filtered prompts sent with a key count against the holder's address. They are not held against the owner."""

    def test_filtered_prompts_time_out_the_holder_without_suspecting_the_owner(
        self,
        app: Flask,
        client: FlaskClient,
        funded,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A classroom key reaches many students. A student is not the owner, so the student's filtered prompts time out
        the student's address and are not held against the owner."""
        from horde import countermeasures
        from tests.integration.test_prompt_moderation import _patch_checker

        owner = funded()
        key_id = _create_key(client, owner)
        _patch_checker(monkeypatch, 2)
        holder_ip = _unique_ip()

        for _ in range(3):
            _assert_refused(_submit(client, key_id, ipaddr=holder_ip, **FILTERED), 400, "CorruptPrompt")

        assert countermeasures.ip_s_r.get(holder_ip) is not None
        assert _corrupt_prompt_suspicions(app, owner.id) == 0

    def test_the_owners_own_filtered_prompts_are_held_against_the_owner(
        self,
        app: Flask,
        client: FlaskClient,
        funded,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from tests.integration.test_prompt_moderation import _patch_checker

        owner = funded()
        _create_key(client, owner)
        _patch_checker(monkeypatch, 2)

        for _ in range(3):
            _assert_refused(_submit(client, owner.api_key, **FILTERED), 400, "CorruptPrompt")

        assert _corrupt_prompt_suspicions(app, owner.id) == 1

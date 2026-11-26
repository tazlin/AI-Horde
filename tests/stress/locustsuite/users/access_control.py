# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Locust users for the shared key policy and linked-account scenario.

The scenario creates one shared key with a concurrency limit at test start, then runs four kinds of user against it:

- ``SharedKeyHolder`` submits and cancels image requests through the key, as a class or a bot sharing one key would.
- ``SharedKeyOwner`` suspends and reactivates the key, and checks the concurrency invariant.
- ``LinkedAccountFarmer`` moves kudos between requestor accounts. Every Locust user shares the load host's address.
  On a target with a real ``secret_key``, the accounts become linked, as they would be for a farm on one network. A
  link does not refuse a transfer, so any refusal outside ``TRANSFER_REFUSALS`` is counted as a failure. The target
  reports each linked transfer with a structured event and the ``horde.kudos.transfers`` metric.
- ``LinkReviewer`` reads the moderator link listing of the requestor accounts while links are being written.

The run passes when the Locust statistics do not show server errors or ``[invariant]`` failures.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Iterable

import requests
from locust import HttpUser, between, tag, task
from locust.runners import MasterRunner

from ..config import _config
from ..helpers import _headers, _random_prompt, _record_expected, _safe_json

SHAREDKEY_REFUSALS: frozenset[str] = frozenset(
    {
        "SharedKeySuspended",
        "SharedKeyConcurrencyExceeded",
    },
)
"""The refusals expected for a holder while the owner suspends the key and other holders fill its concurrency limit."""
TRANSFER_REFUSALS: frozenset[str] = frozenset(
    {
        "TooFastKudosTransfers",
        "KudosTransferNotEnough",
    },
)
"""The refusals expected for a farmer, from the transfer rate limit and from accounts that ran out of kudos."""
SETUP_TIMEOUT_SECONDS: float = 30.0
"""How long the setup calls at test start may take."""

access_state: dict[str, object] = {
    "owner_api_key": "",
    "moderator_api_key": "",
    "sharedkey_id": "",
    "max_concurrency": 0,
    "requestor_accounts": [],
    "concurrency_tolerance": 0,
}
"""The scenario's settings and the key created at test start, shared by every user in the process."""

_suspension_lock = threading.Lock()


def _requestor_keys() -> list[str]:
    return list(_config.get("requestor_api_keys", []))


def _find_accounts(host: str, api_keys: Iterable[str]) -> list[tuple[str, int, str]]:
    """Return the API key, user ID and username of each requestor account found on the target."""
    accounts: list[tuple[str, int, str]] = []
    for api_key in api_keys:
        resp = requests.get(f"{host}/api/v2/find_user", headers=_headers(api_key), timeout=SETUP_TIMEOUT_SECONDS)
        if resp.ok:
            body = resp.json()
            accounts.append((api_key, int(body["id"]), str(body["username"])))
    return accounts


def configure_access_control(environment) -> None:
    """Copy the scenario arguments, create the scenario's shared key and look up the requestor accounts.

    A distributed master does not run users, so it does not create the key.

    Raises:
        RuntimeError: The owner key is missing or the key could not be created.
    """
    options = environment.parsed_options
    access_state["owner_api_key"] = options.access_owner_api_key
    access_state["moderator_api_key"] = options.access_moderator_api_key
    access_state["max_concurrency"] = int(options.access_max_concurrency)
    access_state["concurrency_tolerance"] = int(options.access_holders or options.num_users or 0)
    if isinstance(environment.runner, MasterRunner):
        return
    if not options.access_owner_api_key:
        raise RuntimeError("--access-owner-api-key is required: the scenario creates its shared key with it.")
    host = (environment.host or "").rstrip("/")
    resp = requests.put(
        f"{host}/api/v2/sharedkeys",
        json={
            "kudos": int(options.access_key_kudos),
            "name": f"locust access {int(time.time())}",
            "scopes": ["image_generation"],
            "max_concurrency": int(options.access_max_concurrency),
        },
        headers=_headers(options.access_owner_api_key),
        timeout=SETUP_TIMEOUT_SECONDS,
    )
    if not resp.ok:
        raise RuntimeError(f"Could not create the scenario's shared key: {resp.status_code} {resp.text[:200]}")
    access_state["sharedkey_id"] = resp.json()["id"]
    access_state["requestor_accounts"] = _find_accounts(host, _requestor_keys())


def _expect(environment, resp, name: str, expected_rcs: frozenset[str]) -> bool:
    """Mark a response as success when it is OK or one of the expected refusals, and as failure otherwise.

    Returns:
        True when the response was OK.
    """
    if resp.ok:
        resp.success()
        return True
    body = _safe_json(resp) or {}
    if resp.status_code == 429 or (400 <= resp.status_code < 500 and body.get("rc") in expected_rcs):
        resp.success()
        _record_expected(
            environment, resp.request_meta.get("method", "POST"), name, resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
        )
        return False
    resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")
    return False


def _image_payload() -> dict[str, object]:
    return {
        "prompt": _random_prompt(),
        "params": {"width": 512, "height": 512, "steps": 10, "n": 1},
        "models": _config.get("models", []),
        "r2": True,
    }


class SharedKeyHolder(HttpUser):
    """Submit image requests through the scenario's shared key and cancel each one a moment later."""

    weight = 4
    fixed_count = 0
    wait_time = between(0.5, 2)

    @tag("access_control", "image")
    @task
    def submit_through_key(self) -> None:
        sharedkey_id = str(access_state["sharedkey_id"])
        if not sharedkey_id:
            return
        name = "/api/v2/generate/async [sharedkey]"
        with self.client.post(
            "/api/v2/generate/async", json=_image_payload(), headers=_headers(sharedkey_id), catch_response=True, name=name
        ) as resp:
            accepted = _expect(self.environment, resp, name, SHAREDKEY_REFUSALS)
            request_id = (_safe_json(resp) or {}).get("id") if accepted else None
        if request_id is None:
            return
        time.sleep(random.uniform(0.5, 3.0))
        with self.client.delete(
            f"/api/v2/generate/status/{request_id}",
            headers=_headers(sharedkey_id),
            catch_response=True,
            name="/api/v2/generate/status/[id] [sharedkey-cancel]",
        ) as resp:
            _expect(self.environment, resp, "/api/v2/generate/status/[id] [sharedkey-cancel]", frozenset({"RequestNotFound"}))


class SharedKeyOwner(HttpUser):
    """Suspend and reactivate the key while holders use it, and check its concurrency."""

    weight = 1
    fixed_count = 0
    wait_time = between(2, 5)

    def _patch_status(self, status: str) -> None:
        name = f"/api/v2/sharedkeys/[id] [{status}]"
        with self.client.patch(
            f"/api/v2/sharedkeys/{access_state['sharedkey_id']}",
            json={"status": status},
            headers=_headers(str(access_state["owner_api_key"])),
            catch_response=True,
            name=name,
        ) as resp:
            _expect(self.environment, resp, name, frozenset())

    @tag("access_control")
    @task(1)
    def suspend_briefly(self) -> None:
        # One owner at a time, so two owners cannot interleave and leave the key suspended.
        if not access_state["sharedkey_id"] or not _suspension_lock.acquire(blocking=False):
            return
        try:
            self._patch_status("suspended_by_owner")
            time.sleep(random.uniform(1.0, 3.0))
            self._patch_status("active")
        finally:
            _suspension_lock.release()

    @tag("access_control")
    @task(3)
    def check_concurrency_invariant(self) -> None:
        """Fail when the key has more waiting generations than its limit plus one request per concurrent holder.

        The limit is checked before the request is stored, so requests checked at the same moment can each pass. The
        tolerance is the number of holder users, the most requests that can be in that window at once.
        """
        if not access_state["sharedkey_id"]:
            return
        with self.client.get(
            f"/api/v2/sharedkeys/{access_state['sharedkey_id']}",
            headers=_headers(str(access_state["owner_api_key"])),
            catch_response=True,
            name="/api/v2/sharedkeys/[id] [owner]",
        ) as resp:
            if not _expect(self.environment, resp, "/api/v2/sharedkeys/[id] [owner]", frozenset()):
                return
            queued = int(((_safe_json(resp) or {}).get("active_usage") or {}).get("image", {}).get("queued", 0))
        ceiling = int(access_state["max_concurrency"]) + int(access_state["concurrency_tolerance"])
        self.environment.events.request.fire(
            request_type="CHECK",
            name="[invariant] sharedkey concurrency",
            response_time=0,
            response_length=queued,
            exception=AssertionError(f"{queued} waiting generations, ceiling {ceiling}") if queued > ceiling else None,
            context={},
        )


class LinkedAccountFarmer(HttpUser):
    """Generate with requestor accounts, which links them to the load host's network, and move kudos between them."""

    weight = 1
    fixed_count = 0
    wait_time = between(1, 3)

    @tag("access_control", "image")
    @task(1)
    def generate_once(self) -> None:
        keys = _requestor_keys()
        if not keys:
            return
        api_key = random.choice(keys)
        name = "/api/v2/generate/async [farm]"
        with self.client.post(
            "/api/v2/generate/async", json=_image_payload(), headers=_headers(api_key), catch_response=True, name=name
        ) as resp:
            accepted = _expect(self.environment, resp, name, frozenset({"TooManyPrompts", "TimeoutIP"}))
            request_id = (_safe_json(resp) or {}).get("id") if accepted else None
        if request_id is not None:
            self.client.delete(
                f"/api/v2/generate/status/{request_id}", headers=_headers(api_key), name="/api/v2/generate/status/[id] [farm-cancel]"
            )

    @tag("access_control")
    @task(2)
    def cash_out(self) -> None:
        accounts = list(access_state["requestor_accounts"])
        if len(accounts) < 2:
            return
        (sender_key, _sender_id, _sender_name), (_receiver_key, _receiver_id, receiver_name) = random.sample(accounts, 2)
        name = "/api/v2/kudos/transfer [farm]"
        with self.client.post(
            "/api/v2/kudos/transfer",
            json={"username": receiver_name, "amount": 1},
            headers=_headers(sender_key),
            catch_response=True,
            name=name,
        ) as resp:
            _expect(self.environment, resp, name, TRANSFER_REFUSALS)


class LinkReviewer(HttpUser):
    """Read the moderator link listing of the requestor accounts while links are written."""

    weight = 1
    fixed_count = 0
    wait_time = between(2, 6)

    @tag("access_control", "moderation")
    @task
    def review_links(self) -> None:
        moderator_key = str(access_state["moderator_api_key"])
        accounts = list(access_state["requestor_accounts"])
        if not moderator_key or not accounts:
            return
        name = "/api/v2/operations/moderation/account_links [user]"
        with self.client.get(
            "/api/v2/operations/moderation/account_links",
            params={"user_id": random.choice(accounts)[1]},
            headers=_headers(moderator_key),
            catch_response=True,
            name=name,
        ) as resp:
            _expect(self.environment, resp, name, frozenset())

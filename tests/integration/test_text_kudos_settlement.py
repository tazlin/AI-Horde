# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a text request costs its requester and pays its worker, observed through the API.

These cases tie the four places a price appears to each other: the accepted response's ``kudos``, the
dry run's total, the hold a request without kudos is refused for, and the settlement on submit. They
read balances after the ledger is folded, so they state the invariants a client can rely on.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient

from tests.fixture_types import MakeApiUser

pytestmark = pytest.mark.integration

TEST_MODELS = ["elinas/chronos-70b-v2"]
SHORT_PROMPT = "a horde of cute stable robots repairing a mainframe"
LONG_PROMPT = ("The maintenance log of the mainframe, entry by entry, as the robots recorded it. ") * 600
"""About 48,000 characters, estimated at 12,000 tokens, well past the 4,096-token free-tier reference."""


def _headers(api_key: str, client_agent: str) -> dict[str, str]:
    return {"apikey": api_key, "Client-Agent": client_agent}


def _balance(app: Flask, settle_kudos: Callable[[], int], user_id: int) -> float:
    from horde.database import functions as database

    settle_kudos()
    with app.app_context():
        return database.find_user_by_id(user_id).kudos


def _worker_kudos(app: Flask, settle_kudos: Callable[[], int], worker_name: str) -> float:
    from horde.classes.kobold.worker import TextWorker
    from horde.database import functions as database

    settle_kudos()
    with app.app_context():
        worker = database.find_worker_by_name(worker_name, worker_class=TextWorker)
        return worker.kudos if worker is not None else 0


def _post(client: FlaskClient, headers: dict[str, str], *, prompt: str, max_length: int, n: int = 1, **extra: Any):
    body = {
        "prompt": prompt,
        "params": {"max_length": max_length, "max_context_length": 16384, "n": n, "temperature": 1},
        "models": TEST_MODELS,
        "trusted_workers": True,
        "validated_backends": False,
        "allow_downgrade": False,
        **extra,
    }
    return client.post("/api/v2/generate/text/async", json=body, headers=headers)


def _pop_and_submit(client: FlaskClient, headers: dict[str, str], *, worker_name: str, generation: str, state: str = "ok") -> float:
    pop = client.post(
        "/api/v2/generate/text/pop",
        json={
            "name": worker_name,
            "models": TEST_MODELS,
            "bridge_agent": headers["Client-Agent"],
            "max_context_length": 16384,
            "max_length": 512,
        },
        headers=headers,
    )
    assert pop.status_code < 400, pop.get_data(as_text=True)
    job_id = pop.get_json()["id"]
    assert job_id is not None, pop.get_json()
    submit = client.post(
        "/api/v2/generate/text/submit", json={"id": job_id, "generation": generation, "state": state, "seed": 0}, headers=headers
    )
    assert submit.status_code < 400, submit.get_data(as_text=True)
    return submit.get_json()["reward"]


class TestQuoteHoldAndDryRunAgree:
    def test_the_hold_is_the_quote_plus_the_burn_and_the_dry_run_adds_the_tax(
        self,
        app: Flask,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        request_headers: dict[str, str],
        settle_kudos: Callable[[], int],
    ) -> None:
        agent = request_headers["Client-Agent"]
        rich = make_api_user(trusted=True, kudos=1_000_000)
        poor = make_api_user(trusted=True, kudos=5)
        settle_kudos()

        accepted = _post(client, _headers(rich.api_key, agent), prompt=LONG_PROMPT, max_length=512)
        assert accepted.status_code == 202, accepted.get_data(as_text=True)
        client.delete(f"/api/v2/generate/text/status/{accepted.get_json()['id']}", headers=_headers(rich.api_key, agent))
        dry_run = _post(client, _headers(rich.api_key, agent), prompt=LONG_PROMPT, max_length=512, dry_run=True)
        assert dry_run.status_code == 200, dry_run.get_data(as_text=True)
        refused = _post(client, _headers(poor.api_key, agent), prompt=LONG_PROMPT, max_length=512)
        assert refused.status_code == 403, refused.get_data(as_text=True)
        assert refused.get_json()["rc"] == "KudosUpfront"

        required = float(re.search(r"requires ([0-9.]+) kudos", refused.get_json()["message"]).group(1))
        quoted = accepted.get_json()["kudos"]
        # One job: the hold is quote + 1 burn, the dry run is the hold + 1 activation tax, and the
        # accepted response rounds the bare quote.
        assert round(required) == quoted + 1
        assert dry_run.get_json()["kudos"] == round(required + 1)
        assert quoted > 1_000, "a 12,000-token prompt on a 70B model is not a cheap request"

    def test_the_refusal_states_the_prompt_estimate_and_the_free_allowance(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        request_headers: dict[str, str],
        settle_kudos: Callable[[], int],
    ) -> None:
        poor = make_api_user(trusted=True, kudos=5)
        settle_kudos()

        refused = _post(client, _headers(poor.api_key, request_headers["Client-Agent"]), prompt=LONG_PROMPT, max_length=512)

        message = refused.get_json()["message"]
        assert f"prompt of about {math.ceil(len(LONG_PROMPT) / 4)} tokens" in message
        allowance = int(re.search(r"at most (\d+) tokens", message).group(1))
        assert 16 <= allowance < 512

    def test_a_request_that_can_be_covered_takes_a_hold_of_the_required_amount(
        self,
        app: Flask,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        request_headers: dict[str, str],
        settle_kudos: Callable[[], int],
    ) -> None:
        from horde.classes.base.kudos import KudosReservation
        from horde.flask import db

        agent = request_headers["Client-Agent"]
        rich = make_api_user(trusted=True, kudos=1_000_000)
        settle_kudos()

        accepted = _post(client, _headers(rich.api_key, agent), prompt=LONG_PROMPT, max_length=512, n=2)
        assert accepted.status_code == 202, accepted.get_data(as_text=True)
        request_id = accepted.get_json()["id"]
        try:
            with app.app_context():
                hold = db.session.query(KudosReservation).filter_by(business_id=f"upfront:{request_id}").one()
                assert hold.released_at is None
                # Two jobs: (quote + 1) * 2, quoted per job as the rounded figure in the response.
                assert float(hold.original_amount) == pytest.approx((accepted.get_json()["kudos"] + 1) * 2, abs=1.0)
        finally:
            client.delete(f"/api/v2/generate/text/status/{request_id}", headers=_headers(rich.api_key, agent))


class TestSettlementBalances:
    def test_a_completed_job_pays_the_worker_the_reward_and_charges_the_requester_the_reward_plus_burns(
        self,
        app: Flask,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        request_headers: dict[str, str],
        settle_kudos: Callable[[], int],
    ) -> None:
        agent = request_headers["Client-Agent"]
        requester = make_api_user(trusted=True, kudos=100_000)
        worker_name = f"settlement scribe {uuid.uuid4().hex[:8]}"
        before = _balance(app, settle_kudos, requester.id)

        accepted = _post(client, _headers(requester.api_key, agent), prompt=SHORT_PROMPT, max_length=512)
        assert accepted.status_code == 202, accepted.get_data(as_text=True)
        after_activation = _balance(app, settle_kudos, requester.id)
        reward = _pop_and_submit(client, request_headers, worker_name=worker_name, generation="robot " * 100)
        after_settlement = _balance(app, settle_kudos, requester.id)

        assert reward > 0
        assert after_activation == pytest.approx(before - 1, abs=1)
        assert after_settlement == pytest.approx(after_activation - (reward + 1), abs=1)
        assert _worker_kudos(app, settle_kudos, worker_name) == pytest.approx(reward, abs=1)

    def test_a_censored_job_pays_the_worker_but_waives_the_requesters_generation_charge(
        self,
        app: Flask,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        request_headers: dict[str, str],
        settle_kudos: Callable[[], int],
    ) -> None:
        agent = request_headers["Client-Agent"]
        requester = make_api_user(trusted=True, kudos=100_000)
        worker_name = f"censoring scribe {uuid.uuid4().hex[:8]}"

        accepted = _post(client, _headers(requester.api_key, agent), prompt=SHORT_PROMPT, max_length=512)
        assert accepted.status_code == 202, accepted.get_data(as_text=True)
        after_activation = _balance(app, settle_kudos, requester.id)
        reward = _pop_and_submit(client, request_headers, worker_name=worker_name, generation="robot " * 100, state="censored")
        after_settlement = _balance(app, settle_kudos, requester.id)

        assert reward > 0
        assert _worker_kudos(app, settle_kudos, worker_name) == pytest.approx(reward, abs=1)
        # The generation component is waived; the per-job burn is still charged.
        assert after_settlement == pytest.approx(after_activation - 1, abs=1)

    def test_a_longer_prompt_settles_higher_for_the_same_reply(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        request_headers: dict[str, str],
        settle_kudos: Callable[[], int],
    ) -> None:
        agent = request_headers["Client-Agent"]
        requester = make_api_user(trusted=True, kudos=1_000_000)
        settle_kudos()
        rewards = []
        for prompt in (SHORT_PROMPT, LONG_PROMPT):
            accepted = _post(client, _headers(requester.api_key, agent), prompt=prompt, max_length=512)
            assert accepted.status_code == 202, accepted.get_data(as_text=True)
            rewards.append(
                _pop_and_submit(client, request_headers, worker_name=f"length scribe {uuid.uuid4().hex[:6]}", generation="robot " * 100)
            )

        assert rewards[1] > rewards[0] * 5

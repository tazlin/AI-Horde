# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bounds the text admission gates place on what a job can settle for.

A text request passes three gates before a worker is paid: the dry-run quote tells the requester what
the job will cost, the upfront hold reserves what the jobs will debit, and the free-tier gate decides
which requests may run with no hold at all. Each of these is computed before any worker has taken the
job, and settlement is computed after. These cases state the relations between the two that every
tariff must keep, however the arithmetic on either side changes:

- The hold reserved for a request covers what one of its jobs debits at settlement.
- A request admitted without a hold pays its worker no more for a long prompt than for a short one,
  or it is refused.
- Two requests that settle differently are quoted differently, so a quote cached for one is never
  served for the other.
- A job never settles above its quote.

They are written against the API alone so that they hold on any revision of the tariff: nothing here
depends on where the hold is computed or what the quote is made of. They pass on the tariff before
the prompt was priced at all, and fail on any change that reprices settlement without carrying the
gates with it. ``tests/unit/test_text_kudos_bounds.py`` states the relations that need no database
over stubs, so a run without the integration stack still shows such a change.

These cases need the Postgres the integration suite provisions. Set ``AI_HORDE_REQUIRE_TEST_DEPS=1``
so a missing database fails the run instead of skipping it; a skipped run proves nothing.
"""

from __future__ import annotations

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
MAX_CONTEXT_LENGTH = 16384

_LOG_LINE = "The maintenance log of the mainframe, entry by entry, as the robots recorded it. "
SHORT_PROMPT = _LOG_LINE * 50
"""About 4,000 characters, estimated at 1,000 tokens: longer than any output allowance used here."""
LONG_PROMPT = _LOG_LINE * 600
"""About 48,000 characters, estimated at 12,000 tokens: a prompt the context multiplier prices near its cap."""
FULL_REPLY = "robot " * 1000
"""6,000 characters, so the returned text always reaches the requested ``max_length``."""

FREE_TIER_MAX_LENGTH = 512
"""The largest ``max_length`` the free-tier gate admits with no hold when the queue is quiet."""
HELD_MAX_LENGTH = 1024
"""A ``max_length`` past the free-tier ceiling, so the request has to post a hold."""


def _headers(api_key: str, client_agent: str) -> dict[str, str]:
    return {"apikey": api_key, "Client-Agent": client_agent}


def _post(client: FlaskClient, headers: dict[str, str], *, prompt: str, max_length: int, **extra: Any):
    body = {
        "prompt": prompt,
        "params": {"max_length": max_length, "max_context_length": MAX_CONTEXT_LENGTH, "n": 1, "temperature": 1},
        "models": TEST_MODELS,
        "trusted_workers": True,
        "validated_backends": False,
        "allow_downgrade": False,
        **extra,
    }
    return client.post("/api/v2/generate/text/async", json=body, headers=headers)


def _cancel(client: FlaskClient, headers: dict[str, str], request_id: str) -> None:
    client.delete(f"/api/v2/generate/text/status/{request_id}", headers=headers)


def _hold_amount(app: Flask, request_id: str) -> float | None:
    """Return the amount reserved upfront for a request, or None when it was admitted without a hold."""
    from horde.classes.base.kudos import KudosReservation
    from horde.flask import db

    with app.app_context():
        hold = db.session.query(KudosReservation).filter_by(business_id=f"upfront:{request_id}").one_or_none()
        return None if hold is None else float(hold.original_amount)


def _pop_and_submit(client: FlaskClient, headers: dict[str, str], *, max_length: int) -> float:
    """Take the next job with a fresh worker, return a full-length reply, and report the reward."""
    pop = client.post(
        "/api/v2/generate/text/pop",
        json={
            "name": f"bounds scribe {uuid.uuid4().hex[:8]}",
            "models": TEST_MODELS,
            "bridge_agent": headers["Client-Agent"],
            "max_context_length": MAX_CONTEXT_LENGTH,
            "max_length": max_length,
        },
        headers=headers,
    )
    assert pop.status_code < 400, pop.get_data(as_text=True)
    job_id = pop.get_json()["id"]
    assert job_id is not None, pop.get_json()
    submit = client.post(
        "/api/v2/generate/text/submit",
        json={"id": job_id, "generation": FULL_REPLY, "state": "ok", "seed": 0},
        headers=headers,
    )
    assert submit.status_code < 400, submit.get_data(as_text=True)
    return submit.get_json()["reward"]


def _settle_one(
    client: FlaskClient,
    requester_headers: dict[str, str],
    worker_headers: dict[str, str],
    *,
    prompt: str,
    max_length: int,
) -> float:
    """Submit a request the requester can cover, serve it, and return the worker's reward."""
    accepted = _post(client, requester_headers, prompt=prompt, max_length=max_length)
    assert accepted.status_code == 202, accepted.get_data(as_text=True)
    return _pop_and_submit(client, worker_headers, max_length=max_length)


class TestTheHoldCoversSettlement:
    def test_a_job_never_debits_more_than_the_hold_its_request_posted(
        self,
        app: Flask,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        request_headers: dict[str, str],
        settle_kudos: Callable[[], int],
    ) -> None:
        """The hold is reserved before a worker is chosen; the debit is known only after.

        The hold has to be an upper bound on that debit whatever prompt the request carried, since
        the hold is the only thing the requester was checked against.
        """
        agent = request_headers["Client-Agent"]
        requester = make_api_user(trusted=True, kudos=1_000_000)
        settle_kudos()

        for prompt in (SHORT_PROMPT, LONG_PROMPT):
            accepted = _post(client, _headers(requester.api_key, agent), prompt=prompt, max_length=HELD_MAX_LENGTH)
            assert accepted.status_code == 202, accepted.get_data(as_text=True)
            hold = _hold_amount(app, accepted.get_json()["id"])
            assert hold is not None, "a request past the free-tier ceiling posts a hold"

            reward = _pop_and_submit(client, request_headers, max_length=HELD_MAX_LENGTH)

            # The requester is debited the reward plus the per-job burn of 1.
            assert reward + 1 <= hold, (
                f"a {len(prompt) // 4}-token prompt settled for {reward} kudos against a hold of {hold}; "
                "the hold no longer covers what the job debits"
            )

    def test_a_request_the_requester_cannot_cover_is_refused_for_at_least_what_it_would_settle_for(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        request_headers: dict[str, str],
        settle_kudos: Callable[[], int],
    ) -> None:
        """The amount a refusal names is the hold, so it is bound by the same relation."""
        agent = request_headers["Client-Agent"]
        rich = make_api_user(trusted=True, kudos=1_000_000)
        poor = make_api_user(trusted=True, kudos=5)
        settle_kudos()

        refused = _post(client, _headers(poor.api_key, agent), prompt=LONG_PROMPT, max_length=HELD_MAX_LENGTH)
        assert refused.status_code == 403, refused.get_data(as_text=True)
        assert refused.get_json()["rc"] == "KudosUpfront"
        required = float(re.search(r"requires ([0-9.]+) kudos", refused.get_json()["message"]).group(1))

        reward = _settle_one(client, _headers(rich.api_key, agent), request_headers, prompt=LONG_PROMPT, max_length=HELD_MAX_LENGTH)

        assert reward + 1 <= required, (
            f"the refusal asked for {required} kudos but the same request settled for {reward}; "
            "a requester who had exactly what was asked could not have paid"
        )


class TestFreeAdmissionIsBoundedByTheOutputAllowance:
    def test_a_request_admitted_without_a_hold_does_not_pay_more_for_a_longer_prompt(
        self,
        app: Flask,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        request_headers: dict[str, str],
        settle_kudos: Callable[[], int],
    ) -> None:
        """The free tier is sized by the output allowance, which is the only operand its gate reads.

        A prompt the gate does not look at cannot be allowed to raise what the job pays out, or the
        tier has no bound at all: the requester's balance can go no lower than its floor, and the
        worker is credited in full. Either the gate refuses the longer prompt, or the longer prompt
        settles for no more than the shorter one.
        """
        agent = request_headers["Client-Agent"]
        requester = make_api_user(trusted=True, kudos=5)
        settle_kudos()
        headers = _headers(requester.api_key, agent)

        short = _post(client, headers, prompt=SHORT_PROMPT, max_length=FREE_TIER_MAX_LENGTH)
        assert short.status_code == 202, short.get_data(as_text=True)
        assert _hold_amount(app, short.get_json()["id"]) is None, "a 512-token request on a quiet queue needs no hold"
        short_reward = _pop_and_submit(client, request_headers, max_length=FREE_TIER_MAX_LENGTH)

        long = _post(client, headers, prompt=LONG_PROMPT, max_length=FREE_TIER_MAX_LENGTH)
        if long.status_code == 403:
            assert long.get_json()["rc"] == "KudosUpfront", long.get_data(as_text=True)
            return
        assert long.status_code == 202, long.get_data(as_text=True)
        assert _hold_amount(app, long.get_json()["id"]) is None
        long_reward = _pop_and_submit(client, request_headers, max_length=FREE_TIER_MAX_LENGTH)

        assert long_reward <= short_reward, (
            f"a requester with 5 kudos was admitted without a hold and the worker was paid {long_reward} "
            f"for a 12,000-token prompt against {short_reward} for a 1,000-token one"
        )


class TestTheQuoteTracksSettlement:
    def test_requests_that_settle_differently_are_quoted_differently(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        request_headers: dict[str, str],
        settle_kudos: Callable[[], int],
    ) -> None:
        """A quote is cached and reused for requests that hash alike.

        That is sound only while every request the cache treats as the same one settles the same.
        Two requests with identical params and different prompts must therefore either settle alike
        or be quoted apart; a tariff that prices the prompt at settlement has to price it in the
        quote, and in the quote's cache key, too.
        """
        agent = request_headers["Client-Agent"]
        requester = make_api_user(trusted=True, kudos=1_000_000)
        settle_kudos()
        headers = _headers(requester.api_key, agent)

        quotes = []
        for prompt in (SHORT_PROMPT, LONG_PROMPT):
            quoted = _post(client, headers, prompt=prompt, max_length=FREE_TIER_MAX_LENGTH, dry_run=True)
            assert quoted.status_code == 200, quoted.get_data(as_text=True)
            quotes.append(quoted.get_json()["kudos"])

        rewards = [
            _settle_one(client, headers, request_headers, prompt=prompt, max_length=FREE_TIER_MAX_LENGTH)
            for prompt in (SHORT_PROMPT, LONG_PROMPT)
        ]

        if rewards[0] != rewards[1]:
            assert quotes[0] != quotes[1], (
                f"the two prompts settled for {rewards[0]} and {rewards[1]} but were both quoted {quotes[0]}; "
                "the quote does not see the operand settlement prices"
            )

    def test_a_job_never_settles_above_its_quote(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        request_headers: dict[str, str],
        settle_kudos: Callable[[], int],
    ) -> None:
        """The dry run is the most a request is said to cost; one of its jobs cannot cost more."""
        agent = request_headers["Client-Agent"]
        requester = make_api_user(trusted=True, kudos=1_000_000)
        settle_kudos()
        headers = _headers(requester.api_key, agent)

        for prompt in (SHORT_PROMPT, LONG_PROMPT):
            quoted = _post(client, headers, prompt=prompt, max_length=FREE_TIER_MAX_LENGTH, dry_run=True)
            assert quoted.status_code == 200, quoted.get_data(as_text=True)
            reward = _settle_one(client, headers, request_headers, prompt=prompt, max_length=FREE_TIER_MAX_LENGTH)

            assert reward <= quoted.get_json()["kudos"], (
                f"a {len(prompt) // 4}-token prompt was quoted {quoted.get_json()['kudos']} and settled for {reward}"
            )

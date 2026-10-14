# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bounds the text admission gates place on settlement, over stubs and without a database.

``tests/integration/test_text_kudos_bounds.py`` states these relations through the API, where the
upfront hold can be observed. Those cases skip when no Postgres is available, so this module states
the two relations that need no database against the same methods, bound to lightweight stubs as the
other text unit suites do. A run of this module alone is enough to show a tariff change that reprices
settlement without carrying the gates with it.

- A request the free-tier gate admits without a hold pays its worker no more for a long prompt than
  for a short one, or the gate refuses the long prompt.
- Two requests the quote cache would treat as the same request settle the same, or they are keyed
  apart.
- A job never settles above its quote.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace

import pytest

from horde.apis.v2.base import GenerateTemplate
from horde.apis.v2.kobold import TextAsyncGenerate
from horde.classes.kobold import processing_generation as text_procgen_module
from horde.classes.kobold import waiting_prompt as text_wp_module
from horde.classes.kobold.processing_generation import TextProcessingGeneration
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt

pytestmark = pytest.mark.unit

KNOWN_MODEL = "a-known-13B"
VALIDATED_AGENT = "AI Horde Worker:23:https://example.invalid"
MAX_CONTEXT_LENGTH = 16384

SHORT_PROMPT = "x" * 4000
"""Estimated at 1,000 tokens: longer than any output allowance used here."""
LONG_PROMPT = "x" * 48_000
"""Estimated at 12,000 tokens: a prompt the context multiplier prices near its cap."""
FULL_REPLY = "x" * 6000
"""1,500 tokens, so the returned text always reaches the requested ``max_length``."""

FREE_TIER_MAX_LENGTH = 512
"""The largest ``max_length`` the free-tier gate admits with no hold when the queue is quiet."""
QUIET_QUEUE = {"queued_text_requests": 0}


@pytest.fixture(autouse=True)
def _tariff_environment(monkeypatch):
    """One known 13B model, a validated backend, and none of the environment switches."""
    for module in (text_procgen_module, text_wp_module):
        monkeypatch.setattr(module.model_reference, "is_known_text_model", lambda model_name: model_name == KNOWN_MODEL)
        monkeypatch.setattr(module.model_reference, "get_text_model_multiplier", lambda model_name: 13.0)
    monkeypatch.setattr(text_procgen_module, "is_backed_validated", lambda agent: agent == VALIDATED_AGENT)
    for variable in ("HORDE_REQUIRE_MATCHED_TARGETING", "HORDE_UPFRONT_KUDOS_ON_WORKERLIST", "HORDE_TEXT_FREE_PROMPT_TOKENS"):
        monkeypatch.delenv(variable, raising=False)


def _request(prompt: str, *, max_length: int) -> SimpleNamespace:
    """Build a text waiting prompt as the gate and the quote see it, bound to the real methods."""
    wp = SimpleNamespace(
        prompt=prompt,
        max_length=max_length,
        things=max_length,
        max_context_length=MAX_CONTEXT_LENGTH,
        slow_workers=True,
        workers=[],
        models=[SimpleNamespace(model=KNOWN_MODEL)],
        n=1,
        kudos=0.0,
    )
    for name in ("require_upfront_kudos", "calculate_kudos", "_quote_model_multiplier"):
        method = getattr(TextWaitingPrompt, name, None)
        if method is not None:
            setattr(wp, name, MethodType(method, wp))
    return wp


def _settle(wp: SimpleNamespace, generation: str = FULL_REPLY) -> float:
    """Return what a trusted worker on a validated backend is paid for serving the request."""
    procgen = SimpleNamespace(
        generation=generation,
        model=KNOWN_MODEL,
        wp=wp,
        worker=SimpleNamespace(bridge_agent=VALIDATED_AGENT, user=SimpleNamespace(trusted=True)),
    )
    procgen.get_things_count = MethodType(TextProcessingGeneration.get_things_count, procgen)
    procgen.get_gen_kudos = MethodType(TextProcessingGeneration.get_gen_kudos, procgen)
    return procgen.get_gen_kudos()


def _cache_key(prompt: str, *, max_length: int) -> str:
    """Return the dry-run cache key for a request, as the pre-validate lookup computes it."""
    request_args = SimpleNamespace(
        models=[KNOWN_MODEL],
        style=None,
        extra_source_images=None,
        source_processing="img2img",
        source_image=None,
        source_mask=None,
    )
    generate_request = SimpleNamespace(
        args=request_args,
        params={"n": 1, "max_length": max_length, "max_context_length": MAX_CONTEXT_LENGTH},
        models=[KNOWN_MODEL],
        style_kudos=False,
        prompt=prompt,
    )
    generate_request.get_extra_source_images_count = lambda: GenerateTemplate.get_extra_source_images_count(generate_request)
    return TextAsyncGenerate.get_hashed_params_dict(generate_request)


class TestFreeAdmissionIsBoundedByTheOutputAllowance:
    def test_a_request_admitted_without_a_hold_does_not_pay_more_for_a_longer_prompt(self):
        """The free tier is sized by what its gate reads; what it does not read must not raise the payout."""
        short = _request(SHORT_PROMPT, max_length=FREE_TIER_MAX_LENGTH)
        long = _request(LONG_PROMPT, max_length=FREE_TIER_MAX_LENGTH)

        short_needs_hold, _, _ = short.require_upfront_kudos(QUIET_QUEUE, 0)
        long_needs_hold, _, _ = long.require_upfront_kudos(QUIET_QUEUE, 0)

        assert not short_needs_hold, "a 1,000-token prompt at 512 tokens on a quiet queue runs free"
        if long_needs_hold:
            return
        short_reward = _settle(short)
        long_reward = _settle(long)
        assert long_reward <= short_reward, (
            f"the gate admitted a 12,000-token prompt without a hold and the worker was paid {long_reward} "
            f"for it against {short_reward} for a 1,000-token one"
        )


class TestTheQuoteTracksSettlement:
    def test_requests_the_cache_keys_alike_settle_alike(self):
        """A cached quote answers every request with the same key, so those requests must settle the same."""
        short = _request(SHORT_PROMPT, max_length=FREE_TIER_MAX_LENGTH)
        long = _request(LONG_PROMPT, max_length=FREE_TIER_MAX_LENGTH)

        rewards = (_settle(short), _settle(long))
        keys = (_cache_key(SHORT_PROMPT, max_length=FREE_TIER_MAX_LENGTH), _cache_key(LONG_PROMPT, max_length=FREE_TIER_MAX_LENGTH))

        if rewards[0] != rewards[1]:
            assert keys[0] != keys[1], (
                f"the two prompts settle for {rewards[0]} and {rewards[1]} but share a quote cache key; "
                "a quote cached for one would be served for the other"
            )

    @pytest.mark.parametrize("prompt", [SHORT_PROMPT, LONG_PROMPT], ids=["short", "long"])
    def test_a_job_never_settles_above_its_quote(self, prompt: str):
        wp = _request(prompt, max_length=FREE_TIER_MAX_LENGTH)

        quote = wp.calculate_kudos()
        reward = _settle(wp)

        assert reward <= quote, f"quoted {quote}, settled {reward}"

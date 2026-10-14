# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pure parts of the text pricing load scenarios in ``tests/stress/locustsuite/users/text.py``."""

from __future__ import annotations

import math
import os

import pytest

os.environ.setdefault("LOCUST_SKIP_MONKEY_PATCH", "1")

from tests.stress.locustsuite.users.text import (  # noqa: E402
    PRICING_LONG_PROMPT_TOKENS,
    PRICING_MEDIUM_PROMPT_TOKENS,
    _classify_pricing_response,
    _pricing_prompt,
    _simulated_generation,
)

pytestmark = pytest.mark.unit


class TestPricingPrompt:
    @pytest.mark.parametrize("tokens", [100, PRICING_MEDIUM_PROMPT_TOKENS, PRICING_LONG_PROMPT_TOKENS])
    def test_the_server_estimates_the_prompt_near_the_requested_size(self, tokens: int):
        prompt = _pricing_prompt(tokens)
        # The filler is 80 characters, so the size is met to within one filler plus the unique suffix.
        assert abs(math.ceil(len(prompt) / 4) - tokens) <= 25

    def test_two_prompts_differ_so_neither_is_answered_from_the_others_cache(self):
        assert _pricing_prompt(100) != _pricing_prompt(100)

    def test_the_scenario_sizes_straddle_the_free_tier_reference(self):
        # The medium prompt is past the 4,096-token reference with budget left; the long one is past the
        # point where prompt processing alone exceeds it (about 7,000 tokens with the provisional rates).
        assert 4_096 < PRICING_MEDIUM_PROMPT_TOKENS < 7_000 < PRICING_LONG_PROMPT_TOKENS


class TestClassifyPricingResponse:
    def test_a_queued_request_is_accepted(self):
        assert _classify_pricing_response(202, {"id": "abc", "kudos": 12}) == "accepted"

    def test_a_dry_run_is_quoted(self):
        assert _classify_pricing_response(200, {"kudos": 765, "resolved": {}}) == "quoted"

    @pytest.mark.parametrize("status", [400, 403])
    def test_an_upfront_refusal_is_gated_whatever_status_the_deployment_uses(self, status: int):
        assert _classify_pricing_response(status, {"rc": "KudosUpfront", "message": "..."}) == "gated"

    def test_a_shared_key_shortfall_is_gated_too(self):
        assert _classify_pricing_response(403, {"rc": "SharedKeyInsufficientKudos"}) == "gated"

    def test_rate_limiting_is_throttled(self):
        assert _classify_pricing_response(429, None) == "throttled"

    @pytest.mark.parametrize(("status", "body"), [(500, None), (400, {"rc": "InvalidPromptSize"}), (200, {"id": "x"})])
    def test_anything_else_is_unexpected(self, status: int, body: dict | None):
        assert _classify_pricing_response(status, body) == "unexpected"


class TestSimulatedGeneration:
    def test_the_mix_includes_empty_long_and_ordinary_replies(self):
        replies = {_simulated_generation() for _ in range(2000)}
        assert "" in replies
        assert any(len(reply) > 1000 for reply in replies)
        assert any(0 < len(reply) < 200 for reply in replies)

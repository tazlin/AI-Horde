# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The text tariff in ``horde.classes.kobold.pricing``, exercised without the application.

Expected values are worked by hand from the published formula rather than read back from the
module, so a change to the arithmetic shows up as a changed price and not as a rewritten test.
"""

from __future__ import annotations

import math

import pytest

from horde.classes.kobold import pricing
from horde.classes.kobold.pricing import (
    CONTEXT_MULTIPLIER_CAP,
    DEFAULT_FREE_PROMPT_TOKENS,
    FREE_PROMPT_TOKENS_ENV,
    MATCHED_TARGETING_ENV,
    MIN_OUTPUT_TOKENS,
    MIN_PRICED_TOKENS,
    billing_prompt_tokens,
    calculate_text_generation_reward,
    context_multiplier,
    count_output_tokens,
    estimate_billing_prompt_tokens,
    free_output_allowance,
    matched_targeting_applies,
    quote_text_generation,
    reference_prompt_tokens,
    retained_prompt_tokens,
)

pytestmark = pytest.mark.unit

THIRTEEN_BILLION = 13.0
"""The reference size at which the parameter bonus is exactly one."""


def _known_reward(output_tokens: int, prompt_tokens: int, model_billions: float = THIRTEEN_BILLION, **overrides) -> float:
    return calculate_text_generation_reward(
        output_tokens=output_tokens,
        prompt_tokens=prompt_tokens,
        model_multiplier=model_billions,
        owner_trusted=overrides.get("owner_trusted", True),
        backend_validated=overrides.get("backend_validated", True),
        matched_targeting=overrides.get("matched_targeting", False),
    )


class TestPromptEstimate:
    def test_missing_and_empty_prompts_estimate_to_zero(self):
        assert estimate_billing_prompt_tokens(None) == 0
        assert estimate_billing_prompt_tokens("") == 0

    def test_characters_are_divided_by_four_rounding_up(self):
        assert estimate_billing_prompt_tokens("x" * 4096) == 1024
        assert estimate_billing_prompt_tokens("x" * 4097) == 1025
        assert estimate_billing_prompt_tokens("abc") == 1

    def test_the_estimate_counts_code_points_not_bytes(self):
        assert estimate_billing_prompt_tokens("é" * 8) == 2


class TestRetainedPromptTokens:
    """The prompt operand is what the requested context keeps once ``max_length`` is reserved."""

    def test_a_prompt_that_fits_is_priced_in_full(self):
        assert retained_prompt_tokens(1000, max_context_length=2048, max_length=512) == 1000
        assert retained_prompt_tokens(1536, max_context_length=2048, max_length=512) == 1536

    def test_a_prompt_that_does_not_fit_is_capped_at_what_the_context_keeps(self):
        assert retained_prompt_tokens(10_000, max_context_length=2048, max_length=512) == 1536
        assert retained_prompt_tokens(20_000, max_context_length=16384, max_length=4096) == 12288

    def test_the_backend_keeps_at_least_a_fifth_of_the_context_for_the_prompt(self):
        # max_length past four fifths of the context is lowered by the backend, not taken from the prompt.
        assert retained_prompt_tokens(10_000, max_context_length=2048, max_length=4096) == 410
        assert retained_prompt_tokens(10_000, max_context_length=80, max_length=4096) == 16

    def test_the_cap_only_ever_lowers_the_estimate(self):
        assert retained_prompt_tokens(0, max_context_length=2048, max_length=512) == 0
        assert retained_prompt_tokens(-5, max_context_length=2048, max_length=512) == 0

    def test_billing_prompt_tokens_composes_the_estimate_and_the_cap(self):
        assert billing_prompt_tokens("x" * 40_000, max_context_length=2048, max_length=512) == 1536
        assert billing_prompt_tokens("x" * 4000, max_context_length=2048, max_length=512) == 1000
        assert billing_prompt_tokens(None, max_context_length=2048, max_length=512) == 0


class TestOutputCount:
    def test_no_generation_counts_as_nothing(self):
        assert count_output_tokens(None, 512) == 0

    def test_a_short_or_empty_generation_is_floored(self):
        assert count_output_tokens("", 512) == MIN_PRICED_TOKENS
        assert count_output_tokens("ok", 512) == MIN_PRICED_TOKENS

    def test_the_count_is_capped_by_the_allowance(self):
        assert count_output_tokens("x" * 2048, 512) == 512
        assert count_output_tokens("x" * 4096, 512) == 512
        assert count_output_tokens("x" * 2048, 1024) == 512

    def test_an_allowance_under_the_floor_wins(self):
        assert count_output_tokens("", 16) == 16


class TestContextMultiplier:
    def test_the_base_prompt_prices_at_two_point_two(self):
        assert context_multiplier(1024) == pytest.approx(2.2)

    def test_doubling_the_prompt_multiplies_the_variable_term_by_two_point_two(self):
        assert context_multiplier(2048) == pytest.approx(1.2 + 2.2**1)
        assert context_multiplier(4096) == pytest.approx(1.2 + 2.2**2)

    def test_tiny_prompts_share_a_floor(self):
        assert context_multiplier(0) == context_multiplier(MIN_PRICED_TOKENS)
        assert context_multiplier(MIN_PRICED_TOKENS) == pytest.approx(1.2 + 2.2 ** math.log2(20 / 1024))

    def test_the_cap_is_reached_near_nineteen_thousand_six_hundred_tokens(self):
        assert context_multiplier(19_646) < CONTEXT_MULTIPLIER_CAP
        assert context_multiplier(19_647) == CONTEXT_MULTIPLIER_CAP
        assert context_multiplier(98_304) == CONTEXT_MULTIPLIER_CAP


class TestReward:
    """The settlement kernel under each branch, with the prompt no longer capped by the output allowance."""

    @pytest.mark.parametrize(
        ("prompt_tokens", "expected"),
        [(256, 74.90), (1024, 117.15), (10_000, 775.26), (50_000, 1597.44)],
    )
    def test_the_reported_prompt_sizes_price_apart(self, prompt_tokens: int, expected: float):
        # 512 * 13 / 125 * multiplier, rounded to two decimals.
        assert _known_reward(512, prompt_tokens) == expected

    def test_the_prompt_operand_is_independent_of_the_output_operand(self):
        # 20,000 tokens of prompt against a 20-token output still pays the full context multiplier.
        assert _known_reward(20, 50_000) == 62.40
        assert _known_reward(20, 20) == 2.52

    def test_the_reward_scales_linearly_with_output_and_rounds_once(self):
        # 1024 * 13 / 125 * 2.2 = 234.2912, rounded after the multiplication rather than per half.
        assert _known_reward(1024, 1024) == 234.29
        assert _known_reward(1024, 1024) == pytest.approx(2 * _known_reward(512, 1024), abs=0.02)

    def test_the_parameter_bonus_starts_above_thirteen_billion(self):
        assert _known_reward(512, 1024, 7) == 63.08
        assert _known_reward(512, 1024, 13) == 117.15
        assert _known_reward(512, 1024, 70) == round(512 * (70 / 13) ** 0.2 * 70 / 125 * 2.2, 2)

    def test_an_unvalidated_backend_keeps_thirty_percent(self):
        assert _known_reward(512, 1024, backend_validated=False) == 35.14

    def test_an_unknown_model_under_a_trusted_owner_is_paid_as_two_point_seven_billion_unrounded(self):
        reward = calculate_text_generation_reward(
            output_tokens=512,
            prompt_tokens=1024,
            model_multiplier=None,
            owner_trusted=True,
            backend_validated=True,
            matched_targeting=False,
        )
        assert reward == 512 * 2.2 * (2.7 / 100)

    def test_an_unknown_model_under_an_untrusted_owner_is_paid_the_multiplier_alone(self):
        reward = calculate_text_generation_reward(
            output_tokens=512,
            prompt_tokens=1024,
            model_multiplier=None,
            owner_trusted=False,
            backend_validated=False,
            matched_targeting=False,
        )
        assert reward == pytest.approx(2.2)

    def test_matched_targeting_pays_a_flat_tenth(self):
        assert _known_reward(512, 50_000, matched_targeting=True) == 0.1

    def test_no_output_pays_nothing_on_a_known_model(self):
        # A cancelled job has no generation; its output operand is zero.
        assert _known_reward(0, 50_000) == 0.0
        assert _known_reward(0, 50_000, backend_validated=False) == 0.0


class TestQuote:
    def test_the_quote_is_the_kernel_at_the_full_allowance_on_a_validated_trusted_worker(self):
        quote = quote_text_generation(output_allowance=512, prompt_tokens=2048, model_multiplier=13, matched_targeting=False)
        assert quote == _known_reward(512, 2048)
        assert quote == 181.04

    def test_an_unknown_model_quotes_the_trusted_unknown_branch(self):
        quote = quote_text_generation(output_allowance=100, prompt_tokens=20, model_multiplier=None, matched_targeting=False)
        assert quote == 100 * context_multiplier(20) * (2.7 / 100)

    def test_matched_targeting_quotes_the_flat_tenth(self):
        assert quote_text_generation(output_allowance=512, prompt_tokens=2048, model_multiplier=13, matched_targeting=True) == 0.1


class TestMatchedTargeting:
    def test_requires_both_the_flag_and_a_worker_list(self, monkeypatch):
        monkeypatch.delenv(MATCHED_TARGETING_ENV, raising=False)
        assert matched_targeting_applies(1) is False
        monkeypatch.setenv(MATCHED_TARGETING_ENV, "1")
        assert matched_targeting_applies(0) is False
        assert matched_targeting_applies(1) is True


class TestReferencePrompt:
    def test_defaults_to_four_thousand_ninety_six(self, monkeypatch):
        monkeypatch.delenv(FREE_PROMPT_TOKENS_ENV, raising=False)
        assert reference_prompt_tokens() == DEFAULT_FREE_PROMPT_TOKENS == 4096

    def test_reads_a_positive_integer_from_the_environment(self, monkeypatch):
        monkeypatch.setenv(FREE_PROMPT_TOKENS_ENV, "8192")
        assert reference_prompt_tokens() == 8192

    @pytest.mark.parametrize("configured", ["", "lots", "0", "-5"])
    def test_falls_back_on_an_unusable_value(self, monkeypatch, configured: str):
        warnings: list[str] = []
        monkeypatch.setattr(pricing.logger, "warning", lambda message: warnings.append(message))
        monkeypatch.setenv(FREE_PROMPT_TOKENS_ENV, configured)
        assert reference_prompt_tokens() == DEFAULT_FREE_PROMPT_TOKENS
        assert len(warnings) == 1


class TestFreeOutputAllowance:
    def test_a_prompt_at_or_under_the_reference_keeps_the_whole_allowance(self):
        assert free_output_allowance(prompt_tokens=0, free_output_tokens=512, reference_prompt_tokens=4096) == 512
        assert free_output_allowance(prompt_tokens=4096, free_output_tokens=512, reference_prompt_tokens=4096) == 512
        assert free_output_allowance(prompt_tokens=4096, free_output_tokens=256, reference_prompt_tokens=4096) == 256

    @pytest.mark.parametrize(
        ("prompt_tokens", "free_output_tokens", "expected"),
        [
            # floor(T * M(4096) / M(P)) with M(4096) = 6.04.
            (4097, 512, 511),
            (8192, 512, 261),
            (10_000, 512, 212),
            (50_000, 512, 103),
            (50_000, 256, 51),
        ],
    )
    def test_a_longer_prompt_spends_the_budget_on_the_multiplier(self, prompt_tokens: int, free_output_tokens: int, expected: int):
        allowance = free_output_allowance(
            prompt_tokens=prompt_tokens,
            free_output_tokens=free_output_tokens,
            reference_prompt_tokens=4096,
        )
        assert allowance == expected
        # The allowance fits the budget and the next token would not.
        budget = free_output_tokens * context_multiplier(4096)
        assert allowance * context_multiplier(prompt_tokens) <= budget
        assert (allowance + 1) * context_multiplier(prompt_tokens) > budget

    def test_the_allowance_never_drops_below_the_api_minimum(self):
        assert free_output_allowance(prompt_tokens=100_000, free_output_tokens=16, reference_prompt_tokens=20) == MIN_OUTPUT_TOKENS

    def test_the_reference_prompt_moves_the_threshold(self):
        at_two_k = free_output_allowance(prompt_tokens=8192, free_output_tokens=512, reference_prompt_tokens=2048)
        at_eight_k = free_output_allowance(prompt_tokens=8192, free_output_tokens=512, reference_prompt_tokens=8192)
        assert at_two_k < 261 < at_eight_k == 512

    def test_a_prompt_the_context_truncates_is_gated_on_what_the_worker_processes(self):
        # 50,000 tokens into a 2,048 context: the worker keeps at most 1,536, under the reference.
        assert (
            free_output_allowance(prompt_tokens=50_000, free_output_tokens=512, reference_prompt_tokens=4096, max_context_length=2048)
            == 512
        )
        # The same prompt into a 16K context is processed at the cap of the curve.
        assert (
            free_output_allowance(prompt_tokens=50_000, free_output_tokens=512, reference_prompt_tokens=4096, max_context_length=16384)
            == 126
        )

    def test_a_context_that_does_not_bind_leaves_the_allowance_as_without_it(self):
        with_context = free_output_allowance(
            prompt_tokens=5000, free_output_tokens=512, reference_prompt_tokens=4096, max_context_length=16384
        )
        without = free_output_allowance(prompt_tokens=5000, free_output_tokens=512, reference_prompt_tokens=4096)
        assert with_context == without == 425

    def test_the_allowance_fits_the_budget_with_the_prompt_a_downgrade_to_it_leaves(self):
        # 5,000 tokens into a 5,120 context: 512 of output keeps 4,608 of prompt, which does not fit;
        # lowering the allowance lets more prompt in, and the gate settles where both fit together.
        allowance = free_output_allowance(prompt_tokens=5000, free_output_tokens=512, reference_prompt_tokens=4096, max_context_length=5120)
        budget = 512 * context_multiplier(4096)
        assert allowance == 453
        processed = retained_prompt_tokens(5000, max_context_length=5120, max_length=allowance)
        assert allowance * context_multiplier(processed) <= budget
        # The fixed allowance would have been granted on a smaller prompt than settlement sees.
        assert 512 * context_multiplier(retained_prompt_tokens(5000, max_context_length=5120, max_length=512)) > budget

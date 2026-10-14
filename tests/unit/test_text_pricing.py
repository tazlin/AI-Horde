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
    DEFAULT_FREE_PROMPT_TOKENS,
    FREE_PROMPT_TOKENS_ENV,
    LEGACY_CONTEXT_MULTIPLIER_CAP,
    MATCHED_TARGETING_ENV,
    MIN_PRICED_TOKENS,
    PRICED_PROMPT_TOKENS_CEILING,
    PROVISIONAL_RATES,
    WORK_UNIT_SCALE,
    TextWorkRates,
    billing_prompt_tokens,
    calculate_text_generation_reward,
    count_output_tokens,
    decode_work,
    estimate_billing_prompt_tokens,
    free_output_allowance,
    legacy_context_multiplier,
    matched_targeting_applies,
    modelled_work,
    prefill_work,
    quote_text_generation,
    reference_prompt_tokens,
    retained_prompt_tokens,
)

pytestmark = pytest.mark.unit

THIRTEEN_BILLION = 13.0
"""The reference size at which the parameter bonus is exactly one."""

ROUND_RATES = TextWorkRates(input_rate=0.2, input_quadratic_rate=1e-5, context_decode_rate=2.5e-4, provenance="hand-picked for arithmetic")


def _known_reward(output_tokens: int | None, prompt_tokens: int, model_billions: float = THIRTEEN_BILLION, **overrides) -> float:
    return calculate_text_generation_reward(
        output_tokens=output_tokens,
        prompt_tokens=prompt_tokens,
        model_multiplier=model_billions,
        owner_trusted=overrides.get("owner_trusted", True),
        backend_validated=overrides.get("backend_validated", True),
        matched_targeting=overrides.get("matched_targeting", False),
        empty_output=overrides.get("empty_output", False),
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


class TestLegacyContextMultiplier:
    """The curve the tariff grew out of, still paid on one branch and used to pin the new level."""

    def test_the_base_prompt_prices_at_two_point_two(self):
        assert legacy_context_multiplier(1024) == pytest.approx(2.2)

    def test_tiny_prompts_share_a_floor(self):
        assert legacy_context_multiplier(0) == legacy_context_multiplier(MIN_PRICED_TOKENS)
        assert legacy_context_multiplier(MIN_PRICED_TOKENS) == pytest.approx(1.2 + 2.2 ** math.log2(20 / 1024))

    def test_the_cap_is_reached_near_nineteen_thousand_six_hundred_tokens(self):
        assert legacy_context_multiplier(19_646) < LEGACY_CONTEXT_MULTIPLIER_CAP
        assert legacy_context_multiplier(19_647) == LEGACY_CONTEXT_MULTIPLIER_CAP


class TestWorkModel:
    """work(P, O) = input_rate * P + input_quadratic_rate * P^2 + O * (1 + context_decode_rate * P)."""

    def test_prompt_processing_is_linear_plus_quadratic_in_the_prompt(self):
        # 0.2 * 1000 + 1e-5 * 1000^2 = 200 + 10.
        assert prefill_work(1000, ROUND_RATES) == pytest.approx(210.0)
        assert prefill_work(0, ROUND_RATES) == 0.0

    def test_each_generated_token_costs_more_over_a_longer_context(self):
        # 100 tokens over 1000 of context: 100 * (1 + 0.25) = 125.
        assert decode_work(1000, 100, ROUND_RATES) == pytest.approx(125.0)
        assert decode_work(0, 100, ROUND_RATES) == pytest.approx(100.0)

    def test_the_whole_job_is_the_sum(self):
        assert modelled_work(1000, 100, ROUND_RATES) == pytest.approx(335.0)

    def test_prompts_past_the_ceiling_price_alike(self):
        assert prefill_work(PRICED_PROMPT_TOKENS_CEILING + 1) == prefill_work(PRICED_PROMPT_TOKENS_CEILING)
        assert prefill_work(1_000_000) == prefill_work(PRICED_PROMPT_TOKENS_CEILING)
        assert decode_work(1_000_000, 10) == decode_work(PRICED_PROMPT_TOKENS_CEILING, 10)

    def test_a_negative_estimate_is_treated_as_empty(self):
        assert prefill_work(-5) == 0.0

    def test_the_provisional_rates_record_their_provenance(self):
        assert "KoboldCpp" in PROVISIONAL_RATES.provenance
        assert PROVISIONAL_RATES.input_rate > 0
        assert PROVISIONAL_RATES.input_quadratic_rate > 0
        assert PROVISIONAL_RATES.context_decode_rate > 0


class TestLevelContinuity:
    """The reference job prices exactly as it did under the legacy curve; everything else is relative to it."""

    def test_the_scale_pins_the_reference_job_to_the_legacy_curve(self):
        assert WORK_UNIT_SCALE * modelled_work(1024, 512) == pytest.approx(512 * 2.2)

    def test_a_512_token_job_on_a_1024_token_prompt_on_13b_still_pays_117_15(self):
        # Legacy: round(512 * 13 / 125 * 2.2, 2) = 117.15.
        assert _known_reward(512, 1024) == 117.15

    def test_the_same_job_on_an_unknown_model_under_a_trusted_owner_is_unchanged_too(self):
        reward = calculate_text_generation_reward(
            output_tokens=512,
            prompt_tokens=1024,
            model_multiplier=None,
            owner_trusted=True,
            backend_validated=True,
            matched_targeting=False,
        )
        assert reward == pytest.approx(512 * 2.2 * (2.7 / 100))


class TestReward:
    """The settlement kernel under each branch."""

    @pytest.mark.parametrize(
        ("prompt_tokens", "expected"),
        [(1024, 117.15), (2048, 168.90), (4096, 289.53), (10_000, 765.07), (16_384, 1492.79), (50_000, 1492.79)],
    )
    def test_a_512_token_job_against_prompt_size(self, prompt_tokens: int, expected: float):
        assert _known_reward(512, prompt_tokens) == expected

    def test_the_prompt_is_paid_however_little_comes_back(self):
        # 20 tokens on a 10,000-token prompt: prefill 1,746.6 + 1,960.0 units plus 20 * 3.52 generation.
        assert _known_reward(20, 10_000) == 524.51
        assert _known_reward(64, 10_000) > _known_reward(20, 10_000)

    def test_an_empty_submission_forfeits_the_prompt_term(self):
        # Only the output floor over the prompt's context is paid: 20 * (1 + 0.2521 * 10,000 / 10,000)...
        assert _known_reward(20, 10_000, empty_output=True) == 9.78
        assert _known_reward(20, 10_000, empty_output=True) < _known_reward(20, 10_000) / 50

    def test_no_submission_pays_nothing_on_a_known_model(self):
        assert _known_reward(None, 50_000) == 0.0
        assert _known_reward(None, 50_000, backend_validated=False) == 0.0

    def test_long_outputs_on_short_prompts_are_cheaper_than_under_the_legacy_curve(self):
        # Legacy priced 1,024 tokens on a 1,024-token prompt at 234.29; generation does not grow faster than linearly.
        assert _known_reward(1024, 1024) == 206.60

    def test_the_parameter_bonus_starts_above_thirteen_billion(self):
        assert _known_reward(512, 1024, 7) == 63.08
        assert _known_reward(512, 1024, 70) == round(512 * 2.2 * (70 / 13) ** 0.2 * 70 / 125, 2)

    def test_an_unvalidated_backend_keeps_thirty_percent(self):
        assert _known_reward(512, 1024, backend_validated=False) == 35.14

    def test_an_unknown_model_under_an_untrusted_owner_is_paid_the_legacy_multiplier_alone(self):
        flat = calculate_text_generation_reward(
            output_tokens=None,
            prompt_tokens=1024,
            model_multiplier=None,
            owner_trusted=False,
            backend_validated=False,
            matched_targeting=False,
        )
        assert flat == pytest.approx(2.2)

    def test_matched_targeting_pays_a_flat_tenth(self):
        assert _known_reward(512, 50_000, matched_targeting=True) == 0.1


class TestQuote:
    def test_the_quote_is_the_kernel_at_the_full_allowance_on_a_validated_trusted_worker(self):
        quote = quote_text_generation(output_allowance=512, prompt_tokens=10_000, model_multiplier=13, matched_targeting=False)
        assert quote == _known_reward(512, 10_000) == 765.07

    def test_an_unknown_model_quotes_the_trusted_unknown_branch(self):
        quote = quote_text_generation(output_allowance=100, prompt_tokens=0, model_multiplier=None, matched_targeting=False)
        assert quote == pytest.approx(WORK_UNIT_SCALE * 100 * (2.7 / 100))

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
    """The free budget is the work of the reference prompt with the full allowance; a longer prompt spends it."""

    def test_a_prompt_at_or_under_the_reference_keeps_the_whole_allowance(self):
        assert free_output_allowance(prompt_tokens=0, free_output_tokens=512, reference_prompt_tokens=4096) == 512
        assert free_output_allowance(prompt_tokens=4096, free_output_tokens=512, reference_prompt_tokens=4096) == 512
        assert free_output_allowance(prompt_tokens=4096, free_output_tokens=256, reference_prompt_tokens=4096) == 256

    @pytest.mark.parametrize(
        ("prompt_tokens", "free_output_tokens", "expected"),
        [(4097, 512, 511), (5000, 512, 319), (6000, 512, 131), (5000, 256, 89)],
    )
    def test_a_longer_prompt_keeps_what_the_budget_still_buys(self, prompt_tokens: int, free_output_tokens: int, expected: int):
        allowance = free_output_allowance(prompt_tokens=prompt_tokens, free_output_tokens=free_output_tokens, reference_prompt_tokens=4096)
        assert allowance == expected
        budget = modelled_work(4096, free_output_tokens)
        assert modelled_work(prompt_tokens, allowance) <= budget
        assert modelled_work(prompt_tokens, allowance + 1) > budget

    @pytest.mark.parametrize(("prompt_tokens", "free_output_tokens"), [(7000, 512), (10_000, 512), (6000, 256), (50_000, 512)])
    def test_a_prompt_whose_processing_alone_exceeds_the_budget_has_no_free_allowance(self, prompt_tokens: int, free_output_tokens: int):
        assert free_output_allowance(prompt_tokens=prompt_tokens, free_output_tokens=free_output_tokens, reference_prompt_tokens=4096) == 0

    def test_an_allowance_under_the_api_minimum_is_no_allowance(self):
        # Just under the budget: the remainder buys fewer than 16 tokens, which no request can ask for.
        assert free_output_allowance(prompt_tokens=6690, free_output_tokens=512, reference_prompt_tokens=4096) == 0

    def test_the_reference_prompt_moves_the_threshold(self):
        at_four_k = free_output_allowance(prompt_tokens=8192, free_output_tokens=512, reference_prompt_tokens=4096)
        at_eight_k = free_output_allowance(prompt_tokens=8192, free_output_tokens=512, reference_prompt_tokens=8192)
        assert at_four_k == 0
        assert at_eight_k == 512

    def test_a_prompt_the_context_truncates_is_gated_on_what_the_worker_processes(self):
        # 50,000 tokens into a 2,048 context: the worker keeps at most 1,536, under the reference.
        assert (
            free_output_allowance(prompt_tokens=50_000, free_output_tokens=512, reference_prompt_tokens=4096, max_context_length=2048)
            == 512
        )
        # The same prompt into a 16K context is processed past the budget.
        assert (
            free_output_allowance(prompt_tokens=50_000, free_output_tokens=512, reference_prompt_tokens=4096, max_context_length=16384) == 0
        )

    def test_a_context_that_does_not_bind_leaves_the_allowance_as_without_it(self):
        with_context = free_output_allowance(
            prompt_tokens=5000, free_output_tokens=512, reference_prompt_tokens=4096, max_context_length=16384
        )
        without = free_output_allowance(prompt_tokens=5000, free_output_tokens=512, reference_prompt_tokens=4096)
        assert with_context == without == 319

    def test_the_allowance_fits_the_budget_with_the_prompt_a_downgrade_to_it_leaves(self):
        # 5,000 tokens into a 5,120 context: 512 of output keeps 4,608 of prompt, which does not fit;
        # lowering the allowance lets more prompt in, and the gate settles where both fit together.
        allowance = free_output_allowance(prompt_tokens=5000, free_output_tokens=512, reference_prompt_tokens=4096, max_context_length=5120)
        budget = modelled_work(4096, 512)
        assert allowance == 370
        processed = retained_prompt_tokens(5000, max_context_length=5120, max_length=allowance)
        assert modelled_work(processed, allowance) <= budget
        # The fixed allowance would have been granted on a smaller prompt than settlement sees.
        assert modelled_work(retained_prompt_tokens(5000, max_context_length=5120, max_length=512), 512) > budget

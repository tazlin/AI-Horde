# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Worked examples of what a text job costs and pays, for readers rather than for the arithmetic.

Each case is a realistic request with the figures a requester or a worker would see: the prompt the
worker is sent, the text it returned, the model's reference size, and the resulting kudos. The expected
values are worked by hand from the formula in ``docs/reference/text_kudos_pricing.md``, so a change to
the tariff shows up here as a changed number next to the scenario it changes.

Operands: prompt tokens are ``ceil(characters / 4)``; output tokens are ``ceil(characters / 4)`` floored
at 20 and capped at the requested ``max_length``; the model operand is its parameter count in billions.
Work is counted in units of one generated token at an empty context:
``0.17466 * P + 0.0000196 * P^2`` for processing the prompt, plus ``O * (1 + 0.0002521 * P)`` for
generating, with ``P`` capped at 16,384. Units convert to kudos at ``1.33527 * 13 / 125`` for a 13B model
on a validated backend, which pins a 512-token job on a 1,024-token prompt to the 117.15 kudos it paid
before the prompt term existed.
"""

from __future__ import annotations

import pytest

from horde.classes.kobold.pricing import (
    calculate_text_generation_reward,
    count_output_tokens,
    estimate_billing_prompt_tokens,
    free_output_allowance,
    quote_text_generation,
    retained_prompt_tokens,
)

pytestmark = pytest.mark.unit

VALIDATED = {"owner_trusted": True, "backend_validated": True, "matched_targeting": False}


def reward(*, prompt_chars: int, returned_chars: int | None, max_length: int, billions: float | None, **overrides: object) -> float:
    """Settle a job from the figures a worker log would show."""
    returned = None if returned_chars is None else "x" * returned_chars
    return calculate_text_generation_reward(
        output_tokens=None if returned is None else count_output_tokens(returned, max_length),
        prompt_tokens=estimate_billing_prompt_tokens("x" * prompt_chars),
        model_multiplier=billions,
        empty_output=returned == "",
        **{**VALIDATED, **overrides},
    )


class TestEverydayJobs:
    def test_a_chat_turn_on_a_7b_model(self):
        """A 1,500-character chat history, 150 tokens allowed, 480 characters returned.

        375 prompt tokens cost 65.5 + 2.8 units to process; 120 output tokens cost 120 * 1.095 = 131.3
        units to generate. 199.5 units * 1.33527 * 7 / 125 = 14.92 kudos to the worker.
        """
        assert reward(prompt_chars=1_500, returned_chars=480, max_length=150, billions=7) == 14.92

    def test_summarising_a_long_document_on_a_13b_model(self):
        """A 40,000-character document (10,000 tokens), 512 allowed, 1,200 characters (300 tokens) returned.

        Processing the prompt is 1,746.6 + 1,960.0 units; generating 300 tokens over it is 300 * 3.521 =
        1,056.3 units. 4,762.9 units * 1.33527 * 13 / 125 = 661.41 kudos. The prompt is three quarters of it.
        """
        assert reward(prompt_chars=40_000, returned_chars=1_200, max_length=512, billions=13) == 661.41

    def test_the_same_document_on_a_20b_model_adds_the_size_bonus(self):
        """Above 13B the parameter bonus (B / 13) ** 0.2 applies: (20 / 13) ** 0.2 = 1.090.

        4,762.9 units * 1.33527 * 1.090 * 20 / 125 = 1,109.12 kudos.
        """
        assert reward(prompt_chars=40_000, returned_chars=1_200, max_length=512, billions=20) == 1109.12

    def test_roleplay_on_a_70b_model_whose_reply_overran_the_allowance(self):
        """An 8,000-character scene (2,000 tokens), 300 allowed, 1,400 characters returned.

        1,400 characters estimate to 350 tokens but only 300 were allowed, so 300 are paid. Prompt 349.3 +
        78.4 units, generation 300 * 1.504 = 451.3 units: 879.0 * 1.33527 * (70 / 13) ** 0.2 * 70 / 125 =
        920.38 kudos.
        """
        assert reward(prompt_chars=8_000, returned_chars=1_400, max_length=300, billions=70) == 920.38

    def test_the_worker_is_paid_for_the_prompt_it_was_sent_not_the_context_it_was_offered(self):
        """Two jobs with the same prompt and reply price alike whatever max_context_length the request carried."""
        assert reward(prompt_chars=40_000, returned_chars=1_200, max_length=512, billions=13) == reward(
            prompt_chars=40_000,
            returned_chars=1_200,
            max_length=4096,
            billions=13,
        )

    def test_a_short_answer_to_a_long_document_is_mostly_prompt(self):
        """The same 10,000-token document answered in 20 tokens: 3,706.6 + 70.4 units = 524.51 kudos.

        Under the previous curve this job paid 30.28, because the prompt only scaled the output.
        """
        assert reward(prompt_chars=40_000, returned_chars=80, max_length=512, billions=13) == 524.51


class TestWhatChangesThePrice:
    def test_an_unvalidated_backend_keeps_thirty_percent(self):
        """The chat turn above on a bridge outside LLM_VALIDATED_BACKENDS: 14.92 * 0.3 = 4.48."""
        assert reward(prompt_chars=1_500, returned_chars=480, max_length=150, billions=7, backend_validated=False) == 4.48

    def test_an_empty_reply_to_the_long_document_is_paid_the_floor_but_not_the_prompt(self):
        """Nothing visible came back: 20 floor tokens over the context, 70.4 units = 9.78 kudos, and no prompt term.

        An empty return cannot be told from a skipped job, so the prompt is not paid for it.
        """
        assert reward(prompt_chars=40_000, returned_chars=0, max_length=512, billions=13) == 9.78

    def test_a_cancelled_job_that_returned_nothing_pays_nothing(self):
        """No submission means no output operand and nothing to pay, however long the prompt."""
        assert reward(prompt_chars=40_000, returned_chars=None, max_length=512, billions=13) == 0.0

    def test_an_unknown_model_under_a_trusted_owner_is_paid_as_a_small_model(self):
        """The long document on a model the reference does not list: 4,762.9 * 1.33527 * 2.7 / 100 = 171.71."""
        assert reward(prompt_chars=40_000, returned_chars=1_200, max_length=512, billions=None) == pytest.approx(171.71, abs=0.01)

    def test_an_unknown_model_under_an_untrusted_owner_is_paid_a_flat_prompt_amount(self):
        """The same job pays only the legacy context multiplier, 14.56, whatever was returned."""
        flat = reward(prompt_chars=40_000, returned_chars=1_200, max_length=512, billions=None, owner_trusted=False)
        assert flat == pytest.approx(14.56, abs=0.01)

    def test_prompts_past_sixteen_thousand_tokens_price_alike(self):
        """A 100,000-character prompt and a 65,536-character prompt pay the same: both are at the ceiling."""
        assert reward(prompt_chars=100_000, returned_chars=2_048, max_length=512, billions=13) == reward(
            prompt_chars=65_536,
            returned_chars=2_048,
            max_length=512,
            billions=13,
        )


class TestWhatTheRequesterSees:
    """The quote is the reward at the full allowance; the hold and the dry run add the burns."""

    def test_the_quote_for_the_long_document_request(self):
        """13B, 512 tokens allowed, 10,000-token prompt: 3,706.6 + 512 * 3.521 = 5,509.4 units = 765.07 per job.

        The accepted response reports it rounded: 765.
        """
        quote = quote_text_generation(output_allowance=512, prompt_tokens=10_000, model_multiplier=13, matched_targeting=False)
        assert quote == 765.07
        assert round(quote) == 765

    def test_the_hold_and_the_dry_run_for_two_jobs(self):
        """Each job adds a one-kudos burn; the request adds a one-kudos activation tax.

        Hold: (765.07 + 1) * 2 = 1532.14. Dry run: 1532.14 + 1 = 1533.14, reported as 1533.
        """
        quote = 765.07
        hold = round((quote + 1) * 2, 2)
        dry_run = hold + 1
        assert hold == 1532.14
        assert round(dry_run) == 1533

    def test_the_settled_debit_is_what_was_returned_plus_the_burn(self):
        """The worker returned 300 of the 512 allowed tokens: the requester pays 661.41 + 1 per job, not 766.07."""
        settled = reward(prompt_chars=40_000, returned_chars=1_200, max_length=512, billions=13)
        assert settled + 1 == 662.41


class TestFreeTierAdmission:
    """Without kudos on hand, how much output a prompt may ask for; past that a hold is required."""

    def test_a_short_prompt_keeps_the_whole_allowance(self):
        """Under the 4,096-token reference prompt the queue-pressure allowance stands, here 512."""
        assert free_output_allowance(prompt_tokens=1_000, free_output_tokens=512, reference_prompt_tokens=4_096) == 512

    def test_a_five_thousand_token_prompt_may_ask_for_319_tokens_without_kudos(self):
        """The budget is the work of 4,096 + 512, 2,084.9 units; a 5,000-token prompt spends 1,363.3 of it on
        processing and the rest buys floor((2,084.9 - 1,363.3) / 2.26) = 319 tokens."""
        assert free_output_allowance(prompt_tokens=5_000, free_output_tokens=512, reference_prompt_tokens=4_096) == 319

    def test_the_long_document_cannot_be_run_without_kudos_at_all(self):
        """Processing 10,000 tokens is 3,706.6 units, more than the whole 2,084.9-unit budget, so even 16 tokens of
        output do not fit. The request needs kudos on hand; asking for less output does not help."""
        assert free_output_allowance(prompt_tokens=10_000, free_output_tokens=512, reference_prompt_tokens=4_096) == 0

    def test_a_larger_reference_prompt_widens_the_free_tier(self):
        """With HORDE_TEXT_FREE_PROMPT_TOKENS=8192 the budget is 4,317 units and the 10,000-token document keeps 172 tokens."""
        assert free_output_allowance(prompt_tokens=10_000, free_output_tokens=512, reference_prompt_tokens=8_192) == 172


class TestAChatThatOutgrewItsContext:
    """A 20,000-character chat log (5,000 tokens) sent into a 2,048 context with 256 tokens allowed, under the default
    ``context_fit=ignore``. The worker keeps the last 2,048 - 256 = 1,792 tokens of it and discards the rest."""

    def test_the_worker_processes_the_last_1792_tokens(self):
        assert retained_prompt_tokens(estimate_billing_prompt_tokens("x" * 20_000), max_context_length=2048, max_length=256) == 1792

    def test_the_job_is_priced_on_those_tokens_not_the_five_thousand_sent(self):
        """1,792 tokens cost 313.0 + 62.9 units to process; 256 output tokens cost 256 * 1.452 = 371.6 units.
        747.5 units * 1.33527 * 13 / 125 = 103.82 kudos, where the whole log would have been 269.68."""
        assert calculate_text_generation_reward(output_tokens=256, prompt_tokens=1792, model_multiplier=13, **VALIDATED) == 103.82
        assert calculate_text_generation_reward(output_tokens=256, prompt_tokens=5000, model_multiplier=13, **VALIDATED) == 269.68

    def test_the_log_runs_free_because_what_is_processed_is_under_the_reference(self):
        assert (
            free_output_allowance(prompt_tokens=5_000, free_output_tokens=512, reference_prompt_tokens=4_096, max_context_length=2048)
            == 512
        )

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
"""

from __future__ import annotations

import pytest

from horde.classes.kobold.pricing import (
    calculate_text_generation_reward,
    count_output_tokens,
    estimate_billing_prompt_tokens,
    free_output_allowance,
    quote_text_generation,
)

pytestmark = pytest.mark.unit

VALIDATED = {"owner_trusted": True, "backend_validated": True, "matched_targeting": False}


def reward(*, prompt_chars: int, returned_chars: int | None, max_length: int, billions: float | None, **overrides: object) -> float:
    """Settle a job from the figures a worker log would show."""
    returned = None if returned_chars is None else "x" * returned_chars
    return calculate_text_generation_reward(
        output_tokens=count_output_tokens(returned, max_length),
        prompt_tokens=estimate_billing_prompt_tokens("x" * prompt_chars),
        model_multiplier=billions,
        **{**VALIDATED, **overrides},
    )


class TestEverydayJobs:
    def test_a_chat_turn_on_a_7b_model(self):
        """A 1,500-character chat history, 150 tokens allowed, 480 characters returned.

        375 prompt tokens, 120 output tokens, context multiplier 1.519:
        120 * 7 / 125 * 1.519 = 10.21 kudos to the worker.
        """
        assert reward(prompt_chars=1_500, returned_chars=480, max_length=150, billions=7) == 10.21

    def test_summarising_a_long_document_on_a_13b_model(self):
        """A 40,000-character document (10,000 tokens), 512 allowed, 1,200 characters (300 tokens) returned.

        Context multiplier 14.56: 300 * 13 / 125 * 14.56 = 454.25 kudos. The prompt, not the short
        answer, is what makes this job expensive.
        """
        assert reward(prompt_chars=40_000, returned_chars=1_200, max_length=512, billions=13) == 454.25

    def test_the_same_document_on_a_20b_model_adds_the_size_bonus(self):
        """Above 13B the parameter bonus (B / 13) ** 0.2 applies: (20 / 13) ** 0.2 = 1.090.

        300 * 1.090 * 20 / 125 * 14.56 = 761.73 kudos.
        """
        assert reward(prompt_chars=40_000, returned_chars=1_200, max_length=512, billions=20) == 761.73

    def test_roleplay_on_a_70b_model_whose_reply_overran_the_allowance(self):
        """An 8,000-character scene (2,000 tokens), 300 allowed, 1,400 characters returned.

        1,400 characters estimate to 350 tokens but only 300 were allowed, so 300 are paid:
        300 * (70 / 13) ** 0.2 * 70 / 125 * 3.341 = 786.09 kudos.
        """
        assert reward(prompt_chars=8_000, returned_chars=1_400, max_length=300, billions=70) == 786.09

    def test_the_worker_is_paid_for_the_prompt_it_was_sent_not_the_context_it_was_offered(self):
        """Two jobs with the same prompt and reply price alike whatever max_context_length the request carried."""
        assert reward(prompt_chars=40_000, returned_chars=1_200, max_length=512, billions=13) == reward(
            prompt_chars=40_000,
            returned_chars=1_200,
            max_length=4096,
            billions=13,
        )


class TestWhatChangesThePrice:
    def test_an_unvalidated_backend_keeps_thirty_percent(self):
        """The chat turn above on a bridge outside LLM_VALIDATED_BACKENDS: 10.21 * 0.3 = 3.06."""
        assert reward(prompt_chars=1_500, returned_chars=480, max_length=150, billions=7, backend_validated=False) == 3.06

    def test_an_empty_reply_to_the_long_document_still_pays_the_output_floor(self):
        """Nothing came back, but the submission is paid at the 20-token floor: 20 * 13 / 125 * 14.56 = 30.28."""
        assert reward(prompt_chars=40_000, returned_chars=0, max_length=512, billions=13) == 30.28

    def test_a_cancelled_job_that_returned_nothing_pays_nothing(self):
        """No submission means no output operand, and a known model's reward is proportional to output."""
        assert reward(prompt_chars=40_000, returned_chars=None, max_length=512, billions=13) == 0.0

    def test_an_unknown_model_under_a_trusted_owner_is_paid_as_a_small_model(self):
        """The long document on a model the reference does not list: 300 * 14.56 * 2.7 / 100 = 117.93."""
        assert reward(prompt_chars=40_000, returned_chars=1_200, max_length=512, billions=None) == pytest.approx(117.93, abs=0.01)

    def test_an_unknown_model_under_an_untrusted_owner_is_paid_a_flat_prompt_amount(self):
        """The same job pays only the context multiplier, 14.56, whatever was returned."""
        assert reward(prompt_chars=40_000, returned_chars=1_200, max_length=512, billions=None, owner_trusted=False) == pytest.approx(
            14.56, abs=0.01
        )


class TestWhatTheRequesterSees:
    """The quote is the reward at the full allowance; the hold and the dry run add the burns."""

    def test_the_quote_for_the_long_document_request(self):
        """13B, 512 tokens allowed, 10,000-token prompt: 512 * 13 / 125 * 14.56 = 775.26 per job.

        The accepted response reports it rounded: 775.
        """
        quote = quote_text_generation(output_allowance=512, prompt_tokens=10_000, model_multiplier=13, matched_targeting=False)
        assert quote == 775.26
        assert round(quote) == 775

    def test_the_hold_and_the_dry_run_for_two_jobs(self):
        """Each job adds a one-kudos burn; the request adds a one-kudos activation tax.

        Hold: (775.26 + 1) * 2 = 1552.52. Dry run: 1552.52 + 1 = 1553.52, reported as 1554.
        """
        quote = 775.26
        hold = round((quote + 1) * 2, 2)
        dry_run = hold + 1
        assert hold == 1552.52
        assert round(dry_run) == 1554

    def test_the_settled_debit_is_what_was_returned_plus_the_burn(self):
        """The worker returned 300 of the 512 allowed tokens: the requester pays 454.25 + 1 per job, not 776.26."""
        settled = reward(prompt_chars=40_000, returned_chars=1_200, max_length=512, billions=13)
        assert settled + 1 == 455.25


class TestFreeTierAdmission:
    """Without kudos on hand, how much output a prompt may ask for; past that a hold is required."""

    def test_a_short_prompt_keeps_the_whole_allowance(self):
        """Under the 4,096-token reference prompt the queue-pressure allowance stands, here 512."""
        assert free_output_allowance(prompt_tokens=1_000, free_output_tokens=512, reference_prompt_tokens=4_096) == 512

    def test_the_long_document_may_ask_for_212_tokens_without_kudos(self):
        """10,000 prompt tokens: floor(512 * 6.04 / 14.56) = 212. Asking for 512 needs a hold or a downgrade to 212."""
        assert free_output_allowance(prompt_tokens=10_000, free_output_tokens=512, reference_prompt_tokens=4_096) == 212

    def test_a_fifty_thousand_token_prompt_keeps_103_tokens_at_the_cap(self):
        """Past the 30x cap every prompt prices alike: floor(512 * 6.04 / 30) = 103."""
        assert free_output_allowance(prompt_tokens=50_000, free_output_tokens=512, reference_prompt_tokens=4_096) == 103

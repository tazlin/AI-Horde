# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit coverage for the text (kobold) ``TextWaitingPrompt`` decision logic.

The stable/image line has dedicated tests; the text line had none. The kudos and
upfront-requirement methods here are effectively pure given their inputs. They
read a handful of attributes off ``self`` and an env var, so we exercise the
branch matrix by binding each method to a lightweight stub rather than building a
full persisted ORM graph (``WaitingPrompt.__init__`` commits to the DB). This
keeps the test hermetic and fast while still locking the real arithmetic.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace

import pytest

from horde.classes.kobold.pricing import WORK_UNIT_SCALE
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt


def _wp(slow_workers=True, max_length=80, max_context_length=65_536, workers=(), models=(), prompt="", n=1):
    wp = SimpleNamespace(
        slow_workers=slow_workers,
        max_length=max_length,
        max_context_length=max_context_length,
        workers=list(workers),
        models=list(models),
        prompt=prompt,
        n=n,
        kudos=0.0,
    )
    wp._quote_model_multiplier = MethodType(TextWaitingPrompt._quote_model_multiplier, wp)
    return wp


@pytest.fixture(autouse=True)
def _reference_prompt_default(monkeypatch):
    monkeypatch.delenv("HORDE_TEXT_FREE_PROMPT_TOKENS", raising=False)
    monkeypatch.delenv("HORDE_REQUIRE_MATCHED_TARGETING", raising=False)


class TestRequireUpfrontKudos:
    def test_no_slow_workers_requires_upfront_with_unclamped_budget(self):
        # When slow workers are disallowed the clamp is skipped, so the returned
        # token budget is the raw formula (512 + threads*5 - round(queue*0.9)).
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(slow_workers=False),
            {"queued_text_requests": 0},
            100,
        )
        assert result == (True, 1012, False)

    def test_within_budget_no_upfront(self):
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(slow_workers=True, max_length=400),
            {"queued_text_requests": 0},
            0,
        )
        assert result == (False, 512, False)

    def test_over_budget_requires_upfront(self):
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(slow_workers=True, max_length=600),
            {"queued_text_requests": 0},
            0,
        )
        assert result == (True, 512, False)

    def test_queue_pressure_clamps_budget_to_floor(self):
        # queue=300 -> 512 - 270 = 242, clamped up to the 256 floor; 300 > 256.
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(slow_workers=True, max_length=300),
            {"queued_text_requests": 300},
            0,
        )
        assert result == (True, 256, False)

    def test_workerlist_env_forces_upfront_and_blocks_downgrade(self, monkeypatch):
        monkeypatch.setenv("HORDE_UPFRONT_KUDOS_ON_WORKERLIST", "1")
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(slow_workers=True, max_length=200, workers=["w"]),
            {"queued_text_requests": 0},
            0,
        )
        assert result == (True, 512, True)


class TestKudos:
    def test_extra_kudos_burn_adds_one(self):
        assert TextWaitingPrompt.calculate_extra_kudos_burn(SimpleNamespace(), 10) == 11

    def test_calculate_kudos_empty_model_list_uses_13b_assumption(self):
        # No prompt: 80 generated tokens at an empty context are 80 work units, scaled by 1.33527 to the
        # legacy level and priced as 13B: round(80 * 1.33527 * 13 / 125, 2) = 11.11
        assert TextWaitingPrompt.calculate_kudos(_wp(max_length=80)) == 11.11

    def test_calculate_kudos_matched_targeting_env_floors_to_min(self, monkeypatch):
        monkeypatch.setenv("HORDE_REQUIRE_MATCHED_TARGETING", "1")
        wp = _wp(workers=["w"])
        assert TextWaitingPrompt.calculate_kudos(wp) == 0.1
        assert wp.kudos == 0.1


class TestCalculateKudos:
    """The quote prices the prompt the worker will process and the largest requested model, and stores itself on ``kudos``."""

    @pytest.fixture(autouse=True)
    def reference(self, monkeypatch):
        from horde.classes.kobold import waiting_prompt as text_wp_module

        sizes = {"a-7B": 7.0, "a-13B": 13.0, "a-70B": 70.0}
        monkeypatch.setattr(text_wp_module.model_reference, "is_known_text_model", lambda model_name: model_name in sizes)
        monkeypatch.setattr(text_wp_module.model_reference, "get_text_model_multiplier", lambda model_name: sizes.get(model_name, 1))

    def test_unknown_model_quotes_from_the_prompt_itself(self):
        """The unknown-model branch prices the request from its own max_length; the object has no ``wp``."""
        wp = _wp(max_length=100, models=(SimpleNamespace(model="not-in-reference"),))

        kudos = TextWaitingPrompt.calculate_kudos(wp)

        assert kudos == pytest.approx(100 * WORK_UNIT_SCALE * (2.7 / 100))
        assert wp.kudos == kudos

    def test_the_prompt_sets_the_quote_and_a_context_it_fits_does_not(self):
        short = _wp(max_length=512, max_context_length=98_304, prompt="x" * 4096, models=(SimpleNamespace(model="a-13B"),))
        long = _wp(max_length=512, max_context_length=16384, prompt="x" * 40_000, models=(SimpleNamespace(model="a-13B"),))

        assert TextWaitingPrompt.calculate_kudos(short) == 117.15
        assert TextWaitingPrompt.calculate_kudos(long) == 765.07

    def test_a_prompt_the_context_truncates_is_quoted_at_what_the_worker_keeps(self):
        # 10,000 tokens into a 2,048 context beside 512 of output: 1,536 tokens are processed and quoted.
        truncated = _wp(max_length=512, max_context_length=2048, prompt="x" * 40_000, models=(SimpleNamespace(model="a-13B"),))

        assert TextWaitingPrompt.calculate_kudos(truncated) == 142.31

    def test_the_quote_matches_settlement_for_the_same_operands(self):
        from horde.classes.kobold.pricing import calculate_text_generation_reward

        wp = _wp(max_length=512, prompt="x" * 8192, models=(SimpleNamespace(model="a-13B"),))

        assert TextWaitingPrompt.calculate_kudos(wp) == calculate_text_generation_reward(
            output_tokens=512,
            prompt_tokens=2048,
            model_multiplier=13.0,
            owner_trusted=True,
            backend_validated=True,
            matched_targeting=False,
        )

    def test_the_largest_known_model_is_quoted_whatever_the_order(self):
        small_first = _wp(max_length=512, prompt="x" * 4096, models=(SimpleNamespace(model="a-7B"), SimpleNamespace(model="a-70B")))
        large_first = _wp(max_length=512, prompt="x" * 4096, models=(SimpleNamespace(model="a-70B"), SimpleNamespace(model="a-7B")))

        assert TextWaitingPrompt.calculate_kudos(small_first) == TextWaitingPrompt.calculate_kudos(large_first) == 883.31

    def test_an_unknown_model_beside_a_known_one_does_not_lower_the_quote(self):
        mixed = _wp(max_length=512, prompt="x" * 4096, models=(SimpleNamespace(model="not-in-reference"), SimpleNamespace(model="a-7B")))

        assert TextWaitingPrompt.calculate_kudos(mixed) == 63.08


class TestRequireUpfrontKudosForLongPrompts:
    """A prompt past the reference size lowers the output allowance a free request may keep."""

    def test_a_prompt_at_the_reference_keeps_the_full_allowance(self):
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(max_length=512, prompt="x" * (4096 * 4)),
            {"queued_text_requests": 0},
            0,
        )
        assert result == (False, 512, False)

    def test_a_long_prompt_within_the_output_allowance_requires_upfront(self):
        # 5,000 estimated tokens leave 319 of the 512-token budget's work for generation.
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(max_length=512, prompt="x" * (5_000 * 4)),
            {"queued_text_requests": 0},
            0,
        )
        assert result == (True, 319, False)

    def test_a_long_prompt_with_a_small_output_stays_free(self):
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(max_length=100, prompt="x" * (5_000 * 4)),
            {"queued_text_requests": 0},
            0,
        )
        assert result == (False, 319, False)

    def test_queue_pressure_and_the_prompt_compound(self):
        # queue=300 -> 256 floor; a 5,000-token prompt leaves 89 of that budget.
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(max_length=100, prompt="x" * (5_000 * 4)),
            {"queued_text_requests": 300},
            0,
        )
        assert result == (True, 89, False)

    def test_a_prompt_whose_processing_exceeds_the_budget_cannot_be_downgraded_into_the_free_tier(self):
        # 50,000 estimated tokens: no output allowance at all, and the third entry forbids a downgrade.
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(max_length=16, prompt="x" * (50_000 * 4)),
            {"queued_text_requests": 0},
            0,
        )
        assert result == (True, 0, True)

    def test_a_prompt_the_context_truncates_is_gated_on_what_the_worker_processes(self):
        # 50,000 estimated tokens into a 2,048 context: the worker keeps 1,536, under the reference.
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(max_length=512, max_context_length=2048, prompt="x" * (50_000 * 4)),
            {"queued_text_requests": 0},
            0,
        )
        assert result == (False, 512, False)

    def test_the_reference_prompt_is_configurable(self, monkeypatch):
        monkeypatch.setenv("HORDE_TEXT_FREE_PROMPT_TOKENS", "65536")
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(max_length=512, prompt="x" * (50_000 * 4)),
            {"queued_text_requests": 0},
            0,
        )
        assert result == (False, 512, False)

    def test_excluding_slow_workers_still_returns_the_raw_budget(self):
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(slow_workers=False, max_length=512, prompt="x" * (50_000 * 4)),
            {"queued_text_requests": 0},
            100,
        )
        assert result == (True, 1012, False)


class TestDowngrade:
    @pytest.fixture(autouse=True)
    def no_database(self, monkeypatch):
        from horde.classes.kobold import waiting_prompt as text_wp_module

        monkeypatch.setattr(text_wp_module, "db", SimpleNamespace(session=SimpleNamespace(commit=lambda: None)))
        monkeypatch.setattr(text_wp_module.model_reference, "is_known_text_model", lambda model_name: True)
        monkeypatch.setattr(text_wp_module.model_reference, "get_text_model_multiplier", lambda model_name: 13.0)

    def _wp(self, max_length: int, n: int = 1) -> SimpleNamespace:
        wp = _wp(max_length=max_length, prompt="x" * 4096, models=(SimpleNamespace(model="a-13B"),), n=n)
        wp.id = "wp-id"
        wp.things = max_length
        wp.params = {"max_length": max_length, "temperature": 0.7}
        wp.gen_payload = {"max_length": max_length, "prompt": wp.prompt, "n": 1}
        wp.total_usage = float(max_length * n)
        wp.calculate_kudos = MethodType(TextWaitingPrompt.calculate_kudos, wp)
        TextWaitingPrompt.calculate_kudos(wp)
        return wp

    def test_every_field_carrying_the_allowance_moves_together(self):
        wp = self._wp(max_length=1024, n=3)
        quote_before = wp.kudos

        TextWaitingPrompt.downgrade(wp, 512)

        assert wp.slow_workers is True
        assert wp.max_length == 512
        assert wp.things == 512
        assert wp.params["max_length"] == 512
        assert wp.gen_payload["max_length"] == 512
        assert wp.total_usage == 512 * 3
        assert wp.kudos == 117.15 < quote_before

    def test_the_settlement_cap_follows_the_downgrade(self):
        from horde.classes.kobold.processing_generation import TextProcessingGeneration

        wp = self._wp(max_length=1024)
        TextWaitingPrompt.downgrade(wp, 512)
        procgen = SimpleNamespace(generation="x" * 4096, wp=wp)

        assert TextProcessingGeneration.get_things_count(procgen) == 512

    def test_a_request_within_the_allowance_only_accepts_slow_workers(self):
        wp = self._wp(max_length=200)
        quote_before = wp.kudos

        TextWaitingPrompt.downgrade(wp, 512)

        assert wp.slow_workers is True
        assert wp.max_length == 200
        assert wp.kudos == quote_before

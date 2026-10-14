# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The text settlement adapter, ``TextProcessingGeneration.get_gen_kudos``, over stubs.

The methods read a handful of attributes off ``self``, the waiting prompt and the worker, so they
are bound to a ``SimpleNamespace`` rather than a persisted ORM graph, following
``test_kobold_text_wp.py``.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace

import pytest

from horde.classes.kobold import processing_generation as text_procgen_module
from horde.classes.kobold.processing_generation import TextProcessingGeneration

pytestmark = pytest.mark.unit

KNOWN_MODEL = "a-known-13B"
UNKNOWN_MODEL = "not-in-reference"
VALIDATED_AGENT = "AI Horde Worker:23:https://example.invalid"
UNVALIDATED_AGENT = "SomeOtherBridge:1:https://example.invalid"


@pytest.fixture(autouse=True)
def reference(monkeypatch):
    """Make one model known at 13B and treat the AI Horde Worker agent as validated."""
    monkeypatch.setattr(text_procgen_module.model_reference, "is_known_text_model", lambda model_name: model_name == KNOWN_MODEL)
    monkeypatch.setattr(text_procgen_module.model_reference, "get_text_model_multiplier", lambda model_name: 13.0)
    monkeypatch.setattr(text_procgen_module, "is_backed_validated", lambda agent: agent == VALIDATED_AGENT)
    monkeypatch.delenv("HORDE_REQUIRE_MATCHED_TARGETING", raising=False)


def _procgen(
    *,
    prompt_tokens: int,
    generation: str | None = "x" * 2048,
    max_length: int = 512,
    max_context_length: int = 65_536,
    model: str = KNOWN_MODEL,
    trusted: bool = True,
    bridge_agent: str = VALIDATED_AGENT,
    workers: tuple[str, ...] = (),
) -> SimpleNamespace:
    procgen = SimpleNamespace(
        generation=generation,
        model=model,
        wp=SimpleNamespace(
            prompt="x" * (prompt_tokens * 4),
            things=max_length,
            max_length=max_length,
            max_context_length=max_context_length,
            workers=list(workers),
        ),
        worker=SimpleNamespace(bridge_agent=bridge_agent, user=SimpleNamespace(trusted=trusted)),
    )
    procgen.get_things_count = MethodType(TextProcessingGeneration.get_things_count, procgen)
    procgen.get_gen_kudos = MethodType(TextProcessingGeneration.get_gen_kudos, procgen)
    return procgen


class TestGetGenKudos:
    @pytest.mark.parametrize(
        ("prompt_tokens", "expected"),
        [(1024, 117.15), (10_000, 765.07), (50_000, 1492.79)],
    )
    def test_the_prompt_sent_sets_the_price_for_the_same_returned_text(self, prompt_tokens: int, expected: float):
        assert _procgen(prompt_tokens=prompt_tokens).get_gen_kudos() == expected

    def test_the_requested_allowance_does_not_change_the_prompt_operand_while_the_prompt_fits(self):
        # 2048 returned characters count as 512 tokens under either allowance, so the only thing a
        # larger allowance could change is the prompt measurement, and it must not.
        assert _procgen(prompt_tokens=10_000, max_length=512).get_gen_kudos() == 765.07
        assert _procgen(prompt_tokens=10_000, max_length=1024).get_gen_kudos() == 765.07

    def test_the_requested_context_does_not_change_the_price_while_the_prompt_fits(self):
        assert _procgen(prompt_tokens=10_000, max_context_length=16384).get_gen_kudos() == 765.07
        assert _procgen(prompt_tokens=10_000, max_context_length=98_304).get_gen_kudos() == 765.07

    def test_a_prompt_the_context_truncates_is_priced_at_what_the_worker_keeps(self):
        # 10,000 tokens into a 2,048 context beside 512 of output: the worker keeps the last 1,536.
        assert _procgen(prompt_tokens=10_000, max_context_length=2048, max_length=512).get_gen_kudos() == 142.31
        assert _procgen(prompt_tokens=1536, max_context_length=65_536, max_length=512).get_gen_kudos() == 142.31

    def test_an_allowance_past_four_fifths_of_the_context_still_leaves_the_prompt_a_fifth(self):
        # The backend lowers max_length rather than keep fewer than 410 of 2,048 tokens for the prompt.
        assert _procgen(prompt_tokens=10_000, max_context_length=2048, max_length=4096).get_gen_kudos() == 88.85

    def test_the_returned_text_is_still_capped_by_the_allowance(self):
        assert _procgen(prompt_tokens=1024, generation="x" * 8192, max_length=512).get_gen_kudos() == 117.15

    def test_an_empty_return_pays_the_output_floor_over_the_context_and_no_prompt_term(self):
        # Whitespace counts as empty too: 20 floor tokens at the 16K ceiling's context rate, prompt withheld.
        assert _procgen(prompt_tokens=50_000, generation="").get_gen_kudos() == 14.25
        assert _procgen(prompt_tokens=50_000, generation="  \n ").get_gen_kudos() == 14.25

    def test_a_one_word_return_is_paid_the_prompt_term(self):
        # The same floor of 20 tokens, now with the 16K-ceiling prompt processing paid.
        assert _procgen(prompt_tokens=50_000, generation="ok").get_gen_kudos() > 1_000

    def test_no_generation_pays_nothing_on_a_known_model(self):
        # The cancellation path prices before anything was returned.
        assert _procgen(prompt_tokens=50_000, generation=None).get_gen_kudos() == 0.0

    def test_an_unvalidated_backend_keeps_thirty_percent(self):
        assert _procgen(prompt_tokens=1024, bridge_agent=UNVALIDATED_AGENT).get_gen_kudos() == 35.14

    def test_an_unknown_model_under_a_trusted_owner(self):
        assert _procgen(prompt_tokens=1024, model=UNKNOWN_MODEL).get_gen_kudos() == 512 * 2.2 * (2.7 / 100)

    def test_an_unknown_model_under_an_untrusted_owner_is_paid_the_multiplier_even_without_output(self):
        assert _procgen(prompt_tokens=1024, model=UNKNOWN_MODEL, trusted=False, generation=None).get_gen_kudos() == pytest.approx(2.2)

    def test_matched_targeting_overrides_everything(self, monkeypatch):
        monkeypatch.setenv("HORDE_REQUIRE_MATCHED_TARGETING", "1")
        assert _procgen(prompt_tokens=50_000, workers=("w",)).get_gen_kudos() == 0.1
        assert _procgen(prompt_tokens=50_000).get_gen_kudos() == 1492.79


class TestGetThingsCount:
    def test_counts_the_stored_generation_by_default(self):
        assert _procgen(prompt_tokens=1024, generation="x" * 400).get_things_count() == 100

    def test_counts_a_supplied_generation(self):
        assert _procgen(prompt_tokens=1024, generation=None).get_things_count("x" * 400) == 100

    def test_nothing_stored_counts_as_nothing(self):
        assert _procgen(prompt_tokens=1024, generation=None).get_things_count() == 0

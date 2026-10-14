# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tariff for text generation kudos.

One set of pure functions prices a text job from server-visible operands, so the dry-run quote,
the accepted-request quote, the upfront hold and the settlement on submit all apply the same
arithmetic. Nothing here touches the ORM, Flask, Redis or the kudos ledger.

Operands:

- The prompt operand is the part of the prompt the worker processes: the text it is sent, after any
  style template has been applied, sized from characters (``estimate_billing_prompt_tokens``) and
  capped at what the requested context keeps once ``max_length`` is reserved for the output
  (``retained_prompt_tokens``; ``billing_prompt_tokens`` composes the two). The server has no
  tokenizer and the estimate is a tariff quantity, not a model token count.
- The output operand is the text the worker returned, capped by the requested ``max_length``
  (``count_output_tokens``). Before a job completes the requested allowance stands in for it.
- The model operand is the reference parameter count in billions, or ``None`` for a model the
  reference does not know.

Work model. A job's work is measured in units of one generated token at an empty context:

    work(P, O) = input_rate * P + input_quadratic_rate * P^2 + O * (1 + context_decode_rate * P)

The first two terms are prompt processing, which the worker does once per job however little it
returns; the last is generation, each token of which costs more the longer the context it attends
over. The shape is what a transformer backend measures (``tools/text_tariff_bench.py``); the rates
in ``PROVISIONAL_RATES`` record the hardware they came from. ``WORK_UNIT_SCALE`` converts units to
the legacy curve's level at the reference job, so a 512-token job on a 1,024-token prompt prices as
it did before the input term existed. Prompts past ``PRICED_PROMPT_TOKENS_CEILING`` price alike.

``calculate_text_generation_reward`` is the settlement kernel. ``quote_text_generation`` applies
the same kernel under the assumptions a quote has to make before a worker is chosen.
``free_output_allowance`` turns the free-tier output budget into a budget on modelled work, so a
long prompt lowers how many tokens may be generated without an upfront hold, down to none.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

from horde.logger import logger

__all__ = [
    "BILLING_CHARS_PER_TOKEN",
    "DEFAULT_FREE_PROMPT_TOKENS",
    "DEFAULT_QUOTE_MODEL_BILLIONS",
    "FREE_PROMPT_TOKENS_ENV",
    "MATCHED_TARGETING_ENV",
    "MATCHED_TARGETING_REWARD",
    "MIN_OUTPUT_TOKENS",
    "MIN_PRICED_TOKENS",
    "PRICED_PROMPT_TOKENS_CEILING",
    "PROVISIONAL_RATES",
    "TEXT_TARIFF_VERSION",
    "WORK_UNIT_SCALE",
    "TextWorkRates",
    "billing_prompt_tokens",
    "calculate_text_generation_reward",
    "count_output_tokens",
    "decode_work",
    "estimate_billing_prompt_tokens",
    "free_output_allowance",
    "legacy_context_multiplier",
    "matched_targeting_applies",
    "modelled_work",
    "prefill_work",
    "quote_text_generation",
    "reference_prompt_tokens",
    "retained_prompt_tokens",
]

TEXT_TARIFF_VERSION = "text-tariff-3"
"""Identifies the tariff in the dry-run quote cache key, so a quote computed under another tariff is never reused."""

BILLING_CHARS_PER_TOKEN = 4
"""Characters per estimated token for the prompt and output operands.

Distinct from the context-fit estimate in ``horde.classes.kobold.request_fit``, which is deliberately
conservative so that an over-long prompt is detected before it is sent. On Llama-family tokenizers
Latin-script prose runs near 4.9 characters per token, so this estimate overstates such prompts by
about a fifth; dense scripts such as Chinese or Japanese run under two characters per token and are
understated by half or more.
"""

MIN_PRICED_TOKENS = 20
"""Floor on a non-empty output's token count, so a trivial reply still prices as a job."""

MIN_OUTPUT_TOKENS = 16
"""The smallest ``max_length`` the API accepts; a free-tier allowance below it means no free allowance."""

PRICED_PROMPT_TOKENS_CEILING = 16384
"""Prompt tokens beyond this price alike.

A bound on exposure in the same role as the legacy curve's 30x cap: the largest context the work
model was measured over, past which the quadratic term is an extrapolation.
"""

MIN_RETAINED_CONTEXT_FRACTION = 5
"""The measured backend keeps at least this fraction of the context (one part in five) for the prompt.

When ``max_length`` would leave less, the backend lowers ``max_length`` instead of truncating the
prompt further, so the prompt the worker processes is never shorter than a fifth of the context.
"""

MATCHED_TARGETING_ENV = "HORDE_REQUIRE_MATCHED_TARGETING"
MATCHED_TARGETING_REWARD = 0.1
"""Reward for a job whose request named specific workers while matched targeting is required."""

UNKNOWN_MODEL_BILLIONS = 2.7
"""A trusted owner serving a model the reference does not know is paid as if it were this size."""
UNKNOWN_MODEL_DIVISOR = 100
PARAMETER_BONUS_BASE_BILLIONS = 13
PARAMETER_BONUS_EXPONENT = 0.20
KNOWN_MODEL_DIVISOR = 125
UNVALIDATED_BACKEND_FACTOR = 0.3
"""A worker on a backend outside ``LLM_VALIDATED_BACKENDS`` keeps this fraction of the known-model reward."""

DEFAULT_QUOTE_MODEL_BILLIONS = 13.0
"""A request that names no model is quoted, and held against, as if a model of this size will serve it."""

FREE_PROMPT_TOKENS_ENV = "HORDE_TEXT_FREE_PROMPT_TOKENS"
DEFAULT_FREE_PROMPT_TOKENS = 4096
"""The prompt size the free-tier output allowance is budgeted for.

The historical upfront hold of ``max_length * parameters / 21`` equals the legacy settlement curve at a
prompt of about 4,030 estimated tokens, which is the only record of the prompt size that hold was
sized for.
"""

LEGACY_CONTEXT_BASE_TOKENS = 1024
LEGACY_CONTEXT_MULTIPLIER_OFFSET = 1.2
LEGACY_CONTEXT_MULTIPLIER_GROWTH = 2.2
LEGACY_CONTEXT_MULTIPLIER_CAP = 30.0

REFERENCE_PROMPT_TOKENS = 1024
REFERENCE_OUTPUT_TOKENS = 512
"""The job at which the work model is pinned to the legacy curve's level."""


@dataclass(frozen=True)
class TextWorkRates:
    """Represents the measured cost shape of a text backend, in units of one generated token at an empty context.

    Attributes:
        input_rate: Units per prompt token for prompt processing.
        input_quadratic_rate: Units per squared prompt token, the attention term of prompt processing.
        context_decode_rate: Extra units each prompt token adds to every generated token.
        provenance: Where the rates were measured, so a recalibration can be compared with them.
    """

    input_rate: float
    input_quadratic_rate: float
    context_decode_rate: float
    provenance: str


PROVISIONAL_RATES = TextWorkRates(
    input_rate=0.17466,
    input_quadratic_rate=1.960e-05,
    context_decode_rate=2.521e-04,
    provenance=(
        "KoboldCpp v1.123, Llama-3.2-1B-Instruct Q4_K_M, 4 CPU threads, no GPU, blas batch 512, "
        "context 16384, prompts of 256 to 12,288 tokens, 128 generated; fitted by tools/text_tariff_bench.py report"
    ),
)
"""Rates from a CPU measurement, pending a GPU calibration through docs/how-to/calibrate_text_tariff.md.

Batched prompt processing is relatively cheaper on a GPU than on a CPU, so these rates are expected
to overstate the input share of a job on the workers that serve the Horde; the shape and the
whole-job level at 512 generated tokens are what they establish.
"""


def estimate_billing_prompt_tokens(prompt: str | None) -> int:
    """Return the tariff token estimate for the prompt as it is sent to the worker.

    The estimate depends on the prompt alone; ``retained_prompt_tokens`` applies the context the
    request runs under.

    Args:
        prompt: The effective prompt, after any style has been applied.

    Returns:
        ``ceil(len(prompt) / 4)``, or 0 for a missing or empty prompt.
    """
    if not prompt:
        return 0
    return math.ceil(len(prompt) / BILLING_CHARS_PER_TOKEN)


def retained_prompt_tokens(prompt_tokens: int, *, max_context_length: int, max_length: int) -> int:
    """Return the part of the prompt the worker processes.

    A worker takes a request only when its own context is at least the requested
    ``max_context_length``, so the request's context is the one the prompt is fitted to. The measured
    backend keeps the last ``max_context_length - max_length`` tokens of a prompt that does not fit,
    and lowers ``max_length`` rather than leave the prompt less than a fifth of the context. Both are
    known from the request alone, so the truncation is anticipated here instead of paid for.

    The estimate is in tariff tokens and the context in model tokens; the cap only ever lowers the
    estimate, so a prompt the estimate overstates is brought nearer the worker's count, never further.

    Args:
        prompt_tokens: The estimate from ``estimate_billing_prompt_tokens``.
        max_context_length: The context the request runs under, after any growth.
        max_length: The output allowance the request runs under, after any downgrade.

    Returns:
        ``min(prompt_tokens, max(max_context_length - max_length, ceil(max_context_length / 5)))``,
        never negative.
    """
    kept = max(max_context_length - max_length, math.ceil(max_context_length / MIN_RETAINED_CONTEXT_FRACTION))
    return max(0, min(prompt_tokens, kept))


def billing_prompt_tokens(prompt: str | None, *, max_context_length: int, max_length: int) -> int:
    """Return the prompt operand for a request: the estimate of the prompt, capped at what is processed.

    Args:
        prompt: The effective prompt, after any style has been applied.
        max_context_length: The context the request runs under.
        max_length: The output allowance the request runs under.

    Returns:
        ``retained_prompt_tokens(estimate_billing_prompt_tokens(prompt), ...)``.
    """
    return retained_prompt_tokens(
        estimate_billing_prompt_tokens(prompt),
        max_context_length=max_context_length,
        max_length=max_length,
    )


def count_output_tokens(generation: str | None, output_allowance: int) -> int:
    """Return the tariff token count for a returned generation.

    Args:
        generation: The text the worker returned, or ``None`` when the job has not completed.
        output_allowance: The request's ``max_length``.

    Returns:
        0 when there is no generation; otherwise the character estimate, floored at
        ``MIN_PRICED_TOKENS`` and capped at ``output_allowance``.
    """
    if generation is None:
        return 0
    estimated_tokens = max(MIN_PRICED_TOKENS, math.ceil(len(generation) / BILLING_CHARS_PER_TOKEN))
    return min(output_allowance, estimated_tokens)


def legacy_context_multiplier(prompt_tokens: int) -> float:
    """Return the capped power-law multiplier of the tariff before the input term existed.

    Kept for the one branch that still pays it, an unknown model served by an untrusted owner, which
    is paid this flat amount whatever the job returned.

    Args:
        prompt_tokens: The prompt estimate from ``estimate_billing_prompt_tokens``.

    Returns:
        ``1.2 + (max(20, prompt_tokens) / 1024) ** log2(2.2)``, at most 30.
    """
    priced_tokens = max(MIN_PRICED_TOKENS, prompt_tokens)
    multiplier = float(
        LEGACY_CONTEXT_MULTIPLIER_OFFSET + LEGACY_CONTEXT_MULTIPLIER_GROWTH ** math.log2(priced_tokens / LEGACY_CONTEXT_BASE_TOKENS),
    )
    return min(LEGACY_CONTEXT_MULTIPLIER_CAP, multiplier)


def _priced_prompt_tokens(prompt_tokens: int) -> int:
    return max(0, min(PRICED_PROMPT_TOKENS_CEILING, prompt_tokens))


def prefill_work(prompt_tokens: int, rates: TextWorkRates = PROVISIONAL_RATES) -> float:
    """Return the work of processing the prompt, in generated-token units.

    Args:
        prompt_tokens: The prompt estimate from ``estimate_billing_prompt_tokens``.
        rates: The backend cost shape.

    Returns:
        ``input_rate * P + input_quadratic_rate * P^2`` for ``P`` capped at the ceiling.
    """
    priced = _priced_prompt_tokens(prompt_tokens)
    return rates.input_rate * priced + rates.input_quadratic_rate * priced * priced


def decode_work(prompt_tokens: int, output_tokens: int, rates: TextWorkRates = PROVISIONAL_RATES) -> float:
    """Return the work of generating the output over the prompt's context, in generated-token units.

    Args:
        prompt_tokens: The prompt estimate from ``estimate_billing_prompt_tokens``.
        output_tokens: The output operand from ``count_output_tokens``.
        rates: The backend cost shape.

    Returns:
        ``O * (1 + context_decode_rate * P)`` for ``P`` capped at the ceiling.
    """
    priced = _priced_prompt_tokens(prompt_tokens)
    return output_tokens * (1 + rates.context_decode_rate * priced)


def modelled_work(prompt_tokens: int, output_tokens: int, rates: TextWorkRates = PROVISIONAL_RATES) -> float:
    """Return the whole job's work, prompt processing plus generation, in generated-token units."""
    return prefill_work(prompt_tokens, rates) + decode_work(prompt_tokens, output_tokens, rates)


WORK_UNIT_SCALE = (
    REFERENCE_OUTPUT_TOKENS
    * legacy_context_multiplier(REFERENCE_PROMPT_TOKENS)
    / modelled_work(REFERENCE_PROMPT_TOKENS, REFERENCE_OUTPUT_TOKENS)
)
"""Converts work units to the legacy curve's operand ``O * multiplier`` at the reference job.

With it, the known-model and trusted-unknown branches pay exactly what they did before the input
term existed for a 512-token job on a 1,024-token prompt, and every other job is priced relative to
that one by the measured shape.
"""


def matched_targeting_applies(targeted_worker_count: int) -> bool:
    """Report whether the matched-targeting flat reward applies to a request.

    Args:
        targeted_worker_count: How many workers the request named.

    Returns:
        True when ``HORDE_REQUIRE_MATCHED_TARGETING`` is set and the request named at least one worker.
    """
    return os.getenv(MATCHED_TARGETING_ENV, "0") == "1" and targeted_worker_count > 0


def _scaled_work(prompt_tokens: int, output_tokens: int, empty_output: bool) -> float:
    """Return the job's work in legacy operand units, withholding the prompt term from an empty return.

    A submission with no visible text cannot be told from a worker that skipped the job, so it is paid
    the output floor over the prompt's context and nothing for the prompt itself.
    """
    generation = decode_work(prompt_tokens, output_tokens)
    if empty_output:
        return WORK_UNIT_SCALE * generation
    return WORK_UNIT_SCALE * (prefill_work(prompt_tokens) + generation)


def calculate_text_generation_reward(
    *,
    output_tokens: int | None,
    prompt_tokens: int,
    model_multiplier: float | None,
    owner_trusted: bool,
    backend_validated: bool,
    matched_targeting: bool,
    empty_output: bool = False,
) -> float:
    """Return the kudos a text job earns its worker.

    Branches and rounding are the settlement policy: a known model rounds to two decimals after the
    backend factor; an unknown model served by a trusted owner is paid unrounded as a 2.7B model; an
    unknown model served by an untrusted owner is paid the legacy context multiplier alone, whatever
    was returned. A job nothing was submitted for pays nothing on the first two branches.

    Args:
        output_tokens: The output operand from ``count_output_tokens``, the allowance for a quote, or
            ``None`` when nothing was submitted (a cancelled job).
        prompt_tokens: The prompt operand from ``estimate_billing_prompt_tokens``.
        model_multiplier: The model's parameter count in billions, or ``None`` for an unknown model.
        owner_trusted: Whether the worker's owner is trusted.
        backend_validated: Whether the worker's bridge is a validated backend.
        matched_targeting: Whether the matched-targeting flat reward applies.
        empty_output: Whether the submission carried no visible text; the prompt term is then withheld.

    Returns:
        The nominal reward before owner escrow, bridge multipliers and requester burns.
    """
    if matched_targeting:
        return MATCHED_TARGETING_REWARD
    if model_multiplier is None and not owner_trusted:
        return legacy_context_multiplier(prompt_tokens)
    if output_tokens is None:
        return 0.0
    work = _scaled_work(prompt_tokens, output_tokens, empty_output)
    if model_multiplier is None:
        return work * (UNKNOWN_MODEL_BILLIONS / UNKNOWN_MODEL_DIVISOR)
    size_ratio = max(model_multiplier, PARAMETER_BONUS_BASE_BILLIONS) / PARAMETER_BONUS_BASE_BILLIONS
    parameter_bonus = float(size_ratio**PARAMETER_BONUS_EXPONENT)
    kudos = work * parameter_bonus * model_multiplier / KNOWN_MODEL_DIVISOR
    if not backend_validated:
        kudos *= UNVALIDATED_BACKEND_FACTOR
    return round(kudos, 2)


def quote_text_generation(
    *,
    output_allowance: int,
    prompt_tokens: int,
    model_multiplier: float | None,
    matched_targeting: bool,
) -> float:
    """Return the nominal per-job price of a request before any worker has taken it.

    The quote assumes the request generates its whole ``max_length`` on a validated backend owned by
    a trusted user. Settlement differs from it only through what the worker actually returned and
    which worker served the job.

    Args:
        output_allowance: The request's ``max_length``.
        prompt_tokens: The prompt operand from ``estimate_billing_prompt_tokens``.
        model_multiplier: The largest parameter count among the request's known models, in billions;
            ``DEFAULT_QUOTE_MODEL_BILLIONS`` when it names none; ``None`` when it names only unknown ones.
        matched_targeting: Whether the matched-targeting flat reward applies.

    Returns:
        The per-job quote, before the per-job burn and the request tax.
    """
    return calculate_text_generation_reward(
        output_tokens=output_allowance,
        prompt_tokens=prompt_tokens,
        model_multiplier=model_multiplier,
        owner_trusted=True,
        backend_validated=True,
        matched_targeting=matched_targeting,
    )


def reference_prompt_tokens() -> int:
    """Return the prompt size the free-tier output allowance is budgeted for.

    Returns:
        ``HORDE_TEXT_FREE_PROMPT_TOKENS`` when it is set to a positive integer, else
        ``DEFAULT_FREE_PROMPT_TOKENS``.
    """
    configured = os.getenv(FREE_PROMPT_TOKENS_ENV)
    if configured is None:
        return DEFAULT_FREE_PROMPT_TOKENS
    try:
        reference = int(configured)
    except ValueError:
        logger.warning(f"{FREE_PROMPT_TOKENS_ENV}={configured!r} is not an integer; using {DEFAULT_FREE_PROMPT_TOKENS}")
        return DEFAULT_FREE_PROMPT_TOKENS
    if reference <= 0:
        logger.warning(f"{FREE_PROMPT_TOKENS_ENV}={configured!r} is not positive; using {DEFAULT_FREE_PROMPT_TOKENS}")
        return DEFAULT_FREE_PROMPT_TOKENS
    return reference


def _allowance_within_budget(prompt_tokens: int, free_output_tokens: int, reference_prompt_tokens: int) -> int:
    """Return the largest output, at most ``free_output_tokens``, whose work with this prompt fits the free budget."""
    if prompt_tokens <= reference_prompt_tokens:
        return free_output_tokens
    budget = modelled_work(reference_prompt_tokens, free_output_tokens)
    remaining = budget - prefill_work(prompt_tokens)
    if remaining <= 0:
        return 0
    allowance = math.floor(remaining / decode_work(prompt_tokens, 1))
    if allowance < MIN_OUTPUT_TOKENS:
        return 0
    return min(free_output_tokens, allowance)


def free_output_allowance(
    *,
    prompt_tokens: int,
    free_output_tokens: int,
    reference_prompt_tokens: int,
    max_context_length: int | None = None,
) -> int:
    """Return how many output tokens a prompt may generate without an upfront hold.

    The free tier budgets ``free_output_tokens`` of output against a prompt of
    ``reference_prompt_tokens``. A longer prompt spends part of that budget on its own processing and
    makes each generated token dearer, so its allowance is an output whose modelled work stays within
    the budget.

    With ``max_context_length`` given, the prompt operand is what the context keeps at the allowance
    granted (``retained_prompt_tokens``): a smaller allowance leaves more context to the prompt, which
    can in turn cost more to process. The allowance is lowered until it and the prompt it implies fit
    the budget together, so a downgrade to the returned allowance prices at settlement what the gate
    admitted.

    Args:
        prompt_tokens: The prompt estimate from ``estimate_billing_prompt_tokens``.
        free_output_tokens: The output allowance the queue and worker supply currently permit.
        reference_prompt_tokens: The prompt size that allowance is budgeted for.
        max_context_length: The context the request runs under, or ``None`` to price the estimate as is.

    Returns:
        ``free_output_tokens`` for a prompt at or under the reference; a smaller allowance otherwise;
        0 when even ``MIN_OUTPUT_TOKENS`` of output would exceed the budget, meaning the request
        cannot be made free by generating less.
    """

    def priced(output_tokens: int) -> int:
        if max_context_length is None:
            return prompt_tokens
        return retained_prompt_tokens(prompt_tokens, max_context_length=max_context_length, max_length=output_tokens)

    allowance = free_output_tokens
    while allowance > 0:
        # The prompt operand never shrinks as the allowance falls, so each pass can only lower the
        # allowance further or confirm it; the loop ends at the first allowance that confirms itself.
        fitted = _allowance_within_budget(priced(allowance), free_output_tokens, reference_prompt_tokens)
        if fitted >= allowance:
            return allowance
        allowance = fitted
    return 0

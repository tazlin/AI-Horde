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

The context multiplier (``context_multiplier``) is a capped power law in the prompt estimate:
``1.2 + (prompt_tokens / 1024) ** log2(2.2)``, held to at most 30. It is not exponential in the
prompt and does not describe memory growth; it is the existing tariff curve, kept so that the
only change against the previous settlement is that the prompt operand is no longer capped by the
output allowance.

``calculate_text_generation_reward`` is the settlement kernel. ``quote_text_generation`` applies
the same kernel under the assumptions a quote has to make before a worker is chosen.
``free_output_allowance`` turns the free-tier output budget into a budget on modelled work, so a
long prompt lowers how many tokens may be generated without an upfront hold.
"""

from __future__ import annotations

import math
import os

from horde.logger import logger

__all__ = [
    "BILLING_CHARS_PER_TOKEN",
    "CONTEXT_MULTIPLIER_CAP",
    "DEFAULT_FREE_PROMPT_TOKENS",
    "DEFAULT_QUOTE_MODEL_BILLIONS",
    "FREE_PROMPT_TOKENS_ENV",
    "MATCHED_TARGETING_ENV",
    "MATCHED_TARGETING_REWARD",
    "MIN_OUTPUT_TOKENS",
    "MIN_PRICED_TOKENS",
    "TEXT_TARIFF_VERSION",
    "billing_prompt_tokens",
    "calculate_text_generation_reward",
    "context_multiplier",
    "count_output_tokens",
    "estimate_billing_prompt_tokens",
    "free_output_allowance",
    "matched_targeting_applies",
    "quote_text_generation",
    "reference_prompt_tokens",
    "retained_prompt_tokens",
]

TEXT_TARIFF_VERSION = "text-tariff-2"
"""Identifies the tariff in the dry-run quote cache key, so a quote computed under another tariff is never reused."""

BILLING_CHARS_PER_TOKEN = 4
"""Characters per estimated token for the prompt and output operands.

Distinct from the context-fit estimate in ``horde.classes.kobold.request_fit``, which is deliberately
conservative so that an over-long prompt is detected before it is sent.
"""

MIN_PRICED_TOKENS = 20
"""Floor on a non-empty operand's token estimate, so a trivial prompt or output still prices as a job."""

MIN_OUTPUT_TOKENS = 16
"""The smallest ``max_length`` the API accepts; a free-tier downgrade never goes below it."""

CONTEXT_BASE_TOKENS = 1024
CONTEXT_MULTIPLIER_OFFSET = 1.2
CONTEXT_MULTIPLIER_GROWTH = 2.2
CONTEXT_MULTIPLIER_CAP = 30.0

MIN_RETAINED_CONTEXT_FRACTION = 5
"""The measured backend keeps at least this fraction of the context (one part in five) for the prompt.

When ``max_length`` would leave less, the backend lowers ``max_length`` instead of truncating the
prompt further, so the prompt the worker processes is never shorter than a fifth of the context.
"""
"""Ceiling on the context multiplier; prompts past roughly 19,650 estimated tokens all price alike."""

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

The historical upfront hold of ``max_length * parameters / 21`` equals the settlement kernel at a prompt
of about 4,030 estimated tokens, which is the only record of the prompt size that hold was sized for.
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


def context_multiplier(prompt_tokens: int) -> float:
    """Return the capped context multiplier for a prompt estimate.

    Args:
        prompt_tokens: The prompt estimate from ``estimate_billing_prompt_tokens``.

    Returns:
        ``1.2 + (max(20, prompt_tokens) / 1024) ** log2(2.2)``, at most ``CONTEXT_MULTIPLIER_CAP``.
    """
    priced_tokens = max(MIN_PRICED_TOKENS, prompt_tokens)
    multiplier = float(CONTEXT_MULTIPLIER_OFFSET + CONTEXT_MULTIPLIER_GROWTH ** math.log2(priced_tokens / CONTEXT_BASE_TOKENS))
    return min(CONTEXT_MULTIPLIER_CAP, multiplier)


def matched_targeting_applies(targeted_worker_count: int) -> bool:
    """Report whether the matched-targeting flat reward applies to a request.

    Args:
        targeted_worker_count: How many workers the request named.

    Returns:
        True when ``HORDE_REQUIRE_MATCHED_TARGETING`` is set and the request named at least one worker.
    """
    return os.getenv(MATCHED_TARGETING_ENV, "0") == "1" and targeted_worker_count > 0


def calculate_text_generation_reward(
    *,
    output_tokens: int,
    prompt_tokens: int,
    model_multiplier: float | None,
    owner_trusted: bool,
    backend_validated: bool,
    matched_targeting: bool,
) -> float:
    """Return the kudos a text job earns its worker.

    The branches and their rounding are the settlement policy: a known model rounds to two decimals
    after the backend factor and the context multiplier are applied; an unknown model served by a
    trusted owner is paid unrounded as a 2.7B model; an unknown model served by an untrusted owner is
    paid the context multiplier alone, whatever was returned.

    Args:
        output_tokens: The output operand from ``count_output_tokens``, or the allowance for a quote.
        prompt_tokens: The prompt operand from ``estimate_billing_prompt_tokens``.
        model_multiplier: The model's parameter count in billions, or ``None`` for an unknown model.
        owner_trusted: Whether the worker's owner is trusted.
        backend_validated: Whether the worker's bridge is a validated backend.
        matched_targeting: Whether the matched-targeting flat reward applies.

    Returns:
        The nominal reward before owner escrow, bridge multipliers and requester burns.
    """
    if matched_targeting:
        return MATCHED_TARGETING_REWARD
    multiplier = context_multiplier(prompt_tokens)
    if model_multiplier is None:
        if not owner_trusted:
            return multiplier
        return output_tokens * multiplier * (UNKNOWN_MODEL_BILLIONS / UNKNOWN_MODEL_DIVISOR)
    size_ratio = max(model_multiplier, PARAMETER_BONUS_BASE_BILLIONS) / PARAMETER_BONUS_BASE_BILLIONS
    parameter_bonus = float(size_ratio**PARAMETER_BONUS_EXPONENT)
    kudos = output_tokens * parameter_bonus * model_multiplier / KNOWN_MODEL_DIVISOR
    if not backend_validated:
        kudos *= UNVALIDATED_BACKEND_FACTOR
    return round(kudos * multiplier, 2)


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
    """Return the largest output, at most ``free_output_tokens``, whose multiplied work with this prompt fits the free budget."""
    if prompt_tokens <= reference_prompt_tokens:
        return free_output_tokens
    budget = free_output_tokens * context_multiplier(reference_prompt_tokens)
    allowance = math.floor(budget / context_multiplier(prompt_tokens))
    return max(MIN_OUTPUT_TOKENS, min(free_output_tokens, allowance))


def free_output_allowance(
    *,
    prompt_tokens: int,
    free_output_tokens: int,
    reference_prompt_tokens: int,
    max_context_length: int | None = None,
) -> int:
    """Return how many output tokens a prompt may generate without an upfront hold.

    The free tier budgets ``free_output_tokens`` of output against a prompt of
    ``reference_prompt_tokens``. A longer prompt spends part of that budget on the context multiplier,
    so its allowance is an output for which ``output * multiplier(prompt)`` stays within
    ``free_output_tokens * multiplier(reference)``.

    With ``max_context_length`` given, the prompt operand is what the context keeps at the allowance
    granted (``retained_prompt_tokens``): a smaller allowance leaves more context to the prompt, which
    can in turn raise its multiplier. The allowance is lowered until it and the prompt it implies fit
    the budget together, so a downgrade to the returned allowance prices at settlement what the gate
    admitted.

    Args:
        prompt_tokens: The prompt estimate from ``estimate_billing_prompt_tokens``.
        free_output_tokens: The output allowance the queue and worker supply currently permit.
        reference_prompt_tokens: The prompt size that allowance is budgeted for.
        max_context_length: The context the request runs under, or ``None`` to price the estimate as is.

    Returns:
        ``free_output_tokens`` for a prompt at or under the reference; otherwise a smaller allowance,
        never below ``MIN_OUTPUT_TOKENS``.
    """

    def priced(output_tokens: int) -> int:
        if max_context_length is None:
            return prompt_tokens
        return retained_prompt_tokens(prompt_tokens, max_context_length=max_context_length, max_length=output_tokens)

    allowance = free_output_tokens
    while True:
        # The prompt operand never shrinks as the allowance falls, so each pass can only lower the
        # allowance further or confirm it; the loop ends at the first allowance that confirms itself.
        fitted = _allowance_within_budget(priced(allowance), free_output_tokens, reference_prompt_tokens)
        if fitted >= allowance:
            return allowance
        allowance = fitted

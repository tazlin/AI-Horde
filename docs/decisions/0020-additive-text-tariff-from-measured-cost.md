---
status: accepted
date: 2026-10-14
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Price text jobs by an additive work model with measured rates

## Context and Problem Statement

[ADR 0019](0019-price-text-input-as-processed.md) made the prompt the worker processes the input
operand and gave quote and settlement one kernel, but kept the legacy curve, `reward = O * M(P)`: the
prompt only scales the output reward. Measured against a transformer backend, that shape tracks the total time of a 512-token
job within about 10% across the whole context window, and fails on short outputs: a 64-token reply to a
10,000-token prompt is paid 0.18 of what its time warrants, a 20-token reply 0.06, and a 1,024-token
reply to a 4,096-token prompt 1.5 times. The worker processes the prompt once whatever it returns, and
the multiplicative form cannot express that.

The measurement (`tools/text_tariff_bench.py`, KoboldCpp, Llama 3.2 1B, CPU) fits prefill time as
`a + b * P + c * P^2` with R² 0.999 and decode time per token as `d + e * P` with R² 0.993. The shape
transfers to GPU workers; the constants do not, and no GPU measurement exists yet.

## Decision Drivers

- A worker is paid for processing the prompt independently of how much it returns.
- A 512-token job on a 1,024-token prompt keeps its price, so the typical job is not repriced.
- Rates come from a measurement with recorded provenance and a documented replacement procedure.
- An empty return must not become a way to collect the prompt term for work the server cannot verify.
- Exposure stays bounded as the legacy 30x cap bounded it.
- The free-tier gate follows the same model, so admission and price agree on what a prompt costs.

## Considered Options

- Additive work model with the measured CPU rates, pinned to the legacy level at the reference job
- Additive work model with rates fitted to the legacy curve at two prompt sizes
- Additive work model with invented GPU-plausible rates
- Keep the multiplicative curve until a GPU measurement exists

## Decision Outcome

Chosen option: "Additive work model with the measured CPU rates, pinned to the legacy level at the
reference job".

Work is `input_rate * P + input_quadratic_rate * P^2 + O * (1 + context_decode_rate * P)` in units of
one generated token at an empty context, with `P` capped at 16,384. The rates are the fitted CPU
values (0.17466, 0.0000196, 0.0002521), recorded with their provenance in `PROVISIONAL_RATES`.
`WORK_UNIT_SCALE` converts units to the legacy operand so that `work(1024, 512)` equals `512 * 2.2`;
the model, backend and trust factors are unchanged. With that single anchor, 512-token jobs land
within 13% of the legacy price from 1K to 16K prompts, which is taken as evidence that the measured
shape reproduces the level the service already ran at; the differences fall on short and long outputs,
where the legacy form was wrong by construction.

Two guards: a job nothing was submitted for pays nothing, as before; and an empty submission is paid
the output floor over its context but not the prompt term. The unknown-model, untrusted-owner branch
keeps its flat legacy amount.

The free-tier allowance is the largest output whose work fits the budget `work(reference, allowance)`;
when prompt processing alone exceeds it the allowance is 0, the request needs a hold however little it
would generate, and a downgrade cannot cure it. The refusal says so.

### Consequences

- Good: A 20-token reply to a 10,000-token prompt on 13B pays 524.51 instead of 30.28; a 300-token
  reply 661.41 instead of 454.25. A 1,024-token reply to a 1,024-token prompt pays 206.60 instead of
  234.29.
- Good: Quote, hold, gate and settlement share the work model; the sharp edges are documented in one
  reference page.
- Bad: The rates are from a CPU, where batched prompt processing is relatively dearer than on a GPU.
  The input share is probably overstated on Horde workers until a GPU calibration replaces the values.
- Bad: With the provisional rates, prompts past about 7,000 estimated tokens have no free allowance at
  the default reference; kudos-less requesters with such prompts are refused where they were previously
  downgraded. `HORDE_TEXT_FREE_PROMPT_TOKENS` widens the tier.
- Bad: A one-word non-empty reply to a long prompt pays the prompt term in full (over a thousand kudos
  at the ceiling on 13B); only a whitespace-only return is held to the floor.
- Bad: Prompts beyond 16,384 estimated tokens price alike, a lower ceiling than the legacy curve's
  19,646.

## Pros and Cons of the Options

### Additive work model with rates fitted to the legacy curve at two prompt sizes

- Good: Keeps 512-token jobs closer to the legacy price at every prompt size.
- Bad: Uses the legacy curve as the calibration target when the legacy curve is what the measurement
  showed to be wrong in shape; the implied input share (about 0.7 of the measured one) has no
  provenance of its own.

### Additive work model with invented GPU-plausible rates

- Good: Would likely land nearer the real input share on Horde workers.
- Bad: No measurement backs the number, so neither the rates nor the point at which to replace them
  can be stated.

### Keep the multiplicative curve until a GPU measurement exists

- Good: No new repricing.
- Bad: Leaves the short-output underpayment, the reason the tariff was reopened, in place for as long
  as nobody runs the calibration.

## Confirmation

`tests/unit/test_text_pricing.py` fixes the work model, the level continuity, every branch, the empty
and absent guards and the allowance, including the no-allowance case.
`tests/unit/test_text_pricing_examples.py` carries worked examples a reader can follow.
`tests/unit/test_kobold_text_procgen.py` and `tests/unit/test_kobold_text_wp.py` fix the adapters;
`tests/integration/test_text_kudos_settlement.py` fixes the two refusal outcomes and that hold, dry run
and settlement agree. `docs/reference/text_kudos_pricing.md` records the tariff and
`docs/explanation/text_kudos_pricing.md` the measurements behind it.

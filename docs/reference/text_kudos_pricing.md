---
title: "Text kudos pricing reference"
summary: "The tariff a text job is priced under: the prompt and output operands, the work model and its rates, each settlement branch, the quote, the hold, the free-tier allowance and the quote cache key."
topics: [kudos, generation]
order: 95
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Text kudos pricing reference

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [generation](../topics.md#generation), [kudos](../topics.md#kudos)
<!-- END GENERATED: topics -->

A text job is priced from three operands the server computes itself: the part of the prompt the worker
processes, the text the worker returned, and the reference size of the model that served it. The tariff is a set
of pure functions in `horde/classes/kobold/pricing.py`; the dry-run quote, the accepted response, the
upfront hold and the settlement on submit all call it, so a quote and a settlement differ only through
what the worker returned and which worker served the job. The free-tier admission gate uses the same
work model to turn its output allowance into a budget. The reasoning behind the shape is in
[ADR 0019](../decisions/0019-price-text-input-as-processed.md) and
[ADR 0020](../decisions/0020-additive-text-tariff-from-measured-cost.md), the measurements behind the
rates in [Why text jobs are priced this way](../explanation/text_kudos_pricing.md), and how the
resulting kudos are recorded in the [kudos accounting reference](kudos_accounting.md).

## Operands

| Operand | Source | Estimate |
| --- | --- | --- |
| Prompt | `WaitingPrompt.prompt`, the text put in the worker payload after any style template | `ceil(len(prompt) / 4)`, 0 for an empty prompt, capped at `max(max_context_length - max_length, ceil(max_context_length / 5))` |
| Output | The generation the worker submitted | `ceil(len(generation) / 4)`, floored at 20, capped at the request's `max_length`; absent when nothing was submitted |
| Model | `ModelReference.get_text_model_multiplier` for the model the worker reported | Parameter count in billions, or unknown when the reference has no entry |

The prompt estimate counts code points, not model tokens. The cap is what the worker processes: a
worker takes a request only when its own context is at least the requested `max_context_length`, and
the measured backend keeps the last `max_context_length - max_length` tokens of a prompt that does not
fit, lowering `max_length` rather than leave the prompt less than a fifth of the context. Under
`context_fit=grow` the context is raised until the prompt fits, so the cap never binds; under `reject`
an over-long prompt is refused; under `ignore`, the default, the prompt is priced as the worker will
see it. While the prompt fits, `max_length` and `max_context_length` do not change its price. The
estimate is distinct from the context-fit estimate in `horde/classes/kobold/request_fit.py`, which
divides by three so that an over-long prompt is detected conservatively.

On Llama-family tokenizers, Latin-script prose runs near 4.9 characters per token, so the prompt
estimate overstates such prompts by about a fifth. Chinese and Japanese run at 1.3 to 1.9 characters per
token and are understated by half or more.

## Work model

A job's work is counted in units of one generated token at an empty context:

```text
work(P, O) = input_rate * P + input_quadratic_rate * P^2 + O * (1 + context_decode_rate * P)
P = prompt tokens, capped at 16,384        O = output tokens
```

The first two terms are prompt processing, paid once per job however little is returned. The last is
generation, each token of which attends over the whole context. The rates are a `TextWorkRates` record
with its provenance:

| Rate | Value | Meaning |
| --- | --- | --- |
| `input_rate` | 0.17466 | Units per prompt token |
| `input_quadratic_rate` | 0.0000196 | Units per squared prompt token |
| `context_decode_rate` | 0.0002521 | Extra units per generated token, per prompt token of context |

`PROVISIONAL_RATES` were measured on KoboldCpp with a 1B model on four CPU threads, where batched prompt
processing is relatively dearer than on a GPU; they are expected to overstate the input share on the
workers that serve the Horde and are replaced through
[Calibrate the text tariff](../how-to/calibrate_text_tariff.md).

Units convert to the legacy curve's operand through `WORK_UNIT_SCALE` (1.33527), chosen so that
`work(1024, 512)` equals `512 * 2.2`: a 512-token job on a 1,024-token prompt prices exactly as it did
under the previous curve, and every other job is priced relative to it by the measured shape.

| Prompt tokens | Units to process | Units per generated token |
| --- | --- | --- |
| 0 | 0 | 1.00 |
| 1,024 | 199.4 | 1.26 |
| 4,096 | 1,044.2 | 2.03 |
| 10,000 | 3,706.6 | 3.52 |
| 16,384 and above | 8,124.0 | 5.13 |

## Settlement branches

`TextProcessingGeneration.get_gen_kudos` supplies the operands; `calculate_text_generation_reward`
applies the first matching row. `W` is `WORK_UNIT_SCALE * work(P, O)`, `B` the model size in billions.

| Condition | Reward |
| --- | --- |
| `HORDE_REQUIRE_MATCHED_TARGETING=1` and the request named workers | `0.1` |
| Unknown model, untrusted owner | The legacy multiplier `min(30, 1.2 + (max(20, P) / 1024) ** log2(2.2))`, whatever was returned |
| Nothing submitted (a cancelled job) | `0` |
| Unknown model, trusted owner | `W * 2.7 / 100`, unrounded |
| Known model | `round(W * (max(B, 13) / 13) ** 0.2 * B / 125 * V, 2)`, where `V` is 0.3 on a backend outside `LLM_VALIDATED_BACKENDS` and 1 otherwise |

An empty submission (no visible text, as `is_empty_text_generation` judges it) is paid the output floor
over the prompt's context and nothing for the prompt: a worker that returned nothing cannot be told
from one that skipped the job.

Worked values for a 13B model on a validated backend:

| Prompt tokens | Output tokens | Reward | Previous curve |
| --- | --- | --- | --- |
| 1,024 | 512 | 117.15 | 117.15 |
| 4,096 | 512 | 289.53 | 321.62 |
| 10,000 | 512 | 765.07 | 775.26 |
| 16,384 and above | 512 | 1,492.79 | 1,597.44 at the cap |
| 10,000 | 300 | 661.41 | 454.25 |
| 10,000 | 20 | 524.51 | 30.28 |
| 10,000 | 20, empty | 9.78 | 30.28 |
| 1,024 | 1,024 | 206.60 | 234.29 |
| 10,000 sent, 1,536 processed (2,048 context) | 512 | 142.31 | 181.04 |
| 10,000 sent, 410 processed (2,048 context, 4,096 allowed) | 512 | 88.85 | 88.10 |

The reward is nominal. Owner escrow (`User.record_contributions`), the bridge multiplier and the
requester's per-job burn of one kudos (`TextWaitingPrompt.calculate_extra_kudos_burn`) apply on top. A
cancelled job pays the worker nothing while the requester still pays the burn. A censored job pays the
worker and waives the requester's generation component.

## Quote

`TextWaitingPrompt.calculate_kudos` applies the same kernel with the output operand set to the whole
`max_length`, a validated backend and a trusted owner, and stores the result on `WaitingPrompt.kudos`.
The model operand is the largest size among the request's known models; a request naming no model is
priced as 13B; a request naming only unknown models takes the trusted-unknown branch. This is the same
model the hold covers, so listing a small model beside a large one does not lower the price.

The quote is computed in `TextWaitingPrompt.extract_params`, when the request is constructed, and again
after a downgrade. The accepted (202) response reports it rounded to a whole number. A dry run reports
`(quote + 1) * n + 1 + style surcharge`, rounded to a whole number: the per-job burn on each job, the
activation tax once, and the style author's reward when another user's style applied.

A settlement differs from the quote when the worker returns fewer tokens than `max_length`, when the
owner is untrusted or the backend unvalidated, or when a model other than the largest serves the job.

## Upfront hold and free-tier allowance

`TextAsyncGenerate.initiate_waiting_prompt` sizes the hold as `(quote + 1) * n`, rounded to two
decimals, and takes it through `reserve_kudos` under `upfront:<waiting-prompt-id>` when
`TextWaitingPrompt.require_upfront_kudos` says the request needs one.

The gate first sets an output allowance from supply and queue pressure:
`512 + 5 * active threads - round(0.9 * queued text requests)`, clamped to 256..512 when slow workers
are allowed. Excluding slow workers always requires a hold and returns the unclamped figure. The
allowance is then reduced for the prompt through `free_output_allowance`: the free budget is
`work(reference, allowance)`, and a longer prompt keeps the largest output whose work fits in it:

```text
allowance(P) = floor((work(reference, allowance) - prefill(P)) / (1 + context_decode_rate * P))
```

clamped to the queue allowance above and to 0 below the API minimum of 16. `P` is the prompt as the
worker will process it at the allowance granted; since a smaller allowance leaves more context to the
prompt, the gate lowers the allowance until it and the prompt it implies fit the budget together, so
settlement after a downgrade prices what the gate admitted. The reference prompt size is
`HORDE_TEXT_FREE_PROMPT_TOKENS`, read at each call, default 4096. A request whose `max_length` exceeds
the reduced allowance needs a hold. An allowance of 0 means the prompt's own processing exceeds the
budget; the request then needs a hold whatever it asks to generate, and `allow_downgrade` cannot cure it.

| Prompt tokens | Allowance at 512 | Allowance at 256 |
| --- | --- | --- |
| up to 4,096 | 512 | 256 |
| 5,000 | 319 | 89 |
| 6,000 | 131 | 0 |
| 7,000 and above | 0 | 0 |
| 10,000, reference raised to 8,192 | 172 | 0 |
| 50,000 sent into a 2,048 context | 512 | 256 |
| 5,000 sent into a 5,120 context | 370 | 0 |

When the hold cannot be taken and the request allows a downgrade, `TextWaitingPrompt.downgrade` sets
`max_length`, `things`, `params`, `gen_payload`, `total_usage` and `kudos` to the reduced allowance and
accepts slow workers. The prompt is never trimmed. Without a downgrade the request is refused with
`KudosUpfront`; the message states the prompt estimate, how much of it fits the requested context when
that is less, and the allowance it permits, or that the prompt alone exceeds the free budget.

## Quote cache

`TextAsyncGenerate.get_hashed_params_dict` keys the dry-run cache on the params, the requested models,
the extra-image count, whether a style was named, the prompt estimate and `TEXT_TARIFF_VERSION`; the
context and allowance that cap the estimate are among the params. The lookup runs before validation
on the request's own prompt and the store runs after it on the resolved prompt; the two are the same text for every request the cache may answer, because
`dry_run_answerable_from_cache` already excludes styles, template fields and any `context_fit` other than
`ignore`, and base validation leaves a text prompt as it arrived. A tariff change bumps the version so
no quote computed under the previous tariff is reused during the two-day TTL.

## Code map

| Concept | File | Symbol |
| --- | --- | --- |
| Prompt estimate and cap | `horde/classes/kobold/pricing.py` | `estimate_billing_prompt_tokens`, `retained_prompt_tokens`, `billing_prompt_tokens` |
| Output count | `horde/classes/kobold/pricing.py` | `count_output_tokens` |
| Work model | `horde/classes/kobold/pricing.py` | `prefill_work`, `decode_work`, `modelled_work`, `TextWorkRates` |
| Rates and level | `horde/classes/kobold/pricing.py` | `PROVISIONAL_RATES`, `WORK_UNIT_SCALE`, `PRICED_PROMPT_TOKENS_CEILING` |
| Legacy multiplier | `horde/classes/kobold/pricing.py` | `legacy_context_multiplier` |
| Settlement kernel | `horde/classes/kobold/pricing.py` | `calculate_text_generation_reward` |
| Quote kernel | `horde/classes/kobold/pricing.py` | `quote_text_generation` |
| Free-tier allowance | `horde/classes/kobold/pricing.py` | `free_output_allowance`, `reference_prompt_tokens` |
| Cache version | `horde/classes/kobold/pricing.py` | `TEXT_TARIFF_VERSION` |
| Settlement adapter | `horde/classes/kobold/processing_generation.py` | `TextProcessingGeneration.get_gen_kudos`, `get_things_count`, `is_empty_text_generation` |
| Quote adapter | `horde/classes/kobold/waiting_prompt.py` | `TextWaitingPrompt.calculate_kudos`, `extract_params` |
| Admission gate | `horde/classes/kobold/waiting_prompt.py` | `TextWaitingPrompt.require_upfront_kudos` |
| Downgrade | `horde/classes/kobold/waiting_prompt.py` | `TextWaitingPrompt.downgrade` |
| Hold and refusal | `horde/apis/v2/kobold.py` | `TextAsyncGenerate.initiate_waiting_prompt` |
| Cache key | `horde/apis/v2/kobold.py` | `TextAsyncGenerate.get_hashed_params_dict` |
| Dry-run total | `horde/classes/base/waiting_prompt.py` | `WaitingPrompt.extrapolate_dry_run_kudos` |
| Model size | `horde/model_reference.py` | `ModelReference.get_text_model_multiplier`, `is_known_text_model` |
| Backend validation | `horde/bridge_reference.py` | `is_backed_validated`, `LLM_VALIDATED_BACKENDS` |
| Context-fit estimate (not pricing) | `horde/classes/kobold/request_fit.py` | `estimate_prompt_tokens` |
| Calibration | `tools/text_tariff_bench.py` | `fit_costs`, `derive_rates` |

## Regression-test map

| Contract | Test |
| --- | --- |
| Estimator, retention cap, output count, work model, level continuity, every branch, quote, allowance and its downgrade consistency | `tests/unit/test_text_pricing.py` |
| Worked examples a reader can follow | `tests/unit/test_text_pricing_examples.py` |
| Settlement adapter: prompt sizes, independence from `max_length` and `max_context_length` while the prompt fits, the cap when it does not, empty and absent output | `tests/unit/test_kobold_text_procgen.py` |
| Cancelled job accounting on a persisted graph | `tests/unit/test_kudos_text_cancel.py` |
| Quote model choice, quote of a truncated prompt, long-prompt gate, gate on a truncated prompt, uncurable prompt, downgrade synchronisation | `tests/unit/test_kobold_text_wp.py` |
| Cache key carries prompt estimate and tariff version | `tests/unit/test_dry_run_kudos_quote.py` |
| Hold, dry run, 202 and refusal agree; settled balances follow the submit reward | `tests/integration/test_text_kudos_settlement.py` |
| Hold covers the debit, free admission bounded, quote tracks settlement, on any tariff | `tests/integration/test_text_kudos_bounds.py` |
| Free admission bounded and quote keyed apart, over stubs without a database | `tests/unit/test_text_kudos_bounds.py` |
| Grown context quoted in full; styled and unstyled quotes agree on the resolved prompt | `tests/integration/test_text_style_application.py` |
| Longer prompt settles higher for the same generation | `tests/integration/test_text.py` |
| Calibration fits and rate derivation | `tests/unit/test_text_tariff_bench.py` |

## Sharp edges

- The rates are a CPU measurement. Until a GPU calibration replaces them, prompt processing is likely
  overweighted relative to generation, which raises short-output jobs on long prompts and the free-tier
  threshold more than a GPU measurement would.
- With the provisional rates and the default reference, a prompt past about 7,000 estimated tokens has
  no free allowance at all; a kudos-less requester is refused rather than downgraded. Raising
  `HORDE_TEXT_FREE_PROMPT_TOKENS` widens the tier.
- The 20-token output floor is paid for any non-empty return, and the prompt term with it, so a
  one-word reply to a 16K prompt pays over a thousand kudos on a 13B model. The empty-generation counter
  records whitespace-only returns, which are paid the floor alone.
- Prompts past 16,384 estimated tokens are indistinguishable to the tariff.
- The retention cap is the measured backend's rule. A backend that keeps the whole context for the
  prompt and shortens its output instead processes up to `max_length` more tokens than are priced; the
  difference is bounded by the request's own parameters and is not recovered.
- The accepted response and the dry run round to whole kudos; the hold and the settlement keep two
  decimals.
- A change to the formula applies to jobs already queued at deployment; no per-request tariff version
  is persisted.
- `estimated_prompt_tokens` in a dry run's `resolved` block is the context-fit estimate (`/ 3`), not the
  tariff estimate (`/ 4`).
- The speed sample a submit records divides returned tokens by pop-to-submit time, so prompt processing
  lowers a worker's recorded speed without entering the numerator (`record_fulfilment`).

---
title: "Text kudos pricing reference"
summary: "The tariff a text job is priced under: the prompt and output operands, the context multiplier, each settlement branch, the quote, the hold, the free-tier allowance and the quote cache key."
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
curve to turn its output allowance into a budget on modelled work. The reasoning behind the shape is in
[ADR 0019](../decisions/0019-price-text-input-as-processed.md); how the resulting kudos are recorded is in the
[kudos accounting reference](kudos_accounting.md).

## Operands

| Operand | Source | Estimate |
| --- | --- | --- |
| Prompt | `WaitingPrompt.prompt`, the text put in the worker payload after any style template | `ceil(len(prompt) / 4)`, 0 for an empty prompt, capped at `max(max_context_length - max_length, ceil(max_context_length / 5))` |
| Output | The generation the worker submitted | `ceil(len(generation) / 4)`, floored at 20, capped at the request's `max_length`; 0 when nothing was submitted |
| Model | `ModelReference.get_text_model_multiplier` for the model the worker reported | Parameter count in billions, or unknown when the reference has no entry |

The prompt estimate counts code points, not model tokens. The cap is what the worker processes: a
worker takes a request only when its own context is at least the requested `max_context_length`, and
the measured backend keeps the last `max_context_length - max_length` tokens of a prompt that does not
fit, lowering `max_length` rather than leave the prompt less than a fifth of the context. Under
`context_fit=grow` the context is raised until the prompt fits, so the cap never binds; under `reject`
an over-long prompt is refused; under `ignore`, the default, the prompt is priced as the worker will
see it. While the prompt fits, `max_length` and `max_context_length` do not change its price. The
estimate is distinct from the context-fit estimate in `horde/classes/kobold/request_fit.py`, which
divides by three so that an over-long prompt is detected conservatively; the two serve different
purposes and are not interchangeable.

## Context multiplier

```text
multiplier(prompt_tokens) = min(30, 1.2 + (max(20, prompt_tokens) / 1024) ** log2(2.2))
```

| Prompt tokens | Multiplier |
| --- | --- |
| 20 or fewer | 1.21 |
| 1,024 | 2.20 |
| 2,048 | 3.40 |
| 4,096 | 6.04 |
| 10,000 | 14.56 |
| 19,647 and above | 30.00 |

Doubling the prompt multiplies the variable term by 2.2. The curve is a power law with exponent
`log2(2.2)`, about 1.14, not an exponential, and it does not describe memory growth. Past the cap every
prompt prices alike.

## Settlement branches

`TextProcessingGeneration.get_gen_kudos` supplies the operands; `calculate_text_generation_reward`
applies the first matching row. `B` is the model size in billions, `O` the output operand, `M` the
multiplier.

| Condition | Reward |
| --- | --- |
| `HORDE_REQUIRE_MATCHED_TARGETING=1` and the request named workers | `0.1` |
| Unknown model, untrusted owner | `M` |
| Unknown model, trusted owner | `O * M * 2.7 / 100`, unrounded |
| Known model | `round(O * (max(B, 13) / 13) ** 0.2 * B / 125 * V * M, 2)`, where `V` is 0.3 on a backend outside `LLM_VALIDATED_BACKENDS` and 1 otherwise |

Worked values for a 13B model on a validated backend returning 2,048 characters against `max_length`
512:

| Prompt tokens | Reward |
| --- | --- |
| 256 | 74.90 |
| 1,024 | 117.15 |
| 10,000 | 775.26 |
| 50,000 | 1,597.44 |
| 10,000 sent, 1,536 processed (2,048 context) | 148.35 |
| 10,000 sent, 410 processed (2,048 context, 4,096 allowed) | 82.70 |

The reward is nominal. Owner escrow (`User.record_contributions`), the bridge multiplier and the
requester's per-job burn of one kudos (`TextWaitingPrompt.calculate_extra_kudos_burn`) apply on top. A
cancelled job has no output, so its known-model reward is zero while the requester still pays the burn.
A censored job pays the worker and waives the requester's generation component. An empty submission
counts as 20 output tokens and is paid; `TextProcessingGeneration._record_empty_generation` counts it.

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
allowance is then reduced for the prompt through `free_output_allowance`:

```text
allowance(prompt) = floor(allowance * multiplier(reference) / multiplier(prompt))   for prompt > reference
allowance(prompt) = allowance                                                         otherwise
```

bounded below by 16, the smallest `max_length` the API accepts. The prompt is the one the worker will
process at the allowance granted; since a smaller allowance leaves more context to the prompt, the gate
lowers the allowance until it and the prompt it implies fit the budget together, so settlement after a
downgrade prices what the gate admitted. The reference prompt size is `HORDE_TEXT_FREE_PROMPT_TOKENS`,
read at each call, default 4096. A request whose `max_length` exceeds the reduced allowance needs a
hold.

| Prompt tokens | Allowance at 512 | Allowance at 256 |
| --- | --- | --- |
| up to 4,096 | 512 | 256 |
| 8,192 | 261 | 130 |
| 10,000 | 212 | 106 |
| 50,000 | 103 | 51 |
| 50,000 sent into a 2,048 context | 512 | 256 |
| 5,000 sent into a 5,120 context | 453 | 256 |

When the hold cannot be taken and the request allows a downgrade, `TextWaitingPrompt.downgrade` sets
`max_length`, `things`, `params`, `gen_payload`, `total_usage` and `kudos` to the reduced allowance and
accepts slow workers. The prompt is never trimmed. Without a downgrade the request is refused with
`KudosUpfront`, and the message states the prompt estimate, how much of it fits the requested context
when that is less, and the allowance it permits.

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
| Context multiplier | `horde/classes/kobold/pricing.py` | `context_multiplier` |
| Settlement kernel | `horde/classes/kobold/pricing.py` | `calculate_text_generation_reward` |
| Quote kernel | `horde/classes/kobold/pricing.py` | `quote_text_generation` |
| Free-tier allowance | `horde/classes/kobold/pricing.py` | `free_output_allowance`, `reference_prompt_tokens` |
| Cache version | `horde/classes/kobold/pricing.py` | `TEXT_TARIFF_VERSION` |
| Settlement adapter | `horde/classes/kobold/processing_generation.py` | `TextProcessingGeneration.get_gen_kudos`, `get_things_count` |
| Quote adapter | `horde/classes/kobold/waiting_prompt.py` | `TextWaitingPrompt.calculate_kudos`, `extract_params` |
| Admission gate | `horde/classes/kobold/waiting_prompt.py` | `TextWaitingPrompt.require_upfront_kudos` |
| Downgrade | `horde/classes/kobold/waiting_prompt.py` | `TextWaitingPrompt.downgrade` |
| Hold and refusal | `horde/apis/v2/kobold.py` | `TextAsyncGenerate.initiate_waiting_prompt` |
| Cache key | `horde/apis/v2/kobold.py` | `TextAsyncGenerate.get_hashed_params_dict` |
| Dry-run total | `horde/classes/base/waiting_prompt.py` | `WaitingPrompt.extrapolate_dry_run_kudos` |
| Model size | `horde/model_reference.py` | `ModelReference.get_text_model_multiplier`, `is_known_text_model` |
| Backend validation | `horde/bridge_reference.py` | `is_backed_validated`, `LLM_VALIDATED_BACKENDS` |
| Context-fit estimate (not pricing) | `horde/classes/kobold/request_fit.py` | `estimate_prompt_tokens` |

## Regression-test map

| Contract | Test |
| --- | --- |
| Estimator, retention cap, curve, every branch, quote, allowance and its downgrade consistency | `tests/unit/test_text_pricing.py` |
| Settlement adapter: reported prompt sizes, independence from `max_length` and `max_context_length` while the prompt fits, the cap when it does not, empty and absent output | `tests/unit/test_kobold_text_procgen.py` |
| Quote model choice, quote of a truncated prompt, long-prompt gate, gate on a truncated prompt, downgrade synchronisation | `tests/unit/test_kobold_text_wp.py` |
| Cache key carries prompt estimate and tariff version | `tests/unit/test_dry_run_kudos_quote.py` |
| Hold covers the debit, free admission bounded, quote tracks settlement, on any tariff | `tests/integration/test_text_kudos_bounds.py` |
| Free admission bounded and quote keyed apart, over stubs without a database | `tests/unit/test_text_kudos_bounds.py` |
| Grown context quoted in full; styled and unstyled quotes agree on the resolved prompt | `tests/integration/test_text_style_application.py` |
| Longer prompt settles higher for the same generation | `tests/integration/test_text.py` |

## Sharp edges

- The multiplier caps at 30, so prompts past roughly 19,650 tokens are indistinguishable to the tariff,
  and the prompt reward is still proportional to the output returned. A job returning little on a huge
  prompt is under-compensated relative to its prefill work.
- The 20-token output floor compounds with the multiplier. An empty submission on a long prompt pays
  the floor at the cap (62.40 for 13B validated). The empty-generation counter records these.
- A long-prompt downgrade can set `max_length` below the 256-token floor the queue-pressure clamp
  otherwise guarantees.
- The accepted response and the dry run round to whole kudos; the hold and the settlement keep two
  decimals.
- A change to the formula applies to jobs already queued at deployment; no per-request tariff version
  is persisted.
- `estimated_prompt_tokens` in a dry run's `resolved` block is the context-fit estimate (`/ 3`), not the
  tariff estimate (`/ 4`).
- The retention cap is the measured backend's rule. A backend that keeps the whole context for the
  prompt and shortens its output instead processes up to `max_length` more tokens than are priced; the
  difference is bounded by the request's own parameters and is not recovered.

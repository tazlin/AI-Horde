---
status: accepted
date: 2026-10-14
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Price a text job's prompt as the worker processes it, through one tariff shared by quote and settlement

## Context and Problem Statement

A text job's kudos are meant to recover the volunteer's effort in roughly equal terms. The settlement
formula scales the output reward by a context multiplier that grows with the prompt, but it measured the
prompt with the output-accounting helper, which caps its count at the requested `max_length`. A job
requesting 512 tokens therefore paid the same whether the worker processed a 1,000-token or a
50,000-token prompt, and raising `max_length` raised the prompt measurement without any change to the
prompt. Around that defect, the dry-run quote priced the requested `max_context_length` rather than the
prompt, divided by 100 where settlement divides by 125, and priced the first listed model where the
upfront hold priced the largest. The accepted response reported a quote of zero for every text request,
the quote cache keyed on params alone, the free-tier admission gate considered output only, and a
downgrade left the settlement cap on the output operand at the old allowance.

The service has no tokenizer, cannot verify what a backend cached, and does not accept worker
self-reports as settlement authority. The input operand has to be something the server can compute
from the request alone. Two facts make that possible. A worker takes a request only when its own
context is at least the requested `max_context_length` (`TextWorker.can_generate`), so the request's
context is the one the prompt is fitted to. And the measured backend, given a prompt that does not fit
beside `max_length`, keeps the last `max_context_length - max_length` tokens of it, lowering
`max_length` rather than leave the prompt less than a fifth of the context
(`tools/text_tariff_bench.py truncation`, recorded in the explanation page). Under the default
`context_fit=ignore` the server sends such prompts knowing this will happen.

## Decision Drivers

- A worker is paid for the prompt it processes, independently of the output allowance and of the
  requested context, except where those two bound what it processes.
- The quote, the accepted response, the hold and the settlement apply one arithmetic to one set of
  operands, so the only differences between a quote and a settlement are the text the worker returned and
  which worker served the job.
- Cancellation, censorship, early stop and empty-output policies keep their current accounting.
- No worker-reported measurement (cached tokens, elapsed time, retained context) enters the price.
- Free access to ordinary workloads stays, while a prompt that consumes disproportionate effort meets the
  same kind of upfront bar a large output already meets.

## Considered Options

- Price the prompt the worker processes through one shared kernel, keeping the existing curve and rates
- Price the prompt as sent, whatever the context keeps of it
- Remove the prompt cap only
- Introduce separate input and output tariff terms now

## Decision Outcome

Chosen option: "Price the prompt the worker processes through one shared kernel, keeping the existing
curve and rates".

`horde/classes/kobold/pricing.py` holds the tariff as pure functions. The prompt operand is
`ceil(len(prompt) / 4)` over the prompt the worker is sent, after any style template, capped at what
the requested context keeps once `max_length` is reserved for the output:
`max(max_context_length - max_length, ceil(max_context_length / 5))` (`retained_prompt_tokens`). Under
`context_fit=grow` the context is raised until the prompt fits, so the cap never binds; under `reject`
an over-long prompt never becomes a job; under `ignore` it is priced as the worker will see it. The
output operand is unchanged: the returned text at four characters per token, floored at 20 and capped at
`max_length`. The context multiplier is the existing curve,
`1.2 + (max(20, prompt_tokens) / 1024) ** log2(2.2)`, held to at most 30, which it reaches near 19,650
estimated tokens. Every settlement branch (matched targeting, unknown model under an untrusted or trusted
owner, known model with the parameter bonus and the unvalidated-backend factor) keeps its arithmetic and
rounding.

The quote applies the same kernel at the full `max_length`, assuming a validated backend and a trusted
owner, and prices the largest known model among those requested (13B when none is named, the unknown
branch when none is known). It is computed when the request is constructed, so the accepted response
carries it, and recomputed after a downgrade. The upfront hold is the quote plus the per-job burn, times
`n`. The quote cache key carries the prompt estimate and a tariff version; the params it already carries
supply the context and allowance the cap reads.

The free-tier output allowance becomes a budget on modelled work: a prompt past the reference size
(`HORDE_TEXT_FREE_PROMPT_TOKENS`, default 4096) may generate without a hold only as many tokens as keep
`output * multiplier(prompt)` within `allowance * multiplier(reference)`. The reference comes from the
retired hold formula, `max_length * parameters / 21`, which equals the settlement kernel at a prompt of
about 4,030 estimated tokens. The gate measures the prompt as the worker will process it at the
allowance it grants: a smaller allowance leaves more of the context to the prompt, so the allowance is
lowered until it and the prompt it implies fit the budget together. A downgrade to the computed
allowance therefore clears the output and the prompt trigger in one pass and prices at settlement what
the gate admitted; the prompt itself is never trimmed.

The change deploys as a single cutover. Jobs in flight at deployment settle under the new kernel, as
with every previous change to the formula.

### Consequences

- Good: A 13B validated job returning 512 tokens pays 117.15 for a 1,024-token prompt, 775.26 for a
  10,000-token prompt in a 16K context and 1,597.44 at the cap, where it paid 88.10 for all three.
- Good: Raising `max_length` or `max_context_length` without changing the prompt no longer changes what
  the prompt is priced at, unless the prompt did not fit the context, in which case the price follows
  the part of it the worker processes.
- Good: A prompt the worker will truncate is not paid for. A 50,000-token prompt sent into a 2,048
  context beside 512 tokens of output is priced at 1,536 tokens, which is what the backend keeps, where
  pricing it as sent would charge the ceiling.
- Good: A dry run, the accepted response and the hold agree with each other and with settlement at the
  same operands.
- Bad: A known-model quote at the same operands is 20% lower than before, since it now divides by 125 as
  settlement does. Clients comparing historical quotes see the drop.
- Bad: The hold for a request naming no model rises from `20 * n` to the 13B quote.
- Bad: The retention rule is the measured backend's. A backend that keeps the whole context for the
  prompt and shortens its output instead processes up to `max_length` more tokens than are priced; one
  that keeps less is overpaid by the difference. Both are bounded by the request's own parameters.
- Bad: A long prompt can be downgraded below the 256-token output floor the queue-pressure clamp
  previously guaranteed; a 50,000-token prompt in a 16K context at a 512-token allowance keeps 103
  tokens.
- Bad: The 20-token output floor now compounds with the full multiplier, so an empty return on a long
  prompt pays 62.40 to a 13B validated worker where it paid 3.44. The empty-generation counter already
  observes this; the policy is a separate decision.
- Bad: The curve still saturates at 30, so prompts beyond roughly 19,650 tokens price alike, and the
  input reward remains proportional to the output returned. A separate input term waits on calibration.

## Pros and Cons of the Options

### Price the prompt as sent, whatever the context keeps of it

- Good: The operand depends on the prompt alone, so no request parameter can lower it.
- Bad: Pays for tokens the server knows the worker will discard: a 50,000-token prompt in a 2,048
  context would be priced at the 16,384-token ceiling for about 1,500 tokens of work. The measurement
  established the retention rule, and the pop filter fixes which context applies, so the server has the
  information and would be choosing not to use it. The lever this removes is one the requester already
  holds, since a smaller context genuinely means less work.

### Remove the prompt cap only

- Good: A one-line change with the smallest review surface.
- Bad: Leaves the quote pricing a different operand with a different divisor, the cache answering with
  stale prices, the hold five times short of a long-prompt settlement, and the downgrade cap stale.

### Introduce separate input and output tariff terms now

- Good: Compensates prefill work independently of how much output is returned.
- Bad: No benchmark evidence exists for the relative rates, and an additive term changes what a
  cancelled job with no output pays unless guarded. Deferred until calibrated.

## Confirmation

`tests/unit/test_text_pricing.py` fixes the estimator, the retention cap, the curve, every settlement
branch, the quote and the free-tier allowance, including the allowance's agreement with the operand a
downgrade leaves. `tests/unit/test_kobold_text_procgen.py` fixes the reported prompt sizes through the
settlement adapter, the independence from `max_length` and `max_context_length` while the prompt fits,
and the cap when it does not. `tests/unit/test_kobold_text_wp.py` fixes the quote's model choice, the
long-prompt gate, the gate on a prompt the context truncates and the downgrade.
`tests/unit/test_dry_run_kudos_quote.py` fixes the cache key.
`tests/integration/test_text_style_application.py` fixes that a grown context is priced in full, and
`tests/integration/test_text.py` that a longer prompt settles higher for the same generation.
`tests/integration/test_text_kudos_bounds.py` and `tests/unit/test_text_kudos_bounds.py` fix the relations
between the gates and settlement that hold on any tariff. `docs/reference/text_kudos_pricing.md` records
the tariff.

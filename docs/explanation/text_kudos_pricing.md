---
title: "Why text jobs are priced this way"
summary: "What a text worker spends on a job, what the server can and cannot see of it, the measurements behind the tariff's shape and rates, and what those measurements do not establish."
topics: [kudos, generation, workers]
order: 20
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Why text jobs are priced this way

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [generation](../topics.md#generation), [kudos](../topics.md#kudos), [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

Kudos repay a volunteer for the work a job took, in roughly equal terms, from a server that never sees
the worker's hardware, tokenizer or cache. The text tariff therefore prices the two things the server
does see, the prompt it sent and the text that came back, under a model of how a transformer backend
spends time on them. This page explains what that work is, why the earlier curve got it wrong, what was
measured to replace it, and what the measurement leaves open. The tariff itself is specified in the
[text kudos pricing reference](../reference/text_kudos_pricing.md); the decisions are
[ADR 0019](../decisions/0019-price-text-input-as-processed.md) and
[ADR 0020](../decisions/0020-additive-text-tariff-from-measured-cost.md).

In brief:

- A job has two phases: processing the prompt once, and generating tokens one at a time over a context
  that includes the prompt. Both cost more for a longer prompt; only the first is independent of how
  much is generated.
- The earlier tariff multiplied the output reward by a prompt-dependent factor, so a short answer to a
  long document was paid almost nothing for the document.
- The tariff now adds a prompt term to an output term whose per-token cost rises with the context. The
  shape was measured on KoboldCpp; the rates are from a CPU and are provisional.
- On the Horde, consecutive jobs on a worker come from different requesters, so the cache reuse that
  makes a local chat cheap rarely applies; the prompt the worker receives is close to the work done.
  When the prompt does not fit the requested context the backend keeps a known part of it, and the
  tariff prices that part.

## What a worker spends on a job

A decoder-only transformer handles a request in two phases. Prefill evaluates the whole prompt in
batches to build the attention state; its cost grows with the prompt, linearly through the weight
multiplications and quadratically through attention. Decode then produces each output token with one
forward pass that attends over everything before it, so each token costs a little more the longer the
context is. Prefill is batched and far cheaper per token than decode; decode is serial and dominates a
job with a short prompt and a long reply.

The server sees neither phase. It sees the prompt it put in the pop payload, the text the worker
submitted, the model the worker claims, and the wall clock between pop and submit. Everything the tariff
does has to be a function of those, and nothing a worker reports about its own effort can enter the
price, since a worker that could raise its pay by reporting more work would.

## Why the multiplicative curve was wrong

The earlier settlement paid `output tokens * M(prompt)` with `M` a capped power law in the prompt. Two
things were wrong with it. The prompt was measured through the output-accounting helper, which capped it
at the requested `max_length`, so a 512-token job paid the same for a 1,000-token and a 50,000-token
prompt; ADR 0019 repaired that. And the form itself ties the prompt's cost to the output: with no output,
the prompt is worth nothing, and with twenty tokens of output it is worth a twentieth of what it is
worth with four hundred. The worker processed the same prompt in each case.

## What was measured

`tools/text_tariff_bench.py` drove a KoboldCpp v1.123 build with Llama-3.2-1B-Instruct Q4_K_M on four
CPU threads, no GPU, batch size 512, context 16,384, with fast-forward and context shift disabled.
Prompts of 256 to 12,288 tokens were built from shuffled documentation paragraphs so that no two shared a
prefix, and 128 tokens were generated from each.

| Prompt tokens | Prefill seconds | Prefill ms per prompt token | Decode ms per generated token |
| --- | --- | --- | --- |
| 257 | 2.0 | 7.9 | 43.7 |
| 932 | 8.3 | 8.9 | 51.5 |
| 1,881 | 16.9 | 9.0 | 63.2 |
| 3,771 | 37.1 | 9.9 | 80.7 |
| 7,416 | 94.7 | 12.8 | 108.2 |
| 11,197 | 185.0 | 16.5 | 154.1 |

Prefill fits `0.47 + 7.08 ms * P + 0.795 us * P^2` with R² 0.999; a straight line fits with R² 0.979 and
a power law has exponent 1.16. Decode per token fits `40.6 ms + 10.2 us * P` with R² 0.993. In units of
one generated token at an empty context, a prompt token costs 0.175 units to process plus 0.0000196
units per token of prompt already seen, and adds 0.000252 units to every generated token. Those are the
tariff's `PROVISIONAL_RATES`.

Held against the earlier curve with these costs, a 512-token job is within about 10% at every prompt
size, which is why pinning the new model to the old level at one reference job reproduces familiar
prices for the typical request. The disagreement is all in the output dimension: at 64 output tokens the
old curve paid 0.37 of the job's time at a 1K prompt and 0.14 at 16K; at 1,024 output tokens on a 4K
prompt it paid 1.5 times.

Two further series inform the operands rather than the rates. With fast-forward on, a growing
single-requester conversation reprocessed 13, 10 and then 1 tokens of its 1,800-token history, while one
interleaved request from another requester forced the next turn to reprocess all 1,862; near a full
window, context shift kept the cache and the next turn processed 190 tokens instead of 12,800. Dispatch
on the Horde has no requester affinity, so the first pattern is what a public worker sees, and the prompt
as sent is a fair measure of work on it. And when the prompt plus `max_length` exceeded the context, the
worker kept exactly `min(request context, worker context) - max_length` tokens from the end of the
prompt and lowered `max_length` so that at least a fifth of the context stayed for the prompt. Since a
worker only takes a request whose context it can hold, the server knows this bound from the request
alone, and under `context_fit=ignore` the tariff prices the prompt at it (`retained_prompt_tokens`)
rather than as sent.

The tokenizer series put Latin-script prose, chat logs and instruct templates at about 4.9 characters per
token on both a Llama and a Qwen tokenizer, so the billing estimate of four characters per token
overstates such prompts by a fifth and the context-fit estimate of three by two thirds. Chinese and
Japanese ran at 1.3 to 1.9 characters per token, understated by half or more by both.

## What the measurement does not establish

The container had no GPU. The absolute times above, and with them the ratio of prefill cost to decode
cost, belong to a four-thread CPU on a 1B model. On a GPU, batched prefill is far more efficient
relative to serial decode, so the input share of a job is smaller than the provisional rates say,
probably by several times; the quadratic and context terms shrink with it. The shapes hold: prefill is
linear plus quadratic in the prompt, decode cost per token is linear in the context, and the
multiplicative form cannot represent either. A GPU run of the same two series through the procedure in
[Calibrate the text tariff](../how-to/calibrate_text_tariff.md) replaces the rates; the shape, the level
anchor and the guards stay.

Two consequences of the provisional rates are worth knowing until then. Short replies to long prompts
pay more than a GPU measurement would set, and the free-tier budget, which is the work of a 4,096-token
prompt with 512 tokens of output, is used up by prompt processing alone at about 7,000 tokens, so a
kudos-less requester with a longer prompt is refused rather than downgraded.

## What the design buys and what it costs

The additive form pays the prompt once, pays each generated token by the context it was generated over,
and makes a quote, a hold, an admission decision and a settlement the same calculation at different
operands. The level anchor means a typical job is not repriced by the change in form. The cost is a
pair of unverifiable edges the server guards rather than resolves: an empty return is paid the output
floor without the prompt term, since it cannot be told from a skipped job, and prompts past 16,384
estimated tokens price alike, since the server has no evidence of what a backend does beyond the
context most workers serve.

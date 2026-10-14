---
title: "Calibrate the text tariff against a worker"
summary: "Run tools/text_tariff_bench.py against a KoboldCpp worker to measure tokenizer ratios, prefill and decode cost, cache reuse and truncation, and derive the tariff rates from the result."
topics: [kudos, workers]
order: 60
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Calibrate the text tariff against a worker

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [kudos](../topics.md#kudos), [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

The text tariff's rates say how much a prompt token costs relative to a generated token and how much
each prompt token adds to the cost of every generated token. They are set from measured backend cost,
and the figures in `horde/classes/kobold/pricing.py` record the hardware they came from. This
procedure measures a KoboldCpp worker and derives replacement rates. It takes about half an hour on a
GPU worker, longer on CPU.

Preconditions: a KoboldCpp build with its HTTP API reachable from the machine running the tool, the
model to measure loaded, and this repository checked out for its `docs/` tree, which the tool draws
prompt text from. End state: JSON result files for each series and a printed fit with the derived
rates. Nothing in the deployment is changed; the tool only sends generation requests to the worker
named by `--port`.

## 1. Start the worker for the cost series

Fast-forward and context shift reuse the KV cache between requests and would hide prefill cost, so
the cost series runs with both off:

```sh
koboldcpp --model <model.gguf> --contextsize 16384 --port 5001 --skiplauncher --quiet \
    --nofastforward --noshift
```

Use the context size, batch size and GPU layer settings the worker runs with on the Horde; the rates
depend on them. Verify: `curl localhost:5001/api/v1/model` returns the model name.

## 2. Measure the tokenizer

```sh
uv run python tools/text_tariff_bench.py tokens --corpus-dir docs --results <results-dir> \
    --label <worker-label> --source-file horde/classes/kobold/pricing.py
```

Verify: `<results-dir>/tokens_<worker-label>.json` holds one row per sample with `chars_per_token` and
the error of the billing (`/4`) and context-fit (`/3`) estimates. Prose on Llama-family tokenizers
sits near 4.9 characters per token.

## 3. Measure prefill and decode cost

```sh
uv run python tools/text_tariff_bench.py prefill --corpus-dir docs --results <results-dir> \
    --label <worker-label> --context 16384
```

The series sends prompts of 256 to 12,288 tokens, twice each, built from shuffled paragraphs so no
two share a prefix, and generates 128 tokens from each. Verify: every row's `last_input_count` equals
its `tokenized_prompt` within 3%, which shows the whole prompt was processed. A row whose
`process_seconds` is far below its neighbours means the cache was reused; restart the worker with the
flags above and rerun.

## 4. Derive the rates

```sh
uv run python tools/text_tariff_bench.py report <results-dir>/prefill_<worker-label>.json
```

The report prints the fitted curves, `prefill = a + b * P + c * P^2` and
`decode per token = d + e * P`, with their goodness of fit, and the three tariff rates in decode-token
units: `input_rate` (`b / d`), `input_quadratic_rate` (`c / d`) and `context_decode_rate` (`e / d`).
Verify: both R² values are above 0.98. A prefill fit far from that means timing noise; rerun with
`--repetitions 3`.

To adopt the rates, set the constants in `horde/classes/kobold/pricing.py`, record the worker's
hardware and settings in the docstring beside them, bump `TEXT_TARIFF_VERSION`, and update the
worked examples in `tests/unit/test_text_pricing_examples.py` and the figures in
`docs/reference/text_kudos_pricing.md`.

## 5. Optional: cache reuse and truncation

Restart the worker without `--nofastforward --noshift` and run:

```sh
uv run python tools/text_tariff_bench.py fastforward --corpus-dir docs --results <results-dir> --label <worker-label>
uv run python tools/text_tariff_bench.py truncation --corpus-dir docs --results <results-dir> --label <worker-label>
```

Verify the `fastforward` rows by `wall_seconds`: the growing-conversation steps take about the decode
time alone, the step after another requester's prompt takes a full prefill again. The perf endpoint's
`process_seconds` is not reliable for these rows (see the tool's module docstring). Verify the
`truncation` rows: `prompt_tokens` equals `requested_ctx - requested_max_length` when the prompt does
not fit, and `completion_tokens` is below `requested_max_length` when the worker reserved context for
the prompt.

## Reversal

The tool writes only to `<results-dir>`. Delete it to discard a run. A worker restarted with the flags
in step 1 is returned to its normal configuration by restarting it without them.

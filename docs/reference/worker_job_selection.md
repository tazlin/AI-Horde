---
title: "Worker job selection reference"
summary: "Which queued request a popping image or text worker receives: candidate order, priority users, the filters, the claim that hands a request out, and what a worker is told when it receives nothing."
topics: [requests, workers]
order: 40
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Worker job selection reference

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [requests](../topics.md#requests), [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

In brief:

- A pop reads candidates without row locks: requests from the worker's owner and its `priority_usernames` first, then
  the rest, each group by queue priority (`extra_priority`) and then age. The first read returns up to
  `POP_FIRST_READ_LIMIT` (20) candidates.
- The worker's own checks run on each candidate in order. The first candidate it can serve is claimed with one atomic
  decrement of the request's remaining generations; if another worker emptied it meanwhile, the pop moves on.
- A pop that refuses a whole read continues after its last candidate with reads of up to `POP_CANDIDATE_LIMIT` (200),
  so a servable request is found however many unservable ones are ahead of it.
- A worker that receives nothing is told why in `skipped`. Image counts most reasons across the whole queue; text
  reports only refusals made after the candidate query.

The decision to read unlocked and claim one request is [ADR 19](../decisions/0019-claim-one-request-instead-of-locking-candidate-pages.md).
Interrogation (alchemy) pops use a separate path and are not covered here.

## Code map

| Concept | File | Symbol |
| --- | --- | --- |
| Pop loop for image and text | `horde/apis/v2/base.py` | `JobPopTemplate._post_inner` |
| Per-candidate check, model list read once per pop | `horde/apis/v2/stable.py`, `horde/apis/v2/kobold.py` | `ImageJobPop.worker_can_generate`, `TextJobPop.worker_can_generate` |
| Image candidate query | `horde/database/functions.py` | `get_sorted_wp_filtered_to_worker` |
| Text candidate query | `horde/database/text_functions.py` | `get_sorted_text_wp_filtered_to_worker` |
| Candidate order, read sizes and continuation | `horde/database/functions.py` | `pop_candidate_order`, `after_pop_candidate`, `POP_FIRST_READ_LIMIT`, `POP_CANDIDATE_LIMIT` |
| Claim | `horde/classes/base/waiting_prompt.py` | `WaitingPrompt.claim_generations`, `WaitingPrompt._start_generation` |
| Giving a generation back | `horde/classes/base/waiting_prompt.py` | `WaitingPrompt.return_generation` |
| Worker checks | `horde/classes/base/worker.py`, `horde/classes/stable/worker.py`, `horde/classes/kobold/worker.py` | `Worker.can_generate`, `ImageWorker.can_generate_with_model_names`, `TextWorker.can_generate_with_softprompt_names` |
| Image `skipped` report | `horde/database/functions.py`, `horde/apis/v2/stable.py` | `count_skipped_image_wp`, `ImageJobPop.post` |

The behaviour on this page is covered by `tests/integration/test_pop_selection.py`; each section names its tests.

## Candidate order

Candidates are ordered by:

1. whether the requester is a priority user of this worker (the owner always is),
2. `extra_priority`, highest first,
3. `created`, oldest first,
4. `id`, so the order is total.

`extra_priority` starts at the requester's kudos balance and rises by 50 every 10 seconds for every queued request
(`increment_extra_priority` in `horde/database/threads.py`), so the relative order of waiting requests holds.

A request's `user_id` is the account that pays for it. A style with a valid shared key attached reassigns its requests to
the style owner (`GenerateTemplate.apply_style`), so they are priority requests on the style owner's workers and not on
the requester's.

### Read sizes

Every candidate read is loaded with its models, targeting and fake-job rows, and a worker usually claims the first
candidate. A first read of 200 measured about 50% slower per pop than a first read of 20 with nothing blocked, all of it
spent loading candidates the pop never reached, so the first read is small and only further reads are large.

Tests: `TestOrder`, `TestPriorityUsers`, `TestDeepPriorityQueues`, `test_raising_every_queued_priority_keeps_the_order`,
`test_a_servable_request_at_a_read_boundary_is_found`.

## Filters

A request is a candidate only if it is active, not faulted, unexpired, and still needs generations. The rest of the
candidate query mirrors the worker's capabilities and settings. Every condition in the query repeats a check of the
worker's `can_generate`, which still runs on each candidate, so the query may only refuse what `can_generate` refuses.

### Priority and general requests

| | Priority requests (owner and priority users) | Other requests |
| --- | --- | --- |
| Image worker in maintenance | admitted by the query; `can_generate` serves only the owner | refused |
| Text worker in maintenance | owner only | refused |
| Request naming its workers (`workers`) | served when it names this worker | served when it names this worker, except under `HORDE_REQUIRE_MATCHED_TARGETING=1` on image, where it is refused |
| Request excluding workers (`worker_blacklist`) | refused when it excludes this worker | refused when it excludes this worker |

A worker in maintenance that receives nothing gets `403 WorkerMaintenance`.

Tests: `TestMaintenance`, `TestTargeting`, `TestMatchedTargetingRequirement`,
`test_a_priority_request_that_excludes_this_worker_is_not_served`.

### Image conditions

The query refuses requests the worker cannot run: pixels over `max_pixels`, models it does not serve, source images
without `allow_img2img`, inpainting without `allow_painting`, unsafe addresses without `allow_unsafe_ipaddr`, NSFW
without `nsfw`, LoRAs, post-processing or controlnet without the matching `allow_*` flag or bridge capability, bridge
fields the bridge predates (schedulers, solver options, flow shift, control strength, extra source images,
transparency), and speed limits. It also refuses, on the worker's behalf:

- requests asking for trusted workers when the worker's owner is untrusted,
- requests that once handed this worker a fake job,
- untrusted requesters from unsafe addresses when the worker's owner is untrusted,
- samplers the worker's bridge does not support,
- prompts containing a word on the worker's blacklist (case-insensitive substring; `%`, `_` and `\` match only
  themselves).

Checks that need the model reference or payload arithmetic stay in `can_generate` only: the sampler work ceiling under
`limit_max_steps`, baseline-dependent features, inpainting-only model rules, `require_upfront_kudos`, and per
post-processor bridge support.

Tests: `TestImageEligibility`, `TestImageEligibilityEdges`, `TestWordBlacklist`, `TestPausedWorker`.

### Text conditions

The query refuses requests over the worker's `max_length` or `max_context_length`, NSFW requests for a non-NSFW worker,
models it does not serve, slow-worker exclusions, unvalidated backends where validation is required, and requests that
once handed this worker a fake job. Requests asking for trusted workers, and softprompt mismatches, are refused by
`can_generate` so that they appear in `skipped`.

Tests: `TestTextEligibility`, `test_a_request_that_is_not_waiting_is_never_served`.

## Claim

The pop claims the first candidate the worker can serve with one statement that locks only that row:

- It takes `min(n, wanted)` of the request's remaining generations `n`, where `wanted` is the worker's `amount` reduced by
  `get_safe_amount` (image workers take fewer large images), or 1 when the request disables batching.
- It extends the request's expiry as `refresh` does.
- It returns `n` before and after, so the handed-out count is exact.

A claim that finds `n = 0` or a faulted request hands out nothing, and the pop evaluates the next candidate. No request
is handed out more often than it asked for, and no worker is turned away while servable work remains.

A generation given back (a faulted submission, a timed-out job) increments `n` in the database
(`return_generation`), so it is not lost to, and does not overwrite, a concurrent claim.

Tests: `TestBatching`, `test_an_image_worker_takes_fewer_large_images_than_its_amount`, `TestClaimAfterRead`,
`TestConcurrentPops`, `TestReturnedGenerations`, `TestReturnedGenerationsDuringClaims`.

## What a worker is told

A pop that hands out nothing returns `skipped`, a count per reason.

- Image: `count_skipped_image_wp` counts requests across the whole active queue for `models`, `worker_id`,
  `max_pixels`, `img2img`, `painting`, `unsafe_ip`, `nsfw`, `lora`, `post-processing`, `controlnet`, `performance`,
  `untrusted`, `bridge_version` and `blacklist`. `kudos` and `step_count` come from `can_generate` on the candidates,
  and `bridge_version` and `blacklist` add any refusals `can_generate` made that the query let through.
- Text: only refusals made by `can_generate` on the candidates, for example `untrusted` and `matching_softprompt`.
  Requests the text query filters out are not reported.

A refusal marked secret (a request that once handed this worker a fake job) is never reported.

Tests: the `test_an_incapable_worker_is_told_why_it_received_nothing` cases, and
`TestDeepPriorityQueues.test_each_unservable_request_is_reported_once`.

## Sharp edges

- A pop that refuses a whole read continues after its last candidate in candidate order. A request whose
  `extra_priority` rises past that point between the two reads is not read again in that pop; the next pop reads it.
- Image skipped counts cover the whole queue, including other users' requests the worker would never be offered first,
  so they can exceed the number of candidates the pop read.
- One image request can be counted twice under `bridge_version`: a LoRA or textual inversion request refused to a bridge
  without that capability is counted by two conditions in `count_skipped_image_wp`.
- Image requests carrying extra source images are refused to bridges that predate them with no `skipped` reason.
- The text generate API documents `disable_batching` but does not store it, so text requests are always batched.
- The text pop does not pass `allow_unsafe_ipaddr` to the worker, which keeps its default of allowing unsafe addresses.
- A text worker takes no word blacklist.
- An image worker in maintenance admits priority users' requests in the query but refuses all but the owner's in
  `can_generate`, so its friends' requests are evaluated and refused without being reported.

The double count, the silent extra-source-image refusal, the two text pop gaps and the maintenance case are pinned by
tests in `tests/integration/test_pop_selection.py`, so a change to one is deliberate. The skip after a priority rise
and the queue-wide scope of the image counts have no dedicated test.

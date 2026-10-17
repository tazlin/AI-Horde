---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Stream chat deltas through a retained Redis stream per request

## Context and Problem Statement

OpenAI clients expect tokens as they are generated. The Horde's submit is one-shot; a worker reports a finished text.
koboldcpp can consume its own local token stream and post batches while it generates. Where do those batches go so
that the front can forward them in order, survive a lost batch, and never splice a reissued job into an exposed
choice?

## Decision Drivers

- Ordering and gap detection per job, with the worker's sequence number as the authority.
- Retention long enough for the front to catch up after a brief stall, short enough to cost nothing at rest.
- No per-token traffic on the pub/sub channel every front process receives.
- The terminal outcome stays in Postgres regardless of the stream.

## Considered Options

- One Redis stream per request, appended by a native progress route, read by the front with `XREAD`
- Progress rows in Postgres appended by the native progress route
- Pub/sub only, with no retention

## Decision Outcome

Chosen option: "One Redis stream per request, appended by a native progress route, read by the front with `XREAD`".

A worker posts sequenced batches authenticated against the assigned generation. The route acknowledges an identical
repeated batch without appending it again, rejects changed content for an accepted sequence, and rejects gaps with
the expected sequence. Record the batch and deduplication state consistently before acknowledgment. Entries identify
the server-assigned choice, generation attempt and sequence.

Append to `horde:chat:stream:<request id>` on primary Redis. Retention covers the bounded active stream lifetime and
cleanup grace, with byte/entry bounds and gap detection. The front checks ordering and emits chunks. Authoritative
status backs up a lost terminal marker. Acknowledgments carry cancellation state; heartbeat-only posts do not create
empty deltas or extend execution forever.

### Consequences

- Good: Ordered, retained, cheap; a stalled reader catches up.
- Good: The signal channel stays small.
- Bad: Failover may lose deltas. Whole-result fallback is allowed only before any output is exposed. After exposure,
  unrecoverable loss terminates with an error, not a replay of the full answer. Exact-prefix suffix reconciliation
  is a separate future feature.
- Bad: Attempt fencing is needed so a reissued job cannot write into its predecessor's stream.

## Pros and Cons of the Options

### Redis stream per request

- Good: Built for exactly this; TTL and trimming are native operations.
- Bad: Redis is a cache in this deployment and a stream is state.

### Postgres progress rows

- Good: One store; durable; the cleanup thread already prunes request state.
- Bad: One insert per batch per in-flight stream on the primary database; the pop and submit paths share it.

### Pub/sub only

- Good: No state at all.
- Bad: A lost message is lost output; tool-call arguments cannot tolerate it.

## More Information

This record and [ADR 0025](0025-stream-chat-deltas-through-postgres-progress-rows.md) are mutually exclusive.
Accepting one rejects the other. Both depend on the progress route in the worker contract.

Live streaming follows the buffered-SSE text alpha. Attempt fencing also requires the front to terminate rather than
splice a replacement attempt into an exposed choice. Cancellation signaling stops computation without changing
native text charges/rewards for dispatched work. Measure HTTP handling, cancel checks and lease writes as well as
Redis cost before choosing a batch frequency.

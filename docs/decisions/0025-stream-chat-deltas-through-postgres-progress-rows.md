---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Stream chat deltas through progress rows in Postgres

## Context and Problem Statement

The same question as [ADR 0024](0024-stream-chat-deltas-through-a-retained-redis-stream.md): where do a worker's
delta batches go. This record proposes the primary database.

## Decision Drivers

- Postgres already holds every other fact about a request, and the cleanup thread already prunes request state.
- A Redis failover must not lose in-flight output.
- The progress route is already a database write for the cancel check and the job TTL refresh.

## Considered Options

- Progress rows in Postgres, one per batch, keyed by generation and sequence
- A Redis stream per request
- Pub/sub only

## Decision Outcome

Chosen option: "Progress rows in Postgres, one per batch, keyed by generation and sequence".

The progress route inserts a row per batch with a unique index on generation id and sequence, which refuses a
repeat; a gap is detected by the front when it reads rows after its last sequence. The front reads through a small
native route or directly from the signal event carrying the latest sequence. Rows are deleted with the request.

### Consequences

- Good: One store; survives a Redis failover; durable for audit.
- Bad: One insert per batch per in-flight stream on the primary database, on the same connection pool as pops and
  submits; at one batch per second per stream this is the dominant write load under concurrency.
- Bad: The front gains a read path through the Horde for deltas, or a database connection, which
  [ADR 0023](0023-run-the-chat-front-as-a-separate-asgi-process-woken-by-redis.md) avoids.

## Pros and Cons of the Options

### Postgres progress rows

- Good: Durable and queryable.
- Bad: Write amplification on the primary database.

### Redis stream per request

- Good: Cheap and ordered.
- Bad: In-flight output lost on failover.

### Pub/sub only

- Good: Nothing to store.
- Bad: Lossy.

## More Information

This record and [ADR 0024](0024-stream-chat-deltas-through-a-retained-redis-stream.md) are mutually exclusive.
Accepting one rejects the other. The review recommends ADR 0024; the measurement that decides is the write rate the
primary database absorbs at the expected number of concurrent streams.

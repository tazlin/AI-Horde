---
status: accepted
date: 2026-10-04
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Claim one request instead of locking pages of candidates

## Context and Problem Statement

A popping worker used to read candidates in pages of 10 (3 for text) with `FOR UPDATE SKIP LOCKED`, evaluate them in
Python while holding the locks, and commit between pages. Only the first page of the worker's priority list (its owner
and `priority_usernames`) was read; later pages came from the general queue. Three problems followed:

- When the oldest priority requests were all unservable by the worker, servable priority requests behind them were
  reached only through the general queue, in kudos order behind other users. Owners saw their workers serve strangers
  first.
- Every page re-evaluated every eligible row, sorted all of them, and locked every row up to `offset + limit`
  (PostgreSQL applies row locks below `LIMIT`). Reading N rows cost O(N²), and every page also paid query construction,
  three eager-load round trips and a commit.
- Rows locked by one pop were invisible to every other concurrent pop. With a queue of a few hundred requests and tens
  of concurrent pops, workers were turned away while servable work existed.

## Decision Drivers

- A worker's priority requests are served before the general queue, however deep the unservable ones ahead of them.
- No request is handed out more often than it asked for.
- A worker is not turned away while servable work remains.
- The cost of a pop does not grow with the square of the queue it reads.
- The selection behaviour, filters and `skipped` reporting stay as they are, apart from the defects above.

## Considered Options

- Read the priority list to its last page with the existing locked pages
- Keyset paging, a `user_id` index and larger pages, with the existing locks
- One unlocked priority-first read and an atomic claim of the chosen request
- Serve pops from the queue snapshot already cached in Redis

## Decision Outcome

Chosen option: "One unlocked priority-first read and an atomic claim of the chosen request".

A pop reads candidates without locks, ordered priority requests first, then queue priority, age and id: up to 20 in
its first read and up to 200 in each further read. The worker's checks run on each in order, and the first it can serve
is claimed with one `UPDATE` that locks only that row and takes `min(n, wanted)` of its remaining generations. A claim
that finds nothing left moves the pop to the next candidate. A pop that refuses a whole read continues after its last
candidate. The first read is small because each candidate is loaded with its related rows and a pop usually claims the
first; a first read of 200 measured about 50% slower per pop than one of 20. Giving a generation back is an increment in the
database rather than a write of a value read earlier. The candidate query takes worker settings as bind parameters, so
it compiles once for every worker, and it refuses up front the requests that the worker's own checks would refuse and
that need no model reference to decide.

### Consequences

- Good: The priority cliff is gone, and concurrent pops no longer hide work from each other.
- Good: A pop is one candidate read and one claim in the common case, whatever the queue depth.
- Good: No row is locked while a pop evaluates candidates, so pops do not block `increment_extra_priority` or the
  queue-pruning thread.
- Bad: A pop that continues past a read can miss a request whose queue priority rose past its read position between
  the two reads. The next pop reads it.
- Bad: The claim and the procgen inserts are separate statements, as the decrement and the inserts already were.
- Neutral: The image `bridge_version` count no longer counts an unsupported sampler twice, since the candidate query
  now refuses it before the worker's check sees it.

### Confirmation

`tests/integration/test_pop_selection.py` holds the selection behaviour. Against the previous implementation it passes
except for the cases that define this decision: the priority cliff, workers turned away under concurrent pops, a fault
racing a claim, the shared query shape, and the sampler count.

## Pros and Cons of the Options

### Read the priority list to its last page with the existing locked pages

- Good: Small change; fixes the priority cliff.
- Bad: Keeps the quadratic page cost. Measured at about 2.4 s per pop with 1000 unservable priority requests.
- Bad: Keeps locked rows hidden from other pops.

### Keyset paging, a `user_id` index and larger pages, with the existing locks

- Good: Removes the offset-row locking.
- Bad: Larger locked pages hide more of the queue from concurrent pops.
- Bad: The planner already ignores the matching partial index for this query, because its many filters look selective;
  a `user_id` variant does not change that.
- Bad: Keyset paging over a priority that rises every 10 seconds can skip rows within a pop, as the chosen option can
  past its first read, but on every page.

### Serve pops from the queue snapshot already cached in Redis

- Good: Almost no database reads per pop.
- Bad: The snapshot is up to 5 seconds old, delaying the first pop of a new request, and carries too few columns for the
  filters.

## More Information

The behaviour is described in the [worker job selection reference](../reference/worker_job_selection.md).

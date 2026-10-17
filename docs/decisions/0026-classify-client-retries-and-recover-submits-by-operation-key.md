---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Classify client retries at the front and recover ambiguous submits by operation key

## Context and Problem Statement

The official OpenAI SDK retries 408, 409, 429, every 5xx, timeouts and connection errors up to twice, and sends no
idempotency key. A front that answers a server-side timeout with 408, or a lost submit with 502, turns one client
call into up to three charged jobs. The SDK honours an `x-should-retry` response header. How does the front avoid
duplicate work, and what happens when the front never learns whether a submit reached the Horde?

## Decision Drivers

- A job that exists must not be recreated by a client retry.
- A retry that is safe (nothing was submitted) must stay allowed.
- No client change can be required.
- A header is mitigation; it cannot help when the client never receives the response.

## Considered Options

- Retry classification with `x-should-retry: false` plus a front-generated operation key claimed at activation
- Retry classification alone
- Caller-provided idempotency keys only

## Decision Outcome

Chosen option: "Retry classification with `x-should-retry: false` plus a front-generated operation key claimed at
activation".

The front distinguishes three states: not submitted, submitted with an id, and outcome unknown. It sends
`x-should-retry: false` on every response after a job exists or may exist (the non-streamed timeout, a transport
failure after the request was sent), and leaves refused connections retriable. The front generates a UUID per client
request and sends it as the operation key. The native chat route keeps a unique index on user id and key, claimed in
the same transaction as activation in `horde/classes/base/waiting_prompt.py`, `WaitingPrompt.activate`; a collision
returns the existing request id. Retention is the request row's lifetime.

### Consequences

- Good: The common duplicate paths close without client changes.
- Good: An ambiguous submit is recoverable by the front, not only by the user.
- Bad: A retry after the request row is gone creates a new job; this is documented.
- Bad: A new column and unique index on `waiting_prompts`.

## Pros and Cons of the Options

### Classification plus operation key

- Good: Covers both the header-reading SDK and the client that never saw the response.
- Bad: Transactional work at activation, which commits in more than one place today.

### Classification alone

- Good: Small.
- Bad: Does nothing for a lost response or a client that ignores the header.

### Caller keys only

- Good: Standard shape.
- Bad: The official SDK does not send one.

## More Information

Rulings R8 and R22 of the review. The design's D8 asked for the operation key; the review added the classification.

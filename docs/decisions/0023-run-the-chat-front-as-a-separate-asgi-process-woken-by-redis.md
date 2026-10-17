---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Run the chat front as a separate ASGI process woken by a Redis signal

## Context and Problem Statement

An OpenAI chat call holds one HTTP connection open until the completion exists. A Horde request can wait minutes
for a worker. The Flask application runs under waitress with 45 threads per instance shared with worker pops and
submits (`horde/argparser.py`). Where does the waiting happen, and how does the waiter learn that a request finished?

## Decision Drivers

- Thousands of open waits must not consume request threads of the Horde instances.
- The public status route is limited to 60 reads per minute per address and path (`horde/apis/limiter_api.py`).
- The outcome must never depend on a notification arriving; it lives in Postgres.
- The front must not need a database connection or the Flask stack.

## Considered Options

- A separate ASGI process that submits over HTTP, subscribes to a Redis channel per request and polls as a fallback
- The same, polling the status route only
- Handlers inside the Flask application that wait synchronously

## Decision Outcome

Chosen option: "A separate ASGI process that submits over HTTP, subscribes to a Redis channel per request and polls
as a fallback".

The front is its own process, proposed as the `horde/oai` package, running under uvicorn with one event loop. It
submits to the native chat route, holds one pattern subscription to `horde:chat:*` on the primary Redis, and fetches
the status once on each wake. The native side publishes an event on each generation and on every terminal outcome;
publishing is best effort. A stretched poll covers a lost message. The front holds no database connection.

### Consequences

- Good: A wait costs the front a coroutine and the Horde nothing until the status read.
- Good: The Horde instances see about two status reads per request while signals work.
- Bad: A new process to deploy, drain and monitor; a request is pinned to the process that accepted it.
- Bad: Every front process receives every chat event; the signal channel must carry only terminal and per-generation
  events, never tokens.

## Pros and Cons of the Options

### Separate ASGI process with signal and fallback poll

- Good: Scales by processes; the Horde's thread pool is untouched.
- Bad: One more moving part, and Redis pub/sub is a new dependency pattern for the repository.

### Separate ASGI process polling only

- Good: No Redis dependency.
- Bad: Status load scales with open waits divided by the poll interval, and the status route's per-address limit
  binds unless the proxy passkey is used.

### Waiting inside Flask

- Good: Nothing new to deploy.
- Bad: Each wait pins a waitress thread shared with pops and submits; a few hundred waits starve the workers.

## More Information

The draft implementation follows the chosen option. The native cache in `horde/horde_redis.py` fans writes out to
every configured server; chat signal and stream keys live only on the primary.

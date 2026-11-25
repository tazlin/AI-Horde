---
status: proposed
date: 2026-11-25
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# List every registered worker in pages with a keyset cursor

`GET /v2/workers/page` lists every registered worker, active or inactive, in bounded pages read from the database with a
keyset cursor. `GET /v2/workers/mine` pages through the caller's own workers with the same query. `GET /v2/workers`
keeps returning the active workers in one bare array, and its API description points to `GET /v2/workers/page` for the
rest.

## Context and Problem Statement

`GET /v2/workers` returns only the active workers, which are the workers that checked in within the last 5 minutes. A
worker that stops checking in drops out of every list. It can still be read one at a time through
`GET /v2/workers/{id}` or `GET /v2/workers/name/{name}`, and only by a caller who already has its ID or name.

The active list is small. Usually a few dozen workers are active at once, so the full list stays cheap to build and
send. The registered workers include every worker that has not been deleted, active or not, and can outnumber the
active ones many times over. They are not listed by any endpoint.

Callers want different things from the registered workers. People using a front end want to browse them and find
workers by model, team or part of a name. An owner wants every worker they run, including offline workers and the fields shown only to
owners. An operator wants to see how much traffic reads each list.

## Decision Drivers

- Existing clients of `GET /v2/workers` keep working without a change.
- Every registered worker can be reached by walking a list, without knowing its ID or name first.
- A page loads and builds only its own rows, so the work of building a page does not grow with the number of
  registered workers.
- Workers joining or leaving between two pages do not shift the rest of a walk.
- A filter does not reveal anything hidden from the caller's view, including through a count.
- The moderator and owner views stay out of every shared cache.
- Operators can see how much traffic reads each list, with metric labels drawn from fixed sets.

## Considered Options

- Page the registered workers in the database with a keyset cursor, on a sibling endpoint
- Page the cached active-worker snapshot with a keyset cursor
- Add inactive workers to `GET /v2/workers`
- Add `limit` and `offset` to `GET /v2/workers`

## Decision Outcome

Chosen option: "Page the registered workers in the database with a keyset cursor, on a sibling endpoint".

**A sibling endpoint.** `GET /v2/workers` keeps its bare array of active workers, its filters and its privilege rules.
Its API description states that it lists only active workers and that `GET /v2/workers/page` lists every registered
worker. The paged list returns `{workers, next_cursor, total}`. The moderation listings already name their cursor field
`next_cursor`. Each row has the same fields that the full list and `GET /v2/workers/{id}` return for that worker in the
caller's view.

**A keyset query.** Each page request runs one statement for the page and `total`. A common table expression selects
the ID and sort value of every worker in the `workers` table that passes the filters. The statement counts its rows for
`total` and selects from it the next rows after the cursor, ordered by the sort column and then by worker ID.
PostgreSQL materializes a common table expression referenced twice in one statement, so one scan of the table serves
both. The statement fetches one row more than the limit, so the response can report whether another page follows. The
details of the page's workers are then loaded in bulk, so the number of statements for a page does not depend on the
limit. One table holds image, text and interrogation workers, so one query covers every type.

**A keyset cursor.** The cursor is base64url JSON holding a format version, the sort, the last row's sort value and its
worker ID. Every order breaks ties by ascending worker ID, so the position is exact. A page starts at the first row
strictly after the cursor. The cursor does not hold filters, so a client may change its filters during a walk.

**Orders.** `uptime` is the default and descends, so workers with a long service record come first. Names chosen to sort
first alphabetically, such as runs of `A`, do not crowd the first page. `requests_fulfilled` and `kudos_rewards` also
descend. `name` ascends by the lower-cased name, computed by the database. The list does not offer a suspicion order,
because ranking suspicion is a job for moderation tools.

**Page boundaries are best effort.** The cursor holds a sort value, and a worker's sort value can change during a walk.

- A worker whose value changes so that it moves past the cursor before its page is read is skipped by the walk.
- A worker whose value changes so that it moves from a page already read to a later position is listed twice.
- A worker registered, deleted or renamed during a walk can be listed, left out or listed twice, depending on where it
  lands relative to the cursor.
- Workers joining or leaving do not shift the rest of the walk, because each page starts after the last row's position
  in the order.
- A walk always ends, because each page starts strictly after the previous one.

Uptime is added at each check-in, and jobs and kudos are added as a worker works, so these values change mostly for
online workers. The list does not guarantee that a walk lists every worker exactly once.

**Filters by view.** `type`, `model`, `team_id`, `owner_id`, `name_contains` and `online` are applied in the query. A
public `owner_id` filter matches only the workers of owners who made their workers public, and `total` is 0 for any
other owner. `model` matches whole names, because model names nest. `name_contains` matches any part of a name, ignoring
case, and treats `%` and `_` as plain characters. `online` compares the last check-in with the same 5-minute window as
the full list.

**An owner's list.** `GET /v2/workers/mine` runs the same query restricted to the caller's account and returns the
owner view, which adds addresses, contacts and messages.

**Indexes.** The paged lists do not add indexes. The `workers` table has no index on `uptime`, `fulfilments`, `kudos` or
the lower-cased name. The name order sorts by `lower(name)`, so `ix_workers_name`, which is on `name`, does not serve
it. Each page therefore scans `workers` once and sorts the matching rows. Every check-in updates `uptime`, so an index
on it would add write cost to the busiest update path of the table. Indexes are reconsidered if database time becomes
most of the cost of a page as traffic to the paged lists grows. `horde.workers.list.reads` shows that traffic.

**Refusals.** A limit outside 1 to 100, a malformed or foreign cursor and a malformed filter are refused with a 400 and
a return code for clients to branch on. An `apikey` header that does not match any account is refused with a 401, so a
moderator with a mistyped key does not mistake the public view for the whole list. The full list ignores such a key.

**Caching.** Moderator responses on both lists, and every response of the owner's list, are sent with
`Cache-Control: private, no-store`, because they contain addresses and contacts. The full list keeps reading the active
worker snapshot. When that snapshot is missing, a node keeps its rebuilt list in local Redis for
`LOCAL_CACHE_MAX_LIFETIME` (5 seconds), the longest time that a snapshot copied into local Redis on read is kept.

**Observability.** `horde.workers.list.reads` counts reads by endpoint (`full`, `page` or `mine`), view, sort, filter
names and whether the read followed a cursor. `horde.workers.list.rows` records rows per response.
`horde.workers.list.snapshot_misses` counts full-list reads that rebuilt the active list from the database.

### Consequences

- Good: Every registered worker can be listed, browsed and searched.
- Good: The full list's rows, shape, filters and status codes do not change for existing clients.
- Good: Workers joining or leaving do not shift the rest of a walk, and a walk always ends.
- Good: The paged list and the owner's list share one query, so their filters and orders behave the same.
- Bad: Every page scans the `workers` table once and sorts unindexed columns. The cost grows with the number of
  registered workers.
- Bad: A worker whose sort value changes during a walk can be skipped or listed twice. A client that needs an exact
  copy of the list has to reconcile walks itself.
- Bad: `GET /v2/workers` and `GET /v2/workers/page` list different sets of workers, and clients can confuse the two.
- Neutral: A worker registered or renamed behind a walk's position is missed by that walk.

### Confirmation

`tests/unit/test_worker_pages.py` checks the cursor against malformed and foreign input, its round trip through a URL
and the filter names used as metric labels. `tests/integration/test_worker_pages.py` registers workers through the API
and walks the list against the database. It checks that inactive workers are listed and that workers joining and
leaving between pages do not shift the rest of a walk. It also covers ties and the mixed sort directions of the keyset,
the online filter against the full list, owner privacy, the escaping of `name_contains`, the refusals, the owner's
list, a fixed statement count per page and the metric labels. The locust `MetaBrowser` walks the list under load, and
`MisuseUser` probes the refusals.

## Pros and Cons of the Options

### Page the registered workers in the database with a keyset cursor, on a sibling endpoint

- Good: The list includes inactive workers, which are not in any snapshot.
- Good: Existing clients of `GET /v2/workers` are untouched.
- Bad: Each page queries the database. The privileged fields are built in `get_details`, so each page loads its workers
  and builds their rows in Python.

### Page the cached active-worker snapshot with a keyset cursor

- Good: A page reads Redis and not the database.
- Bad: The snapshot holds only active workers, so the list still does not include inactive workers.
- Bad: The active list usually fits in one page, so paging it saves little.
- Bad: Each node keeps a local copy of the snapshot, and the copies can differ in age, so consecutive pages can show
  different counts for the same worker.

### Add inactive workers to `GET /v2/workers`

- Good: One endpoint.
- Bad: Clients that treat the list as the active workers break.
- Bad: The response grows with every registered worker, without a bound.

### Add `limit` and `offset` to `GET /v2/workers`

- Good: No new endpoint, and the response stays an array.
- Bad: The list still holds only active workers.
- Bad: An offset counts rows, so every worker that joins or leaves ahead of it shifts the rest of the walk.
- Bad: An array does not have room for a next link or a total, so clients have to guess when to stop.

## More Information

The behaviour, the limits, the return codes and the metrics are in [the worker listing
reference](../reference/worker_listing.md). If database time becomes most of the cost of a page, indexes on each
sort column and on the lower-cased name are the first candidates.

---
title: "Worker listing reference"
summary: "What the full worker list, the paged list of registered workers and an owner's worker list return to each caller, how a cursor walk behaves while workers change, and the list metrics."
topics: [workers]
order: 75
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Worker listing reference

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

Three endpoints list workers:

- `GET /v2/workers` returns every active worker in one JSON array. A worker is active when it checked in within the
  last 5 minutes.
- `GET /v2/workers/page` lists every registered worker, active or inactive, one bounded page at a time, with filters, a
  choice of order and a cursor.
- `GET /v2/workers/mine` pages through the caller's workers, online or offline, in the owner view.

The full list reads a [snapshot](#the-snapshot) that is rebuilt every 2 seconds. The two paged lists read the
database for each request. The steps for showing these lists in a front end are in [List workers in a front
end](../how-to/list_workers_in_a_frontend.md).
[ADR 0047](../decisions/0047-paginate-the-worker-list-with-a-keyset-cursor.md) records why the paged lists are separate
endpoints and why they page with a cursor.

## Code map

| Concept | File | Symbol |
| --- | --- | --- |
| Cursor, filters, orders | `horde/worker_pages.py` | `WorkerPageCursor`, `WorkerPageFilters`, `WorkerListSort`, `DEFAULT_WORKER_LIST_SORT` |
| Views and their privilege | `horde/worker_pages.py` | `WorkerListView` |
| Page size and argument limits | `horde/worker_pages.py` | `WorkerPageLimits` |
| Keyset query of the paged lists | `horde/database/functions.py` | `page_registered_workers`, `RegisteredWorkerPage` |
| Details row of one worker | `horde/classes/base/worker.py` | `WorkerTemplate.get_details`, `WORKER_ONLINE_SECONDS` |
| Snapshot job | `horde/database/threads.py` | `store_worker_list` |
| Snapshot read and fallback | `horde/database/functions.py`, `horde/horde_redis.py` | `read_worker_list_snapshot`, `LOCAL_CACHE_MAX_LIFETIME` |
| Bulk loading of workers | `horde/database/functions.py` | `get_active_workers_for_details`, `_query_workers_for_details` |
| Full list | `horde/apis/v2/base.py` | `Workers` |
| Paged lists | `horde/apis/v2/base.py` | `WorkerPageResource`, `WorkersPage`, `WorkersMine` |
| View choice and cache headers | `horde/apis/v2/base.py` | `_resolve_worker_list_view`, `_worker_list_response_headers` |
| Response model | `horde/apis/models/v2.py` | `response_model_worker_page` |
| Return codes | `horde/exceptions.py`, `README_return_codes.md` | `InvalidWorkerListLimit`, `InvalidWorkerListCursor`, `InvalidWorkerListFilter` |
| Metrics | `horde/metrics.py`, `horde/apis/v2/base.py` | `worker_list_reads`, `worker_list_rows`, `worker_list_snapshot_misses`, `_record_worker_list_read` |

`tests/unit/test_worker_pages.py` covers the cursor and the request parts that do not touch the database.
`tests/integration/test_worker_pages.py` covers the endpoints, with workers that register by popping for jobs. The
sections below name the test for each contract.

## Views

A view is the set of fields in each row. Every row is built by `WorkerTemplate.get_details`.

| View | Who gets it | Rows contain |
| --- | --- | --- |
| public | anyone without a moderator's key, on the full list and the paged list | the fields open to every caller, with `owner` and `messages` only for owners who set `public_workers` |
| moderator | a moderator's key, on the full list and the paged list | every field, including `ipaddr`, `contact`, `suspicious` and `paused` |
| owner | the owner's key, on `GET /v2/workers/mine` | the public fields with `owner`, `messages`, `ipaddr` and `contact`, without `suspicious` and `paused` |

A row of the paged list has the same fields that `GET /v2/workers/{id}` returns for that worker in the same view
(`test_an_inactive_worker_is_listed_with_the_fields_of_its_own_details`).

Moderator and owner responses are sent with `Cache-Control: private, no-store`, because they contain addresses and
contacts. Public responses do not have that header.
`test_only_moderator_rows_of_the_full_list_are_kept_out_of_shared_caches` and
`test_a_moderator_finds_a_private_owners_workers_with_addresses_and_no_caching` cover the header.

## The snapshot

The full list does not query the database for rows. The quorum node's `store_worker_list` job runs every 2 seconds and
stores two lists of the active workers in Redis for 300 seconds. The interval and the lifetime are also listed with the
other background jobs in [Operational
constants](operational_constants.md#queue-scans-availability-and-worker-measurements). The two lists are:

| Redis key | View |
| --- | --- |
| `worker_cache` | public |
| `worker_cache_privileged` | moderator |

A request reads its node's local Redis first, then the shared Redis. An entry copied into local Redis is kept for at
most 5 seconds. When the list is missing from both, `read_worker_list_snapshot` builds it from the database, logs a
warning and counts a snapshot miss. The node keeps the rebuilt list in its local Redis for `LOCAL_CACHE_MAX_LIFETIME` (5
seconds), so a burst of reads causes only one rebuild. The recovered snapshot is served within 5 seconds after the job
restarts. `test_a_missing_snapshot_is_rebuilt_once_and_kept_even_when_a_worker_has_a_message` and
`test_a_rebuilt_list_expires_with_the_local_cache_so_the_recovered_snapshot_takes_over` cover the fallback.

## The full list

`GET /v2/workers` lists only the active workers. Its API description points to `GET /v2/workers/page` for every
registered worker. It takes `type` or `name`. `name` matches one worker by exact name, ignoring case. When both are
set, only `name` is applied. The response is a JSON array of rows. A moderator's key gets the moderator view. Any other
key gets the public view, including a key that does not match an account. `test_the_full_list_returns_a_bare_array`
covers the shape.

## The paged list

`GET /v2/workers/page` lists every registered worker of the image, text and interrogation types, active or inactive. It
is limited to 90 requests a minute per address. A moderator's key gets the moderator view, and any other valid key or no
key gets the public view.

| Argument | Default | Meaning |
| --- | --- | --- |
| `limit` | 25 | Rows per page, from 1 to 100. |
| `cursor` | none | The `next_cursor` of the previous page. Omit it for the first page. |
| `sort` | `uptime` | The page order. See [Orders](#orders). |
| `type` | none | `image`, `text` or `interrogation`. |
| `model` | none | Only workers that advertise this exact model name. |
| `team_id` | none | Only workers in the team with this ID. |
| `owner_id` | none | Only workers owned by this account, among the workers whose owner is visible to the caller. |
| `name_contains` | none | Only workers whose name contains this text, ignoring case. At most 100 characters. |
| `online` | none | `true` for workers that checked in within the last 5 minutes, `false` for the others. |

A blank `model`, `team_id` or `name_contains` does not narrow the list. The response is:

```json
{"workers": [...], "next_cursor": "eyJ2IjoxLC...", "total": 412}
```

`total` is the number of registered workers that pass the filters when the page is read, across all pages.
`next_cursor` is null on the last page. With `online=true`, a walk returns the rows of the full list for the same view
(`test_the_online_filter_returns_the_rows_of_the_full_list`).

The paged list refuses a key that does not match an account with a 401, while the full list accepts it. A moderator who
mistypes a key gets an error on the paged list. On the full list, the same moderator gets the public view and may take
it for the whole list (`test_a_mistyped_key_is_refused_rather_than_read_as_the_public_view`).

### Orders

| `sort` | Order |
| --- | --- |
| `uptime` | longest uptime first (the default) |
| `requests_fulfilled` | most jobs completed first |
| `kudos_rewards` | most kudos earned first |
| `name` | name ascending, lower-cased by the database |

Uptime is the default, so workers with a long service record come first and names chosen to sort first alphabetically do
not fill the first page. Rows with the same value are ordered by ascending worker ID, so every order is total and the
cursor has an exact position. The list does not offer an order by suspicion. `TestOrders` covers the default, the ties
and the name order.

### Filters and privacy

The filters are applied in the database query. A public caller who filters by `owner_id` gets only the workers of owners
who made their workers public. For an owner who did not, the page is empty and `total` is 0, so the count does not show
how many workers the account runs (`test_the_public_cannot_find_a_private_owners_workers_by_owner`). An owner who uses
their API key on this endpoint also gets the public view. `GET /v2/workers/mine` lists their workers in the owner view.

`model` matches whole model names, because model names nest and a partial match would list workers that cannot serve
the model. An interrogation worker does not advertise models, so it does not match a `model` filter. `name_contains`
matches any part of a worker name after both are lower-cased. `%` and `_` in the search text match only themselves
(`test_a_name_search_lists_the_workers_whose_name_contains_the_text_in_any_case`,
`test_a_name_search_matches_percent_and_underscore_as_plain_characters`).

### Walking the list

Pass each `next_cursor` back with the same `sort` until it is null. The cursor holds the sort, the last row's sort value
and its worker ID. It does not hold filters, so a client may change its filters partway through. A cursor issued for one
sort is refused under another.

Page boundaries are best effort, because a worker's sort value can change during a walk:

- A worker whose value changes so that it moves past the cursor before its page is read is skipped by the walk.
- A worker whose value changes so that it moves from a page already read to a later position is listed twice.
- A worker registered, deleted or renamed during a walk can be listed, left out or listed twice, depending on where it
  lands relative to the cursor.
- Workers joining or leaving do not shift the rest of the walk, because each page starts after the last row's position
  in the order.
- A walk always ends, because each page starts strictly after the previous one.

Uptime is added at each check-in, and jobs and kudos are added as a worker works, so these values change mostly for
online workers. A client that needs an exact copy of the list has to reconcile walks itself.
`test_workers_joining_and_leaving_between_pages_do_not_shift_the_rest_of_the_walk` covers a walk by name while workers
join, leave and are deleted.

## The owner's list

`GET /v2/workers/mine` requires the `apikey` header and lists the workers owned by that key's account, online or not, in
the owner view. It takes the same arguments as the paged list except `owner_id`, and runs the same query restricted to
the account. A key that does not match an account is refused with 401 `InvalidAPIKey`. A moderator's key lists only the
workers of the moderator's account. The endpoint is limited to 90 requests a minute per address.
`TestOwnersListingTheirOwnWorkers` covers these rules, including `test_a_worker_shows_under_its_new_owner_at_once`.

## Statements per page

A page runs one statement that selects the IDs and sort values of the page's workers and counts `total`. A common table
expression holds the workers that pass the filters. The statement reads it once for the count and once for the page,
so PostgreSQL scans `workers` once. A page after a cursor with no rows left runs a second statement to count `total`,
because the first statement returns no row to hold it. The page then loads its workers with their relationships in
bulk, one statement for each relationship and worker type. The loading statement counts each worker's suspicion rows,
so a moderator page does not load the rows themselves. The number of statements does not depend on the page size, on
how many workers match, on how many suspicion rows a worker holds or on how many interrogation workers are listed
(`test_the_statement_count_does_not_grow_with_the_number_of_owned_workers` and
`test_the_statement_count_does_not_grow_with_suspicion_rows_or_interrogation_workers`).

## Return codes

| Status | `rc` | Cause |
| --- | --- | --- |
| 400 | `InvalidWorkerListLimit` | `limit` is outside 1 to 100. |
| 400 | `InvalidWorkerListCursor` | The cursor is malformed, longer than 1,024 characters, of an unknown version, issued for another sort, or holds a count outside the range of a 64-bit integer. |
| 400 | `InvalidWorkerListFilter` | `team_id` is not a UUID, `owner_id` is negative, or `name_contains` is longer than 100 characters. |
| 400 | (reqparse) | `sort`, `type` or `online` is not one of the listed values, or `apikey` is missing on the owner's list. |
| 401 | `InvalidAPIKey` | The `apikey` header does not match any account. |

`TestRefusedRequests` covers the 400 return codes.

## Metrics

| Instrument | Kind | Labels | Use |
| --- | --- | --- | --- |
| `horde.workers.list.reads` | counter | `horde.workers.list.endpoint` (`full`, `page`, `mine`), `.view` (`public`, `moderator`, `owner`), `.sort` (an order above, or `none` on the full list), `.filters` (the filter names in use, comma separated, or `none`), `.continued` (`true` when the read followed a cursor) | How much traffic reads each list, and how many walks go past the first page. |
| `horde.workers.list.rows` | histogram | `horde.workers.list.endpoint` | Rows per response. The full list's rows grow with the number of active workers, and the paged lists' rows stay at or under the limit. |
| `horde.workers.list.snapshot_misses` | counter | `horde.workers.list.view` | Full-list reads that rebuilt the list from the database. Any steady rate means the snapshot job on the quorum node has stopped. |

Every label value comes from a fixed set. Filter values, such as a model name, a name search or an owner ID, are not
used as labels. `test_reads_are_counted_by_endpoint_and_walk_position_with_bounded_labels` and
`test_reads_are_counted_under_their_own_endpoint_and_view` cover the labels.

## Sharp edges

- **Every page scans `workers` once and sorts unindexed columns.** No sort column has an index. The name order sorts by
  `lower(name)`, so `ix_workers_name`, which is on `name`, does not serve it. Every check-in updates `uptime`, so an
  index on it would add write cost to the busiest update path of the table. The cost of a page grows with the number of
  registered workers. Indexes are reconsidered if database time becomes most of the cost of a page, as ADR 0047
  records.
- **`total` can change during a walk.** It is counted again for each page.
- **A walk is not exact.** Workers whose sort values change during the walk can be skipped or listed twice, as described
  under [Walking the list](#walking-the-list).
- **The name search lower-cases in the database.** Characters with a different case-folded form, such as `ß`, do not
  match their expansions, so `STRASSE` does not match `Straße`.
- **An owner's key on the paged list gets the public view**, as on the full list. Owners who keep their workers private
  see the owner view only on `GET /v2/workers/mine`.
- **Shared keys are refused.** `find_user_by_api_key` does not accept shared keys, so a shared key gets a 401 on both
  paged lists and the public view on the full list.

## Related

- [List workers in a front end](../how-to/list_workers_in_a_frontend.md)
- [ADR 0047: list every registered worker in pages with a keyset
  cursor](../decisions/0047-paginate-the-worker-list-with-a-keyset-cursor.md)
- [Moderation operations reference](moderation_operations.md), for suspicion history and the paused-worker queue

---
title: "List workers in a front end"
summary: "Browse every registered worker one page at a time, load later pages, search and filter them, list an owner's workers, and handle the refusals by return code."
topics: [workers]
order: 60
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# List workers in a front end

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

A front end shows every registered worker, active or inactive, from `GET /api/v2/workers/page`, and loads later pages
with a cursor. It searches and filters the same endpoint, lists an owner's workers from `GET /api/v2/workers/mine`, and
recovers from each refusal by its return code. `GET /api/v2/workers` lists only the workers that checked in within the
last 5 minutes.

You need:

- Network access to the `/api/v2` endpoints of a horde deployment.
- For [step 4](#4-list-an-owners-workers), the API key of an account that runs workers.

Steps 1 and 2 are the core of the procedure. Steps 3 and 4 are independent options that build on the first page
request. Step 5 applies to every request.

The [worker listing reference](../reference/worker_listing.md) holds the full contract: every argument, the fields in
each view, the walk rules and the metrics. The commands below use these shell variables:

```bash
HORDE=<HORDE_URL>/api/v2
APIKEY=<your-api-key>
CLIENT_AGENT="<client name>:<version>:<contact>"
```

## 1. Load the first screen

Request one page without a cursor. The default order lists the workers with the longest uptime first.

```bash
curl -s "$HORDE/workers/page?limit=25" -H "Client-Agent: $CLIENT_AGENT" | jq '{total, next_cursor, shown: (.workers | length)}'
```

The body has `workers`, `next_cursor` and `total`. `shown` is at most 25. `total` is the number of registered workers
that pass the filters, across all pages. Each row has the same fields as `GET /api/v2/workers/{id}` returns for that
worker in the same [view](../reference/worker_listing.md#views), so the code that renders one worker also renders a
row here. The `online` field of each row shows whether the worker is active.

## 2. Load the next pages

Pass the previous page's `next_cursor` back as `cursor`, with the same `sort`, until `next_cursor` is null. A page that
shows workers on demand requests the next page when the user asks for it. A cursor is not tied to a session, so a
client may stop following it at any point.

```bash
CURSOR=$(curl -s "$HORDE/workers/page?limit=25" -H "Client-Agent: $CLIENT_AGENT" | jq -r '.next_cursor')
curl -s "$HORDE/workers/page?limit=25&cursor=$CURSOR" -H "Client-Agent: $CLIENT_AGENT" | jq '{next_cursor, shown: (.workers | length)}'
```

Check a walk against these rules, which are described in full under
[Walking the list](../reference/worker_listing.md#walking-the-list):

- The walk ends with `next_cursor` set to null.
- `total` can change between pages, because it is counted again for each page.
- A worker whose uptime, jobs or kudos change during the walk can be skipped or appear twice. These values change
  mostly for online workers.

If the rows are kept in one list on screen, drop a row whose worker ID is already shown.

## 3. Search and filter

Add filters to the first page request:

- `name_contains` matches any part of a worker name, ignoring case.
- `model` matches a whole model name.
- `type` is `image`, `text` or `interrogation`.
- `team_id` is a team's ID.
- `online` is `true` for active workers and `false` for the others.

```bash
curl -s "$HORDE/workers/page?name_contains=<part of a name>&type=image" -H "Client-Agent: $CLIENT_AGENT" | jq '[.workers[].name]'
curl -s "$HORDE/workers/page?model=<model name>&online=true" -H "Client-Agent: $CLIENT_AGENT" | jq '.total'
```

Every listed name contains the search text, and `total` counts only the matching workers. A blank filter value does
not narrow the list. When the user changes a search box, request the first page again without a cursor, so the results
start from the top of the new list.

## 4. List an owner's workers

The owner's list includes the owner's addresses and contact. The response has `Cache-Control: private, no-store`, and
a front end must not store it in a shared cache.

Send the owner's key to `GET /api/v2/workers/mine`. The list includes private workers and workers that are offline. Pass
`online=false` to show only the workers that have stopped checking in.

```bash
curl -s "$HORDE/workers/mine?online=false" -H "apikey: $APIKEY" -H "Client-Agent: $CLIENT_AGENT" | jq '[.workers[] | {name, online}]'
```

Every row has `"online": false`, and every `owner` is the account of the key. A change of owner or of the worker's
settings shows on the next request.

## 5. Handle refusals

Branch on the `rc` field of the error body, not on the message text.

| Status and `rc` | What the front end does |
| --- | --- |
| 400 `InvalidWorkerListCursor` | Request the first page again without a cursor. The cursor was altered or belongs to another sort. |
| 400 `InvalidWorkerListLimit` | Request a `limit` from 1 to 100. |
| 400 `InvalidWorkerListFilter` | Show the filter as invalid. `team_id` must be a UUID and `name_contains` at most 100 characters. |
| 401 `InvalidAPIKey` | Ask the user to check the API key. The paged lists refuse a key that does not match an account. |
| 429 | Wait and retry. Each paged list allows 90 requests a minute per address. |

To test the handling, request a page with `limit=0` and with `cursor=abc`. The front end shows the limit error for the
first request and starts again from the first page for the second.

```bash
curl -s "$HORDE/workers/page?limit=0" -H "Client-Agent: $CLIENT_AGENT" | jq '.rc'
curl -s "$HORDE/workers/page?cursor=abc" -H "Client-Agent: $CLIENT_AGENT" | jq '.rc'
```

The first command prints `"InvalidWorkerListLimit"` and the second prints `"InvalidWorkerListCursor"`.

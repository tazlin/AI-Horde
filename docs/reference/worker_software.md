---
title: "Worker software and validated backends"
summary: "Bridge and backend fields reported by workers on a pop, including the legacy bridge_agent string, and the admin registry of rules for validated text backends."
topics: [workers, requests, operations]
order: 110
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Worker software and validated backends

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [operations](../topics.md#operations), [requests](../topics.md#requests), [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

On every pop, a worker reports its bridge, which is the program that pops jobs, and its backend, which is the
inference server that generates them. For a text worker, the horde compares that report with an admin-maintained
registry of rules to determine whether the worker runs a validated backend. Only a worker with a validated backend can
take a text request with `validated_backends: true` (the default). A text worker without a validated backend earns 30%
of the text generation and uptime rewards. The decision is recorded in
[ADR 43](../decisions/0043-keep-validated-text-backends-in-an-admin-registry.md).

## Software report on a pop

Every pop route (`/v2/generate/pop`, `/v2/generate/text/pop` and `/v2/interrogate/pop`) accepts these fields:

| Field | Shape | Meaning |
| --- | --- | --- |
| `bridge` | `{name, version, url?}` | The program that pops jobs. When sent, the reported software is taken from `bridge` and `backend`. |
| `backend` | `{name, version?, api?}` | The inference server that receives jobs from the bridge. Requires `bridge`. Leave it out when the bridge is built into its backend. |
| `bridge_agent` | `name:version:url` | The legacy report, still accepted. |

After surrounding spaces are removed, names are 1 to 100 characters and versions 1 to 64. Neither can contain `:` or
`~`. `api` is `openai` or `kobold`. The legacy codes `oai` and `kai` are accepted as `openai` and `kobold`, and other
values are ignored. Unknown keys inside `bridge` and `backend` are ignored.

A pop is refused with `WorkerSoftwareInvalid` when `bridge` or `backend` breaks these limits, or when `backend` is sent
without `bridge`. A new worker whose bridge or backend name is profane is refused with `ProfaneBridgeAgent`, the same
code as for a profane `bridge_agent`.

When `bridge` is sent without `bridge_agent`, the horde stores `bridge_agent` as `name:version:url`, with `unknown`
for a missing URL. A bridge that also connects to older hordes should send both.

### Legacy `bridge_agent` string

A pop that sends only `bridge_agent` is parsed as `bridge[~backend[~api]]:version:url`:

| `bridge_agent` | Bridge | Backend | API |
| --- | --- | --- | --- |
| `AI Horde Worker:24:url` | AI Horde Worker | none | none |
| `AI Horde Worker~aphrodite~oai:24:url` | AI Horde Worker | aphrodite | openai |
| `AI Horde Worker~unknown~kai:24:url` | AI Horde Worker | unknown | kobold |
| `AI Horde Worker~~oai:24:url` | AI Horde Worker | unknown | openai |
| `TabbyAPI:0.0.1:url` | TabbyAPI | none | none |

A string with fewer than three `:`-separated fields, or with a version that is not a loose semantic version, is parsed
as the bridge `unknown`.

### Software report in worker details

`GET /v2/workers/{id}` and the worker lists return the stored report:

- `bridge`: `{name, version}`.
- `backend`: `{name, version, api}`, left out when the worker did not report a backend.
- `validated_backend` (text workers): whether a rule matches the worker.
- `bridge_agent`: the string, as before.

## Validated backend rules

A rule has a bridge name and, optionally, a backend name, each with an optional minimum version. A worker matches a
rule when all of these are true:

- The worker's bridge name equals the rule's bridge name, ignoring case.
- The worker's backend name equals the rule's backend name, ignoring case, or neither the rule nor the worker has a
  backend. A rule without a backend matches only a worker that does not report a backend.
- The reported version meets each minimum version set by the rule. Both are parsed as loose semantic versions, so `24`
  is parsed as `24.0.0`. If the rule sets a minimum version and the worker does not report that version, the worker
  does not match.

A new database starts with these rules, which accept exactly the strings accepted by the earlier hard-coded set:

| Bridge | Backend |
| --- | --- |
| AI Horde Worker | none |
| AI Horde Worker | aphrodite |
| KoboldCppEmbedWorker | none |
| TabbyAPI | none |

Each horde process reloads the rules when its cached copy is 15 seconds old. A change applies at once in the process
that made it, and within 15 seconds in other processes.

### Registry API

| Method and path | Who | Does |
| --- | --- | --- |
| `GET /v2/validated_backends` | anyone | Lists the rules. |
| `GET /v2/validated_backends/{id}` | anyone | Reads a rule. |
| `POST /v2/validated_backends` | admin | Creates a rule. `bridge_name` is required. |
| `PATCH /v2/validated_backends/{id}` | admin | Changes the fields sent in the body. A JSON null clears an optional field. |
| `DELETE /v2/validated_backends/{id}` | admin | Deletes a rule. |

An admin is a user whose alias (`name#id`) is listed in the `ADMINS` environment variable. A write sends that user's
API key in the `apikey` header. The body fields are `bridge_name`, `backend_name`, `min_bridge_version`,
`min_backend_version` and `description`. A body with an unknown field is refused.

| Return code | When |
| --- | --- |
| `NotAdmin` | A write from a user who is not an admin. |
| `ValidatedBackendInvalid` | A missing `bridge_name`, a name with `:` or `~`, a field too long, a minimum that is not a version, or an unknown field. |
| `ValidatedBackendExists` | Another rule has the same bridge and backend, ignoring case. |
| `ValidatedBackendNotFound` | The id is not a UUID or does not match any rule (HTTP 404). |

Reads are limited to 60 a minute per IP address and path, and writes to 30 a minute per API key.

### Adding the registry to an existing database

Run `sql_statements/6.1.0.txt` before deploying. It adds the software columns to the `workers` table, creates the
`validated_backends` table and inserts the seed rules. Every statement is idempotent. On a fresh database, the
application creates the table and inserts the seed rules itself.

## Code map

| Concept | File and symbol |
| --- | --- |
| Structured report, legacy parse and resolution | `horde/worker_software.py`: `BridgeReport`, `BackendReport`, `parse_legacy_bridge_agent`, `resolve_reported_software` |
| Rule matching, seeds and the process snapshot | `horde/validated_backends.py`: `ValidatedBackendRule`, `LEGACY_VALIDATED_BACKEND_SEEDS`, `validated_backend_registry` |
| Stored rules and admin writes | `horde/database/validated_backends.py` |
| Rule table | `horde/classes/base/validated_backend.py`: `ValidatedBackend` |
| Worker columns and validation check | `horde/classes/base/worker.py`: `WorkerTemplate.software`, `set_software`; `horde/classes/kobold/worker.py`: `TextWorker.is_validated_backend` |
| Pop validation | `horde/apis/v2/base.py`: `JobPopTemplate.resolve_software` |
| Registry API | `horde/apis/v2/validated_backends.py` |
| Migration | `sql_statements/6.1.0.txt` |

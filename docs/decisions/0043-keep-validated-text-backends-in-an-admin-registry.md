---
status: proposed
date: 2026-11-23
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Keep validated text backends in an admin registry and report worker software as structured fields

The list of validated text backends is stored as database rows that admins edit through an admin API. It replaces the
`LLM_VALIDATED_BACKENDS` set in `horde/bridge_reference.py`. A worker reports its software in structured `bridge` and
`backend` pop fields. The legacy `bridge_agent` string is still accepted.

## Context and Problem Statement

A text request with `validated_backends` set to true (the default) goes only to workers whose backend is trusted to
apply the request's parameters as sent. A worker that does not run a validated backend also earns only 30% of the text
generation and uptime rewards. The validated backends were a set of five entries in code:

- `AI Horde Worker`
- `AI Horde Worker~aphrodite~oai`
- `AI Horde Worker~aphrodite~kai`
- `KoboldCppEmbedWorker`
- `TabbyAPI`

Each entry was compared with the name part of `bridge_agent`, which is the text before its first `:`. Adding a backend,
or removing one that was found to misbehave, needed a code change and a deploy. The bridge and backend were joined into
that one name with `~`. The name could not hold the backend's version, and the API dialect was a three-letter code. The whole name had to match an entry exactly.

The AI Horde Worker is gaining an OpenAI-compatible chat path that works with any server implementing
`/v1/chat/completions`, so workers will soon report many more backends.

How should the validated backends be administered, and how should a worker report its bridge and backend?

## Decision Drivers

- Admins should be able to change the validated backends without a deploy.
- On the day of the deploy, every worker keeps its current validation status.
- Workers that send only `bridge_agent` keep working and keep their validation status.
- The pop query evaluates every candidate worker, so checking whether one worker is validated must not add a database
  read.
- A rule should be able to require a minimum bridge or backend version.

## Considered Options

- A database registry edited by admins, with structured software fields on the pop.
- The registry alone, matched against the `bridge_agent` name.
- A setting on `HordeSettings` holding the list.
- Keep the set in code.

## Decision Outcome

Chosen option: a database registry edited by admins, with structured software fields on the pop. A change to the
validated backends no longer needs a deploy, and the backend version and API dialect get separate fields. Every
existing `bridge_agent` string keeps its meaning.

### Registry rules

A rule has a bridge name and, optionally, a backend name, each with an optional minimum version. A worker matches a
rule when all of these are true:

- The bridge names are equal, ignoring case.
- The backend names are equal, ignoring case, or neither the rule nor the worker has a backend. A rule without a
  backend matches only a worker that does not report a backend, such as a worker whose bridge is built into its
  backend.
- The reported version meets each minimum version set by the rule. Both are parsed as loose semantic versions, so `24`
  is parsed as `24.0.0`. If the rule sets a minimum version and the worker does not report that version, the worker
  does not match.

The `validated_backends` table holds the rules, one per bridge and backend pair. A unique index on the lower-cased
names enforces that. A new table starts with four seed rules, which accept exactly the strings accepted by the old set.
The two aphrodite entries are one rule, since the API dialect is reported beside the backend and is not part of a rule.
A rule deleted by an admin is not restored on the next boot.

Anyone can read the rules at `GET /v2/validated_backends`. Only users listed in the `ADMINS` environment variable can
create, change or delete them. Moderators cannot, because the rules affect every text worker's rewards.

Each horde process keeps a snapshot of the rules and reloads them when the snapshot is 15 seconds old. A write
refreshes the snapshot in the writing process at once. If a refresh fails, the previous snapshot is kept and the
failure is logged.

### Software report on a pop

A pop may send:

```json
{
  "bridge": {"name": "AI Horde Worker", "version": "25.0.0", "url": "https://github.com/Haidra-Org/AI-Horde-Worker"},
  "backend": {"name": "llamacpp", "version": "0.3.2", "api": "openai"}
}
```

- A pop that sends `backend` without `bridge` is refused with `WorkerSoftwareInvalid`.
- Names and versions cannot contain `:` or `~`, so a structured report can always be written as a `bridge_agent`
  string.
- `api` is `openai` or `kobold`. The legacy codes `oai` and `kai` are accepted as `openai` and `kobold`. Other values
  are ignored instead of refused, so a bridge can report a newer dialect to an older horde.
- When `bridge` is sent, the reported software is taken from `bridge` and `backend`, and `bridge_agent` is not parsed.
  When a pop sends `bridge` without `bridge_agent`, the horde stores `bridge_agent` as `name:version:url`. Code that
  reads the string, such as the image capability gates, keeps working.
- A pop that sends only `bridge_agent` is parsed as before. The name is split on `~` into bridge, backend and API. An
  empty backend part is parsed as the backend `unknown`.

The worker row stores the resolved bridge name and version, and the backend name, version and API dialect. For a row
written before these columns existed, the software is read from `bridge_agent` until the worker's next pop. Worker
details include `bridge` and `backend`, and text worker details also include `validated_backend`.

### Consequences

- Good, because admins can change the validated backends without a deploy.
- Good, because the backend version and API dialect are reported as fields, and a rule can require a minimum version.
- Good, because every legacy `bridge_agent` string gets the same validation result as before, and unit tests check
  this.
- Neutral, because two strings refused by the old set are now accepted: a name in a different case, and
  `AI Horde Worker~aphrodite` without an API code or with an unrecognised one. Both strings identify a validated
  backend.
- Bad, because a rule change takes up to 15 seconds to apply in other processes.
- Bad, because an existing database needs `sql_statements/6.1.0.txt` before the deploy, since `db.create_all()` does
  not add columns to the `workers` table.

## Pros and Cons of the Options

### The registry alone, matched against the `bridge_agent` name

- Good, because the pop fields do not change.
- Bad, because the `bridge_agent` name still cannot hold the backend version, so a rule cannot require a minimum
  backend version.
- Bad, because a rule has to spell the whole `~`-joined name, including the API code.

### A setting on `HordeSettings` holding the list

- Good, because the settings row already exists and is cached.
- Bad, because a JSON list in one row does not give each rule an identity for auditing or deletion, and does not
  enforce uniqueness.

### Keep the set in code

- Good, because the code stays as it is.
- Bad, because every change still needs a deploy.

## Confirmation

- `tests/unit/test_validated_backends.py` checks that the seed rules accept the five legacy strings and refuse similar
  strings. It also checks the matching, the minimum versions, and the snapshot's refresh and failure paths.
- `tests/unit/test_worker_software.py` checks the legacy parse of the deployed strings, the structured report and the
  `bridge_agent` string built from it.
- `tests/integration/test_validated_backends.py` checks the admin API's permission checks and validation, the refused
  reports and the worker details. It also checks that after a new rule is added, a worker can take a
  `validated_backends` request that it could not take before.

## More Information

[Worker software and validated backends](../reference/worker_software.md) describes the pop fields and the registry
API in detail.

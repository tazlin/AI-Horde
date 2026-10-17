---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Route by declared parameter support and refuse unsupported values by default

## Context and Problem Statement

Workers differ in which sampling and capability fields they honour. A client that sends `frequency_penalty` or
`tools` to a worker that ignores them receives a reply that silently differs from what it asked for. What happens
to a request field no eligible worker supports?

## Decision Drivers

- Nothing behaviour-changing may be silently discarded.
- Capability fields (`tools`, `response_format`, image parts) can never be dropped; they change the answer.
- Stock clients send sampling fields at their default values unprompted; refusing those would break everything.
- OpenRouter-aware clients expect a lenient mode to exist.

## Considered Options

- Route by declared support; a default-valued field counts as omitted; a non-default unsupported sampling field is
  refused unless the client opts into dropping it
- Route by declared support; drop unsupported sampling fields by default and report them
- Refuse every unsupported field, including defaults

## Decision Outcome

Chosen option: "Route by declared support; a default-valued field counts as omitted; a non-default unsupported
sampling field is refused unless the client opts into dropping it".

Capability fields are hard requirements: the request is routed only to workers declaring them, and when none is
online the answer is 503 with `Retry-After`. Sampling fields at their documented default are treated as omitted. A
non-default sampling field that no eligible worker supports is refused with 400 naming the field, unless
`horde.require_parameters` is false, in which case it is dropped and listed in the response under
`x_horde.ignored_parameters`.

### Consequences

- Good: The default never hides a changed answer; the lenient mode is explicit.
- Bad: Some clients will meet a 400 on a field they always send, and must either stop sending it or opt in.

## Pros and Cons of the Options

### Refuse by default, opt into dropping

- Good: Honest by default.
- Bad: Friction for clients that spray fields.

### Drop by default, report

- Good: Matches OpenRouter; fewer 400s.
- Bad: The default hides a changed answer in a field most clients never read.

### Refuse everything

- Good: Simplest rule.
- Bad: Breaks every client that sends defaults.

## More Information

This record and [ADR 0030](0030-route-unsupported-parameters-by-declared-support-ignoring-by-default.md) are
mutually exclusive. Accepting one rejects the other. Ruling R5 of the review chose this record.

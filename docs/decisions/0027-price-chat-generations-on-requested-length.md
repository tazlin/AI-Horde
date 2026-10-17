---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Price chat generations on the requested length, with the basis isolated

## Context and Problem Statement

Text generations are priced on `max_length` at submit (`horde/classes/kobold/waiting_prompt.py`,
`TextWaitingPrompt.calculate_kudos`, settled in `horde/classes/kobold/processing_generation.py`), and the worker is
rewarded on the same basis. OpenAI clients routinely request 4096 or more tokens and receive short replies. With
contract v2 the worker reports completion tokens. On what basis are chat generations priced?

## Decision Drivers

- The established text pricing is a product position users and workers understand.
- Reported token counts are claims from volunteers until audited.
- The policy is under consideration; the code must not foreclose either outcome.

## Considered Options

- Requested length, as text, with the charge basis isolated in one function and reported counts stored
- Reported completion tokens, bounded by the requested length
- A blend: reserve on the requested length, settle on reported tokens

## Decision Outcome

Chosen option: "Requested length, as text, with the charge basis isolated in one function and reported counts
stored".

Chat generations charge and reward on `max_length` exactly as text does. The basis is computed by one function per
request type, which today returns the requested length. The reported prompt and completion counts and the basis used
are stored on every generation and shown in the response, so the data to decide a later policy accumulates from the
first request.

### Consequences

- Good: No accounting change at cutover; one function to change later.
- Bad: A short reply to a large `max_tokens` costs the full request; the default of 512 and the model list's output
  limit are the mitigation.

## Pros and Cons of the Options

### Requested length

- Good: Established; workers are paid for capacity they commit.
- Bad: Overcharges short replies.

### Reported tokens

- Good: Fair per reply; what OpenAI-shaped clients expect.
- Bad: Trusts worker reports; changes worker incentives.

### Reserve then settle

- Good: Both properties.
- Bad: The settlement step is new accounting machinery.

## More Information

This record and [ADR 0028](0028-price-chat-generations-on-reported-completion-tokens.md) are mutually exclusive.
Accepting one rejects the other. Ruling R18 of the review chose this record as the hedge while the position is
considered.

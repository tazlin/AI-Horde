---
status: proposed
date: 2026-11-25
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Limit shared keys by owner policy and link accounts by network pseudonym

## Context and Problem Statement

A shared key lets an account spend its kudos and priority without handing out its personal API key. Before this
record, a request made with a shared key ran as the owner in every respect except the per-job size limits and the
key's kudos budget.

- A trusted owner's key let anyone disable batching and skip the raid-mode VPN check.
- A moderator owner's key exempted the holder from the IP timeout that a filtered prompt causes.
- A service account's key let the holder set `proxied_account`.

An owner could not limit a key to one kind of request, one model or one network, and could not stop a leaked key
except by deleting it. Moderators could act only on the owner account. That punished an owner whose key leaked, and
the people using the key were not affected.

The IP protections acted on one address at a time, and accounts were not connected to each other. The owner of a
flagged account could register again from the same network. One person could also register many pseudonymous
accounts, collect kudos on each and transfer the kudos to one main account.

This record settles two questions. What may a request made with a shared key do on the owner's behalf? How can the
horde tell that several accounts belong to one person without keeping their addresses?

## Decision Drivers

- A delegated credential does not get more rights than its owner holds, and loses a right when the owner loses it.
- Restrictions are always inherited from the owner. Allowances are granted to the key one at a time.
- Keys in use on the day of the deploy keep working without the owner doing anything.
- No raw address is stored. Links are kept for a short fixed time and shown only to moderators.
- Shared networks (schools, households, carrier-grade NAT) are common, so a link is evidence for a person to weigh.
  A link does not refuse anything by itself.
- The moderators are volunteers. No check waits on a moderator or sends one a notice, and a person on a shared network
  has a way in without contacting anyone.
- The shared key checks read only the key and the request, so they do not call Redis. Every other new check on the
  request path costs at most one Redis call.

## Considered Options

- Keep shared keys as owner proxies and rely on deleting leaked keys
- Copy the owner's roles onto each key at creation
- Refuse linked transfers and pseudonymous registrations on flagged networks until a moderator clears them
- Intersect the owner's current roles with a per-key grant, and link accounts by HMAC pseudonym

## Decision Outcome

Chosen option: "Intersect the owner's current roles with a per-key grant, and link accounts by HMAC pseudonym".

**Effective roles.** A request made with a key gets the owner's roles that are listed in the key's `roles`. The
list can name `trusted`, `education`, `special` and `service`, and the owner can only grant a role that the owner
holds at that time. The `trusted` grant also covers the supporter allowances, because the generation path treats them
the same way. The moderator role is not effective through a key. Restrictions (flagged, deleted, suspicious, and the
forced SFW and trusted workers of an education account) always come from the owner. `RequestPrincipal` in
`horde/access_control.py` is the one place that applies this rule.

**Owner policy.** The owner can set these limits on a key:

- request types (`scopes`)
- models (`allowed_models`)
- a number of waiting generations (`max_concurrency`)
- CIDR ranges (`allowed_networks`)

The owner can suspend and reactivate a key. The model allowlist and the concurrency limit do not apply to a key bound
to a style, because the style's owner chose the style's models and parameters. Every other limit applies. When a
style's key does not allow a request, the request runs on the caller's key.

**Filtered prompts and moderator suspension.** A filtered prompt through a key emits the
`horde.sharedkey.filtered_prompt` event with the key and owner IDs, and the holder's address is timed out as before.
The key stays usable. The owner's account does not get a suspicion for the prompt, because the holder is not the
owner. A moderator can suspend a key with a reason, and only a moderator can lift it. The key stores the latest
reason and the time of the change, and each suspension or reactivation emits the `horde.sharedkey.status_changed`
event with the actor and both statuses.

**Legacy keys.** The migration adds columns and does not write rows. A key with NULL `roles` is a legacy key that has
every role that its owner holds, except moderator and service. The migration gives each existing key of a service
account every grantable role, service included, so the key keeps the allowances that it had before 6.1.0. New keys
start without roles. Other NULL or -1 policy
values mean that the limit is not set.

**Account links.** A registered account using its personal key is recorded against the HMAC pseudonym of its network (an
IPv4 address or an IPv6 /64), the same pseudonym that prompt evidence uses. Each account has one row per network.
Registration, a key reset on the register page and a kudos transfer update the row. Generation updates it at most once
per account, network and hour, and does not update it when Redis is unavailable. Requests with the anonymous key, shared
keys and service accounts are not recorded. Links expire after 30 days. Two accounts that share a pseudonym in that window are linked. A link has three effects:

- A kudos transfer between linked accounts goes through. It emits the `horde.account_links.linked_transfer` event with
  the account IDs and the amount, unless the sender is trusted or the receiver is an education account.
  `horde.kudos.transfers` counts every completed transfer by link status. A retried transfer with
  the same idempotency key is not reported again.
- A style surcharge paid to a linked style author emits `horde.account_links.linked_transfer` with the policy
  `style_surcharge`, and a generation completed by a linked worker owner emits
  `horde.account_links.shared_network_generation`. Neither is refused.
- Two network signals raise the reCAPTCHA v3 score needed by a pseudonymous registration: a flagged or deleted account
  on the network, and three pseudonymous accounts created in the last day and seen on the network. The score is then
  `RECAPTCHA_LINKED_NETWORK_MIN_SCORE` (default 0.7) instead of `RECAPTCHA_MIN_SCORE` (default 0.5). A registrant
  below it gets the form again with a 403 and can register with OAuth. When Google cannot be reached, the form is
  shown again with a 503. Without reCAPTCHA, the registration is allowed. An OAuth registration on a flagged
  network gets the `LINKED_TO_RESTRICTED_ACCOUNT` suspicion. Each of these registrations emits the
  `horde.account_links.registration_signals` event, which does not include an address.
- Moderators can list an account's networks and the other accounts on each, or the accounts on one pseudonym.

### Consequences

- Good: A stranger with a key no longer gets the owner's moderator, service or trusted allowances by default.
- Good: An owner can bind a key to an integration's servers, a model and a concurrency limit, which limits the cost of a
  leak.
- Good: The filtered prompts sent through a leaked key are reported for the key, and its owner keeps the account.
- Good: A person on a school or carrier network can register and transfer kudos without a moderator.
- Good: Ban evasion from the same network needs a high reCAPTCHA score or an OAuth identity, and moderators can read
  the events whenever they choose.
- Good: The metrics show the share of transfer volume between linked accounts, and whether reCAPTCHA runs at all.
- Good: The database does not hold addresses. Moderators move between prompt evidence and account links with one value.
- Bad: A farm on one network can still move its kudos to a main account. The transfer is reported and not refused.
- Bad: Without reCAPTCHA, a registration from a flagged network is only reported.
- Bad: The concurrency limit is checked before the request is stored, so concurrent requests can each pass and
  overshoot it by the number in flight.
- Bad: Links are not recorded on a deployment without a real `secret_key`, and every link rule then allows the action.
- Bad: Legacy keys keep their owner's trusted and education roles until the owner sets `roles`.
- Bad: The database does not keep a key's history. A key stores only its latest status reason, and the status events
  are kept as long as the structured logs.

## Pros and Cons of the Options

### Keep shared keys as owner proxies and rely on deleting leaked keys

- Good: The schema and the request path stay as they are.
- Bad: The moderator and service delegation stays, and deleting a leaked key breaks the legitimate integration with it.

### Copy the owner's roles onto each key at creation

- Good: One lookup on the request path.
- Bad: A key keeps a role after its owner loses it, which is the escalation ruled out by the decision drivers.
- Bad: The migration would write a row update per key and a role check per owner.

### Refuse linked transfers and pseudonymous registrations on flagged networks until a moderator clears them

- Good: A farm on one network cannot cash out, and ban evasion from the same network needs an OAuth identity.
- Bad: Households, schools and carrier networks are refused, and each one needs a volunteer moderator to clear it.
- Bad: A Discord notice for each OAuth registration on a flagged network is work for moderators, and a Discord
  message cannot be redacted later.

## Confirmation

`tests/unit/test_access_control.py` covers the role intersection, policy validation, network matching and the
suspension check. `tests/integration/test_sharedkey_access.py` covers the owner, holder and moderator behavior through
the API, including style keys and filtered prompts. `tests/integration/test_account_links.py`
covers transfers, registration with and without reCAPTCHA, the moderator listing and the requests that are not
recorded. `tests/unit/test_recaptcha.py` covers the score and action checks, the thresholds and an outage at Google.
`tests/integration/test_access_control_migration.py` applies the migration twice over a pre-6.1.0 key and compares its
schema with the ORM's. `tests/stress/locustfile_access_control.py` checks the concurrency limit under load, and that a
transfer between linked accounts is not refused.

## More Information

The events, metrics and settings are listed in the
[shared keys and account links reference](../reference/shared_keys_and_account_links.md). The metrics do not have an
account, key or address attribute. The moderator endpoints are listed in the
[moderation operations reference](../reference/moderation_operations.md).

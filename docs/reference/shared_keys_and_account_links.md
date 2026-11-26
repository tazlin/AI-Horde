---
title: "Shared keys and account links reference"
summary: "What a shared key may do for its owner, how accounts are linked by network pseudonym, what a link changes for transfers and registration, and the events, metrics and settings that report it."
topics: [moderation, operations, kudos]
order: 66
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Shared keys and account links reference

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [kudos](../topics.md#kudos), [moderation](../topics.md#moderation), [operations](../topics.md#operations)
<!-- END GENERATED: topics -->

A request made with a shared key gets only the owner roles granted to the key, and the owner's limits on the key apply
before the request is stored. A filtered prompt sent with a key is reported for the key, and the key stays usable.
Registered accounts are linked when they use one network pseudonym within 30 days. A link does not refuse a kudos
transfer, a style surcharge or a generation. Each of them is reported with a structured event. A pseudonymous
registration from a network with review signals needs a higher reCAPTCHA score, and a registrant below that score can
still register with OAuth. These checks do not send moderators a notice.

The reasons for the design are in [Shared keys and linked accounts](../explanation/shared_keys_and_linked_accounts.md)
and [ADR 0044](../decisions/0044-shared-key-policies-and-account-links.md). The moderator endpoints are listed in the
[moderation operations reference](moderation_operations.md#shared-keys), and the review procedure is in
[Review linked accounts and registration signals](../how-to/review_linked_accounts.md).

## Code map

| Concept | File | Symbol |
| --- | --- | --- |
| Effective roles of a request | `horde/access_control.py` | `RequestPrincipal` |
| Policy validation | `horde/access_control.py` | `SharedKeyPolicyInput`, `parse_policy_input`, `check_owner_policy` |
| Per-request policy check | `horde/access_control.py` | `check_sharedkey_access` |
| Suspended key check | `horde/classes/base/user.py` | `UserSharedKey.is_valid` |
| Policy check on generation requests | `horde/apis/v2/base.py` | `GenerateTemplate._check_sharedkey_access` |
| Policy updates and status changes | `horde/database/sharedkeys.py` | `apply_policy_update`, `change_sharedkey_status` |
| Filtered prompt report | `horde/database/sharedkeys.py` | `report_sharedkey_filtered_prompt` |
| Link rows | `horde/classes/base/account_link.py` | `IpSubjectAccountLink` |
| Recording a link | `horde/account_links.py` | `record_account_link` |
| Transfer assessment and report | `horde/account_links.py` | `assess_linked_transfer`, `report_linked_transfer` |
| Style surcharge report | `horde/apis/v2/base.py` | `GenerateTemplate.report_linked_style_surcharge` |
| Shared-network generation report | `horde/account_links.py` | `report_shared_network_generation` |
| Registration policies and report | `horde/account_links.py` | `evaluate_registration`, `report_registration` |
| reCAPTCHA verification | `horde/recaptcha.py` | `verify_registration_token` |
| Register page | `horde/routes.py` | `register` |
| Kudos transfer with link status | `horde/database/functions.py` | `transfer_kudos` |
| Retention job | `horde/database/threads.py` | `purge_account_links` |
| Moderator endpoints | `horde/apis/v2/access_operations.py` | `OperationsSharedKey`, `OperationsAccountLinks` |
| Metrics | `horde/metrics.py` | `account_links_decisions`, `kudos_transfers_completed`, `recaptcha_verifications` |

## Shared keys

### Effective roles

A request made with a shared key acts as the owner, with fewer allowances. The key's `roles` list grants owner roles
one at a time, and a granted role applies only while the owner holds it.

| Role | Allowance through a key |
| --- | --- |
| `trusted` | Batching control, extra source images, the raid-mode network check bypass, the rate-limit allowlist and the supporter allowances |
| `education` | The replacement filter and the rate-limit allowlist |
| `special` | Special models and the `special` payload |
| `service` | Setting `proxied_account` and the rate-limit allowlist. Only a service account holds it. |

The moderator role does not apply through a key, and an owner cannot grant a role that the owner does not hold
(`403 SharedKeyRoleNotHeld`). Restrictions always come from the owner: flagged, deleted and suspicious accounts, and
the forced SFW and trusted workers of an education account. A key with NULL `roles` was created before 6.1.0, and it
keeps every owner role except moderator and service until the owner sets `roles`. The 6.1.0 migration gives each such
key of a service account the role list `trusted`, `education`, `special` and `service`, so the key keeps the
allowances that it had before 6.1.0. A new key starts without roles.

### Owner limits

The owner sets the limits with `PUT /api/v2/sharedkeys` or `PATCH /api/v2/sharedkeys/<id>`. An update changes only the
fields sent. A null clears `allowed_models` or `allowed_networks`, and a null in any other field counts as not sent. A
malformed or out-of-range value returns `400 InvalidSharedKeyPolicy`. A request that a limit refuses returns `403` with
the code in the last column.

| Field | Values | Refusal |
| --- | --- | --- |
| `scopes` | `image_generation`, `text_generation` (both by default). `text_generation` includes chat requests. | `SharedKeyScopeDenied` |
| `allowed_models` | Up to 100 model names, or unset for any model | `SharedKeyModelDenied` |
| `max_concurrency` | 1 to 500 waiting generations, or -1 for no limit | `SharedKeyConcurrencyExceeded` |
| `allowed_networks` | Up to 20 CIDR ranges, or unset for any network | `SharedKeyNetworkDenied` |

A key bound to a style ignores `allowed_models` and `max_concurrency`, because the style's owner chose the models.
Every other limit applies. When a style's key does not allow a request, the request runs on the caller's key.

The network allowlist uses the request's address. A request from a proxy with a known passkey, such as the
OpenAI-compatible chat front, uses the end user's address in `Proxied-For`. Each end user behind the proxy is then
checked against the allowlist separately.

### Status and filtered prompts

| Status | Set by | Lifted by |
| --- | --- | --- |
| `active` | The owner or a moderator | |
| `suspended_by_owner` | The owner | The owner or a moderator |
| `suspended_by_moderator` | A moderator, with a reason | A moderator only (`403 SharedKeyModeratorSuspension` for the owner) |

Every request through a suspended key returns `403 SharedKeySuspended`, before any other policy check.

Each status change stores its reason in `status_reason` and its time in `status_changed`, which only the owner and
moderators see. An owner's change clears the reason. Each change also emits the `horde.sharedkey.status_changed` event.
A key created with a status starts with it, and its creation does not emit the event.

A prompt through a key that the prompt filter refuses times out the holder's address, as for any other account. The
owner's account does not get a suspicion for it, because the holder is not the owner. The key stays `active`. The
refusal is counted in `horde.sharedkey.filtered_prompts` and reported in the `horde.sharedkey.filtered_prompt` event.
A moderator who reads the reports can suspend the key. A filtered prompt from the owner's personal key gives the owner
a suspicion, as before, and is not reported for the owner's keys.

## Account links

### What is recorded

A registered account using its personal key is recorded against the pseudonym of its network. The pseudonym is the
`ip_subject_key` HMAC of an IPv4 address or an IPv6 /64, the same value that prompt evidence records. Each account has
one row per network, with `first_seen` and `last_seen`. These requests update the row:

- A submission of the register page, which creates an account or resets an existing account's key.
- An image or text generation request, at most once an hour for each account and network. The hourly limit is kept in
  Redis, so a generation does not update the row while Redis is unavailable.
- The sender's kudos transfer.

The anonymous account, shared keys and service accounts are not recorded. Without a private `secret_key`, the
pseudonym is not computed, links are not recorded, and every policy below allows the request. A row is deleted 30
days after its `last_seen`, in batches of 5000 by `purge_account_links`.

Two accounts are linked while both have a row for the same pseudonym within the 30-day window.

### Kudos transfers

A transfer between linked accounts is not refused. After the kudos move, `assess_linked_transfer` gives the transfer
one link status, and `report_linked_transfer` reports it.

| Link status | Condition | Report |
| --- | --- | --- |
| `unlinked` | Same account, the anonymous account, or no shared network in the window | None |
| `exempt` | The sender is trusted, or the receiver is an education account | None |
| `linked` | None of the above | The `horde.account_links.linked_transfer` event, and `advisory` in `horde.account_links.decisions` |

A transfer to a shared key credits the key's owner, so the owner is the receiver in the assessment. A retry with the
same `Idempotency-Key` does not move kudos again, and it is not counted or reported again.

### Style surcharges

A request that uses another account's style pays the style's author a surcharge at activation. The surcharge is
assessed like a transfer from the requester to the author, and a linked pair is reported with `policy` set to
`style_surcharge`. The request is not affected.

### Generations on a shared network

When a generation is completed, the request's address is compared with the worker's latest address by network
pseudonym. A generation for another account on the worker's network emits the
`horde.account_links.shared_network_generation` event and is counted in
`horde.account_links.shared_network_generations`. A generation for the worker's owner is not reported. The comparison
uses two HMAC computations and does not query the database.

### Registration

When `RECAPTCHA_SECRET_KEY` is set, reCAPTCHA is checked first. A submission that does not pass shows the register form
again, with the submitted name and a message above the form. The account is not created.

| Outcome | Status | Message |
| --- | --- | --- |
| `missing_token` | 400 | Allow scripts on the page and try again |
| `failed`, `wrong_action` | 400 | Submit the form again |
| `low_score` (below `RECAPTCHA_MIN_SCORE`) | 400 | Try again |
| `unavailable` (the verification endpoint failed) | 503 | Try again in a few minutes |

The message does not mention the score or the thresholds. On a pseudonymous form, it also offers the OAuth login
buttons. Then the IP timeout applies, and raid mode refuses every pseudonymous registration.

A pseudonymous registration then checks two signals on the network:

- `linked_to_restricted_account`: a flagged or deleted account used the network within 30 days.
- `registration_limit`: three pseudonymous accounts that were created in the last day and seen on the network. An
  account counts whichever request recorded it on the network.

| Signals | reCAPTCHA | Outcome |
| --- | --- | --- |
| None | Any | `allowed` |
| One or both | Score at least `RECAPTCHA_LINKED_NETWORK_MIN_SCORE` | `verified`, the account is created |
| One or both | Score below it | `refused`, the form is shown again with status 403 and the OAuth login buttons |
| One or both | Not configured | `advisory`, the account is created |

A passed verification without a score comes from a v2 key and meets every threshold. An OAuth registration is always
allowed. On a network used by a flagged or deleted account, its outcome is `flagged`.

Each decision with a signal emits the `horde.account_links.registration_signals` event. A new account with the
`linked_to_restricted_account` signal also gets the `LINKED_TO_RESTRICTED_ACCOUNT` suspicion, one point of the five
that make an account suspicious.

## Configuration

| Variable | Default | Effect |
| --- | --- | --- |
| `secret_key` | Not set | Enables network pseudonyms. Without it, links are not recorded. |
| `RECAPTCHA_SITE_KEY`, `RECAPTCHA_SECRET_KEY` | Empty | Enable reCAPTCHA on the register page. |
| `RECAPTCHA_MIN_SCORE` | `0.5` | The score every registration needs. |
| `RECAPTCHA_LINKED_NETWORK_MIN_SCORE` | `0.7` | The score a pseudonymous registration with a signal needs. It is never lower than `RECAPTCHA_MIN_SCORE`. |

The scores are read at each registration. A value that is not a number between 0 and 1 logs a warning and uses the
default.

## Events

The events are `logfire.info` records, exported with the deployment's other logs. The events do not include an address.

| Event | Attributes |
| --- | --- |
| `horde.sharedkey.filtered_prompt` | `sharedkey_id`, `owner_id` |
| `horde.sharedkey.status_changed` | `sharedkey_id`, `owner_id`, `actor` (`owner`, `moderator`), `actor_user_id`, `old_status`, `new_status`, `has_reason` |
| `horde.account_links.linked_transfer` | `policy`, `source_user_id`, `dest_user_id`, `amount`, `shared_networks` |
| `horde.account_links.shared_network_generation` | `requester_id`, `requester_kind`, `worker_owner_id`, `worker_id`, `kudos` |
| `horde.account_links.registration_signals` | `policy`, `outcome`, `signals`, `user_id` (None when refused), `linked_restricted_user_ids`, `recent_pseudonymous_registrations`, `recaptcha_score`, `required_score`, `subject_key_prefix` |

`subject_key_prefix` is the first 12 characters of the network pseudonym. A moderator finds the full pseudonym in the
account's link listing.

## Metrics

These metrics do not have an account, key or address attribute.

| Metric | Attributes |
| --- | --- |
| `horde.sharedkey.refusals` | `horde.rc` |
| `horde.sharedkey.status_changes` | `horde.actor`, `horde.status` |
| `horde.sharedkey.filtered_prompts` | |
| `horde.sharedkey.legacy_requests` | |
| `horde.account_links.decisions` | `horde.policy`, `horde.outcome` |
| `horde.account_links.shared_network_generations` | `horde.requester` (`registered`, `anonymous`) |
| `horde.account_links.shared_network_generations.kudos` | `horde.requester` |
| `horde.account_links.purged` | |
| `horde.kudos.transfers` | `horde.transfer_type`, `horde.link_status` |
| `horde.kudos.transfers.kudos` | `horde.transfer_type`, `horde.link_status` |
| `horde.recaptcha.verifications` | `horde.outcome` (`passed`, `low_score`, `wrong_action`, `missing_token`, `failed`, `unavailable`, `disabled`) |
| `horde.recaptcha.score` | Histogram in steps of 0.1 |

## Contracts and tests

| Contract | Test |
| --- | --- |
| A key does not get the owner's roles until granted, and loses a role with the owner | `tests/unit/test_access_control.py`, `TestEffectiveRoles` |
| An owner's policy input is validated, and an explicit null clears only an allowlist | `tests/unit/test_access_control.py`, `TestOwnerPolicyValidation` |
| Each owner limit refuses with its own code | `tests/unit/test_access_control.py`, `TestRequestChecks`; `tests/integration/test_sharedkey_access.py`, `TestOwnerLimits` |
| A filtered prompt through a key times out the holder, is reported for the key, and does not give the owner a suspicion | `tests/integration/test_sharedkey_access.py`, `TestFilteredPrompts` |
| The network allowlist checks each end user behind the chat front | `tests/integration/test_sharedkey_access.py`, `TestChatFront` |
| An owner cannot lift a moderator suspension | `tests/integration/test_sharedkey_access.py`, `TestSuspension` |
| A linked transfer goes through and is reported, with the receiver's account for a shared key | `tests/integration/test_account_links.py`, `TestKudosCashOut` |
| A retried transfer is reported once | `tests/integration/test_account_links.py`, `TestKudosCashOut` |
| A surcharge paid to an author on the requester's network is reported | `tests/integration/test_account_links.py`, `TestStyleSurcharge` |
| A generation requested from the worker's network is reported with its kudos | `tests/integration/test_account_links.py`, `TestSharedNetworkGeneration` |
| Shared keys and service accounts do not link their owners, and generations are not recorded without Redis | `tests/integration/test_account_links.py`, `TestWhatIsRecorded` |
| A registration with signals needs the higher score, and OAuth stays open | `tests/integration/test_account_links.py`, `TestRegistration` |
| The registrant's address is not sent to Google, and an outage does not pass | `tests/unit/test_recaptcha.py`, `TestVerification` |
| A bad threshold falls back to the default | `tests/unit/test_recaptcha.py`, `TestThresholds` |
| The migration applies twice over a pre-6.1.0 key | `tests/integration/test_access_control_migration.py` |

## Sharp edges

- The concurrency limit is checked before the request is stored. Concurrent requests can each pass the check and
  overshoot the limit by the number in flight.
- A key keeps only its latest status reason. Earlier reasons and policy changes are not stored, so a moderator reads
  them from the `horde.sharedkey.status_changed` events while the logs keep them.
- A registration over the limit is still recorded, so `recent_pseudonymous_registrations` keeps growing on a busy
  network while reCAPTCHA is off or the registrants pass it.
- `horde.recaptcha.score` is recorded only for a v3 key. A v2 key passes every threshold, so the flagged-network score
  does not apply to it.
- A link listing is capped at 50 networks for an account and 50 accounts for each network. A registration event lists
  at most 50 restricted accounts. The caps do not change a decision.

## Related

- [Moderation operations reference](moderation_operations.md): the moderator API, prompt evidence and countermeasures.
- `README_return_codes.md` at the repository root: every shared key and account link return code.

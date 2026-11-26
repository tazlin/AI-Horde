---
title: "Shared keys and linked accounts"
summary: "Why a shared key gets only the roles granted to it, why a shared network is evidence and not proof, and why the sockpuppet checks report and ask for reCAPTCHA instead of waiting on a moderator."
topics: [moderation, kudos]
order: 30
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Shared keys and linked accounts

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [kudos](../topics.md#kudos), [moderation](../topics.md#moderation)
<!-- END GENERATED: topics -->

A shared key is handed to people outside the owner's control, so it gets only the roles granted by the owner, and only
while the owner holds them. Sockpuppet accounts are hard to tell apart from people who share a school, an office or a
carrier's network. The horde records which accounts share a network pseudonym and treats that link as evidence. A
linked transfer is reported for a moderator to read later and is not refused. A registration from a suspicious network
needs a higher reCAPTCHA score or an OAuth login. The checks work without a moderator, because the horde's moderators
are volunteers.

The fields, return codes, events and metrics are in the
[shared keys and account links reference](../reference/shared_keys_and_account_links.md). The decision and its
rejected options are in [ADR 0044](../decisions/0044-shared-key-policies-and-account-links.md).

## Shared keys as delegated credentials

A shared key lets a class, a Discord bot or an integration spend an account's kudos without the account's personal
key. A key that runs as its owner hands every holder the owner's privileges. A moderator's key would exempt its holders
from IP timeouts, a trusted owner's key would let anyone disable batching, and a service account's key would let anyone
set `proxied_account`. Such a key can only be stopped by deleting it, which also breaks the integration that uses it.

The rule follows the usual practice for delegated credentials. A key holds a subset of the owner's rights, granted
one at a time, and loses a right when the owner loses it. Copying the owner's roles onto the key at creation was
rejected, because a key would keep a role after a moderator took it from the owner. Restrictions always come from the
owner, so a flagged owner cannot hand out an unflagged key.

The owner's limits (request types, models, networks and concurrency) bound the cost of a leak. A key pinned to a
school's CIDR range does not work from elsewhere. A filtered prompt through a key times out the holder's address and is
reported for the key, and the key stays usable. The owner's account does not get a suspicion for it, because the prompt came from a holder and the owner
did not send it. The owner or a moderator can suspend the key.

## Accounts that share a network

Farming kudos takes many free accounts and one main account. Each account collects kudos, and a transfer moves the
kudos to the main account. Ban evasion follows a similar pattern: a new pseudonymous account from the network of the
banned one.

The horde links two accounts when both used the same network pseudonym in the last 30 days. The pseudonym is an HMAC
of the address, so the database does not hold an address, and the 30-day window keeps the record short. The links come
from the requests that are evidence about one person: a submission of the register page, generation with a personal
key, and sending a transfer. Shared keys and service accounts are left out, because their networks belong to the holders and to the
service's users.

A link is weak evidence. Households, schools, offices, libraries and carrier-grade NAT put unrelated people on one
address, and IPv4 carrier NAT is common on mobile networks. Treating a link as proof punishes those people, and
needing a moderator for every false positive moves the work to volunteers.

## Reports instead of refusals

A stricter design refuses a transfer between linked accounts until a moderator clears the pair. It also refuses
pseudonymous registrations on networks with signals, and sends a Discord notice for each OAuth registration from a
flagged network. Each of those outcomes either blocks a person or creates a task for a moderator, so it was rejected.

The horde keeps the evidence and removes the waiting:

- A linked transfer goes through. It emits a structured event with both account IDs, the amount and the number of
  shared networks, and the completed-transfer metrics count it as `linked`. A moderator reads the events when they
  choose to, for example while reviewing an account that was already reported for something else.
- A registration with signals asks reCAPTCHA for a higher score. A person on a school network usually passes, and a
  registration script usually does not. A registrant who does not pass can log in with OAuth, which ties the account
  to an outside identity.
- An OAuth registration on a flagged network gets one suspicion point and an event. A Discord message cannot be
  redacted later, and it needs someone to read it.

The volume metric is what makes the reports useful. `horde.kudos.transfers` by link status shows how much of the
transfer volume runs between linked accounts, before anyone decides that it needs a refusal.

## Properties and costs

The design buys these properties:

- People on shared networks can register and transfer kudos without contacting anyone.
- Moderators get structured events and a link listing that they can read whenever they choose, and the horde does
  not send them anything.
- The database and the events do not hold an address.
- An operator can see from `horde.recaptcha.verifications` whether reCAPTCHA is on, and from `horde.recaptcha.score`
  where the threshold falls on real registrants.

It costs these:

- A farm on one network can still cash out kudos. The transfer is reported, but the kudos move. A refusal can be added
  later, if the metrics show that the advisory mode is not enough.
- Without reCAPTCHA, a registration with signals is only reported. The protection against ban evasion then depends on
  the event being read.
- A reCAPTCHA score is a probability estimate from Google. A person on a heavily abused network can score low and must
  use OAuth instead.
- Links are not recorded on a deployment without a private `secret_key`, and then every check allows the request.

## Related

- [Review linked accounts and registration signals](../how-to/review_linked_accounts.md): the moderator and operator
  procedure.
- [Moderation operations reference](../reference/moderation_operations.md): the rest of the moderator API.

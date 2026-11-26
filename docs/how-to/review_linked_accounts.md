---
title: "Review linked accounts and registration signals"
summary: "Turn on reCAPTCHA for the register page, tune its scores from the metrics, and read the linked-transfer, generation and registration events."
topics: [moderation, operations]
order: 20
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Review linked accounts and registration signals

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [moderation](../topics.md#moderation), [operations](../topics.md#operations)
<!-- END GENERATED: topics -->

These procedures set up and read the sockpuppet checks. The checks work without them, because a linked transfer is
allowed and a registration with signals is decided by reCAPTCHA. Use the procedures to turn on the registration score,
to tune it, and to look into accounts when you choose to.

Preconditions: the deployment runs 6.1.0 or later with a private `secret_key`, and its metrics and logs reach a
backend that you can query. The moderator procedures also need a moderator API key. The rules and the event fields are
in the [shared keys and account links reference](../reference/shared_keys_and_account_links.md).

The examples use these placeholders:

- `<horde-url>`: the base URL of the deployment, such as the public API host.
- `<moderator-api-key>`: a moderator's API key.
- `<user-id>`: an account ID from an event or a listing.
- `<window>`: a Prometheus range such as `7d`.

Metric names below are in Prometheus form: dots become underscores, and attribute names follow the same rule.

## Check whether reCAPTCHA is on

1. Query the registration verifications by outcome:

   ```
   sum by (horde_outcome) (increase(horde_recaptcha_verifications[<window>]))
   ```

   Verify: `disabled` counts registration form submissions without reCAPTCHA. On a deployment with reCAPTCHA on,
   `passed` increases and `disabled` does not.

## Turn on reCAPTCHA for registration

End state: every registration form submission is verified, and a registration with signals needs the higher score.

1. Create a reCAPTCHA v3 key pair for the register page's domain in the Google reCAPTCHA admin console. A v2 key also
   works, but it does not return a score, so the higher score for flagged networks does not apply to it.
2. Set `RECAPTCHA_SITE_KEY` and `RECAPTCHA_SECRET_KEY` in the environment of every API process, and restart them.
3. Register a test account on the register page. Verify: the account is created, and `horde_recaptcha_verifications`
   with `horde_outcome="passed"` increases by one.

Reversal: unset `RECAPTCHA_SECRET_KEY` and restart. Registrations with signals are then allowed and reported, and
`disabled` counts rise again.

## Tune the scores

End state: the thresholds refuse scripted registrations and rarely refuse people on shared networks.

1. Read the score distribution of real registrations:

   ```
   sum by (le) (increase(horde_recaptcha_score_bucket[<window>]))
   ```

2. Read how many pseudonymous registrations with signals were refused and verified:

   ```
   sum by (horde_outcome) (increase(horde_account_links_decisions{horde_policy="pseudonymous_registration"}[<window>]))
   ```

3. Set `RECAPTCHA_MIN_SCORE` (every registration, default `0.5`) or `RECAPTCHA_LINKED_NETWORK_MIN_SCORE` (registrations
   with signals, default `0.7`) to a value between 0 and 1, and restart the API processes. The second value is never
   lower than the first. Verify: the API logs do not show a warning that the value is not a number between 0 and 1,
   and the `refused` share in step 2 changes over the next window.

Reversal: unset the variable and restart, which restores the default.

## Read the linked-transfer reports

A transfer between linked accounts goes through and emits the `horde.account_links.linked_transfer` event. Read the
reports when an account comes up for another reason, or when the volume looks unusual.

1. Compare linked and unlinked transfer volume:

   ```
   sum by (horde_link_status) (increase(horde_kudos_transfers_kudos[<window>]))
   ```

   Verify: the response has a `linked` series, which is the volume between accounts that share a network.
2. Find the receivers of linked transfers. In Logfire's SQL explorer:

   ```sql
   SELECT attributes->>'dest_user_id' AS receiver, count(*) AS transfers, sum((attributes->>'amount')::float) AS kudos
   FROM records
   WHERE message = 'horde.account_links.linked_transfer'
   GROUP BY receiver
   ORDER BY kudos DESC
   ```

   In another log backend, filter on the same message and group by the `dest_user_id` attribute. The `policy`
   attribute is `kudos_transfer` for a transfer and `style_surcharge` for a surcharge paid to a style's author.
3. List the networks and the other accounts of one receiver:

   ```sh
   curl -s -H "apikey: <moderator-api-key>" "<horde-url>/api/v2/operations/moderation/account_links?user_id=<user-id>"
   ```

   Verify: each network lists the other accounts seen there, and whether each one is flagged, deleted or trusted.

Act on what you find with the usual moderator tools. The link does not prove that the accounts have one owner.

## Read the shared-network generation reports

A generation completed by a worker whose owner was on the requester's network emits the
`horde.account_links.shared_network_generation` event. The generation and its kudos are not affected.

1. Compare the reported kudos by requester kind:

   ```
   sum by (horde_requester) (increase(horde_account_links_shared_network_generations_kudos[<window>]))
   ```

   Verify: the series are `registered` and `anonymous`.
2. Find the worker owners with the most reported kudos. In Logfire's SQL explorer:

   ```sql
   SELECT attributes->>'worker_owner_id' AS owner, count(*) AS generations, sum((attributes->>'kudos')::float) AS kudos
   FROM records
   WHERE message = 'horde.account_links.shared_network_generation'
   GROUP BY owner
   ORDER BY kudos DESC
   ```

3. Open the owner's link listing as in the previous procedure, and compare the requesters with the accounts listed
   there.

## Read the registration reports

A pseudonymous registration with signals, and an OAuth registration on a flagged network, emit the
`horde.account_links.registration_signals` event.

1. Filter the logs on that message. `outcome` is `advisory` (reCAPTCHA off), `verified` (passed the higher score),
   `refused` (did not pass, `user_id` is empty) or `flagged` (OAuth).
2. For a created account, open the account's link listing as in the previous procedure. Verify: an account with the
   `linked_to_restricted_account` signal has the `LINKED_TO_RESTRICTED_ACCOUNT` suspicion in its details.

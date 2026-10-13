# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The pop-selection oracles judge only what the harness can prove from its own timestamps.

Each case builds a ledger by hand: requests queued, pops sent and answered, submits, cancels and status reads at
chosen instants. A legal interleaving must never be reported, however unlucky its timing; each behaviour the
oracles exist for must be reported when it happens.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("LOCUST_SKIP_MONKEY_PATCH", "1")

from tests.stress.locustsuite.pop_ledger import (
    Oracle,
    PopLedger,
    PopRecord,
    ProbeRecord,
    RequestKind,
    StatusRecord,
    SubmitRecord,
    TrackedRequest,
    WorkerProfile,
    prompt_matches_blacklist,
)

LANE = "lane-text"
OWNER = "owner#1"
FRIEND = "friend#2"
STRANGER = "stranger#3"


def worker(name: str = "w1", **fields) -> WorkerProfile:
    fields.setdefault("owner", OWNER)
    return WorkerProfile(name=name, lane=LANE, gentype="text", **fields)


def queue(ledger: PopLedger, nonce: str, *, at: float, n: int = 1, owner: str = STRANGER, **fields) -> TrackedRequest:
    request = TrackedRequest(
        request_id=f"id-{nonce}",
        nonce=nonce,
        lane=LANE,
        gentype="text",
        owner=owner,
        n=n,
        prompt=fields.pop("prompt", f"a prompt {nonce}"),
        kind=fields.pop("kind", RequestKind.SERVABLE),
        sent_at=at - 0.1,
        accepted_at=at,
        **fields,
    )
    ledger.record_request(request)
    return request


def pop(
    ledger: PopLedger,
    worker_name: str,
    *,
    sent: float,
    received: float | None,
    nonce: str | None = None,
    gens: int = 1,
    status: int = 200,
    rc: str | None = None,
    amount: int = 1,
    ttl: int | None = 150,
    skipped: dict | None = None,
    job_prefix: str = "",
) -> PopRecord:
    record = PopRecord(worker=worker_name, lane=LANE, sent_at=sent, received_at=None, amount=amount)
    ledger.record_pop_sent(record)
    if received is None:
        return record
    record.received_at = received
    record.status = status
    record.rc = rc
    record.skipped = skipped or {}
    if nonce is not None:
        record.job_ids = tuple(f"{job_prefix}{worker_name}-{nonce}-{sent}-{index}" for index in range(gens))
        record.nonce = nonce
        record.ttl = ttl
    ledger.complete_pop(record)
    return record


def pause(ledger: PopLedger, worker_name: str, start: float, end: float | None = None) -> None:
    """Record ``worker_name`` as certainly paused from ``start`` to ``end``, its transitions taking 0.05 each."""
    interval = ledger.begin_state("paused", worker_name, start - 0.05)
    ledger.acknowledge_begin(interval, start)
    if end is not None:
        ledger.end_state(interval, end)
        ledger.acknowledge_end(interval, end + 0.05)


def submit(ledger: PopLedger, job_id: str, state: str, *, sent: float, received: float | None, status: int | None = 200) -> None:
    ledger.record_submit_sent(SubmitRecord(job_id=job_id, state=state, sent_at=sent, received_at=received, status=status))


def violations(ledger: PopLedger, oracle: Oracle | None = None, end_time: float = 10_000.0):
    found, _ = ledger.evaluate(end_time=end_time)
    return [violation for violation in found if oracle is None or violation.oracle is oracle]


# --------------------------------------------------------------------------- #
# Over-claiming                                                               #
# --------------------------------------------------------------------------- #


class TestOverclaim:
    def test_a_request_handed_out_beyond_its_n_is_reported(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        ledger.register_worker(worker("b"))
        queue(ledger, "r", at=1.0)
        first = pop(ledger, "a", sent=2.0, received=2.5, nonce="r")
        submit(ledger, first.job_ids[0], "ok", sent=3.0, received=3.1)
        pop(ledger, "b", sent=4.0, received=4.5, nonce="r")

        assert len(violations(ledger, Oracle.OVERCLAIM)) == 1

    def test_a_generation_faulted_before_the_next_pop_answered_may_be_handed_out_again(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        ledger.register_worker(worker("b"))
        queue(ledger, "r", at=1.0)
        first = pop(ledger, "a", sent=2.0, received=2.5, nonce="r")
        # The fault is still in flight when the second pop is answered: the server may have processed it first.
        submit(ledger, first.job_ids[0], "faulted", sent=4.2, received=None, status=None)
        pop(ledger, "b", sent=4.0, received=4.5, nonce="r")

        assert violations(ledger, Oracle.OVERCLAIM) == []

    def test_a_generation_faulted_only_after_the_next_pop_answered_does_not_excuse_it(self):
        ledger = PopLedger()
        queue(ledger, "r", at=1.0)
        first = pop(ledger, "a", sent=2.0, received=2.5, nonce="r")
        pop(ledger, "b", sent=4.0, received=4.5, nonce="r")
        submit(ledger, first.job_ids[0], "faulted", sent=5.0, received=5.1)

        assert len(violations(ledger, Oracle.OVERCLAIM)) == 1

    def test_one_job_faulted_twice_gives_back_one_generation(self):
        ledger = PopLedger()
        queue(ledger, "r", at=1.0)
        first = pop(ledger, "a", sent=2.0, received=2.5, nonce="r")
        submit(ledger, first.job_ids[0], "faulted", sent=3.0, received=3.1)
        submit(ledger, first.job_ids[0], "faulted", sent=3.2, received=3.3, status=400)
        pop(ledger, "b", sent=4.0, received=4.5, nonce="r")
        pop(ledger, "a", sent=5.0, received=5.5, nonce="r")

        found = violations(ledger, Oracle.OVERCLAIM)
        assert len(found) == 1
        assert found[0].evidence["handed_out"] == 3
        assert found[0].evidence["given_back_at_most"] == 1

    def test_a_job_faulted_after_its_delivery_was_acknowledged_gives_nothing_back(self):
        ledger = PopLedger()
        queue(ledger, "r", at=1.0)
        first = pop(ledger, "a", sent=2.0, received=2.5, nonce="r")
        submit(ledger, first.job_ids[0], "ok", sent=3.0, received=3.1)
        submit(ledger, first.job_ids[0], "faulted", sent=3.2, received=3.3, status=400)
        pop(ledger, "b", sent=4.0, received=4.5, nonce="r")

        assert len(violations(ledger, Oracle.OVERCLAIM)) == 1

    def test_an_abandoned_job_may_be_handed_out_again_once_its_ttl_passed(self):
        ledger = PopLedger()
        queue(ledger, "r", at=1.0)
        pop(ledger, "a", sent=2.0, received=2.5, nonce="r", ttl=10)
        pop(ledger, "b", sent=12.5, received=13.0, nonce="r")

        assert violations(ledger, Oracle.OVERCLAIM) == []

    def test_an_abandoned_job_handed_out_again_before_its_ttl_is_reported(self):
        ledger = PopLedger()
        queue(ledger, "r", at=1.0)
        pop(ledger, "a", sent=2.0, received=2.5, nonce="r", ttl=10)
        pop(ledger, "b", sent=5.0, received=5.5, nonce="r")

        assert len(violations(ledger, Oracle.OVERCLAIM)) == 1

    def test_fake_jobs_do_not_count_against_n(self):
        ledger = PopLedger()
        ledger.register_worker(worker("paused"))
        pause(ledger, "paused", 0.5, 2.8)
        queue(ledger, "r", at=1.0)
        pop(ledger, "paused", sent=2.0, received=2.5, nonce="r")
        pop(ledger, "b", sent=3.0, received=3.5, nonce="r")

        assert violations(ledger, Oracle.OVERCLAIM) == []

    def test_a_paused_worker_serves_its_owner_for_real(self):
        ledger = PopLedger()
        ledger.register_worker(worker("paused"))
        pause(ledger, "paused", 0.5)
        # The pop is answered before the requester has its own response: the request is known only afterwards.
        queue(ledger, "r", at=2.7, owner=OWNER)
        pop(ledger, "paused", sent=2.0, received=2.5, nonce="r")
        pop(ledger, "b", sent=3.0, received=3.5, nonce="r")

        assert len(violations(ledger, Oracle.OVERCLAIM)) == 1

    def test_a_handout_during_a_pause_transition_is_not_counted_either_way(self):
        ledger = PopLedger()
        ledger.register_worker(worker("paused"))
        pause(ledger, "paused", 2.3)
        queue(ledger, "r", at=1.0)
        pop(ledger, "paused", sent=2.0, received=2.5, nonce="r")
        pop(ledger, "b", sent=3.0, received=3.5, nonce="r")

        assert violations(ledger) == []

    def test_concurrent_pops_splitting_a_batch_within_n_are_legal(self):
        ledger = PopLedger()
        queue(ledger, "r", at=1.0, n=3)
        pop(ledger, "a", sent=2.0, received=3.0, nonce="r", gens=2, amount=2)
        pop(ledger, "b", sent=2.1, received=2.9, nonce="r", gens=1, amount=4)

        assert violations(ledger) == []


# --------------------------------------------------------------------------- #
# Turned away                                                                 #
# --------------------------------------------------------------------------- #


class TestTurnedAway:
    def test_an_empty_pop_while_servable_work_certainly_waited_is_reported(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        queue(ledger, "r", at=1.0)
        pop(ledger, "a", sent=2.0, received=2.5)

        found = violations(ledger, Oracle.TURNED_AWAY)
        assert len(found) == 1
        assert found[0].evidence["waiting"][0]["request_id"] == "id-r"

    def test_work_claimed_by_an_overlapping_pop_was_not_certainly_available(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        ledger.register_worker(worker("b"))
        queue(ledger, "r", at=1.0)
        pop(ledger, "b", sent=2.4, received=3.0, nonce="r")
        pop(ledger, "a", sent=2.0, received=2.5)

        assert violations(ledger, Oracle.TURNED_AWAY) == []

    def test_an_unanswered_overlapping_pop_may_have_claimed_the_work(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        queue(ledger, "r", at=1.0)
        pop(ledger, "b", sent=2.4, received=None)
        pop(ledger, "a", sent=2.0, received=2.5)

        assert violations(ledger, Oracle.TURNED_AWAY) == []

    def test_a_server_error_on_an_overlapping_pop_may_have_claimed_the_work(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        queue(ledger, "r", at=1.0)
        pop(ledger, "b", sent=2.4, received=2.6, status=500)
        pop(ledger, "a", sent=2.0, received=2.5)

        assert violations(ledger, Oracle.TURNED_AWAY) == []

    def test_a_request_accepted_after_the_pop_was_sent_was_not_certainly_queued(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        queue(ledger, "r", at=2.2)
        pop(ledger, "a", sent=2.0, received=2.5)

        assert violations(ledger, Oracle.TURNED_AWAY) == []

    def test_a_request_whose_cancel_was_in_flight_was_not_certainly_queued(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        queue(ledger, "r", at=1.0)
        ledger.record_cancel_sent("r", 2.4)
        pop(ledger, "a", sent=2.0, received=2.5)

        assert violations(ledger, Oracle.TURNED_AWAY) == []

    @pytest.mark.parametrize(
        ("profile", "request_fields"),
        [
            (worker("a"), {"targeted_worker": "b"}),
            (worker("a"), {"excluded_worker": "a"}),
            (worker("a", blacklist=("zebra",)), {"prompt": "a ZEBRA r"}),
            (worker("a", refused_kinds=frozenset([RequestKind.FLOOD])), {"kind": RequestKind.FLOOD}),
        ],
        ids=["targets-another", "excludes-this", "blacklisted", "refused-kind"],
    )
    def test_a_request_the_worker_may_not_serve_does_not_count(self, profile, request_fields):
        ledger = PopLedger()
        ledger.register_worker(profile)
        queue(ledger, "r", at=1.0, **request_fields)
        pop(ledger, "a", sent=2.0, received=2.5)

        assert violations(ledger, Oracle.TURNED_AWAY) == []

    def test_a_request_once_faked_to_this_worker_does_not_count(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        pause(ledger, "a", 0.5, 1.9)
        queue(ledger, "r", at=1.0)
        pop(ledger, "a", sent=1.5, received=1.8, nonce="r")
        pop(ledger, "a", sent=2.0, received=2.5)

        assert violations(ledger, Oracle.TURNED_AWAY) == []

    def test_a_worker_in_maintenance_refused_while_its_owner_waited_is_reported(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        interval = ledger.begin_state("maintenance", "a", 0.5)
        ledger.acknowledge_begin(interval, 0.6)
        queue(ledger, "own", at=1.0, owner=OWNER)
        queue(ledger, "theirs", at=1.0, owner=STRANGER)
        pop(ledger, "a", sent=2.0, received=2.5, status=403, rc="WorkerMaintenance")

        found = violations(ledger, Oracle.TURNED_AWAY)
        assert [entry["request_id"] for entry in found[0].evidence["waiting"]] == ["id-own"]

    def test_a_worker_in_maintenance_with_only_strangers_waiting_is_rightly_refused(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        interval = ledger.begin_state("maintenance", "a", 0.5)
        ledger.acknowledge_begin(interval, 0.6)
        queue(ledger, "theirs", at=1.0, owner=STRANGER)
        pop(ledger, "a", sent=2.0, received=2.5, status=403, rc="WorkerMaintenance")

        assert violations(ledger) == []


# --------------------------------------------------------------------------- #
# Priority, order, targeting, blacklist, maintenance, tricks, cancellation    #
# --------------------------------------------------------------------------- #


def priority_worker(**fields) -> WorkerProfile:
    return worker(
        "p",
        priority_owners=frozenset([OWNER, FRIEND]),
        refused_kinds=frozenset([RequestKind.FLOOD]),
        is_priority_worker=True,
        **fields,
    )


class TestPriority:
    def test_a_stranger_served_while_a_priority_request_waited_is_reported(self):
        ledger = PopLedger()
        ledger.register_worker(priority_worker())
        queue(ledger, "friend", at=1.0, owner=FRIEND)
        queue(ledger, "stranger", at=0.5, owner=STRANGER)
        pop(ledger, "p", sent=2.0, received=2.5, nonce="stranger")

        assert len(violations(ledger, Oracle.PRIORITY_BYPASSED)) == 1

    def test_a_stranger_served_once_the_priority_request_was_taken_is_legal(self):
        ledger = PopLedger()
        ledger.register_worker(priority_worker())
        queue(ledger, "friend", at=1.0, owner=FRIEND)
        queue(ledger, "stranger", at=0.5, owner=STRANGER)
        pop(ledger, "general", sent=2.1, received=2.3, nonce="friend")
        pop(ledger, "p", sent=2.0, received=2.5, nonce="stranger")

        assert violations(ledger, Oracle.PRIORITY_BYPASSED) == []

    def test_unservable_priority_requests_do_not_hold_back_strangers(self):
        ledger = PopLedger()
        ledger.register_worker(priority_worker())
        queue(ledger, "flood", at=1.0, owner=FRIEND, kind=RequestKind.FLOOD)
        queue(ledger, "stranger", at=0.5, owner=STRANGER)
        pop(ledger, "p", sent=2.0, received=2.5, nonce="stranger")

        assert violations(ledger) == []

    def test_serving_a_priority_request_behind_a_deep_flood_is_counted(self):
        ledger = PopLedger()
        ledger.register_worker(priority_worker())
        for index in range(221):
            queue(ledger, f"flood{index}", at=1.0, owner=FRIEND, kind=RequestKind.FLOOD)
        queue(ledger, "friend", at=1.5, owner=FRIEND)
        pop(ledger, "p", sent=2.0, received=2.5, nonce="friend")

        _, coverage = ledger.evaluate(end_time=100.0)
        assert coverage["priority.served_behind_continued_read"] == 1


class TestOrder:
    def test_a_newer_request_served_while_an_older_one_waited_is_reported(self):
        ledger = PopLedger()
        ledger.register_worker(worker("f", fifo=True))
        queue(ledger, "old", at=1.0)
        queue(ledger, "new", at=1.5)
        pop(ledger, "f", sent=2.0, received=2.5, nonce="new")

        assert len(violations(ledger, Oracle.ORDER)) == 1

    def test_requests_served_oldest_first_are_legal(self):
        ledger = PopLedger()
        ledger.register_worker(worker("f", fifo=True))
        queue(ledger, "old", at=1.0)
        queue(ledger, "new", at=1.5)
        pop(ledger, "f", sent=2.0, received=2.5, nonce="old")
        pop(ledger, "f", sent=3.0, received=3.5, nonce="new")

        assert violations(ledger) == []

    def test_requests_queued_concurrently_carry_no_order(self):
        ledger = PopLedger()
        ledger.register_worker(worker("f", fifo=True))
        queue(ledger, "first", at=1.6)
        # Sent at 1.5, before the first one's response at 1.6: the server may have created it first.
        queue(ledger, "second", at=1.65)
        pop(ledger, "f", sent=2.0, received=2.5, nonce="second")

        assert violations(ledger, Oracle.ORDER) == []


class TestStaticRules:
    def test_a_targeted_request_reaching_another_worker_is_reported(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        queue(ledger, "r", at=1.0, targeted_worker="b")
        pop(ledger, "a", sent=2.0, received=2.5, nonce="r")

        assert len(violations(ledger, Oracle.TARGETING)) == 1

    def test_an_excluding_request_reaching_the_excluded_worker_is_reported(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        queue(ledger, "r", at=1.0, excluded_worker="a")
        pop(ledger, "a", sent=2.0, received=2.5, nonce="r")

        assert len(violations(ledger, Oracle.TARGETING)) == 1

    def test_a_blacklisted_prompt_reaching_the_worker_is_reported(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a", blacklist=("a_c",)))
        queue(ledger, "r", at=1.0, prompt="xA_Cx r")
        pop(ledger, "a", sent=2.0, received=2.5, nonce="r")

        assert len(violations(ledger, Oracle.BLACKLIST)) == 1

    def test_a_stranger_reaching_a_worker_certainly_in_maintenance_is_reported(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        interval = ledger.begin_state("maintenance", "a", 0.5)
        ledger.acknowledge_begin(interval, 0.6)
        queue(ledger, "r", at=1.0, owner=FRIEND)
        pop(ledger, "a", sent=2.0, received=2.5, nonce="r")

        assert len(violations(ledger, Oracle.MAINTENANCE)) == 1

    def test_a_pop_overlapping_a_maintenance_transition_is_not_judged(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        interval = ledger.begin_state("maintenance", "a", 2.2)
        ledger.acknowledge_begin(interval, 2.7)
        queue(ledger, "r", at=1.0, owner=FRIEND)
        pop(ledger, "a", sent=2.0, received=2.5, nonce="r")

        assert violations(ledger) == []

    def test_an_empty_200_while_certainly_in_maintenance_is_reported(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        interval = ledger.begin_state("maintenance", "a", 0.5)
        ledger.acknowledge_begin(interval, 0.6)
        ledger.end_state(interval, 3.0)
        ledger.acknowledge_end(interval, 3.1)
        pop(ledger, "a", sent=2.0, received=2.5)
        pop(ledger, "a", sent=4.0, received=4.5)

        assert len(violations(ledger, Oracle.MAINTENANCE)) == 1

    def test_a_request_handed_again_after_a_fake_job_is_reported(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        pause(ledger, "a", 0.5, 2.8)
        queue(ledger, "r", at=1.0)
        pop(ledger, "a", sent=2.0, received=2.5, nonce="r")
        pop(ledger, "a", sent=3.0, received=3.5, nonce="r")

        assert len(violations(ledger, Oracle.TRICK_REPEAT)) == 1

    def test_a_request_handed_out_by_a_pop_sent_after_its_cancellation_is_reported(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        queue(ledger, "r", at=1.0)
        ledger.record_cancel_sent("r", 1.5)
        ledger.record_cancel_acked("r", 1.6)
        pop(ledger, "a", sent=2.0, received=2.5, nonce="r")

        assert len(violations(ledger, Oracle.AFTER_CANCEL)) == 1

    def test_a_pop_racing_a_cancellation_may_still_receive_the_request(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a"))
        queue(ledger, "r", at=1.0)
        ledger.record_cancel_sent("r", 2.1)
        ledger.record_cancel_acked("r", 2.6)
        pop(ledger, "a", sent=2.0, received=2.5, nonce="r")

        assert violations(ledger) == []


@pytest.mark.parametrize(
    ("prompt", "words", "matches"),
    [
        ("a ZEBRA herd", ("zebra",), True),
        ("abc", ("a_c",), False),
        ("xA_Cx", ("a_c",), True),
        ("fifty percent", ("%",), False),
        ("100%", ("%",), True),
        ("back\\slash", ("\\",), True),
        ("İSTANBUL", ("istanbul",), False),
        ("ISTANBUL", ("istanbul",), True),
        ("STRASSE", ("straße",), False),
        ("STRAßE", ("straße",), True),
    ],
)
def test_the_reference_blacklist_comparison_is_a_lowercase_substring(prompt, words, matches):
    assert prompt_matches_blacklist(prompt, words) is matches


# --------------------------------------------------------------------------- #
# Status reads and reports                                                    #
# --------------------------------------------------------------------------- #


def status(
    nonce: str,
    *,
    finished: int,
    waiting: int = 0,
    processing: int = 0,
    final: bool = True,
    names=None,
    at: float = 50.0,
    done: bool = False,
) -> StatusRecord:
    return StatusRecord(
        nonce=nonce,
        observed_at=at,
        finished=finished,
        processing=processing,
        waiting=waiting,
        restarted=0,
        done=done,
        faulted=False,
        worker_names=names,
        final=final,
    )


class TestStatusReads:
    def test_more_finished_than_asked_for_is_reported(self):
        ledger = PopLedger()
        queue(ledger, "r", at=1.0, n=2)
        ledger.record_status(status("r", finished=3, final=False))

        assert len(violations(ledger, Oracle.OVERDELIVERED)) == 1

    def test_a_quiescent_request_missing_waiting_generations_is_reported(self):
        ledger = PopLedger()
        queue(ledger, "r", at=1.0, n=3)
        ledger.record_status(status("r", finished=1, waiting=1))

        assert len(violations(ledger, Oracle.LOST_GENERATION)) == 1

    def test_a_shortfall_gone_on_a_later_read_was_a_generation_in_the_middle_of_being_given_back(self):
        ledger = PopLedger()
        queue(ledger, "r", at=1.0, n=2)
        ledger.record_status(status("r", finished=1, waiting=0, at=50.0))
        ledger.record_status(status("r", finished=1, waiting=1, at=56.0))

        assert violations(ledger, Oracle.LOST_GENERATION) == []

    def test_a_shortfall_on_every_read_is_reported(self):
        ledger = PopLedger()
        queue(ledger, "r", at=1.0, n=2)
        ledger.record_status(status("r", finished=1, waiting=0, at=50.0))
        ledger.record_status(status("r", finished=1, waiting=0, at=56.0))

        assert len(violations(ledger, Oracle.LOST_GENERATION)) == 1

    def test_a_request_reported_done_and_then_not_done_is_reported(self):
        ledger = PopLedger()
        queue(ledger, "r", at=1.0, n=2)
        ledger.record_status(status("r", finished=1, at=10.0, done=True, final=False))
        ledger.record_status(status("r", finished=1, waiting=1, at=11.0, final=False))

        assert len(violations(ledger, Oracle.DONE_REGRESSED)) == 1

    def test_a_request_done_on_every_read_after_its_first_is_legal(self):
        ledger = PopLedger()
        queue(ledger, "r", at=1.0, n=2)
        ledger.record_status(status("r", finished=1, waiting=1, at=9.0, final=False))
        ledger.record_status(status("r", finished=2, at=10.0, done=True, final=False))
        ledger.record_status(status("r", finished=2, at=11.0, done=True))

        assert violations(ledger) == []

    @pytest.mark.parametrize("fields", [{"finished": 1, "waiting": 2}, {"finished": 1, "waiting": 0, "processing": 2}])
    def test_a_consistent_or_still_processing_request_is_not_reported(self, fields):
        ledger = PopLedger()
        queue(ledger, "r", at=1.0, n=3)
        ledger.record_status(status("r", **fields))

        assert violations(ledger) == []

    def test_a_generation_attributed_to_a_worker_never_handed_the_request_is_reported(self):
        ledger = PopLedger()
        queue(ledger, "r", at=1.0, n=2)
        pop(ledger, "a", sent=2.0, received=2.5, nonce="r")
        ledger.record_status(status("r", finished=2, names=("a", "b")))

        found = violations(ledger, Oracle.ATTRIBUTION)
        assert [violation.evidence["worker"] for violation in found] == ["b"]

    def test_an_underreported_blacklist_skip_is_reported(self):
        ledger = PopLedger()
        ledger.register_worker(worker("a", blacklist=("zebra",)))
        queue(ledger, "r1", at=1.0, prompt="zebra one")
        queue(ledger, "r2", at=1.0, prompt="zebra two")
        pop(ledger, "a", sent=2.0, received=2.5, skipped={"blacklist": 1})

        assert len(violations(ledger, Oracle.SKIPPED_UNDERREPORTED)) == 1

    def test_probe_outcomes_are_held_to_their_expectation(self):
        ledger = PopLedger()
        ledger.record_probe(ProbeRecord(label="ok", sent_at=1.0, status=400, expected="400", detail=""))
        ledger.record_probe(ProbeRecord(label="crash", sent_at=1.0, status=500, expected="400|2xx", detail=""))

        assert [violation.evidence["label"] for violation in violations(ledger, Oracle.PROBE)] == ["crash"]


def test_a_dumped_ledger_is_judged_the_same_when_loaded(tmp_path):
    ledger = PopLedger()
    ledger.register_worker(priority_worker(blacklist=("zebra",)))
    interval = ledger.begin_state("maintenance", "p", 0.5)
    ledger.acknowledge_begin(interval, 0.6)
    ledger.end_state(interval, 1.5)
    queue(ledger, "friend", at=1.0, owner=FRIEND)
    queue(ledger, "stranger", at=0.5, owner=STRANGER, targeted_worker="other")
    first = pop(ledger, "p", sent=2.0, received=2.5, nonce="stranger")
    submit(ledger, first.job_ids[0], "faulted", sent=3.0, received=None, status=None)
    ledger.record_status(status("friend", finished=2))
    path = tmp_path / "ledger.jsonl"

    ledger.dump(path)
    loaded = PopLedger.load(path)

    assert loaded.evaluate(end_time=100.0) == ledger.evaluate(end_time=100.0)
    assert violations(ledger)

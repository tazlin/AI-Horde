# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Run-scoped ledger and oracles for the pop-selection scenario.

The pop-selection workload records everything a requester or worker observes through the API: each request
it queued (with the client-side instants just before sending and just after the response), each pop with
what it handed out, each submit, each cancellation, and each status read. Once the run ends, the oracles
below replay that ledger and decide whether the server's choices were possible under the documented
behaviour of worker job selection (``docs/reference/worker_job_selection.md``).

Every oracle compares the server's observable choices with what the server was allowed to do given only
what the harness can prove from its own timestamps. Server work for an HTTP call happens strictly between
the instant the client sent it and the instant the client received the response, so:

- A request is certainly queued and active during a pop that was sent after its submission response arrived.
- A generation can only have been claimed by a pop that was sent before another pop's response arrived.
- A generation can only have been given back by a faulted submit that was sent, or a timeout that could have
  fired, before a pop's response arrived.

From those facts an oracle derives a lower bound on the work a worker could certainly have been handed, or an
upper bound on the work the server was entitled to hand out, and reports a violation only when an observation
falls outside the bound. The bounds never assume anything about the implementation (no row locks, no read
sizes, no SQL), so a violation is a behaviour a worker or requester could see, not a restated detail.

Lanes isolate the populations: every lane has its own models, served only by that lane's workers, so a
request is visible only to the lane it was queued in. The bounds count only the harness's own work, which is
sound only while no other worker serves a lane's models; ``locustfile_pop_selection.py`` checks that at start.
"""

from __future__ import annotations

import bisect
import json
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import gevent.lock

# A request expires 20 minutes after it is queued unless a claim extends it. The bounds treat a request queued
# longer ago than this, less a margin, as possibly gone rather than reason about expiry.
REQUEST_EXPIRY_SECONDS = 20 * 60
EXPIRY_MARGIN_SECONDS = 60


class Oracle(StrEnum):
    """Names under which violations are reported, one per behaviour."""

    OVERCLAIM = "oracle:pop_overclaim"
    """A request was handed out more often than its ``n`` plus the generations it was legitimately given back."""
    OVERDELIVERED = "oracle:pop_overdelivered"
    """A status read reported more finished generations than the request asked for."""
    TURNED_AWAY = "oracle:pop_turned_away"
    """A worker was given nothing while a request it could serve certainly had a generation left."""
    PRIORITY_BYPASSED = "oracle:pop_priority_bypassed"
    """A worker was given a stranger's request while a request of its owner or priority users certainly waited."""
    ORDER = "oracle:pop_order"
    """A worker was given a newer request of one requester while an older one of equal standing certainly waited."""
    TARGETING = "oracle:pop_targeting"
    """A request naming its workers reached another worker, or a request excluding a worker reached it."""
    BLACKLIST = "oracle:pop_blacklist"
    """A worker was given a prompt containing a word on its blacklist."""
    MAINTENANCE = "oracle:pop_maintenance"
    """A worker in maintenance was given a request of someone other than its owner, or told nothing but 200."""
    TRICK_REPEAT = "oracle:pop_trick_repeat"
    """A worker was handed a request again after receiving a fake job for it."""
    AFTER_CANCEL = "oracle:pop_after_cancel"
    """A request was handed out by a pop sent after its cancellation was acknowledged."""
    CROSS_LANE = "oracle:pop_cross_lane"
    """A worker was handed a request for a model it does not serve."""
    SKIPPED_UNDERREPORTED = "oracle:pop_skipped_underreported"
    """A worker given nothing was told about fewer blacklisted requests than certainly waited."""
    ATTRIBUTION = "oracle:pop_generation_attribution"
    """A finished generation was attributed to a worker that was never handed that request, or too often."""
    LOST_GENERATION = "oracle:pop_lost_generation"
    """A quiescent request reported fewer waiting generations than it asked for minus those finished, on every read."""
    DONE_REGRESSED = "oracle:pop_done_regressed"
    """A request reported done and a later read of it reported not done."""
    PROBE = "oracle:pop_probe"
    """An adversarial input was answered with a server error or with an answer contradicting its validation."""
    SERVER_ERROR = "oracle:pop_server_error"
    """A request of the scenario's ordinary traffic was answered with a server error."""


ORACLE_REQUEST_TYPE = "ORACLE"


def prompt_matches_blacklist(prompt: str, words: Iterable[str]) -> bool:
    """Return whether ``prompt`` contains one of ``words``, compared case-insensitively.

    This is the comparison the worker reference documents (``Worker.can_generate``): each word, lowercased, is a
    substring of the lowercased prompt. Wildcard-looking characters (``%``, ``_``, ``\\``) match only themselves.
    """
    lowered = prompt.lower()
    return any(word.lower() in lowered for word in words)


# --------------------------------------------------------------------------- #
# Ledger records                                                              #
# --------------------------------------------------------------------------- #


class RequestKind(StrEnum):
    """What a queued request is for, which decides which workers of its lane may serve it."""

    SERVABLE = "servable"
    """Any worker of the lane can serve it."""
    FLOOD = "flood"
    """Admitted by the candidate query but refused by every worker of its lane on their own check."""


@dataclass
class TrackedRequest:
    request_id: str
    nonce: str
    lane: str
    gentype: str
    owner: str
    n: int
    prompt: str
    kind: RequestKind
    sent_at: float
    accepted_at: float
    targeted_worker: str | None = None
    excluded_worker: str | None = None
    cancel_sent_at: float | None = None
    cancel_acked_at: float | None = None


@dataclass(frozen=True)
class WorkerProfile:
    """What a worker declares and is, fixed for the run."""

    name: str
    lane: str
    gentype: str
    owner: str
    priority_owners: frozenset[str] = frozenset()
    """The aliases whose requests are priority requests for this worker, its owner always among them."""
    blacklist: tuple[str, ...] = ()
    refused_kinds: frozenset[RequestKind] = frozenset()
    is_priority_worker: bool = False
    fifo: bool = False


@dataclass
class StateInterval:
    """A period a worker spent in maintenance or paused.

    The worker was certainly in the state from the acknowledgement of the request entering it (``start``) until the
    request leaving it was sent (``end``), and possibly in it from the send of the first (``maybe_start``) until the
    acknowledgement of the second (``maybe_end``).
    """

    maybe_start: float
    start: float
    end: float | None = None
    maybe_end: float | None = None

    def covers(self, start: float, end: float) -> bool:
        return self.start <= start and (self.end is None or end <= self.end)

    def overlaps(self, start: float, end: float) -> bool:
        return self.maybe_start < end and (self.maybe_end is None or start < self.maybe_end)


@dataclass
class PopRecord:
    worker: str
    lane: str
    sent_at: float
    received_at: float | None
    amount: int
    status: int | None = None
    rc: str | None = None
    job_ids: tuple[str, ...] = ()
    nonce: str | None = None
    skipped: dict[str, int] = field(default_factory=dict)
    ttl: int | None = None

    @property
    def answered(self) -> bool:
        """Whether the response is authoritative about what was claimed: a server error may have claimed and lost it."""
        return self.received_at is not None and self.status is not None and 0 < self.status < 500

    @property
    def gens(self) -> int:
        return len(self.job_ids)


@dataclass
class SubmitRecord:
    job_id: str
    state: str
    sent_at: float
    received_at: float | None
    status: int | None


@dataclass
class StatusRecord:
    nonce: str
    observed_at: float
    finished: int
    processing: int
    waiting: int
    restarted: int
    done: bool
    faulted: bool
    worker_names: tuple[str, ...] | None = None
    final: bool = False


@dataclass
class ProbeRecord:
    label: str
    sent_at: float
    status: int | None
    expected: str
    detail: str


@dataclass
class ServerErrorRecord:
    label: str
    sent_at: float
    status: int
    detail: str


@dataclass(frozen=True)
class Violation:
    oracle: Oracle
    detail: str
    evidence: dict[str, Any]


# --------------------------------------------------------------------------- #
# Ledger                                                                      #
# --------------------------------------------------------------------------- #


class PopLedger:
    """Every observation of one run, and the oracles that judge them."""

    def __init__(self) -> None:
        self._lock = gevent.lock.RLock()
        self.requests: dict[str, TrackedRequest] = {}
        self.request_by_id: dict[str, str] = {}
        self.workers: dict[str, WorkerProfile] = {}
        self.maintenance: dict[str, list[StateInterval]] = defaultdict(list)
        self.paused: dict[str, list[StateInterval]] = defaultdict(list)
        self.pops: list[PopRecord] = []
        self.submits: dict[str, list[SubmitRecord]] = defaultdict(list)
        self.job_pop: dict[str, PopRecord] = {}
        self.statuses: list[StatusRecord] = []
        self.probes: list[ProbeRecord] = []
        self.server_errors: list[ServerErrorRecord] = []
        self.untracked_handouts = 0

    # -- recording ---------------------------------------------------------- #

    def register_worker(self, profile: WorkerProfile) -> None:
        with self._lock:
            self.workers[profile.name] = profile

    def record_request(self, request: TrackedRequest) -> None:
        with self._lock:
            self.requests[request.nonce] = request
            self.request_by_id[request.request_id] = request.nonce

    def record_cancel_sent(self, nonce: str, sent_at: float) -> None:
        """Record a cancellation as sent, before its response: from then on the request may be gone."""
        with self._lock:
            self.requests[nonce].cancel_sent_at = sent_at

    def record_cancel_acked(self, nonce: str, acked_at: float) -> None:
        with self._lock:
            self.requests[nonce].cancel_acked_at = acked_at

    def begin_state(self, kind: str, worker: str, sent_at: float) -> StateInterval:
        """Record that a request putting ``worker`` into maintenance or pause was sent at ``sent_at``.

        Until :meth:`acknowledge_begin` the worker is possibly but not certainly in the state.
        """
        interval = StateInterval(maybe_start=sent_at, start=float("inf"))
        with self._lock:
            self._intervals(kind)[worker].append(interval)
        return interval

    def acknowledge_begin(self, interval: StateInterval, acked_at: float) -> None:
        with self._lock:
            interval.start = acked_at

    def end_state(self, interval: StateInterval, sent_at: float) -> None:
        """Record that a request taking the worker out of the state was sent at ``sent_at``.

        Until :meth:`acknowledge_end` the worker may still be in the state.
        """
        with self._lock:
            interval.end = sent_at
            interval.maybe_end = float("inf")

    def acknowledge_end(self, interval: StateInterval, acked_at: float) -> None:
        with self._lock:
            interval.maybe_end = acked_at

    def _intervals(self, kind: str) -> dict[str, list[StateInterval]]:
        if kind == "maintenance":
            return self.maintenance
        if kind == "paused":
            return self.paused
        raise ValueError(kind)

    def record_pop_sent(self, pop: PopRecord) -> None:
        """Record a pop as sent, before its response: a pop never answered may still have claimed work."""
        with self._lock:
            self.pops.append(pop)

    def complete_pop(self, pop: PopRecord) -> None:
        """Index a pop once its response has been filled in."""
        with self._lock:
            for job_id in pop.job_ids:
                self.job_pop[job_id] = pop
            if pop.job_ids and (pop.nonce is None or pop.nonce not in self.requests):
                self.untracked_handouts += 1

    def record_submit_sent(self, submit: SubmitRecord) -> None:
        """Record a submit as sent: a faulted submit may give its generation back even if its response is lost."""
        with self._lock:
            self.submits[submit.job_id].append(submit)

    def record_status(self, status: StatusRecord) -> None:
        with self._lock:
            self.statuses.append(status)

    def record_probe(self, probe: ProbeRecord) -> None:
        with self._lock:
            self.probes.append(probe)

    def record_server_error(self, record: ServerErrorRecord) -> None:
        with self._lock:
            self.server_errors.append(record)

    # -- persistence ---------------------------------------------------------- #

    def dump(self, path: str | Path) -> None:
        """Write every observation to ``path`` as JSON lines, so a run can be judged again offline."""
        with self._lock, Path(path).open("w", encoding="utf-8") as handle:

            def emit(kind: str, record: Any, **extra: Any) -> None:
                handle.write(json.dumps({**asdict(record), **extra, "_record": kind}, default=_jsonable) + "\n")

            for profile in self.workers.values():
                emit("worker", profile)
            for request in self.requests.values():
                emit("request", request)
            for kind, intervals in (("maintenance", self.maintenance), ("paused", self.paused)):
                for worker, worker_intervals in intervals.items():
                    for interval in worker_intervals:
                        emit("interval", interval, state=kind, worker=worker)
            for pop in self.pops:
                emit("pop", pop)
            for submits in self.submits.values():
                for submit in submits:
                    emit("submit", submit)
            for status in self.statuses:
                emit("status", status)
            for probe in self.probes:
                emit("probe", probe)
            for error in self.server_errors:
                emit("server_error", error)

    @classmethod
    def load(cls, path: str | Path) -> PopLedger:
        """Rebuild a ledger written by :meth:`dump`."""
        loaded = cls()
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                kind = row.pop("_record")
                if kind == "worker":
                    row["priority_owners"] = frozenset(row["priority_owners"])
                    row["blacklist"] = tuple(row["blacklist"])
                    row["refused_kinds"] = frozenset(RequestKind(value) for value in row["refused_kinds"])
                    loaded.register_worker(WorkerProfile(**row))
                elif kind == "request":
                    row["kind"] = RequestKind(row["kind"])
                    loaded.record_request(TrackedRequest(**row))
                elif kind == "interval":
                    state, worker = row.pop("state"), row.pop("worker")
                    loaded._intervals(state)[worker].append(StateInterval(**{key: _float(value) for key, value in row.items()}))
                elif kind == "pop":
                    row.pop("fake", None)
                    row["job_ids"] = tuple(row["job_ids"])
                    pop = PopRecord(**row)
                    loaded.record_pop_sent(pop)
                    loaded.complete_pop(pop)
                elif kind == "submit":
                    loaded.record_submit_sent(SubmitRecord(**row))
                elif kind == "status":
                    if row["worker_names"] is not None:
                        row["worker_names"] = tuple(row["worker_names"])
                    loaded.record_status(StatusRecord(**row))
                elif kind == "probe":
                    loaded.record_probe(ProbeRecord(**row))
                elif kind == "server_error":
                    loaded.record_server_error(ServerErrorRecord(**row))
        return loaded

    def nonce_for_prompt(self, prompt: str | None) -> str | None:
        """Return the tracked nonce a handed-out prompt carries, if any."""
        if not prompt:
            return None
        for token in prompt.replace(",", " ").split():
            if token.startswith(NONCE_PREFIX) and token in self.requests:
                return token
        for token in prompt.replace(",", " ").split():
            if token.startswith(NONCE_PREFIX):
                return token
        return None

    def worker_state_at(self, kind: str, worker: str, start: float, end: float) -> bool | None:
        """Return True if ``worker`` was certainly in ``kind`` throughout ``[start, end]``, False if certainly not
        at any point of it, and None when a transition may have happened during it."""
        intervals = self._intervals(kind).get(worker, [])
        if any(interval.covers(start, end) for interval in intervals):
            return True
        if any(interval.overlaps(start, end) for interval in intervals):
            return None
        return False

    # -- evaluation ----------------------------------------------------------- #

    def evaluate(self, end_time: float | None = None) -> tuple[list[Violation], dict[str, int]]:
        """Replay the ledger and return every violation together with how often each oracle had a case to judge."""
        end_time = end_time if end_time is not None else time.time()
        with self._lock:
            evaluator = _Evaluator(self, end_time)
            return evaluator.run()


NONCE_PREFIX = "popq"


def lost_generation_suspects(ledger: PopLedger) -> list[str]:
    """Requests whose latest final read shows fewer waiting generations than they lack, to be read again."""
    latest: dict[str, StatusRecord] = {}
    for status in ledger.statuses:
        if status.final and (status.nonce not in latest or status.observed_at > latest[status.nonce].observed_at):
            latest[status.nonce] = status
    suspects = []
    for nonce, status in latest.items():
        request = ledger.requests.get(nonce)
        if request is None or request.cancel_sent_at is not None or status.faulted or status.processing != 0:
            continue
        if status.waiting < max(request.n - status.finished, 0):
            suspects.append(nonce)
    return suspects


class _Evaluator:
    def __init__(self, ledger: PopLedger, end_time: float) -> None:
        self.ledger = ledger
        self.end_time = end_time
        self.violations: list[Violation] = []
        self.coverage: dict[str, int] = defaultdict(int)
        # A paused worker is handed a fake job for anything but its owner's requests, and the fake claims nothing.
        # Whether a pop's job was fake is decided here, once every request is known: a pop can be answered with a
        # request before its requester has the response that queued it.
        self.fake: set[int] = set()
        self.maybe_fake: set[int] = set()
        for pop in ledger.pops:
            request = ledger.requests.get(pop.nonce) if pop.nonce else None
            profile = ledger.workers.get(pop.worker)
            if not pop.job_ids or pop.received_at is None or request is None or profile is None or request.owner == profile.owner:
                continue
            paused = ledger.worker_state_at("paused", pop.worker, pop.sent_at, pop.received_at)
            if paused:
                self.fake.add(id(pop))
            elif paused is None:
                self.maybe_fake.add(id(pop))
        # Certainly real handouts per request, ordered by the pop's response instant: what the server must account for.
        self.real_handouts: dict[str, list[PopRecord]] = defaultdict(list)
        # Handouts that may have been real: what may have claimed a request's generations.
        self.claim_handouts: dict[str, list[PopRecord]] = defaultdict(list)
        # Every pop that carried any handout (real or fake) of a request to a worker.
        self.any_handouts: dict[tuple[str, str], list[PopRecord]] = defaultdict(list)
        self.first_fake_at: dict[str, float] = {}
        for pop in ledger.pops:
            if id(pop) in self.fake and pop.received_at is not None:
                self.first_fake_at[pop.worker] = min(self.first_fake_at.get(pop.worker, pop.received_at), pop.received_at)
            if pop.nonce is None or pop.nonce not in ledger.requests or not pop.job_ids:
                continue
            self.any_handouts[(pop.worker, pop.nonce)].append(pop)
            if id(pop) not in self.fake:
                self.claim_handouts[pop.nonce].append(pop)
                if id(pop) not in self.maybe_fake:
                    self.real_handouts[pop.nonce].append(pop)
        for handouts in self.real_handouts.values():
            handouts.sort(key=lambda pop: pop.received_at or 0.0)
        # Per request, the real handouts' send instants with running totals, and the send instant of the pop after
        # which every generation the request asked for may have been claimed.
        self.claim_sends: dict[str, list[float]] = {}
        self.claim_totals: dict[str, list[int]] = {}
        self.full_at: dict[str, float] = {}
        for nonce, request in ledger.requests.items():
            sends = sorted((pop.sent_at, pop.gens) for pop in self.claim_handouts.get(nonce, []))
            running = 0
            totals = []
            full_at = float("inf")
            for sent_at, gens in sends:
                running += gens
                totals.append(running)
                if running >= request.n and full_at == float("inf"):
                    full_at = sent_at
            self.claim_sends[nonce] = [sent_at for sent_at, _ in sends]
            self.claim_totals[nonce] = totals
            self.full_at[nonce] = full_at
        self.lane_requests: dict[str, list[TrackedRequest]] = defaultdict(list)
        for request in ledger.requests.values():
            self.lane_requests[request.lane].append(request)
        for requests in self.lane_requests.values():
            requests.sort(key=lambda request: request.accepted_at)
        self.lane_accepts = {lane: [request.accepted_at for request in requests] for lane, requests in self.lane_requests.items()}
        self.unknown_sends: dict[str, list[float]] = {}
        self.unknown_amounts: dict[str, list[int]] = {}
        unknown_by_lane: dict[str, list[PopRecord]] = defaultdict(list)
        for pop in ledger.pops:
            if not pop.answered:
                unknown_by_lane[pop.lane].append(pop)
        for lane, pops in unknown_by_lane.items():
            ordered = sorted(pops, key=lambda pop: pop.sent_at)
            self.unknown_sends[lane] = [pop.sent_at for pop in ordered]
            amounts, running = [], 0
            for pop in ordered:
                running += max(pop.amount, 1)
                amounts.append(running)
            self.unknown_amounts[lane] = amounts

    def flag(self, oracle: Oracle, detail: str, **evidence: Any) -> None:
        self.violations.append(Violation(oracle=oracle, detail=detail, evidence=evidence))

    def run(self) -> tuple[list[Violation], dict[str, int]]:
        self.check_overclaim()
        self.check_status_reads()
        for pop in self.ledger.pops:
            self.check_pop(pop)
        self.check_probes()
        self.check_server_errors()
        return self.violations, dict(self.coverage)

    # -- bounds --------------------------------------------------------------- #

    def return_instant(self, job_id: str) -> float | None:
        """Return the earliest instant at which the server may have given ``job_id``'s generation back, if ever.

        A generation is given back once at most: by a faulted submit for a job that had not been delivered, or by a
        timeout once the job outlived its ttl without being delivered.
        """
        pop = self.ledger.job_pop.get(job_id)
        submits = sorted(self.ledger.submits.get(job_id, []), key=lambda submit: submit.sent_at)
        delivered_at = min(
            (submit.received_at for submit in submits if submit.state == "ok" and submit.status == 200 and submit.received_at),
            default=None,
        )
        candidates: list[float] = []
        for submit in submits:
            if submit.state == "faulted" and (delivered_at is None or submit.sent_at < delivered_at):
                candidates.append(submit.sent_at)
                break
        if pop is not None and pop.ttl:
            # The job's ttl runs from its claim, which happened after the pop was sent.
            expires_at = pop.sent_at + pop.ttl
            if delivered_at is None or delivered_at > expires_at:
                candidates.append(expires_at)
        return min(candidates) if candidates else None

    def queued_during(self, request: TrackedRequest, start: float, end: float) -> bool:
        """Whether ``request`` was certainly queued, active and uncancelled throughout ``[start, end]``."""
        if request.accepted_at >= start:
            return False
        if request.cancel_sent_at is not None and request.cancel_sent_at < end:
            return False
        if end - request.accepted_at > REQUEST_EXPIRY_SECONDS - EXPIRY_MARGIN_SECONDS:
            return False
        return True

    def left_lower_bound(self, request: TrackedRequest, end: float) -> int:
        """A lower bound on the generations ``request`` had left at every instant before ``end``.

        Every real handout from a pop sent before ``end`` may already have been claimed. Generations given back only
        add to what is left, so they are ignored.
        """
        index = bisect.bisect_left(self.claim_sends.get(request.nonce, []), end)
        claimed = self.claim_totals[request.nonce][index - 1] if index else 0
        return max(request.n - claimed, 0)

    def unknown_claims(self, lane: str, end: float) -> int:
        """Generations unanswered pops of ``lane`` sent before ``end`` may have claimed, at most their amount each."""
        index = bisect.bisect_left(self.unknown_sends.get(lane, []), end)
        return self.unknown_amounts[lane][index - 1] if index else 0

    def unknown_pop_count(self, lane: str, end: float) -> int:
        """How many pops of ``lane`` sent before ``end`` went unanswered."""
        return bisect.bisect_left(self.unknown_sends.get(lane, []), end)

    def candidates(self, lane: str, start: float, end: float) -> Iterable[TrackedRequest]:
        """Requests of ``lane`` queued before ``start`` that may still have had a generation left at ``end``."""
        requests = self.lane_requests.get(lane, [])
        for request in requests[: bisect.bisect_left(self.lane_accepts.get(lane, []), start)]:
            if self.full_at[request.nonce] >= end and self.queued_during(request, start, end):
                yield request

    def eligible(self, worker: WorkerProfile, request: TrackedRequest, pop: PopRecord, in_maintenance: bool) -> bool:
        """Whether ``worker`` may serve ``request`` under the documented rules, judged at ``pop``."""
        if request.lane != worker.lane or request.gentype != worker.gentype:
            return False
        if request.kind in worker.refused_kinds:
            return False
        if request.targeted_worker is not None and request.targeted_worker != worker.name:
            return False
        if request.excluded_worker == worker.name:
            return False
        if worker.blacklist and prompt_matches_blacklist(request.prompt, worker.blacklist):
            return False
        if in_maintenance and request.owner != worker.owner:
            return False
        # A worker once handed a fake job for a request is never handed that request again.
        if any(
            (id(earlier) in self.fake or id(earlier) in self.maybe_fake) and earlier.sent_at < pop.sent_at
            for earlier in self.any_handouts.get((worker.name, request.nonce), [])
        ):
            return False
        return True

    def certainly_available(self, worker: WorkerProfile, pop: PopRecord, in_maintenance: bool, predicate=None) -> tuple[int, list[str]]:
        """A lower bound on the generations ``worker`` could certainly have been handed during ``pop``.

        Returns the bound and the nonces that contribute to it.
        """
        assert pop.received_at is not None
        total = 0
        contributing: list[str] = []
        for request in self.candidates(worker.lane, pop.sent_at, pop.received_at):
            if predicate is not None and not predicate(request):
                continue
            if not self.eligible(worker, request, pop, in_maintenance):
                continue
            left = self.left_lower_bound(request, pop.received_at)
            if left > 0:
                total += left
                contributing.append(request.nonce)
        total -= self.unknown_claims(worker.lane, pop.received_at)
        return total, contributing

    # -- oracles -------------------------------------------------------------- #

    def check_overclaim(self) -> None:
        for nonce, handouts in self.real_handouts.items():
            request = self.ledger.requests[nonce]
            jobs: list[str] = []
            delivered = 0
            for pop in handouts:
                assert pop.received_at is not None
                delivered += pop.gens
                jobs.extend(pop.job_ids)
                self.coverage["overclaim.handouts_checked"] += 1
                # Returns that may have happened before this pop's response: their triggering submit or timeout came first.
                returned = sum(1 for job_id in jobs if (instant := self.return_instant(job_id)) is not None and instant < pop.received_at)
                if delivered > request.n + returned:
                    self.flag(
                        Oracle.OVERCLAIM,
                        f"request {request.request_id} ({request.lane}) asked for {request.n} generations; by the response to "
                        f"pop of {pop.worker} it had handed out {delivered}, with at most {returned} given back",
                        request_id=request.request_id,
                        lane=request.lane,
                        n=request.n,
                        handed_out=delivered,
                        given_back_at_most=returned,
                        handouts=[[p.worker, p.sent_at, p.received_at, list(p.job_ids)] for p in handouts],
                        submits={
                            job_id: [[s.state, s.sent_at, s.received_at, s.status] for s in self.ledger.submits.get(job_id, [])]
                            for job_id in jobs
                        },
                    )
                    break

    def check_status_reads(self) -> None:
        by_request: dict[str, list[StatusRecord]] = defaultdict(list)
        for status in self.ledger.statuses:
            by_request[status.nonce].append(status)
        for nonce, reads in by_request.items():
            request = self.ledger.requests.get(nonce)
            if request is None:
                continue
            reads.sort(key=lambda status: status.observed_at)
            self.check_done_terminal(request, reads)
            finals = [status for status in reads if status.final]
            if finals:
                self.check_quiescent(request, finals)
        for status in self.ledger.statuses:
            request = self.ledger.requests.get(status.nonce)
            if request is None:
                continue
            self.coverage["status.reads_checked"] += 1
            if status.finished > request.n:
                self.flag(
                    Oracle.OVERDELIVERED,
                    f"request {request.request_id} ({request.lane}) asked for {request.n} generations "
                    f"and reported {status.finished} finished",
                    request_id=request.request_id,
                    lane=request.lane,
                    n=request.n,
                    finished=status.finished,
                    final=status.final,
                    observed_at=status.observed_at,
                )
            if status.worker_names is not None:
                self.check_attribution(request, status)

    def check_attribution(self, request: TrackedRequest, status: StatusRecord) -> None:
        handed: dict[str, int] = defaultdict(int)
        for pop in self.claim_handouts.get(request.nonce, []):
            handed[pop.worker] += pop.gens
        finished_by: dict[str, int] = defaultdict(int)
        for name in status.worker_names or ():
            finished_by[name] += 1
        self.coverage["attribution.requests_checked"] += 1
        for name, count in finished_by.items():
            if count > handed.get(name, 0):
                self.flag(
                    Oracle.ATTRIBUTION,
                    f"request {request.request_id} ({request.lane}) lists {count} generations by {name}, "
                    f"which was handed {handed.get(name, 0)} real generations of it",
                    request_id=request.request_id,
                    lane=request.lane,
                    worker=name,
                    listed=count,
                    handed=handed.get(name, 0),
                    handed_by_worker=dict(handed),
                )

    def check_done_terminal(self, request: TrackedRequest, reads: list[StatusRecord]) -> None:
        """Once a request reports done, it has everything it will get: no later read may report it not done."""
        if request.cancel_sent_at is not None:
            return
        done_at = next((status for status in reads if status.done), None)
        if done_at is None:
            return
        if any(status.observed_at > done_at.observed_at for status in reads):
            self.coverage["done.requests_read_again_after_done"] += 1
        regressed = next((status for status in reads if status.observed_at > done_at.observed_at and not status.done), None)
        if regressed is not None:
            self.flag(
                Oracle.DONE_REGRESSED,
                f"request {request.request_id} ({request.lane}) reported done with {done_at.finished} of {request.n} finished, "
                f"then not done with {regressed.finished} finished, {regressed.processing} processing, {regressed.waiting} waiting",
                request_id=request.request_id,
                lane=request.lane,
                n=request.n,
                done_read=[done_at.observed_at, done_at.finished, done_at.processing, done_at.waiting, done_at.restarted],
                later_read=[regressed.observed_at, regressed.finished, regressed.processing, regressed.waiting, regressed.restarted],
            )

    def check_quiescent(self, request: TrackedRequest, finals: list[StatusRecord]) -> None:
        """At the end of the run, nothing processing: what is waiting must be what was asked for less what finished.

        The server's own threads still act after the run (a timed-out job is faulted and given back in two steps), so a
        request is reported only if every final read of it shows the shortfall; the scenario reads a suspect again.
        """
        status = finals[-1]
        if request.cancel_sent_at is not None or any(read.faulted or read.processing != 0 for read in finals):
            return
        self.coverage["quiescent.requests_checked"] += 1
        expected_waiting = max(request.n - status.finished, 0)
        if all(read.waiting < max(request.n - read.finished, 0) for read in finals) and status.waiting < expected_waiting:
            self.flag(
                Oracle.LOST_GENERATION,
                f"request {request.request_id} ({request.lane}) asked for {request.n}, finished {status.finished}, has nothing "
                f"processing, and reports only {status.waiting} waiting",
                request_id=request.request_id,
                lane=request.lane,
                n=request.n,
                finished=status.finished,
                waiting=status.waiting,
                restarted=status.restarted,
                done=status.done,
                final_reads=[[read.observed_at, read.finished, read.processing, read.waiting, read.restarted] for read in finals],
            )

    def check_pop(self, pop: PopRecord) -> None:
        worker = self.ledger.workers.get(pop.worker)
        if worker is None or not pop.answered:
            return
        assert pop.received_at is not None
        maintenance = self.ledger.worker_state_at("maintenance", worker.name, pop.sent_at, pop.received_at)
        paused = self.ledger.worker_state_at("paused", worker.name, pop.sent_at, pop.received_at)
        request = self.ledger.requests.get(pop.nonce) if pop.nonce else None
        if pop.job_ids:
            if request is None:
                return
            self.check_handout(worker, pop, request, maintenance, paused)
            return
        self.check_empty(worker, pop, maintenance, paused)

    def check_handout(self, worker: WorkerProfile, pop: PopRecord, request: TrackedRequest, maintenance, paused) -> None:
        assert pop.received_at is not None
        self.coverage["handout.checked"] += 1
        base = {"worker": worker.name, "lane": worker.lane, "request_id": request.request_id, "pop_sent_at": pop.sent_at}
        if request.lane != worker.lane:
            self.flag(Oracle.CROSS_LANE, f"{worker.name} ({worker.lane}) was handed {request.request_id} of lane {request.lane}", **base)
            return
        if request.targeted_worker is not None:
            self.coverage["targeting.targeted_handouts"] += 1
            if request.targeted_worker != worker.name:
                self.flag(Oracle.TARGETING, f"{request.request_id} names {request.targeted_worker} and reached {worker.name}", **base)
        if request.excluded_worker is not None:
            self.coverage["targeting.excluding_handouts"] += 1
            if request.excluded_worker == worker.name:
                self.flag(Oracle.TARGETING, f"{request.request_id} excludes {worker.name} and reached it", **base)
        if worker.blacklist:
            self.coverage["blacklist.handouts"] += 1
            if prompt_matches_blacklist(request.prompt, worker.blacklist):
                self.flag(Oracle.BLACKLIST, f"{worker.name} blacklists {list(worker.blacklist)} and was handed {request.prompt!r}", **base)
        if maintenance:
            self.coverage["maintenance.handouts"] += 1
            if request.owner != worker.owner:
                self.flag(
                    Oracle.MAINTENANCE,
                    f"{worker.name} was in maintenance and was handed {request.request_id} of {request.owner}, "
                    f"not its owner {worker.owner}",
                    **base,
                )
        if request.cancel_acked_at is not None and pop.sent_at > request.cancel_acked_at:
            self.flag(
                Oracle.AFTER_CANCEL,
                f"{request.request_id} was cancelled at {request.cancel_acked_at:.3f} and handed to {worker.name} "
                f"by a pop sent at {pop.sent_at:.3f}",
                cancel_acked_at=request.cancel_acked_at,
                **base,
            )
        if request.cancel_acked_at is not None:
            self.coverage["cancel.handouts_of_cancelled"] += 1
        earlier_fakes = [
            p
            for p in self.any_handouts.get((worker.name, request.nonce), [])
            if id(p) in self.fake and p.received_at and p.received_at < pop.sent_at
        ]
        if earlier_fakes:
            self.flag(
                Oracle.TRICK_REPEAT,
                f"{worker.name} received a fake job for {request.request_id} at {earlier_fakes[0].received_at:.3f} and was handed it again",
                **base,
            )
        if paused is False and self.first_fake_at.get(worker.name, float("inf")) < pop.sent_at:
            self.coverage["paused.handouts_after_unpause"] += 1
        if paused:
            self.coverage["paused.fake_handouts" if id(pop) in self.fake else "paused.real_handouts"] += 1
        if worker.is_priority_worker:
            self.check_priority_choice(worker, pop, request, maintenance)
        if worker.fifo:
            self.check_order(worker, pop, request)

    def check_priority_choice(self, worker: WorkerProfile, pop: PopRecord, request: TrackedRequest, maintenance) -> None:
        if maintenance is not False:
            return
        bound, contributing = self.certainly_available(
            worker,
            pop,
            in_maintenance=False,
            predicate=lambda candidate: candidate.owner in worker.priority_owners,
        )
        flood_depth = sum(
            1
            for candidate in self.candidates(worker.lane, pop.sent_at, pop.received_at or pop.sent_at)
            if candidate.kind is RequestKind.FLOOD and candidate.owner in worker.priority_owners
        )
        if request.owner in worker.priority_owners:
            self.coverage["priority.priority_served"] += 1
            if flood_depth >= 20:
                self.coverage["priority.served_behind_first_read"] += 1
            if flood_depth >= 220:
                self.coverage["priority.served_behind_continued_read"] += 1
            return
        if bound >= 1:
            self.coverage["priority.stranger_served_with_priority_waiting"] += 1
            self.flag(
                Oracle.PRIORITY_BYPASSED,
                f"{worker.name} was handed {request.request_id} of stranger {request.owner} while at least {bound} generations of "
                f"its priority users certainly waited behind {flood_depth} unservable priority requests",
                worker=worker.name,
                lane=worker.lane,
                request_id=request.request_id,
                waiting_priority=[self.ledger.requests[nonce].request_id for nonce in contributing][:10],
                flood_depth=flood_depth,
                pop_sent_at=pop.sent_at,
                pop_received_at=pop.received_at,
            )
        else:
            self.coverage["priority.stranger_served_with_no_priority_waiting"] += 1

    def check_order(self, worker: WorkerProfile, pop: PopRecord, request: TrackedRequest) -> None:
        """Requests of one requester, queued one after another, are served oldest first.

        A requester's balance never rises in this lane, so each later request starts at the same or a lower
        queue priority, and every waiting request rises together; the older request is never behind.
        """
        assert pop.received_at is not None
        older = [
            candidate
            for candidate in self.candidates(request.lane, pop.sent_at, pop.received_at)
            if candidate.owner == request.owner
            and candidate.accepted_at < request.sent_at
            and candidate.nonce != request.nonce
            and self.eligible(worker, candidate, pop, in_maintenance=False)
            and self.left_lower_bound(candidate, pop.received_at) >= 1
        ]
        self.coverage["order.handouts_checked"] += 1
        if any(
            candidate.owner == request.owner and candidate.nonce != request.nonce
            for candidate in self.candidates(request.lane, pop.sent_at, pop.received_at)
        ):
            self.coverage["order.handouts_with_queue_behind"] += 1
        if older:
            oldest = min(older, key=lambda candidate: candidate.accepted_at)
            self.flag(
                Oracle.ORDER,
                f"{worker.name} was handed {request.request_id} (sent {request.sent_at:.3f}) while {oldest.request_id} "
                f"(queued {oldest.accepted_at:.3f}) certainly waited",
                worker=worker.name,
                lane=worker.lane,
                request_id=request.request_id,
                older_waiting=[candidate.request_id for candidate in older][:10],
                pop_sent_at=pop.sent_at,
            )

    def check_empty(self, worker: WorkerProfile, pop: PopRecord, maintenance, paused) -> None:
        assert pop.received_at is not None
        maintenance_refusal = pop.status == 403 and pop.rc == "WorkerMaintenance"
        if pop.status != 200 and not maintenance_refusal:
            return
        if maintenance and pop.status == 200:
            self.coverage["maintenance.empty_pops"] += 1
            self.flag(
                Oracle.MAINTENANCE,
                f"{worker.name} was in maintenance, received nothing, and was answered 200 rather than WorkerMaintenance",
                worker=worker.name,
                lane=worker.lane,
                pop_sent_at=pop.sent_at,
            )
        if maintenance_refusal and maintenance is False:
            self.flag(
                Oracle.MAINTENANCE,
                f"{worker.name} was not in maintenance and was refused with WorkerMaintenance",
                worker=worker.name,
                lane=worker.lane,
                pop_sent_at=pop.sent_at,
            )
        if paused is not False or maintenance is None:
            return
        self.coverage["turned_away.empty_pops_checked"] += 1
        bound, contributing = self.certainly_available(worker, pop, in_maintenance=bool(maintenance))
        if bound >= 1:
            self.flag(
                Oracle.TURNED_AWAY,
                f"{worker.name} ({worker.lane}) received nothing while at least {bound} generations it could serve certainly waited",
                worker=worker.name,
                lane=worker.lane,
                status=pop.status,
                rc=pop.rc,
                skipped=pop.skipped,
                waiting=[
                    {"request_id": self.ledger.requests[nonce].request_id, "prompt": self.ledger.requests[nonce].prompt[:80]}
                    for nonce in contributing[:10]
                ],
                pop_sent_at=pop.sent_at,
                pop_received_at=pop.received_at,
            )
        if worker.blacklist and pop.status == 200:
            dirty = [
                request
                for request in self.candidates(worker.lane, pop.sent_at, pop.received_at)
                if prompt_matches_blacklist(request.prompt, worker.blacklist) and self.left_lower_bound(request, pop.received_at) >= 1
            ]
            # Unanswered pops may each have emptied one request.
            certain = len(dirty) - self.unknown_pop_count(worker.lane, pop.received_at)
            if certain >= 1:
                self.coverage["skipped.blacklist_reports_checked"] += 1
                reported = int(pop.skipped.get("blacklist", 0) or 0)
                if reported < certain:
                    self.flag(
                        Oracle.SKIPPED_UNDERREPORTED,
                        f"{worker.name} received nothing and was told of {reported} blacklisted requests while {certain} certainly waited",
                        worker=worker.name,
                        lane=worker.lane,
                        skipped=pop.skipped,
                        certain=certain,
                        pop_sent_at=pop.sent_at,
                    )

    def check_server_errors(self) -> None:
        for error in self.ledger.server_errors:
            self.flag(
                Oracle.SERVER_ERROR,
                f"{error.label} was answered with HTTP {error.status}",
                label=error.label,
                status=error.status,
                sent_at=error.sent_at,
                response=error.detail,
            )

    def check_probes(self) -> None:
        for probe in self.ledger.probes:
            self.coverage["probe.sent"] += 1
            outcome = _probe_outcome(probe.status)
            if outcome not in probe.expected.split("|"):
                self.flag(
                    Oracle.PROBE,
                    f"probe {probe.label} expected {probe.expected} and got HTTP {probe.status}: {probe.detail}",
                    label=probe.label,
                    status=probe.status,
                    expected=probe.expected,
                    response=probe.detail,
                )


def _jsonable(value: Any) -> Any:
    if isinstance(value, (set, frozenset, tuple)):
        return sorted(value) if isinstance(value, (set, frozenset)) else list(value)
    if isinstance(value, float) and value == float("inf"):
        return "inf"
    return str(value)


def _float(value: Any) -> Any:
    return float("inf") if value == "inf" else value


def _probe_outcome(status: int | None) -> str:
    if status is None:
        return "transport"
    if status >= 500:
        return "5xx"
    if 200 <= status < 300:
        return "2xx"
    return str(status)


# --------------------------------------------------------------------------- #
# Reporting                                                                   #
# --------------------------------------------------------------------------- #


class EvidenceWriter:
    """Writes violations and the run summary to a JSONL file, and reports violations to Locust's statistics."""

    def __init__(self) -> None:
        self._path: Path | None = None
        self._lock = gevent.lock.RLock()

    def configure(self, path: str | None) -> None:
        with self._lock:
            self._path = Path(path) if path else None
            if self._path is not None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text("", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        with self._lock:
            if self._path is None:
                return
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")

    def report(self, environment, violations: list[Violation], coverage: dict[str, int], summary: dict[str, Any]) -> None:
        for violation in violations:
            if environment is not None:
                environment.events.request.fire(
                    request_type=ORACLE_REQUEST_TYPE,
                    name=str(violation.oracle),
                    response_time=0.0,
                    response_length=0,
                    exception=PopOracleViolation(f"{violation.oracle}: {violation.detail}"),
                    context={},
                )
            self.write({"oracle": str(violation.oracle), "detail": violation.detail, **violation.evidence})
        self.write({"record": "run_summary", "violations": len(violations), "coverage": coverage, **summary})


class PopOracleViolation(Exception):
    """The exception payload of an oracle failure event."""


ledger = PopLedger()
evidence_writer = EvidenceWriter()

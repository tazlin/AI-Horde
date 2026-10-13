# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Requesters, workers and probes for the pop-selection scenario.

The scenario drives the image and text pop endpoints the way production does (many workers popping one queue,
requesters queuing and cancelling, workers faulting, abandoning and toggling maintenance) and records every
observation in :mod:`locustsuite.pop_ledger`, whose oracles decide after the run whether each worker received
what the documented selection rules allowed.

Lanes keep populations apart. Each lane has a model only its own workers serve, and each user is assigned a
slot in one lane by ``locustfile_pop_selection.py`` before the run starts:

- ``swarm``: many workers with different batch sizes, two of them sharing one identity, popping one queue of
  multi-generation requests. Requesters target and exclude workers, disable batching, and cancel; workers fault
  and abandon jobs. Judged for over-claiming, workers turned away, targeting and cancellation.
- ``prio``: workers with priority users, one general worker, priority flooders queuing hundreds of requests every
  lane worker refuses on its own check (so they occupy candidate places), priority requesters with servable work
  and no kudos, and rich strangers. Judged for priority being honoured however deep the unservable queue is.
- ``fifo``: one requester, one worker, single-generation requests. Judged for oldest-first order.
- ``maint``: a worker toggling maintenance, with requests from its owner, a priority user and a stranger.
- ``paused``: a worker paused by a moderator for a while (it receives fake jobs) and then resumed.
- ``adversary``: workers faulting jobs twice, concurrently, or after delivering them.
- ``blacklist`` / ``unicode`` (image only): workers with word blacklists including LIKE wildcards and words
  whose case folding differs between Python and the database.
- ``probe``: malformed and extreme pop inputs.
"""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import count
from typing import Any

import gevent
import gevent.lock
from locust import HttpUser, between, task
from locust.exception import StopUser

from ..pop_ledger import (
    NONCE_PREFIX,
    PopRecord,
    ProbeRecord,
    RequestKind,
    ServerErrorRecord,
    StatusRecord,
    SubmitRecord,
    TrackedRequest,
    WorkerProfile,
    ledger,
)

CLIENT_AGENT = "aihorde_pop_selection_stress:1.0.0:(discord)stress_tester"
TEXT_BRIDGE = "KoboldAI Client:1.19.2-stress:https://github.com/koboldai/koboldai-client"
IMAGE_BRIDGE = "AI Horde Worker reGen:18.0.0-stress:https://github.com/Haidra-Org/horde-worker-reGen"

# A worker dropping more than 20 jobs an hour is put into maintenance by the server, which the maintenance oracle
# would rightly treat as unexplained. Each worker identity therefore drops at most this many.
DROP_BUDGET = 15

# Steps far above what a worker with ``limit_max_steps`` accepts, but valid for the API: such a request passes the
# candidate query and is refused by the worker's own check, so it occupies a candidate place.
FLOOD_IMAGE_STEPS = 100
SERVABLE_IMAGE_STEPS = 8


def _headers(api_key: str) -> dict[str, str]:
    return {"apikey": api_key, "Client-Agent": CLIENT_AGENT}


def _json(resp) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


def _note_server_error(resp, label: str, sent_at: float) -> None:
    """Record a server error answering the scenario's ordinary traffic; the oracles count it as a violation."""
    if resp.status_code and resp.status_code >= 500:
        ledger.record_server_error(ServerErrorRecord(label=label, sent_at=sent_at, status=resp.status_code, detail=resp.text[:300]))


def _use_assigned_host(user: HttpUser) -> None:
    """Point a user at one of the scenario's hosts, round robin, so several server instances share the load."""
    host = scenario.next_host()
    if host:
        user.client.base_url = host


# --------------------------------------------------------------------------- #
# Scenario configuration, filled by the entrypoint before users spawn          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Account:
    api_key: str
    alias: str
    user_id: str
    trusted: bool


@dataclass(frozen=True)
class Lane:
    name: str
    gentype: str
    model: str
    void_model: str
    """A model nobody requests, declared on a worker's first pop so that pop creates the worker and nothing else."""


@dataclass
class WorkerSlot:
    profile: WorkerProfile
    account: Account
    lane: Lane
    amounts: tuple[int, ...] = (1,)
    pop_interval: tuple[float, float] = (0.2, 1.0)
    gen_time: tuple[float, float] = (0.1, 0.8)
    fault_rate: float = 0.0
    abandon_rate: float = 0.0
    adversary: bool = False
    limit_max_steps: bool = False
    priority_usernames: tuple[str, ...] = ()
    maintenance_period: tuple[float, float] | None = None
    moderator: Account | None = None
    pause_seconds: float = 0.0


@dataclass
class RequesterSlot:
    lane: Lane
    account: Account
    kind: RequestKind = RequestKind.SERVABLE
    n_choices: tuple[int, ...] = (1,)
    max_pending: int = 3
    submit_interval: tuple[float, float] = (0.3, 1.2)
    cancel_rate: float = 0.0
    target_rate: float = 0.0
    exclude_rate: float = 0.0
    disable_batching_rate: float = 0.0
    prompt_factory: Callable[[str], str] | None = None
    target_pool: tuple[str, ...] = ()
    """Names of workers this requester may name or exclude."""


@dataclass
class ProbeSlot:
    lane: Lane
    account: Account
    worker_name: str


@dataclass
class PopScenario:
    run_id: str = ""
    submit_deadline: float = float("inf")
    worker_slots: list[WorkerSlot] = field(default_factory=list)
    requester_slots: list[RequesterSlot] = field(default_factory=list)
    probe_slots: list[ProbeSlot] = field(default_factory=list)
    worker_ids: dict[str, str] = field(default_factory=dict)
    _lock: gevent.lock.RLock = field(default_factory=gevent.lock.RLock)
    _next: dict[str, int] = field(default_factory=dict)
    _nonces: count = field(default_factory=count)
    drops: dict[str, int] = field(default_factory=dict)
    hosts: list[str] = field(default_factory=list)
    keys_by_alias: dict[str, str] = field(default_factory=dict)

    def next_host(self) -> str | None:
        if not self.hosts:
            return None
        with self._lock:
            index = self._next.get("host", 0)
            self._next["host"] = index + 1
        return self.hosts[index % len(self.hosts)]

    def take(self, kind: str):
        slots = {"worker": self.worker_slots, "requester": self.requester_slots, "probe": self.probe_slots}[kind]
        with self._lock:
            index = self._next.get(kind, 0)
            self._next[kind] = index + 1
        if index >= len(slots):
            return None
        return slots[index]

    def nonce(self) -> str:
        with self._lock:
            return f"{NONCE_PREFIX}{self.run_id}x{next(self._nonces)}"

    def try_drop(self, worker: str) -> bool:
        """Reserve one dropped job for ``worker`` if its budget allows."""
        with self._lock:
            used = self.drops.get(worker, 0)
            if used >= DROP_BUDGET:
                return False
            self.drops[worker] = used + 1
            return True


scenario = PopScenario()


# --------------------------------------------------------------------------- #
# Request and pop payloads                                                     #
# --------------------------------------------------------------------------- #


def default_prompt(nonce: str) -> str:
    return f"a lighthouse in fog {nonce} oil painting"


def request_payload(lane: Lane, slot: RequesterSlot, prompt: str, n: int) -> dict[str, Any]:
    flood = slot.kind is RequestKind.FLOOD
    if lane.gentype == "text":
        return {
            "prompt": prompt,
            "models": [lane.model],
            "params": {"max_length": 16, "max_context_length": 256, "n": n},
            # Text lane workers belong to untrusted owners, who refuse requests for trusted workers on their own check.
            "trusted_workers": flood,
            "slow_workers": True,
            "validated_backends": False,
        }
    return {
        "prompt": prompt,
        "models": [lane.model],
        "params": {
            "width": 512,
            "height": 512,
            "steps": FLOOD_IMAGE_STEPS if flood else SERVABLE_IMAGE_STEPS,
            "n": n,
            "sampler_name": "k_euler",
        },
        "nsfw": False,
        "r2": True,
        "shared": False,
        "trusted_workers": False,
        "slow_workers": True,
    }


def pop_payload(slot: WorkerSlot, models: list[str], amount: Any, **overrides: Any) -> dict[str, Any]:
    profile = slot.profile
    if slot.lane.gentype == "text":
        body: dict[str, Any] = {
            "name": profile.name,
            "models": models,
            "bridge_agent": TEXT_BRIDGE,
            "max_length": 512,
            "max_context_length": 4096,
            "nsfw": True,
            "amount": amount,
            "softprompts": [],
            "threads": 1,
        }
    else:
        body = {
            "name": profile.name,
            "models": models,
            "bridge_agent": IMAGE_BRIDGE,
            "nsfw": True,
            "amount": amount,
            "max_pixels": 4194304,
            "allow_img2img": True,
            "allow_painting": True,
            "allow_unsafe_ipaddr": True,
            "allow_post_processing": True,
            "allow_controlnet": True,
            "allow_sdxl_controlnet": True,
            "allow_lora": True,
            "limit_max_steps": slot.limit_max_steps,
        }
        if profile.blacklist:
            body["blacklist"] = list(profile.blacklist)
    if slot.priority_usernames:
        body["priority_usernames"] = list(slot.priority_usernames)
    body.update(overrides)
    return body


def _paths(gentype: str) -> dict[str, str]:
    if gentype == "text":
        return {
            "async": "/api/v2/generate/text/async",
            "pop": "/api/v2/generate/text/pop",
            "submit": "/api/v2/generate/text/submit",
            "status": "/api/v2/generate/text/status/{id}",
            "check": "/api/v2/generate/text/status/{id}",
        }
    return {
        "async": "/api/v2/generate/async",
        "pop": "/api/v2/generate/pop",
        "submit": "/api/v2/generate/submit",
        "status": "/api/v2/generate/status/{id}",
        "check": "/api/v2/generate/check/{id}",
    }


def status_record(nonce: str, body: dict[str, Any], observed_at: float, *, final: bool = False) -> StatusRecord:
    generations = body.get("generations")
    worker_names = tuple(str(gen.get("worker_name")) for gen in generations) if isinstance(generations, list) else None
    return StatusRecord(
        nonce=nonce,
        observed_at=observed_at,
        finished=int(body.get("finished") or 0),
        processing=int(body.get("processing") or 0),
        waiting=int(body.get("waiting") or 0),
        restarted=int(body.get("restarted") or 0),
        done=bool(body.get("done")),
        faulted=bool(body.get("faulted")),
        worker_names=worker_names,
        final=final,
    )


# --------------------------------------------------------------------------- #
# Requesters                                                                   #
# --------------------------------------------------------------------------- #


class PopRequester(HttpUser):
    """Queues requests in its lane, polls them, and cancels some."""

    weight = 1
    fixed_count = 0
    wait_time = between(0.05, 0.2)

    def on_start(self) -> None:
        slot = scenario.take("requester")
        if slot is None:
            raise StopUser()
        _use_assigned_host(self)
        self.slot: RequesterSlot = slot
        self.paths = _paths(slot.lane.gentype)
        self.pending: dict[str, TrackedRequest] = {}
        self.cancel_at: dict[str, float] = {}
        self.next_submit_at = 0.0

    @task
    def tick(self) -> None:
        now = time.time()
        if now >= self.next_submit_at and len(self.pending) < self.slot.max_pending and now < scenario.submit_deadline:
            self.submit()
            self.next_submit_at = time.time() + random.uniform(*self.slot.submit_interval)
        due = [nonce for nonce, at in self.cancel_at.items() if at <= now and nonce in self.pending]
        if due:
            self.cancel_at.pop(due[0])
            self.cancel(self.pending[due[0]])
        elif self.pending:
            self.poll(random.choice(list(self.pending.values())))

    def submit(self) -> None:
        slot = self.slot
        nonce = scenario.nonce()
        prompt = (slot.prompt_factory or default_prompt)(nonce)
        n = random.choice(slot.n_choices)
        body = request_payload(slot.lane, slot, prompt, n)
        targeted = excluded = None
        known_targets = [name for name in slot.target_pool if name in scenario.worker_ids]
        roll = random.random()
        if known_targets and roll < slot.target_rate:
            targeted = random.choice(known_targets)
            body["workers"] = [scenario.worker_ids[targeted]]
            body["worker_blacklist"] = False
        elif known_targets and roll < slot.target_rate + slot.exclude_rate:
            excluded = random.choice(known_targets)
            body["workers"] = [scenario.worker_ids[excluded]]
            body["worker_blacklist"] = True
        if slot.lane.gentype == "image" and n > 1 and random.random() < slot.disable_batching_rate:
            body["disable_batching"] = True
        name = f"{self.paths['async']} [pop:{slot.lane.name}:{slot.kind}]"
        sent_at = time.time()
        with self.client.post(
            self.paths["async"], json=body, headers=_headers(slot.account.api_key), catch_response=True, name=name
        ) as resp:
            accepted_at = time.time()
            _note_server_error(resp, name, sent_at)
            data = _json(resp) or {}
            if resp.status_code in (200, 202) and isinstance(data, dict) and data.get("id"):
                resp.success()
                request = TrackedRequest(
                    request_id=data["id"],
                    nonce=nonce,
                    lane=slot.lane.name,
                    gentype=slot.lane.gentype,
                    owner=slot.account.alias,
                    n=n,
                    prompt=prompt,
                    kind=slot.kind,
                    sent_at=sent_at,
                    accepted_at=accepted_at,
                    targeted_worker=targeted,
                    excluded_worker=excluded,
                )
                ledger.record_request(request)
                self.pending[nonce] = request
                if random.random() < slot.cancel_rate:
                    self.cancel_at[nonce] = accepted_at + random.uniform(0.0, 3.0)
                return
            rc = data.get("rc") if isinstance(data, dict) else None
            if rc == "TooManyPrompts" or resp.status_code == 429:
                # The requester is at its limit of parallel requests (flooders live there) or rate limited.
                resp.success()
                self.next_submit_at = time.time() + 1.0
                return
            resp.failure(f"{resp.status_code} {rc}: {resp.text[:200]}")

    def poll(self, request: TrackedRequest) -> None:
        path = self.paths["check"].format(id=request.request_id)
        sent_at = time.time()
        with self.client.get(path, headers=_headers(self.slot.account.api_key), catch_response=True, name=self.paths["check"]) as resp:
            observed_at = time.time()
            _note_server_error(resp, self.paths["check"], sent_at)
            data = _json(resp)
            if resp.ok and isinstance(data, dict):
                resp.success()
                ledger.record_status(status_record(request.nonce, data, observed_at))
                if data.get("done") or data.get("faulted"):
                    del self.pending[request.nonce]
                return
            if resp.status_code in (404, 410, 429):
                resp.success()
                if resp.status_code != 429:
                    self.pending.pop(request.nonce, None)
                return
            resp.failure(f"{resp.status_code}: {resp.text[:200]}")

    def cancel(self, request: TrackedRequest) -> None:
        path = self.paths["status"].format(id=request.request_id)
        sent_at = time.time()
        ledger.record_cancel_sent(request.nonce, sent_at)
        with self.client.delete(
            path,
            headers=_headers(self.slot.account.api_key),
            catch_response=True,
            name=f"{self.paths['status']} [cancel]",
        ) as resp:
            acked_at = time.time()
            _note_server_error(resp, f"{self.paths['status']} [cancel]", sent_at)
            if resp.ok or resp.status_code in (404, 410):
                resp.success()
                if resp.ok:
                    ledger.record_cancel_acked(request.nonce, acked_at)
                self.pending.pop(request.nonce, None)
                return
            resp.failure(f"{resp.status_code}: {resp.text[:200]}")


# --------------------------------------------------------------------------- #
# Workers                                                                      #
# --------------------------------------------------------------------------- #


class PopWorker(HttpUser):
    """Pops its lane, submits, faults or abandons, and toggles maintenance or pause if its slot says so."""

    weight = 1
    fixed_count = 0
    wait_time = between(0.0, 0.0)

    def on_start(self) -> None:
        slot = scenario.take("worker")
        if slot is None:
            raise StopUser()
        _use_assigned_host(self)
        self.slot: WorkerSlot = slot
        self.profile = slot.profile
        self.paths = _paths(slot.lane.gentype)
        self.maintenance_interval = None
        self.next_toggle_at = time.time() + random.uniform(*slot.maintenance_period) if slot.maintenance_period else float("inf")
        self.pause_interval = None
        self.unpause_at = float("inf")
        ledger.register_worker(self.profile)
        self.ensure_worker()
        if slot.moderator is not None:
            self.set_paused(True)
            self.unpause_at = time.time() + slot.pause_seconds

    def ensure_worker(self) -> None:
        """Create the worker with a pop for a model nobody requests, and learn its id."""
        if self.profile.name in scenario.worker_ids:
            return
        for _ in range(20):
            body = pop_payload(self.slot, [self.slot.lane.void_model], 1)
            sent_at = time.time()
            with self.client.post(
                self.paths["pop"],
                json=body,
                headers=_headers(self.slot.account.api_key),
                catch_response=True,
                name=f"{self.paths['pop']} [create]",
            ) as resp:
                _note_server_error(resp, f"{self.paths['pop']} [create]", sent_at)
                data = _json(resp) or {}
                if resp.ok and not data.get("id"):
                    resp.success()
                elif resp.ok:
                    resp.failure(f"the creating pop for {self.slot.lane.void_model} was handed {data.get('id')}")
                else:
                    resp.failure(f"{resp.status_code}: {resp.text[:200]}")
                    gevent.sleep(1.0)
                    continue
            with self.client.get(
                f"/api/v2/workers/name/{self.profile.name}",
                headers=_headers(self.slot.account.api_key),
                catch_response=True,
                name="/api/v2/workers/name/[name]",
            ) as resp:
                data = _json(resp) or {}
                if resp.ok and data.get("id"):
                    resp.success()
                    scenario.worker_ids[self.profile.name] = data["id"]
                    return
                resp.failure(f"{resp.status_code}: {resp.text[:200]}")
            gevent.sleep(1.0)
        raise StopUser()

    def put_worker(self, api_key: str, body: dict[str, Any], label: str) -> tuple[float, float | None]:
        worker_id = scenario.worker_ids[self.profile.name]
        sent_at = time.time()
        with self.client.put(
            f"/api/v2/workers/{worker_id}",
            json=body,
            headers=_headers(api_key),
            catch_response=True,
            name=f"/api/v2/workers/[id] [{label}]",
        ) as resp:
            acked_at = time.time()
            _note_server_error(resp, f"/api/v2/workers/[id] [{label}]", sent_at)
            if resp.ok:
                resp.success()
                return sent_at, acked_at
            resp.failure(f"{resp.status_code}: {resp.text[:200]}")
            return sent_at, None

    def set_maintenance(self, on: bool) -> None:
        if on:
            interval = ledger.begin_state("maintenance", self.profile.name, time.time())
            _, acked_at = self.put_worker(self.slot.account.api_key, {"maintenance": True}, "maintenance-on")
            if acked_at is not None:
                ledger.acknowledge_begin(interval, acked_at)
            self.maintenance_interval = interval
            return
        interval = self.maintenance_interval
        if interval is None:
            return
        ledger.end_state(interval, time.time())
        _, acked_at = self.put_worker(self.slot.account.api_key, {"maintenance": False}, "maintenance-off")
        if acked_at is not None:
            ledger.acknowledge_end(interval, acked_at)
            self.maintenance_interval = None

    def set_paused(self, on: bool) -> None:
        assert self.slot.moderator is not None
        if on:
            interval = ledger.begin_state("paused", self.profile.name, time.time())
            _, acked_at = self.put_worker(self.slot.moderator.api_key, {"paused": True}, "pause")
            if acked_at is not None:
                ledger.acknowledge_begin(interval, acked_at)
            self.pause_interval = interval
            return
        interval = self.pause_interval
        if interval is None:
            return
        ledger.end_state(interval, time.time())
        _, acked_at = self.put_worker(self.slot.moderator.api_key, {"paused": False}, "unpause")
        if acked_at is not None:
            ledger.acknowledge_end(interval, acked_at)
            self.pause_interval = None

    @task
    def work(self) -> None:
        now = time.time()
        if now >= self.next_toggle_at:
            self.set_maintenance(self.maintenance_interval is None)
            self.next_toggle_at = time.time() + random.uniform(*self.slot.maintenance_period)
        if now >= self.unpause_at:
            self.set_paused(False)
            self.unpause_at = float("inf")
        job = self.pop()
        if job is not None:
            self.handle(*job)
        gevent.sleep(random.uniform(*self.slot.pop_interval))

    def pop(self) -> tuple[PopRecord, dict[str, Any]] | None:
        amount = random.choice(self.slot.amounts)
        body = pop_payload(self.slot, [self.slot.lane.model], amount)
        record = PopRecord(worker=self.profile.name, lane=self.slot.lane.name, sent_at=time.time(), received_at=None, amount=amount)
        ledger.record_pop_sent(record)
        with self.client.post(
            self.paths["pop"],
            json=body,
            headers=_headers(self.slot.account.api_key),
            catch_response=True,
            name=f"{self.paths['pop']} [pop:{self.slot.lane.name}]",
        ) as resp:
            received_at = time.time()
            _note_server_error(resp, f"{self.paths['pop']} [pop:{self.slot.lane.name}]", record.sent_at)
            data = _json(resp)
            if resp.status_code == 0 or not isinstance(data, dict):
                resp.failure(f"pop got no answer: {resp.status_code} {resp.text[:200]}")
                return None
            record.status = resp.status_code
            record.rc = data.get("rc")
            record.received_at = received_at
            if not resp.ok:
                if resp.status_code == 403 and record.rc == "WorkerMaintenance":
                    resp.success()
                else:
                    resp.failure(f"{resp.status_code} {record.rc}: {resp.text[:200]}")
                ledger.complete_pop(record)
                return None
            resp.success()
            record.skipped = {key: value for key, value in (data.get("skipped") or {}).items() if isinstance(value, int)}
            job_ids = tuple(str(job_id) for job_id in (data.get("ids") or ([data["id"]] if data.get("id") else [])))
            if not job_ids:
                ledger.complete_pop(record)
                return None
            payload = data.get("payload") or {}
            record.job_ids = job_ids
            record.ttl = data.get("ttl") if isinstance(data.get("ttl"), int) else None
            record.nonce = ledger.nonce_for_prompt(payload.get("prompt"))
            ledger.complete_pop(record)
            return record, data

    def handle(self, record: PopRecord, data: dict[str, Any]) -> None:
        slot = self.slot
        gevent.sleep(random.uniform(*slot.gen_time))
        if slot.adversary and scenario.try_drop(self.profile.name):
            self.adversarial_submit(record)
            return
        for job_id in record.job_ids:
            roll = random.random()
            if roll < slot.abandon_rate and scenario.try_drop(self.profile.name):
                continue
            if roll < slot.abandon_rate + slot.fault_rate and scenario.try_drop(self.profile.name):
                self.submit(job_id, "faulted")
                continue
            self.submit(job_id, "ok")

    def adversarial_submit(self, record: PopRecord) -> None:
        """Submit a job in ways a broken or hostile worker might: twice faulted, concurrently, or faulted after delivery."""
        mode = random.choice(("double_fault", "concurrent_fault", "late_fault"))
        for job_id in record.job_ids:
            if mode == "double_fault":
                self.submit(job_id, "faulted", label="double-fault")
                self.submit(job_id, "faulted", label="double-fault")
            elif mode == "concurrent_fault":
                gevent.joinall([gevent.spawn(self.submit, job_id, "faulted", "concurrent-fault") for _ in range(3)])
            else:
                self.submit(job_id, "ok")
                self.submit(job_id, "faulted", label="late-fault")

    def submit(self, job_id: str, state: str, label: str | None = None) -> None:
        body = {"id": job_id, "state": state, "seed": random.randint(0, 2**31 - 1)}
        body["generation"] = "R2" if self.slot.lane.gentype == "image" else "a short story"
        record = SubmitRecord(job_id=job_id, state=state, sent_at=time.time(), received_at=None, status=None)
        ledger.record_submit_sent(record)
        with self.client.post(
            self.paths["submit"],
            json=body,
            headers=_headers(self.slot.account.api_key),
            catch_response=True,
            name=f"{self.paths['submit']} [{label or state}]",
        ) as resp:
            record.received_at = time.time()
            _note_server_error(resp, f"{self.paths['submit']} [{label or state}]", record.sent_at)
            record.status = resp.status_code or None
            rc = (_json(resp) or {}).get("rc") if resp.status_code else None
            if resp.ok:
                resp.success()
            elif resp.status_code in (400, 404) and rc in ("DuplicateGen", "AbortedGen", "InvalidJobID", "InvalidProcGen"):
                # A fake job, a second submit, or a job given back: the API refuses it without error.
                resp.success()
            elif resp.status_code and resp.status_code < 500 and label:
                resp.success()
            else:
                resp.failure(f"{resp.status_code} {rc}: {resp.text[:200]}")


# --------------------------------------------------------------------------- #
# Probes                                                                       #
# --------------------------------------------------------------------------- #

# (label, pop overrides, accepted outcomes). An outcome is an HTTP status, "2xx", "5xx" or "transport". The pop
# endpoint documents priority usernames as 'alias#number' and validates amount as an integer of at least 1;
# every malformed input must be refused or ignored, never answered with a server error.
_POP_PROBES: list[tuple[str, dict[str, Any], str]] = [
    ("priority:no-hash", {"priority_usernames": ["nohash"]}, "400"),
    ("priority:empty-id", {"priority_usernames": ["someone#"]}, "400"),
    ("priority:bare-hash", {"priority_usernames": ["#"]}, "400"),
    ("priority:non-numeric-id", {"priority_usernames": ["someone#abc"]}, "400"),
    ("priority:sql-id", {"priority_usernames": ["someone#1;DROP TABLE users"]}, "400"),
    ("priority:overflowing-id", {"priority_usernames": ["someone#99999999999999999999"]}, "400|2xx"),
    ("priority:negative-id", {"priority_usernames": ["someone#-1"]}, "400|2xx"),
    ("priority:non-ascii-digit-id", {"priority_usernames": ["someone#١"]}, "400|2xx"),
    ("priority:unknown-user", {"priority_usernames": ["ghost#2147483646"]}, "2xx"),
    ("priority:thousands", {"priority_usernames": [f"ghost#{2_000_000_000 + i}" for i in range(2000)]}, "2xx|400"),
    ("amount:zero", {"amount": 0}, "400"),
    ("amount:negative", {"amount": -5}, "400"),
    ("amount:maximum", {"amount": 20}, "2xx"),
    ("amount:above-maximum", {"amount": 21}, "400"),
    ("amount:int32-max", {"amount": 2**31 - 1}, "400"),
    ("amount:int64-overflow", {"amount": 2**63}, "400"),
    ("amount:huge", {"amount": 10**30}, "400"),
    ("amount:string", {"amount": "3"}, "2xx|400"),
    ("amount:float", {"amount": 1.5}, "2xx|400"),
    ("amount:bool", {"amount": True}, "2xx|400"),
    ("models:many", {"models": [f"probe-model-{i}" for i in range(600)]}, "2xx|400"),
]

_IMAGE_POP_PROBES: list[tuple[str, dict[str, Any], str]] = [
    ("blacklist:wildcards", {"blacklist": ["%", "_", "\\", "%%", "a_c", "\\%"]}, "2xx"),
    ("blacklist:thousands", {"blacklist": [f"word{i}" for i in range(5000)]}, "2xx|400"),
    ("blacklist:long-word", {"blacklist": ["x" * 20000]}, "2xx|400"),
    ("blacklist:nul", {"blacklist": ["bad\x00word"]}, "2xx|400"),
    ("blacklist:empty-word", {"blacklist": [""]}, "2xx|400"),
]


class PopProber(HttpUser):
    """Sends malformed and extreme pops against a lane holding its own small queue, so the claim path runs."""

    weight = 1
    fixed_count = 0
    wait_time = between(0.2, 0.6)

    def on_start(self) -> None:
        slot = scenario.take("probe")
        if slot is None:
            raise StopUser()
        _use_assigned_host(self)
        self.slot: ProbeSlot = slot
        self.paths = _paths(slot.lane.gentype)
        self.worker_slot = WorkerSlot(
            profile=WorkerProfile(name=slot.worker_name, lane=slot.lane.name, gentype=slot.lane.gentype, owner=slot.account.alias),
            account=slot.account,
            lane=slot.lane,
        )
        probes = list(_POP_PROBES)
        if slot.lane.gentype == "image":
            probes.extend(_IMAGE_POP_PROBES)
        self.probes = probes
        self.requester = RequesterSlot(lane=slot.lane, account=slot.account, n_choices=(2, 3), max_pending=2)
        self.pending: list[str] = []

    @task
    def probe(self) -> None:
        self.keep_queue()
        label, probe_overrides, expected = random.choice(self.probes)
        overrides = dict(probe_overrides)
        amount = overrides.pop("amount", 1)
        models = overrides.pop("models", [self.slot.lane.model])
        body = pop_payload(self.worker_slot, models, amount, **overrides)
        record = PopRecord(
            worker=self.slot.worker_name,
            lane=self.slot.lane.name,
            sent_at=time.time(),
            received_at=None,
            amount=amount if isinstance(amount, int) and not isinstance(amount, bool) and 0 < amount <= 20 else 1,
        )
        ledger.record_pop_sent(record)
        with self.client.post(
            self.paths["pop"],
            json=body,
            headers=_headers(self.slot.account.api_key),
            catch_response=True,
            name=f"{self.paths['pop']} [probe:{label}]",
        ) as resp:
            received_at = time.time()
            data = _json(resp)
            status = resp.status_code or None
            ledger.record_probe(ProbeRecord(label=label, sent_at=record.sent_at, status=status, expected=expected, detail=resp.text[:200]))
            # The ledger judges the outcome; Locust's table shows it as a success either way.
            resp.success()
            if status is None or not isinstance(data, dict):
                return
            record.status = status
            record.received_at = received_at
            if resp.ok:
                job_ids = tuple(str(job_id) for job_id in (data.get("ids") or ([data["id"]] if data.get("id") else [])))
                record.job_ids = job_ids
                record.ttl = data.get("ttl") if isinstance(data.get("ttl"), int) else None
                record.nonce = ledger.nonce_for_prompt((data.get("payload") or {}).get("prompt"))
            ledger.complete_pop(record)
        for job_id in record.job_ids:
            self.submit(job_id)

    def submit(self, job_id: str) -> None:
        body = {"id": job_id, "state": "ok", "generation": "R2" if self.slot.lane.gentype == "image" else "a story", "seed": 1}
        record = SubmitRecord(job_id=job_id, state="ok", sent_at=time.time(), received_at=None, status=None)
        ledger.record_submit_sent(record)
        with self.client.post(
            self.paths["submit"],
            json=body,
            headers=_headers(self.slot.account.api_key),
            catch_response=True,
            name=f"{self.paths['submit']} [probe]",
        ) as resp:
            record.received_at = time.time()
            _note_server_error(resp, f"{self.paths['submit']} [probe]", record.sent_at)
            record.status = resp.status_code or None
            if resp.ok or resp.status_code in (400, 404):
                resp.success()
            else:
                resp.failure(f"{resp.status_code}: {resp.text[:200]}")

    def keep_queue(self) -> None:
        """Keep a couple of multi-generation requests queued, so an extreme amount reaches the claim."""
        if time.time() >= scenario.submit_deadline:
            return
        live = [nonce for nonce in self.pending if self.request_left(nonce)]
        self.pending = live
        if len(live) >= self.requester.max_pending:
            return
        nonce = scenario.nonce()
        prompt = default_prompt(nonce)
        n = random.choice(self.requester.n_choices)
        body = request_payload(self.slot.lane, self.requester, prompt, n)
        sent_at = time.time()
        with self.client.post(
            self.paths["async"],
            json=body,
            headers=_headers(self.slot.account.api_key),
            catch_response=True,
            name=f"{self.paths['async']} [probe]",
        ) as resp:
            accepted_at = time.time()
            _note_server_error(resp, f"{self.paths['async']} [probe]", sent_at)
            data = _json(resp) or {}
            if resp.status_code in (200, 202) and data.get("id"):
                resp.success()
                ledger.record_request(
                    TrackedRequest(
                        request_id=data["id"],
                        nonce=nonce,
                        lane=self.slot.lane.name,
                        gentype=self.slot.lane.gentype,
                        owner=self.slot.account.alias,
                        n=n,
                        prompt=prompt,
                        kind=RequestKind.SERVABLE,
                        sent_at=sent_at,
                        accepted_at=accepted_at,
                    ),
                )
                self.pending.append(nonce)
            elif resp.status_code in (400, 429, 503):
                resp.success()
            else:
                resp.failure(f"{resp.status_code}: {resp.text[:200]}")

    def request_left(self, nonce: str) -> bool:
        request = ledger.requests[nonce]
        claimed = sum(len(pop.job_ids) for pop in ledger.pops if pop.nonce == nonce)
        return claimed < request.n


def new_run_id() -> str:
    return uuid.uuid4().hex[:6]

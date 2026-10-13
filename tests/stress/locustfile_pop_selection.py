# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Locust entrypoint for the pop-selection scenario.

Drives the image and text pop endpoints with isolated lanes of workers and requesters (see
``locustsuite/users/pop_selection.py``) and, when the run stops, reads every request's final status and judges the
whole run with the oracles in ``locustsuite/pop_ledger.py``. Violations and a coverage summary are written to the
evidence JSONL that ``check_pop_selection_results.py`` gates on.

The scenario mints its own accounts (trusted and untrusted owners, rich and penniless requesters, a moderator)
through ``POST /api/v2/dev/test-user``, so the server must run with ``HORDE_TEST_APIKEYS=1`` and Locust must reach
it over loopback. Run the server with ``HORDE_TEST_RATELIMIT_DISABLED=1``: requesters poll and workers pop faster
than the default limits allow, and a throttled run would leave the oracles too little to judge.

Usage:
    locust -f tests/stress/locustfile_pop_selection.py --host http://localhost:7001 \
        --headless --users 90 --spawn-rate 30 --run-time 240s \
        --pop-evidence pop_evidence.jsonl --csv pop --only-summary
    python tests/stress/check_pop_selection_results.py --evidence pop_evidence.jsonl --stats pop_stats.csv
"""
# ruff: noqa: I001

from __future__ import annotations

import logging
import time
import uuid

import gevent
import gevent.pool
import requests
from locust import events

from locustsuite import events as _suite_events  # noqa: F401 - shared CLI arguments and target preflight
from locustsuite.helpers import _parse_csv
from locustsuite.pop_ledger import RequestKind, WorkerProfile, evidence_writer, ledger, lost_generation_suspects
from locustsuite.users.pop_selection import (
    CLIENT_AGENT,
    Account,
    Lane,
    PopProber,
    PopRequester,
    PopWorker,
    ProbeSlot,
    RequesterSlot,
    WorkerSlot,
    _paths,
    new_run_id,
    scenario,
    status_record,
)

__all__ = ["PopProber", "PopRequester", "PopWorker"]

logger = logging.getLogger(__name__)

_IMAGE_LANE_MODELS = {
    "swarm": "Comic-Diffusion",
    "prio": "Epic Diffusion",
    "fifo": "Inkpunk Diffusion",
    "maint": "Dungeons and Diffusion",
    "paused": "AIO Pixel Art",
    "adversary": "Fantasy Card Diffusion",
    "faults": "Ghibli Diffusion",
    "blacklist": "App Icon Diffusion",
    "unicode": "Midjourney PaintArt",
    "probe": "Sci-Fi Diffusion",
    "void": "Dreamlike Diffusion",
}

# Clean prompts contain none of the blacklisted words as written; dirty prompts contain one in another case. The
# wildcard-looking words must match only themselves: "a_c" is not in "abc", and "%" or "_" in nothing clean.
_BLACKLIST_WORDS = ("Zebra", "%", "_", "\\", "a_c")
_BLACKLIST_CLEAN = ("abc and a-c at 50 percent {nonce}", "zeb ra crossing {nonce}", "plain meadow {nonce}")
_BLACKLIST_DIRTY = ("a ZEBRA herd {nonce}", "100% charged {nonce}", "snake_case sign {nonce}", "back\\slash {nonce}", "xA_Cx {nonce}")
# Python lowercases "İ" to "i̇" (two code points), so "istanbul" is not in "İSTANBUL".lower(): the worker's own check
# serves it. "STRASSE" does not contain "straße". The dirty prompts contain the words in another case.
_UNICODE_WORDS = ("istanbul", "straße")
_UNICODE_CLEAN = ("İSTANBUL at dusk {nonce}", "STRASSE map {nonce}")
_UNICODE_DIRTY = ("Istanbul harbor {nonce}", "STRAßE sign {nonce}")


@events.init_command_line_parser.add_listener
def _add_pop_arguments(parser) -> None:
    group = parser.add_argument_group("AI Horde Pop Selection Scenario")
    group.add_argument("--pop-gentypes", type=str, env_var="POP_GENTYPES", default="text,image", help="Gentypes to drive (text,image).")
    group.add_argument("--pop-evidence", type=str, env_var="POP_EVIDENCE", default="pop_evidence.jsonl", help="Evidence JSONL path.")
    group.add_argument(
        "--pop-drain-seconds", type=float, env_var="POP_DRAIN_SECONDS", default=40.0, help="Stop queuing this long before the run ends."
    )
    group.add_argument(
        "--pop-swarm-workers", type=int, env_var="POP_SWARM_WORKERS", default=6, help="Swarm workers per gentype (two share one identity)."
    )
    group.add_argument("--pop-swarm-requesters", type=int, env_var="POP_SWARM_REQUESTERS", default=4, help="Swarm requesters per gentype.")
    group.add_argument(
        "--pop-priority-workers", type=int, env_var="POP_PRIORITY_WORKERS", default=2, help="Priority-lane workers with priority users."
    )
    group.add_argument(
        "--pop-priority-flooders",
        type=int,
        env_var="POP_PRIORITY_FLOODERS",
        default=8,
        help="Priority users queuing requests every lane worker refuses on its own check. Each holds up to 30, so 8 exceed 220.",
    )
    group.add_argument(
        "--pop-priority-requesters", type=int, env_var="POP_PRIORITY_REQUESTERS", default=2, help="Priority users with servable work."
    )
    group.add_argument("--pop-strangers", type=int, env_var="POP_STRANGERS", default=2, help="Rich strangers in the priority lane.")
    group.add_argument(
        "--pop-pause-seconds", type=float, env_var="POP_PAUSE_SECONDS", default=60.0, help="How long the paused worker stays paused."
    )
    group.add_argument(
        "--pop-fault-rate", type=float, env_var="POP_FAULT_RATE", default=0.05, help="Swarm job fault rate (within the drop budget)."
    )
    group.add_argument(
        "--pop-abandon-rate", type=float, env_var="POP_ABANDON_RATE", default=0.02, help="Swarm job abandon rate (within the drop budget)."
    )
    group.add_argument("--pop-cancel-rate", type=float, env_var="POP_CANCEL_RATE", default=0.08, help="Swarm request cancel rate.")
    group.add_argument(
        "--pop-lanes",
        type=str,
        env_var="POP_LANES",
        default="swarm,prio,fifo,maint,paused,adversary,faults,blacklist,unicode,probe",
        help="Lanes to run.",
    )
    group.add_argument(
        "--pop-image-models",
        type=str,
        env_var="POP_IMAGE_MODELS",
        default="",
        help="Override image lane models as lane=model pairs, comma separated. Each must be a known model no other worker serves.",
    )
    group.add_argument(
        "--pop-allow-served-models",
        action="store_true",
        env_var="POP_ALLOW_SERVED_MODELS",
        default=False,
        help="Run even if another worker already serves a lane model (the oracles' bounds are then unsound).",
    )
    group.add_argument(
        "--pop-hosts",
        type=str,
        env_var="POP_HOSTS",
        default="",
        help="Comma-separated server base URLs sharing one database; users are spread over them round robin. Defaults to --host.",
    )
    group.add_argument("--pop-ledger-dump", type=str, env_var="POP_LEDGER_DUMP", default="", help="Write every observation here as JSONL.")
    group.add_argument(
        "--pop-final-concurrency", type=int, env_var="POP_FINAL_CONCURRENCY", default=16, help="Parallel final status reads."
    )
    # The shared bootstrap registers keys through /register; this scenario mints its own.
    parser.set_defaults(bootstrap_requestors=0, bootstrap_workers=0)


class _Minter:
    def __init__(self, host: str, run_id: str) -> None:
        self.host = host
        self.run_id = run_id
        self.session = requests.Session()
        self.count = 0

    def __call__(self, role: str, *, trusted: bool = False, kudos: int = 1000, moderator: bool = False) -> Account:
        self.count += 1
        username = f"pop{self.run_id}{role}{self.count}"
        resp = self.session.post(
            f"{self.host}/api/v2/dev/test-user",
            json={"username": username, "oauth_id": username, "trusted": trusted, "kudos": kudos, "moderator": moderator},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Minting {role} through /api/v2/dev/test-user returned {resp.status_code}: {resp.text[:200]}. "
                "Run the server with HORDE_TEST_APIKEYS=1 and reach it over loopback.",
            )
        data = resp.json()
        account = Account(api_key=data["api_key"], alias=data["alias"], user_id=str(data["user_id"]), trusted=bool(data["trusted"]))
        scenario.keys_by_alias[account.alias] = account.api_key
        return account


def _lane(run_id: str, gentype: str, name: str, image_models: dict[str, str]) -> Lane:
    if gentype == "text":
        return Lane(name=f"{name}-text", gentype="text", model=f"popsel/{run_id}/{name}", void_model=f"popsel/{run_id}/void")
    return Lane(name=f"{name}-image", gentype="image", model=image_models[name], void_model=image_models["void"])


def _cycle_factory(templates: tuple[str, ...]):
    state = {"index": 0}

    def factory(nonce: str) -> str:
        template = templates[state["index"] % len(templates)]
        state["index"] += 1
        return template.format(nonce=nonce)

    return factory


_WORKER_PREFIX = "PopSel-"


def _worker_name(run_id: str, lane: Lane, label: str) -> str:
    return f"{_WORKER_PREFIX}{run_id}-{lane.name}-{label}"


def build_scenario(opts, mint: _Minter, run_id: str) -> None:
    """Fill the shared scenario with one slot per user, lane by lane."""
    lanes_enabled = set(_parse_csv(opts.pop_lanes))
    image_models = dict(_IMAGE_LANE_MODELS)
    for pair in _parse_csv(opts.pop_image_models):
        lane_name, _, model = pair.partition("=")
        image_models[lane_name.strip()] = model.strip()
    moderator = mint("mod", moderator=True) if "paused" in lanes_enabled else None

    for gentype in _parse_csv(opts.pop_gentypes):
        if "swarm" in lanes_enabled:
            lane = _lane(run_id, gentype, "swarm", image_models)
            # 20 is the largest amount the pop endpoints accept.
            amounts_cycle = [(1,), (2,), (4,), (1, 3), (1, 20), (2, 5)]
            identities: list[tuple[WorkerProfile, Account]] = []
            for index in range(max(opts.pop_swarm_workers, 0)):
                if index == 1 and identities:
                    # The second greenlet pops under the first one's identity, concurrently.
                    profile, account = identities[0]
                else:
                    account = mint("swarmw")
                    profile = WorkerProfile(
                        name=_worker_name(run_id, lane, f"w{index}"), lane=lane.name, gentype=gentype, owner=account.alias
                    )
                    identities.append((profile, account))
                scenario.worker_slots.append(
                    WorkerSlot(
                        profile=profile,
                        account=account,
                        lane=lane,
                        amounts=amounts_cycle[index % len(amounts_cycle)],
                        pop_interval=(0.1, 0.6),
                        fault_rate=opts.pop_fault_rate,
                        abandon_rate=opts.pop_abandon_rate,
                    ),
                )
            target_pool = tuple(profile.name for profile, _ in identities)
            for _ in range(max(opts.pop_swarm_requesters, 0)):
                scenario.requester_slots.append(
                    RequesterSlot(
                        lane=lane,
                        account=mint("swarmr", trusted=True, kudos=100_000),
                        n_choices=(1, 2, 3, 4, 6),
                        max_pending=4,
                        submit_interval=(0.1, 0.6),
                        cancel_rate=opts.pop_cancel_rate,
                        target_rate=0.08,
                        exclude_rate=0.08,
                        disable_batching_rate=0.3,
                        target_pool=target_pool,
                    ),
                )

        if "prio" in lanes_enabled:
            lane = _lane(run_id, gentype, "prio", image_models)
            flooders = [mint("flood", kudos=10_000_000) for _ in range(max(opts.pop_priority_flooders, 0))]
            priority_requesters = [mint("prior", kudos=0) for _ in range(max(opts.pop_priority_requesters, 0))]
            strangers = [mint("strange", trusted=True, kudos=1_000_000) for _ in range(max(opts.pop_strangers, 0))]
            priority_aliases = [account.alias for account in flooders + priority_requesters]
            for index in range(max(opts.pop_priority_workers, 0)):
                account = mint("priow")
                # Unknown users, the owner itself and a duplicate are accepted and change nothing.
                junk = ["ghost#2147483646", account.alias] + priority_aliases[:1]
                profile = WorkerProfile(
                    name=_worker_name(run_id, lane, f"p{index}"),
                    lane=lane.name,
                    gentype=gentype,
                    owner=account.alias,
                    priority_owners=frozenset([account.alias, *priority_aliases]),
                    refused_kinds=frozenset([RequestKind.FLOOD]),
                    is_priority_worker=True,
                )
                scenario.worker_slots.append(
                    WorkerSlot(
                        profile=profile,
                        account=account,
                        lane=lane,
                        pop_interval=(0.2, 0.8),
                        limit_max_steps=True,
                        priority_usernames=tuple(priority_aliases + junk),
                    ),
                )
            account = mint("priog")
            scenario.worker_slots.append(
                WorkerSlot(
                    profile=WorkerProfile(
                        name=_worker_name(run_id, lane, "general"),
                        lane=lane.name,
                        gentype=gentype,
                        owner=account.alias,
                        refused_kinds=frozenset([RequestKind.FLOOD]),
                    ),
                    account=account,
                    lane=lane,
                    pop_interval=(0.2, 0.8),
                    limit_max_steps=True,
                ),
            )
            for account in flooders:
                scenario.requester_slots.append(
                    RequesterSlot(lane=lane, account=account, kind=RequestKind.FLOOD, max_pending=30, submit_interval=(0.0, 0.05)),
                )
            for account in priority_requesters:
                scenario.requester_slots.append(
                    RequesterSlot(lane=lane, account=account, n_choices=(1, 2), max_pending=3, submit_interval=(0.5, 2.0))
                )
            for account in strangers:
                scenario.requester_slots.append(
                    RequesterSlot(lane=lane, account=account, n_choices=(1, 2), max_pending=5, submit_interval=(0.2, 0.8))
                )

        if "fifo" in lanes_enabled:
            lane = _lane(run_id, gentype, "fifo", image_models)
            account = mint("fifow")
            scenario.worker_slots.append(
                WorkerSlot(
                    profile=WorkerProfile(
                        name=_worker_name(run_id, lane, "w"), lane=lane.name, gentype=gentype, owner=account.alias, fifo=True
                    ),
                    account=account,
                    lane=lane,
                    pop_interval=(0.8, 1.6),
                ),
            )
            scenario.requester_slots.append(
                RequesterSlot(lane=lane, account=mint("fifor", kudos=5000), max_pending=8, submit_interval=(0.2, 0.5)),
            )

        if "maint" in lanes_enabled:
            lane = _lane(run_id, gentype, "maint", image_models)
            owner = mint("maintw")
            friend = mint("maintf")
            stranger = mint("maints")
            scenario.worker_slots.append(
                WorkerSlot(
                    profile=WorkerProfile(
                        name=_worker_name(run_id, lane, "m"),
                        lane=lane.name,
                        gentype=gentype,
                        owner=owner.alias,
                        priority_owners=frozenset([owner.alias, friend.alias]),
                    ),
                    account=owner,
                    lane=lane,
                    pop_interval=(0.3, 0.9),
                    priority_usernames=(friend.alias,),
                    maintenance_period=(6.0, 12.0),
                ),
            )
            helper = mint("mainth")
            scenario.worker_slots.append(
                WorkerSlot(
                    profile=WorkerProfile(name=_worker_name(run_id, lane, "helper"), lane=lane.name, gentype=gentype, owner=helper.alias),
                    account=helper,
                    lane=lane,
                    pop_interval=(1.0, 2.0),
                ),
            )
            scenario.requester_slots.append(RequesterSlot(lane=lane, account=owner, max_pending=2, submit_interval=(1.0, 3.0)))
            scenario.requester_slots.append(RequesterSlot(lane=lane, account=friend, max_pending=3, submit_interval=(0.5, 1.5)))
            scenario.requester_slots.append(RequesterSlot(lane=lane, account=stranger, max_pending=3, submit_interval=(0.5, 1.5)))

        if "paused" in lanes_enabled and moderator is not None:
            lane = _lane(run_id, gentype, "paused", image_models)
            owner = mint("pausew")
            scenario.worker_slots.append(
                WorkerSlot(
                    profile=WorkerProfile(name=_worker_name(run_id, lane, "p"), lane=lane.name, gentype=gentype, owner=owner.alias),
                    account=owner,
                    lane=lane,
                    pop_interval=(0.3, 0.8),
                    moderator=moderator,
                    pause_seconds=opts.pop_pause_seconds,
                ),
            )
            normal = mint("pausen")
            scenario.worker_slots.append(
                WorkerSlot(
                    profile=WorkerProfile(name=_worker_name(run_id, lane, "n"), lane=lane.name, gentype=gentype, owner=normal.alias),
                    account=normal,
                    lane=lane,
                    pop_interval=(1.0, 2.0),
                ),
            )
            scenario.requester_slots.append(
                RequesterSlot(lane=lane, account=mint("pauses"), n_choices=(1, 2), max_pending=4, submit_interval=(0.3, 1.0))
            )
            scenario.requester_slots.append(RequesterSlot(lane=lane, account=owner, max_pending=2, submit_interval=(2.0, 4.0)))

        if "adversary" in lanes_enabled:
            lane = _lane(run_id, gentype, "adversary", image_models)
            for index in range(2):
                account = mint("advw")
                scenario.worker_slots.append(
                    WorkerSlot(
                        profile=WorkerProfile(
                            name=_worker_name(run_id, lane, f"a{index}"), lane=lane.name, gentype=gentype, owner=account.alias
                        ),
                        account=account,
                        lane=lane,
                        pop_interval=(0.3, 1.0),
                        adversary=True,
                    ),
                )
            scenario.requester_slots.append(RequesterSlot(lane=lane, account=mint("advr", kudos=100_000), n_choices=(2, 3), max_pending=3))

        if "faults" in lanes_enabled:
            # Honest workers that fault half their jobs (each within its drop budget), on multi-generation requests:
            # every faulted generation must be handed out again until the request has all it asked for.
            lane = _lane(run_id, gentype, "faults", image_models)
            for index in range(3):
                account = mint("faultw")
                scenario.worker_slots.append(
                    WorkerSlot(
                        profile=WorkerProfile(
                            name=_worker_name(run_id, lane, f"f{index}"), lane=lane.name, gentype=gentype, owner=account.alias
                        ),
                        account=account,
                        lane=lane,
                        amounts=((1,), (2,), (1, 3))[index],
                        pop_interval=(0.2, 0.8),
                        fault_rate=0.5,
                    ),
                )
            scenario.requester_slots.append(
                RequesterSlot(lane=lane, account=mint("faultr", kudos=100_000), n_choices=(2, 3, 4), max_pending=3),
            )

        if gentype == "image":
            for lane_name, words, clean, dirty in (
                ("blacklist", _BLACKLIST_WORDS, _BLACKLIST_CLEAN, _BLACKLIST_DIRTY),
                ("unicode", _UNICODE_WORDS, _UNICODE_CLEAN, _UNICODE_DIRTY),
            ):
                if lane_name not in lanes_enabled:
                    continue
                lane = _lane(run_id, gentype, lane_name, image_models)
                account = mint("blw")
                scenario.worker_slots.append(
                    WorkerSlot(
                        profile=WorkerProfile(
                            name=_worker_name(run_id, lane, "bl"), lane=lane.name, gentype=gentype, owner=account.alias, blacklist=words
                        ),
                        account=account,
                        lane=lane,
                        pop_interval=(0.2, 0.6),
                    ),
                )
                drain = mint("bld")
                scenario.worker_slots.append(
                    WorkerSlot(
                        profile=WorkerProfile(name=_worker_name(run_id, lane, "drain"), lane=lane.name, gentype=gentype, owner=drain.alias),
                        account=drain,
                        lane=lane,
                        pop_interval=(2.0, 4.0),
                    ),
                )
                templates = tuple(item for pair in zip(clean * len(dirty), dirty * len(clean), strict=True) for item in pair)
                scenario.requester_slots.append(
                    RequesterSlot(
                        lane=lane, account=mint("blr"), max_pending=6, submit_interval=(0.3, 0.8), prompt_factory=_cycle_factory(templates)
                    ),
                )

        if "probe" in lanes_enabled:
            lane = _lane(run_id, gentype, "probe", image_models)
            scenario.probe_slots.append(ProbeSlot(lane=lane, account=mint("probe"), worker_name=_worker_name(run_id, lane, "probe")))


def _foreign_workers_serving(host: str, models: set[str]) -> dict[str, list[str]]:
    """Return, per lane model, the workers other than this scenario's (from any run) that serve it."""
    foreign: dict[str, list[str]] = {}
    for model_type in ("image", "text"):
        try:
            resp = requests.get(f"{host}/api/v2/status/models", params={"type": model_type}, timeout=15)
            entries = resp.json() if resp.ok else []
        except (requests.RequestException, ValueError):
            entries = []
        served = {entry.get("name") for entry in entries if isinstance(entry, dict) and int(entry.get("count") or 0) > 0} & models
        if not served:
            continue
        try:
            resp = requests.get(f"{host}/api/v2/workers", params={"type": model_type}, timeout=30)
            workers = resp.json() if resp.ok else []
        except (requests.RequestException, ValueError):
            workers = []
        for worker in workers if isinstance(workers, list) else []:
            name = str(worker.get("name", ""))
            if name.startswith(_WORKER_PREFIX):
                continue
            for model in set(worker.get("models") or []) & served:
                foreign.setdefault(model, []).append(name)
        if not workers:
            logger.warning("Lane models %s show active workers but the worker list is empty; cannot tell whose they are.", sorted(served))
    return foreign


@events.test_start.add_listener
def _configure_pop_selection(environment, **_kwargs) -> None:
    opts = environment.parsed_options
    host = (environment.host or "").rstrip("/") or "http://localhost:7001"
    run_id = new_run_id()
    scenario.run_id = run_id
    scenario.hosts = [item.rstrip("/") for item in _parse_csv(opts.pop_hosts)]
    evidence_writer.configure(opts.pop_evidence)
    build_scenario(opts, _Minter(host, run_id), run_id)

    lane_models = {slot.lane.model for slot in scenario.worker_slots} | {slot.lane.void_model for slot in scenario.worker_slots}
    foreign = _foreign_workers_serving(host, lane_models)
    if foreign and not opts.pop_allow_served_models:
        message = f"Lane models are served by workers outside this scenario, which would void the oracles' bounds: {foreign}"
        logger.error(message)
        environment.runner.quit()
        raise RuntimeError(message)

    PopWorker.fixed_count = len(scenario.worker_slots)
    PopRequester.fixed_count = len(scenario.requester_slots)
    PopProber.fixed_count = len(scenario.probe_slots)
    run_time = getattr(opts, "run_time", None)
    if run_time:
        scenario.submit_deadline = time.time() + float(run_time) - float(opts.pop_drain_seconds)
    total = PopWorker.fixed_count + PopRequester.fixed_count + PopProber.fixed_count
    logger.info(
        "Pop selection run %s: %d workers, %d requesters, %d probes (%d users; pass --users >= %d)",
        run_id,
        PopWorker.fixed_count,
        PopRequester.fixed_count,
        PopProber.fixed_count,
        total,
        total,
    )


def _cancel_unfinished(host: str, concurrency: int, finished: set[str]) -> int:
    """Cancel every request of this run that did not finish, so the next run starts from an empty lane."""
    session = requests.Session()
    cancelled = 0

    def cancel(nonce: str) -> None:
        nonlocal cancelled
        request = ledger.requests[nonce]
        api_key = scenario.keys_by_alias.get(request.owner)
        if api_key is None:
            return
        path = _paths(request.gentype)["status"].format(id=request.request_id)
        try:
            resp = session.delete(f"{host}{path}", headers={"apikey": api_key, "Client-Agent": CLIENT_AGENT}, timeout=60)
        except requests.RequestException:
            return
        if resp.ok:
            cancelled += 1

    pool = gevent.pool.Pool(max(concurrency, 1))
    for nonce in list(ledger.requests):
        if nonce not in finished:
            pool.spawn(cancel, nonce)
    pool.join()
    return cancelled


def _final_statuses(host: str, concurrency: int, nonces: list[str] | None = None) -> int:
    """Read the final status of every tracked request (or of ``nonces``), so the oracles see what each ended with."""
    session = requests.Session()
    observed = 0

    def read(nonce: str) -> None:
        nonlocal observed
        request = ledger.requests[nonce]
        path = _paths(request.gentype)["status"].format(id=request.request_id)
        try:
            resp = session.get(f"{host}{path}", headers={"Client-Agent": CLIENT_AGENT}, timeout=60)
        except requests.RequestException:
            return
        if resp.ok:
            ledger.record_status(status_record(nonce, resp.json(), time.time(), final=True))
            observed += 1

    pool = gevent.pool.Pool(max(concurrency, 1))
    for nonce in list(ledger.requests) if nonces is None else nonces:
        pool.spawn(read, nonce)
    pool.join()
    return observed


@events.test_stop.add_listener
def _judge_pop_selection(environment, **_kwargs) -> None:
    opts = environment.parsed_options
    host = (environment.host or "").rstrip("/") or "http://localhost:7001"
    # Let requests the stopped users left in flight finish on the server before reading the end state.
    gevent.sleep(3.0)
    final_reads = _final_statuses(host, opts.pop_final_concurrency)
    # A shortfall may be a timed-out job the server faulted and has not yet given back; read suspects again.
    for _ in range(2):
        suspects = lost_generation_suspects(ledger)
        if not suspects:
            break
        gevent.sleep(6.0)
        final_reads += _final_statuses(host, opts.pop_final_concurrency, suspects)
    started = time.time()
    finished = {status.nonce for status in ledger.statuses if status.final and (status.done or status.faulted)}
    cancelled = _cancel_unfinished(host, opts.pop_final_concurrency, finished)
    if opts.pop_ledger_dump:
        ledger.dump(opts.pop_ledger_dump)
    violations, coverage = ledger.evaluate()
    summary = {
        "run_id": scenario.run_id,
        "requests": len(ledger.requests),
        "pops": len(ledger.pops),
        "handouts": sum(1 for pop in ledger.pops if pop.job_ids),
        "untracked_handouts": sum(1 for pop in ledger.pops if pop.job_ids and pop.nonce not in ledger.requests),
        "final_status_reads": final_reads,
        "cancelled_after_run": cancelled,
        "evaluation_seconds": round(time.time() - started, 2),
        "lanes": sorted({slot.lane.name for slot in scenario.worker_slots}),
        "evidence_id": uuid.uuid4().hex,
    }
    evidence_writer.report(environment, violations, coverage, summary)
    logger.info("Pop selection oracles: %d violation(s); coverage %s; summary %s", len(violations), coverage, summary)

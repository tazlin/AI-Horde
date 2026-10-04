# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavioural coverage of which queued request a popping worker receives.

Every case goes through the pop endpoints and asserts what a worker observes: the prompt it receives, how many
generations it is handed, or, when it receives nothing, the ``skipped`` reasons it is told. Nothing here depends on
how candidates are queried, so the cases hold the selection behaviour fixed while that implementation changes.

Requests are submitted through the generate endpoints. Where a request shape needs object storage to submit (a source
image, a mask) or an external lookup (LoRAs), the stored row is edited after submission instead; selection reads only
the stored row, so the worker sees the same request either way. Each test starts from an empty active queue because the
image ``skipped`` report counts the whole queue.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from horde.bridge_reference import CAPABILITY_CONTROL_STRENGTH_REGEN_VERSION, CAPABILITY_EXPANDED_REGEN_VERSION
from horde.consts import IMAGE_CONTROL_TYPES, LEGACY_IMAGE_CONTROL_TYPES
from tests.fixture_types import ApiUser, MakeApiUser

IMAGE_MODEL = "Fustercluck"
OTHER_IMAGE_MODEL = "AlbedoBase XL (SDXL)"
TEXT_MODEL = "elinas/chronos-70b-v2"
OTHER_TEXT_MODEL = "koboldcpp/other-model"
CLIENT_AGENT = "aihorde_ci_client:1.0:(test)ci"
REGEN_URL = "https://github.com/Haidra-Org/horde-worker-reGen"
NEW_IMAGE_BRIDGE = f"AI Horde Worker reGen:18.0.0:{REGEN_URL}"
OLD_IMAGE_BRIDGE = f"AI Horde Worker reGen:1.0.0:{REGEN_URL}"
PRE_EXPANDED_IMAGE_BRIDGE = f"AI Horde Worker reGen:{CAPABILITY_EXPANDED_REGEN_VERSION - 1}.0.0:{REGEN_URL}"
PRE_CONTROL_STRENGTH_IMAGE_BRIDGE = f"AI Horde Worker reGen:{CAPABILITY_CONTROL_STRENGTH_REGEN_VERSION - 1}.0.0:{REGEN_URL}"
PRE_EXTRA_SOURCES_IMAGE_BRIDGE = f"AI Horde Worker reGen:4.0.0:{REGEN_URL}"
EXTENDED_CONTROL_TYPE = sorted(set(IMAGE_CONTROL_TYPES) - set(LEGACY_IMAGE_CONTROL_TYPES))[0]
TEXT_BRIDGE = "aihorde_ci_client:1.0:(test)ci"
FAST_WORKER_SPEED = 2_000_000


@pytest.fixture(autouse=True, scope="module")
def _no_rate_limit() -> Iterator[None]:
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture(autouse=True)
def _empty_queue(app) -> Iterator[None]:
    """Deactivate every queued request before and after each test, so only the test's own requests are candidates."""
    from sqlalchemy import text

    from horde.flask import db

    def deactivate_all() -> None:
        with app.app_context():
            db.session.execute(text("UPDATE waiting_prompts SET active = false WHERE active"))
            db.session.commit()
            db.session.remove()

    deactivate_all()
    yield
    deactivate_all()


def _headers(api_key: str) -> dict[str, str]:
    return {"apikey": api_key, "Client-Agent": CLIENT_AGENT}


# --------------------------------------------------------------------------- #
# Requests                                                                    #
# --------------------------------------------------------------------------- #


def submit_image(client, requester: ApiUser, prompt: str = "a robot", **overrides: Any) -> str:
    """Queue an image request and return its id. ``params`` in ``overrides`` merge into the default params."""
    params = {"width": 512, "height": 512, "steps": 8, "n": 1, "sampler_name": "k_euler"}
    params.update(overrides.pop("params", {}))
    body = {
        "prompt": prompt,
        "nsfw": False,
        "r2": False,
        "shared": False,
        "trusted_workers": False,
        "models": [IMAGE_MODEL],
        "params": params,
        **overrides,
    }
    resp = client.post("/api/v2/generate/async", json=body, headers=_headers(requester.api_key))
    assert resp.status_code < 400, resp.get_data(as_text=True)
    return resp.get_json()["id"]


def submit_text(client, requester: ApiUser, prompt: str = "a story", **overrides: Any) -> str:
    """Queue a text request and return its id. ``params`` in ``overrides`` merge into the default params."""
    params = {"max_length": 80, "max_context_length": 1024, "n": 1}
    params.update(overrides.pop("params", {}))
    body = {
        "prompt": prompt,
        "trusted_workers": False,
        "validated_backends": False,
        "models": [TEXT_MODEL],
        "params": params,
        **overrides,
    }
    resp = client.post("/api/v2/generate/text/async", json=body, headers=_headers(requester.api_key))
    assert resp.status_code < 400, resp.get_data(as_text=True)
    return resp.get_json()["id"]


def store(app, request_id: str, *, params: dict[str, Any] | None = None, models: list[str] | None = None, **columns: Any) -> None:
    """Edit a queued request's stored row: columns, keys merged into both params and the job payload, or its models."""
    from horde.classes.base.waiting_prompt import WaitingPrompt, WPModels
    from horde.flask import db

    with app.app_context():
        wp = db.session.get(WaitingPrompt, request_id)
        for column, value in columns.items():
            setattr(wp, column, value)
        if params:
            wp.params = {**wp.params, **params}
            wp.gen_payload = {**wp.gen_payload, **params}
        if models is not None:
            db.session.query(WPModels).filter(WPModels.wp_id == wp.id).delete()
            for model_name in models:
                db.session.add(WPModels(wp_id=wp.id, model=model_name))
        db.session.commit()
        db.session.remove()


def read_request(app, request_id: str) -> dict[str, Any]:
    from horde.classes.base.waiting_prompt import WaitingPrompt
    from horde.flask import db

    with app.app_context():
        wp = db.session.get(WaitingPrompt, request_id)
        state = {"n": wp.n, "jobs": wp.jobs, "active": wp.active, "faulted": wp.faulted}
        db.session.remove()
        return state


# --------------------------------------------------------------------------- #
# Workers                                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class ImageWorkerConfig:
    """Represents what an image worker sends on pop. The defaults accept every request shape these tests create."""

    models: list[str] = field(default_factory=lambda: [IMAGE_MODEL])
    bridge_agent: str = NEW_IMAGE_BRIDGE
    max_pixels: int = 4194304
    nsfw: bool = True
    allow_img2img: bool = True
    allow_painting: bool = True
    allow_unsafe_ipaddr: bool = True
    allow_post_processing: bool = True
    allow_controlnet: bool = True
    allow_extended_controlnet: bool = True
    allow_sdxl_controlnet: bool = True
    allow_lora: bool = True
    extra_slow_worker: bool = False
    limit_max_steps: bool = False
    require_upfront_kudos: bool = False
    blacklist: list[str] = field(default_factory=list)
    priority_usernames: list[str] = field(default_factory=list)
    amount: int = 1

    def payload(self, name: str) -> dict[str, Any]:
        body = {key: value for key, value in vars(self).items() if key not in ("blacklist", "priority_usernames")}
        body["name"] = name
        if self.blacklist:
            body["blacklist"] = self.blacklist
        if self.priority_usernames:
            body["priority_usernames"] = self.priority_usernames
        return body


@dataclass
class TextWorkerConfig:
    """Represents what a text worker sends on pop. The defaults accept every request shape these tests create."""

    models: list[str] = field(default_factory=lambda: [TEXT_MODEL])
    bridge_agent: str = TEXT_BRIDGE
    max_length: int = 512
    max_context_length: int = 4096
    nsfw: bool = True
    allow_unsafe_ipaddr: bool = True
    softprompts: list[str] = field(default_factory=list)
    priority_usernames: list[str] = field(default_factory=list)
    amount: int = 1

    def payload(self, name: str) -> dict[str, Any]:
        body = {key: value for key, value in vars(self).items() if key not in ("softprompts", "priority_usernames")}
        body["name"] = name
        if self.softprompts:
            body["softprompts"] = self.softprompts
        if self.priority_usernames:
            body["priority_usernames"] = self.priority_usernames
        return body


class Worker:
    """A registered worker that pops with a fixed configuration."""

    def __init__(self, client, app, owner: ApiUser, config: ImageWorkerConfig | TextWorkerConfig, *, speed: float | None = None):
        self.client = client
        self.app = app
        self.owner = owner
        self.config = config
        self.name = f"pop-selection-{uuid.uuid4().hex[:10]}"
        self.is_text = isinstance(config, TextWorkerConfig)
        first = self.raw_pop()
        assert first.status_code == 200, first.get_data(as_text=True)
        assert first.get_json()["id"] is None, "a worker is registered against an empty queue"
        self.set(speed=FAST_WORKER_SPEED if speed is None else speed)

    @property
    def url(self) -> str:
        return "/api/v2/generate/text/pop" if self.is_text else "/api/v2/generate/pop"

    def raw_pop(self):
        return self.client.post(self.url, json=self.config.payload(self.name), headers=_headers(self.owner.api_key))

    def pop(self) -> dict[str, Any]:
        resp = self.raw_pop()
        assert resp.status_code == 200, resp.get_data(as_text=True)
        return resp.get_json()

    def set(self, **columns: Any) -> None:
        """Edit the worker's stored row, for state no pop field sets: speed, maintenance, paused."""
        from horde.classes.base.worker import WorkerTemplate
        from horde.flask import db

        with self.app.app_context():
            worker = db.session.query(WorkerTemplate).filter(WorkerTemplate.name == self.name).one()
            for column, value in columns.items():
                setattr(worker, column, value)
            db.session.commit()
            db.session.remove()


def prompt_of(job: dict[str, Any]) -> str | None:
    if job.get("id") is None:
        return None
    return job["payload"]["prompt"]


@pytest.fixture
def requester(make_api_user: MakeApiUser) -> ApiUser:
    return make_api_user(trusted=True, kudos=100000)


@pytest.fixture
def owner(make_api_user: MakeApiUser) -> ApiUser:
    return make_api_user(trusted=True, kudos=100000)


def nonzero(skipped: dict[str, int]) -> dict[str, int]:
    return {reason: count for reason, count in skipped.items() if count}


SOURCE_IMAGE = "https://example.invalid/source.webp"
INPAINTING_MODEL = "Deliberate Inpainting"
SD1_MODEL = "stable_diffusion"
# Flow shift renders only on flux and qwen-image baselines.
FLOW_MODEL = "Flux.1-Schnell fp8 (Compact)"


# --------------------------------------------------------------------------- #
# Image eligibility matrix                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class ImageCase:
    """Represents one request shape, a worker that serves it, and a worker that does not.

    ``skipped`` is the full set of non-zero reasons the refusing worker is told about. ``refusing_speed`` and
    ``refusing_owner_trusted`` cover worker state that the pop payload does not carry.
    """

    case_id: str
    submit: dict[str, Any] = field(default_factory=dict)
    stored: dict[str, Any] = field(default_factory=dict)
    serving: dict[str, Any] = field(default_factory=dict)
    refusing: dict[str, Any] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    refusing_speed: float | None = None
    refusing_owner_trusted: bool = True
    requester_trusted: bool = True
    requester_kudos: int = 100000


IMAGE_CASES = [
    ImageCase("nsfw", submit={"nsfw": True}, refusing={"nsfw": False}, skipped={"nsfw": 1}),
    ImageCase(
        "max_pixels",
        submit={"params": {"width": 1024, "height": 1024}},
        refusing={"max_pixels": 512 * 512},
        skipped={"max_pixels": 1},
    ),
    ImageCase("models", refusing={"models": [OTHER_IMAGE_MODEL]}, skipped={"models": 1}),
    ImageCase(
        "img2img_disallowed",
        stored={"source_image": SOURCE_IMAGE, "source_processing": "img2img"},
        refusing={"allow_img2img": False},
        skipped={"img2img": 1},
    ),
    ImageCase(
        "painting_disallowed",
        stored={"source_image": SOURCE_IMAGE, "source_processing": "inpainting", "models": [INPAINTING_MODEL]},
        serving={"models": [INPAINTING_MODEL]},
        refusing={"models": [INPAINTING_MODEL], "allow_painting": False},
        skipped={"painting": 1},
    ),
    ImageCase(
        "unsafe_ip",
        stored={"safe_ip": False},
        refusing={"allow_unsafe_ipaddr": False},
        skipped={"unsafe_ip": 1},
    ),
    ImageCase(
        "lora_disallowed",
        stored={"params": {"loras": [{"name": "247778"}]}},
        refusing={"allow_lora": False},
        skipped={"lora": 1},
    ),
    # One request is reported twice: the queue-wide count and the candidate check both attribute it to the bridge.
    ImageCase(
        "lora_old_bridge",
        stored={"params": {"loras": [{"name": "247778"}]}},
        refusing={"bridge_agent": OLD_IMAGE_BRIDGE},
        skipped={"bridge_version": 2},
    ),
    ImageCase(
        "textual_inversion_old_bridge",
        stored={"params": {"tis": [{"name": "72437"}]}},
        refusing={"bridge_agent": OLD_IMAGE_BRIDGE},
        skipped={"bridge_version": 2},
    ),
    ImageCase(
        "post_processing_disallowed",
        stored={"params": {"post_processing": ["GFPGAN"]}},
        refusing={"allow_post_processing": False},
        skipped={"post-processing": 1},
    ),
    ImageCase(
        # Control types render only on SD1 and SD2 baselines.
        "controlnet_disallowed",
        submit={"models": [SD1_MODEL]},
        stored={"source_image": SOURCE_IMAGE, "params": {"control_type": "canny"}},
        serving={"models": [SD1_MODEL]},
        refusing={"models": [SD1_MODEL], "allow_controlnet": False},
        skipped={"controlnet": 1},
    ),
    ImageCase(
        "extended_control_type_pre_expanded_bridge",
        submit={"models": [SD1_MODEL]},
        stored={"source_image": SOURCE_IMAGE, "params": {"control_type": EXTENDED_CONTROL_TYPE}},
        serving={"models": [SD1_MODEL]},
        refusing={"models": [SD1_MODEL], "bridge_agent": PRE_EXPANDED_IMAGE_BRIDGE},
        skipped={"bridge_version": 1},
    ),
    ImageCase(
        "extended_control_type_opted_out",
        submit={"models": [SD1_MODEL]},
        stored={"source_image": SOURCE_IMAGE, "params": {"control_type": EXTENDED_CONTROL_TYPE}},
        serving={"models": [SD1_MODEL]},
        refusing={"models": [SD1_MODEL], "allow_extended_controlnet": False},
        skipped={"controlnet": 1},
    ),
    ImageCase(
        "control_strength_pre_field_bridge",
        submit={"models": [SD1_MODEL]},
        stored={"source_image": SOURCE_IMAGE, "params": {"control_type": "canny", "control_strength": 1.0}},
        serving={"models": [SD1_MODEL]},
        refusing={"models": [SD1_MODEL], "bridge_agent": PRE_CONTROL_STRENGTH_IMAGE_BRIDGE},
        skipped={"bridge_version": 1},
    ),
    ImageCase(
        "extended_scheduler_pre_expanded_bridge",
        stored={"params": {"scheduler": "simple"}},
        refusing={"bridge_agent": PRE_EXPANDED_IMAGE_BRIDGE},
        skipped={"bridge_version": 1},
    ),
    ImageCase(
        "sigma_generator_scheduler_pre_expanded_bridge",
        stored={"params": {"scheduler": "align_your_steps"}},
        refusing={"bridge_agent": PRE_EXPANDED_IMAGE_BRIDGE},
        skipped={"bridge_version": 1},
    ),
    ImageCase(
        "solver_option_pre_expanded_bridge",
        stored={"params": {"sampler_eta": 0.5}},
        refusing={"bridge_agent": PRE_EXPANDED_IMAGE_BRIDGE},
        skipped={"bridge_version": 1},
    ),
    ImageCase(
        "flow_shift_pre_expanded_bridge",
        submit={"models": [FLOW_MODEL]},
        stored={"params": {"flow_shift": 3.0}},
        serving={"models": [FLOW_MODEL]},
        refusing={"models": [FLOW_MODEL], "bridge_agent": PRE_EXPANDED_IMAGE_BRIDGE},
        skipped={"bridge_version": 1},
    ),
    # The queue query refuses it and the queue-wide count has no matching reason, so the worker is told nothing.
    ImageCase(
        "extra_source_images_old_bridge",
        stored={"extra_source_images": {"esi": [{"image": SOURCE_IMAGE, "strength": 1.0}]}},
        refusing={"bridge_agent": PRE_EXTRA_SOURCES_IMAGE_BRIDGE},
        skipped={},
    ),
    ImageCase(
        "transparent_old_bridge",
        stored={"params": {"transparent": True}},
        refusing={"bridge_agent": OLD_IMAGE_BRIDGE},
        skipped={"bridge_version": 1},
    ),
    ImageCase(
        "slow_worker",
        stored={"slow_workers": False},
        refusing_speed=100,
        skipped={"performance": 1},
    ),
    ImageCase(
        "extra_slow_worker",
        submit={"extra_slow_workers": False},
        refusing={"extra_slow_worker": True},
        skipped={"performance": 1},
    ),
    ImageCase(
        "trusted_workers",
        submit={"trusted_workers": True},
        refusing_owner_trusted=False,
        skipped={"untrusted": 1},
    ),
    ImageCase(
        "word_blacklist",
        submit={"prompt": "a zebra in a field"},
        refusing={"blacklist": ["zebra"]},
        skipped={"blacklist": 1},
    ),
    ImageCase(
        # Stored rather than submitted: submitting this many steps needs the requester's kudos folded first.
        "limit_max_steps",
        stored={"params": {"steps": 150}},
        refusing={"limit_max_steps": True},
        skipped={"step_count": 1},
    ),
    ImageCase(
        "require_upfront_kudos",
        refusing={"require_upfront_kudos": True},
        skipped={"kudos": 1},
        requester_trusted=False,
        requester_kudos=0,
    ),
]


@pytest.mark.parametrize("case", IMAGE_CASES, ids=lambda case: case.case_id)
class TestImageEligibility:
    def _queue(self, client, app, make_api_user, case: ImageCase) -> str:
        requester = make_api_user(trusted=case.requester_trusted, kudos=case.requester_kudos)
        submit = dict(case.submit)
        prompt = submit.pop("prompt", f"request for {case.case_id}")
        request_id = submit_image(client, requester, prompt, **submit)
        if case.stored:
            store(app, request_id, **case.stored)
        return request_id

    def test_a_capable_worker_receives_the_request(self, client, app, make_api_user, owner, case):
        worker = Worker(client, app, owner, ImageWorkerConfig(**case.serving))
        self._queue(client, app, make_api_user, case)

        job = worker.pop()

        assert job["id"] is not None, job.get("skipped")

    def test_an_incapable_worker_is_told_why_it_received_nothing(self, client, app, make_api_user, case):
        refusing_owner = make_api_user(trusted=case.refusing_owner_trusted, kudos=100000)
        worker = Worker(client, app, refusing_owner, ImageWorkerConfig(**case.refusing), speed=case.refusing_speed)
        self._queue(client, app, make_api_user, case)

        job = worker.pop()

        assert job["id"] is None
        assert nonzero(job["skipped"]) == case.skipped


class TestImageEligibilityEdges:
    def test_a_request_exactly_at_the_pixel_limit_is_served(self, client, app, requester, owner):
        worker = Worker(client, app, owner, ImageWorkerConfig(max_pixels=512 * 512))
        submit_image(client, requester)

        assert worker.pop()["id"] is not None

    def test_a_request_naming_no_model_is_served_to_a_general_worker(self, client, app, requester, owner):
        worker = Worker(client, app, owner, ImageWorkerConfig())
        request_id = submit_image(client, requester)
        store(app, request_id, models=[])

        assert worker.pop()["id"] is not None

    def test_a_request_naming_several_models_is_served_once(self, client, app, requester, owner):
        worker = Worker(client, app, owner, ImageWorkerConfig(models=[IMAGE_MODEL, OTHER_IMAGE_MODEL], amount=4))
        request_id = submit_image(client, requester, models=[IMAGE_MODEL, OTHER_IMAGE_MODEL])

        job = worker.pop()

        assert len(job["ids"]) == 1
        assert read_request(app, request_id)["n"] == 0

    def test_an_inpainting_only_worker_is_not_given_a_plain_request(self, client, app, requester, owner):
        worker = Worker(client, app, owner, ImageWorkerConfig(models=[INPAINTING_MODEL]))
        request_id = submit_image(client, requester)
        store(app, request_id, models=[INPAINTING_MODEL])

        job = worker.pop()

        assert job["id"] is None
        assert nonzero(job["skipped"]) == {}

    def test_an_untrusted_worker_is_not_given_an_untrusted_requester_from_an_unsafe_address(
        self,
        client,
        app,
        make_api_user,
    ):
        worker = Worker(client, app, make_api_user(kudos=100), ImageWorkerConfig())
        request_id = submit_image(client, make_api_user(kudos=100000))
        store(app, request_id, safe_ip=False)

        job = worker.pop()

        assert job["id"] is None
        assert nonzero(job["skipped"]) == {}

    def test_an_unsupported_sampler_is_refused_to_an_old_bridge(self, client, app, requester, owner):
        """The refusal is counted once. The candidate query refuses the sampler, so only the queue-wide count reports it,
        where the worker's own check used to count it a second time."""
        worker = Worker(client, app, owner, ImageWorkerConfig(bridge_agent=OLD_IMAGE_BRIDGE))
        request_id = submit_image(client, requester)
        store(app, request_id, params={"sampler_name": "lcm", "karras": True})

        job = worker.pop()

        assert job["id"] is None
        assert nonzero(job["skipped"]) == {"bridge_version": 1}

    def test_transparency_is_refused_to_a_worker_without_sdxl_controlnet(self, client, app, requester, owner):
        worker = Worker(client, app, owner, ImageWorkerConfig(allow_sdxl_controlnet=False))
        request_id = submit_image(client, requester)
        store(app, request_id, params={"transparent": True})

        job = worker.pop()

        assert job["id"] is None
        assert nonzero(job["skipped"]) == {}


@pytest.mark.parametrize(
    ("column", "value"),
    [("active", False), ("faulted", True), ("n", 0), ("expiry", "past")],
)
@pytest.mark.parametrize("gentype", ["image", "text"])
def test_a_request_that_is_not_waiting_is_never_served(client, app, requester, owner, column, value, gentype):
    from datetime import datetime, timedelta

    config = ImageWorkerConfig() if gentype == "image" else TextWorkerConfig()
    worker = Worker(client, app, owner, config)
    request_id = submit_image(client, requester) if gentype == "image" else submit_text(client, requester)
    store(app, request_id, **{column: datetime.utcnow() - timedelta(minutes=1) if value == "past" else value})

    job = worker.pop()

    assert job["id"] is None
    assert nonzero(job.get("skipped", {})) == {}


# --------------------------------------------------------------------------- #
# Text eligibility matrix                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class TextCase:
    case_id: str
    submit: dict[str, Any] = field(default_factory=dict)
    stored: dict[str, Any] = field(default_factory=dict)
    serving: dict[str, Any] = field(default_factory=dict)
    refusing: dict[str, Any] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    refusing_speed: float | None = None
    refusing_owner_trusted: bool = True


TEXT_CASES = [
    TextCase("max_length", submit={"params": {"max_length": 300}}, refusing={"max_length": 200}),
    TextCase(
        "max_context_length",
        submit={"params": {"max_context_length": 4096}},
        serving={"max_context_length": 8192},
        refusing={"max_context_length": 2048},
    ),
    TextCase("nsfw", stored={"nsfw": True}, refusing={"nsfw": False}),
    TextCase("models", refusing={"models": [OTHER_TEXT_MODEL]}),
    TextCase("slow_worker", stored={"slow_workers": False}, refusing_speed=0.01),
    TextCase(
        "validated_backends",
        stored={"validated_backends": True},
        serving={"bridge_agent": "AI Horde Worker:24:https://github.com/db0/AI-Horde-Worker"},
    ),
    TextCase(
        "softprompt",
        stored={"softprompt": "a-softprompt"},
        serving={"softprompts": ["a-softprompt"]},
        skipped={"matching_softprompt": 1},
    ),
    TextCase("trusted_workers", submit={"trusted_workers": True}, refusing_owner_trusted=False, skipped={"untrusted": 1}),
]


@pytest.mark.parametrize("case", TEXT_CASES, ids=lambda case: case.case_id)
class TestTextEligibility:
    """Text workers are told only about rejections made after the queue query; the query's own filters are silent."""

    def _queue(self, client, app, requester, case: TextCase) -> str:
        request_id = submit_text(client, requester, f"request for {case.case_id}", **case.submit)
        if case.stored:
            store(app, request_id, **case.stored)
        return request_id

    def test_a_capable_worker_receives_the_request(self, client, app, requester, owner, case):
        worker = Worker(client, app, owner, TextWorkerConfig(**case.serving))
        self._queue(client, app, requester, case)

        job = worker.pop()

        assert job["id"] is not None, job.get("skipped")

    def test_an_incapable_worker_is_told_why_it_received_nothing(self, client, app, make_api_user, requester, case):
        refusing_owner = make_api_user(trusted=case.refusing_owner_trusted, kudos=100000)
        worker = Worker(client, app, refusing_owner, TextWorkerConfig(**case.refusing), speed=case.refusing_speed)
        self._queue(client, app, requester, case)

        job = worker.pop()

        assert job["id"] is None
        assert nonzero(job["skipped"]) == case.skipped


# --------------------------------------------------------------------------- #
# Selection order and priority                                                #
# --------------------------------------------------------------------------- #


def _config(gentype: str, **fields: Any) -> ImageWorkerConfig | TextWorkerConfig:
    return ImageWorkerConfig(**fields) if gentype == "image" else TextWorkerConfig(**fields)


def _submit(client, gentype: str, requester: ApiUser, prompt: str, **overrides: Any) -> str:
    if gentype == "image":
        return submit_image(client, requester, prompt, **overrides)
    return submit_text(client, requester, prompt, **overrides)


GENTYPES = ["image", "text"]


@pytest.mark.parametrize("gentype", GENTYPES)
class TestOrder:
    def test_higher_queue_priority_is_served_first(self, client, app, requester, owner, gentype):
        worker = Worker(client, app, owner, _config(gentype))
        low = _submit(client, gentype, requester, "low priority")
        high = _submit(client, gentype, requester, "high priority")
        store(app, low, extra_priority=10)
        store(app, high, extra_priority=1000)

        assert prompt_of(worker.pop()) == "high priority"
        assert prompt_of(worker.pop()) == "low priority"

    def test_equal_priority_is_served_oldest_first(self, client, app, requester, owner, gentype):
        worker = Worker(client, app, owner, _config(gentype))
        first = _submit(client, gentype, requester, "older")
        second = _submit(client, gentype, requester, "newer")
        store(app, first, extra_priority=500)
        store(app, second, extra_priority=500)

        assert prompt_of(worker.pop()) == "older"
        assert prompt_of(worker.pop()) == "newer"

    def test_a_worker_drains_a_queue_in_order_and_then_receives_nothing(self, client, app, requester, owner, gentype):
        worker = Worker(client, app, owner, _config(gentype))
        for index in range(5):
            request_id = _submit(client, gentype, requester, f"request {index}")
            store(app, request_id, extra_priority=100 - index)

        received = [prompt_of(worker.pop()) for _ in range(6)]

        assert received == [f"request {index}" for index in range(5)] + [None]


@pytest.mark.parametrize("gentype", GENTYPES)
class TestPriorityUsers:
    """The owner's requests, and those of the worker's priority users, are served ahead of the general queue."""

    def test_owner_request_is_served_before_higher_priority_strangers(self, client, app, make_api_user, owner, gentype):
        stranger = make_api_user(trusted=True, kudos=1000)
        worker = Worker(client, app, owner, _config(gentype))
        for index in range(3):
            request_id = _submit(client, gentype, stranger, f"stranger {index}")
            store(app, request_id, extra_priority=10**9)
        own = _submit(client, gentype, owner, "owner request")
        store(app, own, extra_priority=0)

        assert prompt_of(worker.pop()) == "owner request"

    def test_priority_user_request_is_served_before_higher_priority_strangers(
        self,
        client,
        app,
        make_api_user,
        owner,
        gentype,
    ):
        friend = make_api_user(trusted=True, kudos=1000)
        stranger = make_api_user(trusted=True, kudos=1000)
        worker = Worker(client, app, owner, _config(gentype, priority_usernames=[friend.alias]))
        request_id = _submit(client, gentype, stranger, "stranger")
        store(app, request_id, extra_priority=10**9)
        friend_request = _submit(client, gentype, friend, "friend request")
        store(app, friend_request, extra_priority=0)

        assert prompt_of(worker.pop()) == "friend request"

    def test_among_priority_requests_queue_order_applies(self, client, app, make_api_user, owner, gentype):
        friend = make_api_user(trusted=True, kudos=1000)
        worker = Worker(client, app, owner, _config(gentype, priority_usernames=[friend.alias]))
        own = _submit(client, gentype, owner, "owner request")
        friend_request = _submit(client, gentype, friend, "friend request")
        store(app, own, extra_priority=1)
        store(app, friend_request, extra_priority=2)

        assert prompt_of(worker.pop()) == "friend request"
        assert prompt_of(worker.pop()) == "owner request"

    def test_a_priority_username_without_an_id_is_rejected(self, client, app, owner, gentype):
        worker = Worker(client, app, owner, _config(gentype))
        worker.config.priority_usernames = ["no-id-here"]

        resp = worker.raw_pop()

        assert resp.status_code == 400
        assert resp.get_json()["rc"] == "InvalidPriorityUsername"

    def test_an_unknown_priority_user_is_ignored(self, client, app, requester, owner, gentype):
        worker = Worker(client, app, owner, _config(gentype, priority_usernames=["nobody#999999999"]))
        _submit(client, gentype, requester, "general request")

        assert prompt_of(worker.pop()) == "general request"


# --------------------------------------------------------------------------- #
# Maintenance and paused workers                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("gentype", GENTYPES)
class TestMaintenance:
    def test_a_worker_in_maintenance_serves_its_owner(self, client, app, requester, owner, gentype):
        worker = Worker(client, app, owner, _config(gentype))
        worker.set(maintenance=True)
        _submit(client, gentype, requester, "stranger request")
        own = _submit(client, gentype, owner, "owner request")
        store(app, own, extra_priority=0)

        assert prompt_of(worker.pop()) == "owner request"

    def test_a_worker_in_maintenance_does_not_serve_its_priority_users(
        self,
        client,
        app,
        make_api_user,
        owner,
        gentype,
    ):
        friend = make_api_user(trusted=True, kudos=1000)
        worker = Worker(client, app, owner, _config(gentype, priority_usernames=[friend.alias]))
        worker.set(maintenance=True)
        _submit(client, gentype, friend, "friend request")

        resp = worker.raw_pop()

        assert resp.status_code == 403
        assert resp.get_json()["rc"] == "WorkerMaintenance"

    def test_a_worker_in_maintenance_with_no_owner_request_is_told_it_is_in_maintenance(
        self,
        client,
        app,
        requester,
        owner,
        gentype,
    ):
        worker = Worker(client, app, owner, _config(gentype))
        worker.set(maintenance=True)
        _submit(client, gentype, requester, "stranger request")

        resp = worker.raw_pop()

        assert resp.status_code == 403
        assert resp.get_json()["rc"] == "WorkerMaintenance"


@pytest.mark.parametrize("gentype", GENTYPES)
class TestPausedWorker:
    def test_a_paused_worker_gets_a_fake_job_that_leaves_the_request_queued(self, client, app, requester, owner, gentype):
        worker = Worker(client, app, owner, _config(gentype))
        worker.set(paused=True)
        request_id = _submit(client, gentype, requester, "stranger request")

        job = worker.pop()

        assert prompt_of(job) == "stranger request"
        assert read_request(app, request_id)["n"] == 1

    def test_a_worker_given_a_fake_job_never_receives_that_request_again(self, client, app, requester, owner, gentype):
        worker = Worker(client, app, owner, _config(gentype))
        worker.set(paused=True)
        _submit(client, gentype, requester, "stranger request")
        worker.pop()
        worker.set(paused=False)

        job = worker.pop()

        assert job["id"] is None
        assert nonzero(job["skipped"]) == {}

    def test_a_paused_worker_serves_its_owner_for_real(self, client, app, owner, gentype):
        worker = Worker(client, app, owner, _config(gentype))
        worker.set(paused=True)
        request_id = _submit(client, gentype, owner, "owner request")

        assert prompt_of(worker.pop()) == "owner request"
        assert read_request(app, request_id)["n"] == 0


# --------------------------------------------------------------------------- #
# Worker targeting                                                            #
# --------------------------------------------------------------------------- #


def _worker_id(client, worker: Worker) -> str:
    resp = client.get(f"/api/v2/workers/name/{worker.name}", headers=_headers(worker.owner.api_key))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["id"]


@pytest.mark.parametrize("gentype", GENTYPES)
class TestTargeting:
    def test_a_request_for_this_worker_is_served(self, client, app, requester, owner, gentype):
        worker = Worker(client, app, owner, _config(gentype))
        _submit(client, gentype, requester, "targeted", workers=[_worker_id(client, worker)])

        assert prompt_of(worker.pop()) == "targeted"

    def test_a_request_for_another_worker_is_not_served(self, client, app, make_api_user, requester, owner, gentype):
        worker = Worker(client, app, owner, _config(gentype))
        other = Worker(client, app, make_api_user(trusted=True), _config(gentype))
        _submit(client, gentype, requester, "targeted elsewhere", workers=[_worker_id(client, other)])

        job = worker.pop()

        assert job["id"] is None
        assert nonzero(job["skipped"]) == ({"worker_id": 1} if gentype == "image" else {})

    def test_a_request_excluding_this_worker_is_not_served(self, client, app, make_api_user, requester, owner, gentype):
        worker = Worker(client, app, owner, _config(gentype))
        other = Worker(client, app, make_api_user(trusted=True), _config(gentype))
        workers = [_worker_id(client, worker), _worker_id(client, other)]
        _submit(client, gentype, requester, "excludes me", workers=workers, worker_blacklist=True)

        job = worker.pop()

        assert job["id"] is None
        assert nonzero(job["skipped"]) == ({"worker_id": 1} if gentype == "image" else {})

    def test_a_request_excluding_another_worker_is_served(self, client, app, make_api_user, requester, owner, gentype):
        worker = Worker(client, app, owner, _config(gentype))
        other = Worker(client, app, make_api_user(trusted=True), _config(gentype))
        _submit(client, gentype, requester, "excludes other", workers=[_worker_id(client, other)], worker_blacklist=True)

        assert prompt_of(worker.pop()) == "excludes other"


class TestMatchedTargetingRequirement:
    """Under ``HORDE_REQUIRE_MATCHED_TARGETING`` an image worker serves a request targeting it only for priority users."""

    def test_a_stranger_request_for_this_image_worker_is_not_served(self, client, app, requester, owner, monkeypatch):
        worker = Worker(client, app, owner, ImageWorkerConfig())
        submit_image(client, requester, "targeted", workers=[_worker_id(client, worker)])
        monkeypatch.setenv("HORDE_REQUIRE_MATCHED_TARGETING", "1")

        job = worker.pop()

        assert job["id"] is None
        assert nonzero(job["skipped"]) == {"worker_id": 1}

    def test_an_owner_request_for_this_image_worker_is_served(self, client, app, owner, monkeypatch):
        worker = Worker(client, app, owner, ImageWorkerConfig())
        submit_image(client, owner, "targeted", workers=[_worker_id(client, worker)])
        monkeypatch.setenv("HORDE_REQUIRE_MATCHED_TARGETING", "1")

        assert prompt_of(worker.pop()) == "targeted"

    def test_a_request_excluding_another_image_worker_is_still_served(self, client, app, make_api_user, requester, owner, monkeypatch):
        worker = Worker(client, app, owner, ImageWorkerConfig())
        other = Worker(client, app, make_api_user(trusted=True), ImageWorkerConfig())
        submit_image(client, requester, "excludes other", workers=[_worker_id(client, other)], worker_blacklist=True)
        monkeypatch.setenv("HORDE_REQUIRE_MATCHED_TARGETING", "1")

        assert prompt_of(worker.pop()) == "excludes other"

    def test_text_targeting_ignores_the_requirement(self, client, app, requester, owner, monkeypatch):
        worker = Worker(client, app, owner, TextWorkerConfig())
        submit_text(client, requester, "targeted", workers=[_worker_id(client, worker)])
        monkeypatch.setenv("HORDE_REQUIRE_MATCHED_TARGETING", "1")

        assert prompt_of(worker.pop()) == "targeted"


# --------------------------------------------------------------------------- #
# Batching and the count of generations still wanted                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("gentype", GENTYPES)
class TestBatching:
    def test_a_worker_takes_up_to_its_amount_and_the_rest_stays_queued(self, client, app, requester, owner, gentype):
        worker = Worker(client, app, owner, _config(gentype, amount=3))
        request_id = _submit(client, gentype, requester, "four wanted", params={"n": 4})

        first = worker.pop()
        second = worker.pop()
        third = worker.pop()

        assert (len(first["ids"]), len(second["ids"]), third["id"]) == (3, 1, None)
        assert read_request(app, request_id)["n"] == 0

    def test_a_request_that_disables_batching_is_handed_out_one_at_a_time(self, client, app, requester, owner, gentype):
        worker = Worker(client, app, owner, _config(gentype, amount=3))
        request_id = _submit(client, gentype, requester, "unbatched", params={"n": 2})
        store(app, request_id, disable_batching=True)

        assert len(worker.pop()["ids"]) == 1
        assert len(worker.pop()["ids"]) == 1
        assert worker.pop()["id"] is None


def test_a_text_request_cannot_disable_batching_through_the_api(client, app, requester, owner):
    """The text generate API documents ``disable_batching`` but does not store it on the request."""
    worker = Worker(client, app, owner, TextWorkerConfig(amount=3))
    submit_text(client, requester, "asks for no batching", params={"n": 2}, disable_batching=True)

    assert len(worker.pop()["ids"]) == 2


def test_a_text_worker_cannot_refuse_requests_from_unsafe_addresses(client, app, requester, owner):
    """The text pop does not pass ``allow_unsafe_ipaddr`` to the worker, which keeps its default of allowing them."""
    worker = Worker(client, app, owner, TextWorkerConfig())
    request_id = submit_text(client, requester, "from an unsafe address")
    store(app, request_id, safe_ip=False)
    worker.config.allow_unsafe_ipaddr = False

    assert prompt_of(worker.pop()) == "from an unsafe address"

    def test_two_workers_split_a_request_without_exceeding_it(self, client, app, make_api_user, requester, owner, gentype):
        first_worker = Worker(client, app, owner, _config(gentype, amount=2))
        second_worker = Worker(client, app, make_api_user(trusted=True), _config(gentype, amount=2))
        request_id = _submit(client, gentype, requester, "three wanted", params={"n": 3})

        handed_out = len(first_worker.pop()["ids"]) + len(second_worker.pop()["ids"])

        assert handed_out == 3
        assert first_worker.pop()["id"] is None
        assert read_request(app, request_id)["n"] == 0


def test_an_image_worker_takes_fewer_large_images_than_its_amount(client, app, requester, owner):
    worker = Worker(client, app, owner, ImageWorkerConfig(amount=4, max_pixels=1024 * 1024))
    request_id = submit_image(client, requester, "large", params={"n": 4, "width": 1024, "height": 1024})

    job = worker.pop()

    assert 1 <= len(job["ids"]) < 4
    assert read_request(app, request_id)["n"] == 4 - len(job["ids"])


@pytest.mark.parametrize("gentype", GENTYPES)
class TestReturnedGenerations:
    def test_a_faulted_generation_is_handed_out_again(self, client, app, requester, owner, gentype):
        worker = Worker(client, app, owner, _config(gentype))
        request_id = _submit(client, gentype, requester, "will fault")
        job = worker.pop()
        submit_url = "/api/v2/generate/text/submit" if gentype == "text" else "/api/v2/generate/submit"

        body = {"id": job["id"], "generation": "", "state": "faulted"}
        if gentype == "image":
            body["seed"] = 0
        resp = client.post(submit_url, json=body, headers=_headers(owner.api_key))

        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert read_request(app, request_id)["n"] == 1
        assert prompt_of(worker.pop()) == "will fault"

    def test_a_timed_out_generation_is_handed_out_again(self, client, app, requester, owner, gentype):
        from datetime import datetime, timedelta

        from horde.classes.base.processing_generation import ProcessingGeneration
        from horde.database.threads import check_waiting_prompts
        from horde.flask import db

        worker = Worker(client, app, owner, _config(gentype))
        request_id = _submit(client, gentype, requester, "will time out")
        job = worker.pop()
        with app.app_context():
            procgen = db.session.get(ProcessingGeneration, job["id"])
            procgen.start_time = datetime.utcnow() - timedelta(hours=1)
            db.session.commit()
            db.session.remove()

        check_waiting_prompts()

        assert read_request(app, request_id)["n"] == 1
        assert prompt_of(worker.pop()) == "will time out"


# --------------------------------------------------------------------------- #
# Deep and pathological queues                                                #
# --------------------------------------------------------------------------- #

# Deep enough to span several pages of any candidate read, and more than the cap on one candidate read.
DEEP_QUEUE = 250


def clone_request(app, template_id: str, count: int, *, prompt_prefix: str, extra_priority: int) -> list[str]:
    """Insert ``count`` copies of a queued request, with their own ids, prompts and model rows.

    Copying in SQL builds a deep queue quickly and is not held to the requester's limit on parallel requests.
    """
    from sqlalchemy import text

    from horde.flask import db

    with app.app_context():
        columns = [
            row[0]
            for row in db.session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'waiting_prompts' AND table_schema = current_schema()",
                ),
            )
        ]
        # The template is usually deactivated so that only its copies are queued.
        replaced = {"id": "gen_random_uuid()", "prompt": ":prefix || copy_index", "extra_priority": ":priority", "active": "true"}
        select_list = ", ".join(replaced.get(column, f"template.{column}") for column in columns)
        new_ids = [
            row[0]
            for row in db.session.execute(
                text(
                    f"INSERT INTO waiting_prompts ({', '.join(columns)}) SELECT {select_list} "
                    "FROM waiting_prompts template, generate_series(0, :count - 1) copy_index WHERE template.id = :template RETURNING id",
                ),
                {"prefix": prompt_prefix, "priority": extra_priority, "count": count, "template": template_id},
            )
        ]
        db.session.execute(
            text(
                "INSERT INTO wp_models (wp_id, model) "
                "SELECT copy_id, model FROM unnest(CAST(:ids AS uuid[])) copy_id, wp_models WHERE wp_id = :template",
            ),
            {"ids": [str(new_id) for new_id in new_ids], "template": template_id},
        )
        db.session.commit()
        db.session.remove()
    return [str(new_id) for new_id in new_ids]


def _queue_unservable(app, client, gentype: str, requester: ApiUser, count: int, extra_priority: int) -> list[str]:
    """Queue requests that a worker configured by ``_blocked_config`` refuses only after reading them as candidates.

    The refusal must come from the worker's own check rather than the candidate query, or the requests never take
    places among the candidates. Image requests exceed the worker's step limit; text requests ask for trusted workers,
    which an untrusted owner's text worker refuses after the query.
    """
    if gentype == "image":
        template = submit_image(client, requester, "over the step limit template")
        store(app, template, active=False, params={"steps": 150})
    else:
        template = submit_text(client, requester, "needs trusted template", trusted_workers=True)
        store(app, template, active=False)
    _allow_parallel_requests(app, requester)
    return clone_request(app, template, count, prompt_prefix="unservable ", extra_priority=extra_priority)


def _blocked_config(gentype: str, **fields: Any) -> ImageWorkerConfig | TextWorkerConfig:
    """Return a worker configuration that refuses the requests ``_queue_unservable`` queues. Its owner must be untrusted."""
    if gentype == "image":
        return ImageWorkerConfig(limit_max_steps=True, **fields)
    return TextWorkerConfig(**fields)


def _allow_parallel_requests(app, user: ApiUser) -> None:
    """Lift a user's limit on parallel requests, so they can still submit with a cloned queue outstanding."""
    from horde.classes.base.user import User
    from horde.flask import db

    with app.app_context():
        db.session.get(User, user.id).concurrency = 10**6
        db.session.commit()
        db.session.remove()


@pytest.mark.parametrize("gentype", GENTYPES)
class TestDeepPriorityQueues:
    """A servable priority request is found however many unservable priority requests are ahead of it."""

    def test_owner_request_behind_unservable_owner_requests_beats_the_general_queue(
        self,
        client,
        app,
        make_api_user,
        gentype,
    ):
        owner = make_api_user(kudos=100000)
        stranger = make_api_user(trusted=True, kudos=1000)
        worker = Worker(client, app, owner, _blocked_config(gentype))
        _queue_unservable(app, client, gentype, owner, DEEP_QUEUE, extra_priority=10**9)
        stranger_request = _submit(client, gentype, stranger, "stranger request")
        store(app, stranger_request, extra_priority=10**8)
        own = _submit(client, gentype, owner, "servable owner request")
        store(app, own, extra_priority=0)

        assert prompt_of(worker.pop()) == "servable owner request"

    def test_priority_user_request_behind_unservable_ones_beats_the_general_queue(
        self,
        client,
        app,
        make_api_user,
        gentype,
    ):
        owner = make_api_user(kudos=100000)
        friend = make_api_user(kudos=100000)
        stranger = make_api_user(trusted=True, kudos=1000)
        worker = Worker(client, app, owner, _blocked_config(gentype, priority_usernames=[friend.alias]))
        _queue_unservable(app, client, gentype, friend, DEEP_QUEUE, extra_priority=10**9)
        stranger_request = _submit(client, gentype, stranger, "stranger request")
        store(app, stranger_request, extra_priority=10**8)
        friend_request = _submit(client, gentype, friend, "servable friend request")
        store(app, friend_request, extra_priority=0)

        assert prompt_of(worker.pop()) == "servable friend request"

    def test_with_no_servable_priority_request_the_general_queue_is_served(self, client, app, make_api_user, gentype):
        owner = make_api_user(kudos=100000)
        stranger = make_api_user(trusted=True, kudos=1000)
        worker = Worker(client, app, owner, _blocked_config(gentype))
        _queue_unservable(app, client, gentype, owner, DEEP_QUEUE, extra_priority=10**9)
        stranger_request = _submit(client, gentype, stranger, "stranger request")
        store(app, stranger_request, extra_priority=0)

        assert prompt_of(worker.pop()) == "stranger request"

    def test_a_servable_request_behind_a_deep_unservable_general_queue_is_found(self, client, app, make_api_user, gentype):
        owner = make_api_user(kudos=100000)
        stranger = make_api_user(kudos=1000)
        worker = Worker(client, app, owner, _blocked_config(gentype))
        _queue_unservable(app, client, gentype, stranger, DEEP_QUEUE, extra_priority=10**9)
        last = _submit(client, gentype, stranger, "servable last")
        store(app, last, extra_priority=0)

        assert prompt_of(worker.pop()) == "servable last"

    def test_each_unservable_request_is_reported_once(self, client, app, make_api_user, gentype):
        """A priority request is also in the general queue, and the worker is told about it once.

        The worker's word blacklist is the image reason because the image pop replaces the untrusted count with a
        queue-wide count; text workers take no word blacklist, so text reports the untrusted check.
        """
        owner = make_api_user(kudos=100000)
        config = _config(gentype, blacklist=["zebra"]) if gentype == "image" else _config(gentype)
        worker = Worker(client, app, owner, config)
        template = _submit(client, gentype, owner, "a zebra template", trusted_workers=gentype == "text")
        store(app, template, active=False)
        clone_request(app, template, DEEP_QUEUE, prompt_prefix="a zebra ", extra_priority=1000)

        job = worker.pop()

        assert job["id"] is None
        reason = "blacklist" if gentype == "image" else "untrusted"
        assert nonzero(job["skipped"]) == {reason: DEEP_QUEUE}


@pytest.mark.parametrize("gentype", GENTYPES)
def test_a_priority_request_that_excludes_this_worker_is_not_served(client, app, make_api_user, owner, gentype):
    stranger = make_api_user(trusted=True, kudos=1000)
    worker = Worker(client, app, owner, _config(gentype))
    _submit(client, gentype, owner, "owner excludes own worker", workers=[_worker_id(client, worker)], worker_blacklist=True)
    _submit(client, gentype, stranger, "stranger request")

    assert prompt_of(worker.pop()) == "stranger request"


@pytest.mark.parametrize("gentype", GENTYPES)
def test_raising_every_queued_priority_keeps_the_order(client, app, requester, owner, gentype):
    from horde.database.threads import increment_extra_priority

    worker = Worker(client, app, owner, _config(gentype))
    for index, priority in enumerate((300, 200, 100)):
        request_id = _submit(client, gentype, requester, f"request {index}")
        store(app, request_id, extra_priority=priority)

    increment_extra_priority()

    assert [prompt_of(worker.pop()) for _ in range(3)] == ["request 0", "request 1", "request 2"]


# --------------------------------------------------------------------------- #
# Concurrent pops                                                             #
# --------------------------------------------------------------------------- #

CONCURRENT_WORKERS = 4
CONCURRENT_REQUESTS = 12
JOBS_PER_REQUEST = 2


def _pop_concurrently(app, client, gentype: str, workers: list[Worker], pops_each: int) -> list[dict[str, Any]]:
    """Pop from every worker at once on its own thread, ``pops_each`` times each, and return every response."""
    import threading

    from horde.flask import db

    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(workers))

    def run(worker: Worker) -> None:
        try:
            with app.app_context():
                thread_client = app.test_client()
                barrier.wait()
                for _ in range(pops_each):
                    resp = thread_client.post(worker.url, json=worker.config.payload(worker.name), headers=_headers(worker.owner.api_key))
                    db.session.remove()
                    with lock:
                        results.append({"status": resp.status_code, **(resp.get_json() or {})})
        except BaseException as err:  # noqa: BLE001 - re-raised on the test thread below
            with lock:
                errors.append(err)

    threads = [threading.Thread(target=run, args=(worker,)) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]
    return results


def _generations_per_request(app, request_ids: list[str]) -> dict[str, int]:
    from horde.classes.base.processing_generation import ProcessingGeneration
    from horde.flask import db

    with app.app_context():
        counts = {
            str(request_id): db.session.query(ProcessingGeneration)
            .filter(ProcessingGeneration.wp_id == request_id, ProcessingGeneration.fake.is_(False))
            .count()
            for request_id in request_ids
        }
        db.session.remove()
        return counts


@pytest.mark.parametrize("gentype", GENTYPES)
class TestConcurrentPops:
    def _setup(self, client, app, make_api_user, requester, gentype) -> tuple[list[Worker], list[str]]:
        workers = [Worker(client, app, make_api_user(trusted=True), _config(gentype, amount=1)) for _ in range(CONCURRENT_WORKERS)]
        request_ids = [
            _submit(client, gentype, requester, f"shared {index}", params={"n": JOBS_PER_REQUEST}) for index in range(CONCURRENT_REQUESTS)
        ]
        return workers, request_ids

    def test_no_request_is_handed_out_more_often_than_it_asked_for(self, client, app, make_api_user, requester, gentype):
        workers, request_ids = self._setup(client, app, make_api_user, requester, gentype)
        total_jobs = CONCURRENT_REQUESTS * JOBS_PER_REQUEST

        results = _pop_concurrently(app, client, gentype, workers, pops_each=total_jobs // CONCURRENT_WORKERS + 2)

        assert all(result["status"] == 200 for result in results), results
        handed_out = _generations_per_request(app, request_ids)
        assert all(count <= JOBS_PER_REQUEST for count in handed_out.values()), handed_out
        assert sum(handed_out.values()) == sum(len(result.get("ids") or []) for result in results)
        assert all(read_request(app, request_id)["n"] == JOBS_PER_REQUEST - handed_out[str(request_id)] for request_id in request_ids)

    def test_no_worker_is_turned_away_while_work_remains(self, client, app, make_api_user, requester, gentype):
        workers, request_ids = self._setup(client, app, make_api_user, requester, gentype)
        total_jobs = CONCURRENT_REQUESTS * JOBS_PER_REQUEST

        results = _pop_concurrently(app, client, gentype, workers, pops_each=total_jobs // CONCURRENT_WORKERS)

        empty = [result for result in results if not result.get("ids")]
        assert empty == [], f"{len(empty)} of {len(results)} pops found no job while {total_jobs} jobs were queued"


@pytest.fixture
def change_after_read(app, monkeypatch):
    """Return a helper that edits a request in its own transaction right after a pop reads its candidates.

    This is what a concurrent pop or a fault does between one pop's candidate read and its claim.
    """
    from sqlalchemy import text

    from horde.database import functions, text_functions
    from horde.flask import db

    def arrange(request_id: str, assignments: str) -> None:
        for module, name in ((functions, "get_sorted_wp_filtered_to_worker"), (text_functions, "get_sorted_text_wp_filtered_to_worker")):
            read_candidates = getattr(module, name)

            def read_then_change(*args, _read_candidates=read_candidates, **kwargs):
                candidates = _read_candidates(*args, **kwargs)
                with db.engine.connect() as connection:
                    connection.execute(text(f"UPDATE waiting_prompts SET {assignments} WHERE id = :id"), {"id": request_id})
                    connection.commit()
                return candidates

            monkeypatch.setattr(module, name, read_then_change)

    return arrange


@pytest.mark.parametrize("gentype", GENTYPES)
class TestClaimAfterRead:
    def test_a_request_emptied_after_it_was_read_gives_way_to_the_next(self, client, app, requester, owner, change_after_read, gentype):
        worker = Worker(client, app, owner, _config(gentype))
        first = _submit(client, gentype, requester, "taken by another worker")
        second = _submit(client, gentype, requester, "next in line")
        store(app, first, extra_priority=1000)
        store(app, second, extra_priority=10)
        change_after_read(first, "n = 0")

        job = worker.pop()

        assert prompt_of(job) == "next in line"
        assert read_request(app, first)["n"] == 0

    def test_a_request_partly_taken_after_it_was_read_hands_out_only_what_is_left(
        self,
        client,
        app,
        requester,
        owner,
        change_after_read,
        gentype,
    ):
        worker = Worker(client, app, owner, _config(gentype, amount=3))
        request_id = _submit(client, gentype, requester, "three wanted", params={"n": 3})
        change_after_read(request_id, "n = 1")

        job = worker.pop()

        assert len(job["ids"]) == 1
        assert read_request(app, request_id)["n"] == 0

    def test_a_request_faulted_after_it_was_read_is_not_handed_out(self, client, app, requester, owner, change_after_read, gentype):
        worker = Worker(client, app, owner, _config(gentype))
        request_id = _submit(client, gentype, requester, "faults meanwhile")
        change_after_read(request_id, "faulted = true")

        job = worker.pop()

        assert job["id"] is None
        assert read_request(app, request_id)["n"] == 1


def _claim_one_elsewhere(app, request_id: str) -> None:
    """Take one generation of a request in a separate transaction, as a concurrent pop's claim does."""
    from sqlalchemy import text

    from horde.flask import db

    with app.app_context(), db.engine.connect() as connection:
        connection.execute(text("UPDATE waiting_prompts SET n = n - 1 WHERE id = :id AND n > 0"), {"id": request_id})
        connection.commit()


class TestReturnedGenerationsDuringClaims:
    """A generation given back while another worker claims from the same request keeps both changes."""

    def test_a_fault_reported_while_another_worker_claims_keeps_the_claim(self, client, app, requester, owner, monkeypatch):
        from horde.classes.base.waiting_prompt import WaitingPrompt

        worker = Worker(client, app, owner, ImageWorkerConfig())
        request_id = submit_image(client, requester, "two wanted", params={"n": 2})
        job = worker.pop()
        assert read_request(app, request_id)["n"] == 1
        count_finished_jobs = WaitingPrompt.count_finished_jobs

        def count_while_another_worker_claims(self):
            _claim_one_elsewhere(app, request_id)
            return count_finished_jobs(self)

        monkeypatch.setattr(WaitingPrompt, "count_finished_jobs", count_while_another_worker_claims)
        resp = client.post(
            "/api/v2/generate/submit",
            json={"id": job["id"], "generation": "", "state": "faulted", "seed": 0},
            headers=_headers(owner.api_key),
        )

        assert resp.status_code == 200, resp.get_data(as_text=True)
        # One left, one claimed elsewhere, one given back.
        assert read_request(app, request_id)["n"] == 1

    @pytest.mark.parametrize("gentype", GENTYPES)
    def test_a_timeout_returned_while_another_worker_claims_keeps_the_claim(
        self,
        client,
        app,
        requester,
        owner,
        monkeypatch,
        gentype,
    ):
        from datetime import datetime, timedelta

        from horde.classes.base.processing_generation import ProcessingGeneration
        from horde.database.threads import check_waiting_prompts
        from horde.flask import db

        worker = Worker(client, app, owner, _config(gentype))
        request_id = _submit(client, gentype, requester, "two wanted", params={"n": 2})
        job = worker.pop()
        with app.app_context():
            procgen = db.session.get(ProcessingGeneration, job["id"])
            procgen.start_time = datetime.utcnow() - timedelta(hours=1)
            db.session.commit()
            db.session.remove()
        abort = ProcessingGeneration.abort

        def abort_while_another_worker_claims(self):
            _claim_one_elsewhere(app, request_id)
            return abort(self)

        monkeypatch.setattr(ProcessingGeneration, "abort", abort_while_another_worker_claims)
        check_waiting_prompts()

        assert read_request(app, request_id)["n"] == 1


@pytest.mark.parametrize("gentype", GENTYPES)
def test_workers_with_different_settings_share_one_candidate_query(client, app, make_api_user, gentype):
    """The candidate query's SQL does not depend on worker settings, so it is compiled once rather than per worker."""
    from sqlalchemy import event

    from horde.flask import db

    # Both list two models: an expanding IN list is rendered at execution with one placeholder per item.
    if gentype == "image":
        permissive = ImageWorkerConfig(models=[IMAGE_MODEL, OTHER_IMAGE_MODEL])
        restrictive = ImageWorkerConfig(
            bridge_agent=OLD_IMAGE_BRIDGE,
            nsfw=False,
            allow_img2img=False,
            allow_painting=False,
            allow_unsafe_ipaddr=False,
            allow_post_processing=False,
            allow_controlnet=False,
            allow_extended_controlnet=False,
            allow_sdxl_controlnet=False,
            allow_lora=False,
            extra_slow_worker=True,
            models=[OTHER_IMAGE_MODEL, IMAGE_MODEL],
        )
    else:
        permissive = TextWorkerConfig(models=[TEXT_MODEL, OTHER_TEXT_MODEL])
        restrictive = TextWorkerConfig(nsfw=False, max_length=40, models=[OTHER_TEXT_MODEL, TEXT_MODEL])
    workers = [
        Worker(client, app, make_api_user(trusted=True), permissive),
        Worker(client, app, make_api_user(trusted=False), restrictive, speed=1),
    ]
    workers[1].set(maintenance=True)
    table_reference = "FROM waiting_prompts"
    candidate_statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if table_reference in statement and "ORDER BY" in statement and "LIMIT" in statement:
            candidate_statements.append(statement)

    with app.app_context():
        event.listen(db.engine, "before_cursor_execute", record)
        try:
            for worker in workers:
                worker.raw_pop()
        finally:
            event.remove(db.engine, "before_cursor_execute", record)

    assert len(candidate_statements) == 2, candidate_statements
    assert candidate_statements[0] == candidate_statements[1]


class TestWordBlacklist:
    """A worker's word blacklist matches words inside the prompt, ignoring case, and only the word itself."""

    @pytest.mark.parametrize(
        ("word", "prompt", "refused"),
        [
            ("zebra", "A ZEBRA at dusk", True),
            ("Zebra", "a zebra at dusk", True),
            ("zebra", "zebras at dusk", True),
            ("50%", "50% off everything", True),
            ("50%", "50 percent off", False),
            ("a_b", "a_b testing", True),
            ("a_b", "axb testing", False),
            ("back\\slash", "a back\\slash here", True),
            ("back\\slash", "a backslash here", False),
        ],
    )
    def test_a_blacklisted_word_refuses_the_request(self, client, app, requester, owner, word, prompt, refused):
        worker = Worker(client, app, owner, ImageWorkerConfig(blacklist=[word]))
        submit_image(client, requester, prompt)

        job = worker.pop()

        if refused:
            assert job["id"] is None
            assert nonzero(job["skipped"]) == {"blacklist": 1}
        else:
            assert prompt_of(job) == prompt


def _read_boundaries() -> list[int]:
    from horde.database.functions import POP_CANDIDATE_LIMIT, POP_FIRST_READ_LIMIT

    first, second = POP_FIRST_READ_LIMIT, POP_FIRST_READ_LIMIT + POP_CANDIDATE_LIMIT
    return [first - 1, first, first + 1, second - 1, second, second + 1]


@pytest.mark.parametrize("unservable_ahead", _read_boundaries())
@pytest.mark.parametrize("gentype", GENTYPES)
def test_a_servable_request_at_a_read_boundary_is_found(client, app, make_api_user, gentype, unservable_ahead):
    """Candidates come in reads of growing size; a servable request on either side of where one read ends is found."""
    owner = make_api_user(kudos=100000)
    stranger = make_api_user(kudos=1000)
    worker = Worker(client, app, owner, _blocked_config(gentype))
    _queue_unservable(app, client, gentype, stranger, unservable_ahead, extra_priority=10**9)
    last = _submit(client, gentype, stranger, "servable after the unservable ones")
    store(app, last, extra_priority=0)

    assert prompt_of(worker.pop()) == "servable after the unservable ones"

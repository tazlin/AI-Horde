# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Text generation requestor and worker Locust users."""

import random
import string
import time
import uuid

from locust import HttpUser, between, tag, task
from locust.exception import RescheduleTask

from ..config import _EXPECTED_RC_RECOVER, _HOT_TEXT_PROMPT, _config
from ..helpers import (
    _headers,
    _is_expected_rc,
    _is_too_many_workers,
    _pick_requestor_key,
    _pick_worker_key,
    _random_prompt,
    _record_expected,
    _safe_json,
)


def _handle_async_text(resp, environment, name):
    """Common handling for POST /generate/text/async responses."""
    if resp.ok:
        body = _safe_json(resp) or {}
        resp.success()
        return body.get("id")
    body = _safe_json(resp)
    if resp.status_code == 429:
        resp.success()
        _record_expected(environment, "POST", name, resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
        time.sleep(random.uniform(2.0, 6.0))
        raise RescheduleTask()
    if resp.status_code in (400, 403) and _is_expected_rc(body, {"KudosUpfront", "SharedKeyInsufficientKudos"}):
        resp.success()
        _record_expected(environment, "POST", name, resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
        raise RescheduleTask()
    resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")
    return None


_PRICING_FILLER = "The maintenance log of the mainframe, entry by entry, as the robots recorded it. "
"""Eighty characters, so ``_pricing_prompt`` sizes a prompt by the server's four-characters-per-token estimate."""

PRICING_SHORT_PROMPT_TOKENS = 100
PRICING_MEDIUM_PROMPT_TOKENS = 5_000
"""Past the 4,096-token free-tier reference with some budget left: the gate reduces the allowance."""
PRICING_LONG_PROMPT_TOKENS = 12_000
"""Prompt processing alone exceeds the free budget: the gate refuses a kudos-less requester outright."""


def _pricing_prompt(estimated_tokens: int) -> str:
    """Return a prompt the server estimates at about ``estimated_tokens`` tokens, unique per call."""
    body = _PRICING_FILLER * (estimated_tokens * 4 // len(_PRICING_FILLER))
    return body + " " + uuid.uuid4().hex[:12]


def _classify_pricing_response(status_code: int, body: dict | None) -> str:
    """Return how a pricing-scenario POST ended: ``accepted``, ``quoted``, ``gated``, ``throttled`` or ``unexpected``.

    A gated outcome is the upfront refusal a kudos-less requester gets for a prompt the free tier
    cannot cover; it is an expected outcome of the scenario, not a failure of the service.
    """
    if status_code == 202:
        return "accepted"
    if status_code == 200 and body is not None and "kudos" in body:
        return "quoted"
    if status_code in (400, 403) and _is_expected_rc(body, {"KudosUpfront", "SharedKeyInsufficientKudos"}):
        return "gated"
    if status_code == 429:
        return "throttled"
    return "unexpected"


class TextRequester(HttpUser):
    """Simulates clients submitting text generation requests and polling them.

    Exercises: /generate/text/async (TextAsyncGenerate), /generate/text/status
    (TextAsyncStatus), kobold WP creation, text kudos calculation path.
    """

    weight = 2
    fixed_count = 0  # set via --text-requestors in on_test_start
    wait_time = between(1, 4)

    def on_start(self):
        self.pending_ids: list[str] = []
        self.api_key = _pick_requestor_key()
        self.short_prompt_quote: float | None = None

    def _post_async(self, payload: dict, name: str):
        with self.client.post(
            "/api/v2/generate/text/async",
            json=payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name=name,
        ) as resp:
            req_id = _handle_async_text(resp, self.environment, name)
            if req_id:
                self.pending_ids.append(req_id)

    @tag("text", "hot", "requestor")
    @task(4)
    def text_async_hot(self):
        """Repeated identical text request: exercises hot cache path."""
        payload = {
            "prompt": _HOT_TEXT_PROMPT,
            "params": {"max_length": 80, "max_context_length": 1024, "temperature": 0.7, "top_p": 0.9},
            "models": [],
            "trusted_workers": False,
        }
        self._post_async(payload, "/api/v2/generate/text/async [hot]")

    @tag("text", "cold", "requestor")
    @task(3)
    def text_async_cold(self):
        """Randomized text request: exercises cold/WP-creation path."""
        payload = {
            "prompt": _random_prompt() + " " + uuid.uuid4().hex[:12],
            "params": {
                "max_length": random.choice([40, 80, 128, 200]),
                "max_context_length": random.choice([1024, 1536, 2048]),
                "temperature": round(random.uniform(0.3, 1.2), 2),
                "top_p": round(random.uniform(0.7, 1.0), 2),
                "top_k": random.choice([0, 40, 60]),
            },
            "models": [],
            "trusted_workers": False,
        }
        self._post_async(payload, "/api/v2/generate/text/async [cold]")

    def _post_pricing(self, payload: dict, name: str) -> tuple[str, dict | None]:
        """POST a pricing scenario and classify the outcome, recording a gate refusal as expected."""
        with self.client.post(
            "/api/v2/generate/text/async",
            json=payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name=name,
        ) as resp:
            body = _safe_json(resp)
            outcome = _classify_pricing_response(resp.status_code, body)
            if outcome == "unexpected":
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")
                return outcome, body
            resp.success()
            if outcome in ("gated", "throttled"):
                _record_expected(self.environment, "POST", name, resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
            if outcome == "accepted" and body and body.get("id"):
                self.pending_ids.append(body["id"])
            return outcome, body

    @tag("text", "pricing", "requestor")
    @task(2)
    def text_dry_run_prices_the_prompt(self):
        """Two quotes with the same params: the long prompt must be quoted above the short one.

        Exercises the dry-run path, the quote cache (the prompt estimate is part of its key) and the
        tariff's prompt term under load.
        """
        params = {"max_length": 128, "max_context_length": 16384, "temperature": 0.7}
        if self.short_prompt_quote is None:
            outcome, body = self._post_pricing(
                {"prompt": _pricing_prompt(PRICING_SHORT_PROMPT_TOKENS), "params": params, "models": [], "dry_run": True},
                "/api/v2/generate/text/async [dry run, short prompt]",
            )
            if outcome != "quoted":
                return
            self.short_prompt_quote = float(body["kudos"])
        name = "/api/v2/generate/text/async [dry run, long prompt]"
        with self.client.post(
            "/api/v2/generate/text/async",
            json={"prompt": _pricing_prompt(PRICING_LONG_PROMPT_TOKENS), "params": params, "models": [], "dry_run": True},
            headers=_headers(self.api_key),
            catch_response=True,
            name=name,
        ) as resp:
            body = _safe_json(resp)
            outcome = _classify_pricing_response(resp.status_code, body)
            if outcome == "quoted":
                if float(body["kudos"]) <= self.short_prompt_quote:
                    resp.failure(f"long prompt quoted {body['kudos']} kudos, not above the short prompt's {self.short_prompt_quote}")
                else:
                    resp.success()
            elif outcome == "unexpected":
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")
            else:
                resp.success()
                _record_expected(self.environment, "POST", name, resp.elapsed.total_seconds() * 1000, len(resp.content or b""))

    @tag("text", "pricing", "requestor")
    @task(2)
    def text_async_medium_prompt_downgrades(self):
        """A 5,000-token prompt asking for 512 tokens with allow_downgrade: queued (downgraded or held) or gated."""
        self._post_pricing(
            {
                "prompt": _pricing_prompt(PRICING_MEDIUM_PROMPT_TOKENS),
                "params": {"max_length": 512, "max_context_length": 16384, "temperature": 0.7},
                "models": [],
                "allow_downgrade": True,
                "trusted_workers": False,
            },
            "/api/v2/generate/text/async [medium prompt, downgrade]",
        )

    @tag("text", "pricing", "requestor")
    @task(1)
    def text_async_long_prompt_needs_kudos(self):
        """A 12,000-token prompt asking for 16 tokens: accepted with a hold, or refused since no output makes it free."""
        self._post_pricing(
            {
                "prompt": _pricing_prompt(PRICING_LONG_PROMPT_TOKENS),
                "params": {"max_length": 16, "max_context_length": 16384, "temperature": 0.7},
                "models": [],
                "allow_downgrade": True,
                "trusted_workers": False,
            },
            "/api/v2/generate/text/async [long prompt, hold or refusal]",
        )

    @tag("text", "status", "requestor")
    @task(8)
    def text_status(self):
        if not self.pending_ids:
            return
        req_id = random.choice(self.pending_ids)
        with self.client.get(
            f"/api/v2/generate/text/status/{req_id}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/text/status/[id]",
        ) as resp:
            if resp.ok:
                data = _safe_json(resp) or {}
                if data.get("done") or data.get("faulted"):
                    self.pending_ids.remove(req_id)
                resp.success()
            elif resp.status_code in (404, 410):
                self.pending_ids.remove(req_id)
                resp.success()
            elif resp.status_code == 429:
                resp.success()
                _record_expected(
                    self.environment,
                    "GET",
                    "/api/v2/generate/text/status/[id]",
                    resp.elapsed.total_seconds() * 1000,
                    len(resp.content or b""),
                )
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")


_EMPTY_GENERATION_SHARE = 0.05
"""Share of simulated submissions that return nothing visible, exercising the empty-output branch of the tariff."""


def _simulated_generation() -> str:
    """Return the text a simulated worker submits: usually a sentence, sometimes a long reply, sometimes nothing."""
    roll = random.random()
    if roll < _EMPTY_GENERATION_SHARE:
        return ""
    if roll < 0.25:
        return "Once upon a time there was a stress test that completed successfully. " * 30
    return "Once upon a time there was a stress test that completed successfully."


class TextWorkerSimulator(HttpUser):
    """Simulates text workers: /generate/text/pop + /generate/text/submit."""

    weight = 1
    fixed_count = 0  # set via --text-workers in on_test_start
    wait_time = between(1, 4)

    def create_worker_name(self):
        return f"StressTextWorker-{''.join(random.choices(string.ascii_lowercase, k=4))}"

    def on_start(self):
        self.worker_name = self.create_worker_name()
        self.api_key = _pick_worker_key()

    @tag("text", "worker")
    @task
    def pop_and_submit_text(self):
        opts = self.environment.parsed_options
        pop_payload = {
            "name": self.worker_name,
            "models": _config.get("models", [])[:2] or ["koboldcpp/llama-3"],
            "bridge_agent": "KoboldAI Client:1.19.2-stress:https://github.com/koboldai/koboldai-client",
            "nsfw": True,
            "max_length": 512,
            # Wide enough to serve the pricing scenarios' 16K-context requests as well as the ordinary ones.
            "max_context_length": 16384,
            "softprompts": [],
            "threads": 1,
        }
        with self.client.post(
            "/api/v2/generate/text/pop",
            json=pop_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/text/pop",
        ) as resp:
            body = _safe_json(resp)
            if not resp.ok:
                if resp.status_code in (400, 403) and (_is_expected_rc(body, _EXPECTED_RC_RECOVER) or _is_too_many_workers(body)):
                    resp.success()
                    _record_expected(
                        self.environment, "POST", "/api/v2/generate/text/pop", resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
                    )
                    rc = (body or {}).get("rc") if isinstance(body, dict) else None
                    if _is_too_many_workers(body) or rc in ("TooManySameIPs", "WrongCredentials", "WorkerFlaggedMaintenance"):
                        self.api_key = _pick_worker_key()
                    self.worker_name = self.create_worker_name()
                    raise RescheduleTask()
                if resp.status_code == 429:
                    resp.success()
                    _record_expected(
                        self.environment, "POST", "/api/v2/generate/text/pop", resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
                    )
                    raise RescheduleTask()
                resp.failure(f"Text pop failed: {resp.status_code}: {resp.text[:200]}")
                return
            data = body or {}
            job_id = data.get("id")
            if not job_id:
                resp.success()
                return
            resp.success()

        time.sleep(random.uniform(opts.sim_gen_time_min / 2, opts.sim_gen_time_max / 2))
        submit_payload = {
            "id": job_id,
            "generation": _simulated_generation(),
            "state": "ok",
            "seed": random.randint(0, 999999999),
        }
        with self.client.post(
            "/api/v2/generate/text/submit",
            json=submit_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/text/submit",
        ) as resp:
            body = _safe_json(resp)
            if resp.ok:
                resp.success()
                return
            if resp.status_code == 404 or _is_expected_rc(body, {"InvalidJobID", "InvalidProcGen"}):
                resp.success()
                _record_expected(
                    self.environment, "POST", "/api/v2/generate/text/submit", resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
                )
                return
            if resp.status_code == 429:
                resp.success()
                _record_expected(
                    self.environment, "POST", "/api/v2/generate/text/submit", resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
                )
                return
            resp.failure(f"Text submit failed: {resp.status_code}: {resp.text[:200]}")

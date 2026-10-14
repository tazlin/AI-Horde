# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import time

from horde import vars as hv
from horde.bridge_reference import (
    is_backed_validated,
    parse_bridge_agent,
)
from horde.classes.base.processing_generation import ProcessingGeneration
from horde.classes.kobold.genstats import record_text_statistic
from horde.classes.kobold.pricing import (
    billing_prompt_tokens,
    calculate_text_generation_reward,
    count_output_tokens,
    matched_targeting_applies,
)
from horde.flask import db
from horde.logger import logger
from horde.metrics import (
    submit_genstats_record_duration,
    submit_state_handling_duration,
    text_empty_generations,
)
from horde.model_reference import model_reference
from horde.suspicions import Suspicions

UNKNOWN_ATTRIBUTE_VALUE = "unknown"
"""Placeholder for a metric attribute the submission did not include."""


def is_empty_text_generation(generation: str | None) -> bool:
    """Report whether a submitted text generation has no visible characters.

    Args:
        generation: The text the worker submitted, if any.

    Returns:
        True when the submission is missing, empty, or whitespace only.
    """
    if generation is None:
        return True
    return not generation.strip()


def build_empty_generation_attributes(model: str | None, bridge_agent: str | None, state: str) -> dict[str, str]:
    """Build the metric attributes for an empty text generation.

    The bridge agent is reduced to its name so the counter is not split across
    every version and worker URL of the same bridge.

    Args:
        model: The model the worker reported serving.
        bridge_agent: The worker's raw agent string, e.g. ``"KoboldCppEmbedWorker:2:https://..."``.
        state: The state the worker submitted the generation under.

    Returns:
        The attribute mapping to pass to the empty-generation counter.
    """
    bridge_name = UNKNOWN_ATTRIBUTE_VALUE
    if bridge_agent is not None:
        bridge_name, _ = parse_bridge_agent(bridge_agent)
    return {
        "model": model if model is not None else UNKNOWN_ATTRIBUTE_VALUE,
        "bridge_agent": bridge_name,
        "state": state,
    }


class TextProcessingGeneration(ProcessingGeneration):
    __mapper_args__ = {
        "polymorphic_identity": "text",
    }
    wp = db.relationship("TextWaitingPrompt", back_populates="processing_gens")
    worker = db.relationship("TextWorker", back_populates="processing_gens")

    def _censorship_state(self) -> str:
        """Determine the state string for this procgen based on its censorship flags."""
        if not self.censored:
            return "ok"
        gen_metadata = self.gen_metadata if self.gen_metadata is not None else []
        for meta in gen_metadata:
            if isinstance(meta, dict) and meta.get("type") == "censorship" and meta.get("value") == "csam":
                return "csam"
        return "censored"

    def get_details(self):
        """Returns a dictionary with details about this processing generation"""
        ret_dict = {
            "text": self.generation,
            "seed": self.seed,
            "worker_id": self.worker.id,
            "worker_name": self.worker.name,
            "model": self.model,
            "id": self.id,
            "state": self._censorship_state(),
            "gen_metadata": self.gen_metadata if self.gen_metadata is not None else [],
        }
        return ret_dict

    def get_gen_kudos(self) -> float:
        """Return the kudos this job earns its worker.

        The prompt operand is the part of the prompt the worker processes, the text it was sent capped
        at what the requested context keeps beside ``max_length``; the output operand is what it
        returned, capped by the requested ``max_length``. The arithmetic lives in
        ``horde.classes.kobold.pricing`` so the quote and the settlement cannot drift apart.

        Returns:
            The nominal reward, before owner escrow and the requester's burn.
        """
        known_model = model_reference.is_known_text_model(self.model)
        model_multiplier = model_reference.get_text_model_multiplier(self.model) if known_model else None
        return calculate_text_generation_reward(
            output_tokens=self.get_things_count(),
            prompt_tokens=billing_prompt_tokens(
                self.wp.prompt,
                max_context_length=self.wp.max_context_length,
                max_length=self.wp.things,
            ),
            model_multiplier=model_multiplier,
            owner_trusted=self.worker.user.trusted,
            backend_validated=is_backed_validated(self.worker.bridge_agent),
            matched_targeting=matched_targeting_applies(len(self.wp.workers)),
        )

    def log_aborted_generation(self):
        record_text_statistic(self)
        logger.info(
            f"Aborted Stale Generation {self.id} of wp {str(self.wp_id)} "
            f"(for {self.get_things_count()} tokens and {self.wp.max_context_length} content length) "
            f" from by worker: {self.worker.name} ({self.worker.id})",
        )

    def _record_empty_generation(self, state: str, things_per_sec: float, kudos: float) -> None:
        """Log and count a text generation that arrived with no visible text.

        The submission is still stored and still paid for; this only records the
        event so the behaviour can be measured before any payment rule changes.

        Args:
            state: The state the worker submitted the generation under.
            things_per_sec: The generation speed the worker reported.
            kudos: The kudos the submission was awarded.
        """
        prompt_characters = len(self.wp.prompt) if self.wp.prompt is not None else 0
        logger.warning(
            f"Empty text generation submitted: procgen {self.id} of wp {self.wp_id} "
            f"by worker {self.worker.name} ({self.worker.id}) "
            f"agent '{self.worker.bridge_agent}' model '{self.model}' state '{state}' "
            f"max_length={self.wp.max_length} max_context_length={self.wp.max_context_length} "
            f"prompt_characters={prompt_characters} things_per_sec={things_per_sec} kudos={kudos}",
        )
        text_empty_generations.add(
            1,
            build_empty_generation_attributes(self.model, self.worker.bridge_agent, state),
        )

    def set_generation(self, generation, things_per_sec, **kwargs):
        # We don't check the state in the super() function as image gen sets it early here
        # as well, so it can abort before doing R2 operations
        state_t0 = time.monotonic()
        state = kwargs.get("state", "ok")
        if state == "faulted":
            self.wp.n += 1
            self.abort()
        elif state in ("censored", "csam"):
            self.censored = True
            db.session.commit()

        # Detect csam from gen_metadata (mirrors the image-side approach).
        # Also inject a csam metadata entry when state == "csam" so the
        # authoritative source (gen_metadata) is always consistent.
        gen_metadata = list(kwargs.get("gen_metadata", self.gen_metadata or []))
        has_csam_meta = any(isinstance(m, dict) and m.get("type") == "censorship" and m.get("value") == "csam" for m in gen_metadata)
        if not has_csam_meta and state == "csam":
            gen_metadata.append({"type": "censorship", "value": "csam"})
            kwargs = {**kwargs, "gen_metadata": gen_metadata}
        elif has_csam_meta:
            self.censored = True
            db.session.commit()
        submit_state_handling_duration.record(time.monotonic() - state_t0, {"horde.gentype": "text"})

        # Checked before super() so the raw submission is examined, but reported
        # after it so the log can include the kudos awarded.
        submission_is_empty = is_empty_text_generation(generation)

        kudos = super().set_generation(generation, things_per_sec, **kwargs)
        if submission_is_empty:
            self._record_empty_generation(state, things_per_sec, kudos)
        genstats_t0 = time.monotonic()
        record_text_statistic(self)
        submit_genstats_record_duration.record(time.monotonic() - genstats_t0, {"horde.gentype": "text"})
        return kudos

    def get_things_count(self, generation: str | None = None) -> int:
        """Return the output operand for a returned generation.

        Args:
            generation: The text to count; the stored generation when omitted.

        Returns:
            0 when there is no generation; otherwise its token estimate, capped by the request's
            ``max_length``. The prompt is never counted here.
        """
        if generation is None:
            generation = self.generation
        return count_output_tokens(generation, self.wp.things)

    def record(self, things_per_sec, kudos):
        # Extended function to try and catch workers using unreasonable
        # speeds at higher params
        # This only affects untrusted workers running known models
        super().record(things_per_sec, kudos)
        if not model_reference.is_known_text_model(self.model):
            return
        if self.worker.user.trusted:
            return
        param_multiplier = model_reference.get_text_model_multiplier(self.model)
        unreasonable_speed = hv.suspicion_thresholds["text"]

        # max_speed_per_multiplier = {
        # 70: 12,
        # 40: 22,
        # 20: 35,
        # 13: 50,
        # 7: 70,
        # }

        # Once upon a time, before batching and other optimizations, these were the speeds we considered unreasonable
        # but new paradigms, backends and breakthroughs have made these numbers increasingly inaccurate or irrelevant.

        max_speed_per_multiplier = {
            70: 30,
            14: 100,
            8: 150,
        }

        for params_count in max_speed_per_multiplier:
            if param_multiplier >= params_count:
                unreasonable_speed = max_speed_per_multiplier[params_count]
                break
        # This handles the 8x7 and 8x22 models which are generally faster than their size implies.
        if "8x" in self.model:
            unreasonable_speed = unreasonable_speed * 3
        if things_per_sec > unreasonable_speed:
            self.worker.report_suspicion(reason=Suspicions.UNREASONABLY_FAST, formats=[f"{things_per_sec} > {unreasonable_speed}"])

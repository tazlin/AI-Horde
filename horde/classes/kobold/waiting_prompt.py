# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
from decimal import Decimal
from typing import Any

from sqlalchemy.sql import expression

from horde import vars as hv
from horde.classes.base.kudos import StyleReward
from horde.classes.base.waiting_prompt import WaitingPrompt
from horde.classes.kobold.pricing import (
    DEFAULT_QUOTE_MODEL_BILLIONS,
    billing_prompt_tokens,
    estimate_billing_prompt_tokens,
    free_output_allowance,
    matched_targeting_applies,
    quote_text_generation,
    reference_prompt_tokens,
)
from horde.flask import db
from horde.logger import logger
from horde.model_reference import model_reference


class TextWaitingPrompt(WaitingPrompt):
    __mapper_args__ = {
        "polymorphic_identity": "text",
    }
    max_length = db.Column(
        db.Integer,
        default=80,
        nullable=False,
        index=True,
        server_default=expression.literal(80),
    )
    max_context_length = db.Column(
        db.Integer,
        default=2048,
        nullable=False,
        index=True,
        server_default=expression.literal(2048),
    )
    softprompt = db.Column(db.String(255), default=None, nullable=True)
    processing_gens = db.relationship(
        "TextProcessingGeneration",
        back_populates="wp",
        passive_deletes=True,
        cascade="all, delete-orphan",
    )

    def extract_params(self, **kwargs):
        self.n = self.params.pop("n", 1)
        self.jobs = self.n
        self.max_length = self.params.get("max_length", 80)
        self.max_context_length = self.params.get("max_context_length", 2048)
        # To avoid unnecessary calculations, we do it once here.
        self.things = self.max_length
        # The total amount of to pixelsteps requested.
        self.total_usage = round(self.max_length * self.n / hv.thing_divisors["text"], 2)
        self.softprompt = kwargs.get("softprompt")
        self.prepare_job_payload(self.params)
        # Quoted here, as the image line does, so the accepted response and the upfront hold carry the
        # same per-job price a dry run would.
        self.calculate_kudos()

    @logger.catch(reraise=True)
    def prepare_job_payload(self, initial_dict=None):
        """Prepares the default job payload. This might be further adjusted per job in get_job_payload()"""
        if not initial_dict:
            initial_dict = {}
        self.gen_payload = initial_dict.copy()
        self.gen_payload["prompt"] = self.prompt
        self.gen_payload["n"] = 1
        db.session.commit()

    def activate(
        self,
        downgrade_wp_priority=False,
        source_image=None,
        source_mask=None,
        extra_source_images=None,
        kudos_adjustment=0,
        *,
        style_reward: StyleReward | None = None,
    ):
        # We separate the activation from __init__ as often we want to check if there's a valid worker for it
        # Before we add it to the queue
        super().activate(
            downgrade_wp_priority,
            extra_source_images=extra_source_images,
            kudos_adjustment=kudos_adjustment,
            style_reward=style_reward,
        )
        proxied_account = ""
        if self.proxied_account:
            proxied_account = f":{self.proxied_account}"
        logger.info(
            f"New text2text prompt with ID {self.id} by {self.user.get_unique_alias()}{proxied_account}: "
            f"max_length:{self.max_length} * n:{self.n} == {self.total_usage} Total Tokens",
        )

    def get_submitted_request(self) -> dict[str, Any]:
        ret_dict = super().get_submitted_request()
        ret_dict["softprompt"] = self.softprompt
        return ret_dict

    def calculate_extra_kudos_burn(self, kudos):
        # This represents the cost of using the resources of the horde
        return kudos + 1

    def log_faulted_prompt(self):
        logger.warning(
            f"Faulting waiting text prompt {self.id} with payload '{self.gen_payload}' due to too many faulted jobs",
        )

    def get_status(self, **kwargs):
        ret_dict = super().get_status(**kwargs)
        return ret_dict

    def record_usage(
        self,
        raw_things,
        kudos,
        usage_type="text",
        avoid_burn=False,
        commit=True,
        *,
        style_reward: StyleReward | None = None,
    ) -> Decimal:
        """I need to extend this to point it to record_text_usage()"""
        return super().record_usage(
            raw_things,
            kudos,
            usage_type,
            avoid_burn=avoid_burn,
            commit=commit,
            style_reward=style_reward,
        )

    def require_upfront_kudos(self, counted_totals, total_threads):
        """Returns A tuple
        First entry in the tuple is True if this wp requires that the user already has the required kudos to fulfil it
        else is False
        Second entry in the tuple is the max tokens that can be used without upfront kudos
        Third entry in the tuple is whether the upfront kudos requirement prevents downgrading to resolve this.

        The output allowance is first set by queue pressure and worker supply, then reduced for a prompt
        past the reference size, so that the modelled work of a free request stays within what the
        allowance buys at the reference prompt. The prompt is measured as the worker will process it,
        capped at what the requested context keeps beside the allowance, so a downgrade to the returned
        allowance clears both the output and the prompt trigger in one pass. An allowance of zero means
        the prompt's own processing already exceeds the budget, and the third entry then forbids
        downgrading.
        """
        queue = counted_totals["queued_text_requests"]
        max_tokens = 512 + (total_threads * 5) - round(queue * 0.9)
        # logger.debug([queue,max_tokens])
        if not self.slow_workers:
            return (True, max_tokens, False)
        if max_tokens < 256:
            max_tokens = 256
        if max_tokens > 512:
            max_tokens = 512
        max_tokens = free_output_allowance(
            prompt_tokens=estimate_billing_prompt_tokens(self.prompt),
            free_output_tokens=max_tokens,
            reference_prompt_tokens=reference_prompt_tokens(),
            max_context_length=self.max_context_length,
        )
        if max_tokens == 0:
            # The prompt alone exceeds the free budget: generating less cannot make this request free.
            return (True, 0, True)
        if self.max_length > max_tokens:
            return (True, max_tokens, False)
        if os.getenv("HORDE_UPFRONT_KUDOS_ON_WORKERLIST", "0") == "1" and len(self.workers) > 0:
            return (True, max_tokens, True)
        return (False, max_tokens, False)

    def downgrade(self, max_tokens: int) -> None:
        """Mutate this request to fit the free-tier output allowance and accept slow workers.

        Every field that carries the output allowance moves together: the settlement cap on the output
        operand reads ``things``, the worker reads ``gen_payload``, the usage aggregate reads
        ``total_usage`` and the accepted response reads ``kudos``. Admission downgrades before any job
        is dispatched, when ``n`` still equals ``jobs``.

        Args:
            max_tokens: The output allowance the request may keep without an upfront hold.
        """
        self.slow_workers = True
        if self.max_length > max_tokens:
            self.max_length = max_tokens
            self.things = max_tokens
            self.params["max_length"] = max_tokens
            self.gen_payload["max_length"] = max_tokens
            self.total_usage = round(self.max_length * self.n / hv.thing_divisors["text"], 2)
            self.calculate_kudos()
            logger.info(f"Text WP {self.id} was downgraded to {self.max_length} tokens")
        db.session.commit()

    def calculate_kudos(self) -> float:
        """Return the per-job quote for this request and store it on ``kudos``.

        The quote applies the settlement kernel to the prompt as the worker will process it and the
        whole requested ``max_length``, assuming a validated backend and a trusted owner. Among the
        requested models it prices the largest known one, which is also what the upfront hold covers,
        so a small model listed next to a large one does not lower the price.

        Returns:
            The per-job quote, before the per-job burn and the request tax.
        """
        self.kudos = quote_text_generation(
            output_allowance=self.max_length,
            prompt_tokens=billing_prompt_tokens(
                self.prompt,
                max_context_length=self.max_context_length,
                max_length=self.max_length,
            ),
            model_multiplier=self._quote_model_multiplier(),
            matched_targeting=matched_targeting_applies(len(self.workers)),
        )
        return self.kudos

    def _quote_model_multiplier(self) -> float | None:
        """Return the model size the quote prices, in billions of parameters.

        Returns:
            The largest multiplier among the request's known models; ``DEFAULT_QUOTE_MODEL_BILLIONS``
            when the request names no model; ``None`` when every named model is unknown.
        """
        model_names = [model.model for model in self.models]
        if len(model_names) == 0:
            return DEFAULT_QUOTE_MODEL_BILLIONS
        known_multipliers = [
            model_reference.get_text_model_multiplier(model_name)
            for model_name in model_names
            if model_reference.is_known_text_model(model_name)
        ]
        if len(known_multipliers) == 0:
            return None
        return max(known_multipliers)

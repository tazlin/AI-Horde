# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The speed sample a text submission records, and what it includes.

``record_fulfilment`` divides the returned token count by the wall-clock time from pop to submit.
Prompt processing is inside that time and is not in the numerator, so a worker that takes long
prompts records a lower speed for the same output, and ``TextWorker.can_generate`` compares that
speed against the slow-worker threshold. The rule is locked here so a change to it is deliberate.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from horde.classes.base import stats
from horde.classes.kobold.processing_generation import TextProcessingGeneration
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TextWorker

pytestmark = pytest.mark.unit


def _processing_generation(db_session: Session, user_id: int, *, max_length: int, seconds_ago: float) -> TextProcessingGeneration:
    worker = TextWorker(
        name=f"speed_worker_{uuid.uuid4().hex[:8]}", user_id=user_id, bridge_agent="AI Horde Worker:24:https://example.invalid"
    )
    db_session.add(worker)
    db_session.flush()
    wp = TextWaitingPrompt(
        worker_ids=[],
        models=["any-model"],
        prompt="a prompt whose length does not enter the speed sample",
        user_id=user_id,
        params={"n": 1, "max_length": max_length, "max_context_length": 4096},
    )
    db_session.flush()
    procgen = TextProcessingGeneration(wp_id=wp.id, worker_id=worker.id, model="any-model")
    procgen.start_time = datetime.utcnow() - timedelta(seconds=seconds_ago)
    db_session.flush()
    return procgen


def test_speed_is_returned_tokens_over_pop_to_submit_seconds(db_session: Session, make_user) -> None:
    user = make_user()
    procgen = _processing_generation(db_session, user.id, max_length=512, seconds_ago=10)

    # 400 characters count as 100 tokens; 100 tokens over about 10 seconds.
    speed = stats.record_fulfilment(procgen, procgen.get_things_count("x" * 400))

    assert speed == pytest.approx(10.0, abs=0.2)


def test_the_requested_allowance_caps_the_tokens_in_the_sample(db_session: Session, make_user) -> None:
    user = make_user()
    procgen = _processing_generation(db_session, user.id, max_length=50, seconds_ago=10)

    speed = stats.record_fulfilment(procgen, procgen.get_things_count("x" * 4000))

    assert speed == pytest.approx(5.0, abs=0.2)


def test_an_empty_return_still_samples_the_output_floor(db_session: Session, make_user) -> None:
    user = make_user()
    procgen = _processing_generation(db_session, user.id, max_length=512, seconds_ago=20)

    speed = stats.record_fulfilment(procgen, procgen.get_things_count(""))

    assert speed == pytest.approx(1.0, abs=0.1)

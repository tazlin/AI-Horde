# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settlement when an in-flight text generation is cancelled.

A text job cancelled before the worker returned anything has no output operand, so a known model
pays the worker nothing; the requester still pays the per-job burn, because the job occupied a
worker. The accounting runs through the real ``TextProcessingGeneration.cancel`` on a persisted ORM
graph, mirroring ``test_kudos_image_cancel.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from horde.classes.base.user import User
from horde.classes.kobold import processing_generation as text_procgen_module
from horde.classes.kobold.processing_generation import TextProcessingGeneration
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TextWorker
from horde.enums import UserRoleTypes
from tests.fixture_types import MakeUser, MakeUserRole

pytestmark = pytest.mark.unit

KNOWN_MODEL = "a-known-13B"
UNKNOWN_MODEL = "not-in-the-reference"
REQUEST_BURN = 1
"""``TextWaitingPrompt.calculate_extra_kudos_burn`` adds one kudos to every requester debit."""


@pytest.fixture(autouse=True)
def _reference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUDOS_TRUST_THRESHOLD", "100000")
    monkeypatch.setattr(text_procgen_module.model_reference, "is_known_text_model", lambda model_name: model_name == KNOWN_MODEL)
    monkeypatch.setattr(text_procgen_module.model_reference, "get_text_model_multiplier", lambda model_name: 13.0)
    monkeypatch.delenv("HORDE_REQUIRE_MATCHED_TARGETING", raising=False)


def _build_processing_generation(
    db_session: Session, requester: User, owner: User, model: str, prompt_chars: int
) -> TextProcessingGeneration:
    """Create a persisted, still-processing text generation for the given requester and owner."""
    worker = TextWorker(
        name=f"text_worker_{uuid.uuid4().hex[:8]}", user_id=owner.id, bridge_agent="AI Horde Worker:24:https://example.invalid"
    )
    db_session.add(worker)
    db_session.flush()
    wp = TextWaitingPrompt(
        worker_ids=[],
        models=[model],
        prompt="x" * prompt_chars,
        user_id=requester.id,
        params={"n": 1, "max_length": 512, "max_context_length": 16384},
    )
    db_session.flush()
    procgen = TextProcessingGeneration(wp_id=wp.id, worker_id=worker.id, model=model)
    db_session.flush()
    return procgen


class TestCancelBeforeAnyOutput:
    """A known model pays nothing for a job that returned nothing, however long its prompt was."""

    def test_worker_and_owner_are_credited_nothing(
        self,
        db_session: Session,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
        settle_kudos: Callable[[], int],
    ) -> None:
        requester = make_user(kudos=1000)
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        procgen = _build_processing_generation(db_session, requester, owner, KNOWN_MODEL, prompt_chars=200_000)

        returned = procgen.cancel()
        settle_kudos()

        assert returned == 0
        assert procgen.worker.kudos == 0
        assert owner.kudos == 1000

    def test_requester_pays_only_the_burn(
        self,
        db_session: Session,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
        settle_kudos: Callable[[], int],
    ) -> None:
        requester = make_user(kudos=1000)
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        procgen = _build_processing_generation(db_session, requester, owner, KNOWN_MODEL, prompt_chars=200_000)

        procgen.cancel()
        settle_kudos()

        assert requester.kudos == 1000 - REQUEST_BURN

    def test_cancel_marks_the_generation_faulted_and_cancelled(
        self,
        db_session: Session,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
    ) -> None:
        requester = make_user(kudos=1000)
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        procgen = _build_processing_generation(db_session, requester, owner, KNOWN_MODEL, prompt_chars=4000)

        procgen.cancel()

        assert procgen.faulted is True
        assert procgen.cancelled is True


class TestUnknownModelUntrustedOwnerCancel:
    """The unknown-model, untrusted-owner branch pays a flat prompt-derived amount with no output operand."""

    def test_the_owner_is_still_paid_the_flat_amount_on_cancel(
        self,
        db_session: Session,
        make_user: MakeUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        requester = make_user(kudos=1000)
        owner = make_user(kudos=1000)
        procgen = _build_processing_generation(db_session, requester, owner, UNKNOWN_MODEL, prompt_chars=4096)

        returned = procgen.cancel()

        # 1024-token prompt: 1.2 + 2.2 ** log2(1024 / 1024) = 2.2, whatever was (not) returned.
        assert returned == pytest.approx(2.2)


class TestAlreadySettledCancelIsInert:
    def test_cancelling_a_completed_generation_moves_nothing(
        self,
        db_session: Session,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
        settle_kudos: Callable[[], int],
    ) -> None:
        requester = make_user(kudos=1000)
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        procgen = _build_processing_generation(db_session, requester, owner, KNOWN_MODEL, prompt_chars=4000)
        procgen.generation = "already delivered text"
        db_session.flush()

        assert procgen.cancel() is None
        settle_kudos()

        assert requester.kudos == 1000
        assert procgen.worker.kudos == 0

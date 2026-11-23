# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Rule matching and the process-local rule snapshot in ``horde.validated_backends``.

The seed rules have to accept exactly the bridge agents accepted by the hard-coded ``LLM_VALIDATED_BACKENDS`` set.
Otherwise a deploy would cut the rewards of workers that were validated, or raise the rewards of workers that were not.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from horde.validated_backends import (
    LEGACY_VALIDATED_BACKEND_SEEDS,
    ValidatedBackendRegistry,
    ValidatedBackendRule,
    meets_minimum_version,
)
from horde.worker_software import WorkerSoftware, parse_legacy_bridge_agent

pytestmark = pytest.mark.unit

_SEED_RULES: tuple[ValidatedBackendRule, ...] = tuple(
    ValidatedBackendRule(
        bridge_name=seed.bridge_name,
        backend_name=seed.backend_name,
        min_bridge_version=seed.min_bridge_version,
        min_backend_version=seed.min_backend_version,
    )
    for seed in LEGACY_VALIDATED_BACKEND_SEEDS
)


def _seed_registry() -> ValidatedBackendRegistry:
    return ValidatedBackendRegistry(load_rules=lambda: _SEED_RULES)


@pytest.mark.parametrize(
    "bridge_agent",
    [
        "AI Horde Worker:24:https://github.com/Haidra-Org/AI-Horde-Worker",
        "AI Horde Worker~aphrodite~oai:24:https://github.com/Haidra-Org/AI-Horde-Worker",
        "AI Horde Worker~aphrodite~kai:24:https://github.com/Haidra-Org/AI-Horde-Worker",
        "KoboldCppEmbedWorker:2:https://github.com/LostRuins/koboldcpp",
        "TabbyAPI:0.0.1:https://github.com/theroyallab/tabbyAPI",
    ],
)
def test_seed_rules_accept_every_legacy_validated_bridge_agent(bridge_agent: str) -> None:
    assert _seed_registry().is_validated(parse_legacy_bridge_agent(bridge_agent))


@pytest.mark.parametrize(
    "bridge_agent",
    [
        "AI Horde Worker~unknown~oai:24:https://github.com/Haidra-Org/AI-Horde-Worker",
        "AI Horde Worker~unknown~kai:24:https://github.com/Haidra-Org/AI-Horde-Worker",
        "AI Horde Worker~vllm~oai:24:https://github.com/Haidra-Org/AI-Horde-Worker",
        "AI Horde Worker~~oai:24:https://github.com/Haidra-Org/AI-Horde-Worker",
        "SomeOtherBridge:1:https://example.com",
        "KoboldCppEmbedWorker~aphrodite:2:https://github.com/LostRuins/koboldcpp",
        "unknown:0:unknown",
        "AI Horde Worker:not-a-version:https://github.com/Haidra-Org/AI-Horde-Worker",
    ],
)
def test_seed_rules_refuse_the_bridge_agents_refused_by_the_legacy_set(bridge_agent: str) -> None:
    assert not _seed_registry().is_validated(parse_legacy_bridge_agent(bridge_agent))


def test_bridge_and_backend_names_match_case_insensitively() -> None:
    rule = ValidatedBackendRule(bridge_name="AI Horde Worker", backend_name="Aphrodite")

    assert rule.matches(WorkerSoftware(bridge_name="ai horde worker", backend_name="APHRODITE"))


def test_rule_without_a_backend_matches_only_workers_that_do_not_report_a_backend() -> None:
    rule = ValidatedBackendRule(bridge_name="TabbyAPI")

    assert rule.matches(WorkerSoftware(bridge_name="TabbyAPI"))
    assert not rule.matches(WorkerSoftware(bridge_name="TabbyAPI", backend_name="exllamav2"))


def test_rule_with_a_backend_does_not_match_workers_that_do_not_report_a_backend() -> None:
    rule = ValidatedBackendRule(bridge_name="AI Horde Worker", backend_name="llamacpp")

    assert not rule.matches(WorkerSoftware(bridge_name="AI Horde Worker"))


def test_minimum_versions_compare_as_loose_semantic_versions() -> None:
    rule = ValidatedBackendRule(
        bridge_name="AI Horde Worker",
        backend_name="llamacpp",
        min_bridge_version="25",
        min_backend_version="0.3.2",
    )

    assert rule.matches(
        WorkerSoftware(bridge_name="AI Horde Worker", bridge_version="25.0.0", backend_name="llamacpp", backend_version="0.3.10"),
    )
    assert not rule.matches(
        WorkerSoftware(bridge_name="AI Horde Worker", bridge_version="24", backend_name="llamacpp", backend_version="0.3.10"),
    )
    assert not rule.matches(
        WorkerSoftware(bridge_name="AI Horde Worker", bridge_version="25", backend_name="llamacpp", backend_version="0.3.1"),
    )


@pytest.mark.parametrize("reported_version", [None, "", "nightly", "1.2.3.4"])
def test_unreported_or_unparsable_versions_do_not_meet_a_minimum(reported_version: str | None) -> None:
    assert not meets_minimum_version(reported_version, "1")


def test_rule_snapshot_is_reused_until_it_is_invalidated() -> None:
    loads: list[int] = []

    def load_rules() -> tuple[ValidatedBackendRule, ...]:
        loads.append(1)
        return _SEED_RULES

    registry = ValidatedBackendRegistry(refresh_seconds=3600, load_rules=load_rules)
    registry.get_rules()
    registry.get_rules()
    assert len(loads) == 1

    registry.invalidate()
    registry.get_rules()
    assert len(loads) == 2


def test_failed_refresh_keeps_the_previous_rule_snapshot() -> None:
    fail_next_load = False

    def load_rules() -> tuple[ValidatedBackendRule, ...]:
        if fail_next_load:
            raise OperationalError("SELECT", {}, Exception("database is gone"))
        return _SEED_RULES

    registry = ValidatedBackendRegistry(refresh_seconds=0, load_rules=load_rules)
    assert registry.get_rules() == _SEED_RULES

    fail_next_load = True
    assert registry.get_rules() == _SEED_RULES


def test_error_from_a_failed_first_load_is_raised() -> None:
    def load_rules() -> tuple[ValidatedBackendRule, ...]:
        raise OperationalError("SELECT", {}, Exception("database is gone"))

    with pytest.raises(OperationalError):
        ValidatedBackendRegistry(load_rules=load_rules).get_rules()

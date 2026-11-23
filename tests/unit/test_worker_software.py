# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The worker software report in ``horde.worker_software``.

A worker reports its bridge and backend either as the structured ``bridge`` and ``backend`` pop fields or packed into
the legacy ``bridge_agent`` string. A regression in the legacy parser would change which workers are accepted by the
validated backend registry, so the strings sent by the deployed bridges are pinned here.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from horde.worker_software import (
    UNKNOWN_SOFTWARE_NAME,
    BackendApi,
    BackendReport,
    BridgeReport,
    SoftwareReportLimits,
    WorkerSoftware,
    build_bridge_agent,
    parse_backend_api,
    parse_legacy_bridge_agent,
    resolve_reported_software,
)

pytestmark = pytest.mark.unit

_WORKER_URL = "https://github.com/Haidra-Org/AI-Horde-Worker"


@pytest.mark.parametrize(
    ("bridge_agent", "expected_software"),
    [
        (
            f"AI Horde Worker:24:{_WORKER_URL}",
            WorkerSoftware(bridge_name="AI Horde Worker", bridge_version="24"),
        ),
        (
            f"AI Horde Worker~aphrodite~oai:24:{_WORKER_URL}",
            WorkerSoftware(
                bridge_name="AI Horde Worker",
                bridge_version="24",
                backend_name="aphrodite",
                backend_api=BackendApi.OPENAI,
            ),
        ),
        (
            f"AI Horde Worker~aphrodite~kai:24:{_WORKER_URL}",
            WorkerSoftware(
                bridge_name="AI Horde Worker",
                bridge_version="24",
                backend_name="aphrodite",
                backend_api=BackendApi.KOBOLD,
            ),
        ),
        (
            "KoboldCppEmbedWorker:2:https://github.com/LostRuins/koboldcpp",
            WorkerSoftware(bridge_name="KoboldCppEmbedWorker", bridge_version="2"),
        ),
        (
            "TabbyAPI:0.0.1:https://github.com/theroyallab/tabbyAPI",
            WorkerSoftware(bridge_name="TabbyAPI", bridge_version="0.0.1"),
        ),
        (
            f"AI Horde Worker~unknown~oai:24:{_WORKER_URL}",
            WorkerSoftware(
                bridge_name="AI Horde Worker",
                bridge_version="24",
                backend_name="unknown",
                backend_api=BackendApi.OPENAI,
            ),
        ),
        (
            f"AI Horde Worker~~oai:24:{_WORKER_URL}",
            WorkerSoftware(
                bridge_name="AI Horde Worker",
                bridge_version="24",
                backend_name=UNKNOWN_SOFTWARE_NAME,
                backend_api=BackendApi.OPENAI,
            ),
        ),
        (
            f"AI Horde Worker~vllm~grpc:24:{_WORKER_URL}",
            WorkerSoftware(bridge_name="AI Horde Worker", bridge_version="24", backend_name="vllm"),
        ),
    ],
)
def test_legacy_bridge_agents_parse_to_the_expected_software(bridge_agent: str, expected_software: WorkerSoftware) -> None:
    assert parse_legacy_bridge_agent(bridge_agent) == expected_software


@pytest.mark.parametrize(
    "bridge_agent",
    [
        None,
        "",
        "unknown:0:unknown",
        "AI Horde Worker",
        "AI Horde Worker:24",
        "AI Horde Worker:not-a-version:url",
    ],
)
def test_unparsable_bridge_agents_parse_to_the_unknown_bridge_without_a_backend(bridge_agent: str | None) -> None:
    software = parse_legacy_bridge_agent(bridge_agent)

    assert software.bridge_name == UNKNOWN_SOFTWARE_NAME
    assert software.backend_name is None


@pytest.mark.parametrize(
    ("api_name", "expected_api"),
    [
        ("openai", BackendApi.OPENAI),
        ("OpenAI", BackendApi.OPENAI),
        ("oai", BackendApi.OPENAI),
        ("kobold", BackendApi.KOBOLD),
        ("kai", BackendApi.KOBOLD),
        ("grpc", None),
        ("", None),
        (None, None),
    ],
)
def test_backend_api_names_parse_with_legacy_aliases(api_name: str | None, expected_api: BackendApi | None) -> None:
    assert parse_backend_api(api_name) == expected_api


def test_structured_report_is_preferred_to_the_bridge_agent() -> None:
    software = resolve_reported_software(
        bridge_agent=f"AI Horde Worker~aphrodite~oai:24:{_WORKER_URL}",
        bridge=BridgeReport(name="AI Horde Worker", version="25.0.0"),
        backend=BackendReport(name="llamacpp", version="0.3.2", api="openai"),
    )

    assert software == WorkerSoftware(
        bridge_name="AI Horde Worker",
        bridge_version="25.0.0",
        backend_name="llamacpp",
        backend_version="0.3.2",
        backend_api=BackendApi.OPENAI,
    )


def test_bridge_agent_backend_is_ignored_when_the_structured_report_omits_the_backend() -> None:
    software = resolve_reported_software(
        bridge_agent=f"AI Horde Worker~aphrodite~oai:24:{_WORKER_URL}",
        bridge=BridgeReport(name="KoboldCppEmbedWorker", version="2"),
        backend=None,
    )

    assert software == WorkerSoftware(bridge_name="KoboldCppEmbedWorker", bridge_version="2")


def test_bridge_agent_built_from_a_bridge_report_parses_back_to_the_same_bridge() -> None:
    bridge = BridgeReport(name="AI Horde Worker", version="25.0.0", url=_WORKER_URL)

    built_agent = build_bridge_agent(bridge)

    assert built_agent == f"AI Horde Worker:25.0.0:{_WORKER_URL}"
    assert parse_legacy_bridge_agent(built_agent) == WorkerSoftware(bridge_name="AI Horde Worker", bridge_version="25.0.0")


def test_bridge_agent_built_from_a_bridge_report_without_a_url_uses_unknown_as_the_url() -> None:
    assert build_bridge_agent(BridgeReport(name="TabbyAPI", version="1.0")) == "TabbyAPI:1.0:unknown"


@pytest.mark.parametrize("name", ["bad:name", "bad~name", "", "   ", "x" * (SoftwareReportLimits.MAX_NAME_LENGTH + 1)])
def test_bridge_names_that_do_not_fit_in_a_bridge_agent_string_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        BridgeReport(name=name, version="1")


def test_report_values_are_stripped_and_unknown_fields_are_ignored() -> None:
    backend = BackendReport.model_validate({"name": "  llamacpp ", "version": " 0.3 ", "api": "openai", "extra": 1})

    assert backend.name == "llamacpp"
    assert backend.version == "0.3"

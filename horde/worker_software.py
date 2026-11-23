# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Describe a worker's software: the bridge, which connects to the horde, and the backend, which runs the generation.

A worker reports its software on every pop, either in the structured ``bridge`` and ``backend`` objects or in the legacy
``bridge_agent`` string of the form ``bridge[~backend[~api]]:version:url``. Both forms resolve to one
[`WorkerSoftware`][horde.worker_software.WorkerSoftware], which is checked against the rules of the validated backend
registry ([`horde.validated_backends`][horde.validated_backends]).

- [`WorkerSoftware`][horde.worker_software.WorkerSoftware]: the resolved report, stored on the worker's database row.
- [`BridgeReport`][horde.worker_software.BridgeReport] and [`BackendReport`][horde.worker_software.BackendReport]:
  validate the structured pop fields.
- [`BackendApi`][horde.worker_software.BackendApi]: the backend API dialects recognised by the horde.
- [`parse_legacy_bridge_agent`][horde.worker_software.parse_legacy_bridge_agent]: resolve a ``bridge_agent`` string.
- [`resolve_reported_software`][horde.worker_software.resolve_reported_software]: resolve the software fields of a pop.
- [`build_bridge_agent`][horde.worker_software.build_bridge_agent]: create the ``bridge_agent`` string for a
  structured report, so code that reads the string keeps working.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Final

import semver
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

__all__ = [
    "LEGACY_AGENT_FIELD_SEPARATOR",
    "LEGACY_DEFAULT_BRIDGE_AGENT",
    "LEGACY_NAME_SEPARATOR",
    "UNKNOWN_SOFTWARE_NAME",
    "BackendApi",
    "BackendReport",
    "BridgeReport",
    "SoftwareReportLimits",
    "WorkerSoftware",
    "build_bridge_agent",
    "parse_backend_api",
    "parse_legacy_bridge_agent",
    "resolve_reported_software",
]

LEGACY_AGENT_FIELD_SEPARATOR: Final = ":"
"""Separates the name, version and URL of a ``bridge_agent`` string."""

LEGACY_NAME_SEPARATOR: Final = "~"
"""Separates the bridge, backend and API parts of a legacy ``bridge_agent`` name."""

LEGACY_DEFAULT_BRIDGE_AGENT: Final = "unknown:0:unknown"
"""The ``bridge_agent`` of a pop that does not send one. It parses as the bridge ``unknown``."""

UNKNOWN_SOFTWARE_NAME: Final = "unknown"
"""The bridge name of an unparsable ``bridge_agent``, and the backend name of an empty legacy backend part."""


class SoftwareReportLimits:
    """Group the length limits of the structured software report.

    The bridge name and version become the first two fields of the ``bridge_agent`` string. The pop's ``bridge_agent``
    field accepts 1000 characters, so the limits leave room for the URL.
    """

    MAX_NAME_LENGTH: Final = 100
    """The longest bridge or backend name."""
    MAX_VERSION_LENGTH: Final = 64
    """The longest bridge or backend version."""
    MAX_URL_LENGTH: Final = 500
    """The longest bridge URL."""
    MAX_API_LENGTH: Final = 16
    """The longest stored API dialect, a ``BackendApi`` value."""


class BackendApi(StrEnum):
    """Identify the API dialect between a worker's bridge and backend."""

    OPENAI = "openai"
    """The OpenAI-compatible ``/v1`` API, such as ``/v1/completions`` and ``/v1/chat/completions``."""
    KOBOLD = "kobold"
    """The KoboldAI ``/api`` generate API."""


_LEGACY_BACKEND_API_ALIASES: Final[dict[str, BackendApi]] = {
    "oai": BackendApi.OPENAI,
    "kai": BackendApi.KOBOLD,
}
"""The short API names used in legacy ``bridge_agent`` strings, such as ``AI Horde Worker~aphrodite~oai``."""


def parse_backend_api(api_name: str | None) -> BackendApi | None:
    """Parse a reported API dialect, accepting the legacy short names.

    Args:
        api_name: The reported dialect, such as ``openai`` or the legacy ``oai``.

    Returns:
        The dialect, or None when a dialect was not reported or the name is not a known dialect. An unknown name is
        ignored instead of rejected, the same as an unknown chat feature, so a bridge can report a newer dialect to an
        older horde.
    """
    if not api_name:
        return None
    normalized_api_name = api_name.strip().lower()
    if normalized_api_name in _LEGACY_BACKEND_API_ALIASES:
        return _LEGACY_BACKEND_API_ALIASES[normalized_api_name]
    try:
        return BackendApi(normalized_api_name)
    except ValueError:
        return None


def _refuse_reserved_characters(reported_value: str) -> str:
    """Validate that a reported name or version does not contain a ``bridge_agent`` separator.

    Raises:
        ValueError: The value contains ``:`` or ``~``.
    """
    if LEGACY_AGENT_FIELD_SEPARATOR in reported_value or LEGACY_NAME_SEPARATOR in reported_value:
        raise ValueError(f"must not contain '{LEGACY_AGENT_FIELD_SEPARATOR}' or '{LEGACY_NAME_SEPARATOR}'")
    return reported_value


SoftwareName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=SoftwareReportLimits.MAX_NAME_LENGTH),
    AfterValidator(_refuse_reserved_characters),
]
"""A bridge or backend name: 1 to 100 characters without ``:`` or ``~``."""

SoftwareVersion = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=SoftwareReportLimits.MAX_VERSION_LENGTH),
    AfterValidator(_refuse_reserved_characters),
]
"""A bridge or backend version: 1 to 64 characters without ``:`` or ``~``."""


class BridgeReport(BaseModel):
    """Represents the ``bridge`` object of a pop: the program that pops jobs from the horde."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: SoftwareName
    """The bridge's name, such as ``AI Horde Worker``."""
    version: SoftwareVersion
    """The bridge's version, such as ``25.0.0``."""
    url: str | None = Field(default=None, max_length=SoftwareReportLimits.MAX_URL_LENGTH)
    """The URL where the bridge is published."""


class BackendReport(BaseModel):
    """Represents the ``backend`` object of a pop: the inference server that receives jobs from the bridge."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: SoftwareName
    """The backend's name, such as ``aphrodite`` or ``llamacpp``."""
    version: SoftwareVersion | None = None
    """The backend's version, when reported."""
    api: str | None = Field(default=None, max_length=SoftwareReportLimits.MAX_NAME_LENGTH)
    """The API dialect between bridge and backend, parsed by [`parse_backend_api`][horde.worker_software.parse_backend_api]."""


@dataclass(frozen=True, kw_only=True)
class WorkerSoftware:
    """Represents the bridge and backend reported by a worker on its last pop."""

    bridge_name: str
    """The bridge's name, or ``unknown`` when the report could not be parsed."""
    bridge_version: str | None = None
    """The bridge's version."""
    backend_name: str | None = None
    """The backend's name, or None when the worker did not report a backend separate from the bridge."""
    backend_version: str | None = None
    """The backend's version, when reported."""
    backend_api: BackendApi | None = None
    """The API dialect between bridge and backend, when reported and recognised."""


def parse_legacy_bridge_agent(bridge_agent: str | None) -> WorkerSoftware:
    """Parse a ``bridge_agent`` string of the form ``bridge[~backend[~api]]:version:url``.

    The string is validated the same way as in
    [`horde.bridge_reference.parse_bridge_agent`][horde.bridge_reference.parse_bridge_agent], which parses it for image
    capabilities. A string without three ``:``-separated fields, or with a version that is not a loose semantic version,
    resolves to the bridge ``unknown``. The name is then split on ``~``. An empty backend part becomes the backend
    ``unknown``, so ``bridge~~oai`` is not read as a bridge without a backend.

    Args:
        bridge_agent: The worker's ``bridge_agent`` string, or None.

    Returns:
        The parsed software.
    """
    unknown_software = WorkerSoftware(bridge_name=UNKNOWN_SOFTWARE_NAME)
    if not bridge_agent:
        return unknown_software
    agent_fields = bridge_agent.split(LEGACY_AGENT_FIELD_SEPARATOR, 2)
    if len(agent_fields) < 3:
        return unknown_software
    agent_name, bridge_version, _bridge_url = agent_fields
    try:
        semver.Version.parse(bridge_version, optional_minor_and_patch=True)
    except (ValueError, TypeError):
        return unknown_software
    name_parts = agent_name.split(LEGACY_NAME_SEPARATOR)
    has_backend_part = len(name_parts) > 1
    backend_name = (name_parts[1] or UNKNOWN_SOFTWARE_NAME) if has_backend_part else None
    backend_api = parse_backend_api(name_parts[2]) if len(name_parts) > 2 else None
    return WorkerSoftware(
        bridge_name=name_parts[0],
        bridge_version=bridge_version,
        backend_name=backend_name,
        backend_api=backend_api,
    )


def build_bridge_agent(bridge: BridgeReport) -> str:
    """Create the ``bridge_agent`` string for a structured bridge report.

    The string does not include the backend, so code that splits the name on ``~`` gets only the bridge name.

    Args:
        bridge: The pop's structured bridge report.

    Returns:
        ``name:version:url``, with ``unknown`` for a missing URL.
    """
    bridge_url = bridge.url or UNKNOWN_SOFTWARE_NAME
    return LEGACY_AGENT_FIELD_SEPARATOR.join((bridge.name, bridge.version, bridge_url))


def resolve_reported_software(
    *,
    bridge_agent: str | None,
    bridge: BridgeReport | None,
    backend: BackendReport | None,
) -> WorkerSoftware:
    """Return the software reported by a pop, preferring the structured fields to the ``bridge_agent`` string.

    Args:
        bridge_agent: The pop's ``bridge_agent``, parsed when ``bridge`` is absent.
        bridge: The pop's structured bridge, or None.
        backend: The pop's structured backend, or None. Ignored when ``bridge`` is None. The pop endpoint rejects a
            ``backend`` without a ``bridge`` before it resolves the report.

    Returns:
        The resolved software.
    """
    if bridge is None:
        return parse_legacy_bridge_agent(bridge_agent)
    if backend is None:
        return WorkerSoftware(bridge_name=bridge.name, bridge_version=bridge.version)
    return WorkerSoftware(
        bridge_name=bridge.name,
        bridge_version=bridge.version,
        backend_name=backend.name,
        backend_version=backend.version,
        backend_api=parse_backend_api(backend.api),
    )

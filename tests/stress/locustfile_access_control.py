# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Locust entry point for the shared key policy and linked-account scenario.

The scenario runs ``SharedKeyHolder``, ``SharedKeyOwner``, ``LinkedAccountFarmer`` and ``LinkReviewer`` (see
``locustsuite.users.access_control``) with fixed counts from the command line. The suite's ``test_start`` handler
parses the requestor keys, so ``locustsuite.events`` is imported before the handler below.

At test start the owner key creates one shared key limited to image generation with the given concurrency. The holders
submit through it while the owner suspends and reactivates it. The owner user also checks that the number of waiting
generations under the key stays at or below its limit plus one request per holder. A generation through a shared key
costs the owner's kudos, so give the key a small budget on a shared target.

Usage:
    locust -f tests/stress/locustfile_access_control.py --host https://dev.example \\
        --headless --users 10 --spawn-rate 2 --run-time 10m \\
        --requestor-api-keys R1,R2,R3 --horde-models "Some Model" \\
        --access-owner-api-key O --access-moderator-api-key M \\
        --access-max-concurrency 4 --access-key-kudos 2000 \\
        --access-holders 6 --access-owners 1 --access-farmers 2 --access-reviewers 1

The run passes when the Locust statistics do not show failures. Expected refusals are counted under separate
``[expected]`` names, and the concurrency check is counted under ``[invariant] sharedkey concurrency``.
"""

# ruff: noqa: I001
from __future__ import annotations

from locust import events

from locustsuite import events as _suite_events  # noqa: F401
from locustsuite.users.access_control import (
    LinkedAccountFarmer,
    LinkReviewer,
    SharedKeyHolder,
    SharedKeyOwner,
    configure_access_control,
)

__all__ = ["LinkReviewer", "LinkedAccountFarmer", "SharedKeyHolder", "SharedKeyOwner"]


@events.init_command_line_parser.add_listener
def _add_access_control_arguments(parser) -> None:
    group = parser.add_argument_group("AI Horde Shared Key And Linked Account Scenario")
    group.add_argument(
        "--access-owner-api-key",
        type=str,
        env_var="HORDE_ACCESS_OWNER_API_KEY",
        default="",
        help="API key of the account that owns the scenario's shared key and pays for its generations.",
    )
    group.add_argument(
        "--access-moderator-api-key",
        type=str,
        env_var="HORDE_ACCESS_MODERATOR_API_KEY",
        default="",
        help="API key of a moderator account. The reviewer users need it.",
    )
    group.add_argument(
        "--access-max-concurrency",
        type=int,
        env_var="HORDE_ACCESS_MAX_CONCURRENCY",
        default=4,
        help="The most waiting generations allowed at once under the scenario's shared key.",
    )
    group.add_argument(
        "--access-key-kudos",
        type=int,
        env_var="HORDE_ACCESS_KEY_KUDOS",
        default=2000,
        help="The kudos budget of the scenario's shared key.",
    )
    for flag, env_var, cls_name in (
        ("--access-holders", "HORDE_ACCESS_HOLDERS", "SharedKeyHolder"),
        ("--access-owners", "HORDE_ACCESS_OWNERS", "SharedKeyOwner"),
        ("--access-farmers", "HORDE_ACCESS_FARMERS", "LinkedAccountFarmer"),
        ("--access-reviewers", "HORDE_ACCESS_REVIEWERS", "LinkReviewer"),
    ):
        group.add_argument(flag, type=int, env_var=env_var, default=0, help=f"Concurrent {cls_name} users (0 means weight-based).")


@events.test_start.add_listener
def _configure(environment, **_kwargs) -> None:
    options = environment.parsed_options
    configure_access_control(environment)
    SharedKeyHolder.fixed_count = options.access_holders
    SharedKeyOwner.fixed_count = options.access_owners
    LinkedAccountFarmer.fixed_count = options.access_farmers
    LinkReviewer.fixed_count = options.access_reviewers

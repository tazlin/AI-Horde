# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A worker's ``priority_usernames`` name users as ``alias#id``; anything else is refused before it reaches the database."""

from __future__ import annotations

import pytest

from horde import exceptions as e
from horde.apis.v2.base import parse_priority_user_ids


def test_each_entry_yields_the_id_after_its_last_hash():
    assert parse_priority_user_ids(["db0#1", "odd#name#42", "other#0007"]) == [1, 42, 7]


@pytest.mark.parametrize("entries", [None, []])
def test_no_entries_name_no_users(entries):
    assert parse_priority_user_ids(entries) == []


def test_an_id_too_large_for_any_user_is_dropped():
    assert parse_priority_user_ids(["db0#1", "huge#2147483648", "max#2147483647"]) == [1, 2147483647]


@pytest.mark.parametrize(
    "entry",
    [
        "no-hash",
        "someone#",
        "#",
        "someone#abc",
        "someone#1;DROP TABLE users",
        "someone#-1",
        "someone# 1",
        "someone#1.0",
        "someone#١",
        7,
        None,
    ],
)
def test_an_entry_whose_id_is_not_a_decimal_number_is_refused(entry):
    with pytest.raises(e.BadRequest) as raised:
        parse_priority_user_ids(["db0#1", entry])

    assert raised.value.rc == "InvalidPriorityUsername"

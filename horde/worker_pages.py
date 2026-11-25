# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Describe the requests and the cursor of the paged worker lists.

`GET /v2/workers` returns only the active workers. `GET /v2/workers/page` lists every registered worker, active or
inactive, and `GET /v2/workers/mine` lists the caller's workers. Both paged lists read the database with a keyset query
in [`page_registered_workers`][horde.database.functions.page_registered_workers]. This module holds the parts of a
page request that do not touch the database: the orders, the filters, the views and the cursor.

A page boundary is a position in a sort order and not an offset. The cursor records the sort value and the worker ID
of the last row of a page, and the next page starts strictly after that pair. Workers joining or leaving between pages
therefore do not shift the rows of later pages. A worker's sort value can change during a walk, as uptime does at
each check-in, so a walk can skip or repeat a worker whose value moves it past the cursor. Page boundaries are best
effort.

Public members:

- [`WorkerListSort`][horde.worker_pages.WorkerListSort]: the available page orders.
- [`DEFAULT_WORKER_LIST_SORT`][horde.worker_pages.DEFAULT_WORKER_LIST_SORT]: the order used when a request does not
  set one.
- [`WorkerListView`][horde.worker_pages.WorkerListView]: the set of fields that a caller receives.
- [`WorkerPageFilters`][horde.worker_pages.WorkerPageFilters]: the filters in a request.
- [`WorkerPageCursor`][horde.worker_pages.WorkerPageCursor]: the encoded position between two pages.
- [`WorkerPage`][horde.worker_pages.WorkerPage]: the body of a page response.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypedDict, TypeGuard

__all__ = [
    "DEFAULT_WORKER_LIST_SORT",
    "InvalidWorkerCursorError",
    "WorkerListSort",
    "WorkerListView",
    "WorkerPage",
    "WorkerPageCursor",
    "WorkerPageFilters",
    "WorkerPageLimits",
]


class WorkerPageLimits:
    """Group the bounds of one page request."""

    DEFAULT_LIMIT: int = 25
    """Rows in a page when the request does not set a limit."""
    MAX_LIMIT: int = 100
    """The largest number of rows in one page.

    ``get_details`` runs once for each row, so the limit bounds the work of building a page. At 100 rows a walk over a
    few thousand workers still needs only a few dozen requests.
    """
    MAX_NAME_QUERY_CHARACTERS: int = 100
    """The longest ``name_contains`` value accepted, which is the longest worker name."""
    MAX_CURSOR_CHARACTERS: int = 1024
    """The longest cursor accepted.

    A name cursor holds the lower-cased name. A 100-character name is at most 400 bytes in UTF-8, and with the worker
    ID the cursor encodes to under 700 characters.
    """


class WorkerListView(StrEnum):
    """Represent the set of worker fields that a caller receives.

    The full list reads the public and moderator views from snapshots of the active workers. The paged lists build
    every view from the database for each request.
    """

    PUBLIC = "public"
    """The fields open to every caller. The owner and messages are included only for owners who made their
    workers public.
    """
    MODERATOR = "moderator"
    """Every field, including addresses, contacts, pause state and suspicion counts."""
    OWNER = "owner"
    """The fields of an owner's workers, including the address, contact and messages, without suspicion counts."""

    @property
    def snapshot_key(self) -> str:
        """Return the Redis key under which the snapshot job stores this view's list of active workers.

        Raises:
            ValueError: The view is the owner view, which is not stored in a snapshot.
        """
        if self is WorkerListView.MODERATOR:
            return "worker_cache_privileged"
        if self is WorkerListView.OWNER:
            raise ValueError("the owner view is built from the database and is not stored in a snapshot")
        return "worker_cache"

    @property
    def details_privilege(self) -> int:
        """Return the ``details_privilege`` argument of ``WorkerTemplate.get_details`` that builds this view's rows."""
        if self is WorkerListView.MODERATOR:
            return 2
        if self is WorkerListView.OWNER:
            return 1
        return 0


class WorkerListSort(StrEnum):
    """Represent the orders available for a worker page.

    Every order breaks ties by worker ID, ascending. Each order is therefore total, and a cursor points to exactly one
    position.
    """

    UPTIME = "uptime"
    """Longest total uptime first."""
    REQUESTS_FULFILLED = "requests_fulfilled"
    """Most jobs completed first."""
    KUDOS_REWARDS = "kudos_rewards"
    """Most kudos earned first."""
    NAME = "name"
    """Worker name ascending, ignoring case."""

    @property
    def descending(self) -> bool:
        """Return whether the order puts larger sort values first."""
        return self is not WorkerListSort.NAME


DEFAULT_WORKER_LIST_SORT: WorkerListSort = WorkerListSort.UPTIME
"""The order of a page when the request does not set one.

Workers with a long service record come first. Names chosen to sort first alphabetically do not crowd the first page.
"""

SortValue = str | int
"""The first element of a sort key: a lower-cased name, or a count."""

_LARGEST_COUNT: int = 2**63 - 1
"""The largest value of the 64-bit integer columns that hold the counts."""


class InvalidWorkerCursorError(ValueError):
    """Raised when a cursor was not produced by this module or belongs to another sort order."""


@dataclass(frozen=True)
class WorkerPageCursor:
    """Represents the position after the last row of a page.

    The encoded form is URL-safe base64 of a small JSON document, which clients treat as opaque. The cursor holds only
    values that are visible in the page, so it is not signed. A crafted cursor only starts a walk at a position chosen
    by the caller.
    """

    FORMAT_VERSION = 1
    """The version of the encoded form. A cursor of another version is refused, so the form can change later."""

    sort: WorkerListSort
    """The order of the page that the cursor follows. A cursor continues a walk only in the same order."""
    sort_value: SortValue
    """The last row's value of the sort column, as read from the database."""
    worker_id: str
    """The ID of the last row, which breaks ties in ``sort_value``."""

    def encode(self) -> str:
        """Return the opaque string that a client passes back as ``cursor``."""
        document = {"v": self.FORMAT_VERSION, "s": self.sort.value, "k": self.sort_value, "i": self.worker_id}
        serialized = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.urlsafe_b64encode(serialized).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, encoded: str, *, expected_sort: WorkerListSort) -> WorkerPageCursor:
        """Parse a cursor passed back by a client and check that it continues a walk in ``expected_sort``.

        Args:
            encoded: The ``next_cursor`` of an earlier page.
            expected_sort: The order named in the current request.

        Returns:
            The decoded position.

        Raises:
            InvalidWorkerCursorError: The cursor is too long, malformed, of another version, of another sort order,
                or holds a value of the wrong type or range for its order.
        """
        if len(encoded) > WorkerPageLimits.MAX_CURSOR_CHARACTERS:
            raise InvalidWorkerCursorError("cursor is longer than the longest cursor issued by this server")
        padding = "=" * (-len(encoded) % 4)
        try:
            document = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError, ValueError) as err:
            raise InvalidWorkerCursorError("cursor was not issued by this server") from err
        if not isinstance(document, dict) or document.get("v") != cls.FORMAT_VERSION:
            raise InvalidWorkerCursorError("cursor was not issued by this server")
        if document.get("s") != expected_sort.value:
            raise InvalidWorkerCursorError("cursor continues a walk in another sort order")
        worker_id = document.get("i")
        sort_value = document.get("k")
        if not isinstance(worker_id, str) or not _is_uuid(worker_id):
            raise InvalidWorkerCursorError("cursor does not hold a valid worker ID")
        if not _sort_value_fits(sort_value, expected_sort):
            raise InvalidWorkerCursorError("cursor holds a value of the wrong type or range for its sort order")
        return cls(sort=expected_sort, sort_value=sort_value, worker_id=worker_id)


def _is_uuid(candidate: str) -> bool:
    """Return whether ``candidate`` parses as a UUID."""
    try:
        uuid.UUID(candidate)
    except ValueError:
        return False
    return True


def _sort_value_fits(candidate: object, sort: WorkerListSort) -> TypeGuard[SortValue]:
    """Return whether ``candidate`` has the type and range of a sort column value under ``sort``.

    The value is bound into the keyset query, so a count outside the range of a 64-bit column would fail in the
    database instead of being refused here.
    """
    if sort is WorkerListSort.NAME:
        return isinstance(candidate, str)
    if not isinstance(candidate, int) or isinstance(candidate, bool):
        return False
    return -_LARGEST_COUNT - 1 <= candidate <= _LARGEST_COUNT


@dataclass(frozen=True)
class WorkerPageFilters:
    """Represents the filters in a request. A field left as None does not narrow the list.

    The database query applies the filters with the same visibility as the caller's view. A filter on the owner
    matches, in the public view, only the workers of owners who made their workers public. A filter therefore cannot
    reveal an owner hidden from the caller.
    """

    worker_type: str | None = None
    """Only workers of this type: image, text or interrogation."""
    model: str | None = None
    """Only workers advertising this model name, compared exactly."""
    team_id: str | None = None
    """Only workers in the team with this ID."""
    owner_id: int | None = None
    """Only workers owned by this account."""
    name_contains: str | None = None
    """Only workers whose name contains this text, ignoring case."""
    online: bool | None = None
    """Only workers that are online (True) or offline (False)."""

    @property
    def applied_names(self) -> list[str]:
        """Return the names of the filters in use, in a fixed order, for a bounded metric label."""
        candidate_filters = {
            "type": self.worker_type,
            "model": self.model,
            "team": self.team_id,
            "owner": self.owner_id,
            "name": self.name_contains,
            "online": self.online,
        }
        return [filter_name for filter_name, filter_value in candidate_filters.items() if filter_value is not None]


class WorkerPage(TypedDict):
    """Represents one page of a worker list."""

    workers: list[Mapping[str, Any]]
    """The page's worker details rows, in the requested order."""
    next_cursor: str | None
    """The cursor of the next page, or None on the last page."""
    total: int
    """How many registered workers pass the filters, across all pages, when this page was read."""

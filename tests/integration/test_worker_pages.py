# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Walk ``GET /v2/workers/page`` and ``GET /v2/workers/mine`` the way front ends, worker owners and moderators do.

Workers register by popping for jobs, and owners change their settings through the API. Both paged lists read the
registered workers from the database, so the other tests in the session add workers of their own. Each test narrows
its pages to its own workers with a filter. Each test states a behaviour that callers depend on.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE

from tests.integration.test_maintenance_threads import (
    FEW_SUSPICION_ROWS,
    INTERROGATION_PERFORMANCE_ROWS,
    MANY_SUSPICION_ROWS,
    _add_suspicion_rows,
    _create_listed_worker,
)
from tests.unit.model_reference_seed import seed_image_reference

AGENT = "aihorde_ci_client:1.0:(test)ci"
TEXT_MODEL = "elinas/chronos-70b-v2"
IMAGE_MODEL = "stable_diffusion"
WORKER_AGENT = "AI Horde Worker reGen:9.0.1-citests:https://github.com/Haidra-Org/horde-worker-reGen"
COUNT_COLUMNS_BY_SORT = {"uptime": "uptime", "requests_fulfilled": "fulfilments", "kudos_rewards": "kudos"}


@pytest.fixture(autouse=True)
def _unlimited_requests() -> Iterator[None]:
    """Lift the per-IP limits, because a walk here makes more requests a minute than one client in production would."""
    from horde.limiter import limiter

    enabled_before = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = enabled_before


@pytest.fixture(autouse=True)
def _known_image_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the image reference so an image worker can register without reaching the model reference service."""
    seed_image_reference(monkeypatch, {IMAGE_MODEL: KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1})


def _headers(api_key: str | None = None) -> dict[str, str]:
    headers = {"Client-Agent": AGENT}
    if api_key is not None:
        headers["apikey"] = api_key
    return headers


def _unique_name(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


def _run_snapshot_job() -> None:
    """Run the quorum node's snapshot job, which runs every two seconds in production and builds the full list."""
    from horde.database.threads import store_worker_list

    store_worker_list()


def _register_text_worker(client: Any, owner_key: str, name: str) -> str:
    """Register a text worker the way the worker software does, by popping for a job, and return its ID."""
    response = client.post(
        "/api/v2/generate/text/pop",
        json={
            "name": name,
            "models": [TEXT_MODEL],
            "bridge_agent": AGENT,
            "amount": 1,
            "max_context_length": 4096,
            "max_length": 512,
        },
        headers=_headers(owner_key),
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return _worker_id_by_name(client, name)


def _register_image_worker(client: Any, owner_key: str, name: str) -> str:
    """Register an image worker by popping for a job, and return its ID."""
    response = client.post(
        "/api/v2/generate/pop",
        json={
            "name": name,
            "models": [IMAGE_MODEL],
            "bridge_agent": WORKER_AGENT,
            "nsfw": True,
            "amount": 1,
            "max_pixels": 262144,
            "allow_unsafe_ipaddr": True,
        },
        headers=_headers(owner_key),
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return _worker_id_by_name(client, name)


def _worker_id_by_name(client: Any, name: str) -> str:
    response = client.get(f"/api/v2/workers/name/{name}", headers=_headers())
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["id"]


def _set_last_check_in(app: Any, worker_id: str, *, minutes_ago: int) -> None:
    """Move a worker's last check-in back, as if it stopped popping for jobs that long ago."""
    _set_worker_fields(app, worker_id, last_check_in=datetime.utcnow() - timedelta(minutes=minutes_ago))


def _set_worker_fields(app: Any, worker_id: str, **fields: Any) -> None:
    """Change stored fields of a worker, as the horde does when the worker completes jobs or changes hands."""
    from horde.classes.base.worker import WorkerTemplate
    from horde.flask import db

    with app.app_context():
        worker = db.session.get(WorkerTemplate, uuid.UUID(worker_id))
        for field_name, value in fields.items():
            setattr(worker, field_name, value)
        db.session.commit()


def _delete_worker(app: Any, worker_id: str) -> None:
    """Delete a worker, as its owner or a moderator does through the API."""
    from horde.classes.base.worker import WorkerTemplate
    from horde.flask import db

    with app.app_context():
        db.session.get(WorkerTemplate, uuid.UUID(worker_id)).delete()


def _forget_cached_worker_details(worker_id: str) -> None:
    """Drop the cached details of one worker, which ``GET /v2/workers/{id}`` keeps for 30 seconds after a read."""
    from horde import horde_redis as horde_redis_module

    redis_connection = horde_redis_module.horde_redis
    for cache_key in (f"cached_worker_{worker_id}", f"cached_worker_{worker_id}_privileged"):
        redis_connection.horde_r.delete(cache_key)
        if redis_connection.horde_local_r is not None:
            redis_connection.horde_local_r.delete(cache_key)


def _get_page(client: Any, *, api_key: str | None = None, path: str = "/api/v2/workers/page", **query: Any) -> Any:
    return client.get(path, query_string=query, headers=_headers(api_key))


def _get_mine(client: Any, api_key: str, **query: Any) -> Any:
    return _get_page(client, api_key=api_key, path="/api/v2/workers/mine", **query)


def _walk(
    client: Any,
    *,
    api_key: str | None = None,
    path: str = "/api/v2/workers/page",
    between_pages: Callable[[int], None] | None = None,
    **query: Any,
) -> list[dict[str, Any]]:
    """Return every page body of a walk, following next_cursor until it is null."""
    bodies: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(200):
        if bodies and between_pages is not None:
            between_pages(len(bodies))
        page_query = dict(query)
        if cursor is not None:
            page_query["cursor"] = cursor
        response = _get_page(client, api_key=api_key, path=path, **page_query)
        assert response.status_code == 200, response.get_data(as_text=True)
        body = response.get_json()
        bodies.append(body)
        cursor = body["next_cursor"]
        if cursor is None:
            return bodies
    raise AssertionError("the walk did not end")


def _walked_rows(bodies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for body in bodies for row in body["workers"]]


def _walked_ids(bodies: list[dict[str, Any]]) -> list[str]:
    return [row["id"] for row in _walked_rows(bodies)]


class TestListingRegisteredWorkers:
    """The paged list includes every registered worker, including workers that stopped checking in long ago."""

    @pytest.mark.parametrize("as_moderator", [False, True], ids=["public", "moderator"])
    def test_an_inactive_worker_is_listed_with_the_fields_of_its_own_details(
        self,
        app,
        client,
        make_api_user,
        as_moderator,
    ) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        caller_key = make_api_user(moderator=True).api_key if as_moderator else None
        token = uuid.uuid4().hex[:8]
        worker_id = _register_text_worker(client, owner.api_key, f"Retired {token} Scribe")
        _set_last_check_in(app, worker_id, minutes_ago=60 * 24 * 30)
        _forget_cached_worker_details(worker_id)

        rows = _get_page(client, api_key=caller_key, name_contains=token).get_json()["workers"]
        details = client.get(f"/api/v2/workers/{worker_id}", headers=_headers(caller_key)).get_json()

        assert [row["id"] for row in rows] == [worker_id]
        assert rows[0]["online"] is False
        assert rows[0] == details

    @pytest.mark.parametrize("as_moderator", [False, True], ids=["public", "moderator"])
    def test_the_online_filter_returns_the_rows_of_the_full_list(self, app, client, make_api_user, as_moderator) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        caller_key = make_api_user(moderator=True).api_key if as_moderator else None
        token = uuid.uuid4().hex[:8]
        for _ in range(3):
            _register_text_worker(client, owner.api_key, _unique_name(f"Parity {token} Scribe"))
        _register_image_worker(client, owner.api_key, _unique_name(f"Parity {token} Painter"))
        offline_id = _register_text_worker(client, owner.api_key, _unique_name(f"Parity {token} Sleeper"))
        _set_last_check_in(app, offline_id, minutes_ago=60)
        _run_snapshot_job()

        full_list = client.get("/api/v2/workers", headers=_headers(caller_key)).get_json()
        walked_rows = _walked_rows(_walk(client, api_key=caller_key, name_contains=token, online="true", limit=2))

        expected_rows = [row for row in full_list if token in row["name"]]
        assert len(expected_rows) == 4
        assert sorted(walked_rows, key=lambda row: row["id"]) == sorted(expected_rows, key=lambda row: row["id"])

    def test_the_full_list_returns_a_bare_array(self, client, make_api_user) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        name = _unique_name("Legacy Scribe")
        _register_text_worker(client, owner.api_key, name)
        _run_snapshot_job()

        response = client.get("/api/v2/workers", query_string={"name": name.upper()}, headers=_headers())

        assert response.status_code == 200
        assert [row["name"] for row in response.get_json()] == [name]

    @pytest.mark.parametrize("as_moderator", [False, True], ids=["public", "moderator"])
    def test_only_moderator_rows_of_the_full_list_are_kept_out_of_shared_caches(self, client, make_api_user, as_moderator) -> None:
        caller_key = make_api_user(moderator=True).api_key if as_moderator else None

        response = client.get("/api/v2/workers", headers=_headers(caller_key))

        assert response.status_code == 200
        if as_moderator:
            assert response.headers["Cache-Control"] == "private, no-store"
        else:
            assert "private" not in response.headers.get("Cache-Control", "")


class TestWalkingWhileWorkersComeAndGo:
    """Workers register, stop checking in and are deleted while a client walks the list."""

    def test_workers_joining_and_leaving_between_pages_do_not_shift_the_rest_of_the_walk(self, app, client, make_api_user) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        prefix = f"Churn {uuid.uuid4().hex[:6]}"
        worker_ids = [_register_text_worker(client, owner.api_key, f"{prefix} {index:02d}") for index in range(8)]
        state: dict[str, str] = {}

        def workers_change_between_pages(pages_read: int) -> None:
            if pages_read != 1:
                return
            # One listed worker stops checking in and one unlisted worker is deleted. One new worker sorts before the
            # cursor and one after it.
            _set_last_check_in(app, worker_ids[1], minutes_ago=10)
            _delete_worker(app, worker_ids[6])
            state["joined_behind"] = _register_text_worker(client, owner.api_key, f"{prefix} 00a")
            state["joined_ahead"] = _register_text_worker(client, owner.api_key, f"{prefix} 99")

        walked_ids = _walked_ids(
            _walk(client, between_pages=workers_change_between_pages, name_contains=prefix, sort="name", limit=3),
        )

        assert len(walked_ids) == len(set(walked_ids)), "a worker was listed twice"
        assert set(worker_ids) - {worker_ids[6]} <= set(walked_ids), "a worker that was not renamed or deleted was skipped"
        assert worker_ids[6] not in walked_ids, "a worker deleted before its page was still listed"
        assert state["joined_ahead"] in walked_ids, "a worker that joined ahead of the cursor was not listed"
        assert state["joined_behind"] not in walked_ids, "a worker that joined behind the cursor is listed only by the next walk"

    def test_a_page_after_the_last_remaining_worker_is_empty_and_still_reports_the_total(self, app, client, make_api_user) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        prefix = f"Tail {uuid.uuid4().hex[:6]}"
        worker_ids = [_register_text_worker(client, owner.api_key, f"{prefix} {index}") for index in range(3)]
        first_page = _get_page(client, name_contains=prefix, sort="name", limit=2).get_json()
        _delete_worker(app, worker_ids[2])

        next_page = _get_page(client, name_contains=prefix, sort="name", limit=2, cursor=first_page["next_cursor"]).get_json()

        assert [row["id"] for row in first_page["workers"]] == worker_ids[:2]
        assert next_page == {"workers": [], "next_cursor": None, "total": 2}


class TestOrders:
    """Every order is total, so a walk one row at a time visits tied workers by ascending ID."""

    def test_without_a_sort_the_workers_with_the_longest_uptime_come_first(self, app, client, make_api_user) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        newcomer_id = _register_text_worker(client, owner.api_key, f"Able {token} Newcomer")
        veteran_id = _register_text_worker(client, owner.api_key, f"Baker {token} Veteran")
        regular_id = _register_text_worker(client, owner.api_key, f"Charlie {token} Regular")
        _set_worker_fields(app, newcomer_id, uptime=10)
        _set_worker_fields(app, veteran_id, uptime=5000)
        _set_worker_fields(app, regular_id, uptime=300)

        rows = _get_page(client, name_contains=token).get_json()["workers"]

        assert [row["id"] for row in rows] == [veteran_id, regular_id, newcomer_id]

    @pytest.mark.parametrize("sort", list(COUNT_COLUMNS_BY_SORT))
    def test_a_count_order_descends_and_breaks_ties_by_ascending_id(self, app, client, make_api_user, sort) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        worker_ids = [_register_text_worker(client, owner.api_key, f"Tied {token} {index}") for index in range(4)]
        for worker_id, count in zip(worker_ids, [200, 100, 100, 50], strict=True):
            _set_worker_fields(app, worker_id, **{COUNT_COLUMNS_BY_SORT[sort]: count})

        walked_ids = _walked_ids(_walk(client, name_contains=token, sort=sort, limit=1))

        assert walked_ids == [worker_ids[0], *sorted(worker_ids[1:3]), worker_ids[3]]

    def test_the_name_order_ascends_ignoring_case(self, client, make_api_user) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        beta_id = _register_text_worker(client, owner.api_key, f"beta {token}")
        alpha_id = _register_text_worker(client, owner.api_key, f"Alpha {token}")
        gamma_id = _register_text_worker(client, owner.api_key, f"GAMMA {token}")

        walked_ids = _walked_ids(_walk(client, name_contains=token, sort="name", limit=1))

        assert walked_ids == [alpha_id, beta_id, gamma_id]

    def test_ordering_by_suspicion_is_refused(self, client, make_api_user) -> None:
        moderator = make_api_user(moderator=True)

        response = _get_page(client, api_key=moderator.api_key, sort="suspicious")

        assert response.status_code == 400


class TestOwnerPrivacy:
    """Owners decide whether the public may see which workers are theirs."""

    def test_the_public_cannot_find_a_private_owners_workers_by_owner(self, client, make_api_user) -> None:
        private_owner = make_api_user(trusted=True, kudos=100)
        _register_text_worker(client, private_owner.api_key, _unique_name("Private Scribe"))

        body = _get_page(client, owner_id=private_owner.id).get_json()

        assert body["workers"] == []
        assert body["total"] == 0, "the count alone would reveal how many workers the account runs"

    def test_an_owner_who_publishes_their_workers_is_found_by_owner(self, client, make_api_user) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        worker_id = _register_text_worker(client, owner.api_key, _unique_name("Public Scribe"))
        response = client.put(f"/api/v2/users/{owner.id}", json={"public_workers": True}, headers=_headers(owner.api_key))
        assert response.status_code == 200, response.get_data(as_text=True)

        body = _get_page(client, owner_id=owner.id).get_json()

        assert [row["id"] for row in body["workers"]] == [worker_id]
        assert body["workers"][0]["owner"] == owner.alias

    def test_a_moderator_finds_a_private_owners_workers_with_addresses_and_no_caching(self, client, make_api_user) -> None:
        private_owner = make_api_user(trusted=True, kudos=100)
        moderator = make_api_user(moderator=True)
        worker_id = _register_text_worker(client, private_owner.api_key, _unique_name("Reviewed Scribe"))

        response = _get_page(client, api_key=moderator.api_key, owner_id=private_owner.id)

        assert [row["id"] for row in response.get_json()["workers"]] == [worker_id]
        assert "ipaddr" in response.get_json()["workers"][0]
        assert response.headers["Cache-Control"] == "private, no-store"

    def test_a_public_page_does_not_contain_moderator_fields(self, client, make_api_user) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        _register_text_worker(client, owner.api_key, f"Plain {token} Scribe")

        rows = _walked_rows(_walk(client, api_key=owner.api_key, name_contains=token))

        assert rows
        assert not any({"ipaddr", "contact", "suspicious", "paused"} & row.keys() for row in rows)

    def test_a_mistyped_key_is_refused_rather_than_read_as_the_public_view(self, client) -> None:
        response = _get_page(client, api_key="not-a-real-key")

        assert response.status_code == 401
        assert response.get_json()["rc"] == "InvalidAPIKey"


class TestFindingWorkers:
    """Front ends narrow the list to find the workers serving a model, the workers in a team or a remembered name."""

    def test_the_model_and_type_filters_find_the_workers_serving_a_model(self, client, make_api_user) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        moderator = make_api_user(moderator=True)
        text_worker_id = _register_text_worker(client, owner.api_key, _unique_name("Model Scribe"))
        image_worker_id = _register_image_worker(client, owner.api_key, _unique_name("Model Painter"))

        by_model = _get_page(client, api_key=moderator.api_key, owner_id=owner.id, model=IMAGE_MODEL).get_json()
        by_type = _get_page(client, api_key=moderator.api_key, owner_id=owner.id, type="text").get_json()

        assert [row["id"] for row in by_model["workers"]] == [image_worker_id]
        assert [row["id"] for row in by_type["workers"]] == [text_worker_id]

    def test_the_team_filter_follows_an_owner_moving_a_worker_into_a_team(self, client, make_api_user) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        in_team_id = _register_text_worker(client, owner.api_key, _unique_name("Team Scribe"))
        _register_text_worker(client, owner.api_key, _unique_name("Solo Scribe"))
        team_response = client.post("/api/v2/teams", json={"name": _unique_name("Pager Team")}, headers=_headers(owner.api_key))
        assert team_response.status_code == 200, team_response.get_data(as_text=True)
        team_id = team_response.get_json()["id"]
        join_response = client.put(f"/api/v2/workers/{in_team_id}", json={"team": team_id}, headers=_headers(owner.api_key))
        assert join_response.status_code == 200, join_response.get_data(as_text=True)

        body = _get_page(client, team_id=team_id).get_json()

        assert [row["id"] for row in body["workers"]] == [in_team_id]

    def test_a_blank_text_filter_does_not_narrow_the_list(self, client, make_api_user) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        _register_text_worker(client, owner.api_key, _unique_name("Blank Filter Scribe"))

        unfiltered = _get_page(client, limit=100).get_json()
        blank_filtered = _get_page(client, limit=100, model="", team_id=" ", name_contains=" ").get_json()

        assert blank_filtered["total"] == unfiltered["total"] > 0

    def test_a_name_search_lists_the_workers_whose_name_contains_the_text_in_any_case(self, client, make_api_user) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        first_match_id = _register_text_worker(client, owner.api_key, f"Lantern{token} Scribe")
        second_match_id = _register_text_worker(client, owner.api_key, f"Old {token.upper()} Scribe")
        _register_text_worker(client, owner.api_key, _unique_name("Candle Scribe"))

        body = _get_page(client, name_contains=token.upper(), sort="name").get_json()

        assert {row["id"] for row in body["workers"]} == {first_match_id, second_match_id}
        assert body["total"] == 2

    # The search runs as a SQL LIKE match. Unescaped, "%" matches any text and "_" any one character.
    def test_a_name_search_matches_percent_and_underscore_as_plain_characters(self, app, client, make_api_user) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        literal_id = _register_text_worker(client, owner.api_key, _unique_name("Literal Scribe"))
        lookalike_id = _register_text_worker(client, owner.api_key, _unique_name("Lookalike Scribe"))
        _set_worker_fields(app, literal_id, name=f"{token} 100%_done")
        _set_worker_fields(app, lookalike_id, name=f"{token} 100xydone")

        body = _get_page(client, name_contains="0%_d").get_json()

        assert [row["id"] for row in body["workers"] if token in row["name"]] == [literal_id]


class TestRefusedRequests:
    """A malformed request is refused with a return code that a client can branch on, instead of a wrong page."""

    @pytest.mark.parametrize("limit", [0, 101, -5])
    def test_a_page_size_outside_the_range_is_refused(self, client, limit) -> None:
        response = _get_page(client, limit=limit)

        assert response.status_code == 400
        assert response.get_json()["rc"] == "InvalidWorkerListLimit"

    def test_a_cursor_reused_with_another_sort_is_refused(self, client, make_api_user) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        for _ in range(3):
            _register_text_worker(client, owner.api_key, _unique_name("Cursor Scribe"))
        name_cursor = _get_page(client, limit=1, sort="name").get_json()["next_cursor"]

        response = _get_page(client, limit=1, sort="uptime", cursor=name_cursor)

        assert response.status_code == 400
        assert response.get_json()["rc"] == "InvalidWorkerListCursor"

    @pytest.mark.parametrize(
        "query",
        [{"team_id": "not-a-uuid"}, {"owner_id": -1}, {"name_contains": "x" * 101}],
        ids=["team-not-uuid", "negative-owner", "name-search-longer-than-a-name"],
    )
    def test_a_malformed_filter_is_refused(self, client, query) -> None:
        response = _get_page(client, **query)

        assert response.status_code == 400
        assert response.get_json()["rc"] == "InvalidWorkerListFilter"


class TestSnapshotOutage:
    """When the snapshot job stops, each node rebuilds the full list from the database and must store the rebuilt list."""

    def test_a_missing_snapshot_is_rebuilt_once_and_kept_even_when_a_worker_has_a_message(
        self,
        client,
        make_api_user,
        monkeypatch,
    ) -> None:
        import horde.database.functions as database_functions
        from horde import horde_redis as horde_redis_module

        owner = make_api_user(trusted=True, kudos=100)
        moderator = make_api_user(moderator=True)
        worker_id = _register_text_worker(client, owner.api_key, _unique_name("Messaged Scribe"))
        message_response = client.post(
            "/api/v2/workers/messages",
            json={"worker_id": worker_id, "message": "Please update your worker", "expiry": 12},
            headers=_headers(moderator.api_key),
        )
        assert message_response.status_code == 200, message_response.get_data(as_text=True)
        misses = _RecordingCounter()
        monkeypatch.setattr(database_functions, "worker_list_snapshot_misses", misses)
        horde_redis_module.horde_redis.horde_r.delete("worker_cache_privileged")
        horde_redis_module.horde_redis.horde_local_r.delete("worker_cache_privileged")

        first = client.get("/api/v2/workers", headers=_headers(moderator.api_key)).get_json()
        second = client.get("/api/v2/workers", headers=_headers(moderator.api_key)).get_json()

        [messaged_row] = [row for row in first if row["id"] == worker_id]
        assert messaged_row["messages"][0]["message"] == "Please update your worker"
        assert second == first
        assert misses.additions == [(1, {"horde.workers.list.view": "moderator"})], "the rebuilt list was not kept"

    def test_a_rebuilt_list_expires_with_the_local_cache_so_the_recovered_snapshot_takes_over(self, client) -> None:
        from horde import horde_redis as horde_redis_module

        redis_connection = horde_redis_module.horde_redis
        redis_connection.horde_r.delete("worker_cache")
        redis_connection.horde_local_r.delete("worker_cache")

        response = client.get("/api/v2/workers", headers=_headers())

        assert response.status_code == 200
        assert 0 < redis_connection.horde_local_r.ttl("worker_cache") <= horde_redis_module.LOCAL_CACHE_MAX_LIFETIME.total_seconds()


class _RecordingCounter:
    """Stand in for a metric counter or histogram and keep what was recorded."""

    def __init__(self) -> None:
        self.additions: list[tuple[int, dict[str, str]]] = []

    def add(self, amount: int, attributes: dict[str, str]) -> None:
        self.additions.append((amount, attributes))

    def record(self, amount: int, attributes: dict[str, str]) -> None:
        self.additions.append((amount, attributes))


class TestObservability:
    """Operators read the worker list traffic by endpoint, view, sort, filters and walk position."""

    def test_reads_are_counted_by_endpoint_and_walk_position_with_bounded_labels(self, client, make_api_user, monkeypatch) -> None:
        import horde.metrics as metrics_module

        owner = make_api_user(trusted=True, kudos=100)
        for _ in range(2):
            _register_text_worker(client, owner.api_key, _unique_name("Counted Scribe"))
        _run_snapshot_job()
        reads = _RecordingCounter()
        rows = _RecordingCounter()
        monkeypatch.setattr(metrics_module, "worker_list_reads", reads)
        monkeypatch.setattr(metrics_module, "worker_list_rows", rows)
        free_text_model = f"model {uuid.uuid4().hex}"

        client.get("/api/v2/workers", headers=_headers())
        first_page = _get_page(client, limit=1).get_json()
        _get_page(client, limit=1, cursor=first_page["next_cursor"])
        _get_page(client, limit=1, model=free_text_model)

        labels = [attributes for _, attributes in reads.additions]
        assert [(label["horde.workers.list.endpoint"], label["horde.workers.list.continued"]) for label in labels] == [
            ("full", "false"),
            ("page", "false"),
            ("page", "true"),
            ("page", "false"),
        ]
        assert labels[3]["horde.workers.list.filters"] == "model"
        assert not any(free_text_model in str(label) for label in labels), "a client-chosen value reached a label"
        assert [amount for amount, _ in rows.additions][1:] == [1, 1, 0]


class TestOwnersListingTheirOwnWorkers:
    """Owners check on every worker they run, including private workers and workers that dropped out."""

    def test_an_owner_lists_their_private_and_offline_workers_with_addresses(self, app, client, make_api_user) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        stranger = make_api_user(trusted=True, kudos=100)
        online_id = _register_text_worker(client, owner.api_key, _unique_name("Home Scribe"))
        offline_id = _register_text_worker(client, owner.api_key, _unique_name("Stalled Scribe"))
        _register_text_worker(client, stranger.api_key, _unique_name("Neighbour Scribe"))
        _set_last_check_in(app, offline_id, minutes_ago=60)

        response = _get_mine(client, owner.api_key)

        rows = {row["id"]: row for row in response.get_json()["workers"]}
        assert set(rows) == {online_id, offline_id}
        assert rows[online_id]["online"] is True
        assert rows[offline_id]["online"] is False
        assert all("ipaddr" in row and row["owner"] == owner.alias for row in rows.values())
        assert response.headers["Cache-Control"] == "private, no-store"

    @pytest.mark.parametrize("online", [True, False])
    def test_the_online_filter_lists_only_the_workers_in_that_state(self, app, client, make_api_user, online) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        online_id = _register_text_worker(client, owner.api_key, _unique_name("Running Scribe"))
        offline_id = _register_text_worker(client, owner.api_key, _unique_name("Stopped Scribe"))
        _set_last_check_in(app, offline_id, minutes_ago=60)

        body = _get_mine(client, owner.api_key, online=str(online).lower()).get_json()

        assert [row["id"] for row in body["workers"]] == [online_id if online else offline_id]

    def test_a_walk_lists_every_owned_worker_once(self, client, make_api_user) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        worker_ids = {_register_text_worker(client, owner.api_key, _unique_name("Fleet Scribe")) for _ in range(5)}

        bodies = _walk(client, api_key=owner.api_key, path="/api/v2/workers/mine", limit=2)

        assert sorted(_walked_ids(bodies)) == sorted(worker_ids)
        assert [body["total"] for body in bodies] == [5, 5, 5], "every page of a walk counts the whole list"

    def test_a_worker_shows_under_its_new_owner_at_once(self, app, client, make_api_user) -> None:
        previous_owner = make_api_user(trusted=True, kudos=100)
        new_owner = make_api_user(trusted=True, kudos=100)
        worker_id = _register_text_worker(client, previous_owner.api_key, _unique_name("Handed Scribe"))

        _set_worker_fields(app, worker_id, user_id=new_owner.id)

        assert [row["id"] for row in _get_mine(client, new_owner.api_key).get_json()["workers"]] == [worker_id]
        assert _get_mine(client, previous_owner.api_key).get_json()["total"] == 0

    def test_the_statement_count_does_not_grow_with_the_number_of_owned_workers(self, client, make_api_user, assert_query_count) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        _register_text_worker(client, owner.api_key, _unique_name("First Scribe"))
        _register_image_worker(client, owner.api_key, _unique_name("First Painter"))
        _get_mine(client, owner.api_key)
        with assert_query_count() as few_workers:
            assert _get_mine(client, owner.api_key).get_json()["total"] == 2
        for _ in range(2):
            _register_text_worker(client, owner.api_key, _unique_name("Later Scribe"))
        _register_image_worker(client, owner.api_key, _unique_name("Later Painter"))
        _get_mine(client, owner.api_key)

        with assert_query_count() as many_workers:
            assert _get_mine(client, owner.api_key).get_json()["total"] == 5

        assert len(many_workers.of_kind("SELECT")) == len(few_workers.of_kind("SELECT")), many_workers.describe()

    def test_a_moderator_gets_only_the_workers_of_their_account(self, client, make_api_user) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        moderator = make_api_user(moderator=True)
        _register_text_worker(client, owner.api_key, _unique_name("Watched Scribe"))

        body = _get_mine(client, moderator.api_key).get_json()

        assert body == {"workers": [], "next_cursor": None, "total": 0}

    def test_a_mistyped_key_is_refused(self, client) -> None:
        response = _get_mine(client, "not-a-real-key")

        assert response.status_code == 401
        assert response.get_json()["rc"] == "InvalidAPIKey"

    def test_reads_are_counted_under_their_own_endpoint_and_view(self, client, make_api_user, monkeypatch) -> None:
        import horde.metrics as metrics_module

        owner = make_api_user(trusted=True, kudos=100)
        reads = _RecordingCounter()
        monkeypatch.setattr(metrics_module, "worker_list_reads", reads)

        _get_mine(client, owner.api_key, online="false")

        [(_, labels)] = reads.additions
        assert labels["horde.workers.list.endpoint"] == "mine"
        assert labels["horde.workers.list.view"] == "owner"
        assert labels["horde.workers.list.filters"] == "online"


class TestSuspicionCountsOnPages:
    """A page reports suspicion counts from SQL, so a worker with many suspicion rows costs no more than any other."""

    def test_a_moderator_page_reports_each_workers_suspicion_row_count(self, app, client, make_api_user) -> None:
        from horde.classes.kobold.worker import TextWorker
        from horde.classes.stable.interrogation_worker import InterrogationWorker
        from horde.classes.stable.worker import ImageWorker

        owner = make_api_user(trusted=True)
        moderator = make_api_user(moderator=True)
        expected_counts: dict[str, int] = {}
        with app.app_context():
            for worker_class, suspicion_rows in [
                (ImageWorker, 0),
                (ImageWorker, MANY_SUSPICION_ROWS),
                (TextWorker, FEW_SUSPICION_ROWS),
                (InterrogationWorker, MANY_SUSPICION_ROWS),
            ]:
                listed_worker = _create_listed_worker(
                    worker_class,
                    owner_id=owner.id,
                    suspicion_rows=suspicion_rows,
                    performance_rows=INTERROGATION_PERFORMANCE_ROWS if worker_class is InterrogationWorker else 0,
                )
                expected_counts[listed_worker.id] = suspicion_rows

        rows = _walked_rows(_walk(client, api_key=moderator.api_key, owner_id=owner.id, limit=2))

        assert {row["id"]: row["suspicious"] for row in rows} == expected_counts

    @pytest.mark.parametrize("view_name", ["public", "moderator"])
    def test_a_page_leaves_suspicion_rows_unloaded(self, app, client, make_api_user, view_name) -> None:
        import sqlalchemy

        from horde.classes.base.worker import WorkerSuspicions
        from horde.classes.kobold.worker import TextWorker
        from horde.classes.stable.interrogation_worker import InterrogationWorker
        from horde.classes.stable.worker import ImageWorker
        from horde.database.functions import page_registered_workers
        from horde.flask import db
        from horde.worker_pages import DEFAULT_WORKER_LIST_SORT, WorkerListView, WorkerPageFilters

        view = WorkerListView(view_name)
        owner = make_api_user(trusted=True, kudos=100)
        with app.app_context():
            created_ids = {
                uuid.UUID(_create_listed_worker(worker_class, owner_id=owner.id, suspicion_rows=FEW_SUSPICION_ROWS).id)
                for worker_class in (ImageWorker, TextWorker, InterrogationWorker)
            }
        response = client.put(f"/api/v2/users/{owner.id}", json={"public_workers": True}, headers=_headers(owner.api_key))
        assert response.status_code == 200, response.get_data(as_text=True)

        with app.app_context():
            page = page_registered_workers(
                filters=WorkerPageFilters(owner_id=owner.id),
                sort=DEFAULT_WORKER_LIST_SORT,
                limit=10,
                after=None,
                view=view,
            )
            for worker in page.workers:
                worker.get_details(view.details_privilege)
            loaded_suspicion_rows = [obj for obj in db.session.identity_map.values() if isinstance(obj, WorkerSuspicions)]

            assert {worker.id for worker in page.workers} == created_ids
            assert all("suspicions" not in sqlalchemy.inspect(worker).dict for worker in page.workers)
            assert loaded_suspicion_rows == []

    def test_the_statement_count_does_not_grow_with_suspicion_rows_or_interrogation_workers(
        self,
        app,
        client,
        make_api_user,
        assert_query_count,
    ) -> None:
        from horde.classes.stable.interrogation_worker import InterrogationWorker
        from horde.classes.stable.worker import ImageWorker

        owner = make_api_user(trusted=True)
        moderator = make_api_user(moderator=True)
        with app.app_context():
            image_worker = _create_listed_worker(ImageWorker, owner_id=owner.id, suspicion_rows=FEW_SUSPICION_ROWS)
            _create_listed_worker(
                InterrogationWorker,
                owner_id=owner.id,
                suspicion_rows=FEW_SUSPICION_ROWS,
                performance_rows=INTERROGATION_PERFORMANCE_ROWS,
            )
        _get_page(client, api_key=moderator.api_key, owner_id=owner.id)
        with assert_query_count() as queries_before:
            assert _get_page(client, api_key=moderator.api_key, owner_id=owner.id).get_json()["total"] == 2
        with app.app_context():
            _add_suspicion_rows(uuid.UUID(image_worker.id), MANY_SUSPICION_ROWS)
            for _ in range(2):
                _create_listed_worker(
                    InterrogationWorker,
                    owner_id=owner.id,
                    suspicion_rows=MANY_SUSPICION_ROWS,
                    performance_rows=INTERROGATION_PERFORMANCE_ROWS,
                )
        _get_page(client, api_key=moderator.api_key, owner_id=owner.id)

        with assert_query_count() as queries_after:
            assert _get_page(client, api_key=moderator.api_key, owner_id=owner.id).get_json()["total"] == 4

        assert len(queries_after) == len(queries_before), f"before:\n{queries_before.describe()}\n\nafter:\n{queries_after.describe()}"

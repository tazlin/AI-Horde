# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The validated backend registry API and the structured software report on a pop.

Admins maintain the rules that determine which text workers can serve requests with ``validated_backends`` set. A
worker reports its software with the structured ``bridge`` and ``backend`` pop fields or the legacy ``bridge_agent``
string. The worker details endpoint returns the reported software and whether it matches a rule.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient

pytestmark = pytest.mark.integration

_TEXT_MODEL = "elinas/chronos-70b-v2"
_WORKER_URL = "https://github.com/Haidra-Org/AI-Horde-Worker"


@pytest.fixture
def admin_headers(app: Flask, api_key: str, request_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Return request headers with the API key of a user listed in the ``ADMINS`` environment variable."""
    from horde.database import functions as database

    with app.app_context():
        admin_alias = database.find_user_by_api_key(api_key).get_unique_alias()
    monkeypatch.setenv("ADMINS", json.dumps([admin_alias]))
    return request_headers


@pytest.fixture
def cleanup_rules(app: Flask) -> Any:
    """Delete the rules created by a test and keep the seed rules, so each test starts with only the seed rules."""
    yield
    from horde.classes.base.validated_backend import ValidatedBackend
    from horde.flask import db
    from horde.validated_backends import validated_backend_registry

    with app.app_context():
        db.session.query(ValidatedBackend).filter(ValidatedBackend.created_by_user_id.is_not(None)).delete()
        db.session.commit()
        validated_backend_registry.invalidate()


def _create_rule(client: FlaskClient, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/api/v2/validated_backends", json=body, headers=headers)
    assert response.status_code == 201, response.get_data(as_text=True)
    return response.get_json()


def _worker_details(client: FlaskClient, worker_name: str) -> dict[str, Any]:
    response = client.get(f"/api/v2/workers/name/{worker_name}")
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def _text_pop(client: FlaskClient, headers: dict[str, str], worker_name: str, **software_fields: Any) -> Any:
    pop_body = {
        "name": worker_name,
        "models": [_TEXT_MODEL],
        "amount": 1,
        "max_context_length": 4096,
        "max_length": 512,
        **software_fields,
    }
    return client.post("/api/v2/generate/text/pop", json=pop_body, headers=headers)


def _unique_worker_name() -> str:
    return f"CICD Validated Scribe {uuid.uuid4().hex[:8]}"


class TestRegistryApi:
    """Anyone can read the rules. Only an admin can write them."""

    def test_rule_list_initially_holds_only_the_legacy_seed_rules(self, client: FlaskClient) -> None:
        response = client.get("/api/v2/validated_backends")

        assert response.status_code == 200, response.get_data(as_text=True)
        identities = {(rule["bridge_name"], rule["backend_name"]) for rule in response.get_json()}
        assert identities == {
            ("AI Horde Worker", None),
            ("AI Horde Worker", "aphrodite"),
            ("KoboldCppEmbedWorker", None),
            ("TabbyAPI", None),
        }

    def test_non_admin_cannot_create_a_rule(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ADMINS", "[]")

        response = client.post("/api/v2/validated_backends", json={"bridge_name": "Sneaky"}, headers=request_headers)

        assert response.status_code == 403
        assert response.get_json()["rc"] == "NotAdmin"

    @pytest.mark.usefixtures("cleanup_rules")
    def test_admin_creates_changes_and_deletes_a_rule(self, client: FlaskClient, admin_headers: dict[str, str]) -> None:
        created = _create_rule(
            client,
            admin_headers,
            {"bridge_name": "AI Horde Worker", "backend_name": "llamacpp", "min_backend_version": "0.3", "description": "CI"},
        )
        assert created["backend_name"] == "llamacpp"
        assert created["min_backend_version"] == "0.3"
        assert created["created"].endswith("+00:00")

        changed = client.patch(
            f"/api/v2/validated_backends/{created['id']}",
            json={"min_backend_version": None, "min_bridge_version": "25"},
            headers=admin_headers,
        )
        assert changed.status_code == 200, changed.get_data(as_text=True)
        assert changed.get_json()["min_backend_version"] is None
        assert changed.get_json()["min_bridge_version"] == "25"
        assert changed.get_json()["description"] == "CI"

        deleted = client.delete(f"/api/v2/validated_backends/{created['id']}", headers=admin_headers)
        assert deleted.status_code == 200, deleted.get_data(as_text=True)
        assert client.get(f"/api/v2/validated_backends/{created['id']}").status_code == 404

    @pytest.mark.usefixtures("cleanup_rules")
    def test_a_second_rule_for_the_same_bridge_and_backend_is_refused(
        self,
        client: FlaskClient,
        admin_headers: dict[str, str],
    ) -> None:
        response = client.post(
            "/api/v2/validated_backends",
            json={"bridge_name": "ai horde worker", "backend_name": "APHRODITE"},
            headers=admin_headers,
        )

        assert response.status_code == 400
        assert response.get_json()["rc"] == "ValidatedBackendExists"

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"bridge_name": ""},
            {"bridge_name": "bad:name"},
            {"bridge_name": "Fine", "backend_name": "bad~name"},
            {"bridge_name": "Fine", "min_bridge_version": "nightly"},
            {"bridge_name": "Fine", "unknown_field": 1},
        ],
    )
    def test_invalid_rules_are_refused(self, client: FlaskClient, admin_headers: dict[str, str], body: dict[str, Any]) -> None:
        response = client.post("/api/v2/validated_backends", json=body, headers=admin_headers)

        assert response.status_code == 400, response.get_data(as_text=True)
        assert response.get_json()["rc"] == "ValidatedBackendInvalid"

    def test_unknown_rule_id_is_not_found(self, client: FlaskClient) -> None:
        assert client.get(f"/api/v2/validated_backends/{uuid.uuid4()}").get_json()["rc"] == "ValidatedBackendNotFound"
        assert client.get("/api/v2/validated_backends/not-a-uuid").status_code == 404


class TestReportedSoftware:
    """The software report in a pop is stored on the worker and matched against the registry rules."""

    def test_software_from_a_legacy_bridge_agent_is_stored_and_validated(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
    ) -> None:
        worker_name = _unique_worker_name()

        pop = _text_pop(client, request_headers, worker_name, bridge_agent=f"AI Horde Worker~aphrodite~oai:24:{_WORKER_URL}")

        assert pop.status_code == 200, pop.get_data(as_text=True)
        details = _worker_details(client, worker_name)
        assert details["bridge"] == {"name": "AI Horde Worker", "version": "24"}
        assert details["backend"] == {"name": "aphrodite", "version": None, "api": "openai"}
        assert details["validated_backend"] is True

    def test_bridge_agent_is_built_from_the_structured_report(self, client: FlaskClient, request_headers: dict[str, str]) -> None:
        worker_name = _unique_worker_name()

        pop = _text_pop(
            client,
            request_headers,
            worker_name,
            bridge={"name": "AI Horde Worker", "version": "25.0.0", "url": _WORKER_URL},
            backend={"name": "llamacpp", "version": "0.3.2", "api": "openai"},
        )

        assert pop.status_code == 200, pop.get_data(as_text=True)
        details = _worker_details(client, worker_name)
        assert details["bridge_agent"] == f"AI Horde Worker:25.0.0:{_WORKER_URL}"
        assert details["backend"] == {"name": "llamacpp", "version": "0.3.2", "api": "openai"}
        assert details["validated_backend"] is False

    @pytest.mark.parametrize(
        ("software_fields", "expected_rc"),
        [
            ({"backend": {"name": "llamacpp"}}, "WorkerSoftwareInvalid"),
            ({"bridge": {"name": "Bad:Bridge", "version": "1"}}, "WorkerSoftwareInvalid"),
            ({"bridge": {"name": "Bridge"}}, None),
        ],
    )
    def test_malformed_software_reports_are_refused(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
        software_fields: dict[str, Any],
        expected_rc: str | None,
    ) -> None:
        pop = _text_pop(client, request_headers, _unique_worker_name(), **software_fields)

        assert pop.status_code == 400, pop.get_data(as_text=True)
        if expected_rc is not None:
            assert pop.get_json()["rc"] == expected_rc

    @pytest.mark.usefixtures("cleanup_rules")
    def test_new_rule_lets_a_worker_serve_requests_that_require_a_validated_backend(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
        admin_headers: dict[str, str],
    ) -> None:
        request_body = {
            "prompt": "a validated backend writes a short poem",
            "trusted_workers": True,
            "validated_backends": True,
            "max_length": 64,
            "max_context_length": 1024,
            "models": [_TEXT_MODEL],
        }
        created_request = client.post("/api/v2/generate/text/async", json=request_body, headers=request_headers)
        assert created_request.status_code < 400, created_request.get_data(as_text=True)
        request_id = created_request.get_json()["id"]
        worker_name = _unique_worker_name()
        software_fields = {
            "bridge": {"name": "AI Horde Worker", "version": "25.0.0", "url": _WORKER_URL},
            "backend": {"name": "llamacpp", "version": "0.3.2", "api": "openai"},
        }
        try:
            unvalidated_pop = _text_pop(client, request_headers, worker_name, **software_fields)
            assert unvalidated_pop.status_code == 200, unvalidated_pop.get_data(as_text=True)
            assert unvalidated_pop.get_json()["id"] is None

            _create_rule(client, admin_headers, {"bridge_name": "AI Horde Worker", "backend_name": "llamacpp"})

            validated_pop = _text_pop(client, request_headers, worker_name, **software_fields)
            assert validated_pop.status_code == 200, validated_pop.get_data(as_text=True)
            assert validated_pop.get_json()["id"] is not None
            assert _worker_details(client, worker_name)["validated_backend"] is True
        finally:
            client.delete(f"/api/v2/generate/text/status/{request_id}", headers=request_headers)

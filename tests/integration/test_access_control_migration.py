# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify that the 6.1.0 migration keeps existing shared keys working, can be run again, and matches the ORM models."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy
import sqlparse

from tests.dependency_runtime import create_schema, drop_schema, new_test_schema_name

MIGRATION_PATH = Path(__file__).resolve().parents[2] / "sql_statements" / "6.1.0.txt"
"""The production migration for shared key policies and account links."""
USERS_STUB = "CREATE TABLE users (id INTEGER PRIMARY KEY)"
"""The table referenced by the new tables."""
SHAREDKEYS_STUB = (
    "CREATE TABLE user_sharedkeys (id UUID PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE, "
    "kudos BIGINT NOT NULL, created TIMESTAMP NOT NULL, expiry TIMESTAMP, name VARCHAR(255), utilized BIGINT NOT NULL, "
    "max_image_pixels INTEGER NOT NULL, max_image_steps INTEGER NOT NULL, max_text_tokens INTEGER NOT NULL)"
)
"""The columns of ``user_sharedkeys`` before 6.1.0."""
USER_ROLES_STUB = (
    "CREATE TYPE userroletypes AS ENUM "
    "('MODERATOR', 'TRUSTED', 'FLAGGED', 'CUSTOMIZER', 'VPN', 'SPECIAL', 'SERVICE', 'EDUCATION', 'DELETED')",
    "CREATE TABLE user_roles (id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE, "
    "user_role userroletypes NOT NULL, value BOOLEAN NOT NULL, CONSTRAINT user_id_role UNIQUE (user_id, user_role))",
)
"""The account roles table before 6.1.0, read by the service role update."""
SERVICE_OWNER_KEY_ID = "00000000-0000-0000-0000-0000000000bb"
"""An existing key of a service account."""
REVOKED_SERVICE_OWNER_KEY_ID = "00000000-0000-0000-0000-0000000000cc"
"""An existing key of an account with a service role row whose value is false."""
NEW_TABLES = ("ip_subject_account_links",)
"""The tables created by the migration."""
NEW_SHAREDKEY_COLUMNS = (
    "scopes",
    "roles",
    "allowed_models",
    "max_concurrency",
    "allowed_networks",
    "status",
    "status_reason",
    "status_changed",
)
"""The columns added to ``user_sharedkeys`` by the migration."""
ABSENT = "<absent>"


def _schema_engine(pg_dsn: str, schema_name: str) -> sqlalchemy.Engine:
    return sqlalchemy.create_engine(
        pg_dsn,
        isolation_level="AUTOCOMMIT",
        connect_args={"options": f"-c search_path={schema_name}"},
    )


def _create_pre_migration_tables(connection: sqlalchemy.Connection) -> None:
    for statement in (USERS_STUB, SHAREDKEYS_STUB, *USER_ROLES_STUB):
        connection.execute(sqlalchemy.text(statement))


def _apply_migration(connection: sqlalchemy.Connection) -> None:
    for statement in sqlparse.split(MIGRATION_PATH.read_text()):
        connection.execute(sqlalchemy.text(statement))


@pytest.fixture
def migrated_schema(pg_dsn: str) -> Iterator[sqlalchemy.Connection]:
    """Yield a connection to a schema holding pre-6.1.0 tables and three existing keys, with the migration applied twice.

    Account 1 does not have roles, account 3 is a service account and account 4 has a service role row with the value
    false.
    """
    schema_name = new_test_schema_name("horde_access_migration")
    create_schema(pg_dsn, schema_name)
    engine = _schema_engine(pg_dsn, schema_name)
    try:
        with engine.connect() as connection:
            _create_pre_migration_tables(connection)
            connection.execute(sqlalchemy.text("INSERT INTO users (id) VALUES (1), (2), (3), (4)"))
            connection.execute(
                sqlalchemy.text("INSERT INTO user_roles (user_id, user_role, value) VALUES (3, 'SERVICE', true), (4, 'SERVICE', false)")
            )
            connection.execute(
                sqlalchemy.text(
                    "INSERT INTO user_sharedkeys VALUES "
                    "('00000000-0000-0000-0000-0000000000aa', 1, 100, now(), NULL, 'old key', 0, -1, -1, -1), "
                    f"('{SERVICE_OWNER_KEY_ID}', 3, 100, now(), NULL, 'service key', 0, -1, -1, -1), "
                    f"('{REVOKED_SERVICE_OWNER_KEY_ID}', 4, 100, now(), NULL, 'former service key', 0, -1, -1, -1)",
                ),
            )
            for _ in range(2):
                _apply_migration(connection)
            yield connection
    finally:
        engine.dispose()
        drop_schema(pg_dsn, schema_name)


def test_an_existing_key_is_a_legacy_key_that_keeps_working(migrated_schema: sqlalchemy.Connection) -> None:
    """Keys in use on the day of the deploy must stay active and unlimited, and keep their owner's roles.

    A NULL role list means the owner's roles.
    """
    row = migrated_schema.execute(
        sqlalchemy.text(
            "SELECT scopes, roles, allowed_models, allowed_networks, max_concurrency, status, status_reason "
            "FROM user_sharedkeys WHERE user_id = 1",
        ),
    ).one()

    assert tuple(row) == (None, None, None, None, -1, "active", None)


def _roles_of(connection: sqlalchemy.Connection, sharedkey_id: str) -> Any:
    return connection.execute(
        sqlalchemy.text("SELECT roles FROM user_sharedkeys WHERE id = CAST(:sharedkey_id AS uuid)"),
        {"sharedkey_id": sharedkey_id},
    ).scalar_one()


def test_an_existing_key_of_a_service_account_keeps_the_service_role(migrated_schema: sqlalchemy.Connection) -> None:
    """A key without a role list does not act with the service role, so a service account's integration that sets
    ``proxied_account`` through a key would break on the day of the deploy without an explicit grant.

    Every other grantable role is listed too, because the request still acts only with the roles that the owner holds,
    as it did before 6.1.0.
    """
    assert _roles_of(migrated_schema, SERVICE_OWNER_KEY_ID) == ["trusted", "education", "special", "service"]


def test_a_service_role_stored_as_false_does_not_grant_the_service_role(migrated_schema: sqlalchemy.Connection) -> None:
    """The application reads a role row with ``value`` false as a role that the account does not hold. Such an account
    is not a service account, so its key stays a legacy key."""
    assert _roles_of(migrated_schema, REVOKED_SERVICE_OWNER_KEY_ID) is None


def test_running_the_migration_again_keeps_an_owner_role_list(migrated_schema: sqlalchemy.Connection) -> None:
    """An owner who narrowed the migrated key after the deploy must keep the narrower list if the migration runs again."""
    migrated_schema.execute(
        sqlalchemy.text("UPDATE user_sharedkeys SET roles = CAST('[]' AS jsonb) WHERE id = CAST(:sharedkey_id AS uuid)"),
        {"sharedkey_id": SERVICE_OWNER_KEY_ID},
    )

    _apply_migration(migrated_schema)

    assert _roles_of(migrated_schema, SERVICE_OWNER_KEY_ID) == []


def test_an_account_is_stored_once_per_network(migrated_schema: sqlalchemy.Connection) -> None:
    """Recording an account again on the same network updates its one row, which the upsert relies on."""
    migrated_schema.execute(sqlalchemy.text("INSERT INTO ip_subject_account_links (subject_key, user_id) VALUES ('k', 2)"))

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        migrated_schema.execute(sqlalchemy.text("INSERT INTO ip_subject_account_links (subject_key, user_id) VALUES ('k', 2)"))


def test_deleting_an_account_removes_its_links(migrated_schema: sqlalchemy.Connection) -> None:
    """Links are personal data and are deleted with the account."""
    migrated_schema.execute(sqlalchemy.text("INSERT INTO ip_subject_account_links (subject_key, user_id) VALUES ('k', 2)"))

    migrated_schema.execute(sqlalchemy.text("DELETE FROM users WHERE id = 2"))

    assert migrated_schema.execute(sqlalchemy.text("SELECT count(*) FROM ip_subject_account_links")).scalar_one() == 0


def _catalog(connection: sqlalchemy.Connection, schema_name: str) -> dict[str, Any]:
    """Return the column attributes, constraints and indexes of the migrated tables and columns, as PostgreSQL renders them."""
    items: dict[str, Any] = {}
    for table in (*NEW_TABLES, "user_sharedkeys"):
        relation = {"table": table}
        for name, type_name, not_null, default in connection.execute(
            sqlalchemy.text(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, pg_get_expr(d.adbin, d.adrelid) "
                "FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE a.attrelid = CAST(:table AS regclass) AND a.attnum > 0 AND NOT a.attisdropped",
            ),
            relation,
        ):
            if table == "user_sharedkeys" and name not in NEW_SHAREDKEY_COLUMNS:
                continue
            items[f"{table} column {name} type"] = type_name
            items[f"{table} column {name} not null"] = not_null
            items[f"{table} column {name} server default"] = default
        if table == "user_sharedkeys":
            continue
        for name, definition in connection.execute(
            sqlalchemy.text("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = CAST(:table AS regclass)"),
            relation,
        ):
            items[f"{table} constraint {name}"] = definition.replace(f"{schema_name}.", "")
        for name, definition in connection.execute(
            sqlalchemy.text(
                "SELECT c.relname, pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE i.indrelid = CAST(:table AS regclass)",
            ),
            relation,
        ):
            items[f"{table} index {name}"] = definition.replace(f"{schema_name}.", "")
    return items


def test_orm_models_build_the_same_schema_as_the_migration(pg_dsn: str) -> None:
    """The behavior tests create their schema from the ORM models, and production uses the migration, so the two must match."""
    from horde.classes.base.account_link import IpSubjectAccountLink
    from horde.classes.base.user import UserSharedKey

    migration_schema = new_test_schema_name("horde_access_parity_sql")
    orm_schema = new_test_schema_name("horde_access_parity_orm")
    engines: list[sqlalchemy.Engine] = []
    for schema_name in (migration_schema, orm_schema):
        create_schema(pg_dsn, schema_name)
        engines.append(_schema_engine(pg_dsn, schema_name))
    migration_engine, orm_engine = engines
    try:
        with migration_engine.connect() as connection:
            _create_pre_migration_tables(connection)
            _apply_migration(connection)
            migration_catalog = _catalog(connection, migration_schema)
        with orm_engine.connect() as connection:
            connection.execute(sqlalchemy.text(USERS_STUB))
            for table in (
                UserSharedKey.__table__,
                IpSubjectAccountLink.__table__,
            ):
                table.create(connection)
            orm_catalog = _catalog(connection, orm_schema)
    finally:
        for engine in engines:
            engine.dispose()
        for schema_name in (migration_schema, orm_schema):
            drop_schema(pg_dsn, schema_name)

    assert any(" index " in item for item in migration_catalog), "the catalog query did not return any index"
    differences = [
        f"{item}: migration {migration_catalog.get(item, ABSENT)!r}, ORM {orm_catalog.get(item, ABSENT)!r}"
        for item in sorted(migration_catalog.keys() | orm_catalog.keys())
        if migration_catalog.get(item, ABSENT) != orm_catalog.get(item, ABSENT)
    ]
    assert not differences, "\n".join(differences)

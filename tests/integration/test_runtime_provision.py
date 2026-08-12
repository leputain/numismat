import os

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from finbot.adapters.database.provision import (
    ProvisioningError,
    ProvisioningTarget,
    provision,
)


def _target() -> ProvisioningTarget:
    configured = make_url(os.environ["TEST_DATABASE_URL"])
    database = configured.database or ""
    owner = configured.username or ""
    assert database.endswith("_test")
    assert owner
    runtime = configured.set(username="finbot_runtime_test", password="synthetic-runtime-only")
    return ProvisioningTarget(
        admin_conninfo=configured.set(drivername="postgresql").render_as_string(
            hide_password=False
        ),
        runtime_conninfo=runtime.set(drivername="postgresql").render_as_string(hide_password=False),
        database=database,
        owner_role=owner,
        runtime_role="finbot_runtime_test",
        runtime_password="synthetic-runtime-only",
    )


def _drop_runtime_role(target: ProvisioningTarget) -> None:
    with psycopg.connect(target.admin_conninfo) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(target.runtime_role)))
            cursor.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(target.runtime_role))
            )


def _lock_down_other_databases(cursor: psycopg.Cursor[tuple[object, ...]], database: str) -> None:
    cursor.execute(
        "SELECT datname FROM pg_database "
        "WHERE datallowconn AND datname <> %s AND datname <> 'template0'",
        (database,),
    )
    for (other_database,) in cursor.fetchall():
        cursor.execute(
            sql.SQL("REVOKE CONNECT, CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(str(other_database))
            )
        )


def test_runtime_role_is_idempotent_and_fail_closed_on_public_privileges() -> None:
    target = _target()
    database = sql.Identifier(target.database)
    with psycopg.connect(target.admin_conninfo) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC").format(database)
            )
            cursor.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            _lock_down_other_databases(cursor, target.database)

    try:
        provision(target)
        provision(target)

        with psycopg.connect(target.runtime_conninfo) as runtime_connection:
            with runtime_connection.cursor() as cursor:
                cursor.execute("SELECT version_num FROM public.alembic_version")
                assert cursor.fetchone() is not None
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cursor.execute("UPDATE public.alembic_version SET version_num = version_num")
            runtime_connection.rollback()

        with psycopg.connect(target.admin_conninfo) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("GRANT TEMPORARY ON DATABASE {} TO PUBLIC").format(database))
        with pytest.raises(ProvisioningError, match="effective database"):
            provision(target)
    finally:
        with psycopg.connect(target.admin_conninfo) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC").format(database)
                )
        _drop_runtime_role(target)

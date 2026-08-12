from __future__ import annotations

import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL, make_url

from finbot.observability.logging import safe_error_class

MIGRATIONS_URL_SECRET = Path("/run/secrets/database_url_migrations")
RUNTIME_URL_SECRET = Path("/run/secrets/database_url_runtime")
_ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_POSTGRES_DRIVERS = frozenset({"postgresql", "postgresql+psycopg"})
_TARGET_OVERRIDE_QUERY_KEYS = frozenset(
    {
        "dbname",
        "host",
        "hostaddr",
        "load_balance_hosts",
        "options",
        "passfile",
        "password",
        "port",
        "service",
        "servicefile",
        "target_session_attrs",
        "user",
    }
)


class ProvisioningError(RuntimeError):
    """The requested runtime privilege boundary cannot be made safe automatically."""


@dataclass(frozen=True)
class ProvisioningTarget:
    admin_conninfo: str = field(repr=False)
    runtime_conninfo: str = field(repr=False)
    database: str
    owner_role: str
    runtime_role: str
    runtime_password: str = field(repr=False)


def _read_secret(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value or "\x00" in value:
        raise ValueError("database URL secret is empty or invalid")
    return value


def _postgres_url(value: str) -> URL:
    url = make_url(value)
    if url.drivername not in _POSTGRES_DRIVERS:
        raise ValueError("only PostgreSQL URLs are supported")
    if not url.database or not url.username or not url.host:
        raise ValueError("database URL must include an explicit host, database, and username")
    if "," in url.host:
        raise ValueError("multi-host database URLs are not supported for role provisioning")
    query_keys = {str(key).lower() for key in url.query}
    if query_keys & _TARGET_OVERRIDE_QUERY_KEYS:
        raise ValueError("database URL query cannot override connection identity or session role")
    return url


def _endpoint(url: URL) -> tuple[str, int, str, tuple[tuple[str, tuple[str, ...]], ...]]:
    host = url.host
    database = url.database
    if host is None or database is None:
        raise RuntimeError("validated database URL lost its endpoint")
    query = tuple(
        sorted(
            (str(key), tuple(str(value) for value in values))
            for key, values in url.normalized_query.items()
        )
    )
    return host, url.port or 5432, database, query


def load_target(
    migrations_secret: Path = MIGRATIONS_URL_SECRET,
    runtime_secret: Path = RUNTIME_URL_SECRET,
) -> ProvisioningTarget:
    migrations = _postgres_url(_read_secret(migrations_secret))
    runtime = _postgres_url(_read_secret(runtime_secret))
    database = migrations.database
    owner_role = migrations.username
    runtime_role = runtime.username
    if database is None or owner_role is None or runtime_role is None:
        raise RuntimeError("validated database URL lost required fields")
    if _endpoint(migrations) != _endpoint(runtime):
        raise ValueError("migration and runtime URLs must target the exact same database endpoint")
    if not migrations.password or not runtime.password:
        raise ValueError("database URL secrets must include passwords")
    if not _ROLE_NAME.fullmatch(runtime_role):
        raise ValueError("runtime database username is invalid")
    if runtime_role == owner_role:
        raise ValueError("runtime and migration database roles must be different")

    admin_conninfo = migrations.set(drivername="postgresql").render_as_string(hide_password=False)
    runtime_conninfo = runtime.set(drivername="postgresql").render_as_string(hide_password=False)
    return ProvisioningTarget(
        admin_conninfo=admin_conninfo,
        runtime_conninfo=runtime_conninfo,
        database=database,
        owner_role=owner_role,
        runtime_role=runtime_role,
        runtime_password=runtime.password,
    )


def _flag(cursor: psycopg.Cursor[Any], statement: str, parameters: tuple[object, ...]) -> bool:
    cursor.execute(statement, parameters)
    row = cursor.fetchone()
    if row is None:
        raise ProvisioningError("privilege verification did not return a result")
    return bool(row[0])


def _server_identity(cursor: psycopg.Cursor[Any]) -> tuple[str, str | None, int | None]:
    cursor.execute("SELECT current_database(), inet_server_addr()::text, inet_server_port()")
    row = cursor.fetchone()
    if row is None:
        raise ProvisioningError("database identity verification did not return a result")
    return str(row[0]), None if row[1] is None else str(row[1]), row[2]


def _assert_admin_identity(cursor: psycopg.Cursor[Any], target: ProvisioningTarget) -> None:
    cursor.execute("SELECT current_database(), session_user, current_user")
    row = cursor.fetchone()
    if row != (target.database, target.owner_role, target.owner_role):
        raise ProvisioningError("migration connection identity does not match its URL")


def _assert_existing_role_is_dedicated(
    cursor: psycopg.Cursor[Any], target: ProvisioningTarget
) -> None:
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (target.runtime_role,))
    if cursor.fetchone() is None:
        return

    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_shdepend AS dependency
            JOIN pg_roles AS owned_by ON owned_by.oid = dependency.refobjid
            WHERE owned_by.rolname = %s AND dependency.deptype = 'o'
        )
        """,
        (target.runtime_role,),
    ):
        raise ProvisioningError("runtime role owns database objects")

    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_auth_members AS membership
            JOIN pg_roles AS granted ON granted.oid = membership.roleid
            WHERE granted.rolname = %s
        )
        """,
        (target.runtime_role,),
    ):
        raise ProvisioningError("runtime role is inherited by another role")

    if _flag(
        cursor,
        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE usename = %s)",
        (target.runtime_role,),
    ):
        raise ProvisioningError("runtime role has active database sessions")


def _set_role_policy(
    connection: psycopg.Connection[Any],
    cursor: psycopg.Cursor[Any],
    target: ProvisioningTarget,
) -> None:
    runtime = sql.Identifier(target.runtime_role)
    role_options = sql.SQL(
        "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION "
        "NOBYPASSRLS VALID UNTIL 'infinity'"
    )

    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (target.runtime_role,))
    if cursor.fetchone() is None:
        cursor.execute(sql.SQL("CREATE ROLE {} WITH ").format(runtime) + role_options)
    else:
        cursor.execute(sql.SQL("ALTER ROLE {} WITH ").format(runtime) + role_options)
        cursor.execute(sql.SQL("ALTER ROLE {} RESET ALL").format(runtime))

    # PostgreSQL 18's password-change protocol sends the plaintext password outside
    # the SQL statement. Unlike ALTER ROLE ... PASSWORD <literal>, it therefore
    # cannot expose the URL password through statement logging.
    connection.pgconn.change_password(
        target.runtime_role.encode("ascii"), target.runtime_password.encode("utf-8")
    )

    cursor.execute(
        """
        SELECT granted.rolname
        FROM pg_auth_members AS membership
        JOIN pg_roles AS granted ON granted.oid = membership.roleid
        JOIN pg_roles AS member ON member.oid = membership.member
        WHERE member.rolname = %s
        """,
        (target.runtime_role,),
    )
    for (granted_role,) in cursor.fetchall():
        cursor.execute(
            sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(str(granted_role)), runtime)
        )
    cursor.execute(sql.SQL("ALTER ROLE {} SET search_path TO public").format(runtime))


def _grant_runtime_privileges(cursor: psycopg.Cursor[Any], target: ProvisioningTarget) -> None:
    runtime = sql.Identifier(target.runtime_role)
    database = sql.Identifier(target.database)
    owner = sql.Identifier(target.owner_role)

    cursor.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(database, runtime)
    )
    cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, runtime))
    cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA public FROM {}").format(runtime))
    cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(runtime))
    cursor.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}").format(runtime)
    )
    cursor.execute(
        sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(
            runtime
        )
    )
    cursor.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {}").format(runtime)
    )
    cursor.execute(
        sql.SQL("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}").format(
            runtime
        )
    )
    cursor.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
            "REVOKE ALL PRIVILEGES ON TABLES FROM {}"
        ).format(owner, runtime)
    )
    cursor.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
        ).format(owner, runtime)
    )
    cursor.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
            "REVOKE ALL PRIVILEGES ON SEQUENCES FROM {}"
        ).format(owner, runtime)
    )
    cursor.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
            "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {}"
        ).format(owner, runtime)
    )
    cursor.execute(
        sql.SQL(
            "REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
            "ON TABLE public.alembic_version FROM {}"
        ).format(runtime)
    )


def _verify_runtime_privileges(cursor: psycopg.Cursor[Any], target: ProvisioningTarget) -> None:
    role = target.runtime_role
    database = target.database
    cursor.execute(
        """
        SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit,
               rolreplication, rolbypassrls
        FROM pg_roles
        WHERE rolname = %s
        """,
        (role,),
    )
    if cursor.fetchone() != (True, False, False, False, False, False, False):
        raise ProvisioningError("runtime role attributes are unsafe")

    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_auth_members AS membership
            JOIN pg_roles AS member ON member.oid = membership.member
            WHERE member.rolname = %s
        )
        """,
        (role,),
    ):
        raise ProvisioningError("runtime role still inherits or can assume another role")

    cursor.execute(
        """
        SELECT has_database_privilege(%s, %s, 'CONNECT'),
               has_database_privilege(%s, %s, 'CREATE'),
               has_database_privilege(%s, %s, 'TEMPORARY'),
               has_schema_privilege(%s, 'public', 'USAGE'),
               has_schema_privilege(%s, 'public', 'CREATE')
        """,
        (role, database, role, database, role, database, role, role),
    )
    if cursor.fetchone() != (True, False, False, True, False):
        raise ProvisioningError("effective database or public schema privileges are unsafe")

    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_database AS database
            WHERE database.datallowconn
              AND database.datname <> %s
              AND database.datname <> 'template0'
              AND has_database_privilege(%s, database.oid, 'CONNECT')
        )
        """,
        (database, role),
    ):
        raise ProvisioningError("runtime role can connect to another database")

    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_namespace AS namespace
            WHERE namespace.nspname <> 'public'
              AND namespace.nspname <> 'information_schema'
              AND namespace.nspname !~ '^pg_'
              AND (
                  has_schema_privilege(%s, namespace.oid, 'USAGE')
                  OR has_schema_privilege(%s, namespace.oid, 'CREATE')
              )
        )
        """,
        (role, role),
    ):
        raise ProvisioningError("runtime role can access a non-application schema")

    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND relation.relname <> 'alembic_version'
              AND NOT (
                  has_table_privilege(%s, relation.oid, 'SELECT')
                  AND has_table_privilege(%s, relation.oid, 'INSERT')
                  AND has_table_privilege(%s, relation.oid, 'UPDATE')
                  AND has_table_privilege(%s, relation.oid, 'DELETE')
              )
        )
        """,
        (role, role, role, role),
    ):
        raise ProvisioningError("runtime role is missing application table DML privileges")

    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND (
                  has_table_privilege(%s, relation.oid, 'TRUNCATE')
                  OR has_table_privilege(%s, relation.oid, 'REFERENCES')
                  OR has_table_privilege(%s, relation.oid, 'TRIGGER')
              )
        )
        """,
        (role, role, role),
    ):
        raise ProvisioningError("runtime role has unsafe effective table privileges")

    cursor.execute("SELECT to_regclass('public.alembic_version')")
    if cursor.fetchone() != ("alembic_version",):
        raise ProvisioningError("alembic_version table is missing")
    cursor.execute(
        """
        SELECT has_table_privilege(%s, 'public.alembic_version', 'SELECT'),
               has_table_privilege(%s, 'public.alembic_version', 'INSERT'),
               has_table_privilege(%s, 'public.alembic_version', 'UPDATE'),
               has_table_privilege(%s, 'public.alembic_version', 'DELETE'),
               has_table_privilege(%s, 'public.alembic_version', 'TRUNCATE'),
               has_table_privilege(%s, 'public.alembic_version', 'REFERENCES'),
               has_table_privilege(%s, 'public.alembic_version', 'TRIGGER')
        """,
        (role, role, role, role, role, role, role),
    )
    if cursor.fetchone() != (True, False, False, False, False, False, False):
        raise ProvisioningError("runtime role can mutate alembic_version")

    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_class AS sequence
            JOIN pg_namespace AS namespace ON namespace.oid = sequence.relnamespace
            WHERE namespace.nspname = 'public'
              AND sequence.relkind = 'S'
              AND NOT (
                  has_sequence_privilege(%s, sequence.oid, 'USAGE')
                  AND has_sequence_privilege(%s, sequence.oid, 'SELECT')
                  AND has_sequence_privilege(%s, sequence.oid, 'UPDATE')
              )
        )
        """,
        (role, role, role),
    ):
        raise ProvisioningError("runtime role is missing sequence privileges")

    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_default_acl AS defaults
            CROSS JOIN LATERAL aclexplode(defaults.defaclacl) AS privilege
            JOIN pg_roles AS grantee ON grantee.oid = privilege.grantee
            LEFT JOIN pg_roles AS owner ON owner.oid = defaults.defaclrole
            LEFT JOIN pg_namespace AS namespace ON namespace.oid = defaults.defaclnamespace
            WHERE grantee.rolname = %s
              AND NOT (
                  owner.rolname = %s
                  AND namespace.nspname = 'public'
                  AND (
                      (defaults.defaclobjtype = 'r' AND privilege.privilege_type IN
                          ('SELECT', 'INSERT', 'UPDATE', 'DELETE'))
                      OR (defaults.defaclobjtype = 'S' AND privilege.privilege_type IN
                          ('USAGE', 'SELECT', 'UPDATE'))
                  )
              )
        )
        """,
        (role, target.owner_role),
    ):
        raise ProvisioningError("another owner grants default privileges to the runtime role")

    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_default_acl AS defaults
            CROSS JOIN LATERAL aclexplode(defaults.defaclacl) AS privilege
            WHERE privilege.grantee = 0
              AND defaults.defaclobjtype = 'r'
              AND privilege.privilege_type IN ('TRUNCATE', 'REFERENCES', 'TRIGGER')
        )
        """,
        (),
    ):
        raise ProvisioningError("PUBLIC receives unsafe default table privileges")


def _expect_runtime_denial(
    connection: psycopg.Connection[Any], statement: sql.SQL | sql.Composed | str
) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement)
    except psycopg.errors.InsufficientPrivilege:
        connection.rollback()
        return
    except Exception as exc:
        connection.rollback()
        raise ProvisioningError("runtime denial probe failed unexpectedly") from exc
    connection.rollback()
    raise ProvisioningError("runtime role unexpectedly completed a forbidden statement")


def _verify_runtime_connection(
    target: ProvisioningTarget, expected_server: tuple[str, str | None, int | None]
) -> None:
    with psycopg.connect(target.runtime_conninfo, connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), session_user, current_user")
            if cursor.fetchone() != (
                target.database,
                target.runtime_role,
                target.runtime_role,
            ):
                raise ProvisioningError("runtime connection identity does not match its URL")
            if _server_identity(cursor) != expected_server:
                raise ProvisioningError("runtime and migration URLs resolved to different servers")

        probe = sql.Identifier(f"finbot_runtime_privilege_probe_{uuid.uuid4().hex}")
        _expect_runtime_denial(
            connection, sql.SQL("CREATE TEMP TABLE {} (id integer)").format(probe)
        )
        _expect_runtime_denial(
            connection, "UPDATE public.alembic_version SET version_num = version_num"
        )


def provision(target: ProvisioningTarget) -> None:
    with psycopg.connect(target.admin_conninfo, connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            _assert_admin_identity(cursor, target)
            expected_server = _server_identity(cursor)
            _assert_existing_role_is_dedicated(cursor, target)
            _set_role_policy(connection, cursor, target)
            _grant_runtime_privileges(cursor, target)
            _verify_runtime_privileges(cursor, target)
    _verify_runtime_connection(target, expected_server)


def main() -> int:
    try:
        provision(load_target())
    except Exception as exc:
        print(
            json.dumps(
                {"error_class": safe_error_class(exc), "status": "error"},
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print('{"status":"ok"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL, make_url

from finbot.observability.logging import safe_error_class

MIGRATIONS_URL_SECRET = Path("/run/secrets/database_url_migrations")
READONLY_URL_SECRET = Path("/run/secrets/database_url_mcp")

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


class ReadonlyProvisioningError(RuntimeError):
    """The dedicated read-only boundary could not be proven safe."""


@dataclass(frozen=True)
class ReadonlyProvisioningTarget:
    admin_conninfo: str = field(repr=False)
    readonly_conninfo: str = field(repr=False)
    database: str
    owner_role: str
    readonly_role: str
    readonly_password: str = field(repr=False)


def _read_secret(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value or "\x00" in value:
        raise ValueError("database URL secret is empty or invalid")
    return value


def _postgres_url(value: str) -> URL:
    url = make_url(value)
    if url.drivername not in _POSTGRES_DRIVERS:
        raise ValueError("only PostgreSQL URLs are supported")
    if not url.database or not url.username or not url.host or not url.password:
        raise ValueError("database URL must include host, database, username, and password")
    if "," in url.host:
        raise ValueError("multi-host database URLs are not supported")
    if {str(key).lower() for key in url.query} & _TARGET_OVERRIDE_QUERY_KEYS:
        raise ValueError("database URL query cannot override connection identity")
    return url


def _endpoint(url: URL) -> tuple[str, int, str, tuple[tuple[str, tuple[str, ...]], ...]]:
    if url.host is None or url.database is None:
        raise RuntimeError("validated database URL lost its endpoint")
    query = tuple(
        sorted(
            (str(key), tuple(str(item) for item in values))
            for key, values in url.normalized_query.items()
        )
    )
    return url.host, url.port or 5432, url.database, query


def load_readonly_target(
    migrations_secret: Path = MIGRATIONS_URL_SECRET,
    readonly_secret: Path = READONLY_URL_SECRET,
) -> ReadonlyProvisioningTarget:
    migrations = _postgres_url(_read_secret(migrations_secret))
    readonly = _postgres_url(_read_secret(readonly_secret))
    if _endpoint(migrations) != _endpoint(readonly):
        raise ValueError("migration and read-only URLs must target the exact same database")
    database = migrations.database
    owner_role = migrations.username
    readonly_role = readonly.username
    if database is None or owner_role is None or readonly_role is None:
        raise RuntimeError("validated database URL lost required fields")
    if not _ROLE_NAME.fullmatch(readonly_role) or readonly_role == owner_role:
        raise ValueError("read-only database username is invalid")
    return ReadonlyProvisioningTarget(
        admin_conninfo=migrations.set(drivername="postgresql").render_as_string(
            hide_password=False
        ),
        readonly_conninfo=readonly.set(drivername="postgresql").render_as_string(
            hide_password=False
        ),
        database=database,
        owner_role=owner_role,
        readonly_role=readonly_role,
        readonly_password=readonly.password or "",
    )


def _flag(cursor: psycopg.Cursor[Any], statement: str, parameters: tuple[object, ...]) -> bool:
    cursor.execute(statement, parameters)
    row = cursor.fetchone()
    if row is None:
        raise ReadonlyProvisioningError("privilege verification returned no result")
    return bool(row[0])


def _ensure_role_is_dedicated(
    cursor: psycopg.Cursor[Any],
    target: ReadonlyProvisioningTarget,
) -> None:
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (target.readonly_role,))
    if cursor.fetchone() is None:
        return
    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_shdepend AS dependency
            JOIN pg_roles AS role ON role.oid = dependency.refobjid
            WHERE role.rolname = %s AND dependency.deptype = 'o'
        )
        """,
        (target.readonly_role,),
    ):
        raise ReadonlyProvisioningError("read-only role owns database objects")
    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_auth_members AS membership
            JOIN pg_roles AS granted ON granted.oid = membership.roleid
            WHERE granted.rolname = %s
        )
        """,
        (target.readonly_role,),
    ):
        raise ReadonlyProvisioningError("read-only role is inherited by another role")
    if _flag(
        cursor,
        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE usename = %s)",
        (target.readonly_role,),
    ):
        raise ReadonlyProvisioningError("read-only role has active database sessions")


def _configure_role(
    connection: psycopg.Connection[Any],
    cursor: psycopg.Cursor[Any],
    target: ReadonlyProvisioningTarget,
) -> None:
    role = sql.Identifier(target.readonly_role)
    options = sql.SQL(
        "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION "
        "NOBYPASSRLS VALID UNTIL 'infinity'"
    )
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (target.readonly_role,))
    if cursor.fetchone() is None:
        cursor.execute(sql.SQL("CREATE ROLE {} WITH ").format(role) + options)
    else:
        cursor.execute(sql.SQL("ALTER ROLE {} WITH ").format(role) + options)
        cursor.execute(sql.SQL("ALTER ROLE {} RESET ALL").format(role))
    connection.pgconn.change_password(
        target.readonly_role.encode("ascii"),
        target.readonly_password.encode("utf-8"),
    )
    cursor.execute(
        """
        SELECT granted.rolname
        FROM pg_auth_members AS membership
        JOIN pg_roles AS granted ON granted.oid = membership.roleid
        JOIN pg_roles AS member ON member.oid = membership.member
        WHERE member.rolname = %s
        """,
        (target.readonly_role,),
    )
    for (granted_role,) in cursor.fetchall():
        cursor.execute(sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(granted_role), role))
    cursor.execute(sql.SQL("ALTER ROLE {} SET search_path TO public").format(role))
    cursor.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only TO on").format(role))


def _grant_select_only(
    cursor: psycopg.Cursor[Any],
    target: ReadonlyProvisioningTarget,
) -> None:
    role = sql.Identifier(target.readonly_role)
    database = sql.Identifier(target.database)
    owner = sql.Identifier(target.owner_role)
    cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(database, role))
    cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, role))
    cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA public FROM {}").format(role))
    cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
    cursor.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}").format(role)
    )
    cursor.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(role))
    cursor.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {}").format(role)
    )
    cursor.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
            "REVOKE ALL PRIVILEGES ON TABLES FROM {}"
        ).format(owner, role)
    )
    cursor.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT ON TABLES TO {}"
        ).format(owner, role)
    )
    cursor.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
            "REVOKE ALL PRIVILEGES ON SEQUENCES FROM {}"
        ).format(owner, role)
    )


def _verify_select_only(
    cursor: psycopg.Cursor[Any],
    target: ReadonlyProvisioningTarget,
) -> None:
    role = target.readonly_role
    cursor.execute(
        """
        SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit,
               rolreplication, rolbypassrls
        FROM pg_roles WHERE rolname = %s
        """,
        (role,),
    )
    if cursor.fetchone() != (True, False, False, False, False, False, False):
        raise ReadonlyProvisioningError("read-only role attributes are unsafe")
    cursor.execute(
        """
        SELECT has_database_privilege(%s, %s, 'CONNECT'),
               has_database_privilege(%s, %s, 'CREATE'),
               has_database_privilege(%s, %s, 'TEMPORARY'),
               has_schema_privilege(%s, 'public', 'USAGE'),
               has_schema_privilege(%s, 'public', 'CREATE')
        """,
        (role, target.database, role, target.database, role, target.database, role, role),
    )
    if cursor.fetchone() != (True, False, False, True, False):
        raise ReadonlyProvisioningError("read-only database or schema privileges are unsafe")
    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_auth_members AS membership
            JOIN pg_roles AS member ON member.oid = membership.member
            WHERE member.rolname = %s
        )
        """,
        (role,),
    ):
        raise ReadonlyProvisioningError("read-only role still inherits another role")
    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_database AS database
            WHERE database.datallowconn
              AND database.datname <> %s
              AND database.datname <> 'template0'
              AND has_database_privilege(%s, database.oid, 'CONNECT')
        )
        """,
        (target.database, role),
    ):
        raise ReadonlyProvisioningError("read-only role can connect to another database")
    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_namespace AS namespace
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
        raise ReadonlyProvisioningError("read-only role can access another schema")
    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND (
                  NOT has_table_privilege(%s, relation.oid, 'SELECT')
                  OR has_table_privilege(%s, relation.oid, 'INSERT')
                  OR has_table_privilege(%s, relation.oid, 'UPDATE')
                  OR has_table_privilege(%s, relation.oid, 'DELETE')
                  OR has_table_privilege(%s, relation.oid, 'TRUNCATE')
                  OR has_table_privilege(%s, relation.oid, 'REFERENCES')
                  OR has_table_privilege(%s, relation.oid, 'TRIGGER')
              )
        )
        """,
        (role, role, role, role, role, role, role),
    ):
        raise ReadonlyProvisioningError("read-only table privileges are unsafe")
    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_class AS sequence
            JOIN pg_namespace AS namespace ON namespace.oid = sequence.relnamespace
            WHERE namespace.nspname = 'public' AND sequence.relkind = 'S'
              AND (
                  has_sequence_privilege(%s, sequence.oid, 'USAGE')
                  OR has_sequence_privilege(%s, sequence.oid, 'SELECT')
                  OR has_sequence_privilege(%s, sequence.oid, 'UPDATE')
              )
        )
        """,
        (role, role, role),
    ):
        raise ReadonlyProvisioningError("read-only role has sequence privileges")
    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_proc AS routine
            JOIN pg_namespace AS namespace ON namespace.oid = routine.pronamespace
            WHERE namespace.nspname = 'public'
              AND has_function_privilege(%s, routine.oid, 'EXECUTE')
        )
        """,
        (role,),
    ):
        raise ReadonlyProvisioningError("read-only role can execute a public-schema routine")
    if _flag(
        cursor,
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_default_acl AS defaults
            CROSS JOIN LATERAL aclexplode(defaults.defaclacl) AS privilege
            JOIN pg_roles AS grantee ON grantee.oid = privilege.grantee
            LEFT JOIN pg_roles AS owner ON owner.oid = defaults.defaclrole
            LEFT JOIN pg_namespace AS namespace ON namespace.oid = defaults.defaclnamespace
            WHERE grantee.rolname = %s
              AND NOT (
                  owner.rolname = %s
                  AND namespace.nspname = 'public'
                  AND defaults.defaclobjtype = 'r'
                  AND privilege.privilege_type = 'SELECT'
              )
        )
        """,
        (role, target.owner_role),
    ):
        raise ReadonlyProvisioningError("read-only default privileges are unsafe")


def _verify_readonly_connection(target: ReadonlyProvisioningTarget) -> None:
    with psycopg.connect(target.readonly_conninfo, connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_database(), session_user, current_user, "
                "current_setting('default_transaction_read_only')"
            )
            if cursor.fetchone() != (
                target.database,
                target.readonly_role,
                target.readonly_role,
                "on",
            ):
                raise ReadonlyProvisioningError("read-only connection identity is invalid")
            cursor.execute("SELECT version_num FROM public.alembic_version")
            if cursor.fetchone() is None:
                raise ReadonlyProvisioningError("read-only connection cannot read schema state")
        connection.rollback()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ WRITE")
                cursor.execute("UPDATE public.alembic_version SET version_num = version_num")
        except psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction:
            connection.rollback()
        else:
            connection.rollback()
            raise ReadonlyProvisioningError("read-only role completed a forbidden write")


def provision_readonly(target: ReadonlyProvisioningTarget) -> None:
    with psycopg.connect(target.admin_conninfo, connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), session_user, current_user")
            if cursor.fetchone() != (
                target.database,
                target.owner_role,
                target.owner_role,
            ):
                raise ReadonlyProvisioningError("migration connection identity is invalid")
            _ensure_role_is_dedicated(cursor, target)
            _configure_role(connection, cursor, target)
            _grant_select_only(cursor, target)
            _verify_select_only(cursor, target)
    _verify_readonly_connection(target)


def main() -> int:
    try:
        provision_readonly(load_readonly_target())
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

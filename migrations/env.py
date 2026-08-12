import os
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from finbot.adapters.database.models import Base

config = context.config
target_metadata = Base.metadata


def database_url(fallback: str) -> str:
    configured = os.getenv("DATABASE_URL")
    if configured:
        return configured
    secret = Path("/run/secrets/database_url")
    if secret.is_file():
        return secret.read_text(encoding="utf-8").strip()
    return fallback


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(config.get_main_option("sqlalchemy.url")),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url(section.get("sqlalchemy.url", ""))
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

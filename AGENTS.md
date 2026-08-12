# Finbot agent instructions

Run `uv sync --frozen`, `make check`, and `docker compose config` before handoff when available.
Keep domain and application layers independent of Telegram and SQLAlchemy. Handlers stay thin.
Never use `float` for money; use integer minor units and `Decimal` only at input boundaries.
Never commit secrets, real Telegram data, or production exports. Database changes require Alembic migrations.
Every feature needs unit tests; PostgreSQL integration tests must use a database name ending in `_test`.
Logs are JSON and must not contain tokens, credentials, Telegram IDs, amounts, descriptions, or message text.

Definition of done: frozen dependencies, migrations, Docker Compose, owner-only private-chat access, idempotent updates,
reports, CSV export, backup/restore documentation, passing available checks, and an accurate PLAN.md.

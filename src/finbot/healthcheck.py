import asyncio
import json
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from finbot.adapters.database.session import session_factory
from finbot.config import Settings
from finbot.observability.logging import safe_error_class


async def check() -> None:
    settings = Settings.from_secret_or_env()
    factory = session_factory(settings)
    async with factory() as session:
        await session.execute(text("SELECT 1"))
        current = await session.scalar(text("SELECT version_num FROM alembic_version"))
    expected = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    if not current or current != expected:
        raise RuntimeError("database migration version does not match application head")


def main() -> int:
    try:
        asyncio.run(check())
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

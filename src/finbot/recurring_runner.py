from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from finbot.adapters.database.recurring_runner import SqlAlchemyRecurringRunner
from finbot.config import RecurringRunnerSettings
from finbot.observability.logging import configure, safe_error_class

_LOGGER = logging.getLogger("finbot.database.recurring_runner_process")


async def run(settings: RecurringRunnerSettings, *, once: bool = False) -> None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    runner = SqlAlchemyRecurringRunner(sessions)
    _LOGGER.info("recurring_runner_started")
    try:
        while True:
            await runner.tick()
            if once:
                return
            await asyncio.sleep(settings.interval_seconds)
    finally:
        await engine.dispose()
        _LOGGER.info("recurring_runner_stopped")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run bounded recurring draft generation")
    parser.add_argument("--once", action="store_true", help="run one bounded tick and exit")
    return parser


def main() -> None:
    try:
        arguments = _parser().parse_args()
        settings = RecurringRunnerSettings.from_secret_or_env()
        configure(settings.log_level)
        asyncio.run(run(settings, once=arguments.once))
    except KeyboardInterrupt:
        raise SystemExit(0) from None
    except Exception as exc:
        print(
            json.dumps(
                {"error_class": safe_error_class(exc), "status": "error"},
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

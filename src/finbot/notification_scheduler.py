from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from finbot.adapters.database.notification_scheduler import (
    SqlAlchemyNotificationScheduler,
)
from finbot.application.notifications import NotificationDigest
from finbot.config import NotificationSchedulerSettings
from finbot.observability.logging import configure, safe_error_class

_LOGGER = logging.getLogger("finbot.notification.scheduler_process")


async def run(settings: NotificationSchedulerSettings, *, once: bool = False) -> None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    scheduler = SqlAlchemyNotificationScheduler(
        sessions,
        NotificationDigest(settings.notification_key_bytes),
    )
    _LOGGER.info("notification_scheduler_started")
    try:
        while True:
            result = await scheduler.tick()
            if result.skipped and not result.owners:
                log_result = "ignored"
            else:
                log_result = "rejected" if result.skipped else "success"
            _LOGGER.info(
                "notification_scheduler_tick_completed",
                extra={"result": log_result},
            )
            if once:
                return
            await asyncio.sleep(settings.interval_seconds)
    finally:
        await engine.dispose()
        _LOGGER.info("notification_scheduler_stopped")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run bounded notification scheduling")
    parser.add_argument("--once", action="store_true", help="run one bounded tick and exit")
    return parser


def main() -> None:
    try:
        arguments = _parser().parse_args()
        settings = NotificationSchedulerSettings.from_secret_or_env()
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

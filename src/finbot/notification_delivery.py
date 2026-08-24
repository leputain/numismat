from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from aiogram import Bot
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from finbot.adapters.database.notification_delivery import (
    SqlAlchemyNotificationDelivery,
)
from finbot.adapters.telegram.notification_sender import AiogramNotificationSender
from finbot.config import NotificationDeliverySettings
from finbot.observability.logging import configure, safe_error_class

_LOGGER = logging.getLogger("finbot.notification.delivery_process")


async def run(settings: NotificationDeliverySettings, *, once: bool = False) -> None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    bot = Bot(token=settings.telegram_bot_token)
    delivery = SqlAlchemyNotificationDelivery(
        sessions,
        AiogramNotificationSender(bot),
        allowed_telegram_user_ids=settings.effective_telegram_user_ids,
    )
    _LOGGER.info("notification_delivery_started")
    try:
        while True:
            result = await delivery.tick()
            _LOGGER.info(
                "notification_delivery_tick_completed",
                extra={"result": "retry" if result.failed else "success"},
            )
            if once:
                return
            await asyncio.sleep(settings.interval_seconds)
    finally:
        await bot.session.close()
        await engine.dispose()
        _LOGGER.info("notification_delivery_stopped")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deliver leased notification jobs")
    parser.add_argument("--once", action="store_true", help="run one bounded tick and exit")
    return parser


def main() -> None:
    try:
        arguments = _parser().parse_args()
        settings = NotificationDeliverySettings.from_secret_or_env()
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

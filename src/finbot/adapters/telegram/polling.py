from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Sequence
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.methods import GetUpdates
from aiogram.types import Update
from aiogram.utils.backoff import Backoff, BackoffConfig

DEFAULT_BACKOFF = BackoffConfig(min_delay=1.0, max_delay=30.0, factor=2.0, jitter=0.1)

_LOGGER = logging.getLogger("finbot.polling")


async def _await_or_stop[T](awaitable: Awaitable[T], stop_event: asyncio.Event) -> T | None:
    operation = asyncio.ensure_future(awaitable)
    stopped = asyncio.create_task(stop_event.wait())
    try:
        done, pending = await asyncio.wait(
            {operation, stopped}, return_when=asyncio.FIRST_COMPLETED
        )
    except BaseException:
        operation.cancel()
        stopped.cancel()
        await asyncio.gather(operation, stopped, return_exceptions=True)
        raise
    else:
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    if stopped in done:
        if operation in done:
            # Retrieve an already completed result/exception before honouring shutdown.
            return operation.result()
        return None
    return operation.result()


async def _backoff_or_stop(backoff: Backoff, stop_event: asyncio.Event) -> bool:
    delay = next(backoff)
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except TimeoutError:
        return False
    return True


async def _dispatch_until_success(
    bot: Bot,
    dispatcher: Dispatcher,
    update: Update,
    stop_event: asyncio.Event,
    backoff_config: BackoffConfig,
) -> bool:
    backoff = Backoff(config=backoff_config)
    while not stop_event.is_set():
        try:
            await dispatcher.feed_update(bot, update)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not advance the getUpdates offset.  The same update is retried serially,
            # so later updates can never overtake a failed application/DB transaction.
            _LOGGER.error(
                "polling_update_failed",
                exc_info=True,
                extra={"result": "retry"},
            )
            if await _backoff_or_stop(backoff, stop_event):
                return False
        else:
            return True
    return False


async def run(
    bot: Bot,
    dispatcher: Dispatcher,
    *,
    polling_timeout: int = 30,
    allowed_updates: Sequence[str] | None = None,
    backoff_config: BackoffConfig = DEFAULT_BACKOFF,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Fetch and dispatch Telegram updates one at a time.

    The Telegram offset is advanced only after ``feed_update`` returns successfully.
    Exceptions are retried with bounded backoff and block every later update.  The caller
    owns the bot session; cancellation or ``stop_event`` still runs dispatcher shutdown
    hooks before returning.
    """

    if not 1 <= polling_timeout <= 50:
        raise ValueError("polling_timeout must be between 1 and 50 seconds")

    shutdown = stop_event or asyncio.Event()
    update_types = (
        list(allowed_updates)
        if allowed_updates is not None
        else dispatcher.resolve_used_update_types()
    )
    request = GetUpdates(timeout=polling_timeout, allowed_updates=update_types)
    fetch_backoff = Backoff(config=backoff_config)
    fetch_failed = False
    workflow_data = {
        "dispatcher": dispatcher,
        "bots": (bot,),
        **dispatcher.workflow_data,
    }
    workflow_data.pop("bot", None)

    await dispatcher.emit_startup(bot=bot, **workflow_data)
    _LOGGER.info("polling_started")
    try:
        while not shutdown.is_set():
            try:
                updates = await _await_or_stop(
                    bot(request, request_timeout=polling_timeout + 10), shutdown
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                fetch_failed = True
                _LOGGER.error(
                    "polling_fetch_failed",
                    exc_info=True,
                    extra={"result": "retry"},
                )
                if await _backoff_or_stop(fetch_backoff, shutdown):
                    break
                continue

            if updates is None:
                break
            if fetch_failed:
                _LOGGER.info("polling_fetch_recovered")
                fetch_backoff.reset()
                fetch_failed = False

            for update in updates:
                if shutdown.is_set():
                    break
                handled = await _dispatch_until_success(
                    bot, dispatcher, update, shutdown, backoff_config
                )
                if not handled:
                    break
                # Telegram acknowledges update IDs below this value on the next request.
                request.offset = update.update_id + 1
    finally:
        _LOGGER.info("polling_stopped", extra={"result": "stopped"})
        await dispatcher.emit_shutdown(bot=bot, **workflow_data)

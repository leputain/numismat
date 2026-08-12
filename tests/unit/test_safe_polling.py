from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, cast

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import GetUpdates
from aiogram.types import Update
from aiogram.utils.backoff import BackoffConfig

from finbot.adapters.telegram.polling import run

FAST_BACKOFF = BackoffConfig(min_delay=0.001, max_delay=0.002, factor=1.1, jitter=0.0)


def _update(update_id: int) -> Update:
    return Update(update_id=update_id)


class FakeBot:
    def __init__(
        self,
        responses: Sequence[list[Update] | Exception],
        stop_event: asyncio.Event,
        *,
        stop_after_calls: int | None = None,
    ) -> None:
        self.responses = list(responses)
        self.stop_event = stop_event
        self.stop_after_calls = stop_after_calls
        self.offsets: list[int | None] = []

    async def __call__(
        self, method: GetUpdates, *, request_timeout: int | None = None
    ) -> list[Update]:
        del request_timeout
        self.offsets.append(method.offset)
        if self.stop_after_calls == len(self.offsets):
            self.stop_event.set()
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class BlockingBot:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def __call__(
        self, method: GetUpdates, *, request_timeout: int | None = None
    ) -> list[Update]:
        del method, request_timeout
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        return []


class FakeDispatcher:
    def __init__(
        self,
        stop_event: asyncio.Event,
        *,
        failures: dict[int, int] | None = None,
        stop_on_failure: bool = False,
    ) -> None:
        self.workflow_data: dict[str, Any] = {}
        self.stop_event = stop_event
        self.failures = failures or {}
        self.stop_on_failure = stop_on_failure
        self.feed_order: list[int] = []
        self.active = 0
        self.max_active = 0
        self.started = 0
        self.stopped = 0

    def resolve_used_update_types(self) -> list[str]:
        return ["message"]

    async def emit_startup(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.started += 1

    async def emit_shutdown(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.stopped += 1

    async def feed_update(self, bot: Bot, update: Update, **kwargs: Any) -> None:
        del bot, kwargs
        self.feed_order.append(update.update_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        remaining = self.failures.get(update.update_id, 0)
        if remaining:
            self.failures[update.update_id] = remaining - 1
            if self.stop_on_failure:
                self.stop_event.set()
            raise RuntimeError("synthetic application failure")


@pytest.mark.asyncio
async def test_updates_are_dispatched_sequentially_and_offset_advances_after_success() -> None:
    stop = asyncio.Event()
    bot = FakeBot([[_update(7), _update(8)], []], stop, stop_after_calls=2)
    dispatcher = FakeDispatcher(stop)

    await run(
        cast(Bot, bot),
        cast(Dispatcher, dispatcher),
        polling_timeout=1,
        backoff_config=FAST_BACKOFF,
        stop_event=stop,
    )

    assert dispatcher.feed_order == [7, 8]
    assert dispatcher.max_active == 1
    assert bot.offsets == [None, 9]
    assert (dispatcher.started, dispatcher.stopped) == (1, 1)


@pytest.mark.asyncio
async def test_failed_update_is_retried_before_later_update_or_offset_advance() -> None:
    stop = asyncio.Event()
    bot = FakeBot([[_update(41), _update(42)], []], stop, stop_after_calls=2)
    dispatcher = FakeDispatcher(stop, failures={41: 1})

    await run(
        cast(Bot, bot),
        cast(Dispatcher, dispatcher),
        polling_timeout=1,
        backoff_config=FAST_BACKOFF,
        stop_event=stop,
    )

    assert dispatcher.feed_order == [41, 41, 42]
    assert bot.offsets == [None, 43]


@pytest.mark.asyncio
async def test_stopping_after_application_failure_does_not_confirm_update() -> None:
    stop = asyncio.Event()
    bot = FakeBot([[_update(55)]], stop)
    dispatcher = FakeDispatcher(stop, failures={55: 1}, stop_on_failure=True)

    await run(
        cast(Bot, bot),
        cast(Dispatcher, dispatcher),
        polling_timeout=1,
        backoff_config=FAST_BACKOFF,
        stop_event=stop,
    )

    assert dispatcher.feed_order == [55]
    assert bot.offsets == [None]
    assert dispatcher.stopped == 1


@pytest.mark.asyncio
async def test_fetch_failure_retries_same_offset_then_recovers() -> None:
    stop = asyncio.Event()
    bot = FakeBot(
        [RuntimeError("synthetic network failure"), [_update(3)], []],
        stop,
        stop_after_calls=3,
    )
    dispatcher = FakeDispatcher(stop)

    await run(
        cast(Bot, bot),
        cast(Dispatcher, dispatcher),
        polling_timeout=1,
        backoff_config=FAST_BACKOFF,
        stop_event=stop,
    )

    assert bot.offsets == [None, None, 4]
    assert dispatcher.feed_order == [3]


@pytest.mark.asyncio
async def test_cancellation_cancels_long_poll_and_runs_shutdown_hooks() -> None:
    stop = asyncio.Event()
    bot = BlockingBot()
    dispatcher = FakeDispatcher(stop)
    task = asyncio.create_task(
        run(
            cast(Bot, bot),
            cast(Dispatcher, dispatcher),
            polling_timeout=1,
            backoff_config=FAST_BACKOFF,
            stop_event=stop,
        )
    )
    await bot.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert bot.cancelled.is_set()
    assert (dispatcher.started, dispatcher.stopped) == (1, 1)

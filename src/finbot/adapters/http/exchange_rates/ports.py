from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol, Self

from finbot.adapters.http.auth.ports import AuthPersistence
from finbot.application.exchange_rates import ExchangeRateReader
from finbot.application.ports import FinanceReader


class ExchangeRateQueryUnitOfWork(Protocol):
    auth: AuthPersistence
    finance: FinanceReader
    rates: ExchangeRateReader

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> bool | None: ...


class ExchangeRateQueryUnitOfWorkFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[ExchangeRateQueryUnitOfWork]: ...


__all__ = ["ExchangeRateQueryUnitOfWork", "ExchangeRateQueryUnitOfWorkFactory"]

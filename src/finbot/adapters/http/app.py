import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI

from finbot import __version__
from finbot.adapters.bank_import import HmacBankImportDigester, StrictBankCsvParser
from finbot.adapters.database.repositories.http_auth import SqlAlchemyAuthUnitOfWorkFactory
from finbot.adapters.database.repositories.http_bank_imports import (
    SqlAlchemyBankImportQueryUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.http_budgets import (
    SqlAlchemyBudgetQueryUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.http_catalogs import (
    SqlAlchemyCatalogQueryUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.http_exchange_rates import (
    SqlAlchemyExchangeRateQueryUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.http_finance import (
    SqlAlchemyFinanceQueryUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.http_mutations import (
    SqlAlchemyHttpMutationUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.http_recurring import (
    SqlAlchemyRecurringQueryUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.readiness import SqlAlchemyReadinessProbe
from finbot.adapters.database.session import session_factory
from finbot.adapters.http.auth.crypto import HttpSecurityDigester
from finbot.adapters.http.auth.headers import AuthSecurityHeadersMiddleware
from finbot.adapters.http.auth.service import TelegramAuthService
from finbot.adapters.http.bank_imports.cursor import BankImportCursorCodec
from finbot.adapters.http.bank_imports.service import HttpBankImportService
from finbot.adapters.http.budgets.cursor import BudgetCursorCodec
from finbot.adapters.http.budgets.service import HttpBudgetService
from finbot.adapters.http.catalogs.service import HttpCatalogService
from finbot.adapters.http.errors import install_error_handlers
from finbot.adapters.http.exchange_rates.cursor import ExchangeRateCursorCodec
from finbot.adapters.http.exchange_rates.service import HttpExchangeRateService
from finbot.adapters.http.finance.cursor import TransactionCursorCodec
from finbot.adapters.http.finance.service import FinanceQueryService
from finbot.adapters.http.maintenance import HttpSecurityMaintenance
from finbot.adapters.http.mutations.service import (
    HttpMutationExecutor,
    HttpRevisionMutationService,
)
from finbot.adapters.http.ports import ReadinessProbe
from finbot.adapters.http.recurring.cursor import RecurringCursorCodec
from finbot.adapters.http.recurring.service import HttpRecurringService
from finbot.adapters.http.request_logging import PrivacySafeRequestLoggingMiddleware
from finbot.adapters.http.routes.auth import auth_router
from finbot.adapters.http.routes.bank_imports import bank_import_router
from finbot.adapters.http.routes.budgets import budget_router
from finbot.adapters.http.routes.catalogs import catalog_router
from finbot.adapters.http.routes.exchange_rates import exchange_rate_router
from finbot.adapters.http.routes.finance import finance_router
from finbot.adapters.http.routes.health import health_router
from finbot.adapters.http.routes.meta import meta_router
from finbot.adapters.http.routes.mutations import mutation_router
from finbot.adapters.http.routes.recurring import recurring_router
from finbot.application.use_cases.bank_imports import BankImportPreparer
from finbot.config import Settings

OPENAPI_TAGS = [
    {"name": "meta", "description": "Versioned API metadata."},
    {"name": "health", "description": "Process and database health probes."},
    {"name": "auth", "description": "Telegram Mini App authentication and session lifecycle."},
    {"name": "finance", "description": "Owner-scoped dashboard, reports, and transactions."},
    {"name": "catalogs", "description": "Bounded owner-scoped accounts and categories."},
    {"name": "budgets", "description": "Owner-scoped expense budgets and bounded progress."},
    {
        "name": "recurring",
        "description": "Review-first recurring schedules and bounded due instances.",
    },
    {
        "name": "exchange-rates",
        "description": "Immutable manual rates and explicitly version-pinned conversion.",
    },
    {
        "name": "bank-imports",
        "description": "Review-first staged CSV imports and explicit reconciliation.",
    },
    {
        "name": "mutations",
        "description": "Revision-safe review-first draft and transaction mutations.",
    },
]


def _alembic_head() -> str:
    revision = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    if not revision:
        raise RuntimeError("Alembic head revision is unavailable")
    return revision


def _lifespan(
    maintenance: HttpSecurityMaintenance | None,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if maintenance is None:
            yield
            return
        task = asyncio.create_task(maintenance.run(), name="http-security-maintenance")
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    return lifespan


def create_app(
    *,
    settings: Settings | None = None,
    readiness_probe: ReadinessProbe | None = None,
    auth_service: TelegramAuthService | None = None,
    auth_origin: str | None = None,
    finance_service: FinanceQueryService | None = None,
    catalog_service: HttpCatalogService | None = None,
    catalog_origin: str | None = None,
    budget_service: HttpBudgetService | None = None,
    budget_origin: str | None = None,
    recurring_service: HttpRecurringService | None = None,
    recurring_origin: str | None = None,
    exchange_rate_service: HttpExchangeRateService | None = None,
    exchange_rate_origin: str | None = None,
    bank_import_service: HttpBankImportService | None = None,
    bank_import_origin: str | None = None,
    mutation_service: HttpRevisionMutationService | None = None,
    mutation_origin: str | None = None,
    maintenance: HttpSecurityMaintenance | None = None,
) -> FastAPI:
    if readiness_probe is None:
        runtime_settings = settings or Settings.from_secret_or_env()
        readiness_probe = SqlAlchemyReadinessProbe(
            session_factory(runtime_settings),
            expected_revision=_alembic_head(),
        )

    app = FastAPI(
        title="Numismat API",
        summary="Private owner-only finance API",
        description="Versioned HTTP adapter over the shared Numismat application contracts.",
        version=__version__,
        openapi_url="/api/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
        openapi_tags=OPENAPI_TAGS,
        license_info={
            "name": "Apache License 2.0",
            "identifier": "Apache-2.0",
        },
        lifespan=_lifespan(maintenance),
    )
    app.router.redirect_slashes = False
    install_error_handlers(app)
    app.add_middleware(PrivacySafeRequestLoggingMiddleware)
    app.add_middleware(AuthSecurityHeadersMiddleware)
    app.include_router(meta_router())
    app.include_router(health_router(readiness_probe))
    if auth_service is not None or auth_origin is not None:
        if auth_service is None or auth_origin is None:
            raise ValueError("auth service and origin must be configured together")
        app.include_router(auth_router(auth_service, expected_origin=auth_origin))
    if finance_service is not None:
        app.include_router(finance_router(finance_service))
    if catalog_service is not None or catalog_origin is not None:
        if catalog_service is None or catalog_origin is None:
            raise ValueError("catalog service and origin must be configured together")
        if auth_origin is not None and auth_origin != catalog_origin:
            raise ValueError("auth and catalog origins must match")
        app.include_router(catalog_router(catalog_service, expected_origin=catalog_origin))
    if budget_service is not None or budget_origin is not None:
        if budget_service is None or budget_origin is None:
            raise ValueError("budget service and origin must be configured together")
        if auth_origin is not None and auth_origin != budget_origin:
            raise ValueError("auth and budget origins must match")
        if catalog_origin is not None and catalog_origin != budget_origin:
            raise ValueError("catalog and budget origins must match")
        app.include_router(budget_router(budget_service, expected_origin=budget_origin))
    if recurring_service is not None or recurring_origin is not None:
        if recurring_service is None or recurring_origin is None:
            raise ValueError("recurring service and origin must be configured together")
        for configured_origin in (auth_origin, catalog_origin, budget_origin):
            if configured_origin is not None and configured_origin != recurring_origin:
                raise ValueError("recurring and existing HTTP origins must match")
        app.include_router(recurring_router(recurring_service, expected_origin=recurring_origin))
    if exchange_rate_service is not None or exchange_rate_origin is not None:
        if exchange_rate_service is None or exchange_rate_origin is None:
            raise ValueError("exchange-rate service and origin must be configured together")
        for configured_origin in (
            auth_origin,
            catalog_origin,
            budget_origin,
            recurring_origin,
        ):
            if configured_origin is not None and configured_origin != exchange_rate_origin:
                raise ValueError("exchange-rate and existing HTTP origins must match")
        app.include_router(
            exchange_rate_router(
                exchange_rate_service,
                expected_origin=exchange_rate_origin,
            )
        )
    if bank_import_service is not None or bank_import_origin is not None:
        if bank_import_service is None or bank_import_origin is None:
            raise ValueError("bank-import service and origin must be configured together")
        for configured_origin in (
            auth_origin,
            catalog_origin,
            budget_origin,
            recurring_origin,
            exchange_rate_origin,
        ):
            if configured_origin is not None and configured_origin != bank_import_origin:
                raise ValueError("bank-import and existing HTTP origins must match")
        app.include_router(
            bank_import_router(bank_import_service, expected_origin=bank_import_origin)
        )
    if mutation_service is not None or mutation_origin is not None:
        if mutation_service is None or mutation_origin is None:
            raise ValueError("mutation service and origin must be configured together")
        if auth_origin is not None and auth_origin != mutation_origin:
            raise ValueError("auth and mutation origins must match")
        if catalog_origin is not None and catalog_origin != mutation_origin:
            raise ValueError("catalog and mutation origins must match")
        if budget_origin is not None and budget_origin != mutation_origin:
            raise ValueError("budget and mutation origins must match")
        if recurring_origin is not None and recurring_origin != mutation_origin:
            raise ValueError("recurring and mutation origins must match")
        if exchange_rate_origin is not None and exchange_rate_origin != mutation_origin:
            raise ValueError("exchange-rate and mutation origins must match")
        if bank_import_origin is not None and bank_import_origin != mutation_origin:
            raise ValueError("bank-import and mutation origins must match")
        app.include_router(mutation_router(mutation_service, expected_origin=mutation_origin))
    if (
        auth_service is not None
        or finance_service is not None
        or catalog_service is not None
        or budget_service is not None
        or recurring_service is not None
        or exchange_rate_service is not None
        or bank_import_service is not None
        or mutation_service is not None
    ):
        original_openapi = app.openapi

        def auth_openapi() -> dict[str, object]:
            schema = original_openapi()
            components = schema.setdefault("components", {})
            security_schemes = components.setdefault("securitySchemes", {})
            security_schemes["SessionCookie"] = {
                "in": "cookie",
                "name": "__Host-numismat_session",
                "type": "apiKey",
            }
            return schema

        app.openapi = auth_openapi  # type: ignore[method-assign]
    return app


def create_runtime_app(*, settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or Settings.from_secret_or_env()
    if (
        runtime_settings.http_security_key is None
        or runtime_settings.miniapp_origin is None
        or runtime_settings.bank_import_key_bytes is None
    ):
        raise RuntimeError("HTTP auth security configuration is required")
    sessions = session_factory(runtime_settings)
    digester = HttpSecurityDigester(runtime_settings.http_security_key)
    mutation_executor = HttpMutationExecutor(
        digester=digester,
        uow_factory=SqlAlchemyHttpMutationUnitOfWorkFactory(sessions),
    )
    return create_app(
        readiness_probe=SqlAlchemyReadinessProbe(
            sessions,
            expected_revision=_alembic_head(),
        ),
        auth_service=TelegramAuthService(
            bot_token=runtime_settings.telegram_bot_token,
            owner_telegram_user_id=runtime_settings.owner_telegram_user_id,
            digester=digester,
            uow_factory=SqlAlchemyAuthUnitOfWorkFactory(sessions),
        ),
        auth_origin=runtime_settings.miniapp_origin,
        finance_service=FinanceQueryService(
            digester=digester,
            cursor_codec=TransactionCursorCodec(runtime_settings.http_security_key),
            uow_factory=SqlAlchemyFinanceQueryUnitOfWorkFactory(sessions),
        ),
        catalog_service=HttpCatalogService(
            digester=digester,
            query_uow_factory=SqlAlchemyCatalogQueryUnitOfWorkFactory(sessions),
            mutation_executor=mutation_executor,
        ),
        catalog_origin=runtime_settings.miniapp_origin,
        budget_service=HttpBudgetService(
            digester=digester,
            cursor_codec=BudgetCursorCodec(runtime_settings.http_security_key),
            query_uow_factory=SqlAlchemyBudgetQueryUnitOfWorkFactory(sessions),
            mutation_executor=mutation_executor,
        ),
        budget_origin=runtime_settings.miniapp_origin,
        recurring_service=HttpRecurringService(
            digester=digester,
            cursor_codec=RecurringCursorCodec(runtime_settings.http_security_key),
            query_uow_factory=SqlAlchemyRecurringQueryUnitOfWorkFactory(sessions),
            mutation_executor=mutation_executor,
        ),
        recurring_origin=runtime_settings.miniapp_origin,
        exchange_rate_service=HttpExchangeRateService(
            digester=digester,
            cursor_codec=ExchangeRateCursorCodec(runtime_settings.http_security_key),
            query_uow_factory=SqlAlchemyExchangeRateQueryUnitOfWorkFactory(sessions),
            mutation_executor=mutation_executor,
        ),
        exchange_rate_origin=runtime_settings.miniapp_origin,
        bank_import_service=HttpBankImportService(
            digester=digester,
            preparer=BankImportPreparer(
                StrictBankCsvParser(),
                HmacBankImportDigester(runtime_settings.bank_import_key_bytes),
            ),
            cursor_codec=BankImportCursorCodec(runtime_settings.http_security_key),
            admission_uow_factory=SqlAlchemyAuthUnitOfWorkFactory(sessions),
            query_uow_factory=SqlAlchemyBankImportQueryUnitOfWorkFactory(sessions),
            mutation_executor=mutation_executor,
        ),
        bank_import_origin=runtime_settings.miniapp_origin,
        mutation_service=HttpRevisionMutationService(mutation_executor),
        mutation_origin=runtime_settings.miniapp_origin,
        maintenance=HttpSecurityMaintenance(sessions),
    )

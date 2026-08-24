from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from finbot.adapters.bank_import.csv_parser import StrictBankCsvParser
from finbot.adapters.bank_import.digests import HmacBankImportDigester
from finbot.adapters.database.models import (
    Account,
    Budget,
    Category,
    Draft,
    HttpIdempotencyRecord,
    ImportBatch,
    ImportRow,
    ProcessedUpdate,
    TelegramResponseOutbox,
    Transaction,
    User,
    WebSession,
)
from finbot.adapters.database.repositories.http_auth import (
    SqlAlchemyAuthUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.http_bank_imports import (
    SqlAlchemyBankImportQueryUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.http_budgets import (
    SqlAlchemyBudgetQueryUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.http_catalogs import (
    SqlAlchemyCatalogQueryUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.http_finance import (
    SqlAlchemyFinanceQueryUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.http_mutations import (
    SqlAlchemyHttpMutationUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.web_sessions import (
    SqlAlchemyWebSessionRepository,
)
from finbot.adapters.database.services.onboarding import ensure_owner_user
from finbot.adapters.database.services.outbox import (
    bind_telegram_draft_presentation,
    response_draft_belongs_to_private_chat,
)
from finbot.adapters.http.app import create_app
from finbot.adapters.http.auth.cookies import CSRF_COOKIE, SESSION_COOKIE
from finbot.adapters.http.auth.crypto import HttpSecurityDigester, OpaqueAuthTokens
from finbot.adapters.http.auth.service import TelegramAuthService
from finbot.adapters.http.bank_imports.cursor import BankImportCursorCodec
from finbot.adapters.http.bank_imports.service import HttpBankImportService
from finbot.adapters.http.budgets.cursor import BudgetCursorCodec
from finbot.adapters.http.budgets.service import HttpBudgetService
from finbot.adapters.http.catalogs.service import HttpCatalogService
from finbot.adapters.http.finance.cursor import TransactionCursorCodec
from finbot.adapters.http.finance.service import FinanceQueryService
from finbot.adapters.http.mutations.service import (
    HttpMutationExecutor,
    HttpRevisionMutationService,
    MutationCredentials,
)
from finbot.application.services.onboarding import INITIAL_CATEGORIES
from finbot.application.use_cases.bank_imports import BankImportPreparer

DATABASE_URL = os.environ["TEST_DATABASE_URL"]
BOT_TOKEN = "123456:synthetic-multitenant-token"
ORIGIN = "https://multi-tenant.integration.test"
SECURITY_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class _ReadinessStub:
    async def check(self) -> None:
        return None


@dataclass(frozen=True, slots=True, repr=False)
class _Tenant:
    owner_id: UUID = field(repr=False)
    telegram_user_id: int = field(repr=False)
    account_id: UUID = field(repr=False)
    expense_category_id: UUID = field(repr=False)
    locale: str


@dataclass(frozen=True, slots=True, repr=False)
class _Tokens:
    session: str = field(repr=False)
    csrf: str = field(repr=False)

    def credentials(self, idempotency_key: str) -> MutationCredentials:
        return MutationCredentials(
            session_token=self.session,
            session_binding=HttpSecurityDigester(SECURITY_KEY).session_binding(self.session),
            csrf_cookie=self.csrf,
            csrf_header=self.csrf,
            idempotency_key=idempotency_key,
        )


def _opaque_token(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes((byte,)) * 32).rstrip(b"=").decode("ascii")


def _synthetic_telegram_user_id() -> int:
    return 1_000_000_000_000 + uuid4().int % 1_000_000_000_000


def _synthetic_update_id() -> int:
    return 3_000_000_000_000 + uuid4().int % 1_000_000_000_000


def _signed_init_data(telegram_user_id: int, *, query_id: str) -> str:
    fields = [
        ("auth_date", str(int(NOW.timestamp()))),
        ("query_id", query_id),
        (
            "user",
            json.dumps(
                {"id": telegram_user_id, "first_name": "Synthetic"},
                separators=(",", ":"),
            ),
        ),
    ]
    data_check = "\n".join(f"{key}={value}" for key, value in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urlencode([*fields, ("hash", signature)])


async def _onboard(
    factory: async_sessionmaker[AsyncSession],
    telegram_user_id: int,
    *,
    locale: str,
) -> _Tenant:
    async with factory.begin() as session:
        user = await ensure_owner_user(
            session,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_user_id,
            locale=locale,
            timezone="Europe/Moscow",
            currency="RUB",
        )
        account = await session.get(Account, user.default_account_id)
        category = await session.scalar(
            select(Category)
            .where(Category.user_id == user.id, Category.kind == "expense")
            .order_by(Category.slug)
            .limit(1)
        )
        assert account is not None and category is not None
        return _Tenant(
            owner_id=user.id,
            telegram_user_id=telegram_user_id,
            account_id=account.id,
            expense_category_id=category.id,
            locale=locale,
        )


async def _onboard_pair(
    factory: async_sessionmaker[AsyncSession],
    first_telegram_user_id: int,
    second_telegram_user_id: int,
) -> tuple[_Tenant, _Tenant]:
    first, second = await asyncio.wait_for(
        asyncio.gather(
            _onboard(factory, first_telegram_user_id, locale="ru_RU"),
            _onboard(factory, second_telegram_user_id, locale="en_US"),
        ),
        timeout=8,
    )
    return first, second


async def _create_session(
    factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
    tokens: _Tokens,
    digester: HttpSecurityDigester,
) -> None:
    async with factory.begin() as session:
        await SqlAlchemyWebSessionRepository(session).create(
            tenant.owner_id,
            digester.session(tokens.session),
            digester.csrf(tokens.csrf),
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )


async def _cleanup(
    factory: async_sessionmaker[AsyncSession],
    telegram_user_ids: tuple[int, int],
    update_ids: tuple[int, ...] = (),
) -> None:
    async with factory.begin() as session:
        owner_ids = tuple(
            await session.scalars(
                select(User.id).where(User.telegram_user_id.in_(telegram_user_ids))
            )
        )
        await session.execute(
            delete(TelegramResponseOutbox).where(
                TelegramResponseOutbox.owner_telegram_user_id.in_(telegram_user_ids)
            )
        )
        if update_ids:
            await session.execute(
                delete(ProcessedUpdate).where(ProcessedUpdate.update_id.in_(update_ids))
            )
        if not owner_ids:
            return
        await session.execute(
            update(User).where(User.id.in_(owner_ids)).values(default_account_id=None)
        )
        for model in (
            HttpIdempotencyRecord,
            WebSession,
            Transaction,
            ImportRow,
            ImportBatch,
            Budget,
            Draft,
            Category,
            Account,
        ):
            await session.execute(delete(model).where(model.user_id.in_(owner_ids)))
        await session.execute(delete(User).where(User.id.in_(owner_ids)))


def _auth_app(
    factory: async_sessionmaker[AsyncSession],
    allowed: frozenset[int],
    token_pairs: tuple[OpaqueAuthTokens, ...],
) -> FastAPI:
    generated = iter(token_pairs)
    service = TelegramAuthService(
        bot_token=BOT_TOKEN,
        allowed_telegram_user_ids=allowed,
        digester=HttpSecurityDigester(SECURITY_KEY),
        uow_factory=SqlAlchemyAuthUnitOfWorkFactory(factory),
        clock=lambda: NOW,
        token_factory=lambda: next(generated),
    )
    app: FastAPI = create_app(
        readiness_probe=_ReadinessStub(),
        auth_service=service,
        auth_origin=ORIGIN,
    )
    return app


def _tenant_app(
    factory: async_sessionmaker[AsyncSession],
    allowed: frozenset[int],
) -> tuple[FastAPI, HttpRevisionMutationService]:
    digester = HttpSecurityDigester(SECURITY_KEY)
    executor = HttpMutationExecutor(
        digester=digester,
        uow_factory=SqlAlchemyHttpMutationUnitOfWorkFactory(factory),
        allowed_telegram_user_ids=allowed,
        clock=lambda: NOW,
    )
    mutations = HttpRevisionMutationService(executor)
    app = create_app(
        readiness_probe=_ReadinessStub(),
        finance_service=FinanceQueryService(
            digester=digester,
            cursor_codec=TransactionCursorCodec(SECURITY_KEY),
            uow_factory=SqlAlchemyFinanceQueryUnitOfWorkFactory(factory),
            allowed_telegram_user_ids=allowed,
            clock=lambda: NOW,
        ),
        catalog_service=HttpCatalogService(
            digester=digester,
            query_uow_factory=SqlAlchemyCatalogQueryUnitOfWorkFactory(factory),
            mutation_executor=executor,
            allowed_telegram_user_ids=allowed,
            clock=lambda: NOW,
        ),
        catalog_origin=ORIGIN,
        budget_service=HttpBudgetService(
            digester=digester,
            cursor_codec=BudgetCursorCodec(SECURITY_KEY),
            query_uow_factory=SqlAlchemyBudgetQueryUnitOfWorkFactory(factory),
            mutation_executor=executor,
            allowed_telegram_user_ids=allowed,
            clock=lambda: NOW,
        ),
        budget_origin=ORIGIN,
        bank_import_service=HttpBankImportService(
            digester=digester,
            preparer=BankImportPreparer(
                StrictBankCsvParser(),
                HmacBankImportDigester(bytes(range(32))),
            ),
            cursor_codec=BankImportCursorCodec(SECURITY_KEY),
            admission_uow_factory=SqlAlchemyAuthUnitOfWorkFactory(factory),
            query_uow_factory=SqlAlchemyBankImportQueryUnitOfWorkFactory(factory),
            mutation_executor=executor,
            allowed_telegram_user_ids=allowed,
            clock=lambda: NOW,
        ),
        bank_import_origin=ORIGIN,
        mutation_service=mutations,
        mutation_origin=ORIGIN,
    )
    return app, mutations


def _cookies(tokens: _Tokens) -> dict[str, str]:
    return {SESSION_COOKIE: tokens.session, CSRF_COOKIE: tokens.csrf}


def _mutation_headers(tokens: _Tokens, idempotency_key: str) -> dict[str, str]:
    return {
        "Origin": ORIGIN,
        "X-CSRF-Token": tokens.csrf,
        "X-Session-Binding": HttpSecurityDigester(SECURITY_KEY).session_binding(tokens.session),
        "Idempotency-Key": idempotency_key,
    }


def _read_headers(tokens: _Tokens) -> dict[str, str]:
    return {"X-Session-Binding": HttpSecurityDigester(SECURITY_KEY).session_binding(tokens.session)}


def _private_log_text(records: list[logging.LogRecord]) -> str:
    safe_records = [record.__dict__ for record in records if record.name.startswith("finbot")]
    return json.dumps(safe_records, default=str, ensure_ascii=False)


@pytest.mark.asyncio
async def test_signed_login_rebinds_a_stale_cookie_to_the_current_allowed_user(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="finbot")
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    telegram_ids = (_synthetic_telegram_user_id(), _synthetic_telegram_user_id())
    first_tokens = OpaqueAuthTokens(_opaque_token(11), _opaque_token(12))
    second_tokens = OpaqueAuthTokens(_opaque_token(21), _opaque_token(22))
    first_proof = _signed_init_data(telegram_ids[0], query_id="tenant-a")
    second_proof = _signed_init_data(telegram_ids[1], query_id="tenant-b")
    try:
        first, second = await _onboard_pair(factory, *telegram_ids)
        app = _auth_app(
            factory,
            frozenset(telegram_ids),
            (first_tokens, second_tokens),
        )
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
            base_url=ORIGIN,
        ) as client:
            login_first = await client.post(
                "/api/v1/auth/telegram",
                json={"initData": first_proof},
            )
            first_binding = login_first.headers["X-Session-Binding"]
            same_user_reload = await client.post(
                "/api/v1/auth/telegram",
                json={"initData": first_proof},
            )
            login_second = await client.post(
                "/api/v1/auth/telegram",
                json={"initData": second_proof},
            )
            second_binding = login_second.headers["X-Session-Binding"]
            stale_me = await client.get(
                "/api/v1/auth/me",
                headers={"X-Session-Binding": first_binding},
            )
            rebound_me = await client.get(
                "/api/v1/auth/me",
                headers={"X-Session-Binding": second_binding},
            )

        assert login_first.status_code == same_user_reload.status_code == 200
        assert same_user_reload.headers["X-Session-Binding"] == first_binding
        assert login_first.json()["locale"] == first.locale
        assert same_user_reload.headers.get_list("set-cookie") == []
        assert login_second.status_code == rebound_me.status_code == 200
        assert second_binding != first_binding
        assert stale_me.status_code == 401
        assert stale_me.json()["error"]["code"] == "auth_session_invalid"
        assert stale_me.headers.get_list("set-cookie") == []
        assert login_second.json()["locale"] == second.locale
        assert rebound_me.json()["locale"] == second.locale
        assert len(login_second.headers.get_list("set-cookie")) == 2

        async with factory() as verification:
            sessions = (
                await verification.execute(
                    select(WebSession.user_id, WebSession.revoked_at).where(
                        WebSession.user_id.in_((first.owner_id, second.owner_id))
                    )
                )
            ).all()
        by_owner = {owner_id: revoked_at for owner_id, revoked_at in sessions}
        assert by_owner[first.owner_id] is not None
        assert by_owner[second.owner_id] is None

        log_text = _private_log_text(caplog.records)
        for sensitive in (
            *(str(value) for value in telegram_ids),
            first_proof,
            second_proof,
            first_tokens.session_token,
            second_tokens.session_token,
            first_binding,
            second_binding,
        ):
            assert sensitive not in log_text
    finally:
        await _cleanup(factory, telegram_ids)
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_tenants_share_names_and_keys_without_sharing_financial_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Representative matrix; feature modules retain their detailed lifecycle coverage."""

    caplog.set_level(logging.INFO, logger="finbot")
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    telegram_ids = (_synthetic_telegram_user_id(), _synthetic_telegram_user_id())
    update_ids = (_synthetic_update_id(), _synthetic_update_id())
    first_tokens = _Tokens(_opaque_token(31), _opaque_token(32))
    second_tokens = _Tokens(_opaque_token(41), _opaque_token(42))
    shared_idempotency_key = _opaque_token(51)
    shared_amount_text = "500"
    first_amounts = (91_827_364_501, 91_827_364_502)
    second_amount = 81_726_354_403
    try:
        first, second = await _onboard_pair(factory, *telegram_ids)
        assert first.owner_id != second.owner_id
        assert first.account_id != second.account_id

        async with factory() as verification:
            catalogs = {}
            for tenant in (first, second):
                accounts = tuple(
                    await verification.scalars(
                        select(Account)
                        .where(Account.user_id == tenant.owner_id)
                        .order_by(Account.slug)
                    )
                )
                categories = tuple(
                    await verification.scalars(
                        select(Category)
                        .where(Category.user_id == tenant.owner_id)
                        .order_by(Category.kind, Category.slug)
                    )
                )
                catalogs[tenant.owner_id] = (
                    tuple(item.slug for item in accounts),
                    tuple((item.kind, item.slug) for item in categories),
                )
            assert catalogs[first.owner_id] == catalogs[second.owner_id]
            assert len(catalogs[first.owner_id][1]) == len(INITIAL_CATEGORIES)

        digester = HttpSecurityDigester(SECURITY_KEY)
        await asyncio.gather(
            _create_session(factory, first, first_tokens, digester),
            _create_session(factory, second, second_tokens, digester),
        )
        app, mutations = _tenant_app(factory, frozenset(telegram_ids))
        first_draft, second_draft = await asyncio.gather(
            mutations.begin_quick_draft(
                first_tokens.credentials(shared_idempotency_key),
                shared_amount_text,
            ),
            mutations.begin_quick_draft(
                second_tokens.credentials(shared_idempotency_key),
                shared_amount_text,
            ),
        )
        assert first_draft.result_id is not None and second_draft.result_id is not None
        assert first_draft.result_id != second_draft.result_id
        assert first_draft.http_status == second_draft.http_status == 201

        async with factory.begin() as session:
            first_budget = Budget(
                user_id=first.owner_id,
                name="Одинаковый бюджет",
                limit_minor=500_000,
                currency="RUB",
                category_id=first.expense_category_id,
                starts_on=date(2026, 8, 1),
                ends_on=date(2026, 8, 31),
                timezone="Europe/Moscow",
            )
            second_budget = Budget(
                user_id=second.owner_id,
                name="Одинаковый бюджет",
                limit_minor=500_000,
                currency="RUB",
                category_id=second.expense_category_id,
                starts_on=date(2026, 8, 1),
                ends_on=date(2026, 8, 31),
                timezone="Europe/Moscow",
            )
            first_batch = ImportBatch(
                user_id=first.owner_id,
                account_id=first.account_id,
                profile="canonical_v1",
                encoding="utf-8",
                row_count=1,
            )
            second_batch = ImportBatch(
                user_id=second.owner_id,
                account_id=second.account_id,
                profile="canonical_v1",
                encoding="utf-8",
                row_count=1,
            )
            session.add_all((first_budget, second_budget, first_batch, second_batch))
            await session.flush()
            first_row = ImportRow(
                batch_id=first_batch.id,
                user_id=first.owner_id,
                position=1,
                occurred_at=NOW - timedelta(minutes=5),
                type="expense",
                amount_minor=111,
                currency="RUB",
                description="",
                fingerprint=bytes((61,)) * 32,
            )
            second_row = ImportRow(
                batch_id=second_batch.id,
                user_id=second.owner_id,
                position=1,
                occurred_at=NOW - timedelta(minutes=5),
                type="expense",
                amount_minor=111,
                currency="RUB",
                description="",
                fingerprint=bytes((61,)) * 32,
            )
            first_transactions = tuple(
                Transaction(
                    user_id=first.owner_id,
                    type="expense",
                    amount_minor=amount,
                    currency="RUB",
                    account_id=first.account_id,
                    category_id=first.expense_category_id,
                    occurred_at=NOW - timedelta(minutes=index),
                    description="",
                )
                for index, amount in enumerate(first_amounts, start=1)
            )
            second_transaction = Transaction(
                user_id=second.owner_id,
                type="expense",
                amount_minor=second_amount,
                currency="RUB",
                account_id=second.account_id,
                category_id=second.expense_category_id,
                occurred_at=NOW - timedelta(minutes=1),
                description="",
            )
            session.add_all((*first_transactions, second_transaction, first_row, second_row))
            session.add_all(ProcessedUpdate(update_id=value) for value in update_ids)
            await session.flush()
            session.add_all(
                (
                    TelegramResponseOutbox(
                        update_id=update_ids[0],
                        sequence=0,
                        owner_telegram_user_id=first.telegram_user_id,
                        chat_id=first.telegram_user_id,
                        method="send_message",
                        message_id=None,
                        body="tenant-a-ready",
                        parse_mode=None,
                        reply_markup=None,
                        draft_id=first_draft.result_id,
                        draft_revision=1,
                        history_page=None,
                        pending_history_page=None,
                    ),
                    TelegramResponseOutbox(
                        update_id=update_ids[1],
                        sequence=0,
                        owner_telegram_user_id=second.telegram_user_id,
                        chat_id=second.telegram_user_id,
                        method="send_message",
                        message_id=None,
                        body="tenant-b-ready",
                        parse_mode=None,
                        reply_markup=None,
                        draft_id=second_draft.result_id,
                        draft_revision=1,
                        history_page=None,
                        pending_history_page=None,
                    ),
                )
            )

        async with (
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
                base_url=ORIGIN,
                cookies=_cookies(first_tokens),
                headers=_read_headers(first_tokens),
            ) as first_client,
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
                base_url=ORIGIN,
                cookies=_cookies(second_tokens),
                headers=_read_headers(second_tokens),
            ) as second_client,
        ):
            first_accounts = await first_client.get("/api/v1/accounts")
            second_accounts = await second_client.get("/api/v1/accounts")
            first_budgets = await first_client.get("/api/v1/budgets")
            second_budgets = await second_client.get("/api/v1/budgets")
            first_imports = await first_client.get("/api/v1/bank-imports")
            second_imports = await second_client.get("/api/v1/bank-imports")
            first_dashboard = await first_client.get("/api/v1/dashboard")
            second_dashboard = await second_client.get("/api/v1/dashboard")
            first_report = await first_client.get("/api/v1/reports/today")
            second_report = await second_client.get("/api/v1/reports/today")
            first_page = await first_client.get("/api/v1/transactions?limit=1")
            second_page = await second_client.get("/api/v1/transactions?limit=10")

            assert all(
                response.status_code == 200
                for response in (
                    first_accounts,
                    second_accounts,
                    first_budgets,
                    second_budgets,
                    first_imports,
                    second_imports,
                    first_dashboard,
                    second_dashboard,
                    first_report,
                    second_report,
                    first_page,
                    second_page,
                )
            )
            assert {item["id"] for item in first_accounts.json()["items"]} == {
                str(first.account_id)
            }
            assert {item["id"] for item in second_accounts.json()["items"]} == {
                str(second.account_id)
            }
            assert {item["id"] for item in first_budgets.json()["items"]} == {str(first_budget.id)}
            assert {item["id"] for item in second_budgets.json()["items"]} == {
                str(second_budget.id)
            }
            assert {item["id"] for item in first_imports.json()["items"]} == {str(first_batch.id)}
            assert {item["id"] for item in second_imports.json()["items"]} == {str(second_batch.id)}
            assert first_dashboard.json()["current_period"]["totals"][0]["expense_minor"] == str(
                sum(first_amounts)
            )
            assert second_dashboard.json()["current_period"]["totals"][0]["expense_minor"] == str(
                second_amount
            )
            assert str(first_transactions[0].id) in json.dumps(first_report.json())
            assert str(second_transaction.id) not in json.dumps(first_report.json())
            assert str(second_transaction.id) in json.dumps(second_report.json())
            assert {item["id"] for item in second_page.json()["items"]} == {
                str(second_transaction.id)
            }

            copied_cursor = first_page.json()["next_cursor"]
            assert isinstance(copied_cursor, str)
            copied_cursor_response = await second_client.get(
                "/api/v1/transactions",
                params={"limit": 1, "cursor": copied_cursor},
            )
            foreign_transaction = await second_client.get(
                f"/api/v1/transactions/{first_transactions[0].id}"
            )
            missing_transaction = await second_client.get(f"/api/v1/transactions/{uuid4()}")
            foreign_draft = await second_client.get(f"/api/v1/drafts/{first_draft.result_id}")
            missing_draft = await second_client.get(f"/api/v1/drafts/{uuid4()}")
            foreign_cancel = await second_client.post(
                f"/api/v1/drafts/{first_draft.result_id}/cancel",
                headers=_mutation_headers(second_tokens, _opaque_token(71)),
                json={"revision": 1},
            )
            missing_cancel = await second_client.post(
                f"/api/v1/drafts/{uuid4()}/cancel",
                headers=_mutation_headers(second_tokens, _opaque_token(72)),
                json={"revision": 1},
            )

        assert copied_cursor_response.status_code == 422
        assert foreign_transaction.status_code == missing_transaction.status_code == 404
        assert foreign_transaction.json() == missing_transaction.json()
        assert foreign_draft.status_code == missing_draft.status_code == 404
        assert foreign_draft.json() == missing_draft.json()
        assert foreign_cancel.status_code == missing_cancel.status_code == 404
        assert foreign_cancel.json() == missing_cancel.json()

        async with factory() as verification:
            drafts = tuple(
                await verification.scalars(
                    select(Draft)
                    .where(Draft.user_id.in_((first.owner_id, second.owner_id)))
                    .order_by(Draft.user_id)
                )
            )
            idempotency = tuple(
                await verification.scalars(
                    select(HttpIdempotencyRecord).where(
                        HttpIdempotencyRecord.user_id.in_((first.owner_id, second.owner_id))
                    )
                )
            )
            stored_first_transaction = await verification.get(Transaction, first_transactions[0].id)
            outbox = tuple(
                await verification.scalars(
                    select(TelegramResponseOutbox)
                    .where(TelegramResponseOutbox.update_id.in_(update_ids))
                    .order_by(TelegramResponseOutbox.update_id)
                )
            )
            forged_response = TelegramResponseOutbox(
                update_id=update_ids[1],
                sequence=9,
                owner_telegram_user_id=second.telegram_user_id,
                chat_id=second.telegram_user_id,
                method="send_message",
                message_id=None,
                body="fixed-test-receipt",
                parse_mode=None,
                reply_markup=None,
                draft_id=first_draft.result_id,
                draft_revision=1,
                history_page=None,
                pending_history_page=None,
            )
            assert not await response_draft_belongs_to_private_chat(
                verification,
                forged_response,
            )
            assert not await bind_telegram_draft_presentation(
                verification,
                draft_id=first_draft.result_id,
                draft_revision=1,
                chat_id=second.telegram_user_id,
                message_id=777,
            )
        assert len(drafts) == 2
        assert {draft.user_id for draft in drafts} == {first.owner_id, second.owner_id}
        assert all(draft.revision == 1 for draft in drafts)
        assert all(draft.state == "wizard_type" for draft in drafts)
        assert all(draft.payload["amount_minor"] == 50_000 for draft in drafts)
        assert all(draft.payload["input_mode"] == "amount_only" for draft in drafts)
        assert len(idempotency) == 2
        assert {record.user_id for record in idempotency} == {
            first.owner_id,
            second.owner_id,
        }
        assert len({record.idempotency_key_hash for record in idempotency}) == 1
        assert stored_first_transaction is not None
        assert stored_first_transaction.deleted_at is None
        assert stored_first_transaction.version == 1
        assert {(item.owner_telegram_user_id, item.chat_id, item.draft_id) for item in outbox} == {
            (first.telegram_user_id, first.telegram_user_id, first_draft.result_id),
            (second.telegram_user_id, second.telegram_user_id, second_draft.result_id),
        }

        log_text = _private_log_text(caplog.records)
        for sensitive in (
            *(str(value) for value in telegram_ids),
            *(str(value) for value in first_amounts),
            str(second_amount),
            str(first_transactions[0].id),
            str(second_transaction.id),
        ):
            assert sensitive not in log_text
    finally:
        await _cleanup(factory, telegram_ids, update_ids)
        await engine.dispose()

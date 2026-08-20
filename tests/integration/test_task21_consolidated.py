from __future__ import annotations

import base64
import os
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

import httpx2
import psycopg
import pytest
from fastapi import FastAPI
from psycopg import sql
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from finbot.adapters.bank_import.csv_parser import StrictBankCsvParser
from finbot.adapters.bank_import.digests import HmacBankImportDigester
from finbot.adapters.database.models import Account, Category, ImportRow, Transaction, User
from finbot.adapters.database.provision_readonly import (
    ReadonlyProvisioningTarget,
    provision_readonly,
)
from finbot.adapters.database.repositories.bank_imports import SqlAlchemyBankImportRepository
from finbot.adapters.database.repositories.budgets import SqlAlchemyBudgetRepository
from finbot.adapters.database.repositories.drafts import SqlAlchemyDraftRepository
from finbot.adapters.database.repositories.exchange_rates import (
    SqlAlchemyExchangeRateRepository,
)
from finbot.adapters.database.repositories.http_auth import SqlAlchemyAuthUnitOfWorkFactory
from finbot.adapters.database.repositories.http_bank_imports import (
    SqlAlchemyBankImportQueryUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.http_mutations import (
    SqlAlchemyHttpMutationUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.recurring import SqlAlchemyRecurringRepository
from finbot.adapters.database.repositories.transactions import (
    SqlAlchemyTransactionCommandRepository,
)
from finbot.adapters.database.repositories.web_sessions import (
    SqlAlchemyWebSessionRepository,
)
from finbot.adapters.http.app import create_app
from finbot.adapters.http.auth.cookies import CSRF_COOKIE, SESSION_COOKIE
from finbot.adapters.http.auth.crypto import HttpSecurityDigester
from finbot.adapters.http.bank_imports.cursor import BankImportCursorCodec
from finbot.adapters.http.bank_imports.service import HttpBankImportService
from finbot.adapters.http.mutations.service import HttpMutationExecutor
from finbot.application.bank_imports import (
    BankImportProfile,
    CreateBankImportCommand,
    LinkBankImportRowCommand,
    VersionedBankImportRowCommand,
)
from finbot.application.budgets import CreateBudgetCommand
from finbot.application.dto import ConfirmTransactionDraftCommand
from finbot.application.errors import InvalidStateError, ObjectVersionConflictError
from finbot.application.exchange_rates import PublishManualRateVersionCommand
from finbot.application.recurring import CreateRecurringScheduleCommand
from finbot.application.use_cases.bank_imports import (
    BankImportPreparer,
    ListReconciliationCandidates,
)
from finbot.application.use_cases.budgets import BudgetUseCases
from finbot.application.use_cases.exchange_rates import (
    GetExchangeRateVersion,
    PublishManualExchangeRateVersion,
)
from finbot.application.use_cases.recurring import GetRecurringSchedule, RecurringUseCases
from finbot.domain.budgets import BudgetDefinition
from finbot.domain.exchange_rates import ExchangeRateEntry, parse_exchange_rate
from finbot.domain.recurrence import (
    RecurrenceCadence,
    RecurrenceRule,
    RecurringTransactionDefinition,
)
from finbot.domain.transactions import TransactionType

DATABASE_URL = os.environ["TEST_DATABASE_URL"]
ORIGIN = "https://task21.integration.test"
HTTP_SECURITY_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
OWNER_TELEGRAM_ID = 9_000_000_121
NOW = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)


class _ReadinessStub:
    async def check(self) -> None:
        return None


class _FailIfRead(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.started = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started = True
        raise AssertionError("unauthenticated upload body was consumed")
        yield b""  # pragma: no cover


class _OversizedBody(httpx2.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"x" * (2 * 1024 * 1024)
        yield b"x"


def _opaque_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


async def _seed_owner(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID, UUID]:
    async with factory.begin() as session:
        owner = User(
            telegram_user_id=OWNER_TELEGRAM_ID,
            telegram_chat_id=OWNER_TELEGRAM_ID,
            timezone="Europe/Moscow",
            base_currency="RUB",
        )
        session.add(owner)
        await session.flush()
        account = Account(
            user_id=owner.id,
            name="Основной",
            slug="основной",
            type="card",
            currency="RUB",
            initial_balance_minor=0,
            version=1,
        )
        category = Category(
            user_id=owner.id,
            kind="expense",
            name="Другое",
            slug="другое",
            emoji="",
            version=1,
        )
        session.add_all((account, category))
        await session.flush()
        owner.default_account_id = account.id
        return owner.id, account.id, category.id


async def _cleanup_owner(
    factory: async_sessionmaker[AsyncSession],
    owner_id: UUID,
) -> None:
    async with factory.begin() as session:
        await session.execute(
            text("UPDATE users SET default_account_id = NULL WHERE id = :owner_id"),
            {"owner_id": owner_id},
        )
        for table_name in (
            "http_idempotency",
            "web_sessions",
            "audit_events",
            "transactions",
            "import_rows",
            "import_batches",
            "recurring_instances",
            "recurring_schedules",
            "drafts",
            "exchange_rate_entries",
            "exchange_rate_versions",
            "exchange_rate_sources",
            "budgets",
            "category_rules",
            "categories",
            "accounts",
        ):
            if table_name == "exchange_rate_entries":
                await session.execute(
                    text(
                        "DELETE FROM exchange_rate_entries WHERE rate_version_id IN "
                        "(SELECT id FROM exchange_rate_versions WHERE user_id = :owner_id)"
                    ),
                    {"owner_id": owner_id},
                )
            else:
                await session.execute(
                    text(f"DELETE FROM {table_name} WHERE user_id = :owner_id"),
                    {"owner_id": owner_id},
                )
        await session.execute(
            text("DELETE FROM users WHERE id = :owner_id"),
            {"owner_id": owner_id},
        )


def _bank_preparer() -> BankImportPreparer:
    encoded_key = os.environ["BANK_IMPORT_SECURITY_KEY"]
    return BankImportPreparer(
        StrictBankCsvParser(),
        HmacBankImportDigester(base64.urlsafe_b64decode(encoded_key + "=")),
    )


def _bank_app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    digester = HttpSecurityDigester(HTTP_SECURITY_KEY)
    executor = HttpMutationExecutor(
        digester=digester,
        uow_factory=SqlAlchemyHttpMutationUnitOfWorkFactory(factory),
        clock=lambda: NOW,
    )
    app: FastAPI = create_app(
        readiness_probe=_ReadinessStub(),
        bank_import_service=HttpBankImportService(
            digester=digester,
            preparer=_bank_preparer(),
            cursor_codec=BankImportCursorCodec(HTTP_SECURITY_KEY),
            admission_uow_factory=SqlAlchemyAuthUnitOfWorkFactory(factory),
            query_uow_factory=SqlAlchemyBankImportQueryUnitOfWorkFactory(factory),
            mutation_executor=executor,
            clock=lambda: NOW,
        ),
        bank_import_origin=ORIGIN,
    )
    return app


def _upload_headers(
    session_token: str,
    csrf_token: str,
    idempotency_key: str,
) -> dict[str, str]:
    return {
        "Content-Type": "text/csv",
        "Cookie": f"{SESSION_COOKIE}={session_token}; {CSRF_COOKIE}={csrf_token}",
        "Origin": ORIGIN,
        "X-CSRF-Token": csrf_token,
        "X-Session-Binding": HttpSecurityDigester(HTTP_SECURITY_KEY).session_binding(session_token),
        "Idempotency-Key": idempotency_key,
    }


@pytest.mark.asyncio
async def test_bank_upload_admission_replay_bounds_and_raw_reference_privacy() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner_id, account_id, _ = await _seed_owner(factory)
    digester = HttpSecurityDigester(HTTP_SECURITY_KEY)
    session_token, csrf_token = _opaque_token(), _opaque_token()
    async with factory.begin() as session:
        await SqlAlchemyWebSessionRepository(session).create(
            owner_id,
            digester.session(session_token),
            digester.csrf(csrf_token),
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
    app = _bank_app(factory)
    url = (
        f"/api/v1/bank-imports/upload?account_id={account_id}"
        "&account_version=1&profile=canonical_v1"
    )
    raw_account_reference = "RAW-ACCOUNT-DO-NOT-PERSIST"
    raw_reference = "RAW-REFERENCE-DO-NOT-PERSIST"
    csv = (
        "occurred_at,type,amount,currency,description,account_reference,reference\n"
        f"2026-08-13T12:00:00Z,expense,12.34,RUB,Кофе,"
        f"{raw_account_reference},{raw_reference}\n"
    ).encode()
    try:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
            base_url=ORIGIN,
        ) as client:
            unread = _FailIfRead()
            denied = await client.post(
                url,
                headers=_upload_headers(_opaque_token(), _opaque_token(), _opaque_token()),
                content=unread,
            )
            assert denied.status_code == 401
            assert unread.started is False

            oversized = await client.post(
                url,
                headers=_upload_headers(session_token, csrf_token, _opaque_token()),
                content=_OversizedBody(),
            )
            assert oversized.status_code == 413

            key = _opaque_token()
            headers = _upload_headers(session_token, csrf_token, key)
            created = await client.post(url, headers=headers, content=csv)
            replayed = await client.post(url, headers=headers, content=csv)
            conflict = await client.post(
                url,
                headers=headers,
                content=csv.replace(b"12.34", b"12.35"),
            )

        assert created.status_code == replayed.status_code == 201
        assert created.json() == replayed.json()
        assert created.json() == {
            "result": {
                "kind": "bank_import_batch",
                "batch_id": created.json()["result"]["batch_id"],
                "version": 1,
            }
        }
        assert conflict.status_code == 409
        async with factory() as session:
            persisted = await session.scalar(
                text(
                    "SELECT concat_ws('|', row_to_json(batch_record)::text, "
                    "row_to_json(row_record)::text, row_to_json(idem_record)::text) "
                    "FROM import_batches AS batch_record "
                    "JOIN import_rows AS row_record ON row_record.batch_id = batch_record.id "
                    "JOIN http_idempotency AS idem_record "
                    "ON idem_record.user_id = batch_record.user_id "
                    "WHERE batch_record.user_id = :owner_id"
                ),
                {"owner_id": owner_id},
            )
            batch_count = await session.scalar(
                text("SELECT count(*) FROM import_batches WHERE user_id = :owner_id"),
                {"owner_id": owner_id},
            )
        assert batch_count == 1
        assert persisted is not None
        assert raw_account_reference not in persisted
        assert raw_reference not in persisted
    finally:
        await _cleanup_owner(factory, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_bank_lifecycle_preserves_provenance_exact_link_filters_and_cas() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner_id, account_id, category_id = await _seed_owner(factory)
    csv = (
        "occurred_at,type,amount,currency,description,account_reference,reference\n"
        "2026-08-10T10:00:00Z,expense,10.00,RUB,Первый,ONE,A\n"
        "2026-08-11T10:00:00Z,expense,20.00,RUB,Второй,ONE,B\n"
        "2026-08-12T10:00:00Z,expense,30.00,RUB,Третий,ONE,C\n"
    ).encode()
    prepared = _bank_preparer().prepare(
        CreateBankImportCommand(
            owner_id=owner_id,
            account_id=account_id,
            expected_account_version=1,
            profile=BankImportProfile.CANONICAL_V1,
            content=csv,
        )
    )
    try:
        async with factory.begin() as session:
            imports = SqlAlchemyBankImportRepository(session)
            batch = await imports.create_batch(prepared)
            row_ids = tuple(
                await session.scalars(
                    select(ImportRow.id)
                    .where(ImportRow.batch_id == batch.batch_id)
                    .order_by(ImportRow.position)
                )
            )
            first = await imports.get_row(owner_id, batch.batch_id, row_ids[0])
            assert first is not None
            staged = await imports.stage_draft(
                VersionedBankImportRowCommand(owner_id, batch.ref, first.ref)
            )
            drafts = SqlAlchemyDraftRepository(session)
            active = await drafts.get_active(owner_id)
            assert active is not None
            with pytest.raises(InvalidStateError):
                await drafts.update(owner_id, active.ref, "edit_amount", active.payload)
            tampered = dict(active.payload)
            tampered["amount_minor"] = 1001
            with pytest.raises(InvalidStateError):
                await drafts.update(owner_id, active.ref, active.state, tampered)

            confirmed = await SqlAlchemyTransactionCommandRepository(
                session
            ).confirm_reviewed_draft(
                ConfirmTransactionDraftCommand(owner_id=owner_id, expected=staged.draft)
            )
            first_after = await imports.get_row(owner_id, batch.batch_id, row_ids[0])
            assert first_after is not None
            assert first_after.transaction_id == confirmed.entity_id
            assert confirmed.transaction.source == "bank_import"

            batch = await imports.get_batch(owner_id, batch.batch_id)
            second = await imports.get_row(owner_id, staged.batch.batch_id, row_ids[1])
            assert batch is not None and second is not None
            exact = Transaction(
                user_id=owner_id,
                type="expense",
                amount_minor=second.amount_minor,
                currency="RUB",
                account_id=account_id,
                category_id=category_id,
                occurred_at=second.occurred_at + timedelta(minutes=5),
                description="manual exact",
                source="manual",
                version=1,
            )
            inexact = Transaction(
                user_id=owner_id,
                type="expense",
                amount_minor=second.amount_minor + 1,
                currency="RUB",
                account_id=account_id,
                category_id=category_id,
                occurred_at=second.occurred_at,
                description="manual inexact",
                source="manual",
                version=1,
            )
            session.add_all((exact, inexact))
            await session.flush()
            candidates = await ListReconciliationCandidates(imports)(
                owner_id, batch.batch_id, second.row_id
            )
            assert tuple(item.transaction.transaction_id for item in candidates) == (exact.id,)
            assert candidates[0].rank == 1
            linked = await imports.link(
                LinkBankImportRowCommand(
                    owner_id,
                    batch.ref,
                    second.ref,
                    exact.id,
                    exact.version,
                )
            )
            await session.refresh(exact)
            assert exact.source == "bank_import"
            assert exact.import_row_id == second.row_id

            third = await imports.get_row(owner_id, batch.batch_id, row_ids[2])
            assert third is not None
            third_staged = await imports.stage_draft(
                VersionedBankImportRowCommand(owner_id, linked.batch.ref, third.ref)
            )
            await drafts.delete(owner_id, third_staged.draft)
            dismissed = await imports.get_row(owner_id, batch.batch_id, third.row_id)
            current_batch = await imports.get_batch(owner_id, batch.batch_id)
            assert dismissed is not None and current_batch is not None
            assert dismissed.draft_id is None
            with pytest.raises(ObjectVersionConflictError):
                await imports.skip(
                    VersionedBankImportRowCommand(
                        owner_id,
                        third_staged.batch.ref,
                        third_staged.row.ref,
                    )
                )
            skipped = await imports.skip(
                VersionedBankImportRowCommand(owner_id, current_batch.ref, dismissed.ref)
            )
            assert skipped.batch.state.value == "completed"
            assert skipped.row.state.value == "skipped"
    finally:
        await _cleanup_owner(factory, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_budget_recurring_and_exchange_rate_contracts_persist_together() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner_id, account_id, category_id = await _seed_owner(factory)
    try:
        async with factory.begin() as session:
            budgets = BudgetUseCases(SqlAlchemyBudgetRepository(session))
            budget = await budgets.create(
                CreateBudgetCommand(
                    owner_id=owner_id,
                    definition=BudgetDefinition(
                        name="Август",
                        limit_minor=50_000,
                        currency="RUB",
                        starts_on=date(2026, 8, 1),
                        ends_on=date(2026, 8, 31),
                        timezone="Europe/Moscow",
                        category_id=category_id,
                    ),
                )
            )
            assert (await budgets.get(owner_id, budget.budget_id)).definition == budget.definition

            recurring_repo = SqlAlchemyRecurringRepository(session)
            schedule = await RecurringUseCases(recurring_repo).create(
                CreateRecurringScheduleCommand(
                    owner_id=owner_id,
                    definition=RecurringTransactionDefinition(
                        name="Подписка",
                        kind=TransactionType.EXPENSE,
                        amount_minor=999,
                        currency="RUB",
                        account_id=account_id,
                        category_id=category_id,
                        recurrence=RecurrenceRule(
                            cadence=RecurrenceCadence.MONTHLY,
                            interval=1,
                            anchor_date=date(2026, 8, 15),
                            local_time=time(9, 30),
                            timezone="Europe/Moscow",
                        ),
                    ),
                )
            )
            loaded_schedule = await GetRecurringSchedule(recurring_repo)(
                owner_id, schedule.schedule_id
            )
            assert loaded_schedule.definition.recurrence.timezone == "Europe/Moscow"
            assert loaded_schedule.version == 1

            rates = SqlAlchemyExchangeRateRepository(session)
            published = await PublishManualExchangeRateVersion(rates)(
                PublishManualRateVersionCommand(
                    owner_id=owner_id,
                    target_currency="RUB",
                    expected_source_version=0,
                    effective_at=NOW,
                    entries=(
                        ExchangeRateEntry(
                            source_currency="USD",
                            target_currency="RUB",
                            value=parse_exchange_rate("90.125"),
                        ),
                    ),
                )
            )
            loaded_rate = await GetExchangeRateVersion(rates)(
                owner_id, published.summary.rate_version_id
            )
            assert loaded_rate.summary.version == 1
            assert loaded_rate.entries[0].entry.value.canonical == "90.125"
    finally:
        await _cleanup_owner(factory, owner_id)
        await engine.dispose()


def _readonly_target() -> ReadonlyProvisioningTarget:
    configured = make_url(DATABASE_URL)
    database = configured.database or ""
    owner = configured.username or ""
    assert database.endswith("_test")
    assert owner
    role = f"finbot_mcp_test_{uuid4().hex[:8]}"
    readonly = configured.set(username=role, password="synthetic-readonly-only")
    return ReadonlyProvisioningTarget(
        admin_conninfo=configured.set(drivername="postgresql").render_as_string(
            hide_password=False
        ),
        readonly_conninfo=readonly.set(drivername="postgresql").render_as_string(
            hide_password=False
        ),
        database=database,
        owner_role=owner,
        readonly_role=role,
        readonly_password="synthetic-readonly-only",
    )


def test_mcp_role_is_idempotently_provisioned_select_only_and_write_denied() -> None:
    target = _readonly_target()
    try:
        provision_readonly(target)
        provision_readonly(target)
        with psycopg.connect(target.readonly_conninfo) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_user, current_database(), "
                    "current_setting('default_transaction_read_only')"
                )
                assert cursor.fetchone() == (target.readonly_role, target.database, "on")
                cursor.execute("SELECT version_num FROM public.alembic_version")
                assert cursor.fetchone() is not None
                cursor.execute(
                    "SELECT has_table_privilege(current_user, 'transactions', 'SELECT'), "
                    "has_table_privilege(current_user, 'transactions', 'INSERT,UPDATE,DELETE')"
                )
                assert cursor.fetchone() == (True, False)
                with pytest.raises(
                    (psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)
                ):
                    cursor.execute("UPDATE public.alembic_version SET version_num = version_num")
            connection.rollback()
    finally:
        with psycopg.connect(target.admin_conninfo) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname = %s",
                    (target.readonly_role,),
                )
                if cursor.fetchone() is not None:
                    cursor.execute(
                        sql.SQL("DROP OWNED BY {}").format(sql.Identifier(target.readonly_role))
                    )
                    cursor.execute(
                        sql.SQL("DROP ROLE {}").format(sql.Identifier(target.readonly_role))
                    )

from __future__ import annotations

import base64
import io
import json
import logging
import warnings
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI
from starlette.types import Message

from finbot.adapters.database.repositories.http_idempotency import IdempotencyResultKind
from finbot.adapters.database.repositories.http_mutation_commands import (
    SqlAlchemyRevisionMutationCommands,
)
from finbot.adapters.http.app import create_app
from finbot.adapters.http.auth.service import (
    CsrfRejectedError,
    SessionBindingMismatchError,
    SessionCredentials,
    SessionInvalidError,
)
from finbot.adapters.http.mutations.service import (
    HttpRevisionMutationService,
    IdempotencyInProgressError,
    IdempotencyKeyReuseError,
    InvalidStoredMutationResultError,
    MutationCredentials,
    MutationReceipt,
)
from finbot.adapters.http.schemas.mutations import draft_response
from finbot.application.draft_composition import ComposedDraftInput, ComposeDraftSpec
from finbot.application.draft_conflicts import (
    PENDING_DRAFT_INTENT_KEY,
    PendingComposeIntent,
    PendingQuickIntent,
    encode_pending_draft_intent,
)
from finbot.application.draft_navigation import (
    DraftCatalogChoice,
    DraftCatalogRef,
    DraftNavigationAction,
    DraftNavigationStatus,
)
from finbot.application.draft_views import project_public_draft
from finbot.application.dto import DraftRef, DraftSnapshot
from finbot.application.errors import DraftRevisionConflictError, InvalidStateError
from finbot.application.notifications import NotificationPreferencesSnapshot
from finbot.application.revision_mutations import (
    DraftExistingSelection,
    DraftPatchAction,
    DraftPatchCommand,
    DraftPatchSpec,
)
from finbot.application.transaction_draft_navigation import (
    TransactionDraftNavigationAction,
)
from finbot.domain.notifications import NotificationPreferences
from finbot.domain.transactions import TransactionType
from finbot.observability.logging import JsonFormatter

ORIGIN = "https://miniapp.example.test"
NOW = datetime(2026, 8, 14, 7, 0, tzinfo=UTC)
SESSION_TOKEN = base64.urlsafe_b64encode(b"s" * 32).rstrip(b"=").decode("ascii")
SESSION_BINDING = base64.urlsafe_b64encode(b"b" * 32).rstrip(b"=").decode("ascii")
CSRF_TOKEN = base64.urlsafe_b64encode(b"c" * 32).rstrip(b"=").decode("ascii")
IDEMPOTENCY_KEY = base64.urlsafe_b64encode(b"i" * 32).rstrip(b"=").decode("ascii")
OWNER_ID = UUID("018f0000-0000-7000-8000-000000000001")
DRAFT_ID = UUID("018f0000-0000-7000-8000-000000000002")
TRANSACTION_ID = UUID("018f0000-0000-7000-8000-000000000003")
ACCOUNT_ID = UUID("018f0000-0000-7000-8000-000000000004")
CATEGORY_ID = UUID("018f0000-0000-7000-8000-000000000005")


class ReadinessStub:
    async def check(self) -> None:
        return None


def _draft(
    *,
    state: str = "review",
    payload: dict[str, object] | None = None,
    revision: int = 4,
    suspended: bool = False,
    schema_version: int = 1,
) -> DraftSnapshot:
    return DraftSnapshot(
        draft_id=DRAFT_ID,
        state=state,
        payload=payload
        or {
            "flow": "wizard",
            "type": "expense",
            "amount_minor": 12345,
            "currency": "RUB",
            "account_id": str(ACCOUNT_ID),
            "account_name": "Synthetic account",
            "category_id": str(CATEGORY_ID),
            "category_name": "Synthetic category",
            "category_emoji": "#",
            "occurred_at": NOW.isoformat(),
            "description": "Synthetic description",
        },
        revision=revision,
        suspended=suspended,
        schema_version=schema_version,
    )


class FakeMutationService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.error: Exception | None = None
        self.current = _draft()
        self.patch_receipt: MutationReceipt | None = None
        self.notification_snapshot = NotificationPreferencesSnapshot(
            OWNER_ID,
            "Europe/Moscow",
            NotificationPreferences(),
            0,
        )

    def _raise_once(self) -> None:
        if self.error is not None:
            error = self.error
            self.error = None
            raise error

    async def active_draft(self, credentials: SessionCredentials) -> DraftSnapshot | None:
        self.calls.append(("active", credentials))
        self._raise_once()
        return self.current

    async def draft(self, _credentials: SessionCredentials, draft_id: UUID) -> DraftSnapshot:
        self.calls.append(("get", draft_id))
        self._raise_once()
        return self.current

    async def create_draft(self, credentials: MutationCredentials) -> MutationReceipt:
        self.calls.append(("create", credentials))
        self._raise_once()
        return MutationReceipt(201, IdempotencyResultKind.DRAFT, DRAFT_ID, 1)

    async def change_timezone(
        self,
        credentials: MutationCredentials,
        timezone: str,
        version: int,
    ) -> MutationReceipt:
        del credentials
        self.calls.append(("timezone", (timezone, version)))
        self._raise_once()
        return MutationReceipt(204, IdempotencyResultKind.NONE)

    async def notification_preferences(
        self,
        credentials: SessionCredentials,
    ) -> NotificationPreferencesSnapshot:
        self.calls.append(("notification_get", credentials))
        self._raise_once()
        return self.notification_snapshot

    async def replace_notification_preferences(
        self,
        credentials: MutationCredentials,
        preferences: NotificationPreferences,
        version: int,
    ) -> MutationReceipt:
        del credentials
        self.calls.append(("notification_put", (preferences, version)))
        self._raise_once()
        return MutationReceipt(204, IdempotencyResultKind.NONE)

    async def begin_quick_draft(
        self,
        credentials: MutationCredentials,
        text: str,
    ) -> MutationReceipt:
        del credentials
        self.calls.append(("quick", text))
        self._raise_once()
        return MutationReceipt(201, IdempotencyResultKind.DRAFT, DRAFT_ID, 1)

    async def compose_draft(
        self,
        credentials: MutationCredentials,
        spec: ComposeDraftSpec,
    ) -> MutationReceipt:
        del credentials
        self.calls.append(("compose", spec))
        self._raise_once()
        return MutationReceipt(201, IdempotencyResultKind.DRAFT, DRAFT_ID, 1)

    async def patch_draft(
        self,
        credentials: MutationCredentials,
        spec: DraftPatchSpec,
    ) -> MutationReceipt:
        del credentials
        self.calls.append(("patch", spec))
        self._raise_once()
        if self.patch_receipt is not None:
            return self.patch_receipt
        if spec.action is DraftPatchAction.BACK:
            return MutationReceipt(204, IdempotencyResultKind.NONE)
        return MutationReceipt(200, IdempotencyResultKind.DRAFT, DRAFT_ID, 5)

    async def confirm_draft(
        self,
        credentials: MutationCredentials,
        expected: DraftRef,
    ) -> MutationReceipt:
        del credentials
        self.calls.append(("confirm", expected))
        self._raise_once()
        return MutationReceipt(201, IdempotencyResultKind.TRANSACTION, TRANSACTION_ID, 1)

    async def cancel_draft(
        self,
        credentials: MutationCredentials,
        expected: DraftRef,
    ) -> MutationReceipt:
        del credentials
        self.calls.append(("cancel", expected))
        self._raise_once()
        return MutationReceipt(204, IdempotencyResultKind.NONE)

    async def resume_draft(
        self,
        credentials: MutationCredentials,
        expected: DraftRef,
    ) -> MutationReceipt:
        del credentials
        self.calls.append(("resume", expected))
        self._raise_once()
        return MutationReceipt(200, IdempotencyResultKind.DRAFT, DRAFT_ID, 5)

    async def replace_draft(
        self,
        credentials: MutationCredentials,
        expected: DraftRef,
    ) -> MutationReceipt:
        del credentials
        self.calls.append(("replace", expected))
        self._raise_once()
        return MutationReceipt(200, IdempotencyResultKind.DRAFT, DRAFT_ID, 1)

    async def repeat_transaction(
        self,
        credentials: MutationCredentials,
        transaction_id: UUID,
        version: int,
    ) -> MutationReceipt:
        del credentials
        self.calls.append(("repeat", (transaction_id, version)))
        self._raise_once()
        return MutationReceipt(201, IdempotencyResultKind.DRAFT, DRAFT_ID, 1)

    async def begin_transaction_edit(
        self,
        credentials: MutationCredentials,
        transaction_id: UUID,
        version: int,
    ) -> MutationReceipt:
        del credentials
        self.calls.append(("edit_draft", (transaction_id, version)))
        self._raise_once()
        return MutationReceipt(200, IdempotencyResultKind.DRAFT, DRAFT_ID, 5)

    async def delete_transaction(
        self,
        credentials: MutationCredentials,
        transaction_id: UUID,
        version: int,
    ) -> MutationReceipt:
        del credentials
        self.calls.append(("delete", (transaction_id, version)))
        self._raise_once()
        return MutationReceipt(200, IdempotencyResultKind.TRANSACTION, transaction_id, version + 1)

    async def restore_transaction(
        self,
        credentials: MutationCredentials,
        transaction_id: UUID,
        version: int,
    ) -> MutationReceipt:
        del credentials
        self.calls.append(("restore", (transaction_id, version)))
        self._raise_once()
        return MutationReceipt(200, IdempotencyResultKind.TRANSACTION, transaction_id, version + 1)


def _app(service: FakeMutationService) -> FastAPI:
    app: FastAPI = create_app(
        readiness_probe=ReadinessStub(),
        mutation_service=cast(HttpRevisionMutationService, service),
        mutation_origin=ORIGIN,
    )
    return app


def _client(app: FastAPI) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=ORIGIN,
    )


def _headers() -> dict[str, str]:
    return {
        "Cookie": (f"__Host-numismat_session={SESSION_TOKEN}; __Host-numismat_csrf={CSRF_TOKEN}"),
        "Idempotency-Key": IDEMPOTENCY_KEY,
        "Origin": ORIGIN,
        "X-CSRF-Token": CSRF_TOKEN,
        "X-Session-Binding": SESSION_BINDING,
    }


def _assert_local_openapi_refs_resolve(document: dict[str, Any]) -> None:
    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/"):
            target: object = document
            for encoded_segment in reference.removeprefix("#/").split("/"):
                segment = encoded_segment.replace("~1", "/").replace("~0", "~")
                assert isinstance(target, dict)
                assert segment in target, reference
                target = target[segment]
        for item in value.values():
            visit(item)

    visit(document)


@pytest.mark.asyncio
async def test_all_mutation_routes_return_only_minimal_receipts() -> None:
    service = FakeMutationService()
    app = _app(service)
    headers = _headers()

    async with _client(app) as client:
        active = await client.get(
            "/api/v1/drafts/active",
            headers={
                "Cookie": headers["Cookie"],
                "X-Session-Binding": headers["X-Session-Binding"],
            },
        )
        detail = await client.get(
            f"/api/v1/drafts/{DRAFT_ID}",
            headers={
                "Cookie": headers["Cookie"],
                "X-Session-Binding": headers["X-Session-Binding"],
            },
        )
        created = await client.post("/api/v1/drafts", headers=headers, json={})
        timezone = await client.put(
            "/api/v1/settings/timezone",
            headers=headers,
            json={"timezone": "Asia/Yekaterinburg", "version": 3},
        )
        notification_preferences = await client.get(
            "/api/v1/settings/notifications",
            headers={
                "Cookie": headers["Cookie"],
                "X-Session-Binding": headers["X-Session-Binding"],
            },
        )
        notifications_updated = await client.put(
            "/api/v1/settings/notifications",
            headers=headers,
            json={
                "budget_80_enabled": True,
                "budget_100_enabled": False,
                "recurring_ready_enabled": True,
                "weekly_digest_enabled": True,
                "quiet_start": "22:00",
                "quiet_end": "07:00",
                "weekly_weekday": 4,
                "weekly_time": "18:30",
                "version": 0,
            },
        )
        quick = await client.post(
            "/api/v1/drafts/quick",
            headers=headers,
            json={"text": "500 synthetic"},
        )
        composed = await client.post(
            "/api/v1/drafts/compose",
            headers=headers,
            json={"type": "expense", "amount": "500.50"},
        )
        confirmed = await client.post(
            f"/api/v1/drafts/{DRAFT_ID}/confirm",
            headers=headers,
            json={"revision": 4},
        )
        cancelled = await client.post(
            f"/api/v1/drafts/{DRAFT_ID}/cancel",
            headers=headers,
            json={"revision": 4},
        )
        resumed = await client.post(
            f"/api/v1/drafts/{DRAFT_ID}/resume",
            headers=headers,
            json={"revision": 4},
        )
        replaced = await client.post(
            f"/api/v1/drafts/{DRAFT_ID}/replace",
            headers=headers,
            json={"revision": 4},
        )
        repeated = await client.post(
            f"/api/v1/transactions/{TRANSACTION_ID}/repeat",
            headers=headers,
            json={"version": 3},
        )
        edit_draft = await client.post(
            f"/api/v1/transactions/{TRANSACTION_ID}/edit-draft",
            headers=headers,
            json={"version": 3},
        )
        deleted = await client.post(
            f"/api/v1/transactions/{TRANSACTION_ID}/delete",
            headers=headers,
            json={"version": 3},
        )
        restored = await client.post(
            f"/api/v1/transactions/{TRANSACTION_ID}/restore",
            headers=headers,
            json={"version": 4},
        )

    assert active.status_code == 200
    assert active.json()["draft"]["transaction"]["amount_minor"] == "12345"
    assert detail.status_code == 200
    assert detail.json()["id"] == str(DRAFT_ID)
    assert "payload" not in detail.json()
    assert created.status_code == 201
    assert created.json() == {"result": {"draft_id": str(DRAFT_ID), "kind": "draft", "revision": 1}}
    assert timezone.status_code == 204 and not timezone.content
    assert notification_preferences.status_code == 200
    assert notification_preferences.json() == {
        "budget_80_enabled": False,
        "budget_100_enabled": False,
        "recurring_ready_enabled": False,
        "weekly_digest_enabled": False,
        "quiet_start": None,
        "quiet_end": None,
        "weekly_weekday": 0,
        "weekly_time": "09:00",
        "version": 0,
    }
    assert notifications_updated.status_code == 204 and not notifications_updated.content
    assert quick.status_code == 201
    assert quick.json() == {"result": {"draft_id": str(DRAFT_ID), "kind": "draft", "revision": 1}}
    assert composed.status_code == 201
    assert composed.json() == {
        "result": {"draft_id": str(DRAFT_ID), "kind": "draft", "revision": 1}
    }
    assert confirmed.status_code == 201
    assert confirmed.json() == {
        "result": {
            "kind": "transaction",
            "transaction_id": str(TRANSACTION_ID),
            "version": 1,
        }
    }
    assert cancelled.status_code == 204 and not cancelled.content
    assert resumed.status_code == 200 and resumed.json()["result"]["revision"] == 5
    assert replaced.status_code == 200 and replaced.json()["result"]["revision"] == 1
    assert repeated.status_code == 201
    assert edit_draft.status_code == 200
    assert deleted.status_code == 200 and deleted.json()["result"]["version"] == 4
    assert restored.status_code == 200 and restored.json()["result"]["version"] == 5


@pytest.mark.asyncio
async def test_timezone_settings_boundary_is_exact_string_and_numeric_version() -> None:
    service = FakeMutationService()
    headers = _headers()

    async with _client(_app(service)) as client:
        accepted = await client.put(
            "/api/v1/settings/timezone",
            headers=headers,
            json={"timezone": "Europe/Samara", "version": 7},
        )
        rejected = [
            await client.put("/api/v1/settings/timezone", headers=headers, json=body)
            for body in (
                {"timezone": "", "version": 7},
                {"timezone": "x" * 65, "version": 7},
                {"timezone": 123, "version": 7},
                {"timezone": "Europe/Samara", "version": True},
                {"timezone": "Europe/Samara", "version": "7"},
                {"timezone": "Europe/Samara", "version": 0},
                {"timezone": "Europe/Samara", "version": 7, "extra": False},
            )
        ]

    assert accepted.status_code == 204 and not accepted.content
    assert service.calls == [("timezone", ("Europe/Samara", 7))]
    assert all(response.status_code == 422 for response in rejected)
    assert all(response.json()["error"]["code"] == "validation_failed" for response in rejected)


@pytest.mark.asyncio
async def test_notification_preferences_boundary_is_exact_and_versioned() -> None:
    service = FakeMutationService()
    headers = _headers()
    valid = {
        "budget_80_enabled": True,
        "budget_100_enabled": False,
        "recurring_ready_enabled": True,
        "weekly_digest_enabled": True,
        "quiet_start": "22:00",
        "quiet_end": "07:00",
        "weekly_weekday": 4,
        "weekly_time": "18:30",
        "version": 0,
    }
    invalid = (
        {key: value for key, value in valid.items() if key != "budget_80_enabled"},
        {**valid, "extra": False},
        {**valid, "budget_80_enabled": "true"},
        {**valid, "budget_100_enabled": 1},
        {**valid, "version": True},
        {**valid, "version": -1},
        {**valid, "version": 2**31},
        {**valid, "weekly_weekday": True},
        {**valid, "weekly_weekday": 7},
        {**valid, "weekly_time": "9:00"},
        {**valid, "weekly_time": "24:00"},
        {**valid, "quiet_start": None},
        {**valid, "quiet_end": None},
        {**valid, "quiet_end": "22:00"},
    )

    async with _client(_app(service)) as client:
        accepted = await client.put(
            "/api/v1/settings/notifications",
            headers=headers,
            json=valid,
        )
        rejected = [
            await client.put(
                "/api/v1/settings/notifications",
                headers=headers,
                json=body,
            )
            for body in invalid
        ]

    assert accepted.status_code == 204 and not accepted.content
    assert service.calls == [
        (
            "notification_put",
            (
                NotificationPreferences(
                    budget_80_enabled=True,
                    budget_100_enabled=False,
                    recurring_ready_enabled=True,
                    weekly_digest_enabled=True,
                    quiet_start=time(22, 0),
                    quiet_end=time(7, 0),
                    weekly_weekday=4,
                    weekly_time=time(18, 30),
                ),
                0,
            ),
        )
    ]
    assert all(response.status_code == 422 for response in rejected)
    assert all(response.json()["error"]["code"] == "validation_failed" for response in rejected)


@pytest.mark.parametrize(
    ("body", "expected_action"),
    [
        ({"revision": 4, "action": "input_text", "text": "safe"}, "input_text"),
        *[
            ({"revision": 4, "action": action}, action)
            for action in (
                "edit_type",
                "edit_amount",
                "edit_category",
                "edit_account",
                "edit_date",
                "edit_description",
                "skip_description",
                "back",
            )
        ],
        ({"revision": 4, "action": "select_type", "value": "expense"}, "select_type"),
        (
            {
                "revision": 4,
                "action": "select_edit_type",
                "value": "income",
                "category": {
                    "kind": "existing",
                    "id": str(CATEGORY_ID),
                    "version": 2,
                },
            },
            "select_edit_type",
        ),
        (
            {
                "revision": 4,
                "action": "select_category",
                "selection": {"kind": "existing", "id": str(CATEGORY_ID), "version": 2},
            },
            "select_category",
        ),
        (
            {
                "revision": 4,
                "action": "select_account",
                "selection": {"kind": "custom"},
            },
            "select_account",
        ),
        ({"revision": 4, "action": "select_date", "value": "custom"}, "select_date"),
        ({"revision": 4, "action": "set_rule", "value": "account"}, "set_rule"),
    ],
)
@pytest.mark.asyncio
async def test_patch_route_maps_the_closed_action_union(
    body: dict[str, object],
    expected_action: str,
) -> None:
    service = FakeMutationService()

    async with _client(_app(service)) as client:
        response = await client.patch(
            f"/api/v1/drafts/{DRAFT_ID}",
            headers=_headers(),
            json=body,
        )

    assert response.status_code == (204 if expected_action == "back" else 200)
    name, value = service.calls[-1]
    assert name == "patch"
    assert isinstance(value, DraftPatchSpec)
    assert value.action.value == expected_action
    assert value.expected == DraftRef(DRAFT_ID, 4)
    if expected_action in {"select_category", "select_edit_type"}:
        assert isinstance(value.selection, DraftExistingSelection)
        assert value.selection.entity_id == CATEGORY_ID
        assert value.selection.version == 2
    if expected_action == "select_category":
        assert value.selection == DraftExistingSelection(CATEGORY_ID, 2)
    if expected_action == "select_account":
        assert value.selection is DraftCatalogChoice.CUSTOM


@pytest.mark.parametrize(
    "content",
    [
        b'{"revision":true,"action":"back"}',
        b'{"revision":"4","action":"back"}',
        b'{"revision":4.0,"action":"back"}',
        b'{"revision":0,"action":"back"}',
        b'{"revision":4,"action":"back","extra":1}',
        b'{"revision":4,"revision":5,"action":"back"}',
        b'{"revision":4,"action":"input_text","text":NaN}',
        b'{"revision":4,"action":"unknown"}',
        (
            b'{"revision":4,"action":"select_category","selection":'
            b'{"kind":"existing","id":"018F0000-0000-7000-8000-000000000005",'
            b'"version":1}}'
        ),
    ],
)
@pytest.mark.asyncio
async def test_patch_boundary_rejects_ambiguous_or_noncanonical_json(content: bytes) -> None:
    service = FakeMutationService()
    headers = {**_headers(), "Content-Type": "application/json"}

    async with _client(_app(service)) as client:
        response = await client.patch(
            f"/api/v1/drafts/{DRAFT_ID}",
            headers=headers,
            content=content,
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"
    assert service.calls == []


@pytest.mark.asyncio
async def test_mutation_boundary_enforces_security_headers_media_type_and_stream_limit() -> None:
    service = FakeMutationService()
    valid = _headers()

    async with _client(_app(service)) as client:
        wrong_type = await client.post(
            "/api/v1/drafts",
            headers={**valid, "Content-Type": "text/plain"},
            content="{}",
        )
        encoded = await client.post(
            "/api/v1/drafts",
            headers={**valid, "Content-Encoding": "gzip"},
            content="{}",
        )
        too_large = await client.post(
            "/api/v1/drafts",
            headers={**valid, "Content-Type": "application/json"},
            content=b"x" * (12 * 1024 + 1),
        )
        duplicate_origin = await client.post(
            "/api/v1/drafts",
            headers=[
                *(item for item in valid.items() if item[0] != "Origin"),
                ("Origin", ORIGIN),
                ("Origin", ORIGIN),
                ("Content-Type", "application/json"),
            ],
            content="{}",
        )
        empty_origin = await client.post(
            "/api/v1/drafts",
            headers=[
                *(item for item in valid.items() if item[0] != "Origin"),
                ("Origin", ""),
                ("Content-Type", "application/json"),
            ],
            content="{}",
        )
        oversized_origin = await client.post(
            "/api/v1/drafts",
            headers={**valid, "Origin": "x" * 4097},
            json={},
        )
        non_ascii_origin = await client.post(
            "/api/v1/drafts",
            headers=[
                *(
                    (name.encode("ascii"), value.encode("ascii"))
                    for name, value in valid.items()
                    if name != "Origin"
                ),
                (b"Origin", b"\xff"),
                (b"Content-Type", b"application/json"),
            ],
            content=b"{}",
        )
        bad_key = await client.post(
            "/api/v1/drafts",
            headers={**valid, "Idempotency-Key": "short"},
            json={},
        )

    assert wrong_type.status_code == 415
    assert encoded.status_code == 415
    assert too_large.status_code == 413
    assert duplicate_origin.status_code == 403
    for response in (empty_origin, oversized_origin, non_ascii_origin):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "origin_forbidden"
    assert bad_key.status_code == 422
    assert service.calls == []


@pytest.mark.asyncio
async def test_quick_draft_boundary_accepts_only_bounded_nonblank_text() -> None:
    service = FakeMutationService()
    valid_text = "x" * 4096

    async with _client(_app(service)) as client:
        accepted = await client.post(
            "/api/v1/drafts/quick",
            headers=_headers(),
            json={"text": valid_text},
        )
        rejected = [
            await client.post(
                "/api/v1/drafts/quick",
                headers=_headers(),
                json=body,
            )
            for body in (
                {"text": ""},
                {"text": " \t\n"},
                {"text": "x" * 4097},
                {"text": 500},
                {"text": "500", "extra": True},
            )
        ]
        duplicate = await client.post(
            "/api/v1/drafts/quick",
            headers={**_headers(), "Content-Type": "application/json"},
            content=b'{"text":"500","text":"600"}',
        )
        missing_csrf_headers = _headers()
        missing_csrf_headers.pop("X-CSRF-Token")
        missing_csrf = await client.post(
            "/api/v1/drafts/quick",
            headers=missing_csrf_headers,
            json={"text": "500"},
        )

    assert accepted.status_code == 201
    assert accepted.json()["result"]["kind"] == "draft"
    assert all(response.status_code == 422 for response in (*rejected, duplicate))
    assert all(
        response.json()["error"]["code"] == "validation_failed"
        for response in (*rejected, duplicate)
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "csrf_failed"
    assert service.calls == [("quick", valid_text)]


@pytest.mark.asyncio
async def test_compose_draft_boundary_is_strict_decimal_and_versioned() -> None:
    service = FakeMutationService()
    valid_body = {
        "type": "expense",
        "amount": "500.50",
        "account": {"kind": "existing", "id": str(ACCOUNT_ID), "version": 3},
        "category": {"kind": "existing", "id": str(CATEGORY_ID), "version": 4},
        "occurred_on": "2026-08-20",
        "description": "synthetic",
    }

    async with _client(_app(service)) as client:
        accepted = await client.post(
            "/api/v1/drafts/compose",
            headers=_headers(),
            json=valid_body,
        )
        rejected = [
            await client.post(
                "/api/v1/drafts/compose",
                headers=_headers(),
                json={"type": "expense", "amount": amount},
            )
            for amount in (
                500.5,
                "0",
                "0500",
                "+500",
                "-500",
                "500,50",
                "5e2",
                "500.500",
                "99999999999999999.99",
            )
        ]
        rejected.extend(
            [
                await client.post(
                    "/api/v1/drafts/compose",
                    headers=_headers(),
                    json={**valid_body, "extra": True},
                ),
                await client.post(
                    "/api/v1/drafts/compose",
                    headers=_headers(),
                    json={
                        "type": "expense",
                        "amount": "500",
                        "account": {"id": str(ACCOUNT_ID), "version": 3},
                    },
                ),
                await client.post(
                    "/api/v1/drafts/compose",
                    headers=_headers(),
                    json={
                        "type": "expense",
                        "amount": "500",
                        "occurred_on": "2026-08-20T10:00:00",
                    },
                ),
                await client.post(
                    "/api/v1/drafts/compose",
                    headers=_headers(),
                    json={
                        "type": "expense",
                        "amount": "500",
                        "description": "x" * 501,
                    },
                ),
            ]
        )

    assert accepted.status_code == 201
    assert accepted.json()["result"]["kind"] == "draft"
    assert all(response.status_code == 422 for response in rejected)
    assert all(response.json()["error"]["code"] == "validation_failed" for response in rejected)
    assert service.calls == [
        (
            "compose",
            ComposeDraftSpec(
                TransactionType.EXPENSE,
                50_050,
                DraftCatalogRef(ACCOUNT_ID, 3),
                DraftCatalogRef(CATEGORY_ID, 4),
                date(2026, 8, 20),
                "synthetic",
            ),
        )
    ]


@pytest.mark.parametrize("origin", [None, "https://web.telegram.org"])
@pytest.mark.asyncio
async def test_mutation_accepts_native_webview_origin_metadata_with_valid_csrf(
    origin: str | None,
) -> None:
    service = FakeMutationService()
    headers = _headers()
    if origin is None:
        headers.pop("Origin")
    else:
        headers["Origin"] = origin

    async with _client(_app(service)) as client:
        response = await client.post("/api/v1/drafts", headers=headers, json={})
        missing_csrf_headers = dict(headers)
        missing_csrf_headers.pop("X-CSRF-Token")
        missing_csrf = await client.post(
            "/api/v1/drafts",
            headers=missing_csrf_headers,
            json={},
        )

    assert response.status_code == 201
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "csrf_failed"
    assert [call[0] for call in service.calls] == ["create"]


@pytest.mark.asyncio
async def test_mutation_boundary_rejects_duplicate_security_and_entity_headers() -> None:
    service = FakeMutationService()
    valid = _headers()

    def duplicated(name: str, value: str) -> list[tuple[str, str]]:
        return [
            *(item for item in valid.items() if item[0].lower() != name.lower()),
            (name, value),
            (name, value),
            ("Content-Type", "application/json"),
        ]

    async with _client(_app(service)) as client:
        duplicate_csrf = await client.post(
            "/api/v1/drafts",
            headers=duplicated("X-CSRF-Token", CSRF_TOKEN),
            content="{}",
        )
        duplicate_key = await client.post(
            "/api/v1/drafts",
            headers=duplicated("Idempotency-Key", IDEMPOTENCY_KEY),
            content="{}",
        )
        duplicate_binding = await client.post(
            "/api/v1/drafts",
            headers=duplicated("X-Session-Binding", SESSION_BINDING),
            content="{}",
        )
        duplicate_type = await client.post(
            "/api/v1/drafts",
            headers=duplicated("Content-Type", "application/json"),
            content="{}",
        )
        duplicate_length = await client.post(
            "/api/v1/drafts",
            headers=[
                *valid.items(),
                ("Content-Type", "application/json"),
                ("Content-Length", "2"),
                ("Content-Length", "2"),
            ],
            content="{}",
        )
        duplicate_cookie_name = await client.post(
            "/api/v1/drafts",
            headers=[
                *(item for item in valid.items() if item[0] != "Cookie"),
                ("Cookie", f"__Host-numismat_session={SESSION_TOKEN}"),
                ("Cookie", f"__Host-numismat_session={SESSION_TOKEN}"),
                ("Cookie", f"__Host-numismat_csrf={CSRF_TOKEN}"),
                ("Content-Type", "application/json"),
            ],
            content="{}",
        )
        split_cookie = await client.post(
            "/api/v1/drafts",
            headers=[
                *(item for item in valid.items() if item[0] != "Cookie"),
                ("Cookie", f"__Host-numismat_session={SESSION_TOKEN}"),
                ("Cookie", f"__Host-numismat_csrf={CSRF_TOKEN}"),
                ("Content-Type", "application/json"),
            ],
            content="{}",
        )

    assert duplicate_csrf.status_code == 403
    assert duplicate_key.status_code == 422
    assert duplicate_type.status_code == 422
    assert duplicate_length.status_code == 422
    assert duplicate_cookie_name.status_code == 401
    assert duplicate_binding.status_code == 401
    assert split_cookie.status_code == 201
    assert [name for name, _value in service.calls] == ["create"]


async def _invoke_streamed_create(
    app: FastAPI,
    *,
    declared_length: bytes | None,
) -> int:
    payload = b"x" * (12 * 1024 + 1)
    messages: Iterator[Message] = iter(
        (
            {"type": "http.request", "body": payload[:6000], "more_body": True},
            {"type": "http.request", "body": payload[6000:], "more_body": False},
        )
    )
    status = 0
    headers = [
        (b"cookie", _headers()["Cookie"].encode("ascii")),
        (b"idempotency-key", IDEMPOTENCY_KEY.encode("ascii")),
        (b"origin", ORIGIN.encode("ascii")),
        (b"x-csrf-token", CSRF_TOKEN.encode("ascii")),
        (b"x-session-binding", SESSION_BINDING.encode("ascii")),
        (b"content-type", b"application/json"),
    ]
    if declared_length is not None:
        headers.append((b"content-length", declared_length))

    async def receive() -> Message:
        return next(messages)

    async def send(message: Message) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            candidate = message.get("status")
            if type(candidate) is int:
                status = candidate

    await app(
        {
            "asgi": {"spec_version": "2.4", "version": "3.0"},
            "client": ("127.0.0.1", 12345),
            "headers": headers,
            "http_version": "2",
            "method": "POST",
            "path": "/api/v1/drafts",
            "query_string": b"",
            "raw_path": b"/api/v1/drafts",
            "scheme": "https",
            "server": ("miniapp.example.test", 443),
            "type": "http",
        },
        receive,
        send,
    )
    return status


@pytest.mark.asyncio
async def test_streaming_limit_rejects_missing_and_lying_small_content_length() -> None:
    service = FakeMutationService()
    app = _app(service)

    without_length = await _invoke_streamed_create(app, declared_length=None)
    lying_length = await _invoke_streamed_create(app, declared_length=b"2")

    assert without_length == 413
    assert lying_length == 413
    assert service.calls == []


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (IdempotencyKeyReuseError(), 409, "idempotency_key_conflict"),
        (IdempotencyInProgressError(), 409, "idempotency_in_progress"),
        (CsrfRejectedError(), 403, "csrf_failed"),
        (SessionBindingMismatchError(), 401, "auth_session_invalid"),
        (SessionInvalidError(), 401, "auth_session_invalid"),
        (InvalidStoredMutationResultError(), 500, "internal_error"),
    ],
)
@pytest.mark.asyncio
async def test_mutation_errors_are_fixed_and_preserve_ambient_cookies(
    error: Exception,
    status: int,
    code: str,
) -> None:
    service = FakeMutationService()
    service.error = error

    async with _client(_app(service)) as client:
        response = await client.post("/api/v1/drafts", headers=_headers(), json={})

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.headers.get_list("set-cookie") == []


def test_draft_projection_never_exposes_raw_or_pending_contents() -> None:
    marker = "private-pending-text-marker"
    payload = dict(_draft().payload)
    payload[PENDING_DRAFT_INTENT_KEY] = encode_pending_draft_intent(PendingQuickIntent(marker))

    response = draft_response(project_public_draft(_draft(payload=payload)))
    serialized = response.model_dump_json()

    assert response.supported
    assert response.conflict is not None
    assert response.conflict.pending_kind == "quick"
    assert marker not in serialized
    assert PENDING_DRAFT_INTENT_KEY not in serialized
    assert "payload" not in serialized

    payload[PENDING_DRAFT_INTENT_KEY] = encode_pending_draft_intent(
        PendingComposeIntent(
            ComposedDraftInput(
                TransactionType.EXPENSE,
                98_765,
                NOW,
                description=marker,
            )
        )
    )
    composed = draft_response(project_public_draft(_draft(payload=payload)))
    composed_serialized = composed.model_dump_json()
    assert composed.conflict is not None
    assert composed.conflict.pending_kind == "compose"
    assert marker not in composed_serialized
    assert "98765" not in composed_serialized


@pytest.mark.parametrize(
    ("state", "payload"),
    [
        ("review", {"flow": "ocr", "ocr_batch": {"private": "value"}}),
        ("settings_account_name", {"private": "value"}),
        ("future_state", {"flow": "wizard", "private": "value"}),
        ("review", {"flow": "future", "private": "value"}),
    ],
)
def test_unknown_ocr_and_settings_drafts_fail_closed(
    state: str,
    payload: dict[str, object],
) -> None:
    response = draft_response(project_public_draft(_draft(state=state, payload=payload)))

    assert not response.supported
    assert response.state.value == "unsupported"
    assert response.flow.value == "unsupported"
    assert response.transaction is None
    assert response.edit_target is None
    assert response.rule is None


class _FutureDraftRepository:
    def __init__(self, draft: DraftSnapshot) -> None:
        self.draft = draft

    async def get_active(self, _owner_id: UUID) -> DraftSnapshot:
        return self.draft

    async def cancel(self, *_args: object) -> None:
        raise AssertionError("future draft must not be cancelled")


class _NoDomainMutation:
    async def begin_wizard(self, *_args: object) -> object:
        raise AssertionError("future draft must not stage wizard intent")

    async def begin_quick(self, *_args: object) -> object:
        raise AssertionError("future draft must not stage quick intent")

    async def begin_repeat(self, *_args: object) -> object:
        raise AssertionError("future draft must not stage repeat intent")

    async def execute(self, *_args: object) -> object:
        raise AssertionError("future draft must not be mutated")

    async def confirm(self, *_args: object) -> object:
        raise AssertionError("future draft must not be confirmed")


@pytest.mark.asyncio
async def test_future_draft_schema_is_unsupported_and_all_task14_writes_fail_closed() -> None:
    future = _draft(state="wizard_confirm", schema_version=2)
    public = draft_response(project_public_draft(future))
    commands = object.__new__(SqlAlchemyRevisionMutationCommands)
    commands._drafts = cast(Any, _FutureDraftRepository(future))
    blocked = _NoDomainMutation()
    commands._ingress = cast(Any, blocked)
    commands._transaction_edit = cast(Any, blocked)
    commands._transactions = cast(Any, blocked)
    commands._conflicts = cast(Any, blocked)

    assert not public.supported
    assert public.transaction is None
    operations = (
        commands.create_draft(OWNER_ID),
        commands.begin_quick_draft(OWNER_ID, "500 synthetic"),
        commands.compose_draft(ComposeDraftSpec(TransactionType.EXPENSE, 50_000).bind(OWNER_ID)),
        commands.patch_draft(
            DraftPatchSpec(DraftRef(DRAFT_ID, 4), DraftPatchAction.BACK).bind(OWNER_ID)
        ),
        commands.confirm_draft(OWNER_ID, DRAFT_ID, 4),
        commands.cancel_draft(OWNER_ID, DRAFT_ID, 4),
        commands.resolve_draft(OWNER_ID, DRAFT_ID, 4, replace=False),
        commands.resolve_draft(OWNER_ID, DRAFT_ID, 4, replace=True),
        commands.repeat_transaction(OWNER_ID, TRANSACTION_ID, 3),
        commands.begin_transaction_edit(OWNER_ID, TRANSACTION_ID, 3),
    )
    for operation in operations:
        with pytest.raises(InvalidStateError):
            await operation


@pytest.mark.asyncio
async def test_mutation_openapi_is_closed_authenticated_and_has_no_dangling_refs() -> None:
    service = FakeMutationService()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        schema = _app(service).openapi()
    _assert_local_openapi_refs_resolve(schema)

    paths = schema["paths"]
    mutation_paths = {
        "/api/v1/settings/notifications",
        "/api/v1/settings/timezone",
        "/api/v1/drafts",
        "/api/v1/drafts/quick",
        "/api/v1/drafts/compose",
        "/api/v1/drafts/{draft_id}",
        "/api/v1/drafts/{draft_id}/confirm",
        "/api/v1/drafts/{draft_id}/cancel",
        "/api/v1/drafts/{draft_id}/resume",
        "/api/v1/drafts/{draft_id}/replace",
        "/api/v1/transactions/{transaction_id}/repeat",
        "/api/v1/transactions/{transaction_id}/edit-draft",
        "/api/v1/transactions/{transaction_id}/delete",
        "/api/v1/transactions/{transaction_id}/restore",
    }
    assert mutation_paths.issubset(paths)
    timezone_schema = paths["/api/v1/settings/timezone"]["put"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert timezone_schema["additionalProperties"] is False
    assert timezone_schema["required"] == ["timezone", "version"]
    assert timezone_schema["properties"]["timezone"]["maxLength"] == 64
    assert timezone_schema["properties"]["version"]["type"] == "integer"
    notifications_schema = paths["/api/v1/settings/notifications"]["put"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert notifications_schema["additionalProperties"] is False
    assert notifications_schema["required"] == [
        "budget_80_enabled",
        "budget_100_enabled",
        "recurring_ready_enabled",
        "weekly_digest_enabled",
        "quiet_start",
        "quiet_end",
        "weekly_weekday",
        "weekly_time",
        "version",
    ]
    assert notifications_schema["properties"]["weekly_weekday"] == {
        "maximum": 6,
        "minimum": 0,
        "title": "Weekly Weekday",
        "type": "integer",
    }
    assert notifications_schema["properties"]["weekly_time"]["pattern"] == (
        r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"
    )
    assert notifications_schema["properties"]["version"]["maximum"] == 2**31 - 1
    assert set(paths["/api/v1/settings/notifications"]) >= {"get", "put"}
    assert set(paths["/api/v1/drafts"]["post"]["responses"]) >= {"200", "201"}
    assert set(paths["/api/v1/drafts/quick"]["post"]["responses"]) >= {"200", "201"}
    assert set(paths["/api/v1/drafts/compose"]["post"]["responses"]) >= {"200", "201"}
    assert set(paths["/api/v1/drafts/{draft_id}"]["patch"]["responses"]) >= {
        "200",
        "204",
    }
    assert set(paths["/api/v1/drafts/{draft_id}/confirm"]["post"]["responses"]) >= {"201"}
    assert "200" not in paths["/api/v1/drafts/{draft_id}/confirm"]["post"]["responses"]
    assert "post" not in paths.get("/api/v1/transactions", {})
    quick_schema = paths["/api/v1/drafts/quick"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert quick_schema["additionalProperties"] is False
    assert quick_schema["required"] == ["text"]
    assert quick_schema["properties"]["text"] == {
        "maxLength": 4096,
        "minLength": 1,
        "pattern": r"\S",
        "title": "Text",
        "type": "string",
    }
    compose_schema = paths["/api/v1/drafts/compose"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert compose_schema["additionalProperties"] is False
    assert compose_schema["required"] == ["type", "amount"]
    assert compose_schema["properties"]["amount"]["type"] == "string"
    assert compose_schema["properties"]["amount"]["pattern"] == (
        r"^(?:0|[1-9][0-9]{0,16})(?:\.[0-9]{1,2})?$"
    )
    assert "$defs" not in json.dumps(compose_schema)
    patch_schema = paths["/api/v1/drafts/{draft_id}"]["patch"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert len(patch_schema["oneOf"]) == 7
    assert patch_schema["discriminator"] == {"propertyName": "action"}
    assert "$defs" not in json.dumps(patch_schema)
    assert "#/$defs/" not in json.dumps(schema)
    assert schema["components"]["securitySchemes"]["SessionCookie"]["name"] == (
        "__Host-numismat_session"
    )
    active_schema = schema["components"]["schemas"]["ActiveDraftResponse"]
    assert "draft" in active_schema["required"]
    draft_schema = schema["components"]["schemas"]["DraftResponse"]
    transaction_ref = draft_schema["properties"]["transaction"]
    assert "DraftTransactionResponse" in json.dumps(transaction_ref)
    amount = schema["components"]["schemas"]["DraftTransactionResponse"]["properties"][
        "amount_minor"
    ]
    assert "string" in json.dumps(amount)
    for path in mutation_paths:
        for method, operation in paths[path].items():
            if method not in {"post", "patch", "put"}:
                continue
            assert operation["security"] == [{"SessionCookie": []}]
            header_parameters = [item for item in operation["parameters"] if item["in"] == "header"]
            headers = {item["name"] for item in header_parameters}
            assert headers == {
                "Origin",
                "X-CSRF-Token",
                "X-Session-Binding",
                "Idempotency-Key",
            }
            for item in header_parameters:
                if item["name"] == "Origin":
                    assert item["required"] is False
                    assert item["schema"]["maxLength"] == 4096
                else:
                    assert item["required"] is True
                    assert item["schema"]["pattern"] == (r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")


def test_mutation_routes_use_only_the_closed_typed_facade() -> None:
    root = Path(__file__).parents[2]
    source = (root / "src/finbot/adapters/http/routes/mutations.py").read_text(encoding="utf-8")

    assert "HttpRevisionMutationService" in source
    assert "HttpMutationExecutor" not in source
    assert "SqlAlchemy" not in source
    assert ".commands" not in source
    assert "create_transaction" not in source


@pytest.mark.asyncio
async def test_mutation_logs_exclude_body_key_ids_revisions_and_results() -> None:
    marker = "private-body-marker"
    service = FakeMutationService()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    try:
        async with _client(_app(service)) as client:
            response = await client.post(
                "/api/v1/drafts/quick",
                headers=_headers(),
                json={"text": marker},
            )
            composed = await client.post(
                "/api/v1/drafts/compose",
                headers=_headers(),
                json={
                    "type": "expense",
                    "amount": "987654321.12",
                    "description": marker,
                },
            )
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)

    assert response.status_code == 201
    assert composed.status_code == 201
    rendered = stream.getvalue()
    for sensitive in (
        marker,
        "987654321",
        IDEMPOTENCY_KEY,
        SESSION_TOKEN,
        CSRF_TOKEN,
        str(DRAFT_ID),
        str(TRANSACTION_ID),
        '"revision":4',
    ):
        assert sensitive not in rendered
    entries = [json.loads(line) for line in rendered.splitlines() if line]
    assert all(
        set(entry)
        <= {
            "component",
            "correlation_id",
            "duration",
            "error_code",
            "event",
            "level",
            "result",
            "route_template",
            "status_code",
        }
        for entry in entries
    )


class _CaptureExecute:
    def __init__(self, result: object) -> None:
        self.result = result
        self.command: object | None = None

    async def execute(self, command: object) -> object:
        self.command = command
        return self.result


class _ActiveDraftAtNewerRevision:
    async def get_active(self, owner_id: UUID) -> DraftSnapshot:
        assert owner_id == OWNER_ID
        return _draft(revision=5)


@pytest.mark.asyncio
async def test_same_draft_at_newer_revision_fails_before_domain_mutation() -> None:
    domain = _CaptureExecute(SimpleNamespace())
    commands = object.__new__(SqlAlchemyRevisionMutationCommands)
    commands._drafts = cast(Any, _ActiveDraftAtNewerRevision())
    commands._draft_navigation = cast(Any, domain)
    command = DraftPatchSpec(DraftRef(DRAFT_ID, 4), DraftPatchAction.BACK).bind(OWNER_ID)

    with pytest.raises(DraftRevisionConflictError) as caught:
        await commands.patch_draft(command)

    assert caught.value.details == {"current_revision": 5}
    assert domain.command is None


@pytest.mark.asyncio
async def test_same_draft_at_newer_revision_is_typed_http_conflict() -> None:
    service = FakeMutationService()
    service.error = DraftRevisionConflictError(current_revision=5)

    async with _client(_app(service)) as client:
        response = await client.patch(
            f"/api/v1/drafts/{DRAFT_ID}",
            headers=_headers(),
            json={"revision": 4, "action": "back"},
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "draft_revision_conflict",
            "details": {"current_revision": 5},
            "message": "Черновик был изменён",
        }
    }


@pytest.mark.parametrize(
    "action",
    [
        DraftPatchAction.EDIT_TYPE,
        DraftPatchAction.EDIT_AMOUNT,
        DraftPatchAction.EDIT_CATEGORY,
        DraftPatchAction.EDIT_ACCOUNT,
        DraftPatchAction.EDIT_DATE,
        DraftPatchAction.EDIT_DESCRIPTION,
        DraftPatchAction.SKIP_DESCRIPTION,
        DraftPatchAction.BACK,
    ],
)
@pytest.mark.asyncio
async def test_finance_no_choice_patch_actions_dispatch_none(action: DraftPatchAction) -> None:
    resulting_draft = _draft(revision=5)
    status = (
        DraftNavigationStatus.CLOSED
        if action is DraftPatchAction.BACK
        else DraftNavigationStatus.UPDATED
    )
    capture = _CaptureExecute(
        SimpleNamespace(
            status=status,
            draft=None if status is DraftNavigationStatus.CLOSED else resulting_draft,
        )
    )
    commands = object.__new__(SqlAlchemyRevisionMutationCommands)
    commands._draft_navigation = cast(Any, capture)
    command = DraftPatchSpec(DraftRef(DRAFT_ID, 4), action).bind(OWNER_ID)

    result = await commands._finance_patch(command)

    assert capture.command is not None
    assert capture.command.action is DraftNavigationAction(action.value)  # type: ignore[attr-defined]
    assert capture.command.choice is None  # type: ignore[attr-defined]
    assert result.closed is (action is DraftPatchAction.BACK)


@pytest.mark.asyncio
async def test_transaction_edit_custom_date_back_maps_to_date_back() -> None:
    capture = _CaptureExecute(SimpleNamespace(draft=_draft(state="edit_date_menu", revision=5)))
    commands = object.__new__(SqlAlchemyRevisionMutationCommands)
    commands._transaction_navigation = cast(Any, capture)
    current = _draft(
        state="edit_date",
        payload={"transaction_id": str(TRANSACTION_ID), "version": 3},
    )
    command = DraftPatchCommand(
        OWNER_ID,
        DraftRef(DRAFT_ID, 4),
        DraftPatchAction.BACK,
    )

    await commands._transaction_patch(command, current)

    assert capture.command is not None
    assert capture.command.action is TransactionDraftNavigationAction.DATE_BACK  # type: ignore[attr-defined]

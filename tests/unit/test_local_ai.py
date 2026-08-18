import json
import logging
from dataclasses import dataclass, field
from uuid import UUID, uuid7

import pytest
from pydantic import ValidationError

from finbot.adapters.ai.ollama import OllamaLocalAiSuggestionProvider
from finbot.application.draft_ingress import DraftIngressStatus
from finbot.application.dto import DraftSnapshot, OwnerSnapshot
from finbot.application.local_ai import (
    CreateLocalAiDraftCommand,
    LocalAiInvalidSuggestionError,
    LocalAiSuggestion,
    SuggestLocalTransactionCommand,
)
from finbot.application.use_cases.local_ai import CreateLocalAiDraft, SuggestLocalTransaction
from finbot.config import Settings
from finbot.domain.transactions import TransactionDraft, TransactionType
from finbot.observability.logging import JsonFormatter


@dataclass(slots=True)
class StubTransport:
    response: bytes = field(repr=False)
    calls: list[tuple[str, bytes]] = field(default_factory=list, repr=False)

    async def post_json(self, url: str, payload: bytes) -> bytes:
        self.calls.append((url, payload))
        return self.response


@dataclass(slots=True)
class StubSuggestionProvider:
    suggestion: LocalAiSuggestion = field(repr=False)

    async def suggest(self, text: str) -> LocalAiSuggestion:
        assert text == "кофе и выпечка"
        return self.suggestion


def _ollama_response(suggestion: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "message": {
                "role": "assistant",
                "content": json.dumps(suggestion, ensure_ascii=False),
            },
            "done": True,
        },
        ensure_ascii=False,
    ).encode()


async def test_ollama_provider_uses_deterministic_local_request_and_exact_schema() -> None:
    transport = StubTransport(
        _ollama_response(
            {
                "amount": "12.34",
                "type": "expense",
                "description": "кофе и выпечка",
                "category_hint": None,
                "account_hint": None,
            }
        )
    )
    provider = OllamaLocalAiSuggestionProvider(
        "http://127.0.0.1:11434",
        "qwen3:4b",
        transport,
    )

    suggestion = await provider.suggest("  кофе\nи выпечка  ")

    assert suggestion.amount_decimal == "12.34"
    assert suggestion.transaction_type is TransactionType.EXPENSE
    [(url, raw_request)] = transport.calls
    request = json.loads(raw_request)
    assert url == "http://127.0.0.1:11434/api/chat"
    assert request["stream"] is False
    assert request["think"] is False
    assert request["keep_alive"] == 0
    assert request["options"] == {
        "temperature": 0,
        "seed": 0,
        "num_ctx": 2048,
        "num_predict": 256,
    }
    assert request["format"]["additionalProperties"] is False
    assert request["messages"][1] == {"role": "user", "content": "кофе и выпечка"}


async def test_ollama_provider_rejects_non_schema_output() -> None:
    transport = StubTransport(
        _ollama_response(
            {
                "amount": "12.34",
                "type": "expense",
                "description": "кофе",
                "category_hint": None,
                "account_hint": None,
                "unexpected": "value",
            }
        )
    )
    provider = OllamaLocalAiSuggestionProvider(
        "http://127.0.0.1:11434",
        "qwen3:4b",
        transport,
    )

    with pytest.raises(LocalAiInvalidSuggestionError):
        await provider.suggest("кофе")


async def test_local_ai_use_case_converts_decimal_without_float() -> None:
    use_case = SuggestLocalTransaction(
        StubSuggestionProvider(
            LocalAiSuggestion(
                amount_decimal="12.34",
                transaction_type=TransactionType.EXPENSE,
                description="кофе и выпечка",
            )
        )
    )

    draft = await use_case.execute(SuggestLocalTransactionCommand("кофе и выпечка"))

    assert draft.amount_minor == 1234
    assert draft.occurred_at is None
    assert draft.category_explicit is False


async def test_active_draft_rejects_local_ai_without_replacement_or_pending_intent() -> None:
    owner_id = uuid7()
    owner = OwnerSnapshot(owner_id, "ru_RU", "Europe/Moscow", "RUB", None)
    current = DraftSnapshot(uuid7(), "review", {"flow": "quick"}, revision=4)

    class Owners:
        async def __call__(self, requested_owner_id: UUID) -> OwnerSnapshot:
            assert requested_owner_id == owner_id
            return owner

    class Drafts:
        async def get_active(self, requested_owner_id: UUID) -> DraftSnapshot:
            assert requested_owner_id == owner_id
            return current

        async def create(self, command: object) -> DraftSnapshot:
            raise AssertionError("active draft must not be replaced or updated")

    class ParsedDrafts:
        async def prepare_for_owner(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("active draft must fail before preparation")

    use_case = CreateLocalAiDraft(Drafts(), ParsedDrafts(), Owners())  # type: ignore[arg-type]
    suggested = TransactionDraft(1234, TransactionType.EXPENSE, description="кофе")

    result = await use_case.execute(CreateLocalAiDraftCommand(owner_id, suggested))

    assert result.status is DraftIngressStatus.CONFLICT
    assert result.draft == current
    assert dict(current.payload) == {"flow": "quick"}


@pytest.mark.parametrize(
    ("endpoint", "model"),
    [
        ("https://example.test", "qwen3:4b"),
        ("http://127.0.0.1:11434", "remote-cloud"),
        (None, None),
    ],
)
def test_enabled_local_ai_configuration_fails_closed(
    endpoint: str | None,
    model: str | None,
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            telegram_bot_token="synthetic-token",
            owner_telegram_user_id=42,
            local_ai_enabled=True,
            local_ai_endpoint=endpoint,
            local_ai_model=model,
        )


def test_local_ai_log_allowlist_discards_sensitive_extras() -> None:
    record = logging.LogRecord(
        "finbot.ai.ollama",
        logging.INFO,
        __file__,
        1,
        "local_ai_suggestion_completed",
        (),
        None,
    )
    record.provider = "ollama"
    record.stage = "validation"
    record.result = "success"
    record.prompt = "sensitive-prompt"
    record.output = "sensitive-output"
    record.model = "sensitive-model"
    record.url = "sensitive-url"

    payload = json.loads(JsonFormatter().format(record))

    assert payload == {
        "component": "ai",
        "correlation_id": "system",
        "event": "local_ai_suggestion_completed",
        "level": "INFO",
        "provider": "ollama",
        "result": "success",
        "stage": "validation",
    }

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from finbot.application.local_ai import (
    MAX_LOCAL_AI_HINT_CHARS,
    MAX_LOCAL_AI_RESPONSE_BYTES,
    LocalAiInvalidSuggestionError,
    LocalAiSuggestion,
    LocalAiUnavailableError,
    normalize_local_ai_input,
)
from finbot.domain.transactions import TransactionType

OLLAMA_LOOPBACK_ENDPOINT = "http://127.0.0.1:11434"
OLLAMA_COMPOSE_ENDPOINT = "http://ollama:11434"
ALLOWED_OLLAMA_ENDPOINTS = frozenset({OLLAMA_LOOPBACK_ENDPOINT, OLLAMA_COMPOSE_ENDPOINT})
LOCAL_AI_TOTAL_TIMEOUT_SECONDS = 8.0

_SOCKET_TIMEOUT_SECONDS = 7.5
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_LOGGER = logging.getLogger("finbot.ai.ollama")

_SUGGESTION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["amount", "type", "description", "category_hint", "account_hint"],
    "properties": {
        "amount": {
            "type": "string",
            "pattern": r"^[0-9]{1,16}(?:\.[0-9]{1,2})?$",
        },
        "type": {"type": "string", "enum": ["expense", "income"]},
        "description": {"type": "string", "minLength": 1, "maxLength": 500},
        "category_hint": {
            "anyOf": [
                {"type": "string", "minLength": 1, "maxLength": MAX_LOCAL_AI_HINT_CHARS},
                {"type": "null"},
            ]
        },
        "account_hint": {
            "anyOf": [
                {"type": "string", "minLength": 1, "maxLength": MAX_LOCAL_AI_HINT_CHARS},
                {"type": "null"},
            ]
        },
    },
}
_SYSTEM_PROMPT = (
    "Convert one explicit personal-finance note into the exact JSON schema. "
    "The amount must be a positive decimal string with at most two fractional digits. "
    "Use expense unless income is explicit. Never invent account or category names: use null "
    "when uncertain. Do not infer transaction dates; retain date words in description. "
    "Return JSON only."
)
_EXPECTED_SUGGESTION_KEYS = frozenset(
    {"amount", "type", "description", "category_hint", "account_hint"}
)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        del req, fp, code, msg, headers, newurl
        return None


class LocalAiHttpTransport(Protocol):
    async def post_json(self, url: str, payload: bytes) -> bytes: ...


class UrllibLocalAiHttpTransport:
    """Bounded no-proxy/no-redirect transport for the fixed local Ollama endpoint."""

    __slots__ = ()

    async def post_json(self, url: str, payload: bytes) -> bytes:
        try:
            async with asyncio.timeout(LOCAL_AI_TOTAL_TIMEOUT_SECONDS):
                return await asyncio.to_thread(self._post_json_sync, url, payload)
        except TimeoutError:
            raise LocalAiUnavailableError("Локальный AI не ответил вовремя") from None

    @staticmethod
    def _post_json_sync(url: str, payload: bytes) -> bytes:
        opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
        request = Request(
            url,
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Connection": "close",
            },
            method="POST",
        )
        try:
            with opener.open(request, timeout=_SOCKET_TIMEOUT_SECONDS) as response:
                if response.status != 200 or response.geturl() != url:
                    raise LocalAiUnavailableError("Локальный AI отклонил запрос")
                if response.headers.get_content_type() != "application/json":
                    raise LocalAiUnavailableError("Локальный AI вернул неожиданный ответ")
                raw_length = response.headers.get("Content-Length")
                if raw_length is not None:
                    try:
                        content_length = int(raw_length)
                    except ValueError:
                        raise LocalAiUnavailableError(
                            "Локальный AI вернул неожиданный ответ"
                        ) from None
                    if not 0 <= content_length <= MAX_LOCAL_AI_RESPONSE_BYTES:
                        raise LocalAiUnavailableError("Ответ локального AI слишком велик")
                body = cast(bytes, response.read(MAX_LOCAL_AI_RESPONSE_BYTES + 1))
        except HTTPError as error:
            error.close()
            raise LocalAiUnavailableError("Локальный AI отклонил запрос") from None
        except OSError, URLError:
            raise LocalAiUnavailableError("Локальный AI недоступен") from None
        if len(body) > MAX_LOCAL_AI_RESPONSE_BYTES:
            raise LocalAiUnavailableError("Ответ локального AI слишком велик")
        return body


def validate_ollama_endpoint(value: str) -> str:
    if type(value) is not str or value not in ALLOWED_OLLAMA_ENDPOINTS:
        raise ValueError("LOCAL_AI_ENDPOINT must be one approved canonical local endpoint")
    return value


def validate_ollama_model(value: str) -> str:
    if type(value) is not str or not _MODEL_NAME.fullmatch(value):
        raise ValueError("LOCAL_AI_MODEL must be a canonical local model name")
    if "cloud" in value.casefold():
        raise ValueError("LOCAL_AI_MODEL must not select a cloud model")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise ValueError(f"non-finite JSON value: {value}")


def _load_json_object(raw: bytes | str) -> dict[str, object]:
    try:
        text = raw.decode("utf-8", errors="strict") if isinstance(raw, bytes) else raw
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except UnicodeDecodeError, json.JSONDecodeError, ValueError:
        raise LocalAiInvalidSuggestionError("Локальный AI вернул некорректный JSON") from None
    if type(document) is not dict:
        raise LocalAiInvalidSuggestionError("Локальный AI вернул некорректный JSON")
    return document


def _optional_string(document: Mapping[str, object], key: str) -> str | None:
    value = document[key]
    if value is None or type(value) is str:
        return value
    raise LocalAiInvalidSuggestionError("Локальный AI вернул некорректный ответ")


class OllamaLocalAiSuggestionProvider:
    """Treat Ollama output as one bounded untrusted draft suggestion."""

    __slots__ = ("_endpoint", "_model", "_transport")

    def __init__(
        self,
        endpoint: str,
        model: str,
        transport: LocalAiHttpTransport | None = None,
    ) -> None:
        self._endpoint = validate_ollama_endpoint(endpoint)
        self._model = validate_ollama_model(model)
        self._transport = transport or UrllibLocalAiHttpTransport()

    async def suggest(self, text: str) -> LocalAiSuggestion:
        prompt = normalize_local_ai_input(text)
        request_body = json.dumps(
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "think": False,
                "format": _SUGGESTION_SCHEMA,
                "options": {
                    "temperature": 0,
                    "seed": 0,
                    "num_ctx": 2048,
                    "num_predict": 256,
                },
                "keep_alive": 0,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            raw_response = await self._transport.post_json(
                f"{self._endpoint}/api/chat",
                request_body,
            )
        except LocalAiUnavailableError:
            self._log(stage="request", result="error")
            raise
        except OSError, TimeoutError, ValueError:
            self._log(stage="request", result="error")
            raise LocalAiUnavailableError("Локальный AI недоступен") from None

        if len(raw_response) > MAX_LOCAL_AI_RESPONSE_BYTES:
            self._log(stage="response", result="rejected")
            raise LocalAiUnavailableError("Ответ локального AI слишком велик")
        try:
            outer = _load_json_object(raw_response)
            suggestion = self._parse_response(outer)
        except LocalAiInvalidSuggestionError:
            self._log(stage="validation", result="rejected")
            raise
        self._log(stage="validation", result="success")
        return suggestion

    @staticmethod
    def _parse_response(document: Mapping[str, object]) -> LocalAiSuggestion:
        message = document.get("message")
        if type(message) is not dict or message.get("role") != "assistant":
            raise LocalAiInvalidSuggestionError("Локальный AI вернул некорректный ответ")
        content = message.get("content")
        if type(content) is not str or len(content.encode("utf-8")) > MAX_LOCAL_AI_RESPONSE_BYTES:
            raise LocalAiInvalidSuggestionError("Локальный AI вернул некорректный ответ")
        if document.get("done") is not True:
            raise LocalAiInvalidSuggestionError("Локальный AI вернул незавершённый ответ")
        suggestion = _load_json_object(content)
        if suggestion.keys() != _EXPECTED_SUGGESTION_KEYS:
            raise LocalAiInvalidSuggestionError("Локальный AI вернул некорректный ответ")
        amount = suggestion["amount"]
        transaction_type = suggestion["type"]
        description = suggestion["description"]
        if (
            type(amount) is not str
            or type(transaction_type) is not str
            or type(description) is not str
        ):
            raise LocalAiInvalidSuggestionError("Локальный AI вернул некорректный ответ")
        try:
            kind = TransactionType(transaction_type)
        except ValueError:
            raise LocalAiInvalidSuggestionError("Локальный AI вернул некорректный ответ") from None
        return LocalAiSuggestion(
            amount_decimal=amount,
            transaction_type=kind,
            description=description,
            category_hint=_optional_string(suggestion, "category_hint"),
            account_hint=_optional_string(suggestion, "account_hint"),
        )

    @staticmethod
    def _log(*, stage: str, result: str) -> None:
        _LOGGER.info(
            "local_ai_suggestion_completed",
            extra={"provider": "ollama", "stage": stage, "result": result},
        )


__all__ = [
    "ALLOWED_OLLAMA_ENDPOINTS",
    "LOCAL_AI_TOTAL_TIMEOUT_SECONDS",
    "LocalAiHttpTransport",
    "OLLAMA_COMPOSE_ENDPOINT",
    "OLLAMA_LOOPBACK_ENDPOINT",
    "OllamaLocalAiSuggestionProvider",
    "UrllibLocalAiHttpTransport",
    "validate_ollama_endpoint",
    "validate_ollama_model",
]

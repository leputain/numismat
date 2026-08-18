import logging

from finbot.application.local_ai import LocalAiDisabledError, LocalAiSuggestion

_LOGGER = logging.getLogger("finbot.ai.disabled")


class DisabledAIParser:
    """Compatibility stub for the pre-existing intentionally disabled AI parser."""

    def parse(self, text: str) -> object:
        del text
        raise NotImplementedError("AI parser is intentionally outside the MVP")


class DisabledLocalAiSuggestionProvider:
    """Fail-closed default that performs no network or persistence work."""

    async def suggest(self, text: str) -> LocalAiSuggestion:
        del text
        _LOGGER.info(
            "local_ai_suggestion_completed",
            extra={"provider": "disabled", "stage": "configuration", "result": "rejected"},
        )
        raise LocalAiDisabledError("Локальный AI отключён")


__all__ = ["DisabledAIParser", "DisabledLocalAiSuggestionProvider"]

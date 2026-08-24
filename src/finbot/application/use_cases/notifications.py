from uuid import UUID

from finbot.application.errors import ApplicationValidationError
from finbot.application.notifications import (
    NotificationPreferencesRepository,
    NotificationPreferencesSnapshot,
    ReplaceNotificationPreferences,
)


class NotificationPreferencesUseCases:
    """Framework-neutral optimistic settings facade; transaction stays with the caller."""

    __slots__ = ("_repository",)

    def __init__(self, repository: NotificationPreferencesRepository) -> None:
        self._repository = repository

    async def get(self, owner_id: UUID) -> NotificationPreferencesSnapshot:
        if not isinstance(owner_id, UUID):
            raise ApplicationValidationError("Владелец настроек не прошёл проверку")
        return await self._repository.get_preferences(owner_id)

    async def replace(
        self,
        command: ReplaceNotificationPreferences,
    ) -> NotificationPreferencesSnapshot:
        try:
            return await self._repository.replace_preferences(command)
        except ValueError as exc:
            raise ApplicationValidationError(str(exc)) from exc

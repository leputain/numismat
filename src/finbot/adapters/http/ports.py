from typing import Protocol


class ReadinessProbe(Protocol):
    async def check(self) -> None: ...

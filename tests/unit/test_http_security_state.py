from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid7

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.repositories.http_idempotency import (
    IdempotencyClaim,
    IdempotencyClaimStatus,
    IdempotencyResult,
    IdempotencyResultKind,
    SqlAlchemyHttpIdempotencyRepository,
)
from finbot.adapters.database.repositories.security_values import (
    CsrfTokenDigest,
    IdempotencyKeyDigest,
    RequestFingerprintDigest,
    SessionTokenDigest,
)
from finbot.adapters.database.repositories.web_sessions import (
    SqlAlchemyWebSessionRepository,
)


@pytest.mark.parametrize(
    "digest_type",
    (SessionTokenDigest, CsrfTokenDigest, IdempotencyKeyDigest, RequestFingerprintDigest),
)
def test_keyed_digest_requires_exact_bytes_and_redacts_repr(
    digest_type: type[SessionTokenDigest],
) -> None:
    marker = b"sensitive-marker" + b"x" * 16
    digest = digest_type(marker)

    assert digest.database_value() == marker
    assert marker.hex() not in repr(digest)
    assert "redacted" in repr(digest)
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        digest_type(b"x" * 31)
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        digest_type(bytearray(32))  # type: ignore[arg-type]


def test_idempotency_result_requires_a_safe_replay_reference() -> None:
    entity_id = uuid7()

    result = IdempotencyResult(
        http_status=200,
        kind=IdempotencyResultKind.DRAFT,
        result_id=entity_id,
        revision=1,
    )

    assert result.result_id == entity_id
    with pytest.raises(ValueError, match="successful HTTP"):
        IdempotencyResult(http_status=500, kind=IdempotencyResultKind.NONE)
    with pytest.raises(ValueError, match="requires id"):
        IdempotencyResult(http_status=200, kind=IdempotencyResultKind.DRAFT)
    with pytest.raises(ValueError, match="cannot reference"):
        IdempotencyResult(
            http_status=204,
            kind=IdempotencyResultKind.NONE,
            result_id=entity_id,
            revision=1,
        )


def test_only_replay_claims_can_carry_a_result() -> None:
    result = IdempotencyResult(http_status=204, kind=IdempotencyResultKind.NONE)

    replay = IdempotencyClaim(
        record_id=uuid7(),
        status=IdempotencyClaimStatus.REPLAY,
        result=result,
    )

    assert replay.result == result
    with pytest.raises(ValueError, match="only replay"):
        IdempotencyClaim(record_id=uuid7(), status=IdempotencyClaimStatus.REPLAY)
    with pytest.raises(ValueError, match="only replay"):
        IdempotencyClaim(
            record_id=uuid7(),
            status=IdempotencyClaimStatus.NEW,
            result=result,
        )


@pytest.mark.asyncio
async def test_repositories_reject_unbounded_lifetimes_before_database_access() -> None:
    session = cast(AsyncSession, AsyncMock())
    now = datetime.now(UTC)

    with pytest.raises(ValueError, match="within 24 hours"):
        await SqlAlchemyWebSessionRepository(session).create(
            uuid7(),
            SessionTokenDigest(b"s" * 32),
            CsrfTokenDigest(b"c" * 32),
            created_at=now,
            expires_at=now + timedelta(hours=24, seconds=1),
        )
    with pytest.raises(ValueError, match="within 24 hours"):
        await SqlAlchemyHttpIdempotencyRepository(session).claim(
            uuid7(),
            IdempotencyKeyDigest(b"i" * 32),
            RequestFingerprintDigest(b"f" * 32),
            operation="draft.create",
            created_at=now,
            expires_at=now + timedelta(hours=24, seconds=1),
        )
    session.execute.assert_not_awaited()  # type: ignore[attr-defined]


def test_security_state_repositories_do_not_own_transactions() -> None:
    root = Path(__file__).parents[2]
    for path in (
        root / "src/finbot/adapters/database/repositories/web_sessions.py",
        root / "src/finbot/adapters/database/repositories/http_idempotency.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert ".commit(" not in source
        assert ".rollback(" not in source

from uuid import UUID

import pytest

from finbot.application.interactions import (
    MAX_CALLBACK_BYTES,
    MAX_OBJECT_VERSION,
    MAX_PAGE,
    MAX_REVISION,
    DraftAction,
    DraftInteraction,
    MalformedInteractionError,
    StaleInteractionError,
    decode_base36,
    decode_draft_interaction,
    decode_uuid,
    encode_base36,
    encode_draft_interaction,
    encode_uuid,
)

DRAFT_ID = UUID("12345678-1234-5678-9abc-def012345678")
OBJECT_ID = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")


@pytest.mark.parametrize(
    ("number", "encoded"),
    [(0, "0"), (1, "1"), (35, "z"), (36, "10"), (1_679_615, "zzzz")],
)
def test_base36_round_trip(number: int, encoded: str) -> None:
    assert encode_base36(number) == encoded
    assert decode_base36(encoded) == number


def test_base36_rejects_negative_and_non_canonical_values() -> None:
    with pytest.raises(ValueError):
        encode_base36(-1)
    with pytest.raises(MalformedInteractionError):
        decode_base36("-1")
    with pytest.raises(MalformedInteractionError):
        decode_base36("01")
    with pytest.raises(MalformedInteractionError):
        decode_base36("Z")


@pytest.mark.parametrize("value", [DRAFT_ID, OBJECT_ID, UUID(int=0)])
def test_uuid_codec_round_trip_uses_22_unpadded_characters(value: UUID) -> None:
    encoded = encode_uuid(value)

    assert len(encoded) == 22
    assert "=" not in encoded
    assert encoded.isascii()
    assert decode_uuid(encoded) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "A" * 21,
        "A" * 23,
        "A" * 21 + "=",
        "*" * 22,
        # Correct alphabet and length, but non-zero unused bits make it non-canonical.
        encode_uuid(DRAFT_ID)[:-1] + "9",
    ],
)
def test_uuid_decoder_rejects_malformed_or_non_canonical_values(value: str) -> None:
    with pytest.raises(MalformedInteractionError):
        decode_uuid(value)


@pytest.mark.parametrize("action", list(DraftAction))
def test_minimal_interaction_round_trip_for_every_known_action(action: DraftAction) -> None:
    interaction = DraftInteraction(action=action, draft_id=DRAFT_ID, revision=1)

    assert DraftInteraction.decode(interaction.encode()) == interaction


def test_full_interaction_round_trip_and_maximum_size() -> None:
    interaction = DraftInteraction(
        action=DraftAction.SELECT_CATEGORY,
        draft_id=DRAFT_ID,
        revision=MAX_REVISION,
        page=MAX_PAGE,
        object_id=OBJECT_ID,
        object_version=MAX_OBJECT_VERSION,
    )

    encoded = encode_draft_interaction(interaction)

    assert len(encoded.encode("ascii")) == 63
    assert len(encoded.encode("ascii")) <= MAX_CALLBACK_BYTES
    assert decode_draft_interaction(encoded) == interaction


def test_interaction_boundary_validation() -> None:
    with pytest.raises(ValueError):
        DraftInteraction(action=DraftAction.CONFIRM, draft_id=DRAFT_ID, revision=0)
    with pytest.raises(ValueError):
        DraftInteraction(
            action=DraftAction.CONFIRM,
            draft_id=DRAFT_ID,
            revision=MAX_REVISION + 1,
        )
    with pytest.raises(ValueError):
        DraftInteraction(action=DraftAction.CONFIRM, draft_id=DRAFT_ID, revision=1, page=-1)
    with pytest.raises(ValueError):
        DraftInteraction(
            action=DraftAction.CONFIRM, draft_id=DRAFT_ID, revision=1, page=MAX_PAGE + 1
        )
    with pytest.raises(ValueError):
        DraftInteraction(
            action=DraftAction.CONFIRM,
            draft_id=DRAFT_ID,
            revision=1,
            object_id=OBJECT_ID,
        )


def test_validate_detects_stale_draft_id_or_revision() -> None:
    interaction = DraftInteraction(action=DraftAction.CONFIRM, draft_id=DRAFT_ID, revision=7)

    assert interaction.is_current(DRAFT_ID, 7)
    assert interaction.validate(DRAFT_ID, 7)
    assert not interaction.is_current(OBJECT_ID, 7)

    with pytest.raises(StaleInteractionError):
        interaction.validate(OBJECT_ID, 7)
    with pytest.raises(StaleInteractionError):
        interaction.validate(DRAFT_ID, 8)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "x0.AAAAAAAAAAAAAAAAAAAAAA.1",
        "dz.AAAAAAAAAAAAAAAAAAAAAA.1",
        f"d0.{encode_uuid(DRAFT_ID)}.0",
        f"d0.{encode_uuid(DRAFT_ID)}.0001",
        f"d0.{encode_uuid(DRAFT_ID)}.10000",
        f"d0.{encode_uuid(DRAFT_ID)}.1.p-1",
        f"d0.{encode_uuid(DRAFT_ID)}.1.p100",
        f"d0.{encode_uuid(DRAFT_ID)}.1.o{encode_uuid(OBJECT_ID)}",
        f"d0.{encode_uuid(DRAFT_ID)}.1.extra",
        "d0." + "A" * 22 + ".1." + "x" * 40,
        "д0." + "A" * 22 + ".1",
    ],
)
def test_interaction_decoder_rejects_malformed_overlong_or_unknown_data(value: str) -> None:
    with pytest.raises(MalformedInteractionError):
        decode_draft_interaction(value)

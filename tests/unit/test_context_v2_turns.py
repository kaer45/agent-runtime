import pytest

from history.turns import HistorySnapshot, RollingSummary, TurnRecord


def make_turn(seq: int, tokens: int = 10) -> TurnRecord:
    return TurnRecord(
        turn_id=f"turn-{seq}",
        seq=seq,
        messages=(
            {"role": "user", "content": f"q{seq}"},
            {"role": "assistant", "content": "a"},
        ),
        token_count=tokens,
        created_at="2026-11-17T00:00:00+00:00",
    )


def test_turn_record_accepts_a_complete_turn():
    turn = make_turn(1, 42)

    assert turn.seq == 1
    assert turn.token_count == 42
    assert len(turn.messages) == 2


def test_turn_record_coerces_message_list():
    turn = TurnRecord(
        turn_id="t1",
        seq=1,
        messages=[{"role": "user", "content": "hi"}],
        token_count=5,
        created_at="2026-11-17T00:00:00+00:00",
    )

    assert turn.messages == ({"role": "user", "content": "hi"},)


def test_turn_record_rejects_illegal_seq():
    with pytest.raises(ValueError, match="seq"):
        make_turn(0)


def test_turn_record_rejects_boolean_seq():
    with pytest.raises(TypeError, match="seq"):
        make_turn(True)


def test_turn_record_rejects_non_positive_token_count():
    with pytest.raises(ValueError, match="token_count"):
        make_turn(1, tokens=0)


def test_turn_record_rejects_empty_messages():
    with pytest.raises(ValueError, match="messages"):
        TurnRecord(
            turn_id="t1",
            seq=1,
            messages=(),
            token_count=5,
            created_at="2026-11-17T00:00:00+00:00",
        )


def test_turn_record_rejects_non_dict_messages():
    with pytest.raises(TypeError, match="dict"):
        TurnRecord(
            turn_id="t1",
            seq=1,
            messages=("hi",),
            token_count=5,
            created_at="2026-11-17T00:00:00+00:00",
        )


def test_turn_record_rejects_blank_identifiers():
    with pytest.raises(ValueError, match="turn_id"):
        TurnRecord(
            turn_id=" ",
            seq=1,
            messages=({"role": "user", "content": "hi"},),
            token_count=5,
            created_at="2026-11-17T00:00:00+00:00",
        )


def test_rolling_summary_only_holds_text():
    assert RollingSummary() == RollingSummary(text="")

    with pytest.raises(TypeError, match="text"):
        RollingSummary(text=None)


def test_empty_snapshot_is_valid():
    snapshot = HistorySnapshot(
        turns=(),
        summary=RollingSummary(),
        covered_through_seq=0,
    )

    assert snapshot.turns == ()


def test_snapshot_rejects_turns_at_or_below_coverage():
    with pytest.raises(ValueError, match="covered_through_seq"):
        HistorySnapshot(
            turns=(make_turn(1), make_turn(2)),
            summary=RollingSummary(text="旧摘要"),
            covered_through_seq=2,
        )


def test_snapshot_rejects_unsorted_turns():
    with pytest.raises(ValueError, match="升序"):
        HistorySnapshot(
            turns=(make_turn(2), make_turn(1)),
            summary=RollingSummary(),
            covered_through_seq=0,
        )


def test_snapshot_rejects_duplicate_seq():
    with pytest.raises(ValueError, match="重复"):
        HistorySnapshot(
            turns=(make_turn(1), make_turn(1)),
            summary=RollingSummary(),
            covered_through_seq=0,
        )


def test_snapshot_rejects_negative_coverage():
    with pytest.raises(ValueError, match="covered_through_seq"):
        HistorySnapshot(
            turns=(),
            summary=RollingSummary(),
            covered_through_seq=-1,
        )


def test_snapshot_accepts_turns_above_coverage():
    snapshot = HistorySnapshot(
        turns=(make_turn(3), make_turn(4)),
        summary=RollingSummary(text="前两轮已经概括"),
        covered_through_seq=2,
    )

    assert [turn.seq for turn in snapshot.turns] == [3, 4]

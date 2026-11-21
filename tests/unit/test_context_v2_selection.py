import pytest

from history.selection import select_recent_turns
from history.models import TurnRecord


def make_turn(seq: int, text: str = "ab") -> TurnRecord:
    return TurnRecord(
        turn_id=f"turn-{seq}",
        seq=seq,
        messages=(
            {"role": "user", "content": text},
            {"role": "assistant", "content": text},
        ),
        token_count=1,
        created_at="2026-11-18T00:00:00+00:00",
    )


def test_empty_history_selects_nothing():
    selection = select_recent_turns((), 1_000, len)

    assert selection.selected == ()
    assert selection.dropped_seqs == ()
    assert selection.used_tokens == 0
    assert selection.truncated is False


def test_everything_fits():
    turns = (make_turn(1), make_turn(2))

    selection = select_recent_turns(turns, 1_000, len)

    assert [turn.seq for turn in selection.selected] == [1, 2]
    assert selection.dropped_seqs == ()
    assert selection.truncated is False


def test_selects_a_contiguous_suffix():
    turns = (make_turn(1), make_turn(2), make_turn(3))
    # 每轮 2 条消息：len 计数 = 2*(4+2) = 12
    selection = select_recent_turns(turns, 24, len)

    assert [turn.seq for turn in selection.selected] == [2, 3]
    assert selection.dropped_seqs == (1,)
    assert selection.truncated is True
    assert selection.used_tokens == 24


def test_stops_at_first_turn_that_does_not_fit():
    turns = (make_turn(1), make_turn(2), make_turn(3))
    # 只放得下两轮：必须选最近两轮，不能跳过 seq=2 去拿 seq=1
    selection = select_recent_turns(turns, 20, len)

    assert [turn.seq for turn in selection.selected] == [3]
    assert selection.dropped_seqs == (2, 1)


def test_newest_turn_larger_than_ceiling_selects_nothing():
    selection = select_recent_turns((make_turn(1, "x" * 100),), 10, len)

    assert selection.selected == ()
    assert selection.dropped_seqs == (1,)
    assert selection.truncated is True


def test_dropped_seqs_are_descending():
    turns = tuple(make_turn(seq) for seq in range(1, 6))
    selection = select_recent_turns(turns, 12, len)

    assert selection.dropped_seqs == (4, 3, 2, 1)


@pytest.mark.parametrize("ceiling", [-1, "10", True])
def test_rejects_bad_ceiling(ceiling):
    with pytest.raises((TypeError, ValueError)):
        select_recent_turns((), ceiling, len)


def test_rejects_non_turn_records():
    with pytest.raises(TypeError, match="TurnRecord"):
        select_recent_turns(({"seq": 1},), 10, len)

import asyncio

from context.models import ContextLimits
from state.models import StateDelta
from tests.integration.harness import (
    SESSION,
    FakeExtractor,
    contents_of,
    final_data,
    make_harness,
    seed_turn,
)


# --------------------------------------------------------------------- 多轮


def test_three_turn_conversation_layers_state_and_history(tmp_path):
    extractor = FakeExtractor(
        [
            StateDelta(preferences_set={"style": "dry"}),
            StateDelta(),
            StateDelta(),
        ]
    )
    harness = make_harness(tmp_path / "agent.db", extractor=extractor)

    harness.llm.queue_chat("第一轮回答")
    asyncio.run(harness.run_turn("我喜欢干型酒。", turn_id="t1"))

    harness.llm.queue_chat("第二轮回答")
    _, events = asyncio.run(
        harness.run_turn("推荐一款。", turn_id="t2")
    )

    messages = harness.llm.chat_calls[-1]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[1]["content"].startswith("<state>")
    assert "dry" in messages[1]["content"]
    assert messages[2] == {"role": "user", "content": "我喜欢干型酒。"}
    assert messages[3] == {"role": "assistant", "content": "第一轮回答"}
    assert messages[-1] == {"role": "user", "content": "推荐一款。"}

    assert harness.history.count_turns(SESSION) == 2
    data = final_data(events)
    assert data["context"]["active_turns"] == 1
    assert "compaction" in data


def test_restart_keeps_history_and_state(tmp_path):
    db_path = tmp_path / "agent.db"

    first = make_harness(
        db_path,
        extractor=FakeExtractor(
            [StateDelta(preferences_set={"style": "dry"})]
        ),
    )
    first.llm.queue_chat("第一轮回答")
    asyncio.run(first.run_turn("我喜欢干型酒。", turn_id="t1"))

    restarted = make_harness(db_path)
    restarted.llm.queue_chat("第二轮回答")
    _, events = asyncio.run(
        restarted.run_turn("推荐一款。", turn_id="t2")
    )

    contents = contents_of(restarted.llm.chat_calls[-1])
    assert "我喜欢干型酒。" in contents
    assert "第一轮回答" in contents

    state = asyncio.run(restarted.service.get(SESSION))
    assert state.state.preferences == {"style": "dry"}
    assert state.version == 1
    assert restarted.history.count_turns(SESSION) == 2
    assert final_data(events)["context"]["active_turns"] == 1


def test_sessions_do_not_share_history(tmp_path):
    harness = make_harness(tmp_path / "agent.db")

    harness.llm.queue_chat("A1")
    asyncio.run(harness.run_turn("A 问题", session="s-a", turn_id="a1"))

    harness.llm.queue_chat("B1")
    asyncio.run(harness.run_turn("B 问题", session="s-b", turn_id="b1"))

    harness.llm.queue_chat("A2")
    asyncio.run(harness.run_turn("A 第二问", session="s-a", turn_id="a2"))

    contents = contents_of(harness.llm.chat_calls[-1])
    assert "A 问题" in contents
    assert "B 问题" not in contents
    assert harness.history.count_turns("s-a") == 2
    assert harness.history.count_turns("s-b") == 1


# --------------------------------------------------------------------- 压缩


def test_compaction_produces_summary_for_next_turn(tmp_path):
    limits = ContextLimits(history_ceiling=400)
    harness = make_harness(
        tmp_path / "agent.db",
        limits=limits,
        extractor=FakeExtractor(),
    )
    for index in range(1, 9):
        seed_turn(
            harness.history,
            SESSION,
            index,
            f"seed-{index}-" + "酒" * 40,
        )

    harness.llm.queue_chat("回答一")
    harness.llm.queue_summary("用户喜欢干型酒，预算 800。")
    _, events = asyncio.run(
        harness.run_turn("继续推荐。", turn_id="t1")
    )

    data = final_data(events)
    assert data["compaction"]["decision"] == "compact"
    assert data["compaction"]["compacted"] is True
    assert data["compaction"]["covered_through_seq"] == 3

    snapshot = harness.history.load(SESSION)
    assert snapshot.covered_through_seq == 3
    assert snapshot.summary.text == "用户喜欢干型酒，预算 800。"
    assert [turn.seq for turn in snapshot.turns] == [4, 5, 6, 7, 8, 9]

    harness.llm.queue_chat("回答二")
    harness.llm.queue_summary("更新后的摘要。")
    _, events = asyncio.run(harness.run_turn("再推荐。", turn_id="t2"))

    messages = harness.llm.chat_calls[-1]
    assert messages[1]["role"] == "user"
    assert messages[1]["content"].startswith("<summary>")
    assert "用户喜欢干型酒" in messages[1]["content"]

    contents = contents_of(messages)
    assert not any("seed-1-" in content for content in contents)
    assert not any("seed-3-" in content for content in contents)
    assert any("seed-4-" in content for content in contents)
    assert final_data(events)["compaction"]["covered_through_seq"] == 4


def test_large_history_is_trimmed_to_ceiling(tmp_path):
    limits = ContextLimits(history_ceiling=2_000)
    harness = make_harness(
        tmp_path / "agent.db",
        limits=limits,
        extractor=FakeExtractor(),
    )
    for index in range(1, 31):
        seed_turn(harness.history, SESSION, index, "酒" * 92)

    harness.llm.queue_chat("回答")
    harness.llm.queue_summary("摘要")
    _, events = asyncio.run(harness.run_turn("继续。", turn_id="t1"))

    report = final_data(events)["context"]
    assert report["active_turns"] == 30
    assert report["history_tokens"] == 3_000
    assert report["history_rendered_tokens"] <= 2_000
    assert report["history_rendered_tokens"] >= 1_900
    assert report["total_tokens"] <= harness.limits.available_tokens
    assert len(harness.llm.summary_calls) == 1


# --------------------------------------------------------------------- 边界


def test_floor_unmet_ends_turn_without_model_call(tmp_path):
    limits = ContextLimits(history_ceiling=100)
    harness = make_harness(
        tmp_path / "agent.db",
        limits=limits,
        extractor=FakeExtractor(),
    )
    for index in range(1, 8):
        seed_turn(harness.history, SESSION, index, "酒" * 48)

    _, events = asyncio.run(harness.run_turn("你好。", turn_id="t1"))

    data = final_data(events)
    assert data["error_code"] == "HISTORY_FLOOR_UNMET"
    assert harness.llm.chat_calls == []
    assert harness.history.count_turns(SESSION) == 7

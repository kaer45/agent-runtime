import asyncio

import pytest

from agent import turn_orchestrator as orchestrator_module
from context.errors import ContextError
from context.models import ContextLimits
from core.issues import ContextIssueCode
from history import compactor as compactor_module
from history.errors import HistoryStoreError
from state.extractor import StateExtractionError
from tests.integration.harness import (
    SESSION,
    BlockingLLM,
    FakeExtractor,
    FakeLLM,
    contents_of,
    final_data,
    make_harness,
    seed_turn,
)


# --------------------------------------------------------------------- 摘要失败


def test_summary_failure_keeps_coverage_and_turn(tmp_path):
    limits = ContextLimits(history_ceiling=400)
    harness = make_harness(
        tmp_path / "agent.db",
        limits=limits,
        extractor=FakeExtractor(),
    )
    for index in range(1, 9):
        seed_turn(harness.history, SESSION, index, "酒" * 48)

    harness.llm.queue_chat("回答")
    for _ in range(3):
        harness.llm.queue_summary_error(RuntimeError("summary down"))

    _, events = asyncio.run(harness.run_turn("继续。", turn_id="t1"))

    data = final_data(events)
    compaction = data["compaction"]
    assert compaction["compacted"] is False
    assert "SUMMARY_GENERATION_FAILED" in [
        issue["code"] for issue in compaction["issues"]
    ]

    snapshot = harness.history.load(SESSION)
    assert snapshot.covered_through_seq == 0
    assert snapshot.summary.text == ""
    assert harness.history.count_turns(SESSION) == 9


# --------------------------------------------------------------------- 状态失败


def test_state_extraction_failure_keeps_turn(tmp_path):
    extractor = FakeExtractor([StateExtractionError("llm down")])
    harness = make_harness(
        tmp_path / "agent.db",
        extractor=extractor,
    )
    harness.llm.queue_chat("回答")

    _, events = asyncio.run(harness.run_turn("你好。", turn_id="t1"))

    event_types = [event.type.value for event in events]
    assert "state_update_failed" in event_types

    data = final_data(events)
    assert "状态更新失败" in data["warnings"][0]
    assert harness.history.count_turns(SESSION) == 1


# --------------------------------------------------------------------- 落库失败


def test_append_failure_does_not_lose_answer(tmp_path, monkeypatch):
    harness = make_harness(
        tmp_path / "agent.db",
        extractor=FakeExtractor(),
    )
    harness.llm.queue_chat("回答")

    def boom(*args, **kwargs):
        raise HistoryStoreError("disk full")

    monkeypatch.setattr(harness.history, "append_turn_once", boom)

    _, events = asyncio.run(harness.run_turn("你好。", turn_id="t1"))

    data = final_data(events)
    assert "历史追加失败" in data["warnings"][0]
    assert harness.history.count_turns(SESSION) == 0
    assert contents_of(harness.llm.chat_calls[-1])[-1] == "你好。"


# --------------------------------------------------------------------- CAS


def test_concurrent_compaction_conflict_is_reported(tmp_path):
    limits = ContextLimits(history_ceiling=400)
    llm = FakeLLM()
    first = make_harness(
        tmp_path / "agent.db", limits=limits, llm=llm, extractor=FakeExtractor()
    )
    second = make_harness(
        tmp_path / "agent.db", limits=limits, llm=llm, extractor=FakeExtractor()
    )
    for index in range(1, 9):
        seed_turn(first.history, SESSION, index, "酒" * 48)

    llm.queue_summary("摘要一")
    llm.queue_summary("摘要二")

    async def run_both():
        return await asyncio.gather(
            first.compactor.after_turn(SESSION),
            second.compactor.after_turn(SESSION),
        )

    first_result, second_result = asyncio.run(run_both())

    compacted = [r for r in (first_result, second_result) if r.compacted]
    conflicts = [
        issue
        for result in (first_result, second_result)
        for issue in result.issues
        if issue.code is ContextIssueCode.SUMMARY_CAS_CONFLICT
    ]

    assert len(compacted) == 1
    assert len(conflicts) == 1
    assert first.history.load(SESSION).covered_through_seq == 2


# --------------------------------------------------------------------- 硬上限


def test_hard_cap_blocks_begin_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator_module, "HARD_ARCHIVE_LIMIT", 3)
    monkeypatch.setattr(compactor_module, "HARD_ARCHIVE_LIMIT", 3)

    limits = ContextLimits(history_ceiling=100)
    harness = make_harness(
        tmp_path / "agent.db",
        limits=limits,
        extractor=FakeExtractor(),
    )
    for index in range(1, 4):
        seed_turn(harness.history, SESSION, index, "酒" * 48)

    with pytest.raises(ContextError) as info:
        asyncio.run(
            harness.orchestrator.begin_turn(SESSION, turn_id="t1")
        )

    assert info.value.code is ContextIssueCode.HISTORY_FLOOR_UNMET


def test_hard_cap_warns_on_finish_then_blocks_next_turn(
    tmp_path, monkeypatch
):
    """finish_turn 只记 warning；下一轮 begin_turn 再试一次才阻塞。"""
    monkeypatch.setattr(orchestrator_module, "HARD_ARCHIVE_LIMIT", 5)
    monkeypatch.setattr(compactor_module, "HARD_ARCHIVE_LIMIT", 5)

    limits = ContextLimits(history_ceiling=1_000)
    harness = make_harness(
        tmp_path / "agent.db",
        limits=limits,
        extractor=FakeExtractor(),
    )
    for index in range(1, 5):
        seed_turn(harness.history, SESSION, index, "酒" * 2)

    harness.llm.queue_chat("回答")
    _, events = asyncio.run(harness.run_turn("第五轮。", turn_id="t1"))

    data = final_data(events)
    assert "compaction" not in data
    assert "历史硬上限" in data["warnings"][0]
    assert harness.history.count_turns(SESSION) == 5

    with pytest.raises(ContextError) as info:
        asyncio.run(
            harness.orchestrator.begin_turn(SESSION, turn_id="t2")
        )

    assert info.value.code is ContextIssueCode.HISTORY_HARD_CAP_EXCEEDED


# --------------------------------------------------------------------- 取消


def test_cancelled_turn_is_not_persisted(tmp_path):
    llm = BlockingLLM("回答")
    harness = make_harness(
        tmp_path / "agent.db",
        llm=llm,
        extractor=FakeExtractor(),
    )

    async def scenario():
        task = asyncio.create_task(harness.run_turn("你好。", turn_id="t1"))
        await llm.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return harness.history.count_turns(SESSION)

    assert asyncio.run(scenario()) == 0

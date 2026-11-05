import asyncio
import tempfile
from pathlib import Path

from state.extractor import StateDelta
from state.models import SessionState
from state.service import StateService
from state.session import SessionRegistry
from state.store import StateStore
from state.updater import TurnStateUpdater


class FakeExtractor:
    """按顺序返回预设 delta，不调用模型。"""

    def __init__(self, deltas):
        self.deltas = list(deltas)

    async def extract(self, state: SessionState, messages):
        await asyncio.sleep(0.01)
        return self.deltas.pop(0)


async def main() -> None:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson19-smoke-",
        suffix=".db",
        delete=False,
    ) as handle:
        db_path = Path(handle.name)

    store = StateStore(db_path)
    store.initialize()
    service = StateService(SessionRegistry(store))
    extractor = FakeExtractor(
        [
            StateDelta(
                preferences_set={"style": "dry"},
                goals_add=["买两瓶干型酒"],
            ),
            StateDelta(preferences_set={"budget": 800}),
        ]
    )
    updater = TurnStateUpdater(extractor, service)

    try:
        messages = [
            {"role": "user", "content": "我喜欢干型酒，想买两瓶。"},
            {"role": "assistant", "content": "可以先看波尔多产区。"},
        ]

        first = await updater.update("demo", "turn-1", messages)
        duplicate = await updater.update("demo", "turn-1", messages)

        print("first applied:", first.applied)
        print("first version:", first.snapshot.version)
        print("duplicate:", duplicate.duplicate)
        print("version after duplicate:", duplicate.snapshot.version)

        task = updater.schedule("demo", "turn-2", messages)
        print("scheduled done:", task.done())
        await updater.wait("demo")

        snapshot = await service.get("demo")
        print("final version:", snapshot.version)
        print("preferences:", snapshot.state.preferences)
        print("goals:", [goal.text for goal in snapshot.state.goals])
    finally:
        db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())

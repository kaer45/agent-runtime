import asyncio
import sys

from agent.runtime import AgentRuntime


async def main() -> None:
    question = sys.argv[1] if len(sys.argv) > 1 else "今天波尔多适合酿酒吗？"
    runtime = AgentRuntime()

    async for event in runtime.run(question):
        print(event.type.value, event.task_id or "", event.data)


if __name__ == "__main__":
    asyncio.run(main())
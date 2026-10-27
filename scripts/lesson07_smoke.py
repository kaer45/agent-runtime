import asyncio
import sys

from logging_config import setup_logging
from agent.runtime import AgentRuntime

setup_logging()


async def main() -> None:
    question = sys.argv[1] if len(sys.argv) > 1 else "曹操的儿子叫什么"
    runtime = AgentRuntime()

    async for event in runtime.run(question):
        print(event.type.value, event.task_id or "", event.data)


if __name__ == "__main__":
    asyncio.run(main())
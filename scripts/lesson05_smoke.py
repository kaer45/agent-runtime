import asyncio
import sys

from logging_config import setup_logging
from agent.llm import LLM
from agent.planner import TaskPlanner
from tools.builtin import brewing, get_current_time, get_weather
from tools.registry import ToolRegistry

setup_logging()


async def main() -> None:
    question = sys.argv[1] if len(sys.argv) > 1 else "今天波尔多适合酿酒吗？"

    registry = ToolRegistry()
    registry.register(get_weather)
    registry.register(get_current_time)
    registry.register(brewing)

    planner = TaskPlanner(LLM(), registry)
    plan = await planner.plan(question)

    print(plan.model_dump_json(indent=2))
    print("拓扑序:", plan.get_dag().topo_order)


if __name__ == "__main__":
    asyncio.run(main())
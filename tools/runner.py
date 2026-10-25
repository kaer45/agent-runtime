import asyncio
import inspect
import json
import time
import logging
from typing import Any, TYPE_CHECKING

from tools.registry import ToolRegistry

# 仅在类型检查时导入，避免运行时循环依赖
if TYPE_CHECKING:
    from tasks.models import Task

logger = logging.getLogger(__name__)

class ToolRunner:
    '''工具执行器，和工具注册管理分离，单一职责原则'''

    def __init__(self, registry: ToolRegistry, timeout: float = 10.0, max_retries: int = 2) -> None:
        self.registry = registry
        self.timeout = timeout
        self.max_retries = max_retries

    async def run(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        error = None
        for attempt in range(self.max_retries + 1):
            try:
                tool = self.registry.get(tool_name)
                result = tool(**arguments)
                
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(
                        result,
                        timeout=self.timeout
                    )
                return {
                    "success": True,
                    "result": result,
                }
            except asyncio.TimeoutError as e:
                return self._error_result(
                    f"Tool '{tool_name}' execution timed out after '{self.timeout}s'."
                )
            except Exception as e:
                error = str(e)

            if attempt >= self.max_retries:
                return {
                    "success": False,
                    "error": error,
                    "attempt": attempt + 1
                }
            
            
            logger.warning("Tool '%s' retrying (%d/%d)...", tool_name, attempt + 1, self.max_retries)
            await asyncio.sleep(1)

        return self._error_result(f"Tool '{tool_name}' failed after {self.max_retries + 1} attempts")

    async def _execute(self, tool_call):
        tool_name = tool_call.function.name
        start_time = time.perf_counter()  # 每个工具开始前独立计时

        try:
            arguments = json.loads(tool_call.function.arguments)
            result = await self.run(
                tool_name=tool_name,
                arguments=arguments
            )
        except json.JSONDecodeError as e:
            result = self._error_result(f"Invalid json arguments: {e}.")
        except Exception as e:
            result = self._error_result(str(e))

        elapsed = time.perf_counter() - start_time
        status = "Success" if result.get("success") else "Failed"
        
        # 直接输出完整结果
        logger.info(
            "Tool completed: %s | status=%s | time=%.2fs", 
            tool_name, status, elapsed
        )
        
        return tool_call, result
        

    async def run_many(self, tool_calls: list):
        tasks = [
            self._execute(tool_call)
            for tool_call in tool_calls
        ]

        results = []

        for completed_task in asyncio.as_completed(tasks):
            tool_call, result = await completed_task
            
            results.append((
                tool_call,
                result
            ))

        return results


    def _error_result(self, error: str) -> dict:
        return {
            "success": False,
            "error": error
        }
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """事件类型枚举，用于前端或日志区分当前处于什么阶段。"""
    INTENT_DETECTED = "intent_detected"
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"

    PLAN_STARTED = "plan_started"
    PLAN_READY = "plan_ready"
    PLAN_FAILED = "plan_failed"
    
    MODEL_STARTED = "model_started"
    MODEL_FINISHED = "model_finished"
    
    TASK_STARTED = "task_started"
    TASK_SUCCESS = "task_success"
    TASK_FAILED = "task_failed"
    TASK_BLOCKED = "task_blocked"
    
    # 预留：用于更细粒度的工具执行监控
    TOOL_STARTED = "tool_started"
    TOOL_SUCCESS = "tool_success"
    TOOL_FAILED = "tool_failed"
    
    FINAL_ANSWER = "final_answer"


@dataclass
class RuntimeEvent:
    """运行时事件模型，通过 SSE 等机制实时推送给前端。"""
    type: EventType
    run_id: str
    task_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典，方便转换为 JSON。"""
        return {
            "type": self.type.value,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "data": self.data,
        }
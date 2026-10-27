"""先规则预筛，再用一次轻量 tool call 分类”。"""

from __future__ import annotations

import json
import logging

from enum import Enum
from typing import Any
from dataclasses import dataclass

from tools.registry import ToolRegistry
from agent.llm import LLM
from agent.chat_rules import match_chat_rule

    
logger = logging.getLogger(__name__)

INTENT_TOOL_NAME = "classify_intent"

INTENT_TOOL : dict[str,Any] = {
    "type":"function",
    "function": {
        "name": INTENT_TOOL_NAME,
        "description": "判断用户请求应该直接聊天回答，还是需要任务规划。",
        "parameters":{
            "type":"object",
            "properties": {
                "intent": {"type":"string","enum":["chat","task"]},
                "reason":{"type":"string","description":"一句话理由"},
            },
            "required":["intent"]
        }
    }
}

INTENT_PROMPT="""\
你是一个意图分类器。判断用户这句话应该走哪条路径：

- chat: 闲聊、问候、常识、知识问答、写作、观点、数学等，不需要实时数据或外部工具。
- task: 需要查询实时/外部信息，或需要调用工具、多步操作才能完成。

注意：
不要因为工具列表里存在某个工具，就强行调用它。
只有当用户的明确请求必须依赖外部工具（如查实时天气、算账、查库）时，才判定为 Task。如果是闲聊、常识问答、解释概念，请判定为 Chat。

可用工具（仅用于判断，不代表用户一定需要）：
"""

INTENT_RULES="""\
判断规则：
1. 不确定时选 chat，避免误执行工具。
2. 工具列表里有某个工具，不代表无关问题要判成 task。
3. 只通过 classify_intent 工具输出，不要输出解释。
4. 假设性问题（如"如果..."、"假如..."）优先判定为 chat，除非用户明确要求获取真实的当前数据。
5. 只根据现有工具能力判断。如果问题需要的数据现有工具无法提供（例如未来天气预报、实时新闻），选 chat。
6. 用户问的是建议、观点、假设或常识，即使提到天气/城市，也优先选 chat。
7. 不要为了让工具"有用"而把无关问题判成 task。

示例：
- "你好" -> chat
- "曹操的儿子叫什么" -> chat
- "1+1 等于几" -> chat
- "如果明天下雨，适合酿酒吗" -> chat
- "今天成都适合酿酒吗" -> task
- "帮我查一下成都天气" -> task
- "明天适合出去吃饭吗"（没有天气预报工具） -> chat
- "今天成都是否下雨"（get_weather 可以查当前天气） -> task
- "明天成都天气怎么样"（没有预报工具） -> chat
- "今天成都天气怎么样"（get_weather 可查当前） -> task
"""


class Intent(str,Enum):
    """用户请求的路由目标。"""
    CHAT = "chat"
    TASK = "task"

@dataclass(frozen=True)
class RouteDecision:
    intent: Intent
    source: str                  # "rule" | "llm" | "fallback"
    rule: str | None = None      # 命中的规则名，如 "greeting"
    reason: str = ""             # 模型给的一句话理由

class IntentRouter:
    """先经过正则化过滤 chat，不通过用一次轻量 LLM 调用判断 chat/task。"""

    def __init__(self,llm:LLM,registry:ToolRegistry):
        """初始化路由器。

        Args:
            llm: 只需要 async chat(...) 方法；测试时可注入假模型。
            registry: 提供工具名与描述，帮助判断是否需要工具。
        """
        self.llm=llm
        self.registry=registry

    def _system_prompt(self)->str:
        """拼接分类提示词和工具清单。"""
        tools=[]
        for name in self.registry.list_tools():
            fn=self.registry.get_tool_schema(name)["function"]
            tools.append(f"- {name}: {fn['description']}")
        return INTENT_PROMPT+"\n".join(tools)+"\n\n"+INTENT_RULES

    async def decide(self,user_input:str)->RouteDecision:
        """先调用正则过滤 chat，不通过调用一次轻量 LLM 判断 chat/task"""
        rule = match_chat_rule(user_input)
        if rule:
            logger.debug("命中聊天规则: %s", rule)
            return RouteDecision(Intent.CHAT, source="rule", rule=rule)
        messages=[
            {"role":"system","content":self._system_prompt()},
            {"role":"user","content":user_input}
        ]
        
        try:
            response=await self.llm.chat(
                messages=messages,
                tools=[INTENT_TOOL],
                tool_choice={"type":"function","function":{"name":INTENT_TOOL_NAME}},
                thinking=False,
                temperature=0,
            )
        except Exception:
            logger.exception("意图分类请求失败，回退到 chat")
            return RouteDecision(Intent.CHAT, source="fallback")

        call = self._find_call(response)
        if call is None:
            logger.warning("意图分类没有返回工具调用，回退到 chat")
            return RouteDecision(Intent.CHAT, source="fallback")

        try:
            arguments = json.loads(call.function.arguments)
            if not isinstance(arguments, dict):
                raise TypeError("intent arguments must be an object")
            return RouteDecision(
                Intent(arguments["intent"]),
                source="llm",
                reason=arguments.get("reason", ""),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("意图分类结果不合法，回退到 chat: %r", call.function.arguments)
            return RouteDecision(Intent.CHAT, source="fallback")
        

    async def route(self, user_input: str) -> Intent:
        """兼容旧调用方。"""
        return (await self.decide(user_input)).intent

    @staticmethod
    def _find_call(response):
        """返回名为 classify_intent 的 tool call；没有则返回 None。"""
        for call in response.tool_calls or []:
            if call.function.name == INTENT_TOOL_NAME:
                return call
        return None

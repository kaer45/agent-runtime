"""文本到 token 的数的粗略估算。

本模块只提供最基础的 token 计数能力，不感知消息结构、
上下文策略或模型厂商。``estimate_tokens`` 是默认实现，
生产环境可以通过 ``TokenCounter`` 注入厂商 tokenizer。
"""
from typing import Callable

TokenCounter = Callable[[str], int] # 实现依赖注入，相当于声明好了一个接口，接收一个字符串，返回统计的token数，以后可以传不同实现

def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：CJK 字符按 1，其他字符按约 4 字符 1 token。

    这不是精确 tokenizer，只用于预算规划。生产环境应留安全余量，
    或注入厂商 tokenizer。
    """
    if not text:
        return 0

    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other = len(text) - cjk
    return cjk + (other + 3) // 4
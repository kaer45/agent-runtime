import asyncio
from datetime import datetime
from typing import Literal

async def get_weather(city: str) -> str:
    """获取指定城市的当前天气。

    只返回当前时刻的天气，不能查询未来预报或历史天气。
    目前使用模拟数据，不访问真实天气服务。

    Args:
        city: 城市名，使用中文或拼音，如 "成都" 或 "chengdu"。

    Returns:
        自然语言描述的天气信息，例如 "成都的天气是晴天，气温25℃"。

    Raises:
        ValueError: 当 city 为空字符串时抛出。
    """
    if not city or not city.strip():
        raise ValueError("city 不能为空")

    await asyncio.sleep(3)  # 模拟网络调用
    return f"{city}的天气是晴天，气温25℃"


async def get_current_time() -> str:
    """获取当前系统时间。

    返回本地时区的当前日期和时间，格式为 "YYYY-MM-DD HH:MM:SS"。

    Returns:
        格式化的当前时间字符串，例如 "2026-10-22 14:30:00"。
    """
    await asyncio.sleep(2)
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def brewing(weather: str, current_time: str) -> str:
    """根据天气和当前时间判断是否适合酿造葡萄酒。

    必须同时提供天气和时间；通常把 get_weather 与 get_current_time 的结果作为输入。
    目前为模拟逻辑，不会真的启动酿造流程。

    Args:
        weather: 天气描述，例如 "晴天" 或 "成都的天气是晴天，气温25℃"。
        current_time: 当前时间字符串，格式为 "YYYY-MM-DD HH:MM:SS"。

    Returns:
        综合天气与时间后的结论文本。
    """
    await asyncio.sleep(0.5)

    return (
        "综合结果："
        f"天气是【{weather}】，"
        f"当前时间是【{current_time}】。"
        f"非常适合酿葡萄酒"
    )


async def recommend_wine_pairing(
    occasion: Literal["dinner", "party", "gift"],
    styles: list[Literal["dry", "sweet", "sparkling"]],
    guest_count: int = 2,
    budget: float | None = None,
    include_notes: bool = False,
) -> str:
    """根据场合、人数和酒款风格生成模拟选酒建议。

    只做本地模拟，不访问真实库存、价格或餐厅数据。
    适合演示 list、Literal、可选参数和布尔开关等复杂参数。

    Args:
        occasion: 场合，只能是 dinner、party 或 gift。
        styles: 酒款风格列表，可多选；每个元素只能是 dry、sweet 或 sparkling。
        guest_count: 用餐人数，默认 2。
        budget: 总预算（元）；不传表示不限预算。
        include_notes: 是否附加配餐说明，默认 False。

    Returns:
        一段模拟选酒建议，例如 "模拟选酒建议：场合为【晚餐】..."。
    """
    occasion_label = {
        "dinner": "晚餐",
        "party": "派对",
        "gift": "送礼",
    }[occasion]
    style_labels = {
        "dry": "干型",
        "sweet": "甜型",
        "sparkling": "起泡",
    }
    style_text = "、".join(style_labels[style] for style in styles) or "未指定"

    await asyncio.sleep(0.2)  # 模拟查询库存与配餐数据

    budget_text = f"，总预算 {budget:.0f} 元" if budget is not None else ""
    notes = ""
    if include_notes:
        notes = " 配餐建议：干型配牛排，甜型配甜点，起泡酒适合开场。"

    return (
        f"模拟选酒建议：场合为【{occasion_label}】，"
        f"风格为【{style_text}】，共 {guest_count} 人{budget_text}。"
        f"建议准备 2 款酒，按人数和预算调整数量。{notes}"
    )
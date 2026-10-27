import asyncio
from datetime import datetime

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
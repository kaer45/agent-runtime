import asyncio
from datetime import datetime

async def get_weather(city: str) -> str:
    """根据城市名获取当前天气信息。

    返回指定城市的天气状况和气温，目前为模拟数据。

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

    综合天气与时间信息，给出是否适合酿酒的结论，目前为模拟逻辑。

    Args:
        weather: 天气描述，例如 "晴天" 或 "成都的天气是晴天，气温25℃"。
        current_time: 当前时间字符串，格式为 "YYYY-MM-DD HH:MM:SS"，例如 "2026-10-22 14:30:00"。

    Returns:
        综合天气与时间后的结论文本，例如 "综合结果：天气是【晴天】，当前时间是【2026-10-22 14:30:00】。非常适合酿葡萄酒"。
    """
    await asyncio.sleep(0.5)

    return (
        "综合结果："
        f"天气是【{weather}】，"
        f"当前时间是【{current_time}】。"
        f"非常适合酿葡萄酒"
    )
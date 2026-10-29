import asyncio
from types import SimpleNamespace

from context.summarizer import HistorySummarizer, RollingSummary


class FakeLLM:
    async def chat(self, messages, **kwargs):
        return SimpleNamespace(content="用户喜欢干型酒；预算 1000 元；待确认是否需要礼盒。")


async def main() -> None:
    summarizer = HistorySummarizer(FakeLLM())
    summary = RollingSummary()

    messages = [
        {"role": "user", "content": "我想买两瓶干型酒，预算 1000 元。"},
        {"role": "assistant", "content": "推荐赤霞珠。"},
    ]
    summary = await summarizer.update(summary, messages)

    print("摘要:", summary.text)
    print("已覆盖消息数:", summary.covered_messages)


if __name__ == "__main__":
    asyncio.run(main())

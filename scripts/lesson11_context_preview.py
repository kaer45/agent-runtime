import asyncio
from agent.llm import LLM
from context.budget import TokenBudget, estimate_tokens, TokenCounter,BudgetResult
from context.summarizer import HistorySummarizer,RollingSummary

async def test():
    llm = LLM()
    
    # 1. 构造一个有足够多内容的 history
    history = [
        # --- 已经被旧摘要覆盖的前 3 条（不要传给 summarizer）---
        {"role": "user", "content": "你好，我想了解下葡萄酒。"},
        {"role": "assistant", "content": "你好！我是酿酒大师，很高兴为你服务。"},
        {"role": "user", "content": "我比较喜欢干型酒，预算大概 500 元。"},

        # --- 发生超限，被裁掉的新消息（这些才是真正需要总结的）---
        {"role": "assistant", "content": "好的，500元预算的话，波尔多产区有一些不错的选择。"},
        {"role": "user", "content": "我听说赤霞珠不错？"},
        {"role": "assistant", "content": "是的，赤霞珠单宁丰富，非常适合陈年。"},
        {"role": "user", "content": "那如果是两个人喝呢？"},
        {"role": "assistant", "content": "两个人喝的话，准备一瓶 750ml 的就足够了。"},
        {"role": "user", "content": "有推荐的配餐吗？"},
        {"role": "assistant", "content": "干型红酒搭配牛排、羊排等红肉非常合适。"},

        # --- 保留下来的最新消息 ---
        {"role": "user", "content": "好的，那推荐个酒庄吧。"}
    ]

    # 上一次的摘要已经覆盖了最前面的 3 条
    previous_summary = RollingSummary(
        text="用户喜欢干型酒，预算 500 元。",
        covered_messages=3
    )
    output_reverse=20
    summarize_reverse=30
    budget = TokenBudget(
        max_tokens=100,
        reserve_output=output_reverse+summarize_reverse,
        counter=estimate_tokens,
    )

    result=budget.fit(history)
    
    print(f"--- 裁剪前总消息数: {len(history)} ---")
    print(f"--- 裁剪前token数: {result.original_tokens} ---")
    print(f"--- 裁剪后token数: {result.final_tokens} ---")
    print(f"--- 裁剪后被保留的消息: ---")
    for m in result.messages:
        print(f"[{m['role']}] {m['content']}")

    new_messages =[history[i] for i in result.dropped_indices if i>=previous_summary.covered_messages]
    print(f"\n--- 被裁掉的总数: {result.dropped_count} 条 ---")
    print(f"--- 实际需要送给摘要器的新增数: {len(new_messages)} 条 ---")

    summarizer=HistorySummarizer(
        llm=llm,
        max_summary_tokens=summarize_reverse-4 # 4 留给 MESSAGE_OVERHEAD
    )
    print("\n正在请求大模型生成滚动摘要...")
    summary=await summarizer.update(previous_summary,new_messages)

    print(f"\n=== 新的历史摘要 ===\n{summary.text}")
    print(f"=== 累计覆盖消息数: {summary.covered_messages} ===")

    context = [
        {"role": "system", "content": f"历史摘要：{summary.text}"},
        *result.messages,
    ]
    print("\n=== 最终上下文 ===")
    for message in context:
        print(f"[{message['role']}] {message['content']}")


if __name__=="__main__":
    asyncio.run(test())
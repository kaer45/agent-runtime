from context.budget import BudgetExceededError, TokenBudget


def main() -> None:
    messages = [
        {"role": "system", "content": "你是葡萄酒酿酒大师。"},
        {"role": "user", "content": "旧问题：今天适合酿酒吗？"},
        {"role": "assistant", "content": "旧回答：适合。"},
        {"role": "user", "content": "新问题：帮我推荐一款干型酒。"},
    ]

    budget = TokenBudget(max_tokens=40, reserve_output=8)
    print("可用输入预算:", budget.available_tokens)
    print("裁剪前 token:", budget.count_messages(messages))

    result = budget.fit(messages)
    print("裁剪后 token:", result.final_tokens)
    print("丢弃下标:", result.dropped_indices)
    for message in result.messages:
        print(f"- {message['role']}: {message['content']}")

    try:
        TokenBudget(max_tokens=10, reserve_output=0).fit(
            [
                {"role": "system", "content": "1234567890"},
                {"role": "user", "content": "1234567890"},
            ]
        )
    except BudgetExceededError as exc:
        print("超预算:", exc)


if __name__ == "__main__":
    main()

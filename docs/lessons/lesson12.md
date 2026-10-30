# 第 12 课：结构化会话状态

> 模块三 · 上下文与状态（第 3/11 课）
> 前置：第 11 课（滚动摘要已经能保留自然语言脉络）
> 下一课：第 13 课从对话里自动提取状态变更
> 预计用时：2-3 小时
> 本课分支：`dev`

---

## 0. 开工前检查

先确认第 11 课是干净的：

```powershell
git switch dev
git status --short
.venv\Scripts\python.exe -m pytest -q
```

期望：`120 passed`。

第 12 课不新增依赖，继续使用 Pydantic 2。

---

## 1. 本课目标

把「会话状态」从自然语言摘要里独立出来，定义一份**可查询、可校验、可序列化**的结构：

```text
SessionState
├─ preferences   用户偏好，例如 style=dry、budget=500
├─ goals         目标，例如「买两瓶干型酒」
├─ constraints   约束，例如「预算不超过 500 元」
├─ progress      进度，例如「选酒: in_progress」
└─ facts         已确认事实，例如「产区: 波尔多」
```

学完你应该能回答：

1. 摘要和结构化状态各自负责什么？为什么不能只有摘要？
2. 为什么 `preferences` / `facts` 先用 `dict`，而 `goals` 用模型列表？
3. 为什么状态模型要 `extra="forbid"`？
4. 为什么 `session_id`、版本号、更新时间不在本课加？

**本课产出**

| 交付物 | 说明 |
|---|---|
| `state/models.py` | `GoalStatus`、`Goal`、`SessionState` |
| `state/__init__.py` | 导出状态模型 |
| `tests/unit/test_lesson12_session_state.py` | 8 个测试 |
| `scripts/lesson12_smoke.py` | 不调用模型的状态序列化冒烟脚本 |

**本课明确不做**

- 不从对话自动提取状态（第 13 课）
- 不做 SQLite 持久化（第 14 课）
- 不做多会话隔离（第 15 课）
- 不做状态合并、版本冲突（第 16 课）
- 不做状态渲染进 prompt（第 17 课）
- 不修改 `AgentRuntime`

---

## 2. 问题：摘要能读，但程序不好改

第 11 课的摘要回答的是：

```text
用户喜欢干型酒，预算 500 元，已经聊到波尔多。
```

它适合让模型“读”，但不适合程序“改”：

- 用户下一轮说「预算改成 800」，你怎么在摘要里精确找到 500 并替换？
- 用户说「我不想要波尔多了」，你怎么知道要删除哪条事实？
- 两个摘要版本怎么比较？怎么判断哪条偏好更新？
- 摘要文本长度不稳定，程序无法可靠查询“当前预算是多少”。

所以需要把关键事实从摘要里拆出来：

| 维度 | 摘要 | 结构化状态 |
|---|---|---|
| 形式 | 自然语言 | 字段/模型 |
| 适合 | 给模型读、保留脉络 | 程序查询、合并、持久化 |
| 更新 | 重新生成整段 | 按字段更新 |
| 冲突 | 难判断 | 可以版本化/合并 |
| 长度 | 不稳定 | 可控 |

一句话：

> **摘要负责“语气和脉络”，状态负责“事实和字段”。**

---

## 3. 设计决策

### 3.1 用 Pydantic，不用普通 dataclass

| 方案 | 优点 | 缺点 |
|---|---|---|
| dataclass | 轻 | 手写校验、序列化、嵌套转换 |
| Pydantic | 已有依赖，校验/JSON/嵌套都现成 | 模型略重 |

项目已经用 Pydantic 2 定义 `TaskPlan`，状态模型继续用 Pydantic，保持一致。

### 3.2 字段设计

```python
class SessionState(BaseModel):
    preferences: dict[str, Any]
    goals: list[Goal]
    constraints: list[str]
    progress: dict[str, str]
    facts: dict[str, Any]
```

为什么这样分：

- `preferences`：键值对最灵活，例如 `style="dry"`、`budget=500`；过早强类型会限制提取器。
- `goals`：目标有状态（active/done/abandoned），用 `Goal` 模型更清楚。
- `constraints`：约束通常是短句列表，例如「预算不超过 500 元」。
- `progress`：会话级进度，`步骤 -> 状态`；不要和任务状态机（第 21 课）混为一谈。
- `facts`：已确认事实，键值对，例如「产区=波尔多」「年份=2019」。

### 3.3 `GoalStatus` 用枚举

```python
class GoalStatus(str, Enum):
    ACTIVE = "active"
    DONE = "done"
    ABANDONED = "abandoned"
```

好处：

- 状态值固定，不会出现 `"finished"` / `"completed"` / `"完成"` 混用；
- 序列化后是字符串，方便存 JSON/SQLite；
- 第 13 课提取器只能从固定集合里选。

### 3.4 `extra="forbid"`

状态模型默认拒绝未知字段：

```python
model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
```

理由：

- 提取器如果把 `preference` 拼成 `preferences`，应该立刻报错，而不是静默丢字段；
- 状态是要持久化的，未知字段会让 schema 漂移；
- 新增字段必须显式改模型，不能靠“模型随便加”。

`str_strip_whitespace=True` 会去掉字符串首尾空格，避免 `" 买酒 "` 和 `"买酒"` 被当成两个目标。

### 3.5 状态模型只做契约，不做业务

本课只定义数据形状和校验：

- 不做“预算必须大于 0”这种业务规则；
- 不做“目标是否合理”的判断；
- 不做合并、版本、持久化。

这些分别属于提取器、状态服务和存储层。模型保持“纯”。

### 3.6 为什么先不加 `session_id` / `version` / `updated_at`

这些字段会在后面出现：

- `session_id`：第 15 课多会话隔离；
- `version` / 冲突：第 16 课；
- `updated_at` / 持久化元数据：第 14 课。

现在加会让模型过早绑定存储实现。第 12 课只关心“状态里有什么事实”。

---

## 4. 接口与数据结构

### 4.1 `GoalStatus`

```python
class GoalStatus(str, Enum):
    ACTIVE = "active"
    DONE = "done"
    ABANDONED = "abandoned"
```

### 4.2 `Goal`

```python
class Goal(BaseModel):
    text: str
    status: GoalStatus = GoalStatus.ACTIVE
```

约束：

- `text` 不能为空；
- 自动 strip；
- 未知字段拒绝。

### 4.3 `SessionState`

```python
class SessionState(BaseModel):
    preferences: dict[str, Any] = Field(default_factory=dict)
    goals: list[Goal] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    progress: dict[str, str] = Field(default_factory=dict)
    facts: dict[str, Any] = Field(default_factory=dict)
```

校验：

- goals 不允许重复文本；
- constraints 不允许重复、不允许空字符串；
- preferences / facts / progress 的 key 不允许空；
- 未知字段拒绝。

### 4.4 序列化

```python
payload = state.model_dump(mode="json")
restored = SessionState.model_validate(payload)
assert restored == state
```

第 14 课会把 `payload` 存进 SQLite。

---

## 5. 实现步骤

### 第 1 步：建 `state/` 包

```text
state/
├── __init__.py
└── models.py
```

✅ 完成标志：`from state.models import SessionState` 可用。

### 第 2 步：定义 `GoalStatus` 和 `Goal`

- 枚举值固定；
- `text` 自动 strip；
- 空字符串拒绝；
- `extra="forbid"`。

✅ 完成标志：`Goal(text="  ")` 抛 `ValidationError`。

### 第 3 步：定义 `SessionState`

- 五个字段；
- `default_factory` 避免可变默认值；
- `extra="forbid"`；
- 字符串 strip。

✅ 完成标志：空状态 `SessionState()` 五个字段都是空容器。

### 第 4 步：加跨字段校验

- goals 文本唯一；
- constraints 唯一且非空；
- dict key 非空。

✅ 完成标志：重复目标/约束、空 key 都能报错。

### 第 5 步：测试序列化

- `model_dump(mode="json")`；
- `model_validate` 回读；
- 枚举序列化成字符串。

✅ 完成标志：8 个测试全绿。

### 第 6 步：冒烟

```powershell
.venv\Scripts\python.exe -m scripts.lesson12_smoke
```

不需要 API key。

---

## 6. 测试代码

### 6.1 `tests/unit/test_lesson12_session_state.py`

> 当前测试是「先红后绿」：实现 `state/models.py` 之前，会因 `ModuleNotFoundError: No module named 'state'` 失败。

```python
import pytest
from pydantic import ValidationError

from state.models import Goal, GoalStatus, SessionState


def test_session_state_defaults_are_empty():
    state = SessionState()

    assert state.preferences == {}
    assert state.goals == []
    assert state.constraints == []
    assert state.progress == {}
    assert state.facts == {}


def test_session_state_accepts_full_payload():
    state = SessionState(
        preferences={"style": "dry", "budget": 500},
        goals=[{"text": "买两瓶干型酒"}],
        constraints=["预算不超过 500 元"],
        progress={"选酒": "in_progress"},
        facts={"产区": "波尔多"},
    )

    assert state.preferences["style"] == "dry"
    assert state.goals[0].text == "买两瓶干型酒"
    assert state.goals[0].status is GoalStatus.ACTIVE
    assert state.progress["选酒"] == "in_progress"
    assert state.facts["产区"] == "波尔多"


def test_goal_text_is_stripped_and_cannot_be_blank():
    goal = Goal(text="  买酒  ")
    assert goal.text == "买酒"

    with pytest.raises(ValidationError):
        Goal(text="   ")


def test_duplicate_goals_are_rejected():
    with pytest.raises(ValidationError, match="重复目标"):
        SessionState(
            goals=[
                {"text": "买酒"},
                {"text": "买酒"},
            ]
        )


def test_duplicate_constraints_are_rejected():
    with pytest.raises(ValidationError, match="重复约束"):
        SessionState(constraints=["预算 500", "预算 500"])


def test_blank_constraint_is_rejected():
    with pytest.raises(ValidationError):
        SessionState(constraints=["   "])


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        SessionState(unknown_field="x")


def test_state_round_trips_through_json():
    state = SessionState(
        preferences={"style": "dry"},
        goals=[{"text": "买酒", "status": "done"}],
        constraints=["预算 500"],
        progress={"选酒": "done"},
        facts={"产区": "波尔多"},
    )

    payload = state.model_dump(mode="json")
    restored = SessionState.model_validate(payload)

    assert restored == state
    assert payload["goals"][0]["status"] == "done"
```

### 6.2 这 8 个测试分别防什么

| 测试 | 证明 |
|---|---|
| `test_session_state_defaults_are_empty` | 默认状态没有共享可变对象 |
| `test_session_state_accepts_full_payload` | 五个字段和 Goal 状态都能正常解析 |
| `test_goal_text_is_stripped_and_cannot_be_blank` | 空目标不会进入状态 |
| `test_duplicate_goals_are_rejected` | 目标不会重复 |
| `test_duplicate_constraints_are_rejected` | 约束不会重复 |
| `test_blank_constraint_is_rejected` | 空约束不会污染状态 |
| `test_unknown_field_is_rejected` | schema 漂移会立即报错 |
| `test_state_round_trips_through_json` | 状态可序列化、可回读，枚举变字符串 |

运行：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_lesson12_session_state.py -q
```

期望 `8 passed`；全量期望 `128 passed`。

---

## 7. 冒烟脚本

`scripts/lesson12_smoke.py`：

```python
from state.models import GoalStatus, SessionState


def main() -> None:
    state = SessionState(
        preferences={"style": "dry", "budget": 500},
        goals=[{"text": "买两瓶干型酒"}],
        constraints=["预算不超过 500 元"],
        progress={"选酒": "in_progress"},
        facts={"产区": "波尔多"},
    )

    print(state.model_dump_json(indent=2))

    state.goals[0].status = GoalStatus.DONE
    print("目标状态:", state.goals[0].status.value)


if __name__ == "__main__":
    main()
```

运行：

```powershell
.venv\Scripts\python.exe -m scripts.lesson12_smoke
```

期望输出包含：

```json
{
  "preferences": {"style": "dry", "budget": 500},
  "goals": [{"text": "买两瓶干型酒", "status": "active"}],
  "constraints": ["预算不超过 500 元"],
  "progress": {"选酒": "in_progress"},
  "facts": {"产区": "波尔多"}
}
```

最后一行：

```text
目标状态: done
```

---

## 8. 参考实现

### 8.1 `state/models.py`

```python
"""结构化会话状态：偏好、目标、约束、进度和已确认事实。"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class GoalStatus(str, Enum):
    ACTIVE = "active"
    DONE = "done"
    ABANDONED = "abandoned"


class Goal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str
    status: GoalStatus = GoalStatus.ACTIVE
    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("goal.text 不能为空")
        return value


class SessionState(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    preferences: dict[str, Any] = Field(default_factory=dict)
    goals: list[Goal] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    progress: dict[str, str] = Field(default_factory=dict)
    facts: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_state(self):
        goal_texts = [goal.text for goal in self.goals]
        if len(goal_texts) != len(set(goal_texts)):
            raise ValueError("goals 中存在重复目标")

        if len(self.constraints) != len(set(self.constraints)):
            raise ValueError("constraints 中存在重复约束")
        if any(not constraint.strip() for constraint in self.constraints):
            raise ValueError("constraints 不能包含空字符串")

        if any(not key.strip() for key in self.preferences):
            raise ValueError("preferences 的 key 不能为空")
        if any(not key.strip() for key in self.facts):
            raise ValueError("facts 的 key 不能为空")
        if any(not key.strip() for key in self.progress):
            raise ValueError("progress 的 key 不能为空")

        return self
```

### 8.2 `state/__init__.py`

```python
from .models import Goal, GoalStatus, SessionState

__all__ = ["Goal", "GoalStatus", "SessionState"]
```

---

## 9. 验收清单（DoD）

- [x] `state/models.py` 存在，不依赖网络
- [x] `SessionState()` 的五个字段都是空容器，没有共享可变默认值
- [x] `Goal` 的文本会 strip，空文本被拒绝
- [x] 重复 goals / constraints 被拒绝
- [x] 空 constraint、空 dict key 被拒绝
- [x] 未知字段被拒绝
- [x] JSON 序列化/反序列化可往返
- [x] `pytest tests/unit/test_lesson12_session_state.py -q` → 8 passed
- [x] 全量 `pytest -q` → 128 passed
- [x] 冒烟脚本不调用模型

---

## 10. 常见坑

| 坑 | 现象 | 处理 |
|---|---|---|
| 用可变默认值 | 两个状态共享同一个 list/dict | 用 `Field(default_factory=...)` |
| 不 strip 字符串 | `"买酒"` 和 `" 买酒 "` 被当两个目标 | `str_strip_whitespace=True` |
| 允许未知字段 | 提取器拼错字段也被静默忽略 | `extra="forbid"` |
| 把摘要塞进 state | 状态字段变成大段自然语言 | 摘要单独存，state 只放关键事实 |
| 把 progress 当任务状态机 | 两套状态混用 | 会话进度先用 dict；任务状态第 21 课再做 |
| 过早加 session_id/version | 模型绑定存储实现 | 第 14/15/16 课再加 |
| 直接修改共享 state | 快照被后续更新污染 | 需要不可变时用 `model_copy(deep=True)` |

---

## 11. 扩展与思考题

1. **强类型偏好**：`budget: int`、`style: Literal[...]` 是否比 `dict[str, Any]` 更稳？代价是什么？
2. **目标状态机**：`GoalStatus` 是否要限制合法转换，例如 done 不能回到 active？
3. **事实来源**：`facts` 是否要记录来源消息 ID、置信度、更新时间？
4. **状态版本**：两个并发更新怎么合并？哪些字段是 last-write-wins，哪些要冲突提示？
5. **状态大小**：状态本身也占 token，怎么限制字段数量和字符串长度？
6. **隐私**：用户偏好/预算/地址是否要脱敏后再持久化？

---

## 12. 与第 13 课的衔接

本课定义了「状态长什么样」，但还没有回答：

```text
怎么从一轮对话里自动得出这些字段？
```

第 13 课做 `StateExtractor`：

```text
messages + current_state
        │
        ▼
StateExtractor
        │
        ▼
StateDelta
├─ preferences_add / update
├─ goals_add / complete
├─ constraints_add
├─ progress_update
└─ facts_add
```

一句话：

> **第 12 课定状态契约，第 13 课写提取规则，第 14 课把状态存起来。**

# ContextEngine v2 变更日志

> 冻结基线：[final-strategy.md](final-strategy.md) ·
> [context-engine-protocol.md](../context-engine-protocol.md)
> 冻结标签：`context-engine-v2-strategy-frozen-20261117`

## 1. 冻结规则

- 冻结日期：2026-11-17；
- `final-strategy.md` 冻结，**禁止直接在冻结版本上改业务逻辑**；
- 冻结后任何变更，先在本文件登记，再改文档，最后改代码；
- 固定顺序：**变更记录 → strategy → protocol → 代码 → 测试**；
- 不允许代码写完再补文档，也不允许代码驱动设计；
- 文档没有描述的，一律视为不在本期范围；
- strategy 与 protocol 冲突时，先改 strategy，再同步 protocol。

## 2. 基线范围

本期只做 ContextEngine + turn 级历史存储 + 摘要压缩 + 归档线 + 动态工具选择。

不在本期：AgentLoop、三种执行模式、工具执行循环、归档恢复 / 检索 / 长期记忆、
每轮摘要。

口径索引（细节以 strategy 为准，这里只做定位）：

| 主题 | strategy | protocol |
|---|---|---|
| Prompt 布局与标签转义 | §2 | §2 |
| 预算、静态分母、报告分母 | §3 | §3.1、§3.7 |
| Turn 级存储与元数据表 | §4 | §6 |
| 压缩触发、范围、摘要输入保护 | §5 | §7、§8 |
| 归档软线 / 硬线 | §6 | §7.5、§7.6 |
| 动态工具选择、exposed 语义 | §7 | §4.7、§4.9 |
| 报告、Issue、格式化 | §8 | §3.7–§3.10 |
| 旧实现处置、实现顺序 | §9、§10 | §12 |

## 3. 变更记录

新增记录复制下表，编号递增，不改历史行。

| 编号 | 日期 | 类型 | 变更原因 | strategy 章节 | protocol 章节 | 影响面 | 状态 |
|---|---|---|---|---|---|---|---|

（暂无记录。冻结后尚无业务逻辑变更。）

类型取值：`设计缺口` / `需求变更` / `口径修正` / `删除项`。
状态取值：`提议` / `已定稿` / `已实现`。

## 4. 未决项

不计入变更，等决策后再走第 3 节流程。

| 编号 | 问题 | 涉及位置 | 状态 |
|---|---|---|---|
| Q1 | optional 全为 0 分时是否显式写“按 registry 顺序全收”；预算放不下时是停止还是跳过 | strategy §7.1 / protocol §4.7 | 待定 |

## 5. 冻结前修订（已并入基线）

只作追溯，不再单独评审：

- 报告分母从 per-turn `active_capacity` 改为静态 `configured_capacity`，
  删除 `active_capacity` / `active_utilization` 字段；
- 明确 state / summary 没有 per-turn 开关，只有 tools 曾是候选；
- strategy 补 `session_turn_counters` / `history_schema_meta` 两张元数据表；
- strategy 补 `load(limit_turns=...)` 及“仅预览、禁止用于压缩判断”约束。

## 6. 里程碑与标签

| 里程碑 | 范围 | 计划标签 |
|---|---|---|
| M1 | 数据模型 + 元数据表 + `history/metrics.py` + `HistoryStore` | `context-engine-m1` |
| M2 | `ContextEngine` + 组件 + `ToolSelector` + 报告与展示 | `context-engine-m2` |
| M3 | `HistoryCompactor` + LLM 摘要适配 + `TurnOrchestrator` + middleware | `context-engine-m3` |

每个里程碑完成时：单元测试全绿 → 回填第 3 节状态 → 打标签。

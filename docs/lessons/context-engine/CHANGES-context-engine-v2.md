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
| C1 | 2026-11-17 | 口径修正 | `decide(metrics)` 无法判定 `FLOOR_UNMET`：判定依据是 `last6_tokens > history_ceiling`，而 `HistoryMetrics` 的四个字段里没有 ceiling。决策顺序、阈值、优先级都不变，只补参数。 | §5.3（顺序不变，无业务改动） | §7.2 签名改为 `decide(metrics, ceiling)` | `history/metrics.py`、`ContextEngine`（M2 issue）、`HistoryCompactor`（M3） | 已实现 |
| C2 | 2026-11-17 | 口径修正 | 预算算术错误：`4K+4K+8K+4K+8K+80K+512` 实际是 **108,512**，文档写成 108.5K / 108,500，等于把 512 取整成 0.5K。改为精确值，否则文档与 `to_display()` 输出对不上。 | §3.2、§3.3、§8.3 | §3.1、§3.7、§3.10、§11.2 | `ContextLimits.configured_capacity`、预算表展示、`tools_ceiling=0` 时的 100,512 | 已实现 |
| C3 | 2026-11-17 | 口径修正 | 消息计数下沉到 `core/tokens.py`，`TokenBudget` 只做委托。原因：`HistoryStore` 写入 `token_count` 也要用同一口径，但 history 不能反向 import context。算法与数值完全不变。 | §5.2（指标口径不变） | §3.2 措辞：改为“与 `TokenBudget.count_messages` 同源实现” | `core/tokens.py`、`context/budget.py`、`history/turn_store.py` | 已实现 |
| C4 | 2026-11-18 | 设计缺口 | `to_display()` 要打印 `Model Window` / `Reserved for Output` / `Safety`，但 `ContextReport` 只有 `available_tokens` 和 `configured_capacity`，无法反推这三项。给 report 增加 `limits` 字段（只读引用，不参与账目）。 | §8（字段清单位于 protocol，strategy 无需改） | §3.7 字段表新增 `limits: ContextLimits` | `ContextReport`、`to_display`、`to_log`（日志不含 limits） | 已实现 |
| C5 | 2026-11-18 | 设计缺口 | 工具预算裁剪遇到“分数更高但放不下”的候选时，文档只写“直到放不下”，未说明停止还是跳过。实现为**跳过并继续尝试更小的候选**：工具没有连续性要求，不该被一个超大 schema 卡住；历史仍保持“遇到第一个放不下就停”，因为要连续后缀。 | §7.1 选择规则第 3 条已补 | §4.7 选择规则第 3 条已补 | `ToolSelector`；Q1 的“停止/跳过”子问题关闭 | 已定稿 |
| C6 | 2026-11-18 | 口径修正 | Q1：文档只用规则 2 + 规则 4 隐含“全 0 分仍全部入选”，实现者可能把打分当过滤，导致所有 optional 都 0 分时一个工具都不暴露。补一句明文，把推导变成规则。 | §7.1 选择规则第 4 条 | §4.7 选择规则第 4 条 | `ToolSelector` 行为不变，已有测试覆盖 | 已定稿 |
| C7 | 2026-11-18 | 需求变更 | 摘要档位改成整数档：local `(2024, 4096, 8192)` → `(2000, 4000, 8000)`，model 相应 `int(x*0.7)` → `(1400, 2800, 5600)`。取整更易核对，且最高档 8000 与 `reserve_output` 同值。 | §5.7 档位表 | §8.2 档位表 | `history/turn_summarizer.py`；`input_ceiling = available - 5600` | 已实现 |

类型取值：`设计缺口` / `需求变更` / `口径修正` / `删除项`。
状态取值：`提议` / `已定稿` / `已实现`。

## 4. 未决项

不计入变更，等决策后再走第 3 节流程。

| 编号 | 问题 | 涉及位置 | 状态 |
|---|---|---|---|
| Q1 | optional 全为 0 分时，是否显式写“按 registry 顺序全部入选” | strategy §7.1 / protocol §4.7 | 已关闭（见 C6） |
| Q2 | `TokenBudget.fit` / `BudgetResult` / `BudgetExceededError` 的删除点，以及 `TokenBudget` 保留为薄计数器还是整体退役。连带 `context/__init__.py` 导出、`turn_orchestrator` 的异常捕获、两个 lesson script | strategy §9、§10.10；protocol §3.1 | 待 M3b 决定 |

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
| M1a | 数据模型 + `history/metrics.py` + `StateSnapshot` 移位 + 计数口径 | `context-engine-m1a` |
| M1b | 元数据表 + `HistoryStore`（`append_turn_once` / `load` / CAS / 归档） | `context-engine-m1b` |
| M2 | `ContextEngine` + 组件 + `ToolSelector` + 报告与展示 + thin slice | `context-engine-m2` |
| M3a | `LLMResponse` / `chat_with_meta` / `HistorySummarizer` | `context-engine-m3a` |
| M3b | `HistoryCompactor` + `TurnOrchestrator` + middleware + 删旧实现 | `context-engine-m3b` |

已打标签：

| 标签 | 指向 | 完成内容 |
|---|---|---|
| `context-engine-m1a` | `6fc5d31` | 数据模型、统一 Issue、`history/metrics.py`、`StateSnapshot` 移位；367 tests green |
| `context-engine-m2` | `78fd7c9` | `ContextEngine.assemble`、六个组件、`ToolSelector`、`select_recent_turns`、报告格式化、measure 截断、thin slice；509 tests green |

每个里程碑完成时：单元测试全绿 → 回填第 3 节状态 → 打标签。

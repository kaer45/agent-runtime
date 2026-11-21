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
| C8 | 2026-11-18 | 删除项 | Q2 关闭：`TokenBudget` 整体退役，不留薄壳。strategy §9 把它列在“保留/修改”，但 C3 之后它只剩三个委托方法，计数唯一入口已是 `core/tokens.py`；保留会诱导新代码继续走旧入口。另：`history/turn_store.py`、`turn_summarizer.py`、`turns.py` 三个新模块名与尚未删除的旧实现同名，改名必须排在删除之后；删除又依赖 orchestrator 不再 import legacy，故 M3b 只保留“不碰旧代码”的两步，orchestrator 重写、删旧、改名合并为 M3c。 | §9 保留清单移除 `TokenBudget`；§10 顺序不变（8 压缩 → 9 编排 → 10 删旧），改名追加在 10 之后 | §3.1 两处 `TokenBudget.count_message(s)` 改为 `core.tokens` 口径；§3.2 `token_count` 措辞同步 | 删 `context/{budget,messages,builder,planner,renderer,prompts}.py`、`history/{store,summarizer,models}.py`；删 6 个旧测试 + 7 个旧脚本 + `test_token_budget_is_the_shared_message_counter`；保留并改写 `scripts/real_llm_state_e2e.py`；`turn_orchestrator` 改捕 `ContextError`；3 处 `git mv` | 已实现 |
| C9 | 2026-11-21 | 设计缺口 | `TurnOrchestrator` 接入 v2 后，每轮的装配报告和压缩结果需要暴露给调用方，但 `RuntimeEvent` 没有字段约定；装配失败若只给 `str(error)`，调用方无法按错误码分支。补事件契约：`RUN_FINISHED.data` 增加 `context`（`ContextReport.to_log()`）、`compaction`（decision / compacted / covered_through_seq / active_turns / issues[code,level,message]）、以及仅在 `ContextError` 时的 `error_code` + `level`；`FINAL_ANSWER` 仍在收尾之前发出；不新增 `EventType`。 | 无（事件属应用层，报告字段清单在 protocol） | §3.7 补“消费方”段：`context` / `compaction` / `error_code` 的落点 | `agent/turn_orchestrator.py`、`agent/runtime.py`、`tests/unit/test_context_v2_orchestrator.py` | 已实现 |

类型取值：`设计缺口` / `需求变更` / `口径修正` / `删除项`。
状态取值：`提议` / `已定稿` / `已实现`。

## 4. 未决项

不计入变更，等决策后再走第 3 节流程。

| 编号 | 问题 | 涉及位置 | 状态 |
|---|---|---|---|
| Q1 | optional 全为 0 分时，是否显式写“按 registry 顺序全部入选” | strategy §7.1 / protocol §4.7 | 已关闭（见 C6） |
| Q2 | `TokenBudget.fit` / `BudgetResult` / `BudgetExceededError` 的删除点，以及 `TokenBudget` 保留为薄计数器还是整体退役。连带 `context/__init__.py` 导出、`turn_orchestrator` 的异常捕获、两个 lesson script | strategy §9、§10.10；protocol §3.1 | 已关闭（见 C8，整体退役） |
| Q3 | 执行失败 / 超时的 tool 消息是否需要稳定错误码（如 `TOOL_TIMEOUT` / `TOOL_EXECUTION_FAILED`）。protocol §4.9 只规定了四个校验错误码，当前 middleware 对执行失败透传 runner 的 `error` 字符串，不新增码。 | protocol §4.9 | 待定（M4 AgentLoop 消费 tool 消息前决定） |
| Q4 | middleware 批量异常隔离：`agent/tool_middleware.py` 的 `execute` 用默认 `asyncio.gather`，任一 `_run_one` 抛异常（自定义 validator 抛错、工具结果循环引用导致 `json.dumps` 抛 `ValueError`、深层 JSON 触发 `RecursionError`）会让整批立即失败，且已启动的兄弟调用不会被取消、继续执行，结果被丢弃（幽灵执行，已实测）。修法方向：每个调用加 `Exception` 守卫并合成失败消息，保留默认 gather 以传播取消；不要用 `return_exceptions=True`（会把异常对象塞进消息列表，且要单独处理 `CancelledError`）。 | protocol §4.9 | 待修（M4 AgentLoop 前） |
| Q5 | `_parse_calls` 是批级硬失败：`execute` 在执行前一次性解析全部调用，一个缺 `id` / `function` 的调用抛 `ValueError`，整批工具都不执行（已实测：有效调用 `runner.calls == []`）。修法方向：能识别的坏调用（有 `id`）降级成 `INVALID_ARGUMENTS` 消息，只有缺 `id`、无法回填 `tool_call_id` 时才中止本轮；后者需要新增错误码或明确“整轮作废”，与 Q3 一起定。 | protocol §4.9 | 待定（M4 前） |
| Q6 | 执行模式 v3（E1/E2，独立于本期）：删 `ChatResponder`，由 TOOL 模式吸收；模式选择（TOOL / PLAN）从 `AgentRuntime` 移到 `TurnOrchestrator`，先用“规则 + 模型自选”；PLAN 必须支持并行 DAG；回退只允许在**未执行任何工具**时，执行中途失败转“部分结果 + 询问用户”，补偿留 E2；审批统一放在 `ToolMiddleware`（已确认不区分模式），待定的是同批其他调用在审批等待期间是否继续执行、以及审批状态的事件语义；PLAN 的规划与最终回答也必须走 `ContextEngine`；`submit_task_plan` 这类合成工具如何进入 engine 的 tools 列表待设计。 | 无（v3 新策略文档） | 待规划（M3d 之后） |
| Q7 | 课程历史文档（`docs/lessons/lesson10/11/17/18/19/20/20_5/20_6.md`）仍引用已删除的 `TokenBudget` / `ContextBuilder` / `ContextPlanner` / `context/renderer` / 旧 `history.*` 模块。这些是课程记录、不是冻结设计文档，C8 的删除清单未包含；后续统一加“代码已退役，见 context-engine v2”提示或重写相关段落。 | `docs/lessons/lesson*.md` | 待处理 |

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
| M3b-1 | `HistoryCompactor` + 单测（真 store、假 summarizer），不碰旧代码 | — |
| M3b-2 | 执行侧 middleware（registry → exposed → whitelist → schema、失败 tool 消息）+ 单测 | `context-engine-m3b` |
| M3c-0 | `TurnOrchestrator` 设计讨论，结论回填 protocol 与本文件 | — |
| M3c-1 | `TurnOrchestrator` 重写 + `agent/runtime.py` 重配 | — |
| M3c-2 | 删旧实现、旧测试、旧脚本（C8 清单） | — |
| M3c-3 | 改名：`turn_store.py`→`store.py`、`turn_summarizer.py`→`summarizer.py`、`turns.py`→`models.py`，单独提交 | `context-engine-m3c` |
| M3d | 集成测试 + 故障注入 + 真实模型 e2e（默认跳过） | `context-engine-m3d` |

真实模型 e2e 在 `tests/e2e/`，默认 skip；设置 `LLM_API_KEY` / `LLM_BASE_URL`
后运行 `pytest tests/e2e -q`，约 10–12 次真实调用。

依赖关系：删除（M3c-2）依赖 orchestrator 不再 import legacy（M3c-1）；改名（M3c-3）
依赖旧文件已删除，否则目标文件名被占用。因此两者都排在 M3c 尾部。
改名中只有 `turn_summarizer.py`→`summarizer.py` 是文档要求（strategy §5.7 写明
`SUMMARY_INPUT_CEILING` 放 `history/summarizer.py`，M3a 为避开旧模块临时改了名），
另两处是为消除命名漂移，行为零变化。

已打标签：

| 标签 | 指向 | 完成内容 |
|---|---|---|
| `context-engine-m1a` | `6fc5d31` | 数据模型、统一 Issue、`history/metrics.py`、`StateSnapshot` 移位；367 tests green |
| `context-engine-m1b` | `84d0a91` | turn 级 `HistoryStore`（schema fail-fast、`append_turn_once`、`load`、`save_summary_cas`、`archive_covered`、`clear`）、turn 结构校验、计数口径下沉 `core/tokens`；419 tests green |
| `context-engine-m2` | `78fd7c9` | `ContextEngine.assemble`、六个组件、`ToolSelector`、`select_recent_turns`、报告格式化、measure 截断、thin slice；509 tests green |
| `context-engine-m3a` | `c421424` | `LLMResponse` / `chat_with_meta`、`SummaryError`、`HistorySummarizer` 双档位重试与 `fit_turns` 精确前缀、C7 档位 `(2000, 4000, 8000)`；532 tests green |
| `context-engine-m3b` | `13862a2` | `HistoryCompactor`（触发顺序、per-session lock、CAS、软/硬归档、硬上限）、`ToolMiddleware`（registry→exposed→白名单→schema 四层校验、四个错误码、按序回填）、C8、Q3–Q5；581 tests green |
| `context-engine-m3c` | `8404ac4` | `TurnOrchestrator` v2 接线（engine / compactor / 硬上限闸门 / C9 事件契约）、删 legacy 模块与旧测试脚本、`history/{store,summarizer,models}.py` 归位；489 tests green |

每个里程碑完成时：单元测试全绿 → 回填第 3 节状态 → 打标签。

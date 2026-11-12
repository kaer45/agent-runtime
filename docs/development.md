# 开发期约定

> 适用阶段：**快速开发期，尚未有真实用户数据**。
> 目标：允许 schema 快速演进，不把旧数据兼容代码带进核心路径。
> 相关记录：[blockers.md](lessons/blockers.md)

---

## 1. 当前策略：数据库可丢弃

当前 `data/agent.db` 是开发库，不承载真实用户数据。

约定：

```text
schema 变更 → 删除 data/agent.db → 重启 → 新库按当前 schema 重建
```

不维护：

- 旧表 `ALTER TABLE`；
- 旧 JSON 字段回填；
- 旧 schema 的条件分支；
- 为旧数据保留的测试。

`StateStore` 和 `HistoryStore` 共用同一个 SQLite 文件，所以删除一个
`data/agent.db` 会同时重置状态和历史。

---

## 2. 允许与不允许

| 动作 | 当前阶段 |
|---|---|
| 增加字段 / 改字段类型 | 允许，删库重建 |
| 新增表 / 删除表 | 允许，删库重建 |
| 修改状态 JSON 结构 | 允许，删库重建 |
| 为旧表写 `ALTER TABLE` | 不做 |
| 为旧 JSON 写 backfill | 不做 |
| 静默兼容旧 schema | 不做 |
| 检测到旧 schema 后继续运行 | 不做，直接报错 |

---

## 3. 旧 schema 处理：fail fast

新库直接创建当前 schema，不再尝试修改旧表。

如果检测到旧表结构不匹配，应该：

```text
抛出明确错误 → 提示删除 data/agent.db → 重启
```

而不是：

```text
ALTER TABLE 补列 → 继续运行
```

原因：

- 开发期没有真实数据，迁移代码没有收益；
- 静默 ALTER 会掩盖 schema 漂移；
- 兼容分支会长期留在核心读写路径；
- 旧库成功升级后，开发者更难判断当前到底跑的是哪版 schema。

错误信息要直接给出动作：

```text
数据库 schema 过旧：请删除 data/agent.db 后重新启动
```

---

## 4. Goal ID 约定

当前开发期决定：

- `Goal.id` 在创建时生成；
- 不再依赖“读取时补 ID”的 `ensure_goal_ids()`；
- 旧 JSON 缺少 `id` 不回头兼容；
- 旧库直接删除重建。

目标形态：

```python
def new_goal_id() -> str:
    return "g_" + uuid.uuid4().hex[:12]


class Goal(BaseModel):
    id: str = Field(default_factory=new_goal_id)
    text: str
    status: GoalStatus = GoalStatus.ACTIVE
```

这样新 Goal 一定有 ID，不需要在 `StateService.get()` 里做数据迁移。

> `ensure_goal_ids()` 是第 16 课旧数据兼容的产物；当前阶段已删除。

---

## 5. 什么时候切换成正式迁移

满足任一条件，就停止“删库重建”：

- 有真实用户或客户数据；
- 部署到共享/生产环境；
- 数据不能随时删除；
- 需要支持旧版本回滚；
- 多个实例共用一个数据库。

切换后需要：

```text
schema_version
migrations/
备份
回滚方案
```

在此之前，不要提前搭半套 migration 框架。

---

## 6. 常用开发命令

确认当前数据库：

```powershell
Get-ChildItem data -ErrorAction SilentlyContinue
```

删除开发库：

```powershell
Remove-Item data/agent.db -Force
```

重置后重启服务，`StateStore.initialize()` 和 `HistoryStore.initialize()`
会创建当前 schema。

> 只对确认可丢弃的本地开发库执行；不要对真实数据执行。

---

## 7. schema 变更检查清单

每次改状态结构时：

- [ ] 已确认没有需要保留的真实数据；
- [ ] 更新模型和当前 `CREATE TABLE`；
- [ ] 删除旧 schema 的 `ALTER` / backfill；
- [ ] 删除或改写旧 schema 测试；
- [ ] 删除 `data/agent.db` 后重启验证；
- [ ] 跑全量测试；
- [ ] 更新相关课程/设计文档。

---

## 8. 不要做的事

- 不要把旧迁移代码注释掉留在文件里；
- 不要加 `if legacy:` 分支“以防万一”；
- 不要为旧测试保留兼容路径；
- 不要让旧库静默升级；
- 不要在开发期引入半套 schema version / migration 框架。

这些都会持续占用注意力，却没有真实数据收益。

---

## 9. 当前两个已决策的技术点

1. **`StateStore` 旧表缺少 `version` 时自动 `ALTER TABLE`**
   - 决策：删除自动 ALTER；旧表直接报错并提示删库。
   - 记录：[blockers.md](lessons/blockers.md) 的「开发期 schema 策略」章节。

2. **`Goal` 旧 JSON 缺少 `id` 时 `ensure_goal_ids()` 回填**
   - 决策：删除回填；`Goal.id` 改为创建时默认生成。
   - 记录：同上。

---

## 10. 总结

```text
现在：
  schema 变 → 删库 → 重建
  旧库 → fail fast
  Goal.id → 创建时生成

以后：
  有真实数据 → 冻结 schema
  → schema_version + migration + 备份
```

当前阶段的重点是快速演进，不是兼容历史。

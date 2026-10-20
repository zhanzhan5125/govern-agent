# EvoAgent Runtime Harness 优化后完整说明

> 状态：已实现、已测试  
> Runtime schema：v2  
> 更新日期：2026-08-30  
> 适用后端：SQLite、PostgreSQL；队列支持内存 ACK 与 Redis Streams

## 1. Runtime Harness 是什么

Runtime Harness 是 EvoAgent 的确定性执行控制面。它不负责替代 Agent 推理，而是负责把一次 PR 审查约束为可暂停、可恢复、可审计、可验证的工程流程。

优化后，一次任务由以下能力共同管理：

- 有界 Workflow：节点、重试、步骤预算、时间预算和取消检查；
- Hook Pipeline：在节点、模型、工具及完成检查前后执行不可绕过的策略；
- Checkpoint：保存各节点最新可恢复状态；
- Run Journal：只追加记录完整执行事实；
- Semantic Effect Ledger：避免恢复或重复请求造成外部副作用重复执行；
- Persistent Approval：危险工具在执行前暂停，审批后从原任务继续；
- Artifact Store：大工具结果卸载为可校验句柄，避免占满模型上下文；
- Goal Gate：模型停止后独立验证任务是否真正完成；
- Metrics 与 Trace：输出可用于排障和评测的结构化指标。

核心边界是：**模型决定如何分析，Runtime 决定哪些动作允许执行、何时可以完成以及失败后如何恢复。**

## 2. 优化后的总体架构

```text
ReviewService / Queue
        │
        ▼
ReviewHarness
        │
        ├── HookPipeline
        │     ├── ToolPermissionHook  [fail-closed]
        │     ├── ApprovalHook        [pause / block]
        │     └── ArtifactOffloadHook [fail-open]
        │
        ├── AgentRuntime
        │     ├── planning
        │     ├── executing ── Role-local Model/Tool Loop
        │     ├── reviewing
        │     └── goal-gate（不消耗 Agent 步骤预算）
        │
        ├── Checkpoint ── 当前可恢复状态
        ├── Run Journal ── 不可变执行事实
        ├── Effect Ledger ── INTENT / COMMITTED / FAILED
        ├── Approval Store ── PENDING / APPROVED / DENIED
        └── Artifact Store ── SHA-256 / preview / artifact:// handle
```

确定性 Workflow 与开放 Agent Loop 保持分层：`planning → executing → reviewing → goal-gate` 的顺序由 Runtime 固定；Lead、Specialist 和 Critic 的模型/工具选择仍在 `executing` 节点内部完成。

## 3. 完整执行流程

### 3.1 启动与恢复

1. `ReviewHarness.run()` 读取任务、旧 Trace 与 Checkpoint。
2. 已为 `SUCCESS` 且存在报告的任务直接返回，不重复执行。
3. Runtime 发送 `RUN_START`，加载节点 Checkpoint。
4. 已完成节点恢复输出并发送 `CHECKPOINT_RESTORED`；未完成节点进入正常执行。
5. 旧版 Checkpoint 没有 schema v2 字段也可以继续读取，因此历史任务无需一次性迁移。

Checkpoint 与 Journal 的职责不同：

| 机制 | 回答的问题 | 是否允许覆盖 | 用途 |
|---|---|---:|---|
| Checkpoint | “从哪里继续？” | 是 | 快速恢复节点输出 |
| Run Journal | “实际发生了什么？” | 否 | 审计、回放、故障定位 |
| 旧 Trace | “业务状态如何变化？” | 追加 | Dashboard 与用户可读状态 |

### 3.2 节点执行

每个未完成节点按以下次序执行：

1. 检查取消、步骤和墙钟时间预算；
2. 生成与重试次数无关的节点语义键；
3. 执行 `NODE_BEFORE` Hook；
4. 创建观测 Span 并调用节点 Handler；
5. 执行 `NODE_AFTER` Hook；
6. 保存 Checkpoint；
7. 记录 `CHECKPOINT_SAVED` 与节点结果事件；
8. 失败时记录 `NODE_ERROR`，根据节点重试策略重试或抛出。

取消、预算耗尽、参数错误等非重试异常仍会写入 Journal，然后终止任务。Goal Gate 是安全检查，不计入 Agent 步骤预算，但仍受总时间预算约束，避免用户原有 `EVOAGENT_MAX_STEPS=3` 配置因新增节点失效。

### 3.3 模型与工具调用

`BoundedRole` 已接入：

```text
MODEL_BEFORE → LLM → MODEL_AFTER
                       └─ error → MODEL_ERROR

TOOL_BEFORE → schema revalidation → effect claim → handler → TOOL_AFTER
                                                    └─ error → TOOL_ERROR
```

`MODEL_BEFORE` 可以在不修改 Agent Loop 的情况下添加预算、输入整形或审计策略；`TOOL_BEFORE` 是权限与审批的强制边界。Hook 修改工具参数后会再次执行 Schema 校验，防止 Hook 产生不符合工具契约的参数。

Agent 工具新增三个元数据：

- `side_effect`：是否会修改外部状态；
- `requires_approval`：是否需要结构化审批；
- `version`：参与语义键计算，工具行为变更后可显式隔离旧结果。

已有四参数 `AgentTool(name, description, schema, handler)` 调用保持兼容。

### 3.4 完成检查

`reviewing` 生成报告后，Runtime 不会立即将任务标记为成功。`ReviewGoalGate` 独立检查：

- planning、executing、reviewing 三个业务 Checkpoint 均已完成；
- Diff 至少包含一个文件和一个 Added Line；
- 报告存在且 repository 与任务一致；
- Finding 必须包含规则、标题、解释、证据和合法 Added Line；
- 高风险 Finding 必须包含修复与测试建议；若已有 Finding Gate 结果，则必须为通过；
- Goal Hook 不能把失败结果篡改为成功。

检查结果为 `complete=false` 时抛出 `RuntimeGoalNotMet`，任务进入 `FAILED`，不会产生伪成功报告；成功时结果写入：

```text
report.execution.runtime_harness.goal_gate
```

并保存独立的 `goal-gate` Checkpoint。

## 4. Hook Pipeline

### 4.1 事件点

| 类型 | 事件 |
|---|---|
| Run | `RUN_START`、`RUN_STOP` |
| Node | `NODE_BEFORE`、`NODE_AFTER`、`NODE_ERROR` |
| Model | `MODEL_BEFORE`、`MODEL_AFTER`、`MODEL_ERROR` |
| Tool | `TOOL_BEFORE`、`TOOL_AFTER`、`TOOL_ERROR` |
| Recovery | `CHECKPOINT_SAVED`、`CHECKPOINT_RESTORED` |
| Completion | `GOAL_CHECK_BEFORE`、`GOAL_CHECK_AFTER` |

### 4.2 Hook 返回动作

- `continue`：不修改数据，继续执行；
- `modify`：返回完整的新 Payload；
- `block`：终止操作；
- `pause_for_approval`：写入审批状态，任务进入 `WAITING_APPROVAL`。

Hook 按 `priority → name` 稳定排序。名称必须唯一，避免启动顺序导致行为漂移。

### 4.3 失败策略

| Hook | 策略 | 原因 |
|---|---|---|
| ToolPermissionHook | fail-closed | 权限检查失败不能放行工具 |
| ApprovalHook | fail-closed | 审批系统不可用时不能执行危险操作 |
| ArtifactOffloadHook | fail-open | 只读结果卸载失败时可返回原始结果，不应误杀审查 |

fail-open 异常不会静默丢失，而是写入事件的 `degraded_hooks` 并累加 Prometheus 指标。

### 4.4 自定义 Hook

```python
from evoagent.runtime.hooks import (
    HookAction, HookPoint, HookRegistration, HookResult,
)

def block_forbidden_repository(context):
    if context.repository == "org/blocked":
        return HookResult(HookAction.BLOCK, reason="repository is blocked")
    return HookResult()

harness.hooks.register(HookRegistration(
    "blocked-repository",
    block_forbidden_repository,
    (HookPoint.RUN_START,),
    priority=5,
    fail_open=False,
))
```

## 5. Run Journal

### 5.1 数据模型

`runtime_journal` 保存：

- `task_id` 与任务内单调递增 `sequence`；
- `kind`、`node`、`step`、`attempt`；
- `semantic_key`；
- JSON Detail 与 UTC 时间。

SQLite 使用任务内序列分配锁；PostgreSQL 使用基于 task id 的事务 advisory lock，允许不同任务并发写入。数据库层禁止 UPDATE/DELETE：SQLite 通过 Trigger，PostgreSQL 通过 Trigger Function 实现，确保 Journal 不只是代码约定上的“只追加”。

任务详情 API 会返回 `runtime_journal`，因此不需要解析文本日志即可复盘执行链路。

### 5.2 语义调用键

语义键由规范化 JSON 计算：

```text
SHA-256(task_id + action + arguments + node + role + version)
```

重试 attempt 不参与计算，所以同一语义动作在重试和进程恢复后得到相同键；工具版本参与计算，避免新版工具错误复用旧结果。

## 6. 副作用幂等账本

### 6.1 状态机

```text
不存在 ──claim──> INTENT ──success──> COMMITTED
                     │
                     └─error──> FAILED ──retry──> INTENT

INTENT lease 过期 ──reclaim──> INTENT
COMMITTED replay ──> 直接返回已保存结果
```

`EffectExecutor` 先原子认领语义键，再执行操作。重复请求命中 `COMMITTED` 时不调用 Handler，直接返回保存的结果；并发 Worker 命中未过期 `INTENT` 时得到 `RuntimeEffectInProgress`。

已接入两个真实写操作：

- GitHub PR 审查评论：使用任务 marker 做远端 upsert，再使用本地 Effect Ledger 去重；
- 自动修复 Draft PR：同一任务和同一报告指纹只执行一次，重复 API 请求返回首次结果。

需要明确：任何本地账本都无法单独消除“远端已成功、本地尚未写 COMMITTED 时进程崩溃”的分布式提交间隙。因此外部 API 仍应提供 marker、幂等键或可查询的确定性资源名；GitHub 评论已经通过 marker 满足这一要求。

## 7. 持久化审批与暂停续跑

### 7.1 状态

危险 AgentTool 设置 `requires_approval=True` 后：

1. `ApprovalHook` 在 Handler 之前检查批准状态；
2. 未审批时创建 `runtime_approvals` 的 `PENDING` 记录；
3. Runtime 抛出包含结构化申请的 `RuntimeApprovalRequired`；
4. ReviewHarness 将任务置为 `WAITING_APPROVAL`，而不是错误地标记为 FAILED；
5. 管理员批准或拒绝；
6. 批准后调用 resume，从已有 Checkpoint 继续；拒绝后 Hook fail-closed 阻断。

审批状态只允许从 `PENDING` 决策一次，包含操作语义键、工具、参数、审批人、原因和时间。

### 7.2 API

查询审批：

```http
GET /v1/tasks/{task_id}/approvals
```

决策：

```http
POST /v1/tasks/{task_id}/approvals/{semantic_key}
Content-Type: application/json

{"approved": true, "reason": "reviewed by repository administrator"}
```

批准后续跑：

```http
POST /v1/tasks/{task_id}/resume
```

接口要求 `manage` 权限并执行 tenant 隔离，决策同时写入现有审计日志。

## 8. Artifact Offload

### 8.1 行为

工具结果的规范化 JSON 超过 `EVOAGENT_RUNTIME_ARTIFACT_THRESHOLD_BYTES` 时：

1. 保存完整 JSON；
2. 计算内容 SHA-256；
3. 由任务、租户、类型和内容摘要生成确定性 artifact id；
4. 相同任务中的相同结果自动去重；
5. 模型只看到 `artifact://` 句柄、预览、大小和摘要；
6. 读取时重新计算 SHA-256，内容被篡改则拒绝返回。

如果原结果符合 EvoAgent Evidence 协议，`evidence_id` 与 `tool` 会保留，只替换较大的 `output`，因此 Finding 的证据关联不会因卸载丢失。

Artifact 内容按 tenant 隔离。任务详情只返回 artifact 元数据，不把完整大结果重新塞回任务 API。

### 8.2 配置

```dotenv
EVOAGENT_RUNTIME_ARTIFACT_THRESHOLD_BYTES=32768
```

下限在 Runtime 内限制为 1024 bytes，避免误配置把所有小结果都变成数据库 Artifact。

## 9. 状态机

优化后的业务任务状态：

```text
PENDING
  └─> PLANNING
        └─> EXECUTING
              └─> REVIEWING
                    └─> SUCCESS

任一运行态 ──approval──> WAITING_APPROVAL
WAITING_APPROVAL ──resume──> 最近未完成节点
任一非终态 ──cancel──> CANCELLED
任一非终态 ──unrecoverable error──> FAILED
```

`WAITING_APPROVAL` 不进入 DLQ；异步 Worker 会正常 ACK 当前消息，批准后由 resume 重新投递。

## 10. 存储结构

本次新增四张表，SQLite 与 PostgreSQL API 对齐：

| 表 | 用途 | 核心约束 |
|---|---|---|
| `runtime_journal` | 执行事实 | `(task_id, sequence)` 唯一，数据库禁止更新/删除 |
| `runtime_effects` | 副作用账本 | `(task_id, semantic_key)` 主键，带 lease |
| `runtime_approvals` | 结构化审批 | `(task_id, semantic_key)` 主键，单次决策 |
| `runtime_artifacts` | 完整大结果 | artifact id 主键、SHA-256、tenant |

启动时自动执行 `CREATE TABLE IF NOT EXISTS` 和兼容索引/触发器创建，不需要人工运行迁移脚本。Docker 实测已在 PostgreSQL 16 中成功创建四张表。

## 11. 可观测性

新增 Prometheus 指标：

- `evoagent_runtime_events_total`；
- `evoagent_runtime_<event>_total`；
- `evoagent_runtime_hook_degradations_total`；
- `evoagent_runtime_checkpoint_restores_total`；
- `evoagent_runtime_goal_gate_passed_total` / `failed_total`；
- `evoagent_runtime_approval_pauses_total`；
- `evoagent_runtime_side_effects_committed_total` / `reused_total` / `failed_total`；
- `evoagent_runtime_artifacts_offloaded_total`；
- `evoagent_runtime_artifacts_offloaded_bytes_total`；
- `evoagent_runtime_harness_duration_seconds` summary。

报告的 `execution.runtime_harness` 同时记录 Runtime schema、Hook 列表、Journal、Checkpoint、幂等账本、Artifact 阈值与 Goal Gate 结果。

## 12. 新增和修改项

### 12.1 新增文件

| 文件 | 内容 |
|---|---|
| `evoagent/runtime/hooks.py` | Hook 事件、动作、注册表、权限与审批 Hook |
| `evoagent/runtime/journal.py` | 语义键、EffectExecutor 与副作用结果模型 |
| `evoagent/runtime/artifacts.py` | Artifact Store、完整性校验和卸载 Hook |
| `evoagent/runtime/goal_gate.py` | 独立完成检查与 `RuntimeGoalNotMet` |
| `tests/runtime/test_runtime_harness_v2.py` | Runtime v2 的 9 项专项测试 |
| `scripts/benchmarks/benchmark_runtime_harness.py` | 可重复运行的 Runtime 微基准 |
| `docs/benchmarks/runtime_harness_benchmark.json` | 本次实测原始结果 |
| 本文档 | 优化后的 Runtime Harness 完整说明 |

### 12.2 修改文件

| 文件 | 修改内容 |
|---|---|
| `evoagent/runtime/runtime.py` | AgentRuntime 接入 Hook/Journal；ToolRegistry 接入模型/工具事件、审批、Artifact 与幂等执行；新增非计步安全节点 |
| `evoagent/runtime/harness.py` | 注册默认 Hook、加入 Goal Gate、等待审批状态、Runtime 指标和报告元数据 |
| `evoagent/agents/agentic_core.py` | BoundedRole 接入 MODEL/TOOL Hook；ModeRouter 向角色工具注入任务 Runtime Context |
| `evoagent/agents/repository_tools.py` | RepositoryToolSuite 向每个角色的 ToolRegistry 传递 Hook、Store 和租户上下文 |
| `evoagent/storage/store.py` | SQLite 四张 Runtime 表、Journal 防篡改 Trigger 和完整 CRUD/claim API |
| `evoagent/storage/postgres_store.py` | PostgreSQL 对等表、advisory lock、Trigger 和 API |
| `evoagent/application/service.py` | GitHub 评论/自动修复接入 EffectExecutor；异步审批暂停不进入失败/DLQ |
| `evoagent/application/api.py` | 新增审批查询与决策接口 |
| `evoagent/core/models.py` | 新增 `WAITING_APPROVAL` 任务状态 |
| `evoagent/core/config.py`、`.env.example` | 新增 Artifact 阈值配置 |
| `README.md` | 补充 Runtime 架构与审批 API |
| `tests/application/test_advanced.py` | 将 SafeFixer 单双引号脆弱断言改为语义等价正则断言 |

## 13. 测试与故障注入

完整命令：

```powershell
.\.venv\Scripts\python.exe -m compileall -q evoagent tests scripts
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

结果：**72/72 通过**，原有 63 项回归全部保留，新增 9 项 Runtime v2 专项测试。

专项覆盖：

- Hook 稳定顺序、Payload 修改、显式阻断；
- 可选 Hook fail-open 与策略 Hook fail-closed；
- Journal 序列连续及数据库层 UPDATE 防篡改；
- 已完成 Checkpoint 恢复且节点不重复执行；
- Side Effect COMMITTED 重放不重复调用 Handler；
- FAILED Effect 可重新认领；
- 审批前 Handler 调用数为 0，批准后恢复成功；
- 128 KiB 工具结果卸载、tenant 隔离与篡改检测；
- BoundedRole 的 MODEL/TOOL Hook 事件完整；
- 非法 Added Line 的伪报告被 Goal Gate 拦截；
- 正常报告保存 goal-gate Checkpoint 与 Runtime 元数据。

## 14. 性能基准与优化效果

运行命令：

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_runtime_harness.py --runs 200
```

环境：Windows、Python 3.10.11、SQLite。本基准只测框架，不包含 LLM、GitHub、网络和业务扫描时间。原始结果位于 `docs/benchmarks/runtime_harness_benchmark.json`。

| 指标 | 结果 | 结论 |
|---|---:|---|
| 原 Checkpoint-only 三节点 P95 | 12.42 ms | 基线 |
| Hook + Journal + Checkpoint 三节点 P95 | 58.69 ms | 增加持久化审计后的绝对延迟 |
| 平均新增开销 | 47.66 ms/任务 | 约 15.89 ms/节点，低于 30 ms/节点目标 |
| P95 新增开销 | 15.42 ms/节点 | 低于 30 ms/节点目标 |
| 三节点完整恢复 P95 | 22.38 ms | 不调用节点 Handler |
| 已提交副作用重放 P95 | 0.90 ms | 200 次重放，重复 Handler 调用为 0 |
| 128 KiB Artifact 卸载 P95 | 10.83 ms | 完整结果持久化并校验 |
| 模型可见结果缩减 | 98.16% | 131072 bytes 降至 2415 bytes |
| Journal 样本 | 16 events | sequence 连续 |

解读：微基准中优化版约为旧路径的 5.16 倍，因为旧路径只写三个 Checkpoint，而新路径增加了多次同步、耐久的 SQLite Journal 写入。这个倍数不能用于推断真实 Agent 审查变慢 5 倍；真实任务通常包含秒级模型与工具调用，应关注 **47.66 ms 的绝对框架成本**。当前已达到每节点额外开销低于 30 ms 的验收目标。若未来在纯本地高吞吐任务中发现瓶颈，可进一步采用节点事务内批量事件写入，但不能以丢失崩溃前 Journal 为代价。

功能效果：

- 恢复：重复节点调用为 0；
- 外部副作用：200 次重放中的重复 Handler 调用为 0；
- 安全：未审批 Side Effect 调用为 0；
- 完成真实性：非法证据报告被 100% 拦截（专项用例）；
- 上下文：示例大结果对模型可见字节减少 98.16%；
- 兼容性：全部旧测试通过，SQLite/PostgreSQL 两套 schema 均验证成功。

## 15. 运行验证

### 15.1 本地进程

执行 `python -m evoagent` 后，实测：

- `GET /health` 返回 200；
- runtime 为 `evoagent-runtime`；
- reviewer 为 `mode-router`；
- 当前配置识别到 `qwen-plus`；
- 开启认证后，未携带 Bearer Token 的 `/metrics` 正确返回 401。

### 15.2 Docker

已执行：

```powershell
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
docker compose down
```

结果：镜像构建成功；EvoAgent、PostgreSQL 16 和 Redis 7 均启动成功，PostgreSQL/Redis 为 healthy，容器内 `/health` 返回 200；PostgreSQL 实际存在 `runtime_approvals`、`runtime_artifacts`、`runtime_effects`、`runtime_journal` 四张新表。验证后已执行 `docker compose down`，未删除持久卷。

## 16. 使用与排障

### 16.1 常用命令

```powershell
# 完整测试
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

# Runtime 专项测试
.\.venv\Scripts\python.exe -m unittest tests.runtime.test_runtime_harness_v2 -v

# 性能基准
.\.venv\Scripts\python.exe scripts\benchmark_runtime_harness.py --runs 200

# Docker 启动与关闭
docker compose up -d --build
docker compose down
```

### 16.2 常见状态

- `WAITING_APPROVAL`：查看 approvals，管理员决策后调用 resume；
- `RuntimeGoalNotMet`：查看 `GOAL_CHECK_AFTER.detail.requirements`；
- `RuntimeEffectInProgress`：同一副作用仍在 lease 内，由已有 Worker 执行；
- `runtime hook ... failed closed`：策略 Hook 自身异常，必须先恢复策略服务；
- `runtime artifact integrity check failed`：Artifact 内容与 SHA-256 不一致，禁止继续作为证据使用；
- `degraded_hooks` 非空：只读增强失败并降级，原任务继续但需要观测告警。

## 17. 已知边界

- Journal 当前采用同步耐久写，可靠性优先；极高吞吐纯本地任务可能需要批量事务优化。
- Artifact 当前保存于业务数据库，适合现有规模；大规模生产可在保持相同接口和 SHA 校验的前提下替换为对象存储。
- Goal Gate 验证确定性字段和已有 Finding Gate 结果，不使用第二个 LLM Judge；语义正确性仍由 Evaluation Harness 衡量。
- 本地 Effect Ledger 不能单独提供跨 GitHub 与本地数据库的分布式原子提交，外部写操作必须继续使用远端幂等策略。
- 当前生产 Agent 工具以只读为主；审批机制已经完备，但只有标记为 `side_effect=True`、`requires_approval=True` 的新工具才会触发等待审批。
- 性能数据是本机 SQLite 微基准，不等于线上 PostgreSQL、网络或模型端到端延迟。

## 18. 验收结论

Runtime Harness 第一项优化已经达到计划中的核心目标：统一 Hook 边界、节点/模型/工具全链路 Journal、兼容 Checkpoint 恢复、结构化审批暂停续跑、语义副作用去重、大结果句柄化、独立 Goal Gate、指标与故障注入测试均已落地。代码、SQLite、PostgreSQL、Docker 和本地进程均完成验证；性能增加已量化，并满足每节点额外 P95/平均工程开销目标所设的绝对阈值。

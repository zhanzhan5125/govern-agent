# EvoAgent 四方向优化方案

> 适用范围：Agent Runtime Harness、Agent 自进化、多 Agent 协作、上下文压缩与 Memory 管理  
> 文档定位：实施设计与验收依据，不代表文中所有目标能力已经落地  
> 评审日期：2026-08-30

## 1. 结论先行

EvoAgent 已经不是一个简单的“LLM 调接口”项目：当前代码包含有界 Agent Loop、节点级 Checkpoint、断点续跑、角色化并行审查、Critic 盲审、租户/仓库隔离记忆、Prompt 与 Skill 版本评测、Validation/Holdout 门禁、灰度发布、评测消融和配对 Bootstrap。后续优化不应推翻现有实现，而应把这些能力收束到同一条可审计的数据链路中。

建议按以下顺序实施：

1. **先加固 Runtime Harness**：统一 Hook、事件日志、幂等恢复和独立完成门禁，为其余三项提供可信执行底座。
2. **再治理上下文与 Memory**：把长输出、证据和经验拆开管理，降低成本且不牺牲高风险证据。
3. **随后升级自进化闭环**：在现有 Validation/Holdout 基础上增加候选生命周期、人工审批、影子回放和多目标门禁。
4. **最后优化多 Agent 调度**：由“固定全员执行”改为“按风险组队、按争议加审”，用消融实验决定额外 Agent 是否值得调用。

优先级与投入判断：

| 方向 | 推荐优先级 | 预估难度 | 正向作用 | 主要风险 |
|---|---:|---:|---|---|
| Runtime Harness | P0 | 中高 | 极高：提升可靠性、可恢复性和安全边界 | 事件与状态模型改造影响面较大 |
| 上下文与 Memory | P0/P1 | 中 | 高：降低 Token、减少证据丢失、积累可复用经验 | 压缩错误可能导致漏报 |
| Agent 自进化 | P1 | 高 | 高：把反馈转化为可控版本增益 | 评测偏差、数据泄漏和错误自动激活 |
| 多 Agent 协作 | P2 | 中高 | 中高：复杂 PR 提升覆盖率，简单 PR 降低成本 | Agent 增多不一定带来净收益 |

不建议近期投入模型权重训练、无限自博弈、全自动修改源码或直接引入 Milvus 等重型设施。对当前项目规模而言，Harness 层演进更快、更可控，也更容易证明收益。

## 2. 调研依据与可迁移结论

### 2.1 三篇文章

- `hook.pdf` 的核心启发是：Prompt 表达意图，Skill 固化流程，Hook 才负责不可绕过的工程边界。长结果卸载、危险操作审批和确定性补充上下文都应由框架执行，而不是提醒模型“记得做”。
- `zjh.pdf` 将自进化拆为评测、记忆、工程落地和人类控制四个齿轮。最值得迁移的是分层诊断、差量候选、Validation/Holdout 隔离、五类门禁、版本追踪、灰度与回滚，以及由低到高的自治等级。
- `zjh1.pdf` 展示了 Proposer、SkillBuilder、Verifier、候选池和 Skill/Test 协同演进。适合 EvoAgent 的是小规模多候选、结构化 Skill 包和独立验证；强化学习及无数据自博弈目前成本高、可解释性弱，不列入近期路线。

### 2.2 learn-claude-code-main

重点迁移各章节 README 中能与现有架构互补的设计：

- `s03_permission`、`s04_hooks`：把权限与横切逻辑放到 Tool 调用前后，而非散落在角色 Prompt 中。
- `s08_context_compact`：先卸载大结果，再压缩旧观察，最后才使用模型摘要；工具调用与结果必须成对保留。
- `s09_memory`：上下文压缩不等于长期记忆；长期写入需要筛选、去重、合并和来源追踪。
- `s10_task_system`、`s11_background_tasks`：任务依赖、原子认领、异步结果通知和幂等恢复应进入 Runtime 状态模型。
- `s13_agent_teams`：成员拥有隔离上下文，通过任务契约和类型化消息协作；“结果完成”和“成员空闲”是不同事件。
- `s16_workflow_runtime`：已知的 PR 治理流程应使用确定性 Workflow，开放推理留给角色内部 Agent Loop。
- `s17_goal_loop`：模型停止输出不等于任务完成，Stop 后应由独立 Gate 检查证据和完成条件。

### 2.3 EvoAgent 当前基线

| 方向 | 已有能力 | 关键缺口 |
|---|---|---|
| Runtime | 有界节点图、步骤/时间预算、重试、取消、节点级 Checkpoint、Trace | 缺少统一 Hook 管线、追加式 Journal、工具级幂等恢复、独立 Goal Gate |
| 自进化 | 失败案例回流、Prompt/Skill 版本、Validation/Holdout、非退化门禁、回滚、Prompt 影子状态 | Skill 候选仍可直接自动激活；缺统一候选状态机、人工审批、轨迹/成本门禁和多候选比较 |
| 多 Agent | Lead 动态委派、Security 与 Correctness 并行、Critic 盲审、最多两轮返工 | 拓扑基本固定；缺风险分级组队、明确任务契约、争议升级和按角色贡献度裁剪 |
| Context/Memory | 风险排序 Diff 分片、旧观察摘要、Working/Episodic/Semantic/Procedural 分层、租户仓库隔离 | 缺大结果句柄化、证据不可损压缩、混合检索、记忆版本/冲突/淘汰与晋升门禁 |

基线测试结果：本次审阅运行 `python -m unittest discover -s tests -v`，63 项中 62 项通过；唯一失败为 `test_safe_fixer_changes_only_supported_rules` 对单双引号形式做了脆弱断言。开始架构改造前应先将该测试改为 AST/语义断言，使主分支恢复全绿。

## 3. 目标架构

```text
PR / Diff
   │
   ▼
Runtime Kernel
   ├─ Hook Pipeline：权限、预算、审计、长结果卸载、审批
   ├─ Run Journal：事件序号、语义调用键、幂等副作用、Checkpoint
   └─ Goal Gate：完成条件、证据完整性、异步任务状态
   │
   ▼
Deterministic Review Workflow
   ├─ Risk Router ── 低风险：最小团队
   │               └─ 高风险/高争议：Specialist + Critic + Arbiter
   ├─ Role-local Agent Loop
   └─ Evidence Gate / Fix Gate / Test Gate
   │
   ├────────► Context & Artifact Store
   │             ├─ 原始证据与大结果句柄
   │             ├─ Working / Episodic / Semantic / Procedural Memory
   │             └─ 混合检索、版本、冲突、衰减与晋升
   │
   └────────► Evaluation & Evolution Plane
                 ├─ 失败诊断 → Prompt / Skill / Tool / Data
                 ├─ Proposer → Builder → Verifier
                 ├─ Validation → Hidden Holdout → 人工审批 → Shadow
                 └─ 激活、监控、回滚与 Playbook
```

设计原则：

- **确定性流程和开放推理解耦**：状态流转、权限、恢复、门禁由 Runtime 控制；问题识别和修复建议由 Agent 完成。
- **证据优先于摘要**：代码行、Diff 范围、CWE、工具输出哈希是事实对象，不能只保留自然语言摘要。
- **默认不自动放权**：所有会改变 Prompt、Skill、外部 PR 或安全策略的动作都经过可审计门禁。
- **用评测决定复杂度**：没有消融收益的 Agent、Memory 或压缩策略不进入默认链路。

## 4. 方向一：Agent Runtime Harness

### 4.1 优化目标

把现有“节点级可恢复执行器”升级为“事件驱动、工具级幂等、边界可插拔、结果可验证”的 Runtime Kernel。重点不是增加更多 Agent，而是保证相同输入、相同版本和相同预算下能够重放、定位和安全恢复。

### 4.2 方案 A：统一 Hook Pipeline

新增事件：

```text
RUN_START / RUN_STOP
NODE_BEFORE / NODE_AFTER / NODE_ERROR
MODEL_BEFORE / MODEL_AFTER
TOOL_BEFORE / TOOL_AFTER / TOOL_ERROR
CHECKPOINT_SAVED / RESUME
GOAL_CHECK_BEFORE / GOAL_CHECK_AFTER
```

每个 Hook 接收统一 `HookContext`：`run_id`、`task_id`、`tenant_id`、`repository`、`role`、`event`、`sequence`、`budget`、`payload_ref`、`trace_id`。Hook 返回 `pass`、`modify`、`block`、`pause_for_approval` 或 `retry`，禁止通过异常字符串隐式控制流程。

执行顺序固定为：安全权限 → 预算 → 输入整形 → 调用 → 输出卸载/校验 → 审计 → 记忆候选。对于只读检索类 Hook，异常可 fail-open 并记录降级；对于自动修复、GitHub 回写和版本激活等写操作，异常必须 fail-closed。

首批 Hook：

- `PermissionHook`：统一角色工具白名单、危险动作审批和参数约束。
- `BudgetHook`：统一步骤、Token、时间、工具调用和费用预算。
- `ArtifactOffloadHook`：长 Tool Result 写入 Artifact Store，仅向模型返回预览、句柄、哈希和读取方法。
- `AuditHook`：把输入摘要、版本、决策和副作用写入 Journal。
- `MemoryCandidateHook`：只生成待审记忆候选，不在主链路直接写入长期记忆。
- `CompletionHook`：Stop 时触发独立 Goal Gate。

### 4.3 方案 B：Run Journal 与细粒度恢复

Checkpoint 继续保存“可快速恢复的状态快照”，同时增加只追加的 `RunJournal` 保存事件事实。两者分工如下：

- Journal：回答“发生过什么”，不可原地修改。
- Checkpoint：回答“从哪里继续”，允许滚动覆盖并带 `schema_version`。
- Trace：面向观测与分析，可由 Journal 派生，不作为恢复真相源。

为模型调用、工具调用、修复分支创建等动作生成稳定语义键：

```text
sha256(task_id + node + role + action + normalized_args + artifact_version)
```

执行副作用前写 `INTENT`，完成后写 `COMMITTED`；恢复时如果已 `COMMITTED` 则复用结果，如果只有 `INTENT` 则按工具的幂等策略查询或补偿。这样可避免断点续跑时重复评论 PR、重复建分支或重复激活版本。

### 4.4 方案 C：独立 Goal Gate

模型返回 Final 只代表角色停止，不代表 PR 治理完成。新增与 Lead 隔离的确定性检查器，至少验证：

- 必选角色是否按风险策略完成，或有明确降级原因；
- 每条 Finding 是否包含有效路径、Added Line、CWE/规则、证据和置信度；
- 高风险 Finding 是否经过独立证据复核；
- 自动修复是否具备 before/after 测试证据；
- 是否仍有后台测试、审批或重试任务未结束；
- 最终报告、Trace、版本和数据集指纹是否已经持久化。

Gate 可返回 `complete`、`continue_with_requirements`、`defer_async`、`blocked`。设置全局最大补全轮数，避免“为了完成而无限循环”。

### 4.5 改造落点

建议新增：

- `evoagent/runtime/hooks.py`：Hook 接口、注册表、排序和失败策略；
- `evoagent/runtime/journal.py`：事件模型、语义键和副作用账本；
- `evoagent/runtime/artifacts.py`：长结果句柄、哈希、权限与生命周期；
- `evoagent/runtime/goal_gate.py`：完成条件及证据校验。

主要集成文件：`runtime.py`、`harness.py`、`agentic_core.py`、`store.py`、`postgres_store.py`、`service.py`。

### 4.6 验收指标

- 在模型调用后、GitHub 回写前、版本激活前分别注入故障，恢复后外部副作用重复数为 0。
- Journal 事件序号连续，关键事件完整率 100%，可由同一版本和输入复现状态流转。
- 高风险写操作未审批执行数为 0；Hook 异常符合预设 fail-open/fail-closed 策略。
- Goal Gate 对缺路径、缺证据、后台任务未完成三类伪完成均能拦截。
- Hook + Journal 在不含模型耗时的本地基准中，额外 P95 开销目标低于 30 ms/节点。

### 4.7 难度、收益与风险

- **难度：高**。涉及 Runtime、存储和外部副作用，需做数据库迁移和兼容读取。
- **收益：极高**。它是自进化安全激活、多 Agent 失败恢复和上下文卸载的共同基础。
- **风险控制**：先仅旁路记录 Journal，再切恢复真相源；Hook 按功能逐个启用；保留旧 Checkpoint 读取适配器至少一个版本周期。

## 5. 方向二：Agent 自进化

### 5.1 优化目标

把“失败反馈生成一个候选并过门禁”升级为“能诊断改哪里、能比较多个候选、能证明没有明显退化、能经人确认后灰度”的 Harness 自进化闭环。自进化对象限于 Prompt、Skill、工具描述、路由策略和 Workflow 参数，不自动修改核心源码和模型权重。

### 5.2 方案 A：先诊断，再选择进化对象

每个失败案例形成统一 `FailureSignal`：任务/仓库、基线版本、期望与实际、错误类型、轨迹片段、证据、成本、人工标签和数据来源。诊断器输出以下之一：

| 诊断 | 处理对象 | 例子 |
|---|---|---|
| 系统性推理遗漏 | Prompt / Skill | 多个仓库重复漏掉鉴权边界 |
| 单项目经验 | Semantic/Episodic Memory | 某仓库特有危险封装函数 |
| 能力缺失 | Tool | 无法定位跨文件调用链 |
| 流程错误 | Workflow/Router | Critic 在无争议任务上浪费预算 |
| 评测问题 | Dataset/Evaluator | 标注冲突或 Added Line 不合法 |

若证据不足，进入 `needs_human_label`，禁止为了形成进化样本而自动猜测根因。

### 5.3 方案 B：Proposer–Builder–Verifier 分工

- **Proposer**：基于同类失败簇、当前版本 diff 和历史 Playbook 生成 2–3 个最小候选，不接触 Hidden Holdout。
- **Builder**：将候选生成标准 artifact；Prompt 只输出差量区块，Skill 生成合法 frontmatter、正文、资源与测试清单。
- **Verifier**：静态校验后，在隔离环境运行 Replay、轨迹检查和安全检查；它不参与候选生成。

候选比较使用小型 Pareto 集，而不是只看单一综合分：风险检测 F1、高风险召回、干净 PR 准确率、平均 Token、延迟和工具调用数均进入排序。最多保留 3 个非支配候选，防止 Skill 库和版本库无界膨胀。

### 5.4 方案 C：统一候选生命周期

```text
DRAFT
  → STATIC_PASSED
  → VALIDATION_PASSED
  → HOLDOUT_PASSED
  → HUMAN_APPROVED
  → SHADOW
  → ACTIVE
  → SUPERSEDED / ROLLED_BACK

任一门禁失败 → REJECTED
```

当前 Prompt 已有 `shadow_ready` 思路，应扩展到 Skill，并禁止 Skill 评测通过后直接激活。人工审批页面只展示差量、失败案例簇、各门禁变化、成本变化和可回滚版本，降低审查负担。

自治等级建议：

- L0：只生成改进建议；
- L1：自动生成和回放，人工批准影子；
- L2：低风险路由参数可自动影子，人工批准激活；
- L3：只有连续多个版本稳定后，低风险仓库才允许自动激活；安全 Skill、写工具权限和全局规则始终不超过 L1。

### 5.5 方案 D：强化评测门禁

复用现有 Evaluation Harness 和配对 Bootstrap，而不是另建评测系统。补充：

- **数据隔离**：按仓库分 Train/Validation/Hidden Holdout；生成器只见训练失败摘要，不见 Holdout 内容与 case id。
- **结果指标**：F1、高风险召回、干净 PR 准确率、证据定位准确率、非法评论数。
- **过程指标**：是否调用目标 Skill、是否遵守工具权限、是否超预算、是否经过要求的 Evidence/Critic Gate。
- **工程指标**：每 PR Token、费用、延迟、LLM 调用数和失败率。
- **成对统计**：沿用现有 Bootstrap，候选必须达到最小效果量且 95% CI 不支持明显负向结论。
- **Skill 归因**：在相同模型、同一预算和同一案例上执行 with/without Skill 消融，证明新增 Skill 的独立贡献。
- **评测器校准**：定期抽样人工复核，记录分歧；规则匹配负责可确定项，LLM Judge 只处理语义项。

现有 100 个受控 Diff 可用于开发回归，不应单独作为“真实生产效果”的证明。对外简历指标需区分受控数据与真实公共 PR 数据，并保留数据集指纹、模型版本和运行报告。

### 5.6 方案 E：经验晋升与离线 Dreaming

失败先进入临时经验区，满足以下条件才晋升：重复出现、人工或规则验证、跨任务有效、与现有规则不冲突、收益通过回放。晋升路径为：

```text
Failure Signal → Episodic Trap → Semantic Rule → Procedural Skill
```

离线 Dreaming 只做失败聚类、冲突发现和候选建议，不直接激活。对于不同底座模型分别保存适配记录，避免把 Qwen/百炼上有效的 Skill 未经验证直接迁移到其他模型。

### 5.7 改造落点

重点修改 `evolution.py`、`skill_evolution.py`、`evaluation_harness.py`、`evaluation_v2.py`、`store.py`、`postgres_store.py` 和管理台版本页；新增 `evolution_pipeline.py`、`candidate_policy.py`、`failure_diagnosis.py`。

### 5.8 验收指标

- Prompt 与 Skill 均不能绕过审批直接从候选进入生产激活。
- Holdout 内容、标识和逐案例结果不进入候选生成上下文；泄漏测试全部通过。
- 新版本在保护指标上无超过 1 个百分点的退化，或采用项目设定的更严格阈值。
- 候选相对基线的成本/Token 增幅默认不超过 10%；超过时必须证明高风险召回带来的收益并人工批准。
- 版本可在一次操作内回滚，回滚后新任务命中旧版本且审计链完整。
- 每个激活版本可追溯到失败簇、候选 diff、数据集指纹、评测报告、审批人和影子结果。

### 5.9 难度、收益与风险

- **难度：高**。难点不是生成文字，而是防泄漏、校准评测器和证明净收益。
- **收益：高**。能够把“自进化”从演示概念转化为工程上可信的版本治理。
- **风险控制**：默认 L1 自治；小候选池；所有变更差量化；先影子后激活；安全类变更始终人工审批。

## 6. 方向三：多 Agent 协作

### 6.1 优化目标

保留现有 Lead、Security、Correctness/Reliability、Critic 的角色优势，但不让所有 PR 无差别走同样链路。目标是复杂 PR 提升覆盖率，简单 PR 减少调用，并能解释“为什么拉起这个 Agent、它贡献了什么”。

### 6.2 方案 A：Risk Router 与弹性拓扑

先用确定性特征计算风险：敏感路径、文件类型、Diff 规模、依赖/权限/配置变更、危险 API、跨文件调用、历史失败和仓库策略。Router 只决定执行拓扑，不直接裁定 Finding。

| 风险层级 | 默认拓扑 | 升级条件 |
|---|---|---|
| L0 文档/低风险 | Local Rules + Lead 快速汇总 | 命中危险模式或证据不完整 |
| L1 单域变更 | Lead + 1 个 Specialist | 低置信度、跨域或 Reviewer 争议 |
| L2 多域/敏感路径 | Lead + 2 个 Specialist 并行 | 高风险 Finding 或结果冲突 |
| L3 安全关键/自动修复 | 全角色 + Critic + Evidence Verifier + Arbiter | 必须完成全部门禁 |

Router 决策、特征和版本写入 Trace，后续用消融评测调整阈值。

### 6.3 方案 B：结构化任务契约

Lead 的委派从自由文本升级为 `AssignmentContract`：

```yaml
assignment_id: uuid
role: security
objective: 检查新增鉴权与命令执行路径
scope: [src/auth.py:40-130, src/runner.py:10-80]
required_evidence: [path, added_line, cwe, code_excerpt]
dependencies: []
budget: {steps: 4, tokens: 6000, seconds: 45}
deadline: timestamp
skill_versions: {security-review: v7}
```

各角色使用独立 ContextView，不共享可变消息列表；只通过结构化 Result 和事件总线返回。`RESULT_READY`、`WORKER_IDLE`、`RETRY_REQUESTED`、`REVISION_REQUIRED` 分开表达，避免把生命周期状态混进模型消息。

### 6.4 方案 C：争议驱动的 Critic 与 Arbiter

Critic 不再默认复查全部候选，只处理：高风险、证据不足、角色结论冲突、置信度靠近阈值、准备自动修复的 Finding。继续保留现有盲审，隐藏候选来源角色，减少迎合。

Finding 合并键由单一规则 id 扩展为：标准化路径 + Added Line 区间 + CWE/规则族 + 根因指纹。对于冲突项记录双方证据，而不是直接平均置信度。

Arbiter 先执行确定性规则：无 Added Line、证据与代码不一致、工具结果哈希失效则拒绝；明确一致则通过；只有语义争议才交给 Lead/Arbiter 模型。这样把昂贵模型调用集中在真正有分歧的样本上。

### 6.5 方案 D：失败隔离与调度约束

- Specialist 超时不拖垮全局；根据风险选择一次重试、换角色或降级并标注不完整。
- 每个 Assignment 原子认领，Checkpoint 按 Assignment 保存，恢复时只重跑未提交任务。
- 全局 Token/时间预算由 Lead 分配，角色不能自行扩容；返工最多两轮的现有限制继续保留。
- 当前审查角色主要只读，不必立即引入 Worktree。只有自动修复 Agent 并行写文件时，才为每个修复任务创建隔离 Worktree；Worktree 不是安全沙箱，命令权限仍由 Hook 控制。

### 6.6 消融与验收

复用现有 `evaluation_v2.py`，增加以下实验臂：

1. 单 Agent/本地规则基线；
2. 固定 Specialist、无 Critic；
3. 固定全角色；
4. 动态 Router；
5. 动态 Router + 争议 Critic。

在相同模型、相同案例、相同总 Token 上进行配对比较。验收建议：

- L2/L3 样本高风险召回不低于固定全角色，95% CI 不支持明显退化。
- L0/L1 平均 LLM 调用数或 Token 相比固定全角色下降至少 25%。
- 重复 Finding 率低于 5%，非法 Added Line 评论为 0。
- 高风险 Finding 独立复核覆盖率 100%。
- 任一角色失败后，任务要么有审计化降级结果，要么明确失败，不能静默输出“完整审查”。
- 记录每个角色的边际贡献：独有真阳性、消除假阳性、额外成本和延迟；连续多个周期无正贡献的角色退出默认拓扑。

### 6.7 改造落点

主要修改 `agentic_core.py`、`harness.py`、`evaluation_v2.py` 和 Context Manager；新增 `risk_router.py`、`assignments.py`、`collaboration_events.py`、`arbiter.py`。

### 6.8 难度、收益与风险

- **难度：中高**。结构化契约和动态拓扑需要重构 Lead 调度，但可逐步兼容当前固定角色。
- **收益：中高**。既提高复杂问题覆盖，也使“多 Agent”有成本收益证据，而非仅增加角色数量。
- **风险控制**：Router 先以 shadow 方式只记录建议拓扑，与固定拓扑对比；达到消融门禁后再接管真实调度。

## 7. 方向六：上下文压缩与 Memory 管理

### 7.1 优化目标

将“当前任务如何装进上下文”和“哪些经验值得长期保留”彻底分离：Context 负责本轮可用信息，Memory 负责跨任务可治理经验，Artifact Store 保存不可损的原始证据。目标不是压得越短越好，而是在固定预算下保持风险召回和证据可追溯。

### 7.2 方案 A：四级上下文压缩

按以下顺序执行，只有前一级仍超预算才进入下一级：

1. **大结果卸载**：长 Diff、测试日志、跨文件检索结果写入 Artifact Store，返回前后预览、句柄、SHA-256、大小和读取工具。
2. **确定性裁剪**：按角色、风险、Added Line、敏感路径和依赖关系选择 Hunk；现有风险排序 Map-Reduce 继续使用。
3. **旧观察微压缩**：已消费的 Tool Result 转为结构化摘要并保留句柄；最近结果和错误结果优先保留。
4. **模型摘要**：只压缩对话性内容，当前用户请求、工具调用/结果配对、候选 Finding id 和证据引用不可丢失。

为每类上下文分配硬预算，例如：系统/规则 15%、任务与 Diff 45%、工具观察 20%、Memory 10%、输出余量 10%。比例由角色和风险动态调整，但总预算不可突破。

### 7.3 方案 B：Evidence 不可损层

把自然语言 Finding 与证据对象分开：

```json
{
  "evidence_id": "ev-...",
  "artifact_ref": "artifact://sha256/...",
  "path": "src/auth.py",
  "added_lines": [72, 79],
  "excerpt_hash": "...",
  "tool": "read_diff_hunk",
  "created_by": "security",
  "verified_by": "evidence-verifier"
}
```

摘要只能引用 `evidence_id`，不能重写行号和代码内容。最终 Gate 重新解析原始 Diff 验证路径、Added Line 和哈希。Artifact 按租户/仓库授权，原始内容只读；自动修复使用可编辑工作副本，并在写回时重新校验基线哈希，防止基于过期内容覆盖。

### 7.4 方案 C：Memory 治理状态机

现有四层记忆保留，增加生命周期：

```text
PROVISIONAL → VERIFIED → PROMOTED
        └──→ REJECTED
VERIFIED/PROMOTED → SUPERSEDED / EXPIRED
```

Memory 元数据至少包括：`memory_id`、scope、tenant/repository、来源任务与证据、模型/Prompt/Skill 版本、置信度、状态、创建与复核时间、使用次数、成功/失败反馈、`supersedes`、内容哈希和乐观锁版本。

写入规则：

- Working：工具观察和中间状态，可 TTL 清理；
- Episodic：已验证的成功案例与失败陷阱，保留原始任务引用；
- Semantic：跨多个任务稳定复现的仓库知识或规则；
- Procedural：可执行流程，必须通过回放和人工审批，优先转为版本化 Skill，而不是无限堆 Prompt。

错误记忆应比正确记忆更快淘汰：一次明确反证即可降级/冻结；正向记忆需要多次独立成功才晋升。冲突时不覆盖旧记录，而是建立 `supersedes/conflicts_with` 并触发复核。

### 7.5 方案 D：混合检索与渐进披露

第一阶段不引入独立向量数据库。使用现有 SQLite/PostgreSQL 增加：

- 精确过滤：tenant、repository、role、risk domain、version、time；
- BM25/全文检索：处理规则名称、路径、CWE 和错误消息；
- 现有关键词/重要度/时效分；
- 可选 Embedding 召回：仅在本地关键词覆盖不足时启用；
- RRF 融合后取 Top-N，再由轻量 reranker 去重和冲突检查。

渐进披露分三层：默认只注入 Memory catalog；命中后加载结构化摘要；Agent 明确请求时再读取原始 Episode/Evidence。每次默认最多注入 4–6 条，记录“被检索、被采用、结果是否成功”，用于后续强化或淘汰。

Memory 内容视为不可信数据，禁止其中的“忽略规则”“调用工具”等文本改变系统权限；注入时使用明确的数据边界和来源标签。

### 7.6 方案 E：压缩保真评测

为同一批 PR 建立 Full Context 与 Compact Context 成对实验：

- Finding recall、High-risk recall、证据路径/行号准确率；
- 输入 Token、总 Token、延迟和成本；
- Artifact 二次读取次数；
- Memory 命中精度、采用率、反证率和过期命中率。

建议门禁：Compact 相对 Full 的高风险召回下降不超过 1 个百分点；高风险证据完整率 100%；平均输入 Token 降低至少 30%；过期或跨租户 Memory 泄漏为 0。若不满足，优先扩大证据预算，而不是继续压缩。

### 7.7 改造落点

主要修改 `context_manager.py`、`memory.py`、`store.py`、`postgres_store.py`、`agentic_core.py`；复用方向一的 `artifacts.py` 和 Hook。建议新增 `memory_retrieval.py`、`memory_governance.py`、`compression_eval.py`。

### 7.8 难度、收益与风险

- **难度：中**。可在不改变审查结果协议的前提下逐层替换。
- **收益：高**。能直接降低 Token 和延迟，并让“长短期记忆”从存储功能升级为可治理资产。
- **风险控制**：证据层永不摘要；压缩策略先 shadow 运行并与 Full Context 成对比较；Embedding 作为可选增强，不成为第一阶段依赖。

## 8. 跨方向实施路线图

### Phase 0：基线冻结（2–3 天）

- 修复单双引号导致的脆弱测试，确保 63/63 全绿。
- 固化当前 100 Diff 的数据集 SHA-256、模型参数、Prompt/Skill 版本和基线报告。
- 为 Runtime、Agent、Context、Memory、Evolution 建统一 ID 和 schema version。

交付：`baseline.json`、基线测试报告、数据字典、迁移/回滚说明。

### Phase 1：Runtime Kernel（1.5–2 周）

- 上线 Hook Registry，先旁路记录、后启用权限与预算拦截。
- 引入 Journal、语义调用键、副作用账本和兼容 Checkpoint。
- 接入 Artifact Offload 与 Goal Gate。
- 完成三类故障注入和恢复测试。

退出条件：重复副作用为 0，伪完成可拦截，旧任务可兼容恢复。

### Phase 2：Context 与 Memory（1–1.5 周）

- 建立 Artifact/Evidence 模型和四级压缩。
- 增加 Memory 生命周期、来源、版本、冲突与乐观锁。
- 上线 BM25/规则/关键词的混合检索与渐进披露。
- 完成 Full vs Compact 成对评测。

退出条件：高风险召回非退化、证据完整，Token 降幅达到目标。

### Phase 3：自进化治理（1.5–2 周）

- 接入 FailureSignal 诊断和 Proposer–Builder–Verifier。
- Prompt/Skill 统一候选状态机、审批与 Shadow。
- 增加轨迹、预算、成本、Skill 消融和统计门禁。
- 打通激活监控与自动回滚，但生产激活仍由人工确认。

退出条件：任一激活版本端到端可追溯、可回滚、无 Holdout 泄漏。

### Phase 4：动态多 Agent（1–1.5 周）

- Router shadow 记录推荐拓扑。
- 上线 AssignmentContract、Assignment 级 Checkpoint 和争议 Critic。
- 扩展评测臂，对比固定与动态拓扑。
- 达到收益门禁后才切换默认路由。

退出条件：复杂样本质量不退化，简单样本调用/Token 明显下降。

整体建议排期约 6–8 周；若单人业余推进，按两个迭代周期拆分更稳妥：第一迭代完成 Runtime + Context，第二迭代完成 Evolution + Dynamic Team。

## 9. 文件级改造清单

| 文件/模块 | 主要改造 |
|---|---|
| `runtime.py` | Hook 调度、Journal 事件、工具级恢复、Goal Gate 接入 |
| `harness.py` | Workflow 状态、异步 defer、完成门禁与兼容恢复 |
| `agentic_core.py` | 角色任务契约、动态拓扑、事件总线、争议 Critic |
| `context_manager.py` | 四级压缩、预算分配、Evidence 引用和保真模式 |
| `memory.py` | 生命周期、晋升/淘汰、混合召回、冲突处理 |
| `evolution.py` | 失败诊断、多候选、统一状态机、审批/Shadow |
| `skill_evolution.py` | Skill diff、Builder/Verifier、消融、禁止直接自动激活 |
| `evaluation_harness.py` | 轨迹、成本、证据和压缩保真指标 |
| `evaluation_v2.py` | 动态拓扑实验臂、角色边际贡献和统计门禁 |
| `store.py` / `postgres_store.py` | Journal、Artifact、Memory lineage、Candidate 状态迁移 |
| Web 管理台 | 审批、候选 diff、影子结果、回滚、运行回放 |

## 10. 最终验收矩阵

| 能力 | 必测场景 | 通过标准 |
|---|---|---|
| Runtime 恢复 | 模型后宕机、外部写前宕机、写后未回执 | 状态可恢复，外部副作用不重复 |
| Hook 边界 | 未审批写操作、超预算、Hook 异常 | 危险写 fail-closed，降级有审计 |
| Goal Gate | 缺证据、缺角色、异步未完成 | 不得输出完整完成态 |
| 自进化 | 提升、退化、泄漏、成本暴涨 | 正确进入候选状态且不能绕过门禁 |
| 版本治理 | 影子、激活、回滚、并发审批 | 状态合法、版本唯一、回滚可复现 |
| 动态多 Agent | 低风险、高风险、角色超时、结论冲突 | 路由合理，失败隔离，争议可追踪 |
| Context | 超长 Diff/日志、压缩后复核 | Token 降低且高风险证据不丢失 |
| Memory | 跨租户、过期、冲突、反证、晋升 | 无泄漏，状态正确，错误记忆可快速淘汰 |

## 11. 优化后简历项目介绍

> 以下是**完成上述改造并通过验收后**可使用的版本。数字指标应由固定数据集与可复现报告生成；当前没有实测依据的部分不写虚构百分比。

### 项目名称

**EvoAgent：评测驱动的自进化多智能体 PR 风险治理平台**

### 项目介绍

面向代码评审中上下文超长、协作链路不稳定及规则迭代易退化等问题，研发可恢复的 Agent Runtime Harness，贯通 PR 风险发现、证据复核、安全修复、版本评测与反馈演进，并通过确定性门禁约束模型权限和生产变更。

### 技术栈

Python / Agent Runtime Harness / Multi-Agent / Dynamic Skill / Evaluation / Redis Streams / PostgreSQL / SQLite / Docker / Prometheus

### 核心条目（建议保留 4 条）

1. **可恢复 Agent Runtime Harness**：构建 Hook 驱动的有界 Agent Loop，以 Run Journal、Checkpoint、语义幂等键和 Goal Gate 统一管理预算、重试、审批、断点续跑及副作用去重，支持执行链路回放与失败归因。
2. **风险自适应多 Agent 协作**：设计 Lead、Specialist、Critic、Evidence Verifier 与 Arbiter 的弹性编排，根据敏感路径、变更范围和结论争议动态组队；通过结构化任务契约、并行初审和证据门禁完成 Finding 校验、去重与置信度校准。
3. **门禁式 Prompt/Skill 自进化**：将误报、漏报和坏修复沉淀为失败信号，由 Proposer–Builder–Verifier 生成并回放差量候选；经 Validation、Hidden Holdout、成本/安全门禁、人工审批和 Shadow 验证后激活，保留版本追踪与一键回滚。
4. **证据保真的上下文与分层记忆**：采用大结果句柄化、风险分片和渐进式压缩控制 Token，并将 Working、Episodic、Semantic、Procedural Memory 纳入来源追踪、混合检索、冲突处理及晋升淘汰机制，保证跨任务经验可复用且高风险证据可追溯。

### LaTeX 精简版

```latex
\resumeSubheading
  {EvoAgent：评测驱动的自进化多智能体 PR 风险治理平台}{}
  {核心开发}{2026年5月 -- 2026年7月}

\item[] \small{\textbf{项目简介}：面向代码评审中上下文超长、协作链路不稳定及规则迭代易退化等问题，研发可恢复的 Agent Runtime Harness，贯通 PR 风险发现、证据复核、安全修复、版本评测与反馈演进，并通过确定性门禁约束模型权限和生产变更。}\vspace{-2pt}

\item[] \small{\textbf{技术栈}：Python / Agent Runtime Harness / Multi-Agent / Dynamic Skill / Evaluation / Redis Streams / PostgreSQL / SQLite / Docker / Prometheus}\vspace{-4pt}

\resumeItemListStart
  \resumeItem{可恢复 Agent Runtime Harness}
    {构建 Hook 驱动的有界 Agent Loop，以 Run Journal、Checkpoint、语义幂等键和 Goal Gate 统一管理预算、重试、审批、断点续跑及副作用去重，支持链路回放与失败归因。}

  \resumeItem{风险自适应多 Agent 协作}
    {设计 Lead、Specialist、Critic、Evidence Verifier 与 Arbiter 弹性编排，根据敏感路径、变更范围和结论争议动态组队，通过任务契约、并行初审和证据门禁完成 Finding 校验、去重与置信度校准。}

  \resumeItem{门禁式 Prompt/Skill 自进化}
    {将误报、漏报和坏修复沉淀为失败信号，由 Proposer--Builder--Verifier 生成并回放差量候选；经 Validation、Hidden Holdout、成本/安全门禁、人工审批和 Shadow 验证后激活，支持版本追踪与一键回滚。}

  \resumeItem{证据保真的上下文与分层记忆}
    {采用大结果句柄化、风险分片和渐进压缩控制 Token，并将 Working、Episodic、Semantic、Procedural Memory 纳入来源追踪、混合检索、冲突处理及晋升淘汰机制，保证经验可复用且证据可追溯。}
\resumeItemListEnd
```

若需要保留原有量化指标，应在重新运行固定模型、固定预算的评测后，把结果补入第 2、3 或第 4 条；不要把受控合成集指标表述成真实线上 PR 效果。

## 12. 最终建议

这四项都值得优化，但其价值不在于堆叠“多 Agent、自进化、Memory”等名词，而在于形成一条可证明的闭环：Runtime 产生可信事件，Context 保留完整证据，Memory 沉淀经过验证的经验，Evaluation 决定候选能否晋升，多 Agent 仅在消融证明有效时增加复杂度。完成这条链路后，项目的核心卖点将从“功能较多的 PR 审查 Agent”升级为“可恢复、可治理、可评测、可演进的 Agent 工程平台”。

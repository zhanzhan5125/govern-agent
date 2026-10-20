# 自进化治理优化实施记录

开始日期：2026-09-18。状态：实施中，尚未完成闭环验收。

依据：[四方向优化方案](<EvoAgent_四方向优化方案.md>)第 5 章及 Phase 3。

## 目标和验收范围

本次覆盖 Prompt 与 Skill 两条进化链：失败诊断、少量差量候选、统一状态机、独立回放、结果/过程/成本门禁、人工审批、影子运行、激活监控、回滚，以及管理台与文档。原计划的完整范围保持不变，以下进度不能解释为整个优化已经完成。

| 要求 | 实现与证据 | 当前状态 |
|---|---|---|
| 候选状态机、租户隔离、版本和去重 | `candidate_store.py`、`evolution_pipeline.py`；SQLite 与 HTTP 专项测试 | 已接入服务及 API |
| 审计历史不可修改、并发更新拒绝 | 数据库触发器、CAS；跨 Store 并发测试 | SQLite、Docker PostgreSQL 实测通过 |
| 多目标门禁、缺失测量拒绝 | `candidate_policy.py`、`evolution_replay.py`；质量/成本/缺失值/NaN 测试 | 已接入回放；尚无真实生产数据收益证明 |
| 成对统计和小型 Pareto 候选池 | 同数据配对 utility bootstrap、最多三个非支配候选 | 已接入流程、比较 API 和管理台；浏览器请求验证通过 |
| 失败诊断与 Holdout 排除 | `failure_diagnosis.py`，基于 tenant 任务、Diff 和反馈 | 已集成并测试；Validation/Holdout/Shadow 均排除出生成输入 |
| Proposer–Builder–Verifier | `candidate_builder.py`，限定输入、差量和候选数量 | Prompt/Skill 生成已接入；更广泛结构化策略仍需核对计划 |
| Prompt/Skill 真实回放和过程测量 | 复用产品角色执行链，Skill with/without 消融 | 已实现并有受控测试，真实标注 PR 基准待配置 |
| 统一审批、Shadow、激活和回滚 | 新状态机控制 API，旧直接激活入口拒绝执行 | HTTP 阶段越过测试、原子发布和首版本回滚通过 |
| 管理台/API | 展示差量、来源、门禁、审批、影子结果和回滚点 | 已实现；真实 HTTP 权限测试及模拟 API 浏览器交互验证通过 |
| Memory 反馈和经验晋升 | 实际读取关联任务、人工反馈去重、负反馈优先、冲突阻断晋升 | 使用反馈已集成并双库验证；完整回放晋升链待完成 |
| 故障注入、全量回归、量化报告 | 单元、集成、数据库与受控端到端验证 | 进行中 |
| 完整优化说明与项目文档同步 | 完成后记录功能边界、实测结果和新增问答 | 待完成 |

## 当前实现

新增四张跨 SQLite/PostgreSQL 共用逻辑的表：`evolution_candidates`、`evolution_candidate_events`、`evolution_release_heads`、`evolution_release_outcomes`。候选内容、基线、策略和来源分别保存；内容与基线包含 SHA-256，候选阶段由 revision 乐观锁保护。同一租户内相同内容、基线、策略和来源重复提交时返回既有候选；不同基线允许重新评测。

事件表通过数据库触发器禁止修改和删除。候选状态更新与事件追加在同一事务内执行。`ACTIVE`、`SUPERSEDED`、`ROLLED_BACK` 不能通过普通阶段迁移写入，由发布事务原子更新候选、旧活动版本、活动版本指针和审计历史。发布使用 generation 与 revision 双重并发校验；首次发布保存评测时的原配置，作为可恢复的基线版本。

门禁要求 F1、高风险召回、干净 PR 准确率、证据准确率和成功率不退化；Validation 还要求最小提升。Token、成本、耗时、工具调用增幅默认不超过 10%；缺失测量、NaN、无限值、越权、预算违规和遗漏必需 Gate 均拒绝。

Bootstrap 统计量明确为成对案例 utility 均值差的 95% 区间，不将其误称为 F1 置信区间。统计结果不能独立替代最小效果量、Holdout 或数据来源门禁。

## 已执行测试

```powershell
.\.venv\Scripts\python.exe -m unittest tests.evolution.test_evolution_governance -v
```

2026-09-18 本轮核验：全量发现 104 项测试，101 项通过，3 项 PostgreSQL 测试因本机未配置专用测试 URL 而跳过；随后在 Docker PostgreSQL 中独立运行这 3 项，全部通过。此后新增 6 项发布监控测试并单独运行，全部通过；最新全量结果见下方续记。测试使用隔离临时数据库或独立临时 schema，不清理业务表。

旧 `EvolutionEngine`、`SkillEvolutionEngine` 的评测结果仅进入 `shadow_ready`，不直接自动激活；旧版本激活接口拒绝执行。`tests/evolution/test_evolution_api.py` 通过真实 HTTP 请求验证伪造报告无效、跳过阶段被拒、旧直接激活失败、普通账号拒绝、跨租户不可见和 revision 冲突。

### 发布监控补测与修复

```powershell
.\.venv\Scripts\python.exe -m unittest tests.evolution.test_evolution_monitor -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

`tests/evolution/test_evolution_monitor.py` 新增六项测试：

1. 首次发布后失败率超阈值，恢复归档的原始配置；重复反馈不重复计数、不反复回滚。
2. 相同任务负反馈优先，后来的正反馈不能覆盖；执行结果和人工反馈分别统计。
3. 旧任务延迟反馈不能回滚后来发布的新版本。
4. 满足最小样本量且严格超过阈值才触发回滚。
5. 跨租户反馈被拒，没有版本快照的任务不产生发布统计。
6. 用户主动取消不应视为版本执行失败：修复 `ReviewService._run_review`，将 `RuntimeCancelled` 与等待审批同样排除出失败率统计。

这里验证的是治理机制，不是模型在真实 PR 上的准确率。发布事务测试中构造的门禁报告只用于隔离测试，不能充当生产发布证据。

### 最新全量回归续记

加入监控测试后，首轮全量回归发现 Windows 临时数据库清理竞争：异步任务已写入 `SUCCESS`，但工作线程还在执行发布监控，测试便删除了数据库。为 `TaskQueue.close` 增加可选 `wait` 参数（默认行为保持不变），该异步测试使用 `close(wait=True)` 等待工作线程退出后再清理资源；不通过忽略文件删除错误掩盖问题。

修复后运行 `python -m unittest discover -s tests -q`：110 项，107 项通过，3 项 PostgreSQL 项按环境跳过，用时 16.329 秒。前述 PostgreSQL 专项已在 Docker 中单独全部通过。测试输出仍有重复注册 TracerProvider 的告警，未将告警描述为已解决。

### 尚未完成的验收

- Memory 从临时经验到语义规则/Skill 的完整回放晋升闭环（实际使用反馈已接入）。
- 评测租约、PostgreSQL 租约实测及阶段中断恢复已验证；阶段内外部调用重复计费窗口保留明确限制。
- 完整计划中结构化候选范围与实现的一致性核对。
- 可复现量化报告与完整项目文档逐章同步；管理台浏览器交互和部署配置说明已补齐。
- 真实标注数据及独立 Shadow 数据的效果验证。未配置时必须保持发布阻断，不把受控样本冒充生产收益。

## Memory 实际使用反馈补充（2026-09-18）

新增 `memory_experience_store.py`，由两个 SQL Store 共用事务逻辑；新增 `agent_memory_uses` 与 `agent_memory_task_outcomes` 两张表。

- Catalog 检索只说明记忆被展示，不能增加成功次数。角色工具 `read_memory` 改为调用 `MemoryManager.read_for_task`，核对任务、租户、仓库、记忆状态和有效期后，记录实际披露的记忆版本与内容哈希。
- `ReviewService.record_feedback` 将已完成任务的人工反馈关联到其读过的记忆；任务正常执行完成本身不等于记忆内容正确，因此不会自动奖励记忆。
- 同一任务/记忆只计一次；人工反馈由正改负时扣回原成功计数并增加失败计数，后续正反馈不覆盖负反馈。计数与反馈记录在同一事务中提交；PostgreSQL 使用行锁防止并发重复计数。
- 内容哈希已改变的记忆不承接旧内容的反馈。未读过的记忆、跨租户任务、未完成任务不获得成功统计。
- 有冲突的记忆不能转为 `PROMOTED`；负反馈沿用原有晋升阻断逻辑。这不等于已完成“规则/人工核验 + 跨任务复用 + 回放收益 + Skill 候选”的全部晋升流程，也不声称读过记忆就证明了因果贡献。

新增六项共享测试，SQLite 和 PostgreSQL 均覆盖：召回与实际读取分离、重复反馈与跨任务计数、并发去重、租户/仓库/状态/过期检查、冲突晋升拦截、真实服务反馈方法接入。PostgreSQL 运行这些测试时发现旧召回计数方法错误调用 `Connection.executemany`，已改为 `Cursor.executemany` 并通过实测。

执行记录：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.memory.test_memory_experience -v
docker run --rm --network evoagent_default --mount 'type=bind,source=D:\PythonProject\EvoAgent,target=/workspace,readonly' --workdir /workspace -e 'EVOAGENT_TEST_POSTGRES_URL=postgresql://evoagent:evoagent-local@postgres:5432/evoagent' evoagent-evoagent:latest python -m unittest tests.evolution.test_evolution_postgres -v
```

结果：Memory 专项 6 项全部通过；Docker PostgreSQL 专项 9 项全部通过（包含上述 6 项共享契约及原有 3 项发布治理测试）。上述数据库账号为本地 Compose 测试配置，不适用于生产环境；每项测试建立并清理自己的隔离 schema。

## 评测租约与旧结果隔离（2026-09-19）

新增 `evolution_replay_leases` 表和 `evolution_lease.py`，对 `evaluate`、`shadow` 使用数据库级单候选认领，不依赖单进程 Python 锁。

- 同一候选的第二个评测请求立即返回冲突，不先调用模型再等待状态更新失败。
- 默认租约 60 秒，每 20 秒续租；租约时间由数据库提供，避免不同应用进程时钟偏移影响所有权判断。
- 异常退出会释放当前持有者的租约；进程崩溃则在过期后允许新请求接管。
- 每阶段启动前重新检查租约和候选 revision。阶段报告提交时，在同一个事务内锁定并校验租约持有者、期限，再执行候选 CAS 和审计追加。
- 旧持有者不能续租、提交结果或删除接管者的新租约。Validation 已持久化后中断，可以使用最新 revision 继续 Holdout，不必重做已经提交的 Validation。

边界：这不是外部模型计费的 exactly-once 保证。数据库断连或进程失联时，已发出的模型请求可能仍在服务端执行；租约接管能够阻止旧报告落库，不能撤销远端已完成的计费。阶段内未提交结果仍可能重算，后续需要明确的调用级检查点和供应商幂等支持才能进一步缩小该窗口。

SQLite 租约专项验证并发认领唯一、过期接管与旧 token 拒绝、异常释放、租户与 revision 校验。另加真实 Pipeline 并发测试，使用两个独立 Store 实例和阻塞回放，断言并发重复请求被拒且回放只调用一次。

本轮先运行 28 项治理/API/租约测试全部通过，随后全量运行 128 项：116 项通过、12 项 PostgreSQL 项按环境跳过（18.833 秒）；之后增加 Pipeline 并发测试并单独通过。最终全量结果需以本节后续记录为准。2026-09-19 Docker 引擎连接暂不可用，因此不能用前一天的 9 项 PostgreSQL 结果代替新增租约测试的验证。

最终全量续记：129 项中 117 项通过、12 项 PostgreSQL 环境项跳过，用时 19.922 秒。启动 Docker Desktop 后已确认界面/后端进程存在，但 Linux Engine 管道仍不可连接；新增 PostgreSQL 租约验证保持待完成，未虚报通过。

## 部署与恢复验证补充（2026-09-19）

Docker 引擎后续恢复，重新运行 `tests.evolution.test_evolution_postgres`：12 项全部通过（4.817 秒），包括新增租约并发认领、旧 token 拒绝与过期接管，不再沿用先前的待验证状态。

新增 Pipeline 故障注入：Validation 成功后在 Holdout 比较处抛出异常，确认候选保留 `VALIDATION_PASSED`；用最新 revision 重试，只执行 Holdout 并到达 `HOLDOUT_PASSED`。专项测试通过。

运行身份补入模型输入/输出单价，避免更换计价口径后复用旧候选的费用报告。

最新全量回归：130 项中 118 项通过、12 项 PostgreSQL 项按环境跳过（18.990 秒）；这 12 项已在本轮 Docker PostgreSQL 中单独全部通过。Compose 语法检查通过。完整版项目文档已先更新路线状态、受控样本边界和“自动激活”核心卖点，历史代码/问答的逐章同步仍未声称完成；原图片与 PDF 页码标记未作删除。

新增 [自进化治理使用说明](<Self_Evolution_治理使用说明.md>)，覆盖 JSONL 字段、数据分区、API 操作顺序、revision/generation、监控与已知限制。`.env.example` 补入两个评测文件路径，Compose 只读挂载 `evolution-data/`，该目录不自带生产样本；`.gitignore` 忽略其中 JSONL，避免意外提交私有 PR 数据。`docker compose config --quiet` 检查通过，未读取或展示用户 `.env` 密钥。README 中旧版本直接激活描述已更新为治理接口。

## 管理台浏览器验证（2026-09-19）

修复自动生成按钮始终提交 Prompt/`llm-review` 的错误：现在读取表单选中的类型与名称，Skill 使用 `/v1/skill-evolution/auto`。候选列表从最近 5 条扩大为最近 50 条，页面明确显示范围；新增 1–3 个候选勾选比较，服务端继续校验同类型/名称/基线。复选框使用现有 `.check` 样式，避免被通用输入框样式撑开。

新增 `tests/ui/evolution_ui.cjs`，用真实 Edge 无头浏览器加载项目原始 HTML/CSS/JS，通过隔离的模拟 API 验证交互，不连接用户业务库，不调用模型，不真实激活版本。覆盖：

- Prompt/Skill 自动生成路由和名称传递。
- 手动候选创建的内容结构；比较操作的候选 ID、数量上下限。
- 缺少原因不发送审批请求；evaluate/approve/shadow/activate 的 revision 请求字段。
- 回滚先读取发布头，再发送当前 generation；审计结果显示。
- 不可信候选名称的 HTML 转义；401 返回登录页；浏览器无未处理脚本异常。

运行方式（需 Node.js、Playwright 及浏览器；Windows 默认使用已安装 Edge）：

```powershell
# Playwright 在普通 node_modules 或 NODE_PATH 中可解析时：
node tests/ui/evolution_ui.cjs
```

本机使用已有 Codex Node/Playwright 依赖运行通过，未下载新的浏览器。390px 移动端全页截图已生成并检查为 `artifacts/evolution-ui-mobile.png`。这验证了前端行为，不替代后端真实 HTTP、数据库和模型效果测试。

同轮 Python 全量回归：130 项中 118 项通过、12 项 PostgreSQL 环境项跳过（18.465 秒）；这些 PostgreSQL 专项在前一轮已单独验证。本轮仅修改管理台及浏览器测试，未将模拟 API 当作生产效果证明。

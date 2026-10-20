# EvoAgent 自进化治理使用说明

更新日期：2026-09-19。本文描述当前可运行的 Prompt/Skill 候选治理路径；完整优化仍处于验收阶段，剩余工作见 [实施记录](<Self_Evolution_优化实施记录.md>)。不将测试通过等同于真实 PR 检测效果提升。

## 1. 工作流与边界

```text
失败反馈 → 证据核对/分类 → 少量差量候选 → 静态校验
  → Validation → Hidden Holdout → 人工审批 → 独立 Shadow
  → 激活 → 反馈监控 → 必要时回滚
```

Prompt 和 Skill 共用候选状态机：`DRAFT → STATIC_PASSED → VALIDATION_PASSED → HOLDOUT_PASSED → HUMAN_APPROVED → SHADOW → ACTIVE`。旧活动版本转为 `SUPERSEDED`，被回滚替换的版本转为 `ROLLED_BACK`；门禁不通过进入 `REJECTED`。

调用方只提交内容和操作意图，不能提交可信评测报告、审批身份或强制状态。旧数字版本直接激活接口已拒绝操作。人工批准并不等于立即激活，仍须经过 Shadow 和数据来源门禁。

## 2. 配置评测数据

在 `evolution-data/` 中准备两份 JSONL 文件：

- `core.jsonl`：包含 `train`、`validation`、`holdout` 三种 split。
- `shadow.jsonl`：独立影子数据。当前沿用通用 JSONL 校验器，文件中 `split` 仍填写 `train`、`validation` 或 `holdout` 之一；加载此文件后统一归入独立 `shadow` 阶段。

所有案例 ID 必须唯一；阶段之间仓库名与完整 Diff 哈希都不能重合。不能仅修改 ID 或仓库名，将相同 Diff 复制到另一个阶段。

单行结构示例（仅解释格式，不能作为生产证据）：

```json
{"id":"format-example","repository":"example/repo","pull_request":1,"split":"train","diff":"--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+eval(user_input)\n","expected_findings":[{"path":"app.py","start_line":1,"end_line":1,"cwe":"CWE-95","severity":"high","should_comment":true}],"source":{"kind":"controlled-example"}}
```

干净案例使用空 `expected_findings` 数组。真实数据必须由维护者核实来源和标注；单纯把 `source.kind` 改成真实来源名称不能证明其真实性。

当前 `production_ready` 的程序检查要求：核心集至少 300 条、四个阶段均非空、来源种类为 `public-github-pr` 或 `private-historical-pr`，且标签包含 `should_comment`。这是最低结构检查，不是数据真实性认证，也不是“达到 300 条就保证有效”。受控数据可以开发回归，但不能授权生产激活。

Docker 使用 `.env`：

```dotenv
EVOAGENT_EVOLUTION_DATASET_PATH=/app/evolution-data/core.jsonl
EVOAGENT_EVOLUTION_SHADOW_DATASET_PATH=/app/evolution-data/shadow.jsonl
```

Compose 已只读挂载 `./evolution-data`。本地 PyCharm 运行时改为真实本机路径，例如 `D:/PythonProject/EvoAgent/evolution-data/core.jsonl`。未准备数据时保持两个变量为空；候选内容仍可提交，评测/发布不能绕过就绪检查。文件不存在或数据结构不合法会在服务初始化时报告错误。

模型配置沿用现有项目配置；不能只配置评测文件而不配置模型。费用按 `EVOAGENT_LLM_INPUT_COST_PER_MILLION`、`EVOAGENT_LLM_OUTPUT_COST_PER_MILLION` 计算，零费率只代表未计价或确实免费，不能据此声称费用降低。模型、服务地址、预算和计价参数纳入候选运行身份，变更后应重新提交评测。

修改配置后执行：

```powershell
cd D:\PythonProject\EvoAgent
docker compose up -d
docker compose logs --tail 100 evoagent
```

容器尚无镜像时仍会触发构建；需先确保 Docker Desktop 引擎运行及镜像网络可用。本说明不要求删除数据库卷。

## 3. API 操作顺序

管理台“演进实验室”可先选择类型、填写名称，再从反馈生成对应 Prompt/Skill 候选；手动 Skill 内容须包含完整 frontmatter。候选区域显示最近 50 条记录，勾选 1–3 个同基线候选后可比较 Validation 结果。操作原因输入框供审批、拒绝、激活、回滚共用；每项操作仍由后端校验权限、阶段和版本。

所有候选管理接口要求具有 `manage` 权限的租户账号，沿用登录获取的 Bearer Token。

| 步骤 | 请求 | 说明 |
|---|---|---|
| 查询就绪状态 | `GET /v1/evolution/status` | 模型、数据指纹、样本数及策略 |
| 创建候选 | `POST /v1/evolution/candidates` | 提交 `kind`、`name`、`content` |
| 读取候选 | `GET /v1/evolution/candidates/{id}` | 内容、差量、报告、历史和发布头 |
| 运行评测 | `POST /v1/evolution/candidates/{id}/evaluate` | 顺序执行 Validation 与 Holdout |
| 比较候选 | `POST /v1/evolution/compare` | 提交 1–3 个 `candidate_ids`，按 Validation 比较 |
| 人工批准 | `POST /v1/evolution/candidates/{id}/approve` | 需 Holdout 通过并填写原因 |
| 独立影子回放 | `POST /v1/evolution/candidates/{id}/shadow` | 只读运行，不修改活动版本 |
| 激活 | `POST /v1/evolution/candidates/{id}/activate` | 检查完整报告和基线后原子发布 |
| 拒绝 | `POST /v1/evolution/candidates/{id}/reject` | 填写拒绝原因 |
| 回滚 | `POST /v1/evolution/candidates/{旧版本id}/rollback` | 恢复曾发布的版本或归档原配置 |

Prompt 创建请求示例：

```json
{"kind":"prompt","name":"llm-review","content":{"prompt":"Review diff evidence. Return JSON findings with severity, fix and test recommendations."}}
```

以上只演示格式，不意味着该 Prompt 已经改进产品。Skill 的 `content` 为包含 `name` 与 `skill_md` 的合法 artifact，可附加项目支持的资源文件；需通过 frontmatter、名称、路径及安全校验。

每次阶段操作传入最新返回值中的 `expected_revision`。审批、拒绝、激活和回滚还需 `reason`。回滚额外传当前发布头的 `expected_generation`，不是旧版本历史里的 generation：

```json
{"expected_revision":4,"reason":"已核对候选差量及门禁报告"}
```

示例 revision 不能直接套用。`409` 表示版本变化或已有评测在运行，应重新读取状态；`403` 为权限不足；`404` 包括当前租户不可见的候选。不要通过修改数据库状态跳过门禁。

## 4. 评测与恢复

结果、过程和工程指标同时检查：F1、高风险召回、干净 PR 准确率、证据定位、成功率、权限/预算违规、必需 Gate、Token、费用、耗时和工具调用。默认成本类增幅上限 10%，保护指标默认不允许退化；缺失测量或非有限数值拒绝通过。

统计区间是成对案例 utility 均值差的 bootstrap 区间，不是 F1 置信区间。Skill 额外运行同条件 with/without 消融，并检查实际角色是否选中 Skill。Holdout/Shadow 不参与候选池排序；生成器不得读取保护集内容或逐案例结果。

同候选评测使用数据库租约，默认 60 秒到期、每 20 秒续租。旧持有者的结果不能在接管后提交。已持久化的 Validation 可用于从 Holdout 继续；阶段内部中断仍可能重算。远端模型请求无法靠数据库事务撤销，不能保证外部计费 exactly-once。

## 5. 发布监控与 Memory

新任务保存活动 Prompt/Skill 快照，恢复旧任务沿用原快照。发布操作同时更新候选、原活动版本、版本头和审计历史；首次发布会归档实际评测的原配置，作为回滚目标。

监控分别统计执行结果与人工反馈，按任务去重且负反馈优先。只有样本量足够且失败率严格超过阈值，才尝试回滚当前发布头；旧任务的延迟反馈不能撤销后续新版本。主动取消和等待审批不计为版本执行失败。

Memory 仅在角色实际 `read_memory` 后关联任务反馈，单纯检索到不奖励成功次数；跨租户/仓库、过期及不可召回状态拒绝读取。有冲突或负反馈的记忆不能晋升。当前使用反馈已接入，但完整的“临时经验 → 语义规则 → 回放验证 → Procedural Skill”仍待验收，不能将读取和接受反馈当作因果收益证明。

## 6. 验证与仍需完成的事项

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用受控模型适配器与隔离数据库，不消耗用户模型额度。PostgreSQL 专项需提供 `EVOAGENT_TEST_POSTGRES_URL`，每项测试创建独立临时 schema，不能直接把跳过当作通过。具体命令和本轮结果见 [实施记录](<Self_Evolution_优化实施记录.md>)。

仍需完成：完整经验晋升回放、计划中结构化候选范围核对、真实标注数据收益报告与完整版项目文档逐章同步。管理台已通过真实浏览器加载原始页面、模拟 API 的交互测试，相关范围见实施记录；这不等于真实模型发布验收。本文是当前使用入口，不替代剩余验收。

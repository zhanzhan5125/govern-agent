# Finding 输出契约

本文件是程序维护的公共输出契约，不参与策略自进化。阶段 Skill 不能覆盖字段含义、证据要求或授权边界。
各阶段返回 JSON，不返回整篇 Markdown。最终报告由程序按下列八项内容统一渲染。
Mapper 返回需求映射，不生成 Finding；其余阶段遵守各自输出要求，不越权执行验证。

## 八项报告内容与字段

| 报告项 | JSON 字段 | 内容要求 |
| --- | --- | --- |
| 风险等级 | `business_priority`, `severity`, `priority_reason`, `priority_basis`, `impact_kind` | P0/P1/P2 是业务影响等级；critical/high/medium/low 是独立技术严重性，不能机械换算。说明触发条件与分级依据。业务依据不足时 `business_priority: null`，显示待确认，不默认填 P2。 |
| 来源模块 | `source_module` | 被影响的实际业务模块或仓库目录，例如作业提交模块。不得用 correctness/security 等审查角色冒充模块；角色另存 `source_stage`。无法确认业务名称时使用真实目录并说明限制。 |
| 关联测试用例 | `related_tests` | 区分实际存在的测试、建议新增的回归测试、已经执行的验证。给出用例位置/标识、前置条件、操作和预期断言；没有找到测试就返回空数组并记录限制，不编造测试路径。 |
| 风险位置 | `path`, `line`, `side` | 冻结快照中的仓库相对路径、从 1 开始的行号及 base/head；删除代码用 base。位置应是根因或变更锚点，关联调用位置放入证据/调用链，不只给笼统模块名。 |
| 问题描述 | `title`, `description` | 简短标题；正文说明前置条件 → 具体代码行为 → 违反的需求/契约 → 实际后果，并解释为何由本次变更引入或触发。区分观测事实和推断，不只写“可能有风险”。 |
| 影响范围 | `impact_scope` | 受影响的用户/角色、接口或链路、输入条件及边界；区分直接影响与有证据支持的传播影响。无业务画像或需求依据时不得推断为核心链路、全量用户、资损。 |
| 修复建议 | `recommendation` | 对应根因的最小修复方向、需保留的兼容性/事务/权限约束，以及建议回归断言。建议不等于已经修复；不自动修改目标仓库。 |
| 证据依据 | `evidence_refs` | 引用冻结代码、已选需求/验收条件、已有测试及验证 Artifact。保留可定位引用；解释因果关系，可附真实调用链。没有执行不能声称已复现，空证据不能作为有效结论。 |

## 必填字段与可追溯信息

每个 Finding 必须包含 `title`, `rule_id`, `severity`, `business_priority`, `priority_reason`,
`priority_basis`（数组）, `impact_kind`, `source_module`, `related_tests`（数组）, `path`,
`line`, `side`, `description`, `impact_scope`, `recommendation`, `evidence_refs`（数组）,
`category`, `confidence`。`confidence` 在 0..1 之间，不能替代证据或风险等级。
无法确认的内容显式写入 `limitations`（数组），不得用猜测填补。

`finding_id`, `source_stage`, `slice_id` 由运行时补齐或保留；综合阶段保留原始 Finding ID
与来源，不将不同根因仅因位置相同而合并。`rule_id` 用于规则归因，不当作证据。
`call_chain` 可选，为带真实路径/行号/关系的数组；检索到的同名符号不自动等于调用关系。

业务优先级证据 `priority_basis` 的元素只能使用以下形式：

- `{ "source": "requirement", "id": "本次已选需求或验收条件ID" }`。
- `{ "source": "business_profile", "path": "core_flows/0" }`，path 是不带起始斜线的画像路径。
- `{ "source": "code", "path": "真实相对路径", "line": 1 }`。

仅代码依据只适用于 `unauthorized_access`, `security_incident`, `data_loss`；
核心业务影响必须有本次需求或业务画像支撑，最终由程序门禁核对，不因模型声明就成立。

## 测试与证据的表达

新增结构化 `related_tests` 元素建议采用下列形式（现有字符串条目仅作兼容描述，不代表已执行）：

```json
{
  "kind": "existing",
  "path": "tests/test_submission.py",
  "test_id": "test_resubmission_invalidates_grade",
  "scenario": "提交v1、批阅v1后重提交v2",
  "expected": "v1评分不能作为v2最终成绩",
  "execution_status": "not_run",
  "evidence_refs": []
}
```

这是格式示例，不表示该文件或测试实际存在。`kind` 为 `existing` 或 `proposed`；
建议新增用例使用 `proposed`、未知路径/标识为 null、`execution_status: not_run`。
实际执行状态使用 `not_run/passed/failed/not_collected`，passed/failed 必须有对应的
验证结果 Artifact，且匹配快照、版本和测试；测试名称、源码存在或模型预测均不算执行证据。

`evidence_refs` 引用应包含来源种类及可解析标识：代码用 snapshot_id/path/line/side；
需求用 requirement_id/criterion_id；执行结果用真实 Artifact 引用。
直接代码片段和 `call_chain` 是引用的补充，不能替代可定位的冻结证据。

## 阶段职责与核验结论

- 三领域 Reviewer：输出候选 Finding、触发条件、证据、测试关联和覆盖限制，不声称执行过测试。
- Synthesizer：按根因去重，保留证据与测试状态，统一影响和优先级，不新增无来源问题。
- Validator：独立检查位置、变更因果、需求关联与证据；按授权使用运行验证工具。

最终附加 `static_verdict`, `runtime_verdict`, `verification_refs`, `limitations`。
`runtime_verdict` 为 `reproduced/not_reproduced/inconclusive/not_applicable`。
未复现不等于误报，环境失败不等于复现，零测试收集不等于测试通过。
模拟实验、mock 边界、base/head 差异及无法验证的条件必须明确说明。

## 旧版字段兼容

保留旧版的解释、直接证据、调用链、修复与建议测试的价值，但新生成结果使用统一字段：
`explanation` → `description`，`fix` → `recommendation`；`evidence` 作为直接证据补充；
`test` 仅作为建议补测内容，不能自动转为已有测试或运行通过。
读取历史报告时允许上述兼容映射；不要要求模型同时输出两套同义字段。

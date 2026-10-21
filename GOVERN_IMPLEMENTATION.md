# Govern Agent：实现、启动与验收边界

## 2026-10-20 可运行控制台更新

本机控制台已由Vue 3 + Vue Router + Vite实现，构建产物在`web/app`，Python 8080端口同源提供前后端，不需要再访问HTML文件或额外的5173服务。

```powershell
cd 'D:\harness code\evoAgent'
python scripts/start_govern.py
```

已存在配置保存在Git忽略的`.govern/config.json`，业务任务库在`.govern/govern.sqlite`。此配置包含明文凭证，仅用于本机；勿提交、分享或上传。启动器检查Node版本，当前Windows机器可使用已存在的新版Node。日志在`.govern/logs/`；当前服务后台运行，仅绑定127.0.0.1。

首次导入旧项目模型可显式使用`--model-env <原.env路径>`；仅支持本项目已有的API/APIKEY/MODEL或EVOAGENT_LLM变量，不扫描其他应用凭证。不再要求用户猜环境变量；后续直接在“连接与设置”保存、测试即可。

浏览器已实际验收七页、仓库读取、本地/PR条件输入、真实空状态、策略片段、固定评测集未就绪提示、Jira缺凭证错误及外发确认。进度基于持久化Stage/工具/流式字符事件，不模拟百分比。

增加“本地预览（不调用模型）”：读取真实diff、两侧内容、ChangeUnit数量与Hash，仅在本机处理，不创建ReviewRun。可先用它验证仓库和范围配置；真实审查仍需独立的代码外发授权。

真实模型连通性测试：aihubmix.com的deepseek-v4-pro返回有效响应（11个Token的连接探针，不含代码）。之后用户明确选择“暂不允许代码外发，先只启动和完善本地前后端”，因此**没有真实代码审查任务或报告**，不能将下面的单元/合成集成测试当作业务实测结果。新接口要求每次创建任务显式`allow_external_model:true`，Vue前端先显示目的地、范围、费用提示并要求勾选授权；取消不会创建任务。`.env`、常见凭证文件不进入代码快照和diff。不要对生产数据库注入测试报告来填充页面。

## 官方Jira MCP连接

已参考官方[入门](https://developer.atlassian.com/cloud/rovo-mcp/guides/getting-started/)、[认证](https://developer.atlassian.com/cloud/rovo-mcp/guides/configuring-authentication-via-api-token/)与[工具目录](https://developer.atlassian.com/cloud/rovo-mcp/guides/supported-tools/)实现专用适配器。默认官方地址`https://mcp.atlassian.com/v2/mcp`；个人Token走Email+Token的Basic认证，服务账号API Key走Bearer认证。Token认证必须由组织管理员允许；交互式OAuth客户端授权尚未在本应用实现，不应声称已完成OAuth登录。

在设置页填写Email与Token（或服务账号Key），保存后测试；测试只读取工具目录与可访问站点，不创建Bug。选择/填写Cloud ID，填Bug项目Key，再保存。所需Jira权限为读取、搜索与创建Issue；不给删除或管理权限。工具参数依实际`tools/list` Schema绑定，缺参数或scope会明确报错。选定Epic/PRD后按`parent` JQL分页获取关联需求，读取Issue描述与配置的验收条件字段；Confluence外链正文仍不自动读取。创建Bug需要用户逐项确认，以Run/Finding标签做重试对账。

当前尚未提供真实Jira站点与账号授权，因此仅完成适配器和本地契约测试，**未完成真实Jira读写联调**。Codex中可另行连接Atlassian集成，但这不等于Govern后台自动获得该连接的凭证。

本轮回归：Python151项，139通过、12跳过（包含4个真实Docker合成测试）；Pi6项通过，均使用本地脚本化模型。Vue构建和浏览器验收通过，没有浏览器控制台异常。

## 启动

Python 3.10+、Git、Ripgrep、Node 22.19+、pnpm。Windows运行验证需要Docker Desktop **Linux containers**。
建议独立venv，不覆盖已有Govern或EvoAgent全局安装。新版发行包为`govern-review-harness`、命令为`govern-harness`（启动Web服务），不覆盖原`govern-agent`包和`govern` CLI；也可按下文使用`python -m govern`。

```powershell
cd 'D:\harness code\evoAgent'
python -m venv .venv
.\.venv\Scripts\python -m pip install -e '.[analysis]'
cd pi-runtime
node setup.mjs
pnpm install --frozen-lockfile
pnpm build:core
pnpm check
pnpm test
cd ..
$env:GOVERN_CONFIG = "$PWD\govern.config.json"
$env:EVOAGENT_LLM_PROVIDER = 'custom'
$env:EVOAGENT_LLM_BASE_URL = 'https://your-provider.example/v1'
$env:EVOAGENT_LLM_API_KEY = 'set-locally'
$env:EVOAGENT_LLM_MODEL = 'your-model'
.\.venv\Scripts\python -m govern
```

先把`govern.config.example.json`复制成本地配置，修改Jira工具映射和模型单价。示例工具名是**适配占位符**，不是任何MCP产品的保证接口。无Jira时可删除`jira`配置，跳过需求选择；不伪造业务一致性验证。
前端默认`http://127.0.0.1:8080`，原界面在`/legacy`。沿用EVOAGENT环境变量兼容旧部署。Python源目录统一为`src/govern`，按职责整合，不再依赖旧包路径。
`GOVERN_NODE`可以显式指定新版node.exe。未配置真实模型时任务明确失败，不输出伪造审查。

## 代码结构与执行契约

### src 布局整合（2026-10-21）

Python 源码统一放在 `src/govern`，不保留 `EvoAgent/` 或旧包名导入桥接。
原审查角色与仓库分析并入 `review`；HTTP 服务并入 `api`；策略片段及候选治理并入 `policy`；
Pi 桥接及运行验证并入 `runtime`；Jira MCP 并入 `integrations`。
`skills/` 只保留 Markdown 策略资源，加载器代码位于 `src/govern/skills`。

启动脚本、editable 安装、Docker 源码挂载、维护与评测脚本均已适配。
资源根目录由 `govern.core.paths.project_root()` 统一解析，部署时可用 `GOVERN_ROOT` 指定；
不依赖调用者的当前目录。既有 `EVOAGENT_*` 配置键及持久化标记保留兼容，来源见 `THIRD_PARTY_NOTICES.md`。

本次验收：Python 155 项（143 通过、12 条件跳过，包含 4 项真实 Docker 合成测试），
Pi 6 项通过，TypeScript 类型检查与 Vue 生产构建通过；本地服务已用新源码重启。
7 个 Vue 页面可打开，无浏览器控制台错误；配置、策略、候选、任务列表及健康接口返回 200。
未运行真实模型代码审查，未以应用源码仓库作为审查测试目标。

- `src/govern/review/repository.py`：Git四范围与内容快照、Python AST、可选JS/TS Tree-sitter、逐行ChangeUnit。删除用base定位；大符号按120行分片，保留全部变更行。引用为有上限的词法候选，不宣称精确调用图。
- `workbench.py`：复用任务、队列、Artifact、Checkpoint和效果账本。branch解析merge-base，range固定两侧Commit；staged/working-tree使用内容Hash，绝不把未提交内容称为Commit SHA。GitHub公共PR固定两侧SHA，独立缓存，不切换用户分支。
- `src/govern/integrations/jira_mcp.py`：标准MCP客户端、分页获取Epic及子需求，用户选择后冻结；Mapper不得任意搜索其他Issue。外链正文未读取会显示限制。自定义Jira工具返回值需要符合示例规范。
- `src/govern/policy/bundle.py`：公共证据规则、独立P级与严重性、六个阶段片段；策略哈希和模型身份随任务冻结。没有核心业务画像时P级待确认，不按模块名称猜测。
- `pi-runtime/src/runtime.ts`：六阶段DAG；各角色实际创建隔离的Pi AgentSession；三领域并行，阶段间只传结构化产物。单Unit形成Slice，跨Slice在Synthesizer汇总；尚不是全语言精确行为/调用图切分器。
- `pi-runtime/patches/govern-core.patch`：固定Pi源码Commit，小范围Core补丁实现共享Token预留/结算、Turn和工具上限；受控子任务深度1、并发2、无宿主Shell、不能再委派。父子共用Core预算，取消传播。恢复任务继承已用预算，不重新增发。
- `verification.py`：仅Validator可调用`run_verification`；Profile由服务端登记，Agent不能指定命令、宿主路径、Docker参数或凭证。测试导入、收集与执行均在Docker中。Docker不可用不回退宿主执行。
- `src/govern/policy/feedback_evolution.py`：反馈归因生成角色片段候选；保护评测仓库/diff；相同Pi流程评测；复用候选租约、审批、shadow、CAS发布指针和回滚。模型只能改阶段指导，不能改公共证据/风险/权限规则。

## 审查文档固定八项

每个Finding输出：**风险等级、来源模块、关联测试用例、风险位置、问题描述、影响范围、修复建议、证据依据**。
另附Finding ID、覆盖限制、静态/运行验证结论、mock边界和Artifact引用。缺失项显式“未提供／未验证”。关联测试不等于执行过；没有实际收集/运行测试不能写验证通过。
Markdown下载和报告页面使用相同字段。有效/无效反馈均留历史，漏报由用户补充位置与证据；不把模型自评当真实标签。

## Docker Profile

先由可信操作者构建/准备镜像，Node镜像只支持内置`node --test`，Python镜像包含Pytest。项目专用依赖必须预装镜像，不能在宿主机临时安装目标依赖。Dockerfile基镜像目前为标签，**不是可复现供应链构建**；生产准备时应将FROM也锁定摘要。
Profile必须填本地已存在的`repo@sha256:<64位摘要>`，不接受仅标签；执行时`--pull=never`。例如配置结构：

```json
{"execution_profiles":{"python-pytest":{"image":"your-registry/govern-python@sha256:<真实摘要>","runner":"pytest","timeout":120,"memory":"512m","cpus":"1"}}}
```

容器非root、禁网、只读根、只读代码输入、临时工作区、无宿主凭证/Socket，限制CPU/内存/PID/时间/输出。base/head使用同一Bundle，分别干净运行。每Finding最多3次，累计360秒、全局验证并发2。
plan必须给出假设、断言来源、目标符号、mock对象与selectors。Bundle形如`{"test_pay.py":"..."}`，selector写`.govern-tests/test_pay.py`。不要在Bundle key重复加`.govern-tests/`。
执行状态与测试状态、Validator结论独立。零测试、全部skip、依赖/收集错误均不算通过。head失败不自动证明回归；base也失败或测试不兼容必须解释。mock规则的字符串检查只是防错，不能证明生成测试语义正确，仍需独立审查测试及原始Artifact。容器内报告不是防恶意仓库伪造的可信证明。

## 反馈、记忆、Trace与评测

反馈存于任务Checkpoint并进入原有failure cases，按阶段、类型、原因分组。有效确认与建Bug分别保存；Marker查询+效果账本支持重试，但Jira搜索索引延迟下不承诺跨系统严格exactly-once。
工作记忆是隔离Session与观察摘要；证据记忆是冻结代码/需求/验证Artifact；治理记忆复用反馈/候选/评测/发布记录。上下文按保守字符估算接近输入预算80%时折叠旧工具观察，保留原始证据引用；仍超限则报错，不静默丢弃变更。不是完整语义记忆压缩算法。
Trace关联Run、Slice、Stage、子Session与Sandbox，记模型用量、工具、压缩与执行。费用是配置单价估算，不是供应商账单；未配置价格不能通过发布测评的完整遥测门禁。

沿用已有100条合成数据作为机制回归，不作为真实PR效果证明。当前**没有新增已人工标注的跨仓库真实PR基准**，需要使用方提供冻结JSONL并核对授权/来源。字段沿用旧EvolutionDataset，运行验证额外需要`before_files`、`after_files`，需求场景需要`requirements`；无完整快照不能宣称运行验证效果。
候选不得读取expected_findings；Validation/Holdout按仓库隔离及diff去重。Recall提升严格大于5个百分点（用十进制比较避免浮点误差），precision及现有质量/成本门禁无退化。另外要求有P0/P1标注样本且其召回无退化、沙箱累计耗时增长不超过20%、超时率无退化。空数据、不完整遥测和无效执行失败关闭。high_severity_recall按技术严重性，与新增p01_recall分别计算。

## 已验证与待补齐

本轮已跑Python原有回归与新增Govern单测，以及真实Pi SDK对本地脚本化模型的六阶段及子会话集成测试；这证明协议和编排可运行，**不证明审查质量**。真实模型质量、实际Jira MCP、GitHub远程权限仍需对应环境验收。

2026-10-20已完成真实Docker合成快照验收：同一外部依赖mock在base通过/head失败；零测试不算通过；导入失败不算复现；非root、只读输入、无Docker Socket、外网不可达及容器回收。构建了本地`govern-verification-python:local`镜像（使用已存在Govern演示镜像为基底，没有修改宿主Python），本次执行固定摘要`govern-verification-python@sha256:b28ceb777c6bbb423c61e68bca8880e8ff024d1ab0229e50df3e75158fbc4beb`。普通受限进程无法访问Docker管道，需要在有权限访问Docker Desktop的服务进程中运行；不能因此回退宿主执行。

复测（在允许访问Docker的PowerShell中）：

```powershell
$env:GOVERN_TEST_IMAGE='govern-verification-python@sha256:b28ceb777c6bbb423c61e68bca8880e8ff024d1ab0229e50df3e75158fbc4beb'
python -B -m unittest tests.govern.test_docker_integration -v
```

其他机器需自行准备镜像并填写它的真实RepoDigest，不能直接假定本机摘要存在。该4项测试证明指定环境中的隔离及分类行为，不是恶意代码逃逸安全认证，也不代表真实项目验证覆盖率。

最终回归：启用上述Docker镜像后，Python unittest共146项，134通过、12项按原条件跳过；Pi侧6项通过；TypeScript类型检查、前端JS语法检查和HTTP资源/报告冒烟测试通过。本地未保留测试容器；验证镜像保留供复测。

尚未完成原大计划的部分：真实运行验证的消融实验及统计报告、新增人工真实PR测试集、任意JS/TS项目测试命令Profile、外链正文适配器、多语言精确依赖图、进化失败轨迹语义聚类、完善线上监控仪表盘及前端完整浏览器验收。前端已有六页与操作接线、沙箱用量与环境错误统计，但不应包装成上述全部能力已生产完成。
简历暂不使用71%→85%等旧数值；以改造后冻结评测报告为准。可诚实描述“实现Pi Core共享预算与受控子任务、需求选择及变更映射、Validator专用Docker运行验证工具和可审计反馈治理”。

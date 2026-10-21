# Govern Agent

来源与第三方依赖说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。历史设计材料保留在 `docs/`，其中旧目录及命令以本页和当前启动脚本为准。

新版入口是Vue 3控制台，Python服务同源提供前端与API。旧版EvoAgent控制台保留在`/legacy`。

```powershell
cd 'D:\harness code\evoAgent'
python scripts/start_govern.py
# 打开 http://127.0.0.1:8080/
```

首次安装需准备Node 22.19+，在`frontend`目录执行`npm install`、`npm run build`；Pi运行层按[GOVERN_IMPLEMENTATION.md](GOVERN_IMPLEMENTATION.md)准备。当前机器已完成构建和服务启动。

- “连接与设置”可保存/测试真实模型与官方Atlassian Rovo MCP连接；密钥不回显。
- 本地仓库与远程PR分别显示各自输入；支持范围选择与可选文件筛选，不切换用户分支。
- “冻结并开始审查”会先显示具体模型目的地及代码外发范围，逐次确认前不会创建任务。
- 没有Jira账号授权时无法获取真实PRD或创建Bug，页面会明确提示，不提供模拟需求。
- 本轮按用户最新选择**只启动并完善本地前后端，未获准发送仓库代码，不宣称真实代码审查已完成**。真实模型连接测试成功与真实代码审查是两件事。

## 原EvoAgent说明（兼容功能）

面向 Pull Request 风险审查的个人独立项目：通过可恢复的 Agent Runtime 组织多角色取证，以证据门禁约束报告，对 Prompt / Skill 的演进进行评测、审批与回滚治理。

输入为统一 Diff 或 GitHub PR 事件，输出为带位置、证据、修复建议和测试建议的报告。项目包含 Python 服务、Web 管理台、SQLite / PostgreSQL 存储，以及进程内队列 / Redis Streams 两种配置。

## 核心能力

- **可恢复执行**：有界 Agent Loop、阶段检查点、执行日志、审批暂停和副作用幂等账本。
- **多角色审查**：Lead 分派，Security 与 Correctness/Reliability Worker 并行取证，Critic 质疑，Lead 裁决与程序门禁复核。
- **上下文与记忆**：完整材料留存、风险优先压缩、证据引用、按需读取与记忆生命周期治理。
- **受控自进化**：失败诊断、候选差量、Validation / Holdout、人工审批、Shadow、发布和回滚。
- **集成与管理**：GitHub Webhook、可选评论回写、受限 Draft PR 修复、身份认证、租户隔离和监控。

当前结果主要来自本地机制测试与受控样本。真实模型的角色收益、真实 PR 泛化与生产容量需要分别验证。

## 架构与目录

```text
HTTP / GitHub Webhook → application → 数据库任务 + 异步队列
                                          ↓
                                   runtime / Harness
                                          ↓
Lead → 并行 Worker → 证据返工 → Critic → Lead 裁决 → 完成门禁
                ↑                                    ↓
     memory / context / skills              报告 / 可选 GitHub 写入

反馈 → evolution → evaluation → 审批 / Shadow → 发布或回滚
```

```text
govern-agent/
├── src/govern/               # python -m govern（先 editable 安装）
│   ├── api/                  # HTTP API、应用服务和任务入口
│   ├── core/                 # 配置、模型、模式和 Diff 解析
│   ├── review/               # 仓库分析、工作台、审查角色、工具和报告
│   ├── runtime/              # Harness、循环、恢复、日志、审批和证据
│   ├── memory/               # 上下文、检索、记忆治理和使用反馈
│   ├── policy/               # 阶段策略、反馈进化、候选、租约和发布治理
│   ├── evaluation/           # 样本、计分、消融和机制证明
│   ├── storage/              # SQLite / PostgreSQL
│   ├── integrations/         # GitHub 与身份认证
│   ├── skills/               # Skill 加载、校验和演进
│   ├── repair/               # 补丁、修复与验证
│   └── infrastructure/       # 队列、指标、追踪和告警
├── tests/                    # 对应模块测试；support/ 为共享助手
├── scripts/
│   ├── benchmarks/           # Runtime、Context/Memory 基准
│   ├── evaluation/           # 数据导入、数据检查、消融与演进回放
│   └── maintenance/          # 布局检查与干净副本导出
├── data/benchmarks/          # 公开受控 PR Diff 样本
├── evolution-data/           # 外部评测数据挂载入口，仅跟踪说明文件
├── policies/                 # govern-review 为 Pi 策略源；legacy-skills 仅用于兼容流程
├── web/                      # 管理台静态资源
├── docs/
│   ├── project/              # 项目长文与原始 PDF
│   ├── design/               # 设计、实施记录和使用说明
│   ├── interview/            # 面试与简历资料
│   ├── benchmarks/           # 已留存基准报告
│   └── assets/               # 文档图片
├── .env.example
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

推荐入口为 `python scripts/start_govern.py`，直接解析源码目录并保留本地配置。`pip install -e .` 后可用 `python -m govern` 或 `govern-harness` 启动基础 API。内部导入例如 `from govern.api.service import ReviewService`；不保留旧包路径桥接。`EVOAGENT_*` 环境变量、历史数据库标记和队列键暂保留，以兼容已有本地状态。

## 本地启动

使用 Python 3.10 或更高版本；Docker 镜像使用 Python 3.11。以下命令均在仓库根目录执行。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
Copy-Item .env.example .env
```

Linux / macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e .
cp .env.example .env
```

编辑 `.env`，配置可用的兼容 Chat Completions 的模型服务：

```dotenv
EVOAGENT_LLM_PROVIDER=custom
EVOAGENT_LLM_BASE_URL=https://your-provider.example/v1
EVOAGENT_LLM_API_KEY=replace-with-your-key
EVOAGENT_LLM_MODEL=replace-with-your-model
```

地址、密钥和模型名需替换。服务可以在没有模型时启动，但正式审查需要模型，不会静默降级为规则报告。离线机制测试无需模型凭据。

```bash
python -m govern
```

打开 `http://127.0.0.1:8080/`，健康检查为 `GET /health`。本地默认数据库为 `evoagent.db`，队列为进程内实现。根目录 `.env` 会自动读取，实际进程环境变量优先；修改配置后需重启。

模板默认关闭认证，仅用于本地访问。启用认证时，在 `.env` 中设置以下配置并替换占位符：

```dotenv
EVOAGENT_AUTH_REQUIRED=true
EVOAGENT_AUTH_SECRET=replace-with-a-random-secret-at-least-32-bytes
EVOAGENT_BOOTSTRAP_ADMIN_USERNAME=admin
EVOAGENT_BOOTSTRAP_ADMIN_PASSWORD=replace-with-a-password-at-least-10-characters
```

Bootstrap 管理员只在用户名不存在时创建，不会覆盖已有用户密码。

## Docker Compose

准备 `.env` 后执行：

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f evoagent
```

Compose 启动应用、PostgreSQL 和 Redis，访问端口为 `8080`。代码、前端、Skill 及外部评测目录使用挂载；数据库与队列使用命名卷。

```bash
# 修改挂载代码后
docker compose restart evoagent
# 修改依赖或 Dockerfile 后
docker compose up -d --build
# 停止容器，保留数据卷
docker compose down
```

Compose 是包含本地默认账号与数据库密码的演示配置。实际使用时设置自己的登录密钥和管理员密码，并按部署环境调整数据库与网络。上传副本不包含 `.env`；普通停止无需使用会删除数据卷的 `down -v`。

## 请求与报告

可通过管理台提交 Diff、查询任务和报告。常用接口：

| 接口 | 用途 |
|---|---|
| `GET /health` | 健康检查 |
| `POST /v1/auth/login` | 启用认证时获取会话 |
| `POST /v1/reviews` | 提交审查 |
| `GET /v1/tasks/<task-id>` | 查询任务 |
| `GET /v1/tasks/<task-id>/report` | Markdown 报告 |
| `POST /v1/tasks/<task-id>/resume` | 恢复可续跑任务 |

启用认证时，业务请求携带 `Authorization: Bearer <access-token>`。GitHub 接入另需 Webhook 密钥、仓库授权与对应凭据，评论回写默认关闭。修复受授权与验证约束，不会把每次审查自动转成代码合并。

## 测试

```bash
python -m unittest discover -s tests -t . -v
```

标准库 `unittest` 测试已按模块分组。布局检查可运行 `python -B scripts/maintenance/check_layout.py`，涵盖语法、导入、脚本入口、静态资源和文档链接。仅运行 Runtime 测试：

```bash
python -m unittest discover -s tests/runtime -t . -v
```

PostgreSQL 专项测试仅在显式配置 `EVOAGENT_TEST_POSTGRES_URL` 后执行，并创建、清理临时 schema，应使用专用测试数据库。

浏览器测试为 `tests/ui/evolution_ui.cjs`，需要额外安装 Playwright 和浏览器后执行 `node tests/ui/evolution_ui.cjs`；不包含在 Python 测试中。Windows 脚本使用 Edge 通道。

## 评测与基准

[受控评测集](data/benchmarks/pr_diff_100.jsonl) 包含 100 条 Diff、10 个模拟仓库：40 条风险样本，60 条干净样本；80 条验证与 20 条保留样本按仓库划分。它不是 100 个真实生产 PR。

无需模型的机制基准：

```bash
python scripts/benchmarks/benchmark_runtime_harness.py --runs 100
python scripts/benchmarks/benchmark_context_memory.py --output output/context-memory.json
python scripts/evaluation/run_prompt_evolution_proof.py --output-dir output/prompt-proof
```

最后一项生成受控回放证明。重复运行需使用新的输出目录，不能覆盖已存在的证明数据库。

真实 PR 数据检查与模型角色消融：

```bash
python scripts/evaluation/run_real_pr_benchmark.py evolution-data/core.jsonl
python scripts/evaluation/run_agentic_evaluation.py evolution-data/core.jsonl --output output/agentic-evaluation.json
```

需要自行准备符合格式的标注数据；角色消融还需通过进程环境变量 `EVOAGENT_LLM_BASE_URL`、`EVOAGENT_LLM_API_KEY`、`EVOAGENT_LLM_MODEL` 或对应命令行参数传入模型配置，不能假设评测脚本会自动加载 `.env`。该消融会调用模型并产生费用。数据导入工具为 `scripts/evaluation/import_github_pr_dataset.py`，各脚本支持 `--help`。

历史高风险召回 `84.2% → 94.7%` 对应 19 个目标命中 16 个到 18 个，其中包含规则覆盖变化，不能单独证明多 Agent 增益。引用保留、重放去重和记忆检索也各有分母。详细口径见 [面试准备手册](docs/interview/EvoAgent_面试准备手册.md) 第九章及 [基准目录](docs/benchmarks/)。

## 文档与边界

- [文档导航](docs/README.md)
- [自进化治理使用说明](docs/design/Self_Evolution_治理使用说明.md)
- [自进化实施记录](docs/design/Self_Evolution_优化实施记录.md)
- [Runtime / Harness](docs/design/Runtime_Harness_优化后完整说明.md)
- [Context / Memory](docs/design/Context_Memory_优化后完整说明.md)

长文与 PDF 包含历史设计，当前行为以代码及使用说明为准。队列确认不等于跨系统严格只执行一次；协作式超时不能强制终止所有阻塞操作；本地验证不等于真实模型效果和生产部署已验收。

## 上传 GitHub

提交源码、测试、脚本、Web / Skill 资源、公开样本与文档即可。`.gitignore` 排除了虚拟环境、实际 `.env`、数据库、缓存、日志、临时产物和外部评测数据。

无需上传 `.venv`，使用者按 `requirements.txt` 重建环境。整理后的副本可以自行初始化 Git、提交并推送；本次整理没有绑定或发布远端仓库。

后续可再次生成干净副本（目标必须是尚不存在的目录）：

```bash
python scripts/maintenance/export_github.py ../EvoAgent-GitHub-new --zip
```

导出会生成文件 SHA-256 清单，可用于核对复制完整性。公开材料放在源码、文档等目录中；私人数据放在 `evolution-data/` 或运行输出目录，不应混入公开样本目录。

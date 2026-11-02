# Govern Agent

面向 Git 变更的需求驱动代码审查与策略治理工具，提供 Vue 3 控制台和 Pi Agent 执行层。

## 核心能力

- 本地 branch、range、staged、working-tree 与 GitHub PR 审查，冻结 base/head、代码和需求。
- Jira 需求映射、ReviewSlice 分片、正确性／安全性／契约测试并行审查、结果综合与独立核验。
- 共享证据、持久化记忆、上下文压缩、阶段检查点及 Token／工具调用监控。
- Validator 按需构造测试与外部依赖 mock，通过独立 Docker 验证服务运行并保存证据。
- 反馈案例驱动阶段 Skill 候选生成，固定 Validation／Holdout 评测，人工发布及版本回滚。

## 架构与目录

```text
Vue 控制台 → Python API → Pi 六阶段审查
                  │              │
            SQLite / Artifact    └→ Validator → 独立验证服务 → Docker 沙箱
                  └→ 反馈案例 → 策略候选 → 评测 → 人工发布 / 回滚

frontend/                 Vue 3 源码
web/app/                  构建后的控制台
src/govern/
  api/                    API 与应用服务
  review/                 Git 快照、代码分析、需求与审查工作台
  runtime/                Pi 桥接、工具、验证网关与独立执行服务
  memory/                 记忆检索与上下文压缩
  policy/                 策略编译、反馈进化与版本治理
  evaluation/             固定评测集加载与计分
  storage/                数据存储
  integrations/           Jira MCP、GitHub 与认证
policies/govern-review/   公共规则、风险规则、六阶段 Skill 与专项片段
pi-runtime/               Pi SDK 执行层及受控 Core 补丁
data/benchmarks/           受控评测样本
tests/                    回归测试
scripts/                  启动、维护及验证脚本
```

## 本地启动

依赖 Python 3.11、Node.js 22.19+、pnpm 10、Git、Ripgrep。以下命令在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[analysis,container]"

node pi-runtime/setup.mjs
cd pi-runtime
pnpm install --frozen-lockfile
pnpm build:core
cd ../frontend
npm ci
npm run build
cd ..

python scripts/start_govern.py
```

打开 http://127.0.0.1:8080/，在设置页配置模型、Jira MCP 和 GitHub Token。
正式审查需逐次确认代码外发；历史记忆外发另行授权。配置及 SQLite 数据保存在被 Git 忽略的 `.govern/` 中。

## Docker 部署

需要 Docker Desktop 的 Linux 引擎。先准备本地配置及已缓存的、带 Pytest 的固定摘要镜像，
在 `.govern/config.json` 的 `execution_profiles` 中登记；镜像构建文件见
[verification-python.Dockerfile](pi-runtime/verification-python.Dockerfile)。

```powershell
$env:PYTHONPATH = "src"
python scripts/prepare_verifier.py
$env:GOVERN_REPOSITORY_PATH = "D:/path/to/repository"
docker compose -f compose.govern.yaml up -d --build
```

访问地址仍为 http://127.0.0.1:8080/。Compose 启动主应用与独立验证服务，复用 `.govern/` 数据，
目标仓库只读挂载到 `/repositories/project`；容器控制台中填写该路径。

只有验证服务挂载 Docker Socket，该权限近似宿主级容器管理权限。实际测试容器禁网、非 root、只读输入并限资源。
不要同时运行本地服务和容器服务写入同一 SQLite。详细配置见[验证服务说明](docs/design/govern-verification-service.md)。

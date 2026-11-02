# Govern 个人版 Docker 与 SQLite

## 当前部署（2026-10-23）

Docker Desktop 应用组和唯一应用容器均为 govern-agent，入口 http://127.0.0.1:8080/。
前端、Python API、Pi 运行时和 SQLite 引擎在同一应用容器内；不再启动 PostgreSQL 或 Redis。
原8081应用与PG容器已移除，旧 govern-agent_govern_* 命名卷保留，不参与当前运行。

当前 Compose 直接绑定宿主机 .govern 到 /var/lib/govern，绑定 policies 到 /app/policies，
所以使用原来的 govern.sqlite、模型/Jira配置与人工维护策略，不复制成另一套历史。
严禁同时启动宿主机服务和容器写入同一SQLite。服务监听只绑定宿主127.0.0.1。

切换前已备份SQLite、config.json与policies，位于Git忽略的
.govern/migration-backup/sqlite-container-34dff06cff804a989b0b925d5d053e54。
基准：12条任务、5条反馈案例、2个候选版本、29个Artifact、59个检查点，
生效策略a63f7f41-db58-4373-a958-4ebf60be6bd9、generation=2。
备份含凭证，不能上传、截图或公开。恢复应先停止容器，不能覆盖运行中的数据库。

## 启动

```powershell
docker compose --env-file .govern/docker.env -f compose.govern.yaml up -d --build
docker compose --env-file .govern/docker.env -f compose.govern.yaml ps
```

.gitignore及.dockerignore都排除.govern；配置仅运行时挂载，不烘焙进镜像。
.govern/docker.env仅包含本地端口与授权仓库挂载路径，不再包含PG密码。
不加env文件也可启动，但需自行准备默认的.govern/repositories或设置GOVERN_REPOSITORY_PATH。

页面仓库路径填写 /repositories/project，不使用Windows盘符。
AIedu仓库只读挂载，不修改目标工作区。Windows/Linux Git的core.quotePath差异可能影响原始diff Hash，
不代表base/head Commit发生变化。新建审查仍须逐次确认代码外发，自动进化模型调用保持关闭。
原历史冻结快照和引用留在SQLite中，不改写历史报告的宿主路径。

## 镜像与边界

Dockerfile.govern构建Vue静态页面、Python、Node/Pi、Git、Ripgrep、Tree-sitter。
应用以非root运行，保留第三方许可证。模型和Jira配置沿用原文件，但本次不发起外部连接验证或模型调用。
没有挂载Docker Socket；运行验证明确显示unavailable，不回退宿主执行。
本地Trace、指标和OpenTelemetry SDK保留，远端OTLP导出未启用，需要时另装.[otlp]并验证端点。
前端依赖沿用锁文件，已有npm audit告警不等于已经完成安全审计。

## PostgreSQL代码与历史验证

PostgresTaskStore与原版适配、可选依赖及测试代码仍保留；本次不删除这项可选能力。
个人版Compose不使用它，配置错误不会静默回退数据库。
如以后确需PG，可通过GOVERN_DATABASE_URL和宿主启动器--storage postgres单独启用。

2026-10-22用独立PostgreSQL16验证：
原版集成12项通过；Govern共享契约58项通过（审查/反馈/策略/自动进化49项，Runtime9项）。
一次双后端全量282项：278通过、4项跳过，随后启动专项3项通过。
模型/Jira/沙箱仍用原有确定性假实现，不代表真实服务质量实测。
临时PG/SQLite验证容器已清理，所有测试采用随机schema，不写入当前用户案例池。

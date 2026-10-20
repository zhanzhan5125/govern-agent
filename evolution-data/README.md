# 自进化评测数据目录

将经过授权、人工标注和按仓库隔离的核心数据放入 `core.jsonl`，独立影子数据放入 `shadow.jsonl`。不要放入 API Key、认证信息或未经授权的私有源码。

Docker Compose 将本目录只读挂载到 `/app/evolution-data`。本目录不提供伪造的生产样本；没有真实数据时可保持 `.env` 中两个评测路径为空。

配置、字段、门禁与调用步骤见 [自进化治理使用说明](<../docs/design/Self_Evolution_治理使用说明.md>)。核心数据、Holdout 和 Shadow 均由评测端读取，不应复制到 Skill 资源、Prompt 或候选生成上下文。

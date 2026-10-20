# 文档导航

## 当前使用与设计

- [启动、配置、测试与评测](../README.md)
- [模块化目录与迁移说明](design/模块化目录与迁移说明.md)
- [模块重构检查报告](design/模块重构检查报告.md)
- [Runtime / Harness](design/Runtime_Harness_优化后完整说明.md)
- [Context / Memory](design/Context_Memory_优化后完整说明.md)
- [自进化治理使用说明](design/Self_Evolution_治理使用说明.md)
- [自进化实施记录](design/Self_Evolution_优化实施记录.md)
- [四方向优化方案](design/EvoAgent_四方向优化方案.md)

## 项目资料

- [完整项目文档](<project/EvoAgent：自进化Harness智能体 (3)_1.md>)
- [原始 PDF](<project/EvoAgent：自进化Harness智能体 (3)_1.pdf>)
- [面试准备手册](interview/EvoAgent_面试准备手册.md)
- [STAR 写作指南](interview/STAR法则_项目与实习经历编写指南.md)

完整长文和原始 PDF 保留历史版本说明；PDF 未随 Python 目录迁移改写。当前导入路径以源码为准，当前运行方式以根目录 README 为准。

## 已留存评测报告

- [Runtime 基准](benchmarks/runtime_harness_benchmark.json)
- [Context / Memory 基准](benchmarks/context_memory_benchmark.json)
- [简历指标核算](benchmarks/interview_metric_audit.json)
- [受控样本](../data/benchmarks/pr_diff_100.jsonl)

目录整理不覆盖既有基准结果。新的基准建议输出到被 Git 忽略的 `output/` 目录。

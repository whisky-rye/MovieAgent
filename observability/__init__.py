"""可观测性包：进程内指标注册表 + 告警规则引擎。

不依赖任何第三方库（纯标准库），便于在强 MySQL / 多 worker 服务中零成本接入。
跨进程汇总（多 worker 场景）后续可接 Redis 后端；当前每个 worker 暴露自身快照，
Prometheus 端点可被各 worker 分别 scrape 后聚合。
"""

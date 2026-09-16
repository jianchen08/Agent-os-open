# 开发指南索引

> 指南 = **横切规则 + 指针**：只承载跨模式公共契约与权威指针，写成可注入遵守的规则体。
> **模式私有知识（怎么配 agent / 编排 / 主题 / 面板）的权威载体在模式包内物料**
> `<USER_ROOT>/plugins/modes/<mode>/{agents/,pipelines/,rules/,policies/,profile.yaml,webview/}`——自包含、自文档，本目录不复制其内容。
> 主线依据：`docs/working/模式体系落地设计_20260915.md`；架构决策查 `docs/decisions/`（ADR，按日期排序，被否方案在各 ADR Alternatives Considered 节）。

| 文档 | 定位 | 行数 |
|---|---|---|
| [plugin-protocol.md](plugin-protocol.md) | 插件协议权威：包结构 / 注册发现 / manifest 契约 / 依赖引用 / 种子生命周期与用户仓 git | 164 |
| [execution-semantics.md](execution-semantics.md) | 执行语义规则：Agent 身份（双来源/身份性字段/agent_id 三视角）× 编排（三级解析/管道=函数/G10 DSL） | 79 |
| [theme.md](theme.md) | 主题协议规则：声明面红线 / 现行两轨 / theme.apply 动态协议（规划中 Wave 2） | 35 |
| [plugin-external-mcp.md](plugin-external-mcp.md) | 外部 MCP 接入规则（HTTP / stdio / 零声明观测导入） | 74 |
| [plugin-native-rust.md](plugin-native-rust.md) | native 插件规则（cdylib / ABI 契约 / G8） | 116 |
| [streaming-protocol.md](streaming-protocol.md) | 流式事件协议（capabilities.streaming 契约、块协议、对账认领） | 296 |
| [ai-coding-spec.md](ai-coding-spec.md) | AI 编码总纲：生成/审查规则指针 + 模式体系约束（前端冻结/引用语法/A-B 面/进化两型） | 36 |
| [deployment.md](deployment.md) | 部署手册（拓扑/环境变量/守护/安全清单） | 240 |
| [backup-restore.md](backup-restore.md) | SQLite 备份恢复操作 | 77 |
| [logging.md](logging.md) | 日志体系（双运行时汇聚/链路追踪） | 86 |
| [ci-cd-guide.md](ci-cd-guide.md) | 测试与 CI 手册（run_gates 门禁源） | 1106 |
| [troubleshooting.md](troubleshooting.md) | 排障对照表（症状/根因/解法） | 199 |

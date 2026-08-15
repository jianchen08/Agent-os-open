//! Capability 路由——sidecar 插件反向调用内核能力的通道。
//!
//! 内核的 McpClient 默认只发起请求（内核→sidecar），本模块补齐反向通道：
//! sidecar 通过 stdout 发出 JSON-RPC request，内核 reader loop 识别后路由到
//! 对应的 capability handler。
//!
//! ## 标准能力与 method 约定
//!
//! | capability | method | 语义 |
//! |------------|--------|------|
//! | pipeline-executor | suspend | 挂起当前管道 |
//! | pipeline-executor | resume | 恢复挂起的管道 |
//! | pipeline-executor | start_run | 起一个新管道 |
//! | event-bus | emit | 发事件/通知前端 |
//! | config-reader | get | 读配置节 |
//! | metrics | record | 插件上报指标（record_metric，监控设计 §三 通道2） |
//! | service-registry | <storage 域>.* | 插件访问内核共享基础设施（M2：execution-records/summaries/memory 存储，基础设施下沉内核） |
//! | frontend | emit | 插件 → 内核 → 前端一次性事件推送（ADR §3.5，task_observability：cost_update/tool_progress/termination_status） |
//!
//! [来源: ROADMAP.md 审批闭环/复盘调管道/event-bus 三项业务的前置地基]
//! [来源: docs/working/重要设计/插件监控与指标机制设计.md §三 通道2]
//! [来源: docs/working/channel_api_migration_plan.md §七 M2]

use async_trait::async_trait;
use serde_json::Value;

use crate::error::McpError;

/// 标准 capability 名称（与 SDK `STANDARD_CAPABILITIES` 对齐）。
pub const STANDARD_CAPABILITIES: &[&str] = &[
    "pipeline-executor",
    "config-reader",
    "tenant-context",
    "event-bus",
    "logger",
    "metrics",
    "tool-executor",
    "service-registry",
    "frontend",
];

/// Capability 路由器——处理 sidecar 反向调用内核能力。
///
/// 实现方（通常在 engine/api 层）持有所需资源（engine 句柄等），
/// 按 `capability.method` 分派到具体内核实现。
///
/// trait 定义在 mcp crate 以避免 engine→mcp 的循环依赖；
/// 实现放在能访问 engine 的上层 crate。
#[async_trait]
pub trait CapabilityRouter: Send + Sync {
    /// 处理一次 capability 调用。
    ///
    /// Args:
    /// - capability: 能力名（如 "pipeline-executor"）
    /// - method: 方法名（如 "resume"）
    /// - params: 调用参数
    ///
    /// Returns:
    /// - Ok(value): 成功结果（JSON-RPC result 字段）
    /// - Err(McpError): 失败（将作为 JSON-RPC error 返回给 sidecar）
    async fn handle(
        &self,
        capability: &str,
        method: &str,
        params: Value,
    ) -> Result<Value, McpError>;

    /// 该 router 已知的 capability namespace 列表。
    ///
    /// 用于 [`crate::client`] 的 reader loop 调
    /// [`parse_capability_method_with`] 做动态白名单解析——不再依赖编译期
    /// [`STANDARD_CAPABILITIES`] 常量，运行时注册的 namespace 也能被识别为
    /// 合法 capability 调用。
    ///
    /// 默认返回 [`STANDARD_CAPABILITIES`]，保证旧实现（如 `EchoRouter`、
    /// `ToolInvokeRouter` 等测试 stub）无需改动即可工作；动态注册表实现
    /// （[`crate::handler_registry::CapabilityHandlerRegistry`]）覆盖此方法
    /// 返回实际注册的 namespace。
    fn known_namespaces(&self) -> Vec<String> {
        STANDARD_CAPABILITIES
            .iter()
            .map(|s| s.to_string())
            .collect()
    }
}

/// 解析 incoming message 的 capability + method。
///
/// sidecar 反向调用使用约定的 method 命名：`<capability>.<method>`
/// （如 `pipeline-executor.resume`）。本函数拆分命名空间。
///
/// 使用编译期 [`STANDARD_CAPABILITIES`] 作为白名单。**新代码应优先使用
/// [`parse_capability_method_with`]，它接受动态 namespace 列表，支持运行时
/// 注册的能力（M2 注册表改造）**。本函数保留是为了向后兼容现有调用方，
/// 内部委托给 `parse_capability_method_with(method, STANDARD_CAPABILITIES)`。
///
/// Returns:
/// - Some((capability, method)): 解析成功
/// - None: method 不含命名空间，非 capability 调用
pub fn parse_capability_method(method: &str) -> Option<(&str, &str)> {
    parse_capability_method_with(method, STANDARD_CAPABILITIES)
}

/// 解析 capability + method，白名单由调用方提供（动态 namespace）。
///
/// 与 [`parse_capability_method`] 的区别：白名单不再是编译期常量，而是运行时
/// 由调用方传入。这让 [`crate::handler_registry::CapabilityHandlerRegistry`]
/// 里注册的任意 namespace 都能被识别为合法 capability 调用，新增能力不再
/// 需要修改 `STANDARD_CAPABILITIES` 常量。
///
/// Args:
/// - `method`: 待解析的 JSON-RPC method（如 `"human-interaction.create_choice"`）；
/// - `known_namespaces`: 当前已注册的 namespace 列表（如 `["pipeline-executor",
///   "human-interaction"]`），通常来自 `CapabilityHandlerRegistry::namespaces()`。
///
/// Returns:
/// - `Some((namespace, method))`: 解析成功，namespace 在白名单且 method 含 `.`；
/// - `None`: method 含 `/`（MCP 标准方法）、namespace 不在白名单、或不含 `.`。
pub fn parse_capability_method_with<'a, T: AsRef<str>>(
    method: &'a str,
    known_namespaces: &[T],
) -> Option<(&'a str, &'a str)> {
    // 跳过 MCP 标准方法（initialize / tools/* / resources/* / notifications/*）
    // 这些是协议层方法，不是 capability 调用
    if method.contains('/')
        || known_namespaces
            .iter()
            .all(|cap| !method.starts_with(cap.as_ref()))
    {
        return None;
    }

    method.split_once('.')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_pipeline_executor_method() {
        let (cap, method) = parse_capability_method("pipeline-executor.resume").unwrap();
        assert_eq!(cap, "pipeline-executor");
        assert_eq!(method, "resume");
    }

    #[test]
    fn test_parse_config_reader_method() {
        let (cap, method) = parse_capability_method("config-reader.get").unwrap();
        assert_eq!(cap, "config-reader");
        assert_eq!(method, "get");
    }

    #[test]
    fn test_parse_event_bus_method() {
        let (cap, method) = parse_capability_method("event-bus.emit").unwrap();
        assert_eq!(cap, "event-bus");
        assert_eq!(method, "emit");
    }

    #[test]
    fn test_reject_mcp_standard_methods() {
        // MCP 标准方法（含 /）不应被识别为 capability 调用
        assert_eq!(parse_capability_method("notifications/initialized"), None);
        assert_eq!(parse_capability_method("tools/list"), None);
        assert_eq!(parse_capability_method("resources/read"), None);
    }

    #[test]
    fn test_reject_initialize() {
        // initialize 是协议方法，非 capability
        assert_eq!(parse_capability_method("initialize"), None);
    }

    #[test]
    fn test_reject_unknown_capability() {
        // 不在标准清单里的命名空间
        assert_eq!(parse_capability_method("unknown-capability.foo"), None);
    }

    #[test]
    fn test_standard_capabilities_complete() {
        // 确保 6 个标准能力都在清单里（与 SDK STANDARD_CAPABILITIES 对齐）
        assert!(STANDARD_CAPABILITIES.contains(&"pipeline-executor"));
        assert!(STANDARD_CAPABILITIES.contains(&"config-reader"));
        assert!(STANDARD_CAPABILITIES.contains(&"tenant-context"));
        assert!(STANDARD_CAPABILITIES.contains(&"event-bus"));
        assert!(STANDARD_CAPABILITIES.contains(&"logger"));
        assert!(STANDARD_CAPABILITIES.contains(&"metrics"));
    }

    #[test]
    fn test_parse_metrics_method() {
        // 监控设计 §三 通道2：metrics.record 反向调用
        let (cap, method) = parse_capability_method("metrics.record").unwrap();
        assert_eq!(cap, "metrics");
        assert_eq!(method, "record");
    }

    #[test]
    fn test_parse_tool_executor_method() {
        // tool-executor 是 SDK STANDARD_CAPABILITIES 之一，必须能被识别为 capability 调用。
        // 当前会失败：tool-executor 不在内核 STANDARD_CAPABILITIES 白名单，
        // parse_capability_method 第 77 行会返回 None，unwrap 触发 panic。
        let (cap, method) = parse_capability_method("tool-executor.invoke").unwrap();
        assert_eq!(cap, "tool-executor");
        assert_eq!(method, "invoke");
    }

    #[test]
    fn test_standard_capabilities_includes_tool_executor() {
        // 与 SDK STANDARD_CAPABILITIES（8 项）对齐：内核白名单必须含 tool-executor。
        assert!(
            STANDARD_CAPABILITIES.contains(&"tool-executor"),
            "STANDARD_CAPABILITIES 缺少 tool-executor，导致 tool-executor.* 反向调用被白名单挡掉"
        );
    }

    #[test]
    fn test_parse_with_accepts_dynamic_namespace() {
        // 动态 namespace 列表里有的，能识别为 capability 调用。
        // 这是 M2 的核心：不再依赖编译期 STANDARD_CAPABILITIES 常量。
        let known = vec!["human-interaction".to_string()];
        let (cap, method) =
            parse_capability_method_with("human-interaction.create_choice", &known).unwrap();
        assert_eq!(cap, "human-interaction");
        assert_eq!(method, "create_choice");
    }

    #[test]
    fn test_parse_with_rejects_namespace_not_in_list() {
        // 动态列表里没有的 namespace，返回 None（即便它看起来像 capability.method）。
        let known = vec!["pipeline-executor".to_string()];
        assert_eq!(
            parse_capability_method_with("human-interaction.foo", &known),
            None
        );
    }

    #[test]
    fn test_parse_with_empty_list_rejects_everything() {
        // 空 namespace 列表 = 没有任何 capability 注册，全部返回 None。
        let empty: Vec<&str> = vec![];
        assert_eq!(
            parse_capability_method_with("pipeline-executor.resume", &empty),
            None
        );
    }

    #[test]
    fn test_parse_with_still_rejects_mcp_standard_methods() {
        // 含 / 的 MCP 标准方法（tools/list 等）即便 namespace 在列表里也要拒绝。
        let known = vec!["tools".to_string()];
        assert_eq!(parse_capability_method_with("tools/list", &known), None);
    }
}

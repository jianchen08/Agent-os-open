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
//!
//! [来源: ROADMAP.md 审批闭环/复盘调管道/event-bus 三项业务的前置地基]
//! [来源: docs/working/重要设计/插件监控与指标机制设计.md §三 通道2]

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
}

/// 解析 incoming message 的 capability + method。
///
/// sidecar 反向调用使用约定的 method 命名：`<capability>.<method>`
/// （如 `pipeline-executor.resume`）。本函数拆分命名空间。
///
/// Returns:
/// - Some((capability, method)): 解析成功
/// - None: method 不含命名空间，非 capability 调用
pub fn parse_capability_method(method: &str) -> Option<(&str, &str)> {
    // 跳过 MCP 标准方法（initialize / tools/* / resources/* / notifications/*）
    // 这些是协议层方法，不是 capability 调用
    if method.contains('/') || STANDARD_CAPABILITIES.iter().all(|cap| !method.starts_with(cap)) {
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
}

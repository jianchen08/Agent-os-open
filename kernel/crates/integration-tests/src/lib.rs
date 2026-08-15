//! 集成测试共享模块
//!
//! 提供跨测试文件复用的 Mock 类型和辅助工具。

use std::collections::HashMap;

use agentos_core::traits::{HookContext, LifecycleHook, PluginInvoker};
use agentos_core::types::{PluginContext, PluginError, PluginResult};
use async_trait::async_trait;
use serde_json::json;

/// 空操作 Mock Invoker——基准测试和通用测试中不引入外部开销。
///
/// invoke_pipeline_plugin 返回固定 state_updates，无路由信号，无错误。
/// 被 bench_baseline.rs 和 pipeline_benchmark.rs 共享。
pub struct NoopInvoker;

#[async_trait]
impl PluginInvoker for NoopInvoker {
    async fn invoke_pipeline_plugin(
        &self,
        _plugin_id: &str,
        _ctx: &PluginContext,
    ) -> Result<PluginResult, PluginError> {
        let mut updates = HashMap::new();
        updates.insert("bench".to_string(), json!("ok"));
        Ok(PluginResult {
            state_updates: updates,
            route_signal: None,
            skip_remaining: false,
            error: None,
        })
    }

    async fn invoke_tool(
        &self,
        _: &str,
        _: &str,
        _: &serde_json::Value,
    ) -> Result<agentos_core::types::ToolExecutionResult, PluginError> {
        Ok(agentos_core::types::ToolExecutionResult::success(json!({})))
    }

    async fn send_lifecycle_hook(
        &self,
        _: &str,
        _: LifecycleHook,
        _: &HookContext,
    ) -> Result<(), PluginError> {
        Ok(())
    }
}

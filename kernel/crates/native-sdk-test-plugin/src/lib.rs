//! 测试用原生插件（cdylib，直接 trait 对象）——验证 NativePluginLoader 加载链路。
//!
//! `impl PipelinePlugin` + `agentos_plugin_create` 构造函数，行为：
//! - pipeline 语义（无 `tool_call` 字段）：回显输入 state，并在 state_updates 写入
//!   `processed_by: "test_plugin"`；
//! - 工具调用语义（B2：`tool_call` 约定字段）：返回 ToolExecutionResult 形状 JSON，
//!   回显工具名 + 入参。

use std::collections::HashMap;

use agentos_native_sdk::{plugin_into_raw, ExecContext, PipelinePlugin};

/// 测试插件：回显 state + 标记 processed_by。
pub struct TestPlugin;

impl PipelinePlugin for TestPlugin {
    fn execute(&self, ectx: &ExecContext) -> Result<String, String> {
        // B2：tool_call 约定字段 → 工具调用语义（与 pipeline 同一 execute 入口）。
        if let Some(tool_call) = ectx.ctx.tool_call_value() {
            let name = tool_call
                .get("name")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown_tool");
            let args = ectx.ctx.state_value();
            return serde_json::to_string(&serde_json::json!({
                "success": true,
                "data": {"tool": name, "echo_args": args},
            }))
            .map_err(|e| format!("serialize: {e}"));
        }

        let state = ectx.ctx.state_value();
        let mut updates: HashMap<String, serde_json::Value> = HashMap::new();
        updates.insert("processed_by".to_string(), serde_json::json!("test_plugin"));
        updates.insert("echoed_state".to_string(), state);
        if !ectx.ctx.tenant_id.is_empty() {
            updates.insert(
                "tenant".to_string(),
                serde_json::json!(ectx.ctx.tenant_id.as_str()),
            );
        }
        if ectx.host.is_some() {
            updates.insert("host_available".to_string(), serde_json::json!(true));
        }
        serde_json::to_string(&updates).map_err(|e| format!("serialize: {e}"))
    }
}

/// 构造函数（extern "C"）：内核 dlopen 后调它拿 trait 对象裸指针。
#[no_mangle]
pub extern "C" fn agentos_plugin_create() -> *mut () {
    plugin_into_raw(TestPlugin)
}

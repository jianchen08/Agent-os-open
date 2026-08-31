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
/// 测试插件：回显 state + 标记 processed_by。
///
/// 跨分配器契约：execute 结果存自持缓冲（dll 堆）借 `&str` 给内核，不返回
/// String（内核 drop = 跨堆 free UB）。blocking 线程串行调用。
pub struct TestPlugin {
    out_buf: std::cell::UnsafeCell<String>,
}

impl TestPlugin {
    pub fn new() -> Self {
        Self {
            out_buf: std::cell::UnsafeCell::new(String::with_capacity(16 * 1024)),
        }
    }

    /// 写自持缓冲并借出（&str 绑 &self，调用方同步拷贝消费）。
    fn write_out(&self, json: String) -> &str {
        let buf = unsafe { &mut *self.out_buf.get() };
        *buf = json;
        buf.as_str()
    }
}

impl PipelinePlugin for TestPlugin {
    fn execute(&self, ectx: &ExecContext) -> Result<&str, String> {
        // B2：tool_call 约定字段 → 工具调用语义（与 pipeline 同一 execute 入口）。
        if let Some(tool_call) = ectx.ctx.tool_call_value() {
            let name = tool_call
                .get("name")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown_tool");
            let args = ectx.ctx.state_value();
            let json = serde_json::to_string(&serde_json::json!({
                "success": true,
                "data": {"tool": name, "echo_args": args},
            }))
            .map_err(|e| format!("serialize: {e}"))?;
            return Ok(self.write_out(json));
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
        let json = serde_json::to_string(&updates).map_err(|e| format!("serialize: {e}"))?;
        Ok(self.write_out(json))
    }
}

// SAFETY: out_buf 仅在 execute（blocking 线程串行，单实例不重入）的 &self 独占
// 借用期写入；借出的 &str 由调用方同步拷贝消费，无并发写面。
unsafe impl Send for TestPlugin {}
unsafe impl Sync for TestPlugin {}

/// 构造函数（extern "C"）：内核 dlopen 后调它拿 trait 对象裸指针。
#[no_mangle]
pub extern "C" fn agentos_plugin_create() -> *mut () {
    plugin_into_raw(TestPlugin::new())
}

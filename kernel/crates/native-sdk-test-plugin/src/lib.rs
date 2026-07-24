//! 测试用原生插件（cdylib）——NativePluginLoader 集成测试加载它。
//!
//! 用 `plugin_entry!` 宏注册入口，行为：把输入 state 透传一份，并在 state_updates
//! 写入 `"echoed": <input.state>` 和 `"processed_by": "test_plugin"`。
//!
//! 不写 panic 测试用例插件（panic 路径在 loader 侧 catch_unwind 测，另用专门的
//! panic 插件 crate 过度——本测试插件只覆盖正常路径）。

use agentos_native_sdk::{plugin_entry, PluginInput, PluginResult};
use std::collections::HashMap;

plugin_entry!(|input: PluginInput| -> PluginResult {
    let mut updates: HashMap<String, serde_json::Value> = HashMap::new();
    updates.insert("processed_by".to_string(), serde_json::json!("test_plugin"));
    // 回显输入 state，供测试断言
    updates.insert("echoed_state".to_string(), input.state.clone());
    if !input.tenant_id.is_empty() {
        updates.insert("tenant".to_string(), serde_json::json!(input.tenant_id));
    }
    PluginResult::ok().with_state_updates(updates)
});

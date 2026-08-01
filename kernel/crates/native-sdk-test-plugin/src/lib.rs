//! 测试用原生插件（cdylib，abi_stable trait 对象）——验证 NativePluginLoader 加载链路。
//!
//! `impl PipelinePlugin` + `#[export_root_module]`，行为：回显输入 state，并在
//! state_updates 写入 `processed_by: "test_plugin"`。

use std::collections::HashMap;

use abi_stable::export_root_module;
use abi_stable::prefix_type::PrefixTypeTrait;
use abi_stable::std_types::{RBox, RResult, RString};
use agentos_native_sdk::{
    create_plugin_value, NativePluginModule, NativePluginModule_Ref, PipelinePlugin,
    PipelinePlugin_TO, PluginCtx,
};

/// 测试插件：回显 state + 标记 processed_by。
pub struct TestPlugin;

impl PipelinePlugin for TestPlugin {
    fn execute(&self, ctx: &PluginCtx) -> RResult<RString, RString> {
        let state = ctx.state_value();
        let mut updates: HashMap<String, serde_json::Value> = HashMap::new();
        updates.insert("processed_by".to_string(), serde_json::json!("test_plugin"));
        updates.insert("echoed_state".to_string(), state);
        if !ctx.tenant_id.as_str().is_empty() {
            updates.insert("tenant".to_string(), serde_json::json!(ctx.tenant_id.as_str()));
        }
        if let abi_stable::std_types::ROption::RSome(_) = &ctx.host {
            updates.insert("host_available".to_string(), serde_json::json!(true));
        }
        match serde_json::to_string(&updates) {
            Ok(json) => RResult::ROk(RString::from(json)),
            Err(e) => RResult::RErr(RString::from(format!("serialize: {e}"))),
        }
    }
}

/// 构造函数：返回 PipelinePlugin trait 对象（extern "C"，供 RootModule 函数指针字段）。
extern "C" fn create_test_plugin() -> PipelinePlugin_TO<'static, RBox<()>> {
    create_plugin_value(TestPlugin)
}

// 导出 root module：内核经 abi_stable 加载此 cdylib 拿 create_plugin 构造函数。
// 返回 _Ref 类型（PrefixTypeTrait::leak_into_prefix 把 struct 转 FFI-safe 静态引用）。
#[export_root_module]
pub fn get_library() -> NativePluginModule_Ref {
    NativePluginModule {
        create_plugin: create_test_plugin,
    }
    .leak_into_prefix()
}

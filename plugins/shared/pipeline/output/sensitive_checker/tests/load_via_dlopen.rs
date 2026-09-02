//! 集成验证：经 dlopen + box_from_raw 加载本 crate 的 release cdylib，
//! 走与生产 NativePluginLoader（kernel/crates/plugin-loader/src/native_loader.rs）
//! 完全一致的契约路径，确认：
//!   1. 产物导出 `agentos_plugin_create` 符号；
//!   2. 双重 Box 裸指针可被 box_from_raw 正确还原为 trait 对象；
//!   3. execute 在真实 state 输入下产出预期脱敏结果。
//!
//! 前置：`cargo build --release` 必须先跑过，使 target/release 下存在产物。

use std::path::PathBuf;

use agentos_native_sdk::{box_from_raw, ExecContext, HostServices, PluginCtx};
use libloading::{Library, Symbol};

/// 构造函数签名：返回双重 Box 裸指针（实为 `Box<Box<dyn PipelinePlugin>>`）。
type CreateFn = unsafe extern "C" fn() -> *mut ();

/// 产物 dll 路径：本 crate 的 release 输出。
fn dll_path() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("target/release/pipeline_sensitive_checker_native.dll");
    p
}

/// 假 HostServices（本插件不调 capability，host=None 也能工作；这里仅用于占位）。
struct NoopHost;
impl HostServices for NoopHost {
    fn call_capability(&self, _: &str, _: &str, _: &str) -> Result<&str, &str> {
        Err("not implemented")
    }
}

#[test]
fn load_and_execute_masks_openai_key() {
    let path = dll_path();
    assert!(
        path.exists(),
        "release dll missing at {} — run `cargo build --release` first",
        path.display()
    );

    // SAFETY: 与 NativePluginLoader::load_inner 等价——dlopen + 符号解析 + box_from_raw。
    unsafe {
        let lib = Library::new(&path).expect("dlopen failed");
        let create_fn: CreateFn = {
            let sym: Symbol<CreateFn> = lib
                .get(agentos_native_sdk::CREATE_FN_NAME)
                .expect("symbol agentos_plugin_create missing");
            *sym
        };
        let ptr = create_fn();
        assert!(!ptr.is_null(), "create returned null");
        let plugin = box_from_raw(ptr).expect("box_from_raw returned None");

        // 注意：lib 必须在 plugin 之后 drop（trait 对象的代码指向 lib 内）。
        // 这里把 lib 装进一个结构体延长生命周期——但本测试末尾顺序是 plugin 先 drop。
        let state = serde_json::json!({
            "tool_results": [{"output": "leaked sk-abcdef1234567890ABCDEF1234 here"}]
        });
        let ectx = ExecContext {
            ctx: PluginCtx {
                state_json: serde_json::to_string(&state).unwrap(),
                ..Default::default()
            },
            host: Some(&NoopHost),
        };
        let out = plugin.execute(&ectx).expect("execute returned error");
        let updates: std::collections::HashMap<String, serde_json::Value> =
            serde_json::from_str(&out).expect("state_updates parse failed");

        assert_eq!(updates["sensitive_detected"], serde_json::Value::Bool(true));
        let results = updates["tool_results"].as_array().expect("tool_results array");
        assert_eq!(results[0]["output"], "leaked *** here");
    }
}

#[test]
fn load_and_execute_clean_input_returns_empty_updates() {
    let path = dll_path();
    assert!(path.exists(), "release dll missing");

    unsafe {
        let lib = Library::new(&path).expect("dlopen failed");
        let create_fn: CreateFn = {
            let sym: Symbol<CreateFn> = lib
                .get(agentos_native_sdk::CREATE_FN_NAME)
                .expect("symbol missing");
            *sym
        };
        let plugin = box_from_raw(create_fn()).expect("box_from_raw None");

        let state = serde_json::json!({"tool_results": [{"output": "clean"}]});
        let ectx = ExecContext {
            ctx: PluginCtx {
                state_json: serde_json::to_string(&state).unwrap(),
                ..Default::default()
            },
            host: None,
        };
        let out = plugin.execute(&ectx).expect("execute error");
        let updates: std::collections::HashMap<String, serde_json::Value> =
            serde_json::from_str(&out).expect("parse failed");
        assert!(updates.is_empty(), "clean input should produce no updates");
    }
}

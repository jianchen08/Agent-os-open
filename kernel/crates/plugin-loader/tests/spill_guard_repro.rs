//! 复现#9：多线程 tokio + spawn_blocking + host=Some + 真机序列（最接近真机装配）。
use agentos_native_sdk::{HostServices, PluginCtx};
use agentos_plugin_loader::native_loader::NativePluginLoader;
use std::sync::Arc;

#[derive(Clone)]
struct ReplyOk;
impl HostServices for ReplyOk {
    fn call_capability(&self, capability: &str, method: &str, params: &str) -> Result<String, String> {
        eprintln!("[host] {} {} {} bytes", capability, method, params.len());
        if capability == "tool-executor" {
            return Ok(r#"{"success":true,"data":{"content":"hello tool result"},"tool":{"name":"file_read"}}"#.into());
        }
        Ok(r#"{"ok":true}"#.into())
    }
}

static GLOBAL_ALLOC: mimalloc::MiMalloc = mimalloc::MiMalloc;

#[test]
fn tool_round_tokio_blocking_no_segfault() {
    let base = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../plugins/shared");
    let tc = std::path::Path::new(base).join("pipeline/core/tool_core/pipeline_tool_core_native.dll");
    let sg = std::path::Path::new(base).join("pipeline/output/spill_guard/pipeline_spill_guard_native.dll");
    let loader = Arc::new(NativePluginLoader::new());
    loader.load("pipeline_tool_core", &tc).expect("load tc");
    loader.load("pipeline_spill_guard", &sg).expect("load sg");

    let rt = Arc::new(
        tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .unwrap(),
    );
    let state = std::fs::read_to_string(r"D:\crashdumps_kernel\big_state.json").unwrap();

    let ctx_tc = PluginCtx {
        state_json: state.clone(),
        config_json: "{}".into(),
        tenant_id: "default".into(),
        session_id: "t".into(),
        task_id: String::new(),
        pipeline_id: "a33573e5ee2f".into(),
        tool_call_json: Some(r#"{"name":"file_read"}"#.into()),
    };

    for i in 0..3 {
        eprintln!("=== round {} ===", i);
        {
            let l = Arc::clone(&loader);
            let ctx = ctx_tc.clone();
            let host = ReplyOk;
            let out = Arc::clone(&rt).block_on(async {
                tokio::task::spawn_blocking(move || loader_clone_execute(l, ctx, host))
                    .await
                    .expect("join")
            });
            eprintln!("tool_core -> {} bytes", out.len());
        }
        {
            let host = ReplyOk;
            let out = loader.execute("pipeline_spill_guard", &ctx_tc, Some(&host)).expect("spill ok");
            eprintln!("spill_guard -> {} bytes", out.len());
        }
    }
}

fn loader_clone_execute(
    loader: Arc<NativePluginLoader>,
    ctx: PluginCtx,
    host: ReplyOk,
) -> String {
    loader
        .execute("pipeline_tool_core", &ctx, Some(&host))
        .expect("tool_core ok")
}

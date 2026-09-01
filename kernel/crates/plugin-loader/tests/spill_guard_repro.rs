//! 复现#9：多线程 tokio + spawn_blocking + host=Some + 真机序列（最接近真机装配）。
use agentos_native_sdk::{HostServices, PluginCtx};
use agentos_plugin_loader::native_loader::NativePluginLoader;
use std::sync::Arc;

#[derive(Clone)]
struct ReplyOk;
impl HostServices for ReplyOk {
    fn call_capability(
        &self,
        capability: &str,
        method: &str,
        params: &str,
    ) -> Result<&str, String> {
        eprintln!("[host] {} {} {} bytes", capability, method, params.len());
        if capability == "tool-executor" {
            // 借用协议（跨分配器契约）：返回实现方持有的 &str。
            // 单调用串行 + &self 生命周期，static 推导安全（测试桩数据不可变）。
            if capability == "tool-executor" {
                return Ok(
                    r#"{"success":true,"data":{"content":"hello tool result"},"tool":{"name":"file_read"}}"#,
                );
            }
        }
        Ok(r#"{"ok":true}"#)
    }
}

// 取证期对齐真机 mimalloc 分配器用；当前断言不依赖，保留待再次取证启用
#[allow(dead_code)]
static GLOBAL_ALLOC: mimalloc::MiMalloc = mimalloc::MiMalloc;

#[test]
fn tool_round_tokio_blocking_no_segfault() {
    // Windows-only SEGV 复现/回归：native cdylib 产物（.dll）不入库（.gitignore
    // *.dll），CI runner 无产物也无 D:\crashdumps_kernel 大状态文件——该测试
    // 依赖取证期本机资产，非 Windows 环境直接跳过（与 rust-coverage 注释的
    // STATUS_ACCESS_VIOLATION 容错同族）。
    if !cfg!(windows) {
        eprintln!("skip: Windows-only native cdylib SEGV repro（CI 无 dll 产物/取证资产）");
        return;
    }
    let base = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../plugins/shared");
    let tc =
        std::path::Path::new(base).join("pipeline/core/tool_core/pipeline_tool_core_native.dll");
    let sg = std::path::Path::new(base)
        .join("pipeline/output/spill_guard/pipeline_spill_guard_native.dll");
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
            let out = loader
                .execute("pipeline_spill_guard", &ctx_tc, Some(&host))
                .expect("spill ok");
            eprintln!("spill_guard -> {} bytes", out.len());
        }
    }
}

fn loader_clone_execute(loader: Arc<NativePluginLoader>, ctx: PluginCtx, host: ReplyOk) -> String {
    loader
        .execute("pipeline_tool_core", &ctx, Some(&host))
        .expect("tool_core ok")
}

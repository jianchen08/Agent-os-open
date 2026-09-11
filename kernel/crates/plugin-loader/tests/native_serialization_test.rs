// @feature: FP-0.2.插件加载器 native 并发安全 | @vision: V1 可靠性 | @ci: rust-test
//! native 插件并发安全回归：
//!
//! 1. load single-flight：并发首载同一 plugin_id 全部拿到同一实例（无双
//!    dlopen 泄漏）；
//! 2. 同实例 execute 并发调用经 exec_lock 串行——两个 spawn_blocking 并发
//!    执行，各自结果完整且只含本调用输入（无缓冲串味），无 panic。
//!
//! 测试插件 = agentos-native-sdk-test-plugin（UnsafeCell 返回缓冲，可检测
//! 并发串味）。cdylib 产物不入库（.gitignore）：优先取 workspace target 下
//! 既有产物，缺失时 cargo build 一次；仍不可得则 SKIP（与 invoker 的
//! e2e_native 同款诚实降级）。

use agentos_native_sdk::PluginCtx;
use agentos_plugin_loader::native_loader::NativePluginLoader;
use std::path::PathBuf;
use std::sync::Arc;

fn workspace_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .expect("workspace root (kernel/) resolvable")
}

fn locate_cdylib() -> Option<PathBuf> {
    let target_dir = std::env::var_os("CARGO_TARGET_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| workspace_root().join("target"));
    for profile in ["debug", "release"] {
        let dir = target_dir.join(profile);
        if let Some(p) =
            NativePluginLoader::resolve_artifact(&dir, "agentos_native_sdk_test_plugin.dll")
        {
            return Some(p);
        }
    }
    None
}

/// 进程级单次解析：并发测试共享一次构建/定位结果（Windows 上 dll 被加载期间
/// 不能重写文件，二次 build 会撞文件锁）。
static CDYLIB: std::sync::OnceLock<Option<PathBuf>> = std::sync::OnceLock::new();

/// 定位（必要时构建）test-plugin cdylib；不可得返回 None（调用方 SKIP）。
///
/// 命中产物前**强制增量重编**：cdylib 与测试 exe 必须同源编译（native FFI
/// 对称借用契约，见 scripts/check_native_artifacts_sync.py 的同源检查）——
/// target 下遗留的旧产物与新 exe 布局不同源会 SIGSEGV/乱分配，不可直接信任。
fn ensure_cdylib() -> Option<PathBuf> {
    CDYLIB
        .get_or_init(|| {
            let cargo = std::env::var_os("CARGO").unwrap_or_else(|| "cargo".into());
            let status = std::process::Command::new(cargo)
                .args(["build", "-p", "agentos-native-sdk-test-plugin", "--quiet"])
                .current_dir(workspace_root())
                .status()
                .ok()?;
            if !status.success() {
                return None;
            }
            locate_cdylib()
        })
        .clone()
}

fn make_ctx(tenant: &str, state: serde_json::Value) -> PluginCtx {
    PluginCtx {
        state_json: state.to_string(),
        config_json: "{}".into(),
        tenant_id: tenant.to_string(),
        session_id: "s".into(),
        task_id: "task".into(),
        pipeline_id: "p".into(),
        tool_call_json: None,
    }
}

/// 并发首载：全部调用方拿到同一实例（single-flight，无双 dlopen 泄漏）。
#[tokio::test(flavor = "multi_thread")]
async fn concurrent_load_same_id_yields_single_instance() {
    let Some(dll) = ensure_cdylib() else {
        eprintln!("SKIP: agentos-native-sdk-test-plugin cdylib 不可得（build 失败）");
        return;
    };
    let loader = Arc::new(NativePluginLoader::new());
    let mut handles = Vec::new();
    for _ in 0..8 {
        let loader = Arc::clone(&loader);
        let path = dll.clone();
        handles.push(tokio::task::spawn_blocking(move || {
            loader.load("single_flight_target", &path)
        }));
    }
    let mut arcs = Vec::new();
    for h in handles {
        arcs.push(h.await.expect("load 无 panic").expect("load ok"));
    }
    assert!(loader.is_loaded("single_flight_target"));
    assert_eq!(
        loader.list_loaded().len(),
        1,
        "同一 plugin_id 只应有一个表项"
    );
    for a in &arcs {
        assert!(
            Arc::ptr_eq(a, &arcs[0]),
            "并发 load 必须命中同一实例（single-flight）"
        );
    }
}

/// 并发 execute（同实例）：结果完整且只含本调用输入——UnsafeCell 返回缓冲
/// 在无串行化保障时会被并发覆写串味（UB 检测放大：2 线程 × 200 轮交替写读）。
#[tokio::test(flavor = "multi_thread")]
async fn concurrent_execute_serialized_and_correct() {
    let Some(dll) = ensure_cdylib() else {
        eprintln!("SKIP: agentos-native-sdk-test-plugin cdylib 不可得（build 失败）");
        return;
    };
    let loader = Arc::new(NativePluginLoader::new());
    loader.load("echo", &dll).expect("test plugin loads");

    const ROUNDS: usize = 200;
    let mut handles = Vec::new();
    for tid in 0..2 {
        let loader = Arc::clone(&loader);
        handles.push(tokio::task::spawn_blocking(move || {
            for i in 0..ROUNDS {
                let state = serde_json::json!({ "round": i, "payload": format!("from-{tid}") });
                let ctx = make_ctx(&format!("tenant-{tid}"), state);
                let out = loader
                    .execute("echo", &ctx, None)
                    .expect("execute ok (no panic escapes loader)");
                let v: serde_json::Value = serde_json::from_str(&out).expect("合法 JSON 输出");
                // 串行性 ⇒ 本调用的回显完整且只含本线程输入（无跨线程串味）
                assert_eq!(
                    v["echoed_state"]["payload"],
                    format!("from-{tid}"),
                    "返回缓冲被并发覆写（串行化失效）"
                );
                assert_eq!(
                    v["echoed_state"]["round"],
                    serde_json::json!(i),
                    "回显轮次错乱"
                );
                assert_eq!(v["tenant"], format!("tenant-{tid}"), "租户串味");
            }
        }));
    }
    for h in handles {
        h.await.expect("并发 execute 无 panic");
    }
}

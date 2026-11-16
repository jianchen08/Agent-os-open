//! B1 验收三断言：插件目录活真源（ADR 2026-10-15-mode-registry-retirement §B1）。
//!
//! 退役前的 boot 快照 `AppState.plugin_dirs` 在热发现新插件后三类消费路径全断
//! （直到重启）：web 静态资源 404、配置保存 500、validate-all 盲区。本文件锁
//! 活真源语义——同一 loader 实例补一轮 discover（watcher 每轮 sync 的等价物）
//! 后，新插件的三类消费**零重启即时可用**。

use std::sync::Arc;

use agentos_api::routes::{
    get_plugin_config_handler, put_plugin_config_handler, validate_all_plugins_handler, AppState,
    PluginConfigUpdateRequest,
};
use agentos_api::server::build_router;
use agentos_core::traits::{
    HttpHandleCapability, HttpHandleRequest, HttpHandleResponse, PluginInvoker, PluginLoader,
    PluginManifest,
};
use agentos_plugin_loader::{CapabilityRegistryImpl, PluginLoaderImpl};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

const HOT_ID: &str = "p_hot_live";

struct NopHandler;

#[async_trait::async_trait]
impl HttpHandleCapability for NopHandler {
    async fn handle(&self, _req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
        Ok(HttpHandleResponse {
            status: 200,
            headers: std::collections::HashMap::new(),
            body: String::new(),
            body_encoding: "base64".to_string(),
        })
    }
}

/// validate-all 的 invoker 桩：工具清单 = 注册表声明的空集（磁盘一致面）。
struct EmptyListInvoker;

#[async_trait::async_trait]
impl PluginInvoker for EmptyListInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        _ctx: &agentos_core::types::PluginContext<'a>,
    ) -> Result<agentos_core::types::PluginResult, agentos_core::types::PluginError> {
        unimplemented!("本测试不触达管道插件调用")
    }
    async fn invoke_tool(
        &self,
        _plugin_id: &str,
        _tool_name: &str,
        _inputs: &Value,
    ) -> Result<agentos_core::types::ToolExecutionResult, agentos_core::types::PluginError> {
        unimplemented!("本测试不触达工具调用")
    }
    async fn send_lifecycle_hook(
        &self,
        _plugin_id: &str,
        _hook: agentos_core::traits::LifecycleHook,
        _context: &agentos_core::traits::HookContext,
    ) -> Result<(), agentos_core::types::PluginError> {
        Ok(())
    }
    async fn list_plugin_tools(
        &self,
        _plugin_id: &str,
    ) -> Result<Value, agentos_core::types::PluginError> {
        Ok(json!({"tools": [{"name": "probe_tool", "description": "probe_tool"}]}))
    }
}

/// 写一个可被 loader 发现的最小插件包（plugin.json + web/ + 内联配置声明）。
fn write_plugin(root: &std::path::Path, id: &str) -> std::path::PathBuf {
    let pkg = root.join(id);
    std::fs::create_dir_all(pkg.join("web")).unwrap();
    std::fs::write(pkg.join("plugin.json"), plugin_json(id)).unwrap();
    std::fs::write(pkg.join("web").join("index.html"), "<h1>live</h1>").unwrap();
    pkg
}

fn plugin_json(id: &str) -> String {
    json!({
        "id": id, "name": id, "version": "1.0.0",
        "plugin_type": "tool", "language": "python",
        "host_type": "sidecar", "entry": "python server.py",
        "capabilities": {
            "tools": [{"name": "probe_tool", "description": "probe_tool"}]
        },
        "config_files": [{
            "id": "settings",
            "label": "设置",
            "fields": [
                {"name": "threshold", "label": "阈值", "type": "number", "default": 0.5}
            ]
        }]
    })
    .to_string()
}

fn base_state(loader: Arc<PluginLoaderImpl>, root: &std::path::Path) -> AppState {
    let mut state = AppState::new();
    state.capability_registry = Some(Arc::new(CapabilityRegistryImpl::new()));
    state.http_handler = Some(Arc::new(NopHandler));
    state.plugin_loader = Some(loader);
    state.project_root = Some(root.to_path_buf());
    state
}

async fn get_body(app: axum::Router, uri: &str) -> (StatusCode, Value) {
    let resp = app
        .clone()
        .oneshot(Request::builder().uri(uri).body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

#[tokio::test]
async fn hot_registered_plugin_serves_assets_config_and_validate_all_without_restart() {
    let tmp = tempfile::tempdir().unwrap();
    let _guard = agentos_api::test_env::pin_user_root(tmp.path());

    // boot：只有 p_live；同一 loader 实例贯穿全程（生产装配同款活真源句柄）
    write_plugin(tmp.path(), "p_live");
    let loader = Arc::new(PluginLoaderImpl::new(tmp.path(), None));
    loader.discover(&[]).await.expect("boot discover");
    assert!(loader.plugin_dir("p_live").is_some());

    // ── 热注册：盘上新增 p_hot（目录 + plugin.json + web/），零重启 ──
    write_plugin(tmp.path(), HOT_ID);
    loader.discover(&[]).await.expect("hot discover");
    assert!(
        loader.plugin_dir(HOT_ID).is_some(),
        "同实例补 discover 后热插件必须进活路径表"
    );

    let mut state = base_state(loader.clone(), tmp.path());
    // 注册面（manifests）同步热插件——watcher 每轮 sync 的等价物
    let hot_manifest: PluginManifest = serde_json::from_str(&plugin_json(HOT_ID)).unwrap();
    *state.manifests.write().await = vec![hot_manifest];
    state.invoker = Some(Arc::new(EmptyListInvoker) as Arc<dyn PluginInvoker>);
    // validate-all 只巡检启用集插件
    state.enabled_plugin_ids =
        Arc::new(tokio::sync::RwLock::new(std::collections::HashSet::from([
            HOT_ID.to_string(),
        ])));

    // 断言一：热插件的 web 静态资源 200（旧 boot 快照形态下 404 直到重启）
    let app = build_router(state.clone());
    let (status, _) = get_body(app, &format!("/ext/{HOT_ID}/assets/index.html")).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "热注册插件 /ext/assets 必须 200（活真源现取）"
    );

    // 断言二：热插件配置读 + 保存 200 且写回磁盘（旧形态下 500：目录映射缺失）
    let got = get_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path((HOT_ID.to_string(), "settings".to_string())),
    )
    .await
    .expect("热插件配置 GET 应成功");
    let put = put_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path((HOT_ID.to_string(), "settings".to_string())),
        axum::Json(PluginConfigUpdateRequest {
            data: json!({"threshold": 0.9}),
            if_match: Some(got.0.etag.clone()),
        }),
    )
    .await
    .expect("热插件配置保存应 200（活真源定位磁盘 manifest）");
    assert!(
        put.0["etag"].is_string(),
        "保存成功必须返回新 ETag: {:?}",
        put.0
    );
    let on_disk = std::fs::read_to_string(tmp.path().join(HOT_ID).join("plugin.json")).unwrap();
    assert!(
        on_disk.contains("0.9"),
        "保存值应写回磁盘 manifest: {on_disk}"
    );

    // 断言三：validate-all 覆盖热插件且磁盘一致（旧形态下 disk_manifest_unreadable 盲区）
    let resp = validate_all_plugins_handler(axum::extract::State(state)).await;
    let reports = resp.0["consistency_reports"].as_array().unwrap();
    assert!(
        reports.iter().all(|r| r["plugin_id"] != HOT_ID),
        "热插件磁盘一致不得出差异报告: {reports:?}"
    );
    assert_eq!(resp.0["registry_disk_mismatches"], 0);
    assert_eq!(
        resp.0["clean"], 1,
        "validate-all 必须覆盖到热插件（磁盘 manifest 可读）"
    );
}

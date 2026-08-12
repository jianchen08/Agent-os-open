//! 阶段2(后端):/api/v1/schema 的 plugin_contributes 原样透传 manifest.contributes(TDD)。
//!
//! 内核不解释 contributes 结构(ADR §3.4/§六),前端 ContributionRegistry
//! (`frontend/src/services/schema/ContributionRegistry.ts` 的 registerFromSchema)
//! 遍历 contributes 的任意 key。统一架构协议里插件声明 `contributes.pages[]`
//! (page 声明,含 space/slot/schema/layout/widget/detachable 等字段)。
//! 本测试锁定透传链路:pages 声明必须原样到达响应,字段不丢。

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use agentos_core::traits::{HostType, PluginManifest, PluginType};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use std::collections::HashSet;
use std::sync::Arc;
use tokio::sync::RwLock;
use tower::ServiceExt;

fn manifest_with_contributes(plugin_id: &str, contributes: Option<Value>) -> PluginManifest {
    PluginManifest {
        id: plugin_id.to_string(),
        name: plugin_id.to_string(),
        version: "1.0.0".to_string(),
        plugin_type: PluginType::System,
        pipeline_role: None,
        language: "python".to_string(),
        host_type: HostType::Sidecar,
        entry: "python server.py".to_string(),
        capabilities: Default::default(),
        dependencies: vec![],
        permissions: Default::default(),
        error_policy: Default::default(),
        priority: 100,
        mcp: None,
        native: None,
        wasm: None,
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        // 基线要求:PluginManifest 字面量必须带 provides 字段(见 core/src/traits.rs:825)
        provides: None,
    }
}

async fn fetch_schema(manifests: Vec<PluginManifest>, enabled_ids: HashSet<String>) -> Value {
    let state = AppState::with_config(json!({}));
    // 注入 manifests + enabled_plugin_ids(L1 过滤依赖它,缺省空集会导致 contributes 不出口)
    let state = AppState {
        manifests: Arc::new(manifests),
        enabled_plugin_ids: Arc::new(RwLock::new(enabled_ids)),
        ..state
    };
    let app = build_router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/schema")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 16384).await.unwrap();
    serde_json::from_slice(&body).unwrap()
}

#[tokio::test]
async fn schema_passes_contributes_pages_verbatim() {
    // 统一架构协议:插件在 plugin.json 声明 contributes.pages[](page 声明)。
    // 覆盖协议字段:space/slot/schema/layout/widget/detachable。
    let pages = json!([
        {
            "id": "dashboard",
            "title": "仪表盘",
            "space": "main",
            "slot": "primary",
            "schema": {
                "type": "object",
                "properties": {"greeting": {"type": "string"}}
            },
            "layout": {"width": "full"},
            "widget": "dashboard_grid",
            "detachable": true,
        }
    ]);
    // 混入一个非 pages 的任意 key:验证整个 contributes 对象原样透传(不只挑已知 key)
    let contributes = json!({
        "pages": pages.clone(),
        "quick_actions": [{"id": "new_chat", "label": "新建会话"}],
    });

    let manifests = vec![manifest_with_contributes(
        "approval_ui",
        Some(contributes.clone()),
    )];
    let schema = fetch_schema(manifests, HashSet::from(["approval_ui".to_string()])).await;

    let plugin_contributes = schema["plugin_contributes"]
        .as_array()
        .expect("plugin_contributes array missing");
    assert_eq!(plugin_contributes.len(), 1, "got: {plugin_contributes:?}");

    let entry = &plugin_contributes[0];
    assert_eq!(entry["plugin_id"], "approval_ui");
    assert_eq!(entry["plugin_name"], "approval_ui");

    // 关键断言 1:contributes.pages 原样存在,声明字段不丢
    assert_eq!(
        entry["contributes"]["pages"],
        pages,
        "contributes.pages 应原样透传,声明字段不丢"
    );
    // 关键断言 2:整个 contributes 对象 == 原声明(任意新增 key 都透传)
    assert_eq!(entry["contributes"], contributes, "contributes 应整体原样透传");
}

#[tokio::test]
async fn schema_omits_contributes_for_disabled_plugin() {
    // L1 安装触发模型:disabled 插件的 contributes 不出口(routes.rs:372 注释)。
    // 这决定 pages 透传的前提:插件必须处于 enabled 集合。
    let contributes = json!({
        "pages": [{"id": "p", "title": "P", "space": "main"}]
    });
    let manifests = vec![manifest_with_contributes("disabled_ui", Some(contributes))];
    let schema = fetch_schema(manifests, HashSet::new()).await;

    let plugin_contributes = schema["plugin_contributes"]
        .as_array()
        .expect("plugin_contributes array missing");
    assert!(
        plugin_contributes.is_empty(),
        "disabled 插件的 contributes(含 pages)不应出口, got: {plugin_contributes:?}"
    );
}

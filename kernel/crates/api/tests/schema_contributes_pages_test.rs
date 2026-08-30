// @feature: FP-0.2.四 前端Schema | @vision: V6 可即用 | @ci: rust-test
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
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::System,
        pipeline_role: None,
        language: "python".to_string(),
        host_type: HostType::Sidecar,
        host_group: None,
        entry: "python server.py".to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        priority: 100,
        mcp: None,
        lifecycle: None,
        native: None,
        granted_capabilities: vec![],
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        export_fields: vec![],
        // 基线要求:PluginManifest 字面量必须带 provides 字段(见 core/src/traits.rs:825)
        provides: None,
    }
}

async fn fetch_schema(manifests: Vec<PluginManifest>, enabled_ids: HashSet<String>) -> Value {
    let state = AppState::with_config(json!({}));
    // 注入 manifests + enabled_plugin_ids(L1 过滤依赖它,缺省空集会导致 contributes 不出口)
    let state = AppState {
        manifests: Arc::new(RwLock::new(manifests)),
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
        entry["contributes"]["pages"], pages,
        "contributes.pages 应原样透传,声明字段不丢"
    );
    // 关键断言 2:整个 contributes 对象 == 原声明(任意新增 key 都透传)
    assert_eq!(
        entry["contributes"], contributes,
        "contributes 应整体原样透传"
    );
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

#[tokio::test]
async fn schema_passes_visual_contributes_themes_and_client_styles_verbatim() {
    // 插件前端定制化(任务域):contributes.themes / client_styles 是纯数据视觉贡献,
    // 内核不解释结构(与 pages 同规则)——必须原样到达响应,前端 ContributionRegistry
    // 的旁路注册表承接(不归一化为页面)。
    let visual = json!({
        "themes": [{
            "id": "gold-lace",
            "name": "金色蕾丝",
            "base": "dark",
            "variables": {"--ds-accent-primary": "#D4AF37"},
            "backgrounds": {"image": {"enabled": false}}
        }],
        "client_styles": [{
            "id": "gold-lace-border",
            "path": "/assets/border.css",
            "scope": "global",
            "description": "金色蕾丝边框"
        }]
    });

    let manifests = vec![manifest_with_contributes(
        "visual_customization_demo",
        Some(visual.clone()),
    )];
    let schema = fetch_schema(
        manifests,
        HashSet::from(["visual_customization_demo".to_string()]),
    )
    .await;

    let entry = &schema["plugin_contributes"][0];
    // 关键断言:themes / client_styles 原样透传,字段不丢(变量值、背景开关、scope 都保真)
    assert_eq!(
        entry["contributes"]["themes"], visual["themes"],
        "contributes.themes 应原样透传"
    );
    assert_eq!(
        entry["contributes"]["client_styles"], visual["client_styles"],
        "contributes.client_styles 应原样透传"
    );
}

#[tokio::test]
async fn schema_omits_visual_contributes_for_disabled_plugin() {
    // 禁用语义(任务验收):插件禁用 → contributes 不出口 → 前端重载 schema 后
    // 主题从列表移除、注入 CSS 清理(与 pages 同规则)。
    let visual = json!({
        "themes": [{"id": "gold-lace", "name": "金色蕾丝", "base": "dark"}],
        "client_styles": [{"id": "gold-lace-border", "path": "/assets/border.css"}]
    });
    let manifests = vec![manifest_with_contributes("visual_off", Some(visual))];
    let schema = fetch_schema(manifests, HashSet::new()).await;

    let plugin_contributes = schema["plugin_contributes"]
        .as_array()
        .expect("plugin_contributes array missing");
    assert!(
        plugin_contributes.is_empty(),
        "disabled 插件的 themes/client_styles 不应出口, got: {plugin_contributes:?}"
    );
}

#[tokio::test]
async fn schema_exports_ui_schema_for_tool_plugin_via_contributes_entry() {
    // 非 System/Pipeline 类型插件（如 tool 型 trigger_setup_tool）的 ui_schema.widgets
    // 进不了 agents/pipelines 数组（那两处只收 System/Pipeline），contributes 入口项
    // 是它们到达前端的唯一通道：ui_schema 必须随 entry 原样出口，否则声明页面
    // 只有壳（contributes.pages）没有内容件（widgets）——页面渲染为空。
    let widgets = json!([
        {
            "id": "triggers_table",
            "type": "table",
            "space": "triggers",
            "props": {"title": "触发器列表"}
        },
        {
            "id": "triggers_create",
            "type": "form",
            "space": "triggers",
            "props": {"title": "创建触发器"}
        }
    ]);
    let mut manifest = manifest_with_contributes(
        "trigger_setup_tool",
        Some(json!({"pages": [{"id": "triggers", "title": "触发器"}]})),
    );
    manifest.plugin_type = PluginType::Tool;
    manifest.ui_schema = Some(json!({"widgets": widgets.clone()}));

    let schema = fetch_schema(
        vec![manifest],
        HashSet::from(["trigger_setup_tool".to_string()]),
    )
    .await;

    let arr = schema["plugin_contributes"]
        .as_array()
        .expect("plugin_contributes array missing");
    assert_eq!(arr.len(), 1, "got: {arr:?}");
    let entry = &arr[0];
    assert_eq!(
        entry["ui_schema"]["widgets"], widgets,
        "tool 类型插件的 ui_schema.widgets 应随 contributes 入口项原样出口"
    );
}

#[tokio::test]
async fn schema_exports_ui_schema_only_plugin_without_contributes() {
    // 过滤条件是 contributes 或 ui_schema 任一存在：只声明 ui_schema 的插件
    // 也出口（否则 widget 声明被静默吞掉）。
    let ui_schema = json!({"widgets": [{"id": "w1", "type": "table", "space": "s1"}]});
    let mut manifest = manifest_with_contributes("ui_only_plugin", None);
    manifest.plugin_type = PluginType::Tool;
    manifest.ui_schema = Some(ui_schema.clone());

    let schema = fetch_schema(
        vec![manifest],
        HashSet::from(["ui_only_plugin".to_string()]),
    )
    .await;

    let arr = schema["plugin_contributes"]
        .as_array()
        .expect("plugin_contributes array missing");
    assert_eq!(arr.len(), 1, "got: {arr:?}");
    assert_eq!(arr[0]["ui_schema"], ui_schema);
    assert!(
        arr[0]["contributes"].is_null(),
        "未声明 contributes 时该字段应为 null"
    );
}

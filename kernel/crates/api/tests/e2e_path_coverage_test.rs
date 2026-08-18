// @feature: FP-0.2.七 路由收敛 | @vision: V6 可即用 | @ci: rust-test
//! 端到端路径覆盖测试（跨端点链路验证）。
//!
//! 目的:本次 16 个包新增的端点/能力分散在多个测试文件,本文件补**跨端点链路**
//! 验证,确认数据路径完整。沿用现有 oneshot + 真实 router + tempfile 范式,不 mock。
//!
//! 路径划分(对应任务规格 A-E):
//! - A:agent 配置完整 CRUD 闭环(list→read→write→read-back + 备份 + 非法 yaml 行为)
//! - B:schema 透传多 key contributes 完整性(pages/commands/viewsContainers 同时存在)
//! - C:actions 执行 + command 查找边界(tool 字段/命名空间边界/空 args)
//! - D:静态资源 + dispatcher 共存(同插件 assets 静态文件 + http_endpoints 各走各的)
//! - E:agents/schema 字段完整性(五种 type + agent_type/level options 枚举)

use std::collections::{HashMap, HashSet};
use std::fs;
use std::sync::Arc;

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use agentos_core::traits::{
    CapabilityRegistry, HostType, HttpEndpoint, HttpHandleCapability, HttpHandleRequest,
    HttpHandleResponse, PluginManifest, PluginType,
};
use agentos_plugin_loader::CapabilityRegistryImpl;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tokio::sync::RwLock;
use tower::ServiceExt;

/// 登录内置 admin（无 store 时回退内置用户表）返回 access_token。
async fn admin_token(app: &axum::Router) -> String {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method(axum::http::Method::POST)
                .uri("/api/v1/auth/login")
                .header("content-type", "application/json")
                .body(Body::from(
                    json!({"username": "admin", "password": "admin12345"}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let v: Value = serde_json::from_slice(&body).unwrap();
    v["access_token"].as_str().unwrap().to_string()
}

// ── 共享字面量构造 ─────────────────────────────────────────────

/// 构造一个最小 PluginManifest 字面量(contributes/http_endpoints 由调用方覆盖)。
/// 基线要求:必须带 `provides: None`(见 core/src/traits.rs:825 注释)。
fn manifest_base(plugin_id: &str) -> PluginManifest {
    PluginManifest {
        id: plugin_id.to_string(),
        name: plugin_id.to_string(),
        description: None,
        version: "1.0.0".to_string(),
        plugin_type: PluginType::System,
        pipeline_role: None,
        language: "python".to_string(),
        host_type: HostType::Sidecar,
        entry: "python server.py".to_string(),
        capabilities: Default::default(),
        requires_services: vec![],
        permissions: Default::default(),
        error_policy: Default::default(),
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
        contributes: None,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        provides: None,
    }
}

/// 构造临时项目根 + config/agents/main/<id>.yaml 的测试环境。
fn state_with_agent(id: &str, yaml_content: &str) -> (tempfile::TempDir, AppState) {
    let tmp = tempfile::tempdir().unwrap();
    let agent_dir = tmp.path().join("config").join("agents").join("main");
    fs::create_dir_all(&agent_dir).unwrap();
    fs::write(agent_dir.join(format!("{id}.yaml")), yaml_content).unwrap();

    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());
    (tmp, state)
}

/// NopHandler:dispatcher 命中路由后用到的进程内 HttpHandleCapability,原样 200。
struct NopHandler;

#[async_trait::async_trait]
impl HttpHandleCapability for NopHandler {
    async fn handle(&self, _req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
        Ok(HttpHandleResponse {
            status: 200,
            headers: HashMap::new(),
            body: String::new(),
            body_encoding: "base64".to_string(),
        })
    }
}

// ════════════════════════════════════════════════════════════════
// 路径 A:agent 配置完整 CRUD 闭环
// ════════════════════════════════════════════════════════════════

/// A1:GET /api/v1/agents(列表)→ 确认某 agent 存在 → GET config → PUT → GET 读回一致 →
///  确认 .bak 备份文件存在。整条 CRUD 链路闭合。
#[tokio::test]
async fn path_a_agent_config_full_crud_loop() {
    let original = "config_id: crud_agent\nname: 原名\nagent_type: main\nlevel: L1\n";
    let (tmp, state) = state_with_agent("crud_agent", original);

    // 1) GET /api/v1/agents 列表里能看到本 agent
    let app = build_router(state.clone());
    let token = admin_token(&app).await;
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/agents")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 64 * 1024)
        .await
        .unwrap();
    let list: Value = serde_json::from_slice(&body).unwrap();
    let ids: Vec<&str> = list["items"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|i| i["id"].as_str())
        .collect();
    assert!(
        ids.contains(&"crud_agent"),
        "agent 列表应含 crud_agent: {ids:?}"
    );

    // 2) GET config 读到原 yaml
    let app = build_router(state.clone());
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/agents/crud_agent/config")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 64 * 1024)
        .await
        .unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();
    assert!(
        json["yaml"].as_str().unwrap().contains("原名"),
        "GET 应返回原内容"
    );

    // 3) PUT 写新内容 → 返回 200 + success（A13：先取 GET 的 etag 走 If-Match）
    let new_yaml =
        "config_id: crud_agent\nname: 新名\nagent_type: main\nlevel: L2\nmodel_tier: large\n";
    let etag = json["etag"].as_str().expect("GET 应返回 etag").to_string();
    let put_body = serde_json::to_string(&json!({ "yaml": new_yaml, "if_match": etag })).unwrap();
    let app = build_router(state.clone());
    let resp = app
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri("/api/v1/agents/crud_agent/config")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(put_body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK, "PUT 应成功");

    // 4) GET 再读回 → 内容一致
    let app = build_router(state.clone());
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/agents/crud_agent/config")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 64 * 1024)
        .await
        .unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();
    let yaml_back = json["yaml"].as_str().unwrap();
    assert!(
        yaml_back.contains("新名") && yaml_back.contains("L2"),
        "GET 应读回 PUT 的新内容: {yaml_back}"
    );

    // 5) .bak 备份文件存在(同目录 <file>.yaml.bak),内容为原内容
    let backup = tmp.path().join("config/agents/main/crud_agent.yaml.bak");
    assert!(backup.is_file(), "备份文件应存在: {}", backup.display());
    let backup_content = fs::read_to_string(&backup).unwrap();
    assert_eq!(backup_content, original, "备份内容应为 PUT 前的原内容");

    // 让 _tmp 在函数末尾释放(不提前 drop)
    drop(tmp);
}

/// A2:PUT 写入非法 yaml(语法错误)→ 400 + 磁盘保持原值(T2 校验上线后的单分支契约)。
#[tokio::test]
async fn path_a_put_invalid_yaml_rejected_400_keeps_disk() {
    let original = "config_id: yamlcheck\nname: 原名\n";
    let (_tmp, state) = state_with_agent("yamlcheck", original);

    // 故意构造语法错误的 yaml(裸 tab 缩进 + 不闭合 quote)
    let broken_yaml = "config_id: yamlcheck\nname: \"未闭合\n\tbad: tab\n";
    let put_body = serde_json::to_string(&json!({ "yaml": broken_yaml })).unwrap();

    let app = build_router(state.clone());
    let token = admin_token(&app).await;
    let resp = app
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri("/api/v1/agents/yamlcheck/config")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(put_body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        StatusCode::BAD_REQUEST,
        "非法 yaml 语法应 400 拒绝(不写盘)"
    );

    let disk_path = state
        .project_root
        .as_ref()
        .unwrap()
        .join("config/agents/main/yamlcheck.yaml");
    let disk_after = fs::read_to_string(&disk_path).unwrap();
    assert_eq!(disk_after, original, "PUT 400 时文件内容应保持原值不变");
}

/// A3:PUT 缺 If-Match / etag 过期 → 409(A13 乐观锁,对齐 plugin config PUT)。
#[tokio::test]
async fn path_a_put_agent_config_etag_mismatch_409() {
    let original = "config_id: etagcheck\nname: 原名\n";
    let (_tmp, state) = state_with_agent("etagcheck", original);
    let valid_yaml = "config_id: etagcheck\nname: 新名\n";

    let app = build_router(state.clone());
    let token = admin_token(&app).await;

    // 缺 if_match → 409,磁盘不动
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri("/api/v1/agents/etagcheck/config")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::to_string(&json!({ "yaml": valid_yaml })).unwrap(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CONFLICT, "缺 If-Match 应 409");

    // etag 不匹配(伪造) → 409,磁盘不动
    let resp = app
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri("/api/v1/agents/etagcheck/config")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::to_string(&json!({
                        "yaml": valid_yaml,
                        "if_match": "stale-etag"
                    }))
                    .unwrap(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CONFLICT, "过期 etag 应 409");

    let disk = fs::read_to_string(
        state
            .project_root
            .as_ref()
            .unwrap()
            .join("config/agents/main/etagcheck.yaml"),
    )
    .unwrap();
    assert_eq!(disk, original, "409 时磁盘应保持原值");
}

// ════════════════════════════════════════════════════════════════
// 路径 B:schema 透传多 key contributes 完整性
// ════════════════════════════════════════════════════════════════

/// B1:一个 PluginManifest 的 contributes 同时含 pages + commands + viewsContainers。
/// GET /api/v1/schema → plugin_contributes 里的 contributes 对象**所有 key 都在**。
#[tokio::test]
async fn path_b_schema_contributes_multi_key_passthrough() {
    let contributes = json!({
        "pages": [{"id": "dashboard", "title": "仪表盘", "space": "main"}],
        "commands": [{"id": "say.hi", "title": "Say Hi"}],
        "viewsContainers": [{"id": "activitybar", "title": "活动栏"}]
    });

    let mut manifest = manifest_base("multi_contrib_plugin");
    manifest.contributes = Some(contributes.clone());

    let state = AppState {
        manifests: Arc::new(RwLock::new(vec![manifest])),
        enabled_plugin_ids: Arc::new(RwLock::new(HashSet::from([
            "multi_contrib_plugin".to_string()
        ]))),
        ..AppState::with_config(json!({}))
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
    let body = axum::body::to_bytes(resp.into_body(), 64 * 1024)
        .await
        .unwrap();
    let schema: Value = serde_json::from_slice(&body).unwrap();

    let arr = schema["plugin_contributes"]
        .as_array()
        .expect("plugin_contributes array missing");
    assert_eq!(arr.len(), 1, "应仅 1 个 enabled 插件的 contributes 出口");
    let entry = &arr[0];
    assert_eq!(entry["plugin_id"], "multi_contrib_plugin");

    // 关键:contributes 整体等于原声明(任意 key 都透传,字段不丢)
    assert_eq!(
        entry["contributes"], contributes,
        "contributes 应整体原样透传,不丢 key"
    );

    // 三个 key 都在(双保险,即便上面整体比较失败也能定位是哪个 key 丢)
    for key in ["pages", "commands", "viewsContainers"] {
        assert!(
            entry["contributes"].get(key).is_some(),
            "contributes 应含 key '{key}'"
        );
    }
}

/// B2:disabled 插件的 contributes 不出口(回归保护 + 与 B1 形成对照)。
#[tokio::test]
async fn path_b_disabled_plugin_contributes_not_exported() {
    let contributes = json!({
        "pages": [{"id": "p", "title": "P"}],
        "commands": [{"id": "c", "title": "C"}],
        "viewsContainers": [{"id": "v", "title": "V"}]
    });

    let mut manifest = manifest_base("disabled_multi");
    manifest.contributes = Some(contributes);

    // enabled_plugin_ids 为空集 → 该插件被视为 disabled
    let state = AppState {
        manifests: Arc::new(RwLock::new(vec![manifest])),
        enabled_plugin_ids: Arc::new(RwLock::new(HashSet::new())),
        ..AppState::with_config(json!({}))
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
    let body = axum::body::to_bytes(resp.into_body(), 64 * 1024)
        .await
        .unwrap();
    let schema: Value = serde_json::from_slice(&body).unwrap();

    let arr = schema["plugin_contributes"]
        .as_array()
        .expect("plugin_contributes array missing");
    assert!(
        arr.is_empty(),
        "disabled 插件的 contributes 不应出口(含 pages/commands/viewsContainers): {arr:?}"
    );
}

// ════════════════════════════════════════════════════════════════
// 路径 C:actions 执行 + command 查找边界
// ════════════════════════════════════════════════════════════════

/// C1:command 声明带 `tool` 字段 + invoker 不可用(None)→ 明确失败。
///
/// 降级语义(T1 修复后):tool 路由已声明但执行器缺席时,返回 success:false +
/// "工具执行器不可用"错误——不再静默占位 success:true(假成功会让前端把
/// "没执行"当成"执行成功")。无 tool 字段的纯声明命令仍走 ack 占位(见 C3)。
#[tokio::test]
async fn path_c_command_with_tool_field_but_no_invoker_returns_failure() {
    let mut manifest = manifest_base("tool_cmd_plugin");
    manifest.contributes = Some(json!({
        "commands": [{
            "id": "tool.cmd",
            "title": "Tool Command",
            "tool": "some_tool"
        }]
    }));

    let mut state = AppState::new();
    state.manifests = Arc::new(RwLock::new(vec![manifest]));
    // invoker 保持 None(AppState::new 默认),验证降级语义

    let app = build_router(state);
    let token = admin_token(&app).await;
    let body = serde_json::to_string(&json!({
        "action": "tool.cmd",
        "args": { "foo": "bar" }
    }))
    .unwrap();
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/actions/execute")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        StatusCode::OK,
        "降级失败走业务信封(HTTP 200 + success:false),而非 HTTP 错误码"
    );

    let body = axum::body::to_bytes(resp.into_body(), 64 * 1024)
        .await
        .unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(
        json["success"], false,
        "tool 已声明 + invoker=None 应返回明确失败: {json}"
    );
    let err = json["error"].as_str().unwrap_or_default();
    assert!(
        err.contains("工具执行器不可用"),
        "error 应说明执行器不可用: {json}"
    );
}

/// C2:command 声明在 contributes.commands[].id(如 "commandId"),但 action 参数带
/// 命名空间(如 "pluginId.commandId")→ 探测现有匹配语义。
///
/// 现有实现:`commands.iter().find(|c| c.get("id").as_str() == Some(action))` 是
/// **精确匹配**;故带命名空间的 action 不应命中 → 404。锁定此边界。
#[tokio::test]
async fn path_c_namespaced_action_does_not_match_bare_id() {
    let mut manifest = manifest_base("ns_plugin");
    manifest.contributes = Some(json!({
        "commands": [{ "id": "commandId", "title": "Bare" }]
    }));

    let mut state = AppState::new();
    state.manifests = Arc::new(RwLock::new(vec![manifest]));

    let app = build_router(state);
    let token = admin_token(&app).await;
    let body = serde_json::to_string(&json!({
        "action": "ns_plugin.commandId",
        "args": {}
    }))
    .unwrap();
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/actions/execute")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();

    // 现有匹配为精确比对 id 字符串,故 "ns_plugin.commandId" != "commandId" → 404。
    // 这是当前实现语义,本测试锁定它(若未来要支持命名空间解析,需更新本测试)。
    assert_eq!(
        resp.status(),
        StatusCode::NOT_FOUND,
        "精确匹配语义下,带命名空间的 action 不命中裸 id 声明 → 应 404;实际: {}",
        resp.status()
    );
}

/// C3:空 args({})能正常执行(闭合前端空参命令调用)。
#[tokio::test]
async fn path_c_empty_args_executes_normally() {
    let mut manifest = manifest_base("empty_args_plugin");
    manifest.contributes = Some(json!({
        "commands": [{ "id": "noop", "title": "Noop" }]
    }));

    let mut state = AppState::new();
    state.manifests = Arc::new(RwLock::new(vec![manifest]));

    let app = build_router(state);
    let token = admin_token(&app).await;
    let body = serde_json::to_string(&json!({
        "action": "noop",
        "args": {}
    }))
    .unwrap();
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/actions/execute")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 64 * 1024)
        .await
        .unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(json["success"], true, "空 args 应正常执行: {json}");
}

// ════════════════════════════════════════════════════════════════
// 路径 D:静态资源 + dispatcher 共存
// ════════════════════════════════════════════════════════════════

/// 构造一个临时插件目录:web/index.html + web/app.js(静态资源)。
fn make_plugin_with_web_assets() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    let web = dir.path().join("web");
    fs::create_dir_all(&web).unwrap();
    fs::write(
        web.join("index.html"),
        "<!doctype html><html><body><h1>SPA</h1></body></html>",
    )
    .unwrap();
    fs::write(web.join("app.js"), "console.log('app');").unwrap();
    dir
}

/// 构造一个 HttpEndpoint 字面量(route_id/method/path/auth=none)。
fn http_endpoint(route_id: &str, method: &str, path: &str) -> HttpEndpoint {
    HttpEndpoint {
        route_id: route_id.to_string(),
        method: method.to_string(),
        path: path.to_string(),
        auth: "none".to_string(),
        handler_capability: "http.handle".to_string(),
        timeout_ms: None,
        max_concurrency: None,
        description: None,
    }
}

/// D1:同一插件既有 assets 静态文件,又注册了 http_endpoint →
///  - GET /ext/{id}/assets/xxx → 静态分支直读返回文件
///  - GET /ext/{id}/api/hello   → 走 dispatcher(注册路由命中) → NopHandler 200
///  - GET /ext/{id}/api/unknown → 走 dispatcher(无路由命中) → 404
///  - GET /ext/{id}/not-assets/x → 静态分支不拦截 → dispatcher 404(无路由)
#[tokio::test]
async fn path_d_static_assets_and_dispatcher_coexist() {
    const PLUGIN_ID: &str = "coexist_plugin";
    let tmp = make_plugin_with_web_assets();

    // 注册 dispatcher 路由:/ext/{PLUGIN_ID}/api/hello GET(在 PLUGIN_ID 命名空间下)
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry
        .register_http_route(
            PLUGIN_ID,
            http_endpoint("hello", "GET", &format!("/ext/{PLUGIN_ID}/api/hello")),
        )
        .unwrap();

    let mut state = AppState::new();
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(NopHandler));
    let mut dirs = HashMap::new();
    dirs.insert(PLUGIN_ID.to_string(), tmp.path().to_path_buf());
    state.plugin_dirs = Arc::new(dirs);

    // 1) 静态资源:GET /ext/{id}/assets/index.html → 200 + 文件内容
    let app = build_router(state.clone());
    let resp = app
        .oneshot(
            Request::builder()
                .uri(format!("/ext/{PLUGIN_ID}/assets/index.html"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK, "静态资源应 200");
    let body = axum::body::to_bytes(resp.into_body(), 64 * 1024)
        .await
        .unwrap();
    let text = String::from_utf8(body.to_vec()).unwrap();
    assert!(text.contains("SPA"), "静态资源 body 应含 SPA: {text}");

    // 2) dispatcher 命中:GET /ext/{id}/api/hello → 200(NopHandler)
    let app = build_router(state.clone());
    let resp = app
        .oneshot(
            Request::builder()
                .uri(format!("/ext/{PLUGIN_ID}/api/hello"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        StatusCode::OK,
        "dispatcher 已注册路由应 200(NopHandler)"
    );

    // 3) dispatcher 未命中:GET /ext/{id}/api/unknown → 404
    let app = build_router(state.clone());
    let resp = app
        .oneshot(
            Request::builder()
                .uri(format!("/ext/{PLUGIN_ID}/api/unknown"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        StatusCode::NOT_FOUND,
        "dispatcher 未注册路由应 404"
    );

    // 4) 回归:非 assets 路径不被静态分支拦截(走 dispatcher,无路由 → 404)
    let app = build_router(state.clone());
    let resp = app
        .oneshot(
            Request::builder()
                .uri(format!("/ext/{PLUGIN_ID}/not-assets/index.html"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        StatusCode::NOT_FOUND,
        "非 assets 子路径不应被静态分支拦截,应由 dispatcher 处理(此处无路由 → 404)"
    );

    drop(tmp);
}

/// D2:assets 子路径下文件不存在 → 静态分支直接 404(不交回 dispatcher)。
///
/// 这是静态资源命名空间语义:/assets/** 归静态分支所有,即便文件不存在,
/// 也不应交回 dispatcher 再 404 一次(参考 try_serve_static_asset 的"路径形态归静态"分支)。
#[tokio::test]
async fn path_d_assets_missing_file_returns_static_404_not_dispatcher() {
    const PLUGIN_ID: &str = "assets_missing_plugin";
    let tmp = make_plugin_with_web_assets();

    // 即便注册了一个看起来像它的 dispatcher 路由,也不应被命中(因为 /assets/ 命名空间归静态)
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry
        .register_http_route(
            PLUGIN_ID,
            http_endpoint(
                "catchall",
                "GET",
                &format!("/ext/{PLUGIN_ID}/assets/{{file:path}}"),
            ),
        )
        .unwrap();

    let mut state = AppState::new();
    state.capability_registry = Some(registry);
    state.http_handler = Some(Arc::new(NopHandler));
    let mut dirs = HashMap::new();
    dirs.insert(PLUGIN_ID.to_string(), tmp.path().to_path_buf());
    state.plugin_dirs = Arc::new(dirs);

    let app = build_router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .uri(format!("/ext/{PLUGIN_ID}/assets/no_such_file.html"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        StatusCode::NOT_FOUND,
        "/assets/ 下文件不存在应静态分支 404,不应交回 dispatcher 命中通配路由"
    );

    drop(tmp);
}

// ════════════════════════════════════════════════════════════════
// 路径 E:agents/schema 端点的字段完整性
// ════════════════════════════════════════════════════════════════

/// E1:GET /api/v1/agents/schema → fields 数组覆盖五种 type + 每个字段带 label。
#[tokio::test]
async fn path_e_agents_schema_covers_five_field_types() {
    let app = build_router(AppState::new());
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/agents/schema")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let body = axum::body::to_bytes(resp.into_body(), 64 * 1024)
        .await
        .unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();
    let fields = json["fields"].as_array().expect("schema 应含 fields 数组");
    assert!(!fields.is_empty(), "fields 不应为空");

    // 五种 type 都应至少出现一次
    let mut types_seen: HashSet<&str> = HashSet::new();
    for f in fields {
        let t = f["type"].as_str().expect("字段应有 type");
        types_seen.insert(t);
        // 每个字段必须带 label(前端表单渲染依赖)
        assert!(f["label"].is_string(), "字段缺 label: {f}");
    }
    for expected in ["string", "textarea", "number", "select", "multiselect"] {
        assert!(
            types_seen.contains(expected),
            "fields 类型集合应含 '{expected}',实际: {:?}",
            types_seen
        );
    }
}

/// E2:agent_type 与 level 字段是 select 类型,且 options 含完整的枚举值集合。
#[tokio::test]
async fn path_e_agents_schema_select_fields_have_full_options() {
    let app = build_router(AppState::new());
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/agents/schema")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let body = axum::body::to_bytes(resp.into_body(), 64 * 1024)
        .await
        .unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();
    let fields = json["fields"].as_array().unwrap();

    let find_field = |name: &str| -> Value {
        fields
            .iter()
            .find(|f| f["name"].as_str() == Some(name))
            .cloned()
            .unwrap_or_else(|| panic!("schema 缺字段 {name}"))
    };

    // agent_type:select,options 含 main/orchestrator/specialized/atomic/system
    let agent_type = find_field("agent_type");
    assert_eq!(agent_type["type"], "select", "agent_type 应为 select");
    let at_opts: HashSet<&str> = agent_type["options"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|o| o["value"].as_str())
        .collect();
    for v in ["main", "orchestrator", "specialized", "atomic", "system"] {
        assert!(
            at_opts.contains(v),
            "agent_type options 缺 '{v}',实际: {:?}",
            at_opts
        );
    }

    // level:select,options 含 L1/L2/L3
    let level = find_field("level");
    assert_eq!(level["type"], "select", "level 应为 select");
    let lv_opts: HashSet<&str> = level["options"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|o| o["value"].as_str())
        .collect();
    for v in ["L1", "L2", "L3"] {
        assert!(
            lv_opts.contains(v),
            "level options 缺 '{v}',实际: {:?}",
            lv_opts
        );
    }
}

// ════════════════════════════════════════════════════════════════
// 路径 F:会话创建表单 schema（sessions/schema 聚合插件 thread_fields）
// ════════════════════════════════════════════════════════════════

/// F1:GET /api/v1/sessions/schema → 内置 title/intent + enabled 插件的
///  contributes.thread_fields（isolation 风格 workspace/isolationMode）聚合出口；
/// disabled 插件与无 name 的非法项被滤掉。
#[tokio::test]
async fn path_f_sessions_schema_aggregates_plugin_thread_fields() {
    let mut m_on = manifest_base("isolation");
    m_on.contributes = Some(json!({
        "thread_fields": [
            {"name": "workspace", "type": "string", "label": "工作空间"},
            {"name": "isolationMode", "type": "select", "label": "隔离模式",
             "options": [{"label": "非隔离", "value": "non_isolated"}]},
            {"type": "string", "label": "缺 name 的非法项应被忽略"}
        ]
    }));
    let mut m_off = manifest_base("disabled_plugin");
    m_off.contributes = Some(json!({
        "thread_fields": [{"name": "secret", "type": "string", "label": "不应出现"}]
    }));
    let m_none = manifest_base("no_contributes_plugin");

    let state = AppState {
        manifests: Arc::new(RwLock::new(vec![m_on, m_off, m_none])),
        enabled_plugin_ids: Arc::new(RwLock::new(HashSet::from([
            "isolation".to_string(),
            "no_contributes_plugin".to_string(),
        ]))),
        ..AppState::with_config(json!({}))
    };

    let app = build_router(state);
    let token = admin_token(&app).await;
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/sessions/schema")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 64 * 1024)
        .await
        .unwrap();
    let v: Value = serde_json::from_slice(&body).unwrap();

    let names: Vec<&str> = v["fields"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|f| f["name"].as_str())
        .collect();
    // 内置字段在前，插件字段随后；非法项（无 name）与 disabled 插件不出口
    assert_eq!(
        names,
        vec!["title", "intent", "workspace", "isolationMode"],
        "fields 应为内置 + enabled 插件贡献，实际: {names:?}"
    );
    let ws = &v["fields"][2];
    assert_eq!(ws["label"], "工作空间");
    assert_eq!(v["fields"][3]["options"][0]["value"], "non_isolated");
}

// ════════════════════════════════════════════════════════════════
// 路径 G:插件 enabled 写盘失败 → 5xx 统一错误信封
// ════════════════════════════════════════════════════════════════

/// G1:PUT /api/v1/plugins/{id}/enabled 写 profile 失败 → 500 统一信封。
///
/// profile 路径被同名目录占位 → fs::write 必败。契约(A12):HTTP 500 +
/// 通用文案 "internal server error"(ApiError::Internal 不透传 IO 细节),
/// 不再 200 + success:false 混装——前端据状态码即可区分"已生效"与"没写进去"。
#[tokio::test]
async fn path_g_plugin_enabled_write_failure_returns_500() {
    let tmp = tempfile::tempdir().unwrap();
    let plugins_dir = tmp.path().join("config").join("plugins");
    fs::create_dir_all(&plugins_dir).unwrap();
    // default_profile.yaml 被目录占位:读失败(回退空 profile),写必败
    fs::create_dir_all(plugins_dir.join("default_profile.yaml")).unwrap();

    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());

    let app = build_router(state);
    let token = admin_token(&app).await;
    let resp = app
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri("/api/v1/plugins/llm_service/enabled")
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::to_string(&json!({"enabled": false})).unwrap(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        StatusCode::INTERNAL_SERVER_ERROR,
        "写盘失败应 5xx,而非 200 假成功(实际 {})",
        resp.status()
    );
    let body = axum::body::to_bytes(resp.into_body(), 64 * 1024)
        .await
        .unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(
        json["error"]["code"], "500",
        "统一错误信封应含 error.code: {json}"
    );
    assert_eq!(
        json["error"]["message"], "internal server error",
        "内部错误细节(IO 报错含路径)不透传客户端: {json}"
    );
}

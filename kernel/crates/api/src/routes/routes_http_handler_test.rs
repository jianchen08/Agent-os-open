//! HTTP 处理器行为补测：upload 静态服务 / schema ETag 协商 / actions 命令
//! 出口 / 管道域读端点 / pending-inputs 队列 / 插件配置三形态读写 /
//! 插件监管端点 / 管道配置端点 / G8 排空。
//! 构造跟随既有模式：裸 AppState 注入内存 store 直调 handler；鉴权面走
//! build_router + oneshot（内存库播种 admin + encode_token 铸 token，
//! 同 sessions_crud_tests / system_memstats_tests 构造）。

use super::*;

use agentos_core::traits::{
    ConfigFileMapping, EnvConfigField, HostType, HttpEndpoint, PendingSpawnEntry, ToolDescriptor,
};
use agentos_core::types::{
    PendingInputRecord, PendingInputSource, ToolCategory, ToolExecutionResult, ToolSource,
};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

const ADMIN_USER_ID: &str = "00000000-0000-0000-0000-000000000001";
const ADMIN_PW: &str = "routes-http-test-admin-pw";

/// 全字段 manifest 构造（必填字段齐备，按用例需要改写特定字段）。
fn base_manifest(id: &str, plugin_type: PluginType) -> PluginManifest {
    PluginManifest {
        force_include_tools: Vec::new(),
        state: None,
        id: id.to_string(),
        name: format!("{id} 显示名"),
        description: Some("测试插件".to_string()),
        version: "1.0.0".to_string(),
        plugin_type,
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
        restricted_capabilities: vec![],
        requires_content: None,
        invoke_entry: None,
        config_files: vec![],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        export_fields: vec![],
        aux_venvs: Vec::new(),
        provides: None,
        visibility: Default::default(),
    }
}

fn field(name: &str, default: Option<Value>) -> EnvConfigField {
    EnvConfigField {
        name: name.to_string(),
        label: format!("{name} 标签"),
        field_type: "string".to_string(),
        required: false,
        description: None,
        extra: default.map(|d| {
            let mut m = serde_json::Map::new();
            m.insert("default".to_string(), d);
            m
        }),
    }
}

fn config_mapping(id: &str, path: &str, fields: Vec<EnvConfigField>) -> ConfigFileMapping {
    ConfigFileMapping {
        id: id.to_string(),
        settings: None,
        path: path.to_string(),
        label: format!("{id} 配置"),
        target: None,
        fields,
    }
}

fn sqlite() -> Arc<agentos_engine::SqliteStore> {
    Arc::new(agentos_engine::SqliteStore::open_memory().unwrap())
}

/// 内存库播种 admin 的完整路由 app + 该 admin 的 access token
/// （口令绑定校验要求 token 内嵌哈希与库内存储哈希一致——seed 与铸
/// token 共用同一哈希串，同 sessions_crud_tests）。
/// 第三返回值 = 播种库句柄（构造携带 manifest 等额外状态的同源 app 用）。
async fn app_with_admin_token() -> (axum::Router, String, Arc<agentos_engine::SqliteStore>) {
    let store = sqlite();
    let password_hash = agentos_http::auth::hash_password(ADMIN_PW).unwrap();
    store
        .create_user(&agentos_core::types::UserRecord {
            user_id: ADMIN_USER_ID.to_string(),
            username: "admin".to_string(),
            password: password_hash.clone(),
            email: None,
            role: "admin".to_string(),
            tenant_id: agentos_http::auth::DEFAULT_TENANT_ID.to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            last_login_at: None,
            must_change_password: false,
        })
        .await
        .unwrap();
    let admin = agentos_http::auth::BuiltInUser {
        id: ADMIN_USER_ID.to_string(),
        username: "admin".to_string(),
        password: password_hash,
        email: String::new(),
        role: "admin".to_string(),
        tenant_id: "default".to_string(),
        created_at: String::new(),
        must_change_password: false,
    };
    let token =
        agentos_http::auth::encode_token(agentos_http::auth::TokenType::Access, &admin, 3600);
    let mut state = AppState::new();
    state.store = Some(store.clone());
    (crate::server::build_router(state), token, store)
}

async fn oneshot(
    app: &axum::Router,
    method: &str,
    uri: &str,
    token: Option<&str>,
    body: Option<Value>,
) -> axum::response::Response {
    let mut builder = Request::builder().method(method).uri(uri);
    if let Some(t) = token {
        builder = builder.header("authorization", format!("Bearer {t}"));
    }
    let req = match body {
        Some(v) => builder
            .header("content-type", "application/json")
            .body(Body::from(v.to_string()))
            .unwrap(),
        None => builder.body(Body::empty()).unwrap(),
    };
    app.clone().oneshot(req).await.unwrap()
}

async fn body_json(resp: axum::response::Response) -> Value {
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20)
        .await
        .unwrap();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

// ── /uploads/{filename} 静态服务 ─────────────────────────

#[tokio::test]
async fn upload_rejects_empty_traversal_and_separators() {
    let state = AppState::new();
    for name in ["", "a/b.png", "a\\b.png", "..", "a/../b.png", "."] {
        let resp = serve_upload_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(name.to_string()),
        )
        .await;
        assert_eq!(
            resp.status(),
            StatusCode::NOT_FOUND,
            "路径不安全文件名 {name:?} 应 404"
        );
    }
}

#[tokio::test]
async fn upload_rejects_disallowed_extensions() {
    let state = AppState::new();
    // 可执行/文档/数据类与无扩展名一律 404（媒体白名单契约）
    for name in [
        "evil.exe",
        "notes.yaml",
        "archive.zip",
        "noext",
        " pic.PNG.exe ",
    ] {
        let resp = serve_upload_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(name.trim().to_string()),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::NOT_FOUND, "{name} 应 404");
    }
}

#[tokio::test]
async fn upload_missing_or_non_file_or_no_root_returns_404() {
    // 允许的扩展名但 (a) project_root 未接线 (b) 文件不存在 (c) 是目录
    let tmp = tempfile::tempdir().unwrap();
    let uploads = tmp.path().join("data").join("default").join("uploads");
    std::fs::create_dir_all(uploads.join("dir.png")).unwrap();
    // 解析面已纳入用户空间（BUG-39），按 test_env 契约钉桩隔离宿主机真实目录
    let _user = crate::test_env::pin_user_root(&tmp.path().join("user-root"));

    let mut no_root = AppState::new();
    no_root.project_root = None;
    let resp = serve_upload_handler(
        axum::extract::State(no_root),
        axum::extract::Path("x.png".to_string()),
    )
    .await;
    assert_eq!(
        resp.status(),
        StatusCode::NOT_FOUND,
        "无 project_root 应 404"
    );

    let mut with_root = AppState::new();
    with_root.project_root = Some(tmp.path().to_path_buf());
    for name in ["missing.png", "dir.png"] {
        let resp = serve_upload_handler(
            axum::extract::State(with_root.clone()),
            axum::extract::Path(name.to_string()),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::NOT_FOUND, "{name} 应 404");
    }
}

#[tokio::test]
async fn upload_serves_media_with_content_type_and_cache() {
    let tmp = tempfile::tempdir().unwrap();
    let uploads = tmp.path().join("data").join("default").join("uploads");
    std::fs::create_dir_all(&uploads).unwrap();
    std::fs::write(uploads.join("pic.png"), b"\x89PNG-bytes").unwrap();
    std::fs::write(uploads.join("clip.mp4"), b"mp4-bytes").unwrap();

    // 解析面已纳入用户空间（BUG-39），按 test_env 契约钉桩隔离宿主机真实目录
    let _user = crate::test_env::pin_user_root(&tmp.path().join("user-root"));
    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());
    for (name, expected_ct) in [("pic.png", "image/png"), ("clip.mp4", "video/mp4")] {
        let resp = serve_upload_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(name.to_string()),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::OK, "{name} 应 200");
        assert_eq!(
            resp.headers()["content-type"],
            expected_ct,
            "{name} content-type 应按扩展名映射"
        );
        assert!(
            resp.headers().get("cache-control").is_some(),
            "{name} 应带缓存头"
        );
        let bytes = axum::body::to_bytes(resp.into_body(), 4096).await.unwrap();
        assert!(!bytes.is_empty(), "{name} 应回传文件内容");
    }
}

/// BUG-39 复现：写端（artifacts 插件 `_get_uploads_dir`，ADR
/// 2026-09-13-unified-user-root）把上传落盘用户空间数据根
/// `<user_data>/default/uploads`，读端必须同源——文件只在用户空间、
/// project_root（仓库侧）没有时必须 200 回内容（劈叉读仓库根 = 全部
/// /uploads 404 = 历史会话图片附件「附件加载失败」）。
#[tokio::test]
async fn upload_serves_from_user_space_data_root() {
    let tmp = tempfile::tempdir().unwrap();
    let user_uploads = tmp
        .path()
        .join("user-root")
        .join("data")
        .join("default")
        .join("uploads");
    std::fs::create_dir_all(&user_uploads).unwrap();
    std::fs::write(user_uploads.join("new.png"), b"user-space-bytes").unwrap();

    let _user = crate::test_env::pin_user_root(&tmp.path().join("user-root"));
    let mut state = AppState::new();
    state.project_root = Some(tmp.path().join("project"));
    let resp = serve_upload_handler(
        axum::extract::State(state),
        axum::extract::Path("new.png".to_string()),
    )
    .await;
    assert_eq!(resp.status(), StatusCode::OK, "用户空间上传文件应可服务");
    let bytes = axum::body::to_bytes(resp.into_body(), 4096).await.unwrap();
    assert_eq!(
        bytes.as_ref(),
        b"user-space-bytes",
        "应回传用户空间侧文件内容"
    );
}

/// 用户空间与仓库侧同名并存（uuid 文件名下实属人为，钉优先级契约）：
/// 用户空间是迁移后唯一写点，优先命中。
#[tokio::test]
async fn upload_prefers_user_space_copy_over_project_root() {
    let tmp = tempfile::tempdir().unwrap();
    let user_uploads = tmp
        .path()
        .join("user-root")
        .join("data")
        .join("default")
        .join("uploads");
    std::fs::create_dir_all(&user_uploads).unwrap();
    std::fs::write(user_uploads.join("both.png"), b"user-wins").unwrap();
    let repo_uploads = tmp
        .path()
        .join("project")
        .join("data")
        .join("default")
        .join("uploads");
    std::fs::create_dir_all(&repo_uploads).unwrap();
    std::fs::write(repo_uploads.join("both.png"), b"repo-legacy").unwrap();

    let _user = crate::test_env::pin_user_root(&tmp.path().join("user-root"));
    let mut state = AppState::new();
    state.project_root = Some(tmp.path().join("project"));
    let resp = serve_upload_handler(
        axum::extract::State(state),
        axum::extract::Path("both.png".to_string()),
    )
    .await;
    assert_eq!(resp.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(resp.into_body(), 4096).await.unwrap();
    assert_eq!(bytes.as_ref(), b"user-wins", "用户空间副本优先");
}

/// 存量部署兼容：迁移脚本显式调用、仓库侧文件仍在（用户空间无此文件）
/// → 回退 project_root 仓库路径继续服务，不让未迁移部署的旧附件全灭。
#[tokio::test]
async fn upload_falls_back_to_project_root_for_legacy_files() {
    let tmp = tempfile::tempdir().unwrap();
    let repo_uploads = tmp
        .path()
        .join("project")
        .join("data")
        .join("default")
        .join("uploads");
    std::fs::create_dir_all(&repo_uploads).unwrap();
    std::fs::write(repo_uploads.join("legacy.png"), b"repo-legacy").unwrap();

    let _user = crate::test_env::pin_user_root(&tmp.path().join("user-root"));
    let mut state = AppState::new();
    state.project_root = Some(tmp.path().join("project"));
    let resp = serve_upload_handler(
        axum::extract::State(state),
        axum::extract::Path("legacy.png".to_string()),
    )
    .await;
    assert_eq!(resp.status(), StatusCode::OK, "仓库侧存量文件应回退可服务");
    let bytes = axum::body::to_bytes(resp.into_body(), 4096).await.unwrap();
    assert_eq!(bytes.as_ref(), b"repo-legacy");
}

// ── /uploads 大小上限（审查总册 P1-12：整读无上限 = 认证用户内存放大面）──

/// 生效上限三边界（钉 AGENTOS_UPLOAD_MAX_BYTES=16 缩小实物）：限内 8B 与恰好
/// 等于上限 16B（闭区间）→ 200 且限内文件完整回传；超限 17B → 413。
#[tokio::test]
async fn upload_size_limit_boundaries_under_at_over() {
    let tmp = tempfile::tempdir().unwrap();
    let uploads = tmp.path().join("data").join("default").join("uploads");
    std::fs::create_dir_all(&uploads).unwrap();
    std::fs::write(uploads.join("under.png"), b"12345678").unwrap();
    std::fs::write(uploads.join("at.png"), b"abcdefghijklmnop").unwrap();
    std::fs::write(uploads.join("over.png"), b"abcdefghijklmnopQ").unwrap();

    let _user = crate::test_env::pin_user_root(&tmp.path().join("user-root"));
    let _limit = crate::test_env::pin_env(UPLOAD_MAX_BYTES_ENV, Some("16"));
    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());

    let resp = serve_upload_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path("under.png".to_string()),
    )
    .await;
    assert_eq!(resp.status(), StatusCode::OK, "限内文件应 200");
    let bytes = axum::body::to_bytes(resp.into_body(), 4096).await.unwrap();
    assert_eq!(bytes.as_ref(), b"12345678", "限内文件应完整回传");

    let resp = serve_upload_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path("at.png".to_string()),
    )
    .await;
    assert_eq!(resp.status(), StatusCode::OK, "恰好等于上限属闭区间应 200");

    let resp = serve_upload_handler(
        axum::extract::State(state),
        axum::extract::Path("over.png".to_string()),
    )
    .await;
    assert_eq!(resp.status(), StatusCode::PAYLOAD_TOO_LARGE, "超限应 413");
}

/// 默认上限接线：env 未设时 DEFAULT_UPLOAD_MAX_BYTES 生效——默认上限 +1 字节
/// 的文件必须 413（防默认值被静默调大到失去挡巨物意义；限内 200 由既有小文件
/// 用例在 env 未设下覆盖）。
#[tokio::test]
async fn upload_over_default_limit_is_rejected_413() {
    let tmp = tempfile::tempdir().unwrap();
    let uploads = tmp.path().join("data").join("default").join("uploads");
    std::fs::create_dir_all(&uploads).unwrap();
    std::fs::write(
        uploads.join("big.png"),
        vec![b'x'; DEFAULT_UPLOAD_MAX_BYTES as usize + 1],
    )
    .unwrap();

    let _user = crate::test_env::pin_user_root(&tmp.path().join("user-root"));
    let _no_env = crate::test_env::pin_env(UPLOAD_MAX_BYTES_ENV, None);
    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());
    let resp = serve_upload_handler(
        axum::extract::State(state),
        axum::extract::Path("big.png".to_string()),
    )
    .await;
    assert_eq!(
        resp.status(),
        StatusCode::PAYLOAD_TOO_LARGE,
        "默认上限 +1 字节必须 413"
    );
}

/// 无长度声明（declared_len = None）走流式字节计数：跨多个读取块的 150_000B
/// 文件（块界 64KiB），注入上限 100_000 → TooLarge；同上限下 90_000B 多块
/// 文件 → Ok 全量（计数不得误伤限内文件）。
#[test]
fn upload_capped_read_without_declared_length_counts_bytes() {
    let tmp = tempfile::tempdir().unwrap();
    let big = tmp.path().join("big.bin");
    std::fs::write(&big, vec![7u8; 150_000]).unwrap();
    let small = tmp.path().join("small.bin");
    std::fs::write(&small, vec![7u8; 90_000]).unwrap();

    let err = read_upload_capped(&big, None, 100_000).unwrap_err();
    assert!(
        matches!(err, UploadReadError::TooLarge),
        "无长度声明且计数超限应 TooLarge"
    );
    let ok = read_upload_capped(&small, None, 100_000).unwrap();
    assert_eq!(ok.len(), 90_000, "无长度声明限内多块文件应全量返回");
}

/// 声明长度过闸但实际超限（stat 与读之间文件增长 / 声明失真）：流式计数必须
/// 兜住——declared_len=4 骗过声明闸，实际 150_000B 仍 TooLarge。
#[test]
fn upload_capped_read_counts_despite_stale_declared_length() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("grown.bin");
    std::fs::write(&path, vec![9u8; 150_000]).unwrap();
    let err = read_upload_capped(&path, Some(4), 100_000).unwrap_err();
    assert!(
        matches!(err, UploadReadError::TooLarge),
        "声明长度失真须由流式计数兜住"
    );
}

/// 上限配置语义：未设 / 非法 / "0"（会使一切请求 413 的误配自瘫）→ 默认值；
/// 合法正值生效；默认值本身必须为正（否则默认即自瘫，编译期即红）。
const _: () = assert!(DEFAULT_UPLOAD_MAX_BYTES > 0);

#[test]
fn upload_max_bytes_env_semantics() {
    assert_eq!(upload_max_bytes_from(None), DEFAULT_UPLOAD_MAX_BYTES);
    assert_eq!(upload_max_bytes_from(Some("abc")), DEFAULT_UPLOAD_MAX_BYTES);
    assert_eq!(upload_max_bytes_from(Some("0")), DEFAULT_UPLOAD_MAX_BYTES);
    assert_eq!(upload_max_bytes_from(Some("4096")), 4096);
}

// ── /api/v1/schema 聚合 + ETag 协商 ─────────────────────

#[tokio::test]
async fn schema_requires_authentication() {
    let (app, _token, _store) = app_with_admin_token().await;
    let resp = oneshot(&app, "GET", "/api/v1/schema", None, None).await;
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED, "匿名应 401");
}

#[tokio::test]
async fn schema_aggregates_manifests_configs_and_enabled_contributes() {
    let (_app, token, store) = app_with_admin_token().await;

    let mut sys = base_manifest("rt_sys", PluginType::System);
    sys.ui_schema = Some(json!({"widgets": []}));
    let pipe = base_manifest("rt_pipe", PluginType::Pipeline);
    // tool 插件带 config_files（一条可见 + 一条注入专用 settings:false）与
    // contributes + ui_schema，enabled 集合含它 → 出 contributes；另一插件
    // 声明了 contributes 但未启用 → 不出口。
    let mut tool = base_manifest("rt_tool", PluginType::Tool);
    tool.config_files = vec![
        config_mapping("visible_cfg", "config/plugins/rt_tool.yaml", vec![]),
        ConfigFileMapping {
            settings: Some(false),
            ..config_mapping("hidden_cfg", "config/plugins/rt_tool_extra.yaml", vec![])
        },
    ];
    tool.contributes = Some(json!({"commands": [{"id": "cmd.x"}]}));
    tool.ui_schema = Some(json!({"widgets": [{"type": "chart"}]}));
    let mut disabled = base_manifest("rt_disabled", PluginType::Tool);
    disabled.contributes = Some(json!({"commands": []}));

    let mut state = AppState::new();
    *state.manifests.write().await = vec![sys, pipe, tool, disabled];
    state.store = Some(store);
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("rt_tool".to_string());
    let app = crate::server::build_router(state);

    let resp = oneshot(&app, "GET", "/api/v1/schema", Some(&token), None).await;
    assert_eq!(resp.status(), StatusCode::OK);
    let etag = resp
        .headers()
        .get("etag")
        .expect("schema 应带 ETag 头")
        .to_str()
        .unwrap()
        .to_string();
    assert!(!etag.is_empty());
    let body = body_json(resp).await;

    let agents = body["agents"].as_array().unwrap();
    assert_eq!(agents.len(), 1, "只收 System 类型");
    assert_eq!(agents[0]["id"], "rt_sys");
    let pipelines = body["pipelines"].as_array().unwrap();
    assert_eq!(pipelines.len(), 1, "只收 Pipeline 类型");
    assert!(
        pipelines[0].get("role").is_none(),
        "PipelineRole 死字段已删（ADR 2026-10-13），schema 不再出口 role"
    );
    assert_eq!(
        body["tools"].as_array().unwrap().len(),
        0,
        "无 registry 空工具面"
    );
    assert_eq!(body["routes"], json!({}), "无 registry 路由面空对象");
    // settings:false 条目不出口为配置面板
    let cfgs = body["plugin_configs"].as_array().unwrap();
    assert_eq!(cfgs.len(), 1);
    assert_eq!(cfgs[0]["plugin_id"], "rt_tool");
    let file_ids: Vec<&str> = cfgs[0]["config_files"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|c| c["id"].as_str())
        .collect();
    assert!(file_ids.contains(&"visible_cfg"));
    assert!(!file_ids.contains(&"hidden_cfg"), "settings:false 不出口");
    // contributes 只出口 enabled 插件
    let contributes = body["plugin_contributes"].as_array().unwrap();
    assert_eq!(contributes.len(), 1, "未启用插件不出口 contributes");
    assert_eq!(contributes[0]["plugin_id"], "rt_tool");
    assert_eq!(contributes[0]["ui_schema"]["widgets"][0]["type"], "chart");
    assert_eq!(
        body["kernel_capabilities"].as_array().unwrap().len(),
        0,
        "未接线契约时缺省空"
    );
}

#[tokio::test]
async fn schema_etag_negotiation_304_and_200() {
    let (app, token, _store) = app_with_admin_token().await;

    let resp = oneshot(&app, "GET", "/api/v1/schema", Some(&token), None).await;
    assert_eq!(resp.status(), StatusCode::OK);
    let etag = resp
        .headers()
        .get("etag")
        .unwrap()
        .to_str()
        .unwrap()
        .to_string();
    assert!(
        !axum::body::to_bytes(resp.into_body(), 1 << 20)
            .await
            .unwrap()
            .is_empty(),
        "首次请求应回全量 body"
    );

    // 命中当前 ETag → 304 空体
    let uri = "/api/v1/schema".to_string();
    let req = Request::builder()
        .method("GET")
        .uri(&uri)
        .header("authorization", format!("Bearer {token}"))
        .header("if-none-match", &etag)
        .body(Body::empty())
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_MODIFIED, "匹配 ETag 应 304");

    // `*` 通配命中 → 304
    let req = Request::builder()
        .method("GET")
        .uri(&uri)
        .header("authorization", format!("Bearer {token}"))
        .header("if-none-match", "*")
        .body(Body::empty())
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_MODIFIED, "`*` 应 304");

    // 多候选逗号分隔：错误候选 + 正确候选 → 304
    let req = Request::builder()
        .method("GET")
        .uri(&uri)
        .header("authorization", format!("Bearer {token}"))
        .header("if-none-match", format!("\"stale\", {etag}"))
        .body(Body::empty())
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    assert_eq!(
        resp.status(),
        StatusCode::NOT_MODIFIED,
        "多候选命中任一应 304"
    );

    // 全部候选过期 → 200 全量
    let req = Request::builder()
        .method("GET")
        .uri(&uri)
        .header("authorization", format!("Bearer {token}"))
        .header("if-none-match", "\"stale-a\", \"stale-b\"")
        .body(Body::empty())
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK, "候选全不匹配应 200");
}

#[tokio::test]
async fn schema_tools_and_routes_from_registry() {
    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry.register_tool(
        "rt_reg",
        ToolDescriptor {
            name: "rt_tool_a".to_string(),
            description: "注册工具".to_string(),
            plugin_id: "rt_reg".to_string(),
            input_schema: json!({"type": "object"}),
            output_schema: None,
            category: ToolCategory::System,
            source: ToolSource::Mcp,
            ui: None,
            render: None,
            isolation_capable: None,
        },
    );
    registry
        .register_http_route(
            "rt_reg",
            HttpEndpoint {
                route_id: "ui".to_string(),
                method: "GET".to_string(),
                path: "/ext/rt_reg/ui".to_string(),
                auth: Some("user".to_string()),
                handler_capability: "http.handle".to_string(),
                timeout_ms: None,
                max_concurrency: None,
                description: None,
            },
        )
        .expect("合法 /ext 命名空间路由应注册成功");

    let mut state = AppState::new();
    state.capability_registry = Some(registry.clone());

    // schema 端点带鉴权，直调 handler 验证聚合语义（鉴权面由其余 oneshot 用例覆盖）。
    let resp = schema_handler(
        axum::extract::State(AppState {
            capability_registry: Some(registry),
            ..AppState::new()
        }),
        axum::http::HeaderMap::new(),
    )
    .await;
    assert_eq!(resp.status(), StatusCode::OK);
    let body = body_json(resp).await;
    let tools = body["tools"].as_array().unwrap();
    assert_eq!(tools.len(), 1, "registry 工具应聚合");
    assert_eq!(tools[0]["name"], "rt_tool_a");
    let routes = body["routes"].as_object().unwrap();
    assert!(routes.contains_key("rt_reg"), "路由按插件分组");
    let entries = routes["rt_reg"].as_array().unwrap();
    assert_eq!(entries.len(), 1);
    assert_eq!(entries[0]["route_id"], "ui");
    assert_eq!(entries[0]["path"], "/ext/rt_reg/ui");
}

// ── /api/v1/actions/execute 命令出口 ────────────────────

#[tokio::test]
async fn actions_execute_missing_or_blank_action_400() {
    let state = AppState::new();
    for action in ["", "   "] {
        let err = actions_execute_handler(
            axum::extract::State(state.clone()),
            axum::Json(ActionsExecuteRequest {
                action: action.to_string(),
                args: json!({}),
            }),
        )
        .await
        .unwrap_err();
        assert!(
            matches!(err, ApiError::BadRequest { .. }),
            "缺 action 应 400，实际 {err:?}"
        );
    }
}

#[tokio::test]
async fn actions_execute_undeclared_command_404() {
    let state = AppState::new();
    let err = actions_execute_handler(
        axum::extract::State(state),
        axum::Json(ActionsExecuteRequest {
            action: "cmd.never".to_string(),
            args: json!({}),
        }),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, ApiError::NotFound { .. }), "未声明命令应 404");
}

#[tokio::test]
async fn actions_execute_declared_command_acks_with_plugin_id() {
    let mut m = base_manifest("cmd_plugin", PluginType::Tool);
    m.contributes = Some(json!({"commands": [{"id": "cmd.ping", "label": "ping"}]}));
    let state = AppState::new();
    *state.manifests.write().await = vec![m];

    let resp = actions_execute_handler(
        axum::extract::State(state),
        axum::Json(ActionsExecuteRequest {
            action: "cmd.ping".to_string(),
            args: json!({}),
        }),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["success"], true, "纯声明命令回占位 ack");
    assert_eq!(resp.0["result"]["acknowledged"], true);
    assert_eq!(resp.0["plugin_id"], "cmd_plugin");
}

/// invoke_tool 可编程的 mock invoker（actions 工具路由 / validate-all 共用）。
struct StubInvoker {
    tool_result:
        std::sync::Mutex<Option<Result<ToolExecutionResult, agentos_core::types::PluginError>>>,
    list_result: std::sync::Mutex<Option<Result<Value, agentos_core::types::PluginError>>>,
    last_tool_call: std::sync::Mutex<Option<(String, String, Value)>>,
}

impl StubInvoker {
    fn with_tool(r: Result<ToolExecutionResult, agentos_core::types::PluginError>) -> Arc<Self> {
        Arc::new(Self {
            tool_result: std::sync::Mutex::new(Some(r)),
            list_result: std::sync::Mutex::new(None),
            last_tool_call: std::sync::Mutex::new(None),
        })
    }
    fn with_list(r: Result<Value, agentos_core::types::PluginError>) -> Arc<Self> {
        Arc::new(Self {
            tool_result: std::sync::Mutex::new(None),
            list_result: std::sync::Mutex::new(Some(r)),
            last_tool_call: std::sync::Mutex::new(None),
        })
    }
}

#[async_trait::async_trait]
impl PluginInvoker for StubInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        _ctx: &agentos_core::types::PluginContext<'a>,
    ) -> Result<agentos_core::types::PluginResult, agentos_core::types::PluginError> {
        unimplemented!("本测试不触达管道插件调用")
    }
    async fn invoke_tool(
        &self,
        plugin_id: &str,
        tool_name: &str,
        inputs: &Value,
    ) -> Result<ToolExecutionResult, agentos_core::types::PluginError> {
        *self.last_tool_call.lock().unwrap() =
            Some((plugin_id.to_string(), tool_name.to_string(), inputs.clone()));
        self.tool_result
            .lock()
            .unwrap()
            .clone()
            .expect("invoke_tool 不应被调用")
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
        self.list_result
            .lock()
            .unwrap()
            .clone()
            .expect("list_plugin_tools 不应被调用")
    }
}

#[tokio::test]
async fn actions_execute_tool_route_without_invoker_fails_explicitly() {
    let mut m = base_manifest("tool_cmd", PluginType::Tool);
    m.contributes = Some(json!({"commands": [{"id": "cmd.run", "tool": "do_thing"}]}));
    let state = AppState::new();
    *state.manifests.write().await = vec![m];

    let resp = actions_execute_handler(
        axum::extract::State(state),
        axum::Json(ActionsExecuteRequest {
            action: "cmd.run".to_string(),
            args: json!({"k": 1}),
        }),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["success"], false, "invoker 缺席必须显式失败不假成功");
    let msg = resp.0["error"].as_str().unwrap();
    assert!(msg.contains("工具执行器不可用"), "错误应说明原因: {msg}");
    assert_eq!(resp.0["plugin_id"], "tool_cmd");
}

#[tokio::test]
async fn actions_execute_tool_route_invoker_success_and_error() {
    let manifest = || {
        let mut m = base_manifest("tool_cmd2", PluginType::Tool);
        m.contributes = Some(json!({"commands": [{"id": "cmd.go", "tool": "go_tool"}]}));
        m
    };

    // 成功路径：result.success/data/error 透传 + 参数落到调用侧
    let invoker = StubInvoker::with_tool(Ok(ToolExecutionResult {
        success: true,
        data: json!({"echo": "done"}),
        error: None,
        duration_ms: Some(5),
        metadata: None,
    }));
    let mut state = AppState::new();
    *state.manifests.write().await = vec![manifest()];
    state.invoker = Some(invoker.clone() as Arc<dyn PluginInvoker>);
    let resp = actions_execute_handler(
        axum::extract::State(state),
        axum::Json(ActionsExecuteRequest {
            action: "cmd.go".to_string(),
            args: json!({"n": 7}),
        }),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["success"], true);
    assert_eq!(resp.0["result"]["echo"], "done");
    assert_eq!(
        invoker.last_tool_call.lock().unwrap().as_ref().unwrap(),
        &(
            "tool_cmd2".to_string(),
            "go_tool".to_string(),
            json!({"n": 7})
        ),
        "命令声明的 tool 路由应按 (plugin_id, tool, args) 调用"
    );

    // 失败路径：invoker Err → success:false + error 文案
    let invoker = StubInvoker::with_tool(Err(agentos_core::types::PluginError {
        message: "sidecar crashed".to_string(),
        code: None,
        source: None,
    }));
    let mut state = AppState::new();
    *state.manifests.write().await = vec![manifest()];
    state.invoker = Some(invoker as Arc<dyn PluginInvoker>);
    let resp = actions_execute_handler(
        axum::extract::State(state),
        axum::Json(ActionsExecuteRequest {
            action: "cmd.go".to_string(),
            args: json!({}),
        }),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["success"], false);
    assert_eq!(resp.0["error"], "sidecar crashed");
}

// ── /api/v1/pipelines 与 /api/v1/tools ──────────────────

#[tokio::test]
async fn pipelines_handler_lists_pipeline_type_only_with_host() {
    let mut pipe = base_manifest("pipe_only", PluginType::Pipeline);
    pipe.host_type = HostType::InProcess;
    let state = AppState::new();
    *state.manifests.write().await = vec![base_manifest("sys_x", PluginType::System), pipe];

    let resp = pipelines_handler(axum::extract::State(state)).await;
    assert_eq!(resp.0.len(), 1, "只列 Pipeline 类型");
    assert_eq!(resp.0[0]["id"], "pipe_only");
    assert!(
        resp.0[0].get("role").is_none(),
        "PipelineRole 死字段已删（ADR 2026-10-13），清单不再出口 role"
    );
    assert_eq!(resp.0[0]["host_type"], "in_process");
}

#[tokio::test]
async fn tools_handler_envelope_with_and_without_registry() {
    // registry 未装配 → 空面但信封不变
    let resp = tools_handler(axum::extract::State(AppState::new())).await;
    assert_eq!(resp.0["items"].as_array().unwrap().len(), 0);
    assert_eq!(resp.0["total"], 0);

    let registry = Arc::new(CapabilityRegistryImpl::new());
    registry.register_tool(
        "t_reg",
        ToolDescriptor {
            name: "z_tool".to_string(),
            description: "工具描述".to_string(),
            plugin_id: "t_reg".to_string(),
            input_schema: json!({}),
            output_schema: None,
            category: ToolCategory::Search,
            source: ToolSource::Mcp,
            ui: None,
            render: None,
            isolation_capable: None,
        },
    );
    let mut state = AppState::new();
    state.capability_registry = Some(registry);
    let resp = tools_handler(axum::extract::State(state)).await;
    assert_eq!(resp.0["total"], 1);
    let item = &resp.0["items"][0];
    assert_eq!(item["name"], "z_tool");
    assert_eq!(item["plugin_id"], "t_reg");
    assert_eq!(item["category"], "search");
    assert_eq!(item["source"], "mcp");
}

// ── /api/v1/pipelines/runs ──────────────────────────────

#[tokio::test]
async fn pipelines_runs_without_db_returns_404() {
    let state = AppState::new();
    let err = pipelines_runs_handler(
        axum::extract::State(state),
        axum::extract::Query(HashMap::new()),
        axum::http::HeaderMap::new(),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, ApiError::NotFound { .. }), "db 未接线应 404");
}

#[tokio::test]
async fn pipelines_runs_with_db_returns_seeded_run() {
    let db = sqlite();
    // 运行簿记 = state 运行键（ADR 2026-09-18）：record_run_start 即建立
    // run ↔ pipeline 归属，快照由 pipeline_state 聚合合成。
    db.record_run_start(
        "pipe_runs_rt",
        "default",
        "run_rt_1",
        "hash",
        "2026-10-15T00:00:00+00:00",
    )
    .unwrap();

    let mut state = AppState::new();
    state.db = Some(db);
    let resp = pipelines_runs_handler(
        axum::extract::State(state),
        axum::extract::Query(HashMap::new()),
        axum::http::HeaderMap::new(),
    )
    .await
    .unwrap();
    let items = resp.0["items"].as_array().unwrap();
    assert_eq!(items.len(), 1, "播种 run 应出现在快照");
    assert_eq!(items[0]["run_id"], "run_rt_1");
    assert_eq!(items[0]["pipeline_id"], "pipe_runs_rt");
}

// ── pending-inputs 队列端点（列表/修改/清空成功面） ─────────

fn pending_record(pipeline_id: &str, id: &str, cmid: &str) -> PendingInputRecord {
    PendingInputRecord {
        id: id.to_string(),
        tenant_id: "default".to_string(),
        created_at: chrono::Utc::now().to_rfc3339(),
        request: agentos_core::types::DispatchRequest {
            thread_id: "thread-pi".to_string(),
            user_id: "u1".to_string(),
            pipeline_id: pipeline_id.to_string(),
            content: "排队输入".to_string(),
            client_message_id: cmid.to_string(),
            source: PendingInputSource::Trigger,
            execution_context: None,
            state_overlay: None,
            pipeline_config_id: None,
        },
    }
}

fn state_with_store() -> (AppState, Arc<agentos_engine::SqliteStore>) {
    let store = sqlite();
    let mut state = AppState::new();
    state.store = Some(store.clone() as Arc<dyn StorageBackend>);
    (state, store)
}

#[tokio::test]
async fn pending_inputs_list_without_store_404_and_seeded_items_shape() {
    // store 未接线 → 404
    let err = pending_inputs_list_handler(
        axum::extract::State(AppState::new()),
        axum::extract::Path("pipe_pi_a".to_string()),
        axum::http::HeaderMap::new(),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, ApiError::NotFound { .. }));

    // 播种两条 → FIFO 列表，条目字段齐备
    let (state, store) = state_with_store();
    StorageBackend::enqueue_pending_input(
        store.as_ref(),
        "default",
        &pending_record("pipe_pi_a", "pi_1", ""),
    )
    .await
    .unwrap();
    StorageBackend::enqueue_pending_input(
        store.as_ref(),
        "default",
        &pending_record("pipe_pi_a", "pi_2", "http_cmid_1"),
    )
    .await
    .unwrap();
    let resp = pending_inputs_list_handler(
        axum::extract::State(state),
        axum::extract::Path("pipe_pi_a".to_string()),
        axum::http::HeaderMap::new(),
    )
    .await
    .unwrap();
    let items = resp.0["items"].as_array().unwrap();
    assert_eq!(items.len(), 2);
    assert_eq!(items[0]["id"], "pi_1", "FIFO 序");
    for key in ["id", "pipeline_id", "content", "source", "created_at"] {
        assert!(items[0].get(key).is_some(), "列表条目应含 {key}");
    }
}

#[tokio::test]
async fn pending_inputs_update_validates_content_and_existence() {
    // content 空/缺失 → 400
    let (state, _store) = state_with_store();
    for body in [json!({}), json!({"content": ""}), json!({"content": 42})] {
        let err = pending_inputs_update_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(("pipe_pi_b".to_string(), "pi_x".to_string())),
            axum::http::HeaderMap::new(),
            axum::Json(body),
        )
        .await
        .unwrap_err();
        assert!(
            matches!(err, ApiError::BadRequest { .. }),
            "content 非空字符串约束应 400，实际 {err:?}"
        );
    }

    // 条目不存在 → 404
    let (state, _store) = state_with_store();
    let err = pending_inputs_update_handler(
        axum::extract::State(state),
        axum::extract::Path(("pipe_pi_b".to_string(), "pi_missing".to_string())),
        axum::http::HeaderMap::new(),
        axum::Json(json!({"content": "新内容"})),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, ApiError::NotFound { .. }), "不存在条目应 404");
}

#[tokio::test]
async fn pending_inputs_update_success_persists_content() {
    let (state, store) = state_with_store();
    StorageBackend::enqueue_pending_input(
        store.as_ref(),
        "default",
        &pending_record("pipe_pi_c", "pi_ok", "http_cmid_2"),
    )
    .await
    .unwrap();

    let resp = pending_inputs_update_handler(
        axum::extract::State(state),
        axum::extract::Path(("pipe_pi_c".to_string(), "pi_ok".to_string())),
        axum::http::HeaderMap::new(),
        axum::Json(json!({"content": "修改后的内容"})),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["status"], "updated");
    let rows = StorageBackend::list_pending_inputs(store.as_ref(), "default", "pipe_pi_c")
        .await
        .unwrap();
    assert_eq!(rows[0].request.content, "修改后的内容", "修改应落库");
}

/// 捕获型 EventSink（同 chat_send_handler tests 构造）：记录全部文本帧。
struct FrameSink {
    frames: Arc<std::sync::Mutex<Vec<String>>>,
}

#[async_trait::async_trait]
impl agentos_session::EventSink for FrameSink {
    async fn send_text(&self, text: &str) -> bool {
        self.frames.lock().unwrap().push(text.to_string());
        true
    }
    fn id(&self) -> u64 {
        7
    }
}

#[tokio::test]
async fn pending_inputs_clear_success_persists_and_emits_event() {
    let (mut state, store) = state_with_store();
    for id in ["pi_c1", "pi_c2"] {
        StorageBackend::enqueue_pending_input(
            store.as_ref(),
            "default",
            &pending_record("pipe_pi_clear", id, ""),
        )
        .await
        .unwrap();
    }
    // 会话接线 + pipeline→thread 映射：清空事件应单播到该 thread。
    let session = Arc::new(agentos_session::SessionCoordinator::new());
    let frames = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    session.register_thread("thread-pi-clear", "u_pi");
    session.register(
        "u_pi",
        Arc::new(FrameSink {
            frames: frames.clone(),
        }),
    );
    state.session = Some(session.clone());
    StorageBackend::link_pipeline_session(
        store.as_ref(),
        "pipe_pi_clear",
        "thread-pi-clear",
        "default",
    )
    .await
    .unwrap();

    let resp = pending_inputs_clear_handler(
        axum::extract::State(state),
        axum::extract::Path("pipe_pi_clear".to_string()),
        axum::http::HeaderMap::new(),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["status"], "cleared");
    assert_eq!(resp.0["deleted"], 2, "清空应返回删除条数");
    let rows = StorageBackend::list_pending_inputs(store.as_ref(), "default", "pipe_pi_clear")
        .await
        .unwrap();
    assert!(rows.is_empty(), "清空应落库");

    // WS 事件推送：thread 坐标反查命中 → pending_inputs_changed 单播
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(3);
    let got = loop {
        let snapshot = frames.lock().unwrap().clone();
        if !snapshot.is_empty() {
            break snapshot;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "3s 内应收到 pending_inputs_changed 帧"
        );
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    };
    let frame: Value = serde_json::from_str(got.last().unwrap()).unwrap();
    assert_eq!(frame["type"], "pending_inputs_changed");
    assert_eq!(frame["data"]["action"], "cleared");
    assert_eq!(frame["data"]["pipeline_id"], "pipe_pi_clear");
    assert_eq!(frame["data"]["thread_id"], "thread-pi-clear");
}

// ── GET /api/v1/pipelines/state 摘要列表 ─────────────────

#[tokio::test]
async fn state_handler_memory_rows_tenant_filtered_with_summary() {
    let reg = agentos_session::pipeline_state_registry::global_registry();
    reg.get_or_init(
        "default",
        "pipe_st_mem",
        "thread_st_mem",
        "agentos",
        json!({
            "run_status": "running",
            "messages": [{"role": "user"}, {"role": "assistant"}],
            "task.goal": "g",
        }),
    );
    reg.get_or_init(
        "tenant_st_other",
        "pipe_st_foreign",
        "thread_st_foreign",
        "agentos",
        json!({"run_status": "running"}),
    );

    let state = AppState::new();
    let resp = pipelines_state_handler(axum::extract::State(state), axum::http::HeaderMap::new())
        .await
        .unwrap();
    let items = resp.0["items"].as_array().unwrap();
    let row = items
        .iter()
        .find(|i| i["pipeline_id"] == "pipe_st_mem")
        .expect("本租户内存行应出口");
    assert_eq!(row["source"], "memory");
    assert_eq!(row["thread_id"], "thread_st_mem");
    assert_eq!(row["state"]["run_status"], "running");
    assert_eq!(row["state"]["message_count"], 2, "messages 只出口条数");
    assert!(
        row["state"].get("task.goal").is_none(),
        "未声明 export_fields 的插件域键不出口"
    );
    assert!(
        !items.iter().any(|i| i["pipeline_id"] == "pipe_st_foreign"),
        "他租户内存行必须被过滤"
    );
}

#[tokio::test]
async fn state_handler_backfills_export_declared_keys_from_store() {
    // manifest 声明 export_fields（llm_model）：内存行缺失该键时从
    // pipeline_state 表补齐（运行中投影键每轮落表）。
    let mut m = base_manifest("st_decl", PluginType::Tool);
    m.export_fields = vec!["llm_model".to_string()];
    let (state, store) = state_with_store();
    *state.manifests.write().await = vec![m];
    StorageBackend::upsert_state_field(
        store.as_ref(),
        "pipe_st_backfill",
        "default",
        "llm_model",
        &json!("k2"),
    )
    .await
    .unwrap();

    let reg = agentos_session::pipeline_state_registry::global_registry();
    reg.get_or_init(
        "default",
        "pipe_st_backfill",
        "thread_st_backfill",
        "agentos",
        json!({"run_status": "running"}),
    );

    let resp = pipelines_state_handler(axum::extract::State(state), axum::http::HeaderMap::new())
        .await
        .unwrap();
    let row = resp.0["items"]
        .as_array()
        .unwrap()
        .iter()
        .find(|i| i["pipeline_id"] == "pipe_st_backfill")
        .expect("内存行应出口");
    assert_eq!(row["state"]["llm_model"], "k2", "声明键应从表补齐");
}

#[tokio::test]
async fn state_handler_cold_rows_from_checkpoint_and_orphans_skipped() {
    let db = sqlite();
    let dyn_store: Arc<dyn StorageBackend> = db.clone();

    // 冷行：运行簿记（state 运行键）+ checkpoint → source=checkpoint 出口
    db.record_run_start(
        "pipe_st_cold",
        "default",
        "run_st_cold",
        "hash",
        "2026-10-15T00:00:00+00:00",
    )
    .unwrap();
    StorageBackend::save_checkpoint(
        dyn_store.as_ref(),
        "pipe_st_cold",
        "default",
        2,
        // 基线键（current_phase/raw_result）随摘要直接出口；run_status/ended
        // 属易变 per-run 键，checkpoint 落档即剥离（引擎语义）；插件域键需
        // export_fields 声明（声明化语义由 summarize/export 单测覆盖）
        &json!({"current_phase": "post", "raw_result": "复盘结论"}),
    )
    .await
    .unwrap();

    // 孤儿：只有消息槽行、无运行簿记（state 无运行键）无 checkpoint → 不出口
    let mut st2 = json!({"pipeline_id": "pipe_st_orphan", "messages": []});
    agentos_engine::apply_messages_op_update(
            &mut st2,
            dyn_store.as_ref(),
            "default",
            &[json!({"op": "set", "_run_id": "run_st_orphan", "msg": {"role": "user", "content": "x"}})],
        )
        .await
        .unwrap();

    let mut state = AppState::new();
    state.db = Some(db);
    let resp = pipelines_state_handler(axum::extract::State(state), axum::http::HeaderMap::new())
        .await
        .unwrap();
    let items = resp.0["items"].as_array().unwrap();
    let cold = items
        .iter()
        .find(|i| i["pipeline_id"] == "pipe_st_cold")
        .expect("checkpoint 冷行应出口");
    assert_eq!(cold["source"], "checkpoint");
    assert_eq!(cold["state"]["raw_result"], "复盘结论", "基线键随摘要出口");
    assert_eq!(cold["state"]["current_phase"], "post");
    // 运行状态随 state 运行键（run_status='running'）出口；收束投影缺失时
    // 才以投影状态 fill-if-absent 补齐（BUG-2 幽灵 running 契约）。
    assert_eq!(
        cold["state"]["run_status"],
        json!("running"),
        "run_status 以运行簿记权威状态出口"
    );
    assert_eq!(
        cold["thread_id"], "",
        "无 pipeline_sessions 映射时 thread 缺省空"
    );
    assert!(
        !items.iter().any(|i| i["pipeline_id"] == "pipe_st_orphan"),
        "孤儿 run（无运行簿记痕迹）不出口"
    );
}

// ── 插件配置端点（三形态：env / 内联 / 文件引用） ───────────

const ENV_TOKEN_KEY: &str = "AGENTOS_RT_TEST_TOKEN";

/// §2.4 反向依赖查询端点：消费者经 requires_services 反查提供者（含启停态标注）。
#[tokio::test]
async fn dependents_handler_lists_service_consumers() {
    let state = AppState::new();
    let mut provider = base_manifest("prov", PluginType::Tool);
    provider.capabilities.services = vec![agentos_core::traits::ServiceCapability {
        name: "prov.ping".to_string(),
        description: None,
        input_schema: None,
        output_schema: None,
    }];
    let mut consumer = base_manifest("consumer", PluginType::Tool);
    consumer.requires_services = vec!["prov.ping".into()];
    {
        let mut ms = state.manifests.write().await;
        ms.push(provider);
        ms.push(consumer);
    }
    state.enabled_plugin_ids.write().await.insert("prov".into());
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("consumer".into());

    let resp = plugins_dependents_handler(
        axum::extract::Path("prov".to_string()),
        axum::extract::State(state),
    )
    .await
    .unwrap();
    let body = resp.0;
    assert_eq!(body["total"], 1, "仅 consumer 依赖 prov.ping");
    assert_eq!(body["dependents"][0]["id"], "consumer");
    assert_eq!(body["dependents"][0]["enabled"], true, "启停态标注透出");
}

/// 带三条 config_files（env/内联/文件引用）manifest 的 AppState 脚手架。
/// 返回 (state, tmp)。文件引用路径 config/plugins/cfg_rt.yaml。
///
/// 用户根钉到同一个 tmp：`.env` 与文件引用配置的读写落点都在用户空间
/// （ADR 2026-09-13-unified-user-root），把两者合一后用例里 `tmp/.env`
/// 的写读语义不变；不钉会落到开发机真实用户目录（污染 + 断言依赖环境）。
async fn plugin_cfg_state() -> (AppState, tempfile::TempDir, crate::test_env::UserSpaceGuard) {
    let tmp = tempfile::tempdir().unwrap();
    let guard = crate::test_env::pin_user_root(tmp.path());
    let mut m = base_manifest("cfg_plugin", PluginType::Tool);
    m.config_files = vec![
        ConfigFileMapping {
            target: Some("env".to_string()),
            path: ".env".to_string(),
            ..config_mapping(
                "env_cfg",
                "",
                vec![
                    field(ENV_TOKEN_KEY, None),
                    field("AGENTOS_RT_TEST_OTHER", None),
                ],
            )
        },
        config_mapping("inline_cfg", "", vec![field("threshold", Some(json!(0.5)))]),
        config_mapping(
            "file_cfg",
            "config/plugins/cfg_rt.yaml",
            vec![field("api_key", None), field("model", None)],
        ),
    ];
    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());
    *state.manifests.write().await = vec![m];
    (state, tmp, guard)
}

fn err_status(err: ApiError) -> axum::http::StatusCode {
    err.into_response().status()
}

#[tokio::test]
async fn plugin_config_get_defends_and_maps_env_target() {
    // project_root 缺席 → 500
    let err = get_plugin_config_handler(
        axum::extract::State(AppState::new()),
        axum::extract::Path(("cfg_plugin".to_string(), "env_cfg".to_string())),
    )
    .await
    .unwrap_err();
    assert_eq!(err_status(err), StatusCode::INTERNAL_SERVER_ERROR);

    // 未知插件 / 未知 file_id → 404
    let (state, _tmp, _g) = plugin_cfg_state().await;
    for (pid, fid) in [
        ("no_such_plugin", "env_cfg"),
        ("cfg_plugin", "no_such_file"),
    ] {
        let err = get_plugin_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path((pid.to_string(), fid.to_string())),
        )
        .await
        .unwrap_err();
        assert_eq!(err_status(err), StatusCode::NOT_FOUND, "{pid}/{fid} 应 404");
    }

    // env 形态：.env 已设字段掩码 ***，未设字段空串，path 恒 ".env"
    let (state, tmp, _g) = plugin_cfg_state().await;
    std::fs::write(tmp.path().join(".env"), format!("{ENV_TOKEN_KEY}=abc123\n")).unwrap();
    let resp = get_plugin_config_handler(
        axum::extract::State(state),
        axum::extract::Path(("cfg_plugin".to_string(), "env_cfg".to_string())),
    )
    .await
    .unwrap();
    assert_eq!(resp.0.plugin_id, "cfg_plugin");
    assert_eq!(resp.0.path, ".env");
    assert_eq!(resp.0.data[ENV_TOKEN_KEY], "***", "已设字段掩码");
    assert_eq!(resp.0.data["AGENTOS_RT_TEST_OTHER"], "", "未设字段空串");
    assert!(!resp.0.etag.is_empty());
}

#[tokio::test]
async fn plugin_config_get_inline_defaults_and_file_reference() {
    // 内联形态：真值 = fields.default
    let (state, _tmp, _g) = plugin_cfg_state().await;
    let resp = get_plugin_config_handler(
        axum::extract::State(state),
        axum::extract::Path(("cfg_plugin".to_string(), "inline_cfg".to_string())),
    )
    .await
    .unwrap();
    assert_eq!(
        resp.0.data,
        json!({"threshold": 0.5}),
        "内联形态回 defaults"
    );

    // 文件引用形态：文件缺失 → 空配置视图（ETag 与 PUT 同源）
    let (state, _tmp, _g) = plugin_cfg_state().await;
    let resp = get_plugin_config_handler(
        axum::extract::State(state),
        axum::extract::Path(("cfg_plugin".to_string(), "file_cfg".to_string())),
    )
    .await
    .unwrap();
    assert_eq!(resp.0.data, json!({}), "文件缺失回空视图");

    // 文件引用形态：有内容 → secret 键掩码；with_etag 变体响应头带 ETag
    let (state, tmp, _g) = plugin_cfg_state().await;
    let cfg_dir = tmp.path().join("config").join("plugins");
    std::fs::create_dir_all(&cfg_dir).unwrap();
    std::fs::write(
        cfg_dir.join("cfg_rt.yaml"),
        "api_key: super_secret\nmodel: k2\n",
    )
    .unwrap();
    let resp = get_plugin_config_with_etag(
        axum::extract::State(state),
        axum::extract::Path(("cfg_plugin".to_string(), "file_cfg".to_string())),
    )
    .await
    .unwrap();
    let etag_header = resp
        .headers()
        .get("etag")
        .expect("with_etag 变体应带 ETag 响应头")
        .to_str()
        .unwrap()
        .to_string();
    let body = body_json(resp).await;
    assert_eq!(body["data"]["api_key"], "****", "secret 键必须掩码");
    assert_eq!(body["data"]["model"], "k2", "普通键原样");
    // ETag 头与 body 内 etag 同源一致
    assert_eq!(etag_header, body["etag"].as_str().unwrap());
}

#[tokio::test]
async fn plugin_config_put_file_etag_lock_and_sentinel_merge() {
    let (state, tmp, _g) = plugin_cfg_state().await;
    let cfg_path = tmp
        .path()
        .join("config")
        .join("plugins")
        .join("cfg_rt.yaml");
    std::fs::create_dir_all(cfg_path.parent().unwrap()).unwrap();
    std::fs::write(&cfg_path, "api_key: old_key\nmodel: m1\n").unwrap();

    // 缺 if_match / 错 etag → 409
    let make_req = |if_match: Option<String>| PluginConfigUpdateRequest {
        data: json!({"api_key": "****", "model": "m2"}),
        if_match,
    };
    for given in [None, Some("wrong-etag".to_string())] {
        let err = put_plugin_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(("cfg_plugin".to_string(), "file_cfg".to_string())),
            axum::Json(make_req(given.clone())),
        )
        .await
        .unwrap_err();
        assert_eq!(
            err_status(err),
            StatusCode::CONFLICT,
            "if_match={given:?} 应 409"
        );
    }

    // 正确 etag → 哨兵保留 api_key 磁盘原值，model 更新
    let current = get_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("cfg_plugin".to_string(), "file_cfg".to_string())),
    )
    .await
    .unwrap();
    let etag = current.0.etag.clone();
    let resp = put_plugin_config_handler(
        axum::extract::State(state),
        axum::extract::Path(("cfg_plugin".to_string(), "file_cfg".to_string())),
        axum::Json(make_req(Some(etag))),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["plugin_id"], "cfg_plugin");
    assert!(!resp.0["etag"].as_str().unwrap().is_empty());
    let on_disk = std::fs::read_to_string(&cfg_path).unwrap();
    assert!(on_disk.contains("old_key"), "哨兵字段必须保留磁盘原值");
    assert!(on_disk.contains("m2"), "非哨兵字段应更新");
    assert!(!on_disk.contains("m1"), "旧值不应残留");
}

#[tokio::test]
async fn plugin_config_put_inline_writes_manifest_and_syncs_memory() {
    let (mut state, tmp, _g) = plugin_cfg_state().await;
    // 内联 PUT 需要插件目录映射（loader 活真源现取）+ 磁盘 plugin.json
    let plugin_dir = tmp.path().join("plugins").join("cfg_plugin");
    std::fs::create_dir_all(&plugin_dir).unwrap();
    std::fs::write(
        plugin_dir.join("plugin.json"),
        json!({
            "id": "cfg_plugin",
            "name": "cfg_plugin",
            "version": "1.0.0",
            "plugin_type": "tool",
            "language": "python",
            "host_type": "sidecar",
            "entry": "python server.py",
            "priority": 100,
            "capabilities": {},
            "config_files": [{
                "id": "inline_cfg",
                "label": "inline_cfg 配置",
                "fields": [
                    {"name": "threshold", "label": "threshold", "type": "number", "default": 0.5}
                ]
            }],
        })
        .to_string(),
    )
    .unwrap();

    state.plugin_loader =
        Some(crate::test_env::discovered_plugin_loader(&tmp.path().join("plugins")).await);

    // 当前值视图 etag → PUT 更新 → 磁盘 plugin.json 与内存 manifest 同步
    let current = get_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("cfg_plugin".to_string(), "inline_cfg".to_string())),
    )
    .await
    .unwrap();
    let etag = current.0.etag;
    let resp = put_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("cfg_plugin".to_string(), "inline_cfg".to_string())),
        axum::Json(PluginConfigUpdateRequest {
            data: json!({"threshold": 0.9}),
            if_match: Some(etag),
        }),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["plugin_id"], "cfg_plugin");

    let on_disk: Value =
        serde_json::from_str(&std::fs::read_to_string(plugin_dir.join("plugin.json")).unwrap())
            .unwrap();
    assert_eq!(
        on_disk["config_files"][0]["fields"][0]["default"],
        json!(0.9),
        "内联保存必须写回 plugin.json fields.default"
    );
    // 内存 manifest 同步：GET 即时反映新值
    let view = get_plugin_config_handler(
        axum::extract::State(state),
        axum::extract::Path(("cfg_plugin".to_string(), "inline_cfg".to_string())),
    )
    .await
    .unwrap();
    assert_eq!(view.0.data["threshold"], 0.9, "内存 manifest 应即时同步");
}

#[tokio::test]
async fn plugin_config_put_inline_undeclared_and_null_clear() {
    let (mut state, tmp, _g) = plugin_cfg_state().await;
    let plugin_dir = tmp.path().join("plugins").join("cfg_plugin");
    std::fs::create_dir_all(&plugin_dir).unwrap();
    std::fs::write(
        plugin_dir.join("plugin.json"),
        json!({
            "id": "cfg_plugin", "name": "cfg_plugin", "version": "1.0.0",
            "plugin_type": "tool", "language": "python", "host_type": "sidecar",
            "entry": "python server.py", "priority": 100,
            "capabilities": {},
            "config_files": [{
                "id": "inline_cfg", "label": "inline_cfg 配置",
                "fields": [
                    {"name": "threshold", "label": "threshold", "type": "number", "default": 0.5}
                ]
            }],
        })
        .to_string(),
    )
    .unwrap();
    state.plugin_loader =
        Some(crate::test_env::discovered_plugin_loader(&tmp.path().join("plugins")).await);

    let etag_of = |data: Value| compute_etag(serde_json::to_string(&data).unwrap().as_bytes());

    // 未声明字段 → 400（fail-closed，不落盘）
    let err = put_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("cfg_plugin".to_string(), "inline_cfg".to_string())),
        axum::Json(PluginConfigUpdateRequest {
            data: json!({"nonsense": 1}),
            if_match: Some(etag_of(json!({"threshold": 0.5}))),
        }),
    )
    .await
    .unwrap_err();
    assert_eq!(err_status(err), StatusCode::BAD_REQUEST, "未声明字段应 400");

    // null = 清除 default：写回后 fields 无 default，GET 空视图
    let resp = put_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("cfg_plugin".to_string(), "inline_cfg".to_string())),
        axum::Json(PluginConfigUpdateRequest {
            data: json!({"threshold": null}),
            if_match: Some(etag_of(json!({"threshold": 0.5}))),
        }),
    )
    .await
    .unwrap();
    let _ = resp;
    let on_disk: Value =
        serde_json::from_str(&std::fs::read_to_string(plugin_dir.join("plugin.json")).unwrap())
            .unwrap();
    assert!(
        on_disk["config_files"][0]["fields"][0]
            .get("default")
            .is_none(),
        "null 应剥除 default 键: {on_disk}"
    );
    let view = get_plugin_config_handler(
        axum::extract::State(state),
        axum::extract::Path(("cfg_plugin".to_string(), "inline_cfg".to_string())),
    )
    .await
    .unwrap();
    assert_eq!(view.0.data, json!({}), "清除后 GET 空视图");
}

#[tokio::test]
async fn plugin_config_put_env_target_write_sentinel_and_clear() {
    // 成功写：新值进 .env，掩码视图翻转 ***
    let (state, tmp, _g) = plugin_cfg_state().await;
    std::fs::write(tmp.path().join(".env"), "unrelated=1\n").unwrap();
    let current = get_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("cfg_plugin".to_string(), "env_cfg".to_string())),
    )
    .await
    .unwrap();
    let etag = current.0.etag;
    let resp = put_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("cfg_plugin".to_string(), "env_cfg".to_string())),
        axum::Json(PluginConfigUpdateRequest {
            data: json!({ENV_TOKEN_KEY: "v_value_9"}),
            if_match: Some(etag),
        }),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["ok"], true);
    let env_text = std::fs::read_to_string(tmp.path().join(".env")).unwrap();
    assert!(
        env_text.contains(&format!("{ENV_TOKEN_KEY}=v_value_9")),
        "新值应写入 .env: {env_text}"
    );
    assert!(env_text.contains("unrelated=1"), "既有键不得破坏");
    let view = get_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("cfg_plugin".to_string(), "env_cfg".to_string())),
    )
    .await
    .unwrap();
    assert_eq!(view.0.data[ENV_TOKEN_KEY], "***", "写入后掩码视图");

    // 哨兵 *** = 保留现值：.env 不变
    let etag2 = view.0.etag;
    let _resp = put_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("cfg_plugin".to_string(), "env_cfg".to_string())),
        axum::Json(PluginConfigUpdateRequest {
            data: json!({ENV_TOKEN_KEY: "***"}),
            if_match: Some(etag2),
        }),
    )
    .await
    .unwrap();
    let env_text2 = std::fs::read_to_string(tmp.path().join(".env")).unwrap();
    assert!(
        env_text2.contains(&format!("{ENV_TOKEN_KEY}=v_value_9")),
        "哨兵必须保留现值: {env_text2}"
    );

    // 空串 = 清除：键从 .env 移除
    let view2 = get_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("cfg_plugin".to_string(), "env_cfg".to_string())),
    )
    .await
    .unwrap();
    let _resp = put_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("cfg_plugin".to_string(), "env_cfg".to_string())),
        axum::Json(PluginConfigUpdateRequest {
            data: json!({ENV_TOKEN_KEY: ""}),
            if_match: Some(view2.0.etag),
        }),
    )
    .await
    .unwrap();
    let env_text3 = std::fs::read_to_string(tmp.path().join(".env")).unwrap();
    assert!(
        !env_text3.contains(ENV_TOKEN_KEY),
        "空串清除应移除键: {env_text3}"
    );

    // 未声明字段 → 400（须带当前正确 etag——乐观锁先于字段校验）；
    // etag 不匹配 → 409
    let view = get_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("cfg_plugin".to_string(), "env_cfg".to_string())),
    )
    .await
    .unwrap();
    let err = put_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("cfg_plugin".to_string(), "env_cfg".to_string())),
        axum::Json(PluginConfigUpdateRequest {
            data: json!({"AGENTOS_RT_TEST_UNDEFINED_X": "v"}),
            if_match: Some(view.0.etag.clone()),
        }),
    )
    .await
    .unwrap_err();
    assert_eq!(err_status(err), StatusCode::BAD_REQUEST);
    let err = put_plugin_config_handler(
        axum::extract::State(state),
        axum::extract::Path(("cfg_plugin".to_string(), "env_cfg".to_string())),
        axum::Json(PluginConfigUpdateRequest {
            data: json!({ENV_TOKEN_KEY: "v"}),
            if_match: Some("stale".to_string()),
        }),
    )
    .await
    .unwrap_err();
    assert_eq!(err_status(err), StatusCode::CONFLICT);
}

// ── 通用用户空间 .env 读写（批次 A2） ─────────────────────

#[tokio::test]
async fn user_env_get_masks_values_and_etags() {
    let (state, tmp, _g) = plugin_cfg_state().await;
    std::fs::write(
        tmp.path().join(".env"),
        "DEEPSEEK_API_KEY=sk-real\nOTHER=1\n",
    )
    .unwrap();
    let resp = get_user_env_handler(axum::extract::State(state))
        .await
        .unwrap();
    assert_eq!(resp.0["vars"]["DEEPSEEK_API_KEY"], "***", "值恒掩码");
    assert_eq!(resp.0["vars"]["OTHER"], "***");
    let etag = resp.0["etag"].as_str().unwrap();
    assert!(!etag.is_empty());
}

#[tokio::test]
async fn user_env_put_writes_sentinel_skips_unset_clears() {
    let (state, tmp, _g) = plugin_cfg_state().await;
    std::fs::write(
        tmp.path().join(".env"),
        "EXISTING=keep\nDEEPSEEK_API_KEY=old\n",
    )
    .unwrap();
    let view = get_user_env_handler(axum::extract::State(state.clone()))
        .await
        .unwrap();
    let etag = view.0["etag"].as_str().unwrap().to_string();

    let resp = put_user_env_handler(
        axum::extract::State(state.clone()),
        axum::Json(UserEnvUpdateRequest {
            set: HashMap::from([
                ("MINIMAX_API_KEY".to_string(), "new-key".to_string()),
                ("DEEPSEEK_API_KEY".to_string(), "***".to_string()),
            ]),
            unset: vec!["EXISTING".to_string()],
            if_match: Some(etag),
        }),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["ok"], true);
    let env_text = std::fs::read_to_string(tmp.path().join(".env")).unwrap();
    assert!(env_text.contains("MINIMAX_API_KEY=new-key"), "{env_text}");
    assert!(
        env_text.contains("DEEPSEEK_API_KEY=old"),
        "哨兵保留现值: {env_text}"
    );
    assert!(
        !env_text.contains("EXISTING="),
        "unset 移除该行: {env_text}"
    );
    // 落点恒用户空间（pin_user_root → <USER_ROOT>/.env）
    let new_vars = &resp.0["vars"];
    assert_eq!(new_vars["MINIMAX_API_KEY"], "***");
    assert!(new_vars.get("EXISTING").is_none(), "清除后掩码视图不含该键");
}

#[tokio::test]
async fn user_env_put_rejects_reserved_names_and_etag_mismatch() {
    let (state, tmp, _g) = plugin_cfg_state().await;
    std::fs::write(tmp.path().join(".env"), "A=1\n").unwrap();

    // 保留名 → 422（内核行为开关与系统变量不得经配置面改写）
    for name in ["AGENTOS_ADMIN_PASSWORD", "agentos_db_path", "PATH"] {
        let err = put_user_env_handler(
            axum::extract::State(state.clone()),
            axum::Json(UserEnvUpdateRequest {
                set: HashMap::from([(name.to_string(), "x".to_string())]),
                unset: vec![],
                if_match: None,
            }),
        )
        .await
        .unwrap_err();
        assert_eq!(err_status(err), StatusCode::UNPROCESSABLE_ENTITY, "{name}");
    }

    // ETag 不匹配 → 409
    let err = put_user_env_handler(
        axum::extract::State(state),
        axum::Json(UserEnvUpdateRequest {
            set: HashMap::from([("K".to_string(), "v".to_string())]),
            unset: vec![],
            if_match: Some("stale".to_string()),
        }),
    )
    .await
    .unwrap_err();
    assert_eq!(err_status(err), StatusCode::CONFLICT);
}

// ── 管道配置端点（config/pipelines/{name}.yaml） ───────────

fn valid_pipeline_cfg() -> Value {
    // PipelineFile DSL：loop_bodies[].steps[] 是带 id 的阶段节点，
    // 阶段的 steps[] 才是插件步骤名（对齐 config/pipelines/autonomous.yaml）。
    json!({
        "name": "cfg_rt",
        "loop_bodies": [
            {"id": "main", "steps": [
                {"id": "core", "steps": ["some_plugin_step"]}
            ]}
        ]
    })
}

#[test]
fn pipeline_name_charset_rejects_traversal_and_empty() {
    assert!(validate_pipeline_name("a-b_C1"));
    assert!(!validate_pipeline_name(""));
    assert!(!validate_pipeline_name("a/b"));
    assert!(!validate_pipeline_name("a\\b"));
    assert!(!validate_pipeline_name("a..b"));
    assert!(!validate_pipeline_name("a.b"));
    assert!(!validate_pipeline_name("管道名"));
}

#[tokio::test]
async fn pipeline_config_get_rejects_and_reads_with_etag() {
    // 用户层钉桩：不钉会落到开发机真实用户目录（既有用例的隐式污染面）。
    // 本用例只读 factory（用户层无该文件 → 回落 factory），guard 保 hermetic。
    let tmp = tempfile::tempdir().unwrap();
    let _user_guard = crate::test_env::pin_user_config_dir(&tmp.path().join("user-config"));
    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());
    // 非法名 → 400
    let err = get_pipeline_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path("bad/name".to_string()),
    )
    .await
    .unwrap_err();
    assert_eq!(err_status(err), StatusCode::BAD_REQUEST);

    // 未知管道 → 404
    let err = get_pipeline_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path("never_pipe".to_string()),
    )
    .await
    .unwrap_err();
    assert_eq!(err_status(err), StatusCode::NOT_FOUND);

    // 既有管道 → data + ETag 头
    let pipe_dir = tmp.path().join("config").join("pipelines");
    std::fs::create_dir_all(&pipe_dir).unwrap();
    std::fs::write(
        pipe_dir.join("cfg_rt.yaml"),
        serde_yaml::to_string(&valid_pipeline_cfg()).unwrap(),
    )
    .unwrap();
    let resp = get_pipeline_config_with_etag(
        axum::extract::State(state),
        axum::extract::Path("cfg_rt".to_string()),
    )
    .await
    .unwrap();
    assert!(resp.headers().get("etag").is_some(), "GET 应带 ETag 头");
    let body = body_json(resp).await;
    assert_eq!(body["name"], "cfg_rt");
    assert_eq!(
        body["data"]["loop_bodies"][0]["steps"][0]["id"], "core",
        "YAML → JSON 解析保留阶段节点"
    );
}

#[tokio::test]
async fn pipeline_config_put_validates_then_atomic_writes() {
    let tmp = tempfile::tempdir().unwrap();
    let user_config = tmp.path().join("user-config");
    std::fs::create_dir_all(&user_config).unwrap();
    let _user_guard = crate::test_env::pin_user_config_dir(&user_config);
    let pipe_dir = tmp.path().join("config").join("pipelines");
    std::fs::create_dir_all(&pipe_dir).unwrap();
    std::fs::write(
        pipe_dir.join("cfg_rt.yaml"),
        serde_yaml::to_string(&valid_pipeline_cfg()).unwrap(),
    )
    .unwrap();
    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());
    // 写侧落点（用户层），首次 PUT 后生效
    let user_pipe = user_config.join("pipelines").join("cfg_rt.yaml");

    // 非映射 data → 400（类型名进错误消息）
    for bad in [json!(["a"]), json!("scalar"), json!(7)] {
        let err = put_pipeline_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path("cfg_rt".to_string()),
            axum::Json(PipelineConfigUpdateRequest {
                data: bad.clone(),
                if_match: None,
            }),
        )
        .await
        .unwrap_err();
        assert_eq!(err_status(err), StatusCode::BAD_REQUEST, "{bad} 应 400");
    }

    // 非法 DSL 结构（loop_bodies 体缺 id）→ 400
    let err = put_pipeline_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path("cfg_rt".to_string()),
        axum::Json(PipelineConfigUpdateRequest {
            data: json!({"name": "cfg_rt", "loop_bodies": [{"steps": []}]}),
            if_match: None,
        }),
    )
    .await
    .unwrap_err();
    assert_eq!(err_status(err), StatusCode::BAD_REQUEST, "G10 DSL 校验应拒");

    // 文件缺失 → 404（PUT 不隐式创建）
    let err = put_pipeline_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path("never_pipe".to_string()),
        axum::Json(PipelineConfigUpdateRequest {
            data: valid_pipeline_cfg(),
            if_match: None,
        }),
    )
    .await
    .unwrap_err();
    assert_eq!(err_status(err), StatusCode::NOT_FOUND);

    // if_match 缺失/不匹配 → 409
    for given in [None, Some("wrong".to_string())] {
        let err = put_pipeline_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path("cfg_rt".to_string()),
            axum::Json(PipelineConfigUpdateRequest {
                data: valid_pipeline_cfg(),
                if_match: given.clone(),
            }),
        )
        .await
        .unwrap_err();
        assert_eq!(err_status(err), StatusCode::CONFLICT, "if_match={given:?}");
    }

    // 正确 etag → 原子写 + 新 etag；GET 回读一致
    let current = get_pipeline_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path("cfg_rt".to_string()),
    )
    .await
    .unwrap();
    let etag = current.0.etag;
    let mut next = valid_pipeline_cfg();
    next["loop_bodies"][0]["steps"][0]["steps"] = json!(["another_step"]);
    let resp = put_pipeline_config_handler(
        axum::extract::State(state),
        axum::extract::Path("cfg_rt".to_string()),
        axum::Json(PipelineConfigUpdateRequest {
            data: next.clone(),
            if_match: Some(etag),
        }),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["name"], "cfg_rt");
    assert!(!resp.0["etag"].as_str().unwrap().is_empty());
    // 改动落用户层（ADR 2026-09-13-unified-user-root）：出仓，不覆写 git 跟踪文件
    let on_disk: Value =
        serde_yaml::from_str(&std::fs::read_to_string(&user_pipe).unwrap()).unwrap();
    assert_eq!(
        on_disk["loop_bodies"][0]["steps"][0]["steps"][0], "another_step",
        "PUT 应原子写回用户层"
    );
    // 播种：首次接管即拿到 factory 完整内容（非 diff 片段）
    assert_eq!(
        on_disk["loop_bodies"][0]["steps"][0]["id"], "core",
        "播种内容来自 factory（未改字段原样保留）"
    );
    // factory 保持原样（未被覆写）
    let factory_disk: Value =
        serde_yaml::from_str(&std::fs::read_to_string(pipe_dir.join("cfg_rt.yaml")).unwrap())
            .unwrap();
    assert_ne!(
        factory_disk["loop_bodies"][0]["steps"][0]["steps"][0], "another_step",
        "factory 文件不得被用户改动覆写"
    );
    // 无 .tmp 残骸
    assert!(
        !user_pipe.with_extension("yaml.tmp").exists(),
        "不留 tmp 残骸"
    );
}

/// 内核加载侧与 HTTP 写侧同源：用户在管道配置页保存后，`load_pipeline_config`
/// 必须读到用户层那份（否则"保存成功、重启后还是旧配置"）。
#[tokio::test]
async fn pipeline_config_load_rereads_user_layer_after_put() {
    let tmp = tempfile::tempdir().unwrap();
    let user_config = tmp.path().join("user-config");
    std::fs::create_dir_all(&user_config).unwrap();
    let _user_guard = crate::test_env::pin_user_config_dir(&user_config);
    let pipe_dir = tmp.path().join("config").join("pipelines");
    std::fs::create_dir_all(&pipe_dir).unwrap();
    std::fs::write(
        pipe_dir.join("autonomous.yaml"),
        serde_yaml::to_string(&valid_pipeline_cfg()).unwrap(),
    )
    .unwrap();
    // load_pipeline_config 的入参是 config 根（`<repo>/config`），与内核启动
    // 装配传的 config_root 同义
    let config_root = tmp.path().join("config");

    // 未接管：加载读 factory
    let factory_loaded =
        crate::pipeline_loader::resolve_pipeline_config_path(&config_root, "autonomous.yaml");
    assert_eq!(factory_loaded, pipe_dir.join("autonomous.yaml"));
    assert!(
        crate::pipeline_loader::load_pipeline_config(&config_root).is_ok(),
        "factory 配置应可加载"
    );

    // 用户层落一份（PUT 的落点），加载必须改读它
    let user_pipe = user_config.join("pipelines").join("autonomous.yaml");
    std::fs::create_dir_all(user_pipe.parent().unwrap()).unwrap();
    std::fs::write(&user_pipe, "name: user_taken_over\n").unwrap();
    assert_eq!(
        crate::pipeline_loader::resolve_pipeline_config_path(&config_root, "autonomous.yaml"),
        user_pipe,
        "用户层文件存在时加载侧必须读它（整体替换）"
    );
}

// ── 插件监管端点（status / validate-all / contract-status） ──

#[tokio::test]
async fn plugins_status_maps_type_activation_host_and_enabled() {
    let mut sys = base_manifest("st_sys", PluginType::System);
    sys.activation = Some(agentos_core::traits::ActivationPolicy::Eager);
    sys.host_type = HostType::InProcess;
    let mut pipe = base_manifest("st_pipe", PluginType::Pipeline);
    pipe.activation = Some(agentos_core::traits::ActivationPolicy::Manual);
    let mut tool = base_manifest("st_tool", PluginType::Tool);
    tool.contributes = Some(json!({"commands": []}));
    tool.http_endpoints = vec![HttpEndpoint {
        route_id: "r".to_string(),
        method: "GET".to_string(),
        path: "/ext/st_tool/r".to_string(),
        auth: None,
        handler_capability: "http.handle".to_string(),
        timeout_ms: None,
        max_concurrency: None,
        description: None,
    }];
    tool.config_files = vec![
        config_mapping("vis", "config/plugins/x.yaml", vec![]),
        ConfigFileMapping {
            settings: Some(false),
            ..config_mapping("hid", "config/plugins/y.yaml", vec![])
        },
    ];
    let composite = base_manifest("st_comp", PluginType::Composite);

    let state = AppState::new();
    *state.manifests.write().await = vec![sys, pipe, tool, composite];
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("st_sys".to_string());
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("st_tool".to_string());

    let resp = plugins_status_handler(axum::extract::State(state)).await;
    let items = resp.0.as_array().unwrap();
    assert_eq!(items.len(), 4);
    let find = |id: &str| {
        items
            .iter()
            .find(|i| i["plugin_id"] == id)
            .unwrap_or_else(|| panic!("{id} 应在列表"))
    };
    let sys = find("st_sys");
    assert_eq!(sys["config_type"], "system");
    assert_eq!(sys["activation"], "eager");
    assert_eq!(sys["host_type"], "in_process");
    assert_eq!(sys["enabled"], true);
    assert_eq!(sys["status"], "active", "enabled 插件运行态 active");
    let pipe = find("st_pipe");
    assert_eq!(pipe["activation"], "manual");
    assert_eq!(pipe["enabled"], false);
    assert_eq!(pipe["status"], "disabled", "disabled 插件运行态 disabled");
    assert_eq!(find("st_comp")["config_type"], "composite");
    let tool = find("st_tool");
    assert_eq!(tool["activation"], "lazy", "缺省 activation 走 lazy");
    assert_eq!(tool["has_contributes"], true);
    assert_eq!(tool["has_http_endpoints"], true);
    let cfg_ids: Vec<&str> = tool["config_files"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|c| c["id"].as_str())
        .collect();
    assert_eq!(cfg_ids, vec!["vis"], "settings:false 条目不出口");
    assert_eq!(
        find("st_pipe")["host_type"],
        "sidecar",
        "缺省 host_type 是 sidecar"
    );
}

/// BUG-51：验证未完成/验证失败插件在 plugins 列表可见原因（error 字段），
/// 用户可在插件管理看到；正常/漂移插件 error 维持 null（漂移有专属净化标示）。
#[tokio::test]
async fn plugins_status_surfaces_verify_failure_reason() {
    use crate::contract::PluginContractState;
    let state = AppState::new();
    *state.manifests.write().await = vec![
        base_manifest("vp_pending", PluginType::Tool),
        base_manifest("vp_drift", PluginType::Tool),
        base_manifest("vp_ok", PluginType::Tool),
    ];
    let manifests = state.manifests.read().await.clone();
    let g2_fail = crate::plugin_watcher::G2VerifyOutcome {
        manifest: manifests[0].clone(),
        rejected_tools: Vec::new(),
        drift: false,
        soft_drift: false,
        spawn_failed: true,
        smoke_failed: false,
        stub_armed: false,
    };
    state.contract_states.upsert(PluginContractState::derived(
        &manifests[0],
        true,
        Some(&g2_fail),
    ));
    let g2_drift = crate::plugin_watcher::G2VerifyOutcome {
        manifest: manifests[1].clone(),
        rejected_tools: vec!["t1".to_string()],
        drift: true,
        soft_drift: false,
        spawn_failed: false,
        smoke_failed: false,
        stub_armed: false,
    };
    state.contract_states.upsert(PluginContractState::derived(
        &manifests[1],
        true,
        Some(&g2_drift),
    ));

    let resp = plugins_status_handler(axum::extract::State(state)).await;
    let items = resp.0.as_array().unwrap();
    let find = |id: &str| {
        items
            .iter()
            .find(|i| i["plugin_id"] == id)
            .unwrap_or_else(|| panic!("{id} 应在列表"))
    };
    let pending = find("vp_pending");
    assert!(
        pending["error"]
            .as_str()
            .expect("待复验插件必须可见原因")
            .contains("复验"),
        "error 应说明校验未完成待复验: {:?}",
        pending["error"]
    );
    assert_eq!(
        find("vp_drift")["error"],
        serde_json::Value::Null,
        "漂移插件不走 error 面（有专属净化标示）"
    );
    assert_eq!(find("vp_ok")["error"], serde_json::Value::Null);
}

#[tokio::test]
async fn validate_all_without_invoker_reports_unavailable() {
    let state = AppState::new();
    *state.manifests.write().await = vec![base_manifest("va_x", PluginType::Tool)];
    let resp = validate_all_plugins_handler(axum::extract::State(state)).await;
    assert_eq!(resp.0["checked"], 0);
    assert_eq!(resp.0["errors"], 1);
    let msg = resp.0["message"].as_str().unwrap();
    assert!(msg.contains("invoker 未接线"), "应说明不可用原因: {msg}");
}

#[tokio::test]
async fn validate_all_non_tool_not_covered_and_non_sidecar_skipped() {
    let invoker = StubInvoker::with_list(Ok(json!({"tools": []})));
    let mut state = AppState::new();
    *state.manifests.write().await = vec![
        // 无 tools 声明 → not_covered，不进 reports
        base_manifest("va_empty", PluginType::System),
        // 有 tools 但 host 非 sidecar → skipped 报告
        {
            let mut m = base_manifest("va_inproc", PluginType::Tool);
            m.host_type = HostType::InProcess;
            m.capabilities.tools = vec![agentos_core::traits::ToolCapability {
                name: "t9".to_string(),
                description: None,
                input_schema: None,
                output_schema: None,
                category: None,
                ui: None,
                render: None,
                isolation_capable: None,
                smoke: None,
                timeout_ms: None,
            }];
            m
        },
    ];
    state.invoker = Some(invoker as Arc<dyn PluginInvoker>);
    let resp = validate_all_plugins_handler(axum::extract::State(state)).await;
    assert_eq!(resp.0["errors"], 0);
    let reports = resp.0["reports"].as_array().unwrap();
    assert_eq!(reports.len(), 1, "仅非 sidecar 进 reports");
    assert_eq!(reports[0]["status"], "skipped");
    assert_eq!(reports[0]["plugin_id"], "va_inproc");
}

#[tokio::test]
async fn validate_all_clean_drift_and_error_paths() {
    let manifest = || {
        let mut m = base_manifest("va_g2", PluginType::Tool);
        m.capabilities.tools = vec![agentos_core::traits::ToolCapability {
            name: "t1".to_string(),
            description: None,
            input_schema: None,
            output_schema: None,
            category: None,
            ui: None,
            render: None,
            isolation_capable: None,
            smoke: None,
            timeout_ms: None,
        }];
        m
    };

    // clean：sidecar 实际上报与声明一致
    let invoker = StubInvoker::with_list(Ok(json!({
        "tools": [{"name": "t1", "description": "d", "inputSchema": {}}]
    })));
    let mut state = AppState::new();
    *state.manifests.write().await = vec![manifest()];
    state.invoker = Some(invoker as Arc<dyn PluginInvoker>);
    let resp = validate_all_plugins_handler(axum::extract::State(state)).await;
    assert_eq!(resp.0["clean"], 1);
    assert_eq!(resp.0["drifted"], 0);
    assert_eq!(resp.0["reports"][0]["status"], "clean");

    // drifted：实际有 t2 缺 t1 → missing + undeclared
    let invoker = StubInvoker::with_list(Ok(json!({
        "tools": [{"name": "t2", "inputSchema": {}}]
    })));
    let mut state = AppState::new();
    *state.manifests.write().await = vec![manifest()];
    state.invoker = Some(invoker as Arc<dyn PluginInvoker>);
    let resp = validate_all_plugins_handler(axum::extract::State(state)).await;
    assert_eq!(resp.0["drifted"], 1);
    let kinds: Vec<&str> = resp.0["reports"][0]["mismatches"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|m| m["kind"].as_str())
        .collect();
    assert!(kinds.contains(&"missing"), "声明有实际无 → missing");
    assert!(kinds.contains(&"undeclared"), "实际有声明无 → undeclared");

    // error：spawn/list 失败 → errors 计数 + status error
    let invoker = StubInvoker::with_list(Err(agentos_core::types::PluginError {
        message: "spawn failed".to_string(),
        code: None,
        source: None,
    }));
    let mut state = AppState::new();
    *state.manifests.write().await = vec![manifest()];
    state.invoker = Some(invoker as Arc<dyn PluginInvoker>);
    let resp = validate_all_plugins_handler(axum::extract::State(state)).await;
    assert_eq!(resp.0["errors"], 1);
    assert_eq!(resp.0["reports"][0]["status"], "error");
}

#[tokio::test]
async fn validate_all_detects_disk_manifest_mismatch_and_unreadable() {
    let manifest = || {
        let mut m = base_manifest("va_disk", PluginType::Tool);
        m.capabilities.tools = vec![agentos_core::traits::ToolCapability {
            name: "t1".to_string(),
            description: None,
            input_schema: None,
            output_schema: None,
            category: None,
            ui: None,
            render: None,
            isolation_capable: None,
            smoke: None,
            timeout_ms: None,
        }];
        m
    };
    let invoker = StubInvoker::with_list(Ok(json!({
        "tools": [{"name": "t1", "inputSchema": {}}]
    })));

    // 磁盘 manifest 缺该工具 → registry_disk_mismatch 报告
    let tmp = tempfile::tempdir().unwrap();
    let plugin_dir = tmp.path().join("va_disk");
    std::fs::create_dir_all(&plugin_dir).unwrap();
    let mut disk = manifest();
    disk.capabilities.tools.clear();
    std::fs::write(
        plugin_dir.join("plugin.json"),
        serde_json::to_string(&disk).unwrap(),
    )
    .unwrap();
    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());
    state.plugin_loader = Some(crate::test_env::discovered_plugin_loader(tmp.path()).await);
    *state.manifests.write().await = vec![manifest()];
    state.invoker = Some(invoker as Arc<dyn PluginInvoker>);
    let resp = validate_all_plugins_handler(axum::extract::State(state)).await;
    assert_eq!(
        resp.0["registry_disk_mismatches"], 1,
        "注册表含磁盘未声明工具应检出"
    );
    let cr = resp.0["consistency_reports"].as_array().unwrap();
    assert_eq!(cr.len(), 1);
    assert_eq!(cr[0]["status"], "registry_disk_mismatch");
    assert_eq!(cr[0]["diffs"][0]["kind"], "extra_tool");

    // 无目录映射 → disk_manifest_unreadable（不可读 ≠ 一致，不静默当绿）
    let invoker = StubInvoker::with_list(Ok(json!({"tools": []})));
    let mut state = AppState::new();
    *state.manifests.write().await = vec![base_manifest("va_nodir", PluginType::System)];
    state.invoker = Some(invoker as Arc<dyn PluginInvoker>);
    let resp = validate_all_plugins_handler(axum::extract::State(state)).await;
    let cr = resp.0["consistency_reports"].as_array().unwrap();
    assert_eq!(cr.len(), 1);
    assert_eq!(cr[0]["status"], "disk_manifest_unreadable");
    assert!(cr[0]["reason"].as_str().unwrap().contains("路径表"));
}

/// BUG-51 验证链：合宿成员的 validate-all 观测域化——宿主聚合上报剥本成员
/// 前缀、剔他组前缀条目后对照；成员声明工具不再误报 missing（虚假剔除判定）。
#[tokio::test]
async fn validate_all_scopes_cohost_member_report_to_member() {
    let mut m = base_manifest("va_mem", PluginType::Tool);
    m.host_group = Some("light".to_string());
    m.capabilities.tools = vec![agentos_core::traits::ToolCapability {
        name: "t1".to_string(),
        description: None,
        input_schema: None,
        output_schema: None,
        category: None,
        ui: None,
        render: None,
        isolation_capable: None,
        smoke: None,
        timeout_ms: None,
    }];
    // 宿主聚合面：本成员带前缀 + 他组前缀条目
    let invoker = StubInvoker::with_list(Ok(json!({
        "tools": [
            {"name": "va_mem.t1", "inputSchema": {}},
            {"name": "peer_mem.other", "inputSchema": {}}
        ]
    })));
    let mut state = AppState::new();
    *state.manifests.write().await = vec![m.clone()];
    state.invoker = Some(invoker as Arc<dyn PluginInvoker>);
    let ledger = state.contract_states.clone();
    let resp = validate_all_plugins_handler(axum::extract::State(state)).await;
    let report = resp.0["reports"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["plugin_id"] == "va_mem")
        .expect("va_mem 应在报告")
        .clone();
    assert_eq!(
        report["status"], "clean",
        "成员域化对照干净 → clean（他组条目不计入）: {report}"
    );
    let mismatches = report["mismatches"].as_array().unwrap();
    assert!(
        mismatches.is_empty(),
        "本成员声明工具不得误报 missing: {mismatches:?}"
    );
    let gates = ledger.get("va_mem").expect("账本应登记 va_mem");
    assert!(
        gates.gates.rejected_tools.is_empty(),
        "不得产生虚假剔除名单: {:?}",
        gates.gates.rejected_tools
    );
}

#[tokio::test]
async fn contract_status_defaults_not_covered_and_reflects_ledger() {
    let mut m = base_manifest("cs_x", PluginType::Tool);
    m.export_fields = vec![];
    let state = AppState::new();
    *state.manifests.write().await = vec![m];
    state
        .enabled_plugin_ids
        .write()
        .await
        .insert("cs_x".to_string());
    // 未登记 → not_covered 缺省（诚实标未覆盖）
    let resp = plugins_contract_status_handler(axum::extract::State(state)).await;
    assert_eq!(resp.0["count"], 1);
    let plugins = resp.0["plugins"].as_array().unwrap();
    assert_eq!(plugins[0]["plugin_id"], "cs_x");
    assert_eq!(plugins[0]["enabled"], true);
    assert_eq!(
        plugins[0]["gates"]["g2_consistency"], "not_covered",
        "未登记插件补 not_covered 缺省"
    );
}

// ── G8 排空重启 ─────────────────────────────────────────

#[tokio::test]
async fn system_restart_drains_running_runs_and_reports() {
    // 测试逃生门：禁自退出（只排空不 exit，不杀 sidecar）
    std::env::set_var("AGENTOS_DISABLE_SELF_EXIT", "1");

    let db = sqlite();
    db.record_run_start(
        "pipe_restart_1",
        "default",
        "run_restart_1",
        "hash",
        "2026-10-15T00:00:00+00:00",
    )
    .unwrap();
    let mut state = AppState::new();
    state.db = Some(db.clone());

    let resp = system_restart_handler(axum::extract::State(state)).await;
    assert_eq!(resp.0["success"], true);
    assert_eq!(resp.0["exit_code"], 75);
    assert_eq!(
        resp.0["suspended_runs"], 1,
        "在途 running run 应被排空为 suspended"
    );
    let run = StorageBackend::get_run(db.as_ref(), "run_restart_1")
        .await
        .expect("run 记录应存在");
    assert!(
        matches!(run.status, agentos_core::types::RunStatus::Suspended),
        "排空后 run 状态应为 suspended，实际 {:?}",
        run.status
    );
}
// ── 批次C 宿主盒子快照端点（GET /api/v1/plugins/hosts） ──────────────

/// 空插件加载器（路由测试只需可构造的 invoker，不触达插件面）。
struct HostsStubLoader;

#[async_trait::async_trait]
impl agentos_core::traits::PluginLoader for HostsStubLoader {
    async fn discover(
        &self,
        _root_paths: &[&str],
    ) -> Result<Vec<PluginManifest>, agentos_core::types::PluginError> {
        Ok(vec![])
    }

    fn validate_manifest(
        &self,
        _manifest: &PluginManifest,
    ) -> Result<(), agentos_core::types::PluginError> {
        Ok(())
    }

    async fn load(
        &self,
        _plugin_id: &str,
    ) -> Result<agentos_core::traits::LoadedPlugin, agentos_core::types::PluginError> {
        Err(agentos_core::types::PluginError {
            message: "stub loader".to_string(),
            code: None,
            source: None,
        })
    }

    async fn unload(&self, _plugin_id: &str) -> Result<(), agentos_core::types::PluginError> {
        Ok(())
    }

    fn get_status(&self, _plugin_id: &str) -> agentos_core::traits::PluginStatus {
        agentos_core::traits::PluginStatus::Discovered
    }
}

/// 内存库播种 admin 的路由 app + access token，可注入 invoker 与启用清单
/// （种子/铸 token 与 [`app_with_admin_token`] 同源同构，多注 invoker）。
async fn hosts_app_with_invoker(
    invoker: Option<Arc<dyn PluginInvoker>>,
    enabled_ids: &[&str],
) -> (axum::Router, String) {
    let store = sqlite();
    let password_hash = agentos_http::auth::hash_password(ADMIN_PW).unwrap();
    store
        .create_user(&agentos_core::types::UserRecord {
            user_id: ADMIN_USER_ID.to_string(),
            username: "admin".to_string(),
            password: password_hash.clone(),
            email: None,
            role: "admin".to_string(),
            tenant_id: agentos_http::auth::DEFAULT_TENANT_ID.to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            last_login_at: None,
            must_change_password: false,
        })
        .await
        .unwrap();
    let admin = agentos_http::auth::BuiltInUser {
        id: ADMIN_USER_ID.to_string(),
        username: "admin".to_string(),
        password: password_hash,
        email: String::new(),
        role: "admin".to_string(),
        tenant_id: "default".to_string(),
        created_at: String::new(),
        must_change_password: false,
    };
    let token =
        agentos_http::auth::encode_token(agentos_http::auth::TokenType::Access, &admin, 3600);
    let mut state = AppState::new();
    state.store = Some(store);
    state.invoker = invoker;
    state.enabled_plugin_ids = Arc::new(tokio::sync::RwLock::new(
        enabled_ids.iter().map(|s| s.to_string()).collect(),
    ));
    (crate::server::build_router(state), token)
}

/// 白名单读面拒绝匿名（与 memstats 同语义链：缺凭证 401，
/// 403 保留给有凭证但角色不足）。
#[tokio::test]
async fn hosts_endpoint_without_token_unauthorized() {
    let (app, _) = hosts_app_with_invoker(None, &[]).await;
    let resp = oneshot(&app, "GET", "/api/v1/plugins/hosts", None, None).await;
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
}

/// invoker 未装配（裸测试构造）→ 200 + 冻结顶层空骨架——只读诊断面
/// 诚实表达「无盒子」，不 500。
#[tokio::test]
async fn hosts_endpoint_unwired_invoker_returns_empty_frozen_shape() {
    let (app, token) = hosts_app_with_invoker(None, &[]).await;
    let resp = oneshot(&app, "GET", "/api/v1/plugins/hosts", Some(&token), None).await;
    assert_eq!(resp.status(), StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 65536).await.unwrap();
    let v: Value = serde_json::from_slice(&body).unwrap();
    let obj = v.as_object().expect("响应必须是 JSON 对象");
    assert_eq!(obj.len(), 2, "顶层字段冻结为 hosts + pending_spawn");
    assert!(obj.contains_key("hosts") && obj.contains_key("pending_spawn"));
    assert_eq!(v["hosts"], json!([]));
    assert_eq!(v["pending_spawn"], json!([]));
}

/// 已接线空 invoker → 200 + 同一冻结空骨架（Some 分支：直调
/// invoker.host_boxes_snapshot 取数）。
#[tokio::test]
async fn hosts_endpoint_wired_invoker_returns_snapshot_shape() {
    let invoker = Arc::new(agentos_invoker::PluginInvokerImpl::new(Arc::new(
        HostsStubLoader,
    )));
    let (app, token) = hosts_app_with_invoker(Some(invoker), &[]).await;
    let resp = oneshot(&app, "GET", "/api/v1/plugins/hosts", Some(&token), None).await;
    assert_eq!(resp.status(), StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 65536).await.unwrap();
    let v: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(v["hosts"], json!([]));
    assert_eq!(v["pending_spawn"], json!([]));
}

/// 差集补全（handler 级）：从未触达 invoker 的启用插件进「待装载」区
/// （metrics_admin 型缺口观测闭环）——AppState 启用清单 − 盒子成员全集，
/// 补条目 enabled=true / last_call_at=null，按 plugin_id 排序。
#[tokio::test]
async fn hosts_endpoint_unwired_invoker_lists_enabled_plugins_as_pending_spawn() {
    let (app, token) = hosts_app_with_invoker(None, &["metrics_admin", "aaa_untouched"]).await;
    let resp = oneshot(&app, "GET", "/api/v1/plugins/hosts", Some(&token), None).await;
    assert_eq!(resp.status(), StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 65536).await.unwrap();
    let v: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(v["hosts"], json!([]));
    assert_eq!(
        v["pending_spawn"],
        json!([
            {"plugin_id": "aaa_untouched", "enabled": true, "last_call_at": null},
            {"plugin_id": "metrics_admin", "enabled": true, "last_call_at": null},
        ])
    );
}

/// merge 纯函数·从未触达场景：启用清单减盒子成员全集的差集补入；
/// 已装载插件（在盒子 members 中）不重复出现。
#[test]
fn merge_pending_spawn_fills_never_touched_enabled_plugins() {
    let mut boxed = std::collections::HashSet::new();
    boxed.insert("boxed_a".to_string());
    let mut enabled = std::collections::HashSet::new();
    enabled.insert("boxed_a".to_string());
    enabled.insert("never_touched".to_string());
    let merged = super::merge_pending_spawn(vec![], &enabled, &boxed);
    assert_eq!(
        merged,
        vec![PendingSpawnEntry {
            plugin_id: "never_touched".to_string(),
            enabled: true,
            last_call_at: None,
        }],
        "已装载的 boxed_a 不入差集，从未触达的 never_touched 补入"
    );
}

/// merge 纯函数·去重优先级场景：invoker 记账条目（enabled=false / 带
/// last_call_at）优先保留，AppState 侧不重复补入；输出按 plugin_id 排序。
#[test]
fn merge_pending_spawn_invoker_entry_wins_and_output_sorted() {
    let mut boxed = std::collections::HashSet::new();
    boxed.insert("boxed_a".to_string());
    let mut enabled = std::collections::HashSet::new();
    enabled.insert("boxed_a".to_string());
    enabled.insert("ghost".to_string());
    enabled.insert("aaa".to_string());
    let invoker_entries = vec![PendingSpawnEntry {
        plugin_id: "ghost".to_string(),
        enabled: false,
        last_call_at: Some(7.0),
    }];
    let merged = super::merge_pending_spawn(invoker_entries, &enabled, &boxed);
    assert_eq!(
        merged,
        vec![
            PendingSpawnEntry {
                plugin_id: "aaa".to_string(),
                enabled: true,
                last_call_at: None,
            },
            PendingSpawnEntry {
                plugin_id: "ghost".to_string(),
                enabled: false,
                last_call_at: Some(7.0),
            },
        ],
        "invoker 条目原样保留（记账 enabled 面不丢），AppState 不补第二条 ghost"
    );
}

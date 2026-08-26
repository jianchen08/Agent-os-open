// @feature: FP-0.2.〇 管道引擎 | @ci: rust-test
//! GET /api/v1/sessions/{id}/messages 工具字段内容契约测试（TDD）。
//!
//! 契约：持久化了 `tool_result_json` envelope 的 tool 消息，HTTP 返回必须携带
//! 结构化字段（camelCase，对齐前端 BackendMessageResponse）：
//! - `toolResultData`（envelope.data，结构化完整结果）
//! - `toolDurationMs`（envelope.duration_ms）
//! - `toolName`（envelope.tool_name）
//! - `containerTaskId`（envelope.metadata.container_task_id 存在时）
//!
//! 这是前端刷新后还原 resultData/durationMs 的 HTTP 通道——缺失即冷热不一致。
//!
//! 既有字段回归：toolCalls / toolCallId / status / error / reasoningContent。

use std::fs;
use std::sync::Arc;

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use agentos_core::traits::{
    ConfigFileMapping, HostType, PluginManifest, PluginType, StorageBackend,
};
use axum::body::Body;
use axum::http::{Method, Request, StatusCode};
use serde_json::{json, Value};
use tokio::sync::RwLock;
use tower::ServiceExt;

const PID: &str = "p-api-tool-1";

async fn app_with_deps() -> (
    tempfile::TempDir,
    axum::Router,
    Arc<agentos_engine::SqliteStore>,
) {
    let tmp = tempfile::tempdir().unwrap();

    let agent_dir = tmp.path().join("config").join("agents").join("main");
    fs::create_dir_all(&agent_dir).unwrap();
    fs::write(
        agent_dir.join("test_agent.yaml"),
        "config_id: test_agent\nname: t\n",
    )
    .unwrap();

    let pipe_dir = tmp.path().join("config").join("pipelines");
    fs::create_dir_all(&pipe_dir).unwrap();
    fs::write(pipe_dir.join("default.yaml"), "name: default\n").unwrap();

    let model_dir = tmp.path().join("config").join("models");
    fs::create_dir_all(&model_dir).unwrap();
    fs::write(
        model_dir.join("llm.yaml"),
        "name: glm\napi_key: ${ENV_KEY}\n",
    )
    .unwrap();

    let manifest = PluginManifest {
        id: "llm_service".to_string(),
        name: "llm_service".to_string(),
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
        config_files: vec![ConfigFileMapping {
            id: "llm".to_string(),
            path: "config/models/llm.yaml".to_string(),
            label: "LLM".to_string(),
            target: None,
            fields: vec![],
        }],
        http_endpoints: vec![],
        ui_schema: None,
        contributes: None,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        provides: None,
    };

    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    // 播种 admin 对齐生产启动行为（seed_admin_user）：auth 加固后
    // store 存在但用户名未命中不再回退内置硬编码凭据
    store
        .create_user(&agentos_core::types::UserRecord {
            user_id: "00000000-0000-0000-0000-000000000001".to_string(),
            username: "admin".to_string(),
            password: "admin12345".to_string(),
            email: Some("admin@agentos.dev".to_string()),
            role: "admin".to_string(),
            tenant_id: "default".to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            last_login_at: None,
        })
        .await
        .unwrap();
    let mut state = AppState::new();
    state.store = Some(store.clone());
    state.manifests = Arc::new(RwLock::new(vec![manifest]));
    state.project_root = Some(tmp.path().to_path_buf());
    (tmp, build_router(state), store)
}

async fn admin_token(app: &axum::Router) -> String {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method(Method::POST)
                .uri("/api/v1/auth/login")
                .header("content-type", "application/json")
                .body(Body::from(
                    json!({"username": "admin", "password": "admin12345"}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK, "admin 登录应成功");
    let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
    let json: Value = serde_json::from_slice(&body).unwrap();
    json["access_token"].as_str().unwrap().to_string()
}

async fn get_messages_json(app: &axum::Router, token: &str) -> Value {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method(Method::GET)
                .uri(format!(
                    "/api/v1/sessions/thr-tool-1/messages?pipeline_run_id={PID}"
                ))
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK, "admin 读取消息应 200");
    let body = axum::body::to_bytes(resp.into_body(), 1 << 20)
        .await
        .unwrap();
    serde_json::from_slice(&body).unwrap()
}

/// 写入一条带 envelope 的完整工具调用轮次（assistant tool_calls + tool 结果）。
/// envelope 随消息持久化（tool 消息自带 tool_result 字段，整条进 blob）。
fn seed_enriched_tool_turn(store: &agentos_engine::SqliteStore) {
    let msgs = [
        json!({
            "role": "user",
            "content": "写文件"
        }),
        json!({
            "role": "assistant",
            "content": "",
            "reasoning_content": "思考中",
            "tool_calls": [{
                "id": "call_a1",
                "type": "function",
                "function": { "name": "file_write", "arguments": "{\"file_path\":\"a.rs\"}" }
            }]
        }),
        json!({
            "role": "tool",
            "tool_call_id": "call_a1",
            "content": "added: 2\nlines: 1\n",
            "tool_result": {
                "call_id": "call_a1",
                "tool_name": "file_write",
                "success": true,
                "error": null,
                "data": { "added": 2, "lines": 1, "new_content": "fn main() {}" },
                "metadata": { "container_task_id": "task_api_1" },
                "duration_ms": 88.8
            }
        }),
    ];
    let ops: Vec<Value> = msgs
        .iter()
        .enumerate()
        .map(|(i, m)| json!({ "op": "set", "seq": i as u64, "msg": m }))
        .collect();
    store
        .apply_messages_ops_to_table(PID, "default", &ops)
        .unwrap();
}

#[tokio::test]
async fn tool_message_returns_structured_envelope_fields() {
    let (_tmp, app, store) = app_with_deps().await;
    seed_enriched_tool_turn(&store);
    let token = admin_token(&app).await;

    let body = get_messages_json(&app, &token).await;
    let messages = body["messages"].as_array().expect("messages 数组");
    let tool_msg = messages
        .iter()
        .find(|m| m["role"] == "tool")
        .expect("tool 消息必须存在");

    // ── 新契约字段（camelCase 对齐前端 BackendMessageResponse） ──
    assert_eq!(
        tool_msg["toolResultData"]["added"], 2,
        "toolResultData 必须返回 envelope.data"
    );
    assert_eq!(tool_msg["toolResultData"]["new_content"], "fn main() {}");
    assert_eq!(
        tool_msg["toolDurationMs"], 88.8,
        "toolDurationMs 必须返回 envelope.duration_ms"
    );
    assert_eq!(
        tool_msg["toolName"], "file_write",
        "toolName 必须返回 envelope.tool_name"
    );
    assert_eq!(
        tool_msg["containerTaskId"], "task_api_1",
        "envelope.metadata.container_task_id 存在时必须返回 containerTaskId"
    );

    // ── content 契约：必须返回纯文本，而非整条消息 envelope JSON ──
    // （回归保护：blob 存的是整条消息 JSON，HTTP 读路径不得把裸 blob 当 content，
    //   否则前端气泡显示 {"content":"...","role":"..."} 原始字段。）
    let user_msg = messages
        .iter()
        .find(|m| m["role"] == "user")
        .expect("user 消息必须存在");
    assert_eq!(
        user_msg["content"], "写文件",
        "user 消息 content 必须是纯文本，不能是 envelope JSON"
    );
    assert_eq!(
        tool_msg["content"], "added: 2\nlines: 1\n",
        "tool 消息 content 必须是纯文本，不能是 envelope JSON"
    );

    // ── 既有字段回归 ──
    assert_eq!(tool_msg["toolCallId"], "call_a1");
    assert_eq!(tool_msg["status"], "completed");
}

#[tokio::test]
async fn assistant_tool_calls_and_reasoning_unchanged() {
    let (_tmp, app, store) = app_with_deps().await;
    seed_enriched_tool_turn(&store);
    let token = admin_token(&app).await;

    let body = get_messages_json(&app, &token).await;
    let messages = body["messages"].as_array().expect("messages 数组");
    let assistant = messages
        .iter()
        .find(|m| m["role"] == "assistant")
        .expect("assistant 消息必须存在");

    assert_eq!(assistant["toolCalls"][0]["id"], "call_a1");
    assert_eq!(assistant["toolCalls"][0]["function"]["name"], "file_write");
    assert_eq!(assistant["reasoningContent"], "思考中");
}

#[tokio::test]
async fn failed_tool_message_returns_error_fields() {
    let (_tmp, app, store) = app_with_deps().await;
    store
        .apply_messages_ops_to_table(
            PID,
            "default",
            &[json!({
                "op": "set", "seq": 0,
                "msg": {
                    "role": "tool",
                    "tool_call_id": "call_f1",
                    "content": "操作未成功",
                    "tool_result": {
                        "call_id": "call_f1",
                        "tool_name": "bash_execute",
                        "success": false,
                        "error": "boom",
                        "data": null,
                        "metadata": null,
                        "duration_ms": 5.0
                    }
                }
            })],
        )
        .unwrap();
    let token = admin_token(&app).await;

    let body = get_messages_json(&app, &token).await;
    let messages = body["messages"].as_array().expect("messages 数组");
    let tool_msg = messages
        .iter()
        .find(|m| m["role"] == "tool")
        .expect("tool 消息");

    assert_eq!(tool_msg["status"], "failed");
    assert_eq!(tool_msg["error"], "boom");
    assert_eq!(tool_msg["toolName"], "bash_execute");
    assert_eq!(tool_msg["toolDurationMs"], 5.0);
}

/// ADR 2026-08-21 消息幂等契约：user 消息 metadata.client_message_id 必须经
/// HTTP 原样回显——前端乐观消息对账去重的唯一桥接键，缺失即重复气泡复发。
#[tokio::test]
async fn user_message_metadata_client_message_id_echoed() {
    let (_tmp, app, store) = app_with_deps().await;
    store
        .apply_messages_ops_to_table(
            PID,
            "default",
            &[json!({
                "op": "set", "seq": 0,
                "msg": {
                    "role": "user",
                    "content": "幂等键回显",
                    "metadata": { "client_message_id": "0198-cmid-echo-1" }
                }
            })],
        )
        .unwrap();
    let token = admin_token(&app).await;

    let body = get_messages_json(&app, &token).await;
    let messages = body["messages"].as_array().expect("messages 数组");
    let user_msg = messages
        .iter()
        .find(|m| m["role"] == "user")
        .expect("user 消息");

    assert_eq!(
        user_msg["metadata"]["client_message_id"], "0198-cmid-echo-1",
        "metadata.client_message_id 必须原样回显（前端对账去重桥接键）"
    );
    assert_eq!(user_msg["content"], "幂等键回显", "content 契约不回归");

    // 无 metadata 的消息（历史数据/触发器注入）不携带 metadata 字段（不造空对象）
    store
        .apply_messages_ops_to_table(
            PID,
            "default",
            &[json!({"op": "set", "seq": 1, "msg": {"role": "user", "content": "无键"}})],
        )
        .unwrap();
    let body2 = get_messages_json(&app, &token).await;
    let plain = body2["messages"]
        .as_array()
        .expect("messages 数组")
        .iter()
        .find(|m| m["content"] == "无键")
        .expect("无键消息");
    assert!(
        plain.get("metadata").is_none(),
        "无 metadata 的消息不得回显空对象"
    );
}

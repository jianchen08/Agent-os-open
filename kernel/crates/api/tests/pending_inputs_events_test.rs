//! pending 输入队列补测（ADR-2026-08-26 diff-cov 缺口）
//!
//! 覆盖三块 lib 单测未触及的分支：
//! 1. dispatch_user_input 带 store+session 全链路：引擎成功 → new_message/stream_end 推送；
//!    引擎失败（invoker Err）→ stream_error 推送（ws_session run_pipeline_round 分支）。
//! 2. 端点变更（PUT/DELETE/clear）→ pending_inputs_changed 事件送达（routes 事件推送函数）。
//! 3. 独立集成测试（不依赖 server.rs 私有测试夹具，避免并发编辑冲突）。

use std::sync::{Arc, Mutex};

use agentos_api::routes::AppState;
use agentos_api::server::build_router;
use agentos_core::traits::{HostType, PluginManifest, PluginType};
use agentos_core::types::{
    LoopBody, PendingInputRecord, PendingInputSource, PipelineConfig, PipelineStep, PluginContext,
    PluginResult,
};
use agentos_session::SessionCoordinator;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::json;
use tower::ServiceExt;

/// 帧捕获 sink：记录收到的全部事件帧文本。
#[derive(Clone)]
struct FrameSink {
    frames: Arc<Mutex<Vec<String>>>,
}

#[async_trait::async_trait]
impl agentos_session::EventSink for FrameSink {
    async fn send_text(&self, text: &str) -> bool {
        self.frames.lock().unwrap().push(text.to_string());
        true
    }
    fn id(&self) -> u64 {
        42
    }
}

/// 最小 PluginManifest（对齐 e2e_path_coverage_test 的 manifest_base）。
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
        contributes: None,
        enabled: None,
        activation: None,
        persistent_fields: vec![],
        provides: None,
    }
}

/// 成功 invoker：模拟 LLM 回复（与 server.rs RecordingInvoker 同款行为）。
struct OkInvoker;
#[async_trait::async_trait]
impl agentos_core::traits::PluginInvoker for OkInvoker {
    async fn invoke_pipeline_plugin(
        &self,
        _plugin_id: &str,
        ctx: &PluginContext,
    ) -> Result<PluginResult, agentos_core::types::PluginError> {
        let history = ctx
            .state
            .get("messages")
            .cloned()
            .unwrap_or_else(|| json!([]));
        let reply = json!({
            "role": "assistant",
            "content": format!(
                "回复第{}条",
                history.as_array().map(|a| a.len()).unwrap_or(1)
            ),
        });
        let mut updates = std::collections::HashMap::new();
        updates.insert(
            "raw_result".to_string(),
            json!(reply["content"].as_str().unwrap_or("")),
        );
        updates.insert(
            "messages".to_string(),
            json!({ "_ops": [{ "op": "set", "msg": reply }] }),
        );
        Ok(PluginResult {
            state_updates: updates,
            ..Default::default()
        })
    }
    async fn invoke_tool(
        &self,
        _plugin_id: &str,
        _tool_name: &str,
        _inputs: &serde_json::Value,
    ) -> Result<agentos_core::types::ToolExecutionResult, agentos_core::types::PluginError> {
        Ok(agentos_core::types::ToolExecutionResult::success(json!({})))
    }
    async fn send_lifecycle_hook(
        &self,
        _plugin_id: &str,
        _hook: agentos_core::traits::LifecycleHook,
        _context: &agentos_core::traits::HookContext,
    ) -> Result<(), agentos_core::types::PluginError> {
        Ok(())
    }
}

/// 构造带完整引擎装配的 AppState（store + invoker + session + mock 管道配置）。
///
/// 临时项目根含 `config/pipelines/autonomous.yaml`（引用 mock_llm_core 插件），
/// 使 Pull 热加载能编译出真实管道（否则 load_pipeline_config 文件缺失返回空
/// 配置，executor 不调用任何插件）。
///
/// `broken_route=true` 时给 step 装一条恒真自跳路由（`Step("llm")` 跳自身）——
/// 编译期目标存在可通过，运行期跳转次数超限 → `run_compiled` 返回 Err
/// （invoker 错误只 warn+continue，不会置 failed，无法触发 stream_error 分支）。
fn make_engine_state(
    store: Arc<agentos_engine::SqliteStore>,
    invoker: Arc<dyn agentos_core::traits::PluginInvoker>,
    session: Arc<SessionCoordinator>,
    broken_route: bool,
) -> AppState {
    let store_dyn: Arc<dyn agentos_core::traits::StorageBackend> = store.clone();
    let mut state = AppState::new();
    state.store = Some(store_dyn);
    state.invoker = Some(invoker);
    state.session = Some(session);
    let tmp_root =
        std::env::temp_dir().join(format!("pending_test_{}", uuid::Uuid::new_v4().simple()));
    let cfg_dir = tmp_root.join("config").join("pipelines");
    std::fs::create_dir_all(&cfg_dir).unwrap();
    // broken_route：step 级恒真自跳路由（then: llm 跳自身）——编译期目标存在
    // 可通过，运行期跳转次数超限 → run_compiled 返回 Err。
    let yaml = if broken_route {
        "name: test_pending\nloop_bodies:\n  - id: llm\n    steps:\n      - id: llm\n        steps:\n          - mock_llm_core\n        next:\n          - when: \"True\"\n            then: llm\n"
    } else {
        "name: test_pending\nloop_bodies:\n  - id: llm\n    steps:\n      - id: llm\n        steps:\n          - mock_llm_core\n"
    };
    std::fs::write(cfg_dir.join("autonomous.yaml"), yaml).unwrap();
    state.project_root = Some(tmp_root);
    state.pipeline_config = Arc::new(PipelineConfig {
        name: "test_pending".to_string(),
        loop_bodies: vec![LoopBody {
            id: "llm".to_string(),
            steps: vec![PipelineStep {
                id: "llm".to_string(),
                steps: vec!["mock_llm_core".into()],
                when: None,
                context: std::collections::HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
            while_cond: None,
            exit_routes: vec![],
            run_on_error: false,
        }],
        checkpoint: Default::default(),
    });
    state.step_library = Arc::new(agentos_core::types::StepLibrary::default());
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest_base(
        "mock_llm_core",
    )]));
    state
}

/// 等帧辅助：轮询直到帧列表出现含 needle 的帧。
async fn wait_for_frame(frames: &Arc<Mutex<Vec<String>>>, needle: &str) {
    let frames = frames.clone();
    tokio::time::timeout(std::time::Duration::from_secs(8), async {
        loop {
            if frames.lock().unwrap().iter().any(|f| f.contains(needle)) {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("应收到事件帧");
}

#[tokio::test]
async fn dispatch_success_pushes_new_message_and_stream_end() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let coord = Arc::new(SessionCoordinator::new());
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register(
        "u1",
        Arc::new(FrameSink {
            frames: frames.clone(),
        }),
    );
    coord.register_thread("thread-ok-1", "u1");
    let state = make_engine_state(store, Arc::new(OkInvoker), coord, false);

    let dispatcher = agentos_api::ws_session::EngineDispatcher::new(state);
    use agentos_session::router::PipelineDispatcher;
    dispatcher
        .dispatch_user_input(
            "thread-ok-1",
            "u1",
            "嗨",
            "",
            "",
            None,
            None,
            "agentos",
            "cmid-ok-1",
            PendingInputSource::User,
        )
        .await
        .unwrap();

    // 成功路径：stream_start → new_message → stream_end 全链路帧
    wait_for_frame(&frames, "\"type\":\"new_message\"").await;
    wait_for_frame(&frames, "\"type\":\"stream_end\"").await;
    let all = frames.lock().unwrap();
    let has_start = all.iter().any(|f| f.contains("\"type\":\"stream_start\""));
    assert!(has_start, "应先 stream_start");
}

#[tokio::test]
async fn dispatch_engine_failure_pushes_stream_error() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let coord = Arc::new(SessionCoordinator::new());
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register(
        "u1",
        Arc::new(FrameSink {
            frames: frames.clone(),
        }),
    );
    coord.register_thread("thread-fail-1", "u1");
    let state = make_engine_state(store, Arc::new(OkInvoker), coord, true);

    let dispatcher = agentos_api::ws_session::EngineDispatcher::new(state);
    use agentos_session::router::PipelineDispatcher;
    dispatcher
        .dispatch_user_input(
            "thread-fail-1",
            "u1",
            "会失败",
            "",
            "",
            None,
            None,
            "agentos",
            "",
            PendingInputSource::User,
        )
        .await
        .unwrap();

    wait_for_frame(&frames, "\"type\":\"stream_error\"").await;
    let all = frames.lock().unwrap();
    let err = all
        .iter()
        .find(|f| f.contains("\"type\":\"stream_error\""))
        .unwrap();
    let parsed: serde_json::Value = serde_json::from_str(err).unwrap();
    assert_eq!(parsed["data"]["error"]["code"], "ENGINE_RUN_FAILED");
}

#[tokio::test]
async fn endpoint_update_emits_pending_inputs_changed() {
    // 造 store + 一条 pending + pipeline↔thread 映射 + session
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let store_dyn: Arc<dyn agentos_core::traits::StorageBackend> = store.clone();
    let pid = "pipe-evt-1";
    store_dyn
        .enqueue_pending_input(
            "default",
            pid,
            &PendingInputRecord {
                id: "p_evt_1".to_string(),
                pipeline_id: pid.to_string(),
                tenant_id: "default".to_string(),
                user_id: "u1".to_string(),
                content: "旧内容".to_string(),
                thread: "thread-evt-1".to_string(),
                source: PendingInputSource::Trigger,
                agent_id: "agentos".to_string(),
                route_id: pid.to_string(),
                thinking_strength: String::new(),
                client_message_id: String::new(),
                execution_context: None,
                state_overlay: None,
                created_at: chrono::Utc::now().to_rfc3339(),
            },
        )
        .await
        .unwrap();
    store_dyn
        .link_pipeline_session(pid, "thread-evt-1", "default")
        .await
        .unwrap();

    let coord = Arc::new(SessionCoordinator::new());
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    coord.register(
        "u1",
        Arc::new(FrameSink {
            frames: frames.clone(),
        }),
    );
    coord.register_thread("thread-evt-1", "u1");

    let mut state = AppState::new();
    state.store = Some(store_dyn);
    state.session = Some(coord);
    let app = build_router(state);

    // PUT 触发 pending_inputs_changed（updated）
    let resp = app
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri(format!("/api/v1/pipelines/{pid}/pending-inputs/p_evt_1"))
                .header("content-type", "application/json")
                .body(Body::from(r#"{"content":"新内容"}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    wait_for_frame(&frames, "pending_inputs_changed").await;
    let all = frames.lock().unwrap();
    let evt = all
        .iter()
        .find(|f| f.contains("pending_inputs_changed"))
        .expect("应收到 pending_inputs_changed 帧");
    let parsed: serde_json::Value = serde_json::from_str(evt).unwrap();
    assert_eq!(parsed["data"]["action"], "updated");
    assert_eq!(parsed["data"]["items"][0]["content"], "新内容");
}

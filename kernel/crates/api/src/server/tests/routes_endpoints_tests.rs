use super::*;
use crate::routes::{
    get_pipeline_config_handler, get_plugin_config_handler, ActionsExecuteRequest, EnabledBody,
    PipelineConfigUpdateRequest, PluginConfigUpdateRequest,
};
use agentos_core::traits::{HookContext, LifecycleHook, PluginInvoker, PluginManifest};
use agentos_core::types::{PluginContext, PluginError, PluginResult, ToolExecutionResult};
use axum::http::HeaderMap;

/// 记录 invoke_tool 调用并可注入失败的 invoker（其余方法本组测试不可达）。
struct ToolCallInvoker {
    calls: std::sync::Mutex<Vec<(String, String, serde_json::Value)>>,
    fail: bool,
}

#[async_trait::async_trait]
impl PluginInvoker for ToolCallInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        _ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        unimplemented!("actions 测试不触达管道插件调用")
    }
    async fn invoke_tool(
        &self,
        plugin_id: &str,
        tool_name: &str,
        inputs: &serde_json::Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        self.calls.lock().unwrap().push((
            plugin_id.to_string(),
            tool_name.to_string(),
            inputs.clone(),
        ));
        if self.fail {
            return Err(PluginError {
                message: "sidecar 执行失败".to_string(),
                code: None,
                source: None,
            });
        }
        Ok(ToolExecutionResult::success(serde_json::json!({
            "echo": inputs,
        })))
    }
    async fn send_lifecycle_hook(
        &self,
        _plugin_id: &str,
        _hook: LifecycleHook,
        _context: &HookContext,
    ) -> Result<(), PluginError> {
        Ok(())
    }
}

fn manifest_from_json(v: serde_json::Value) -> PluginManifest {
    serde_json::from_value(v).expect("valid test manifest")
}

fn base_manifest(id: &str) -> serde_json::Value {
    serde_json::json!({
        "id": id, "name": id, "version": "1.0.0",
        "plugin_type": "system", "language": "python",
        "host_type": "sidecar", "entry": "python server.py",
        "capabilities": {},
    })
}

fn bearer_headers() -> HeaderMap {
    let mut h = HeaderMap::new();
    h.insert("authorization", scaffold_admin_bearer().parse().unwrap());
    h
}

// ── POST /api/v1/actions/execute ─────────────────────────────────────

fn manifest_with_commands(id: &str, commands: serde_json::Value) -> PluginManifest {
    manifest_from_json(serde_json::json!({
        "id": id, "name": id, "version": "1.0.0",
        "plugin_type": "system", "language": "python",
        "host_type": "sidecar", "entry": "python server.py",
        "contributes": { "commands": commands },
        "capabilities": {},
    }))
}

#[tokio::test]
async fn actions_execute_blank_action_returns_400() {
    let state = AppState::new();
    for action in ["", "   "] {
        let err = actions_execute_handler(
            axum::extract::State(state.clone()),
            axum::Json(ActionsExecuteRequest {
                action: action.to_string(),
                args: serde_json::json!({}),
            }),
        )
        .await
        .unwrap_err();
        assert!(
            matches!(err, ApiError::BadRequest { .. }),
            "空 action {action:?} 应 400，实际 {err:?}"
        );
    }
}

#[tokio::test]
async fn actions_execute_unknown_command_returns_404() {
    let state = AppState::new();
    let err = actions_execute_handler(
        axum::extract::State(state),
        axum::Json(ActionsExecuteRequest {
            action: "no.such.command".to_string(),
            args: serde_json::json!({}),
        }),
    )
    .await
    .unwrap_err();
    match err {
        ApiError::NotFound { message } => {
            assert!(message.contains("no.such.command"), "{message}");
        }
        other => panic!("未声明命令应 404，实际 {other:?}"),
    }
}

#[tokio::test]
async fn actions_execute_declared_command_without_tool_acks() {
    let mut state = AppState::new();
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest_with_commands(
        "cmd_plugin",
        serde_json::json!([
            { "id": "cmd.a", "label": "A" },
            { "id": "cmd.b", "label": "B" }
        ]),
    )]));

    for action in ["cmd.a", "cmd.b"] {
        let resp = actions_execute_handler(
            axum::extract::State(state.clone()),
            axum::Json(ActionsExecuteRequest {
                action: action.to_string(),
                args: serde_json::json!({"k": 1}),
            }),
        )
        .await
        .unwrap();
        assert_eq!(resp.0["success"], serde_json::json!(true));
        assert_eq!(resp.0["result"]["acknowledged"], serde_json::json!(true));
        assert_eq!(resp.0["result"]["action"], action);
        assert_eq!(resp.0["plugin_id"], "cmd_plugin");
    }
}

#[tokio::test]
async fn actions_execute_tool_command_routes_to_invoker() {
    let mut state = AppState::new();
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest_with_commands(
        "tool_plugin",
        serde_json::json!([{ "id": "cmd.tool", "tool": "my_tool", "label": "T" }]),
    )]));
    let invoker = Arc::new(ToolCallInvoker {
        calls: std::sync::Mutex::new(Vec::new()),
        fail: false,
    });
    state.invoker = Some(invoker.clone() as Arc<dyn PluginInvoker>);

    let resp = actions_execute_handler(
        axum::extract::State(state),
        axum::Json(ActionsExecuteRequest {
            action: "cmd.tool".to_string(),
            args: serde_json::json!({"n": 7}),
        }),
    )
    .await
    .unwrap();

    assert_eq!(invoker.calls.lock().unwrap().len(), 1, "恰好一次工具调用");
    let (pid, tool, args) = invoker.calls.lock().unwrap()[0].clone();
    assert_eq!(pid, "tool_plugin");
    assert_eq!(tool, "my_tool");
    assert_eq!(args, serde_json::json!({"n": 7}));
    assert_eq!(resp.0["success"], serde_json::json!(true));
    assert_eq!(resp.0["result"]["echo"]["n"], serde_json::json!(7));
    assert_eq!(resp.0["plugin_id"], "tool_plugin");
}

#[tokio::test]
async fn actions_execute_tool_invoker_error_is_explicit_failure() {
    let mut state = AppState::new();
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest_with_commands(
        "tool_plugin",
        serde_json::json!([{ "id": "cmd.tool", "tool": "my_tool" }]),
    )]));
    let invoker = Arc::new(ToolCallInvoker {
        calls: std::sync::Mutex::new(Vec::new()),
        fail: true,
    });
    state.invoker = Some(invoker as Arc<dyn PluginInvoker>);

    let resp = actions_execute_handler(
        axum::extract::State(state),
        axum::Json(ActionsExecuteRequest {
            action: "cmd.tool".to_string(),
            args: serde_json::json!({}),
        }),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["success"], serde_json::json!(false));
    assert!(
        resp.0["error"]
            .as_str()
            .unwrap()
            .contains("sidecar 执行失败"),
        "invoker 错误必须显式透出: {}",
        resp.0["error"]
    );
}

#[tokio::test]
async fn actions_execute_tool_without_invoker_reports_unavailable() {
    let mut state = AppState::new();
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest_with_commands(
        "tool_plugin",
        serde_json::json!([{ "id": "cmd.tool", "tool": "my_tool" }]),
    )]));
    // invoker = None

    let resp = actions_execute_handler(
        axum::extract::State(state),
        axum::Json(ActionsExecuteRequest {
            action: "cmd.tool".to_string(),
            args: serde_json::json!({}),
        }),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["success"], serde_json::json!(false));
    assert!(
        resp.0["error"]
            .as_str()
            .unwrap()
            .contains("工具执行器不可用"),
        "执行器缺席必须明说，不得假成功: {}",
        resp.0["error"]
    );
    assert_eq!(resp.0["plugin_id"], "tool_plugin");
}

// ── GET /api/v1/plugins（状态清单）────────────────────────────────────

#[tokio::test]
async fn plugins_status_projects_manifest_fields() {
    let mut state = AppState::new();
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
        manifest_from_json(serde_json::json!({
            "id": "p_sys", "name": "系统插件", "version": "2.0.0",
            "description": "desc-sys",
            "plugin_type": "system", "language": "python",
            "host_type": "in_process", "entry": "x",
            "activation": "eager",
            "capabilities": {},
        })),
        manifest_from_json(serde_json::json!({
            "id": "p_tool", "name": "p_tool", "version": "1.0.0",
            "plugin_type": "tool", "language": "python",
            "host_type": "sidecar", "entry": "python server.py",
            "activation": "manual",
            "contributes": {"commands": []},
            "http_endpoints": [
                {"route_id": "r1", "method": "GET", "path": "/ext/p_tool/x",
                 "handler_capability": "http.handle"}
            ],
            "config_files": [
                {"id": "c1", "label": "可见", "path": "config/mine/a.yaml"},
                {"id": "c2", "label": "注入专用", "path": "config/mine/b.yaml",
                 "settings": false}
            ],
            "capabilities": {},
        })),
        manifest_from_json(serde_json::json!({
            "id": "p_pipe", "name": "p_pipe", "version": "1.0.0",
            "plugin_type": "pipeline", "language": "python",
            "host_type": "sidecar", "entry": "x",
            "capabilities": {},
        })),
    ]));
    state.enabled_plugin_ids =
        Arc::new(tokio::sync::RwLock::new(std::collections::HashSet::from([
            "p_sys".to_string(),
        ])));

    let resp = plugins_status_handler(axum::extract::State(state)).await;
    let items = resp.0.as_array().unwrap();
    assert_eq!(items.len(), 3);
    let by_id = |i: &str| items.iter().find(|m| m["plugin_id"] == i).unwrap();

    let sys = by_id("p_sys");
    assert_eq!(sys["config_type"], "system");
    assert_eq!(sys["host_type"], "in_process");
    assert_eq!(sys["activation"], "eager");
    assert_eq!(sys["enabled"], true);
    assert_eq!(sys["status"], "active");
    assert_eq!(sys["name"], "系统插件");
    assert_eq!(sys["has_contributes"], false);
    assert_eq!(sys["has_http_endpoints"], false);

    let tool = by_id("p_tool");
    assert_eq!(tool["config_type"], "tool");
    assert_eq!(tool["host_type"], "sidecar");
    assert_eq!(tool["activation"], "manual");
    assert_eq!(tool["enabled"], false);
    assert_eq!(tool["status"], "disabled");
    assert_eq!(tool["has_contributes"], true);
    assert_eq!(tool["has_http_endpoints"], true);
    let cfgs = tool["config_files"].as_array().unwrap();
    assert_eq!(cfgs.len(), 1, "settings:false 注入专用条目不出口");
    assert_eq!(cfgs[0]["id"], "c1");

    assert_eq!(by_id("p_pipe")["config_type"], "pipeline");
}

// ── GET /api/v1/pipelines（管道插件清单）─────────────────────────────

#[tokio::test]
async fn pipelines_lists_only_pipeline_manifests_with_host() {
    let mut state = AppState::new();
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
        manifest_from_json(serde_json::json!({
            "id": "pl_1", "name": "pl_1", "version": "1.0.0",
            "plugin_type": "pipeline", "language": "python",
            "host_type": "sidecar", "entry": "x",
            "capabilities": {},
        })),
        manifest_from_json(base_manifest("sys_1")),
    ]));
    let resp = pipelines_handler(axum::extract::State(state)).await;
    let arr = &resp.0;
    assert_eq!(arr.len(), 1, "只出口 Pipeline 类型清单");
    assert_eq!(arr[0]["id"], "pl_1");
    assert!(
        arr[0].get("role").is_none(),
        "PipelineRole 死字段已删（ADR 2026-10-13），清单不再出口 role"
    );
    assert_eq!(arr[0]["host_type"], "sidecar");
}

// ── GET /api/v1/pipelines/runs ───────────────────────────────────────

async fn seed_run_with_slot(
    sqlite: &Arc<agentos_engine::SqliteStore>,
    run_id: &str,
    pipeline_id: &str,
    thread_id: &str,
) {
    use agentos_core::traits::StorageBackend;
    let dyn_store: Arc<dyn StorageBackend> = sqlite.clone();
    dyn_store
        .record_run_start(
            pipeline_id,
            "default",
            run_id,
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .await
        .unwrap();
    sqlite
        .apply_messages_ops_to_table(
            pipeline_id,
            "default",
            &[serde_json::json!({
                "op": "set", "seq": 0,
                "msg": {"role": "user", "content": "hi"},
                "_run_id": run_id,
            })],
        )
        .unwrap();
    dyn_store
        .link_pipeline_session(pipeline_id, thread_id, "default")
        .await
        .unwrap();
}

#[tokio::test]
async fn pipelines_runs_without_db_returns_404() {
    let state = AppState::new();
    let err = pipelines_runs_handler(
        axum::extract::State(state),
        axum::extract::Query(std::collections::HashMap::new()),
        HeaderMap::new(),
    )
    .await
    .unwrap_err();
    assert!(
        matches!(err, ApiError::NotFound { .. }),
        "db 缺席应 404: {err:?}"
    );
}

#[tokio::test]
async fn pipelines_runs_lists_joined_rows_with_status_and_limit_filters() {
    use agentos_core::traits::StorageBackend;
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    seed_run_with_slot(&sqlite, "run_a", "pipe_a", "thread_a").await;
    seed_run_with_slot(&sqlite, "run_b", "pipe_b", "thread_b").await;
    let dyn_store: Arc<dyn StorageBackend> = sqlite.clone();
    dyn_store
        .upsert_state_fields(
            "pipe_b",
            "default",
            &serde_json::json!({ "run_status": "completed" })
                .as_object()
                .unwrap()
                .clone(),
        )
        .await
        .unwrap();

    let mut state = AppState::new();
    state.db = Some(sqlite);

    // 无过滤：两个 run 都出口，行带 pipeline/thread 坐标
    let resp = pipelines_runs_handler(
        axum::extract::State(state.clone()),
        axum::extract::Query(std::collections::HashMap::new()),
        bearer_headers(),
    )
    .await
    .unwrap();
    let items = resp.0["items"].as_array().unwrap();
    assert_eq!(items.len(), 2);
    let ids: Vec<&str> = items
        .iter()
        .map(|r| r["run_id"].as_str().unwrap())
        .collect();
    assert!(ids.contains(&"run_a") && ids.contains(&"run_b"));
    let row_a = items.iter().find(|r| r["run_id"] == "run_a").unwrap();
    assert_eq!(row_a["pipeline_id"], "pipe_a");
    assert_eq!(row_a["thread_id"], "thread_a");
    assert_eq!(row_a["status"], "running");

    // status=completed 只剩 run_b
    let resp = pipelines_runs_handler(
        axum::extract::State(state.clone()),
        axum::extract::Query(std::collections::HashMap::from([(
            "status".to_string(),
            "completed".to_string(),
        )])),
        bearer_headers(),
    )
    .await
    .unwrap();
    let items = resp.0["items"].as_array().unwrap();
    assert_eq!(items.len(), 1);
    assert_eq!(items[0]["run_id"], "run_b");

    // limit=1 截断；非法 limit 回退默认（不报错）
    for (limit_param, expect_len) in [("1", 1), ("not-a-number", 2)] {
        let resp = pipelines_runs_handler(
            axum::extract::State(state.clone()),
            axum::extract::Query(std::collections::HashMap::from([(
                "limit".to_string(),
                limit_param.to_string(),
            )])),
            bearer_headers(),
        )
        .await
        .unwrap();
        assert_eq!(
            resp.0["items"].as_array().unwrap().len(),
            expect_len,
            "limit={limit_param}"
        );
    }
}

// ── GET /api/v1/pipelines/state ──────────────────────────────────────

#[tokio::test]
async fn pipelines_state_merges_memory_db_enrichment_and_cold_fallback() {
    use agentos_core::traits::StorageBackend;
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let dyn_store: Arc<dyn StorageBackend> = sqlite.clone();
    let reg = agentos_session::pipeline_state_registry::global_registry();

    let mut state = AppState::new();
    state.db = Some(sqlite.clone());
    state.store = Some(dyn_store.clone());
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest_from_json(
        serde_json::json!({
            "id": "p_exp", "name": "p_exp", "version": "1.0.0",
            "plugin_type": "system", "language": "python",
            "host_type": "sidecar", "entry": "x",
            "export_fields": ["task.status", "llm_model"],
            "capabilities": {},
        }),
    )]));

    // 全局 registry 是进程级共享（其余测试可能并发登记 default 租户条目），
    // 这里不断言全局空态，只按本测试唯一 pipeline_id 断言行级行为。

    // ① 内存热行（default 租户）+ ② 表行补齐声明键 llm_model
    let pid_mem = "pipe_state_mem_cov";
    reg.get_or_init(
        "default",
        pid_mem,
        "thread_mem",
        "agentos",
        serde_json::json!({
            "pipeline_id": pid_mem,
            "current_phase": "final",
            "run_status": "running",
            "secret_field": "must-not-leak",
        }),
    );
    dyn_store
        .upsert_state_field(pid_mem, "default", "llm_model", &serde_json::json!("k2"))
        .await
        .unwrap();

    // ③ 跨租户内存行不出口
    reg.get_or_init(
        "other_tenant",
        "pipe_state_other_cov",
        "t",
        "a",
        serde_json::json!({"pipeline_id": "pipe_state_other_cov"}),
    );

    // ④ DB 冷兜底行：checkpoint task.status=pending 被表行 completed 覆盖
    let pid_cold = "pipe_state_cold_cov";
    let ckpt = serde_json::json!({"task.status": "pending", "pipeline_id": pid_cold});
    dyn_store
        .save_checkpoint(pid_cold, "default", 3, &ckpt)
        .await
        .unwrap();
    dyn_store
        .upsert_state_field(
            pid_cold,
            "default",
            "task.status",
            &serde_json::json!("completed"),
        )
        .await
        .unwrap();
    // 冷行需要 state 运行簿记键可见（runs 表退役，ADR 2026-09-18）
    sqlite
        .record_run_start(
            pid_cold,
            "default",
            "run_cold_cov",
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    sqlite
        .apply_messages_ops_to_table(
            pid_cold,
            "default",
            &[serde_json::json!({
                "op": "set", "seq": 0,
                "msg": {"role": "user", "content": "hi"},
                "_run_id": "run_cold_cov",
            })],
        )
        .unwrap();
    dyn_store
        .link_pipeline_session(pid_cold, "thread_cold", "default")
        .await
        .unwrap();

    // 租户解析按真值走（K8 修订 ADR 2026-10-10）：播种 admin 并用绑定
    // 同源哈希的 token（旧无播种 bearer 靠解析失败降级 default 的路径已收口）。
    let mut auth_hdrs = axum::http::HeaderMap::new();
    auth_hdrs.insert(
        "authorization",
        seed_admin_token(&sqlite).await.parse().unwrap(),
    );
    let resp = pipelines_state_handler(axum::extract::State(state), auth_hdrs)
        .await
        .unwrap();
    let items = resp.0["items"].as_array().unwrap();
    let by_pid = |p: &str| {
        items
            .iter()
            .find(|i| i["pipeline_id"] == p)
            .unwrap_or_else(|| panic!("缺 {p} 行: {}", resp.0))
    };

    let mem = by_pid(pid_mem);
    assert_eq!(mem["source"], "memory");
    assert_eq!(mem["thread_id"], "thread_mem");
    assert_eq!(mem["agent_id"], "agentos");
    assert_eq!(mem["state"]["run_status"], "running", "基线键出口");
    assert_eq!(
        mem["state"]["llm_model"], "k2",
        "内存缺失的声明键由表行补齐"
    );
    assert!(mem["state"].get("secret_field").is_none(), "未声明键不出口");

    let cold = by_pid(pid_cold);
    assert_eq!(cold["source"], "checkpoint");
    assert_eq!(cold["thread_id"], "thread_cold");
    assert_eq!(
        cold["state"]["task.status"], "completed",
        "表行最新值覆盖 checkpoint 过期值"
    );

    assert!(
        items
            .iter()
            .all(|i| i["pipeline_id"] != "pipe_state_other_cov"),
        "跨租户内存行不得出口"
    );

    reg.remove("default", pid_mem);
    reg.remove("other_tenant", "pipe_state_other_cov");
    reg.remove("default", pid_cold);
}

// ── /api/v1/config/pipelines/{name}（P7 管道配置读写）────────────────

#[tokio::test]
async fn pipeline_config_get_guards_and_etag() {
    let tmp = tempfile::tempdir().unwrap();
    let _user_space_guard = crate::test_env::pin_user_root(tmp.path());
    let cfg_dir = tmp.path().join("config").join("pipelines");
    std::fs::create_dir_all(&cfg_dir).unwrap();
    std::fs::write(cfg_dir.join("demo.yaml"), "name: demo\nmax_rounds: 5\n").unwrap();
    std::fs::write(cfg_dir.join("broken.yaml"), "[:not yaml").unwrap();

    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());

    // 非法 name（穿越/含点）→ 400
    for bad in ["../../evil", "bad.name"] {
        let err = get_pipeline_config_with_etag(
            axum::extract::State(state.clone()),
            axum::extract::Path(bad.to_string()),
        )
        .await
        .unwrap_err();
        assert!(
            matches!(err, ApiError::BadRequest { .. }),
            "非法 name {bad} 应 400，实际 {err:?}"
        );
    }
    // project_root 缺席 → 500
    let err = get_pipeline_config_with_etag(
        axum::extract::State(AppState::new()),
        axum::extract::Path("demo".to_string()),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, ApiError::Internal { .. }));
    // 未知管道 → 404
    let err = get_pipeline_config_with_etag(
        axum::extract::State(state.clone()),
        axum::extract::Path("nope".to_string()),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, ApiError::NotFound { .. }));
    // 损坏 yaml → 500
    let err = get_pipeline_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path("broken".to_string()),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, ApiError::Internal { .. }));

    // 正常读：内容解析 + ETag 响应头
    let resp = get_pipeline_config_with_etag(
        axum::extract::State(state),
        axum::extract::Path("demo".to_string()),
    )
    .await
    .unwrap();
    assert!(resp.headers().get("etag").is_some(), "ETag 响应头");
    let body = axum::body::to_bytes(resp.into_body(), 65536).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(v["name"], "demo");
    assert_eq!(v["data"]["max_rounds"], 5);
    assert!(!v["etag"].as_str().unwrap().is_empty());
}

fn valid_pipeline_config() -> serde_json::Value {
    serde_json::json!({
        "name": "demo",
        "loop_bodies": [
            {"id": "main", "steps": [{"id": "llm", "steps": ["mock_llm_core"]}]}
        ],
    })
}

#[tokio::test]
async fn pipeline_config_put_guards() {
    let tmp = tempfile::tempdir().unwrap();
    let _user_space_guard = crate::test_env::pin_user_root(tmp.path());
    let cfg_dir = tmp.path().join("config").join("pipelines");
    std::fs::create_dir_all(&cfg_dir).unwrap();
    std::fs::write(cfg_dir.join("demo.yaml"), "name: demo\n").unwrap();

    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());
    let req = |data: serde_json::Value, if_match: Option<&str>| PipelineConfigUpdateRequest {
        data,
        if_match: if_match.map(str::to_string),
    };

    // 非法 name → 400
    let err = put_pipeline_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path("../../evil".to_string()),
        axum::Json(req(valid_pipeline_config(), None)),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, ApiError::BadRequest { .. }));

    // data 非映射 → 400，且消息带实际类型名
    for (data, ty) in [
        (serde_json::json!([1, 2]), "array"),
        (serde_json::json!("scalar"), "string"),
    ] {
        let err = put_pipeline_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path("demo".to_string()),
            axum::Json(req(data, None)),
        )
        .await
        .unwrap_err();
        match err {
            ApiError::BadRequest { message } => assert!(message.contains(ty), "{message}"),
            other => panic!("非映射 data 应 400，实际 {other:?}"),
        }
    }

    // G10 DSL 校验失败（旧形态键 routes）→ 400
    let legacy = serde_json::json!({
        "name": "demo",
        "loop_bodies": [{"id": "m", "routes": []}],
    });
    let err = put_pipeline_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path("demo".to_string()),
        axum::Json(req(legacy, None)),
    )
    .await
    .unwrap_err();
    match err {
        ApiError::BadRequest { message } => {
            assert!(message.contains("validation failed"), "{message}")
        }
        other => panic!("DSL 违例应 400，实际 {other:?}"),
    }

    // 文件不存在 → 404（PUT 不再隐式创建）
    let err = put_pipeline_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path("ghost".to_string()),
        axum::Json(req(valid_pipeline_config(), None)),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, ApiError::NotFound { .. }));

    // If-Match 缺失/不匹配 → 409
    for if_match in [None, Some("deadbeef")] {
        let err = put_pipeline_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path("demo".to_string()),
            axum::Json(req(valid_pipeline_config(), if_match)),
        )
        .await
        .unwrap_err();
        assert!(
            matches!(err, ApiError::Conflict { .. }),
            "If-Match {if_match:?} 应 409，实际 {err:?}"
        );
    }
}

#[tokio::test]
async fn pipeline_config_put_roundtrip_updates_file() {
    let tmp = tempfile::tempdir().unwrap();
    let _user_space_guard = crate::test_env::pin_user_root(tmp.path());
    let cfg_dir = tmp.path().join("config").join("pipelines");
    std::fs::create_dir_all(&cfg_dir).unwrap();
    std::fs::write(cfg_dir.join("demo.yaml"), "name: demo\nmax_rounds: 5\n").unwrap();

    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());

    let current = std::fs::read_to_string(cfg_dir.join("demo.yaml")).unwrap();
    let current_etag = crate::config_service::compute_etag(current.as_bytes());
    let mut new_config = valid_pipeline_config();
    new_config["max_rounds"] = serde_json::json!(9);

    let resp = put_pipeline_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path("demo".to_string()),
        axum::Json(PipelineConfigUpdateRequest {
            data: new_config,
            if_match: Some(current_etag.clone()),
        }),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["name"], "demo");
    let new_etag = resp.0["etag"].as_str().unwrap().to_string();
    assert!(!new_etag.is_empty(), "新 ETag 非空");
    assert_ne!(new_etag, current_etag, "写盘后 ETag 必须更新");

    // GET 回读新内容
    let resp = get_pipeline_config_handler(
        axum::extract::State(state),
        axum::extract::Path("demo".to_string()),
    )
    .await
    .unwrap();
    assert_eq!(resp.data["max_rounds"], 9);
    assert_eq!(resp.etag, new_etag, "GET ETag 与 PUT 返回一致");
}

// ── /api/v1/plugins/{id}/config/{file_id}（插件配置读写）─────────────

/// env target 条目清单（GAP-4：key 写 .env）。
fn env_config_manifest() -> PluginManifest {
    manifest_from_json(serde_json::json!({
        "id": "env_plugin", "name": "env_plugin", "version": "1.0.0",
        "plugin_type": "tool", "language": "python",
        "host_type": "sidecar", "entry": "python server.py",
        "config_files": [{
            "id": "env_main", "label": "Env Keys", "target": "env",
            "fields": [
                {"name": "FOO_KEY", "label": "Foo", "type": "secret"},
                {"name": "BAR_SECRET", "label": "Bar", "type": "secret"}
            ]
        }],
        "capabilities": {},
    }))
}

#[tokio::test]
async fn plugin_config_get_guards_and_env_masked_view() {
    let tmp = tempfile::tempdir().unwrap();
    let _user_space_guard = crate::test_env::pin_user_root(tmp.path());
    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![env_config_manifest()]));

    // project_root 缺席 → 500
    let err = get_plugin_config_with_etag(
        axum::extract::State(AppState::new()),
        axum::extract::Path(("env_plugin".to_string(), "env_main".to_string())),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, ApiError::Internal { .. }));

    // 未知插件 / 未知 file_id → 404
    for (pid, fid) in [("ghost", "env_main"), ("env_plugin", "nope")] {
        let err = get_plugin_config_with_etag(
            axum::extract::State(state.clone()),
            axum::extract::Path((pid.to_string(), fid.to_string())),
        )
        .await
        .unwrap_err();
        assert!(
            matches!(err, ApiError::NotFound { .. }),
            "({pid},{fid}) 应 404，实际 {err:?}"
        );
    }

    // .env 未写：两字段均未设置 → "" 掩码视图 + ETag
    let resp = get_plugin_config_with_etag(
        axum::extract::State(state.clone()),
        axum::extract::Path(("env_plugin".to_string(), "env_main".to_string())),
    )
    .await
    .unwrap();
    let body = axum::body::to_bytes(resp.into_body(), 65536).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(v["path"], ".env");
    assert_eq!(v["data"]["FOO_KEY"], "");
    assert_eq!(v["data"]["BAR_SECRET"], "");

    // 写入 FOO_KEY 后：已设置字段 → "***" 哨兵（has_key 语义）
    agentos_mcp::env_file::write_env_updates(
        &agentos_mcp::env_file::env_path_for_root(tmp.path()),
        &[("FOO_KEY".to_string(), "hello".to_string())],
    )
    .unwrap();
    let resp = get_plugin_config_handler(
        axum::extract::State(state),
        axum::extract::Path(("env_plugin".to_string(), "env_main".to_string())),
    )
    .await
    .unwrap();
    assert_eq!(resp.data["FOO_KEY"], "***", "已设置字段只出口哨兵");
    assert_eq!(resp.data["BAR_SECRET"], "", "未设置字段出口空串");
}

#[tokio::test]
async fn plugin_config_put_env_target_guards_and_write() {
    let tmp = tempfile::tempdir().unwrap();
    let _user_space_guard = crate::test_env::pin_user_root(tmp.path());
    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![env_config_manifest()]));

    // 当前掩码视图的 ETag（乐观锁基准）
    let current = get_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("env_plugin".to_string(), "env_main".to_string())),
    )
    .await
    .unwrap();
    let current_etag = current.etag.clone();

    let req = |data: serde_json::Value, if_match: Option<String>| PluginConfigUpdateRequest {
        data,
        if_match,
    };

    // 未声明字段 → 400
    let err = put_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("env_plugin".to_string(), "env_main".to_string())),
        axum::Json(req(
            serde_json::json!({"UNDECLARED": "x"}),
            Some(current_etag.clone()),
        )),
    )
    .await
    .unwrap_err();
    match err {
        ApiError::BadRequest { message } => assert!(message.contains("UNDECLARED")),
        other => panic!("未声明 env 字段应 400，实际 {other:?}"),
    }

    // If-Match 缺失 / 不匹配 → 409
    for if_match in [None, Some("stale".to_string())] {
        let err = put_plugin_config_handler(
            axum::extract::State(state.clone()),
            axum::extract::Path(("env_plugin".to_string(), "env_main".to_string())),
            axum::Json(req(serde_json::json!({}), if_match)),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ApiError::Conflict { .. }), "实际 {err:?}");
    }

    // 正确 If-Match：*** 哨兵保留现值，新值写入 .env
    let resp = put_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("env_plugin".to_string(), "env_main".to_string())),
        axum::Json(req(
            serde_json::json!({"FOO_KEY": "***", "BAR_SECRET": "new-bar"}),
            Some(current_etag),
        )),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["ok"], serde_json::json!(true));
    assert!(!resp.0["etag"].as_str().unwrap().is_empty());

    let env_path = agentos_mcp::env_file::env_path_for_root(tmp.path());
    let raw = std::fs::read_to_string(&env_path).unwrap();
    let parsed = agentos_mcp::env_file::parse_env_text_for_read(&raw);
    assert_eq!(
        parsed.get("BAR_SECRET").map(String::as_str),
        Some("new-bar")
    );
    assert!(
        !parsed.contains_key("FOO_KEY"),
        "哨兵字段不得写入（保留现值语义）: {raw}"
    );

    // GET 掩码视图反映新状态
    let after = get_plugin_config_handler(
        axum::extract::State(state),
        axum::extract::Path(("env_plugin".to_string(), "env_main".to_string())),
    )
    .await
    .unwrap();
    assert_eq!(after.data["FOO_KEY"], "");
    assert_eq!(after.data["BAR_SECRET"], "***");
}

/// 内联形态（path 空）配置：真值 = fields.default，PUT 原地写回 plugin.json。
fn inline_plugin_json() -> serde_json::Value {
    serde_json::json!({
        "id": "inline_plugin", "name": "inline_plugin", "version": "1.0.0",
        "plugin_type": "tool", "language": "python",
        "host_type": "sidecar", "entry": "python server.py",
        "capabilities": {},
        "config_files": [{
            "id": "limits", "label": "Limits", "path": "",
            "fields": [
                {"name": "budgets.l1", "label": "L1", "type": "number",
                 "default": 0.3},
                {"name": "compression.model", "label": "M", "type": "string",
                 "default": "k2"}
            ]
        }],
    })
}

fn inline_manifest() -> PluginManifest {
    manifest_from_json(inline_plugin_json())
}

#[tokio::test]
async fn plugin_config_inline_put_roundtrip_writes_manifest_and_syncs_memory() {
    let plugin_dir = tempfile::tempdir().unwrap();
    let _user_space_guard = crate::test_env::pin_user_root(plugin_dir.path());
    // 插件目录映射走 loader 活真源（B1）：pkg/ 布局 + discover 填充路径表
    let pkg_dir = plugin_dir.path().join("pkg");
    std::fs::create_dir_all(&pkg_dir).unwrap();
    std::fs::write(
        pkg_dir.join("plugin.json"),
        serde_json::to_string_pretty(&inline_plugin_json()).unwrap(),
    )
    .unwrap();
    let mut state = AppState::new();
    // 内联形态入口同样强制 project_root 门（校验先行），真值读写走 loader 路径表
    state.project_root = Some(plugin_dir.path().to_path_buf());
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![inline_manifest()]));
    state.plugin_loader = Some(crate::test_env::discovered_plugin_loader(plugin_dir.path()).await);

    // GET 内联视图 = fields.default 组装
    let current = get_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("inline_plugin".to_string(), "limits".to_string())),
    )
    .await
    .unwrap();
    assert_eq!(current.data["budgets"]["l1"], serde_json::json!(0.3));
    assert_eq!(current.data["compression"]["model"], "k2");
    assert_eq!(current.path, "", "内联形态 path 为空");
    let etag_v1 = current.etag.clone();

    // If-Match 缺失 → 409
    let err = put_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("inline_plugin".to_string(), "limits".to_string())),
        axum::Json(PluginConfigUpdateRequest {
            data: serde_json::json!({"budgets": {"l1": 0.5}}),
            if_match: None,
        }),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, ApiError::Conflict { .. }));

    // data 非对象 → 400
    let err = put_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("inline_plugin".to_string(), "limits".to_string())),
        axum::Json(PluginConfigUpdateRequest {
            data: serde_json::json!([1]),
            if_match: Some(etag_v1.clone()),
        }),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, ApiError::BadRequest { .. }));

    // 未声明叶路径 fail-closed → 400 点名完整路径
    let err = put_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("inline_plugin".to_string(), "limits".to_string())),
        axum::Json(PluginConfigUpdateRequest {
            data: serde_json::json!({"budgets": {"nonsense": 1}}),
            if_match: Some(etag_v1.clone()),
        }),
    )
    .await
    .unwrap_err();
    match err {
        ApiError::BadRequest { message } => {
            assert!(message.contains("budgets.nonsense"), "{message}")
        }
        other => panic!("未声明叶路径应 400，实际 {other:?}"),
    }

    // 正确保存：0.3→0.5 + null 清除 compression.model default
    let resp = put_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("inline_plugin".to_string(), "limits".to_string())),
        axum::Json(PluginConfigUpdateRequest {
            data: serde_json::json!(
                {"budgets": {"l1": 0.5}, "compression.model": null}
            ),
            if_match: Some(etag_v1),
        }),
    )
    .await
    .unwrap();
    let etag_v2 = resp.0["etag"].as_str().unwrap().to_string();
    assert!(!etag_v2.is_empty(), "新 ETag 非空");
    assert_ne!(etag_v2, current.etag, "保存后 ETag 必须变化");

    // 磁盘 plugin.json 落盘核对（pkg/ 布局：loader 活真源发现面）
    let disk: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(pkg_dir.join("plugin.json")).unwrap())
            .unwrap();
    let fields = disk["config_files"][0]["fields"].as_array().unwrap();
    let l1 = fields.iter().find(|f| f["name"] == "budgets.l1").unwrap();
    assert_eq!(l1["default"], serde_json::json!(0.5), "新默认值落盘");
    let model = fields
        .iter()
        .find(|f| f["name"] == "compression.model")
        .unwrap();
    assert!(
        model.get("default").is_none(),
        "null 保存 = 清除 default: {model}"
    );

    // 内存 manifest 同步：GET 即时反映新值
    let after = get_plugin_config_handler(
        axum::extract::State(state),
        axum::extract::Path(("inline_plugin".to_string(), "limits".to_string())),
    )
    .await
    .unwrap();
    assert_eq!(after.data["budgets"]["l1"], serde_json::json!(0.5));
    assert!(after.data.get("compression").is_none(), "清除后不再出口");
    assert_eq!(after.etag, etag_v2);
}

#[tokio::test]
async fn plugin_config_inline_put_without_plugin_dir_returns_500() {
    let plugin_dir = tempfile::tempdir().unwrap();
    let _user_space_guard = crate::test_env::pin_user_root(plugin_dir.path());
    let mut state = AppState::new();
    state.project_root = Some(plugin_dir.path().to_path_buf());
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![inline_manifest()]));
    // plugin_loader 未装配 → 目录映射缺失

    let current = get_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("inline_plugin".to_string(), "limits".to_string())),
    )
    .await
    .unwrap();
    let err = put_plugin_config_handler(
        axum::extract::State(state),
        axum::extract::Path(("inline_plugin".to_string(), "limits".to_string())),
        axum::Json(PluginConfigUpdateRequest {
            data: serde_json::json!({"budgets": {"l1": 0.5}}),
            if_match: Some(current.etag.clone()),
        }),
    )
    .await
    .unwrap_err();
    assert!(
        matches!(err, ApiError::Internal { .. }),
        "目录映射缺失应 500，实际 {err:?}"
    );
}

#[tokio::test]
async fn plugin_config_referenced_file_masking_and_put_sentinels() {
    let tmp = tempfile::tempdir().unwrap();
    let _user_space_guard = crate::test_env::pin_user_root(tmp.path());
    let cfg_dir = tmp.path().join("config").join("mymodels");
    std::fs::create_dir_all(&cfg_dir).unwrap();
    std::fs::write(
        cfg_dir.join("llm.yaml"),
        "api_key: real_secret\nmodel: gpt-x\nplaceholder: ${MY_VAR}\n",
    )
    .unwrap();
    std::fs::write(cfg_dir.join("broken.yaml"), "[:bad").unwrap();

    let manifest = manifest_from_json(serde_json::json!({
        "id": "ref_plugin", "name": "ref_plugin", "version": "1.0.0",
        "plugin_type": "tool", "language": "python",
        "host_type": "sidecar", "entry": "python server.py",
        "config_files": [
            {"id": "llm", "label": "LLM", "path": "config/mymodels/llm.yaml",
             "fields": []},
            {"id": "absent", "label": "Absent", "path": "config/mymodels/none.yaml",
             "fields": []},
            {"id": "broken", "label": "Broken", "path": "config/mymodels/broken.yaml",
             "fields": []}
        ],
        "capabilities": {},
    }));
    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest]));

    // GET 掩码：真实 secret → ****，${VAR} 占位符原样
    let current = get_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("ref_plugin".to_string(), "llm".to_string())),
    )
    .await
    .unwrap();
    assert_eq!(current.data["api_key"], "****");
    assert_eq!(current.data["placeholder"], "${MY_VAR}");
    assert_eq!(current.data["model"], "gpt-x");
    assert_eq!(current.path, "config/mymodels/llm.yaml");
    let etag_v1 = current.etag.clone();

    // 引用文件缺失 → 空配置视图（PUT 保存将创建文件）
    let absent = get_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("ref_plugin".to_string(), "absent".to_string())),
    )
    .await
    .unwrap();
    assert!(absent.data.as_object().unwrap().is_empty());

    // 损坏 yaml → 500
    let err = get_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("ref_plugin".to_string(), "broken".to_string())),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, ApiError::Internal { .. }));

    // PUT：If-Match 缺失 → 409
    let err = put_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("ref_plugin".to_string(), "llm".to_string())),
        axum::Json(PluginConfigUpdateRequest {
            data: serde_json::json!({"model": "gpt-5"}),
            if_match: None,
        }),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, ApiError::Conflict { .. }));

    // PUT：掩码哨兵保留磁盘原 secret，普通字段更新
    let resp = put_plugin_config_handler(
        axum::extract::State(state.clone()),
        axum::extract::Path(("ref_plugin".to_string(), "llm".to_string())),
        axum::Json(PluginConfigUpdateRequest {
            data: serde_json::json!({"model": "gpt-5", "api_key": "****"}),
            if_match: Some(etag_v1.clone()),
        }),
    )
    .await
    .unwrap();
    let etag_v2 = resp.0["etag"].as_str().unwrap().to_string();
    assert_ne!(etag_v2, etag_v1);

    let disk = std::fs::read_to_string(cfg_dir.join("llm.yaml")).unwrap();
    assert!(
        disk.contains("real_secret"),
        "哨兵字段必须保留磁盘原值: {disk}"
    );
    assert!(disk.contains("gpt-5"), "普通字段必须更新: {disk}");

    // GET 新 ETag 与更新视图一致
    let after = get_plugin_config_handler(
        axum::extract::State(state),
        axum::extract::Path(("ref_plugin".to_string(), "llm".to_string())),
    )
    .await
    .unwrap();
    assert_eq!(after.data["model"], "gpt-5");
    assert_eq!(after.etag, etag_v2);
}

// ── POST /api/v1/plugins/validate-all（registry ↔ 磁盘一致性检出）────

/// 在目录落一份磁盘 plugin.json。
fn write_disk_manifest(dir: &std::path::Path, v: &serde_json::Value) {
    std::fs::write(
        dir.join("plugin.json"),
        serde_json::to_string_pretty(v).unwrap(),
    )
    .unwrap();
}

#[tokio::test]
async fn validate_all_detects_registry_disk_consistency() {
    let plugin_dir = tempfile::tempdir().unwrap();
    let registry_manifest = serde_json::json!({
        "id": "p_disk", "name": "p_disk", "version": "1.0.0",
        "plugin_type": "tool", "language": "python",
        "host_type": "sidecar", "entry": "python server.py",
        "capabilities": {"tools": [{"name": "t1", "description": "t1"}]},
    });

    // 无目录映射 → disk_manifest_unreadable（不可读不得静默当绿）
    let mut state = AppState::new();
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest_from_json(
        registry_manifest.clone(),
    )]));
    state.invoker = Some(Arc::new(RecordingInvoker {
        seen: std::sync::Mutex::new(Vec::new()),
        seen_states: std::sync::Mutex::new(Vec::new()),
        hooks: std::sync::Mutex::new(Vec::new()),
        list_tools: std::collections::HashMap::from([(
            "p_disk".to_string(),
            serde_json::json!({"tools": [{"name": "t1", "description": "t1"}]}),
        )]),
    }) as Arc<dyn PluginInvoker>);
    let resp = validate_all_plugins_handler(axum::extract::State(state.clone())).await;
    let reports = resp.0["consistency_reports"].as_array().unwrap();
    assert_eq!(reports.len(), 1);
    assert_eq!(reports[0]["status"], "disk_manifest_unreadable");
    assert_eq!(resp.0["registry_disk_mismatches"], 0);

    // 磁盘一致 → 无 consistency 报告
    let pkg_dir = plugin_dir.path().join("pkg");
    std::fs::create_dir_all(&pkg_dir).unwrap();
    write_disk_manifest(&pkg_dir, &registry_manifest);
    state.plugin_loader = Some(crate::test_env::discovered_plugin_loader(plugin_dir.path()).await);
    let resp = validate_all_plugins_handler(axum::extract::State(state.clone())).await;
    assert!(
        resp.0["consistency_reports"].as_array().unwrap().is_empty(),
        "一致时不得有差异报告: {}",
        resp.0
    );
    assert_eq!(resp.0["registry_disk_mismatches"], 0);
    assert_eq!(resp.0["clean"], 1);

    // 磁盘漂移（磁盘多声明一个工具 → 注册表侧 missing_tool）→ mismatch + diffs
    let mut drifted = registry_manifest.clone();
    drifted["capabilities"]["tools"] = serde_json::json!([
        {"name": "t1", "description": "t1"},
        {"name": "t2_extra", "description": "磁盘新声明，注册表无"}
    ]);
    write_disk_manifest(&pkg_dir, &drifted);
    let resp = validate_all_plugins_handler(axum::extract::State(state)).await;
    assert_eq!(resp.0["registry_disk_mismatches"], 1);
    let reports = resp.0["consistency_reports"].as_array().unwrap();
    assert_eq!(reports[0]["status"], "registry_disk_mismatch");
    assert_eq!(reports[0]["plugin_id"], "p_disk");
    assert!(
        !reports[0]["diffs"].as_array().unwrap().is_empty(),
        "差异明细必须给出"
    );
}

// ── POST /api/v1/system/restart（G8 排空；测试逃生门禁退出）──────────

/// 逃生门 RAII：测试期间设 AGENTOS_DISABLE_SELF_EXIT=1，结束恢复原值。
/// 环境变量是进程级共享，两个用例标 #[serial] 串行（同 plugin_watcher_test 先例）。
struct DisableSelfExit;
impl DisableSelfExit {
    fn set() -> Self {
        std::env::set_var("AGENTOS_DISABLE_SELF_EXIT", "1");
        Self
    }
}
impl Drop for DisableSelfExit {
    fn drop(&mut self) {
        std::env::remove_var("AGENTOS_DISABLE_SELF_EXIT");
    }
}

#[tokio::test]
#[serial_test::serial]
async fn system_restart_drains_running_runs_and_reports_exit_75() {
    let _guard = DisableSelfExit::set();

    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    sqlite
        .record_run_start(
            "pipe_g8_cov",
            "default",
            "run_g8_cov",
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    let mut state = AppState::new();
    state.db = Some(sqlite.clone());

    let resp = system_restart_handler(axum::extract::State(state)).await;
    assert_eq!(resp.0["success"], serde_json::json!(true));
    assert_eq!(resp.0["exit_code"], 75);
    assert_eq!(resp.0["suspended_runs"], 1);

    // 排空副作用：running → suspended
    let run = sqlite.get_run("run_g8_cov").await.unwrap();
    assert_eq!(run.status, agentos_core::types::RunStatus::Suspended);

    // 无 db / 无在途 run → suspended_runs 0
    let resp = system_restart_handler(axum::extract::State(AppState::new())).await;
    assert_eq!(resp.0["suspended_runs"], 0);
}

#[tokio::test]
#[serial_test::serial]
async fn drain_and_exit75_counts_suspended_runs_with_db() {
    let _guard = DisableSelfExit::set();
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    sqlite
        .record_run_start(
            "pipe_drain_1",
            "default",
            "run_drain_1",
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    sqlite
        .record_run_start(
            "pipe_drain_2",
            "default",
            "run_drain_2",
            "cfg",
            "2026-10-15T00:00:00+00:00",
        )
        .unwrap();
    sqlite
        .set_run_status_projection(
            "pipe_drain_2",
            "default",
            agentos_core::types::RunStatus::Completed,
        )
        .unwrap();

    let n = crate::routes::drain_and_exit75(Some(&sqlite), None, "unit-test drain").await;
    assert_eq!(n, 1, "只排空 running，completed 不动");
}

// ── PUT /api/v1/plugins/{id}/enabled（registry 热更新双向）────────────

#[tokio::test]
async fn set_enabled_hot_reloads_registry_both_directions() {
    use agentos_core::traits::CapabilityRegistry as _;
    let tmp = tempfile::tempdir().unwrap();
    let _user_space_guard = crate::test_env::pin_user_root(tmp.path());
    let profile_dir = tmp.path().join("config").join("kernel");
    std::fs::create_dir_all(&profile_dir).unwrap();

    let manifest = manifest_from_json(serde_json::json!({
        "id": "p_toggle", "name": "p_toggle", "version": "1.0.0",
        "plugin_type": "tool", "language": "python",
        "host_type": "sidecar", "entry": "python server.py",
        "capabilities": {"tools": [{"name": "t1", "description": "t1"}]},
        "http_endpoints": [
            {"route_id": "r1", "method": "GET", "path": "/ext/p_toggle/things",
             "handler_capability": "http.handle"}
        ],
    }));
    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest]));
    state.invoker = Some(Arc::new(RecordingInvoker {
        seen: std::sync::Mutex::new(Vec::new()),
        seen_states: std::sync::Mutex::new(Vec::new()),
        hooks: std::sync::Mutex::new(Vec::new()),
        list_tools: std::collections::HashMap::from([(
            "p_toggle".to_string(),
            serde_json::json!({"tools": [{"name": "t1", "description": "t1"}]}),
        )]),
    }) as Arc<dyn PluginInvoker>);
    let registry = Arc::new(agentos_plugin_loader::CapabilityRegistryImpl::new());
    state.capability_registry = Some(registry.clone());

    // 启用：G2 复核干净 → tools/http_routes 全注册 + 内存 enabled 位翻转
    let resp = plugins_set_enabled_handler(
        axum::extract::Path("p_toggle".to_string()),
        axum::extract::State(state.clone()),
        axum::Json(EnabledBody { enabled: true }),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["success"], serde_json::json!(true));
    assert_eq!(resp.0["restart_required"], serde_json::json!(false));
    assert_eq!(resp.0["registered"]["tools"], 1);
    assert_eq!(resp.0["registered"]["http_routes"], 1);
    assert!(state.enabled_plugin_ids.read().await.contains("p_toggle"));
    assert_eq!(
        registry.list_tools().len(),
        1,
        "启用的工具必须立即出现在注册表"
    );
    assert_eq!(registry.list_http_routes().len(), 1);
    let profile_raw = std::fs::read_to_string(profile_dir.join("default_profile.yaml")).unwrap();
    assert!(profile_raw.contains("p_toggle"));
    assert!(profile_raw.contains("enabled: true"));

    // 禁用：注册即收（tools/http_routes 零残留）+ enabled 位摘除
    let resp = plugins_set_enabled_handler(
        axum::extract::Path("p_toggle".to_string()),
        axum::extract::State(state.clone()),
        axum::Json(EnabledBody { enabled: false }),
    )
    .await
    .unwrap();
    assert_eq!(resp.0["registered"], serde_json::Value::Null);
    assert!(!state.enabled_plugin_ids.read().await.contains("p_toggle"));
    assert!(registry.list_tools().is_empty(), "禁用必须结构性收回工具");
    assert!(registry.list_http_routes().is_empty());
}

#[tokio::test]
async fn set_enabled_without_project_root_returns_500() {
    let state = AppState::new();
    let err = plugins_set_enabled_handler(
        axum::extract::Path("p_x".to_string()),
        axum::extract::State(state),
        axum::Json(EnabledBody { enabled: true }),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, ApiError::Internal { .. }));
}

#[tokio::test]
async fn set_enabled_unreadable_profile_returns_500() {
    // profile 路径是目录 → 读失败（非 NotFound）→ 500，不写入
    let tmp = tempfile::tempdir().unwrap();
    let _user_space_guard = crate::test_env::pin_user_root(tmp.path());
    let profile_dir = tmp.path().join("config").join("kernel");
    std::fs::create_dir_all(profile_dir.join("default_profile.yaml")).unwrap();

    let mut state = AppState::new();
    state.project_root = Some(tmp.path().to_path_buf());
    let err = plugins_set_enabled_handler(
        axum::extract::Path("p_x".to_string()),
        axum::extract::State(state),
        axum::Json(EnabledBody { enabled: true }),
    )
    .await
    .unwrap_err();
    assert!(
        matches!(err, ApiError::Internal { .. }),
        "读失败（权限/目录）应 500，实际 {err:?}"
    );
}

// ── GET /api/v1/schema（聚合分支）────────────────────────────────────

#[tokio::test]
async fn schema_aggregates_registry_routes_and_manifest_sections() {
    let registry = Arc::new(agentos_plugin_loader::CapabilityRegistryImpl::new());
    let endpoint: agentos_core::traits::HttpEndpoint = serde_json::from_value(serde_json::json!({
        "route_id": "r1", "method": "GET", "path": "/ext/p_ext/things",
        "handler_capability": "http.handle", "auth": "user"
    }))
    .unwrap();
    // 注册并持有 guard：guard drop 即撤销注册（结构性收回语义），本测试
    // 内必须保活以让 schema 聚合读到该路由
    let (_descriptor, _route_guard) = registry
        .register_http_route_guarded("p_ext", endpoint)
        .unwrap();

    let mut state = AppState::new();
    state.capability_registry = Some(registry);
    state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
        manifest_from_json(serde_json::json!({
            "id": "p_sys_ui", "name": "p_sys_ui", "version": "1.0.0",
            "plugin_type": "system", "language": "python",
            "host_type": "sidecar", "entry": "x",
            "ui_schema": {"widgets": [{"kind": "stat"}]},
            "config_files": [
                {"id": "visible", "label": "V", "path": "config/mine/v.yaml"},
                {"id": "hidden", "label": "H", "path": "config/mine/h.yaml",
                 "settings": false}
            ],
            "capabilities": {},
        })),
        manifest_from_json(serde_json::json!({
            "id": "p_pipe_role", "name": "p_pipe_role", "version": "1.0.0",
            "plugin_type": "pipeline", "language": "python",
            "host_type": "sidecar", "entry": "x",
            "capabilities": {},
        })),
    ]));
    state.enabled_plugin_ids =
        Arc::new(tokio::sync::RwLock::new(std::collections::HashSet::from([
            "p_sys_ui".to_string(),
        ])));

    let resp = schema_handler(axum::extract::State(state.clone()), HeaderMap::new()).await;
    assert_eq!(resp.status(), axum::http::StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 262144)
        .await
        .unwrap();
    let v: serde_json::Value = serde_json::from_slice(&body).unwrap();

    // routes：注册表数据驱动，{plugin_id: [route…]} 形状
    let route_entry = &v["routes"]["p_ext"][0];
    assert_eq!(route_entry["method"], "GET");
    assert_eq!(route_entry["path"], "/ext/p_ext/things");
    assert_eq!(route_entry["auth"], "user");

    // agents/pipelines 按 manifest 类型分桶，ui_schema 随行（role 已随
    // PipelineRole 死字段删除，ADR 2026-10-13）
    assert_eq!(v["agents"].as_array().unwrap().len(), 1);
    assert_eq!(v["agents"][0]["id"], "p_sys_ui");
    assert!(v["pipelines"][0].get("role").is_none());

    // plugin_configs：settings:false 注入专用条目不出口
    let cfgs = v["plugin_configs"].as_array().unwrap();
    assert_eq!(cfgs.len(), 1);
    let visible = cfgs[0]["config_files"].as_array().unwrap();
    assert_eq!(visible.len(), 1);
    assert_eq!(visible[0]["id"], "visible");

    // plugin_contributes：enabled 的 ui_schema 插件出口；未启用不出
    let contribs = v["plugin_contributes"].as_array().unwrap();
    assert_eq!(contribs.len(), 1);
    assert_eq!(contribs[0]["plugin_id"], "p_sys_ui");

    // 未注入内核契约 → 空数组
    assert!(v["kernel_capabilities"].as_array().unwrap().is_empty());

    // If-None-Match 多候选（stale, 当前）→ 304（RFC 9110 逗号列表语义）
    let etag = crate::config_service::compute_etag(&body);
    let mut inm = HeaderMap::new();
    inm.insert(
        "if-none-match",
        format!("\"stale-1\", {etag}").parse().unwrap(),
    );
    let resp2 = schema_handler(axum::extract::State(state), inm).await;
    assert_eq!(resp2.status(), axum::http::StatusCode::NOT_MODIFIED);
}

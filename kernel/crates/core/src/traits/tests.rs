// @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @ci: rust-test
//! traits 单元测试。
use super::*;

/// 最小合法 manifest JSON 底座（tool/sidecar/python），调用方按需
/// merge 进额外键（host_group / state 等）或直接解析。
fn base_manifest_json() -> serde_json::Value {
    serde_json::json!({
        "id": "p", "name": "p", "version": "1", "plugin_type": "tool",
        "language": "python", "host_type": "sidecar", "entry": "python s.py",
        "capabilities": {}
    })
}

#[test]
fn mcp_config_request_timeout_secs_parse_roundtrip() {
    // 显式声明 → 覆盖内核默认（86400s，BUG-60 审批族统一值）
    let cfg: McpConfig = serde_json::from_value(serde_json::json!({
        "transport": "stdio",
        "request_timeout_secs": 90000,
    }))
    .expect("合法配置应可解析");
    assert_eq!(cfg.request_timeout_secs, Some(90000));

    // 缺省 → None（内核 mcp 客户端默认兜底，现为 86400s 统一值）
    let cfg2: McpConfig =
        serde_json::from_value(serde_json::json!({"transport": "stdio"})).expect("缺省可解析");
    assert_eq!(cfg2.request_timeout_secs, None);

    // 序列化往返：None 字段不输出（兼容旧清单）
    let s = serde_json::to_string(&cfg2).unwrap();
    assert!(
        !s.contains("request_timeout_secs"),
        "None 字段不应序列化: {s}"
    );
}

#[test]
fn plugin_manifest_host_group_serde_roundtrip() {
    // 合宿进程模型 §4.1：host_group 缺省 None（旧清单零迁移）；
    // 显式 "light" → Some；None 不序列化输出（兼容旧清单）。
    let base = base_manifest_json();
    let m: PluginManifest = serde_json::from_value(base).expect("缺省 host_group 应可解析");
    assert_eq!(m.host_group, None, "缺省 = None（独占宿主，默认保守）");

    let mut light = base_manifest_json();
    light["host_group"] = serde_json::json!("light");
    let m2: PluginManifest = serde_json::from_value(light).expect("host_group=light 应可解析");
    assert_eq!(m2.host_group.as_deref(), Some("light"));

    // 序列化往返：None 字段不输出
    let s = serde_json::to_string(&m).unwrap();
    assert!(!s.contains("host_group"), "None 字段不应序列化: {s}");
    let s2 = serde_json::to_string(&m2).unwrap();
    assert!(s2.contains("host_group"), "Some 字段应保留: {s2}");
}

#[test]
fn tool_capability_isolation_capable_serde() {
    // 显式声明 → 保留；缺省 → None（旧清单零迁移）；None 不序列化输出。
    let mut base = base_manifest_json();
    base["capabilities"]["tools"] = serde_json::json!([{
        "name": "t", "input_schema": {"type": "object"},
        "isolation_capable": true,
    }]);
    let m: PluginManifest = serde_json::from_value(base.clone()).expect("显式声明应可解析");
    assert_eq!(
        m.capabilities.tools[0].isolation_capable,
        Some(true),
        "显式 true 应保留"
    );

    let mut absent = base_manifest_json();
    absent["capabilities"]["tools"] = serde_json::json!([{
        "name": "t", "input_schema": {"type": "object"},
    }]);
    let m2: PluginManifest = serde_json::from_value(absent).expect("缺省应可解析（旧清单零迁移）");
    assert_eq!(m2.capabilities.tools[0].isolation_capable, None);

    let s = serde_json::to_string(&m2.capabilities.tools[0]).unwrap();
    assert!(!s.contains("isolation_capable"), "None 字段不应序列化: {s}");
    let s2 = serde_json::to_string(&m.capabilities.tools[0]).unwrap();
    assert!(s2.contains("isolation_capable"), "Some 字段应保留: {s2}");
}

/// capabilities.steps 契约（管道步骤服务化提案 §3.1）：带 steps 的
/// manifest 反序列化 → 序列化 → 反序列化一致（roundtrip 幂等）。
#[test]
fn manifest_steps_serde_roundtrip() {
    let json = serde_json::json!({
        "id": "task_service",
        "name": "Task Service",
        "version": "1.0.0",
        "plugin_type": "pipeline",
        "language": "python",
        "host_type": "sidecar",
        "entry": "python server.py",
        "capabilities": {
            "steps": [
                {
                    "name": "task.inject_params",
                    "description": "注入任务参数",
                    "input_schema": {"type": "object", "properties": {"params": {"type": "string"}}}
                },
                { "name": "task.remind", "description": "评估闸门推进" }
            ]
        }
    });
    let m: PluginManifest = serde_json::from_value(json).expect("带 steps 的 manifest 应可解析");
    assert_eq!(m.capabilities.steps.len(), 2);
    assert_eq!(m.capabilities.steps[0].name, "task.inject_params");
    assert_eq!(
        m.capabilities.steps[0].description.as_deref(),
        Some("注入任务参数")
    );
    assert!(m.capabilities.steps[0].input_schema.is_some());
    assert_eq!(m.capabilities.steps[1].name, "task.remind");
    assert_eq!(m.capabilities.steps[1].input_schema, None);

    // roundtrip：序列化后重新反序列化，再序列化产物逐字节一致
    // （None 字段不输出、空 input_schema 不输出，但语义等价）
    let s = serde_json::to_string(&m).unwrap();
    assert!(s.contains("task.inject_params"), "序列化应保留步骤名: {s}");
    let m2: PluginManifest = serde_json::from_str(&s).expect("序列化产物应可重新解析");
    let s2 = serde_json::to_string(&m2).unwrap();
    assert_eq!(s, s2, "roundtrip 后序列化产物应逐字节一致");
}

/// state.reads 读面声明契约：三种合法形态（键名 / `messages` 全量 /
/// `messages_tail:N`）解析并保留；roundtrip 幂等；空 reads 不序列化输出。
#[test]
fn state_declaration_reads_serde_roundtrip() {
    let mut json = base_manifest_json();
    json["state"] = serde_json::json!({
        "volatile_keys": ["mood.notes"],
        "reads": ["task.status", "messages", "messages_tail:50"]
    });
    let m: PluginManifest = serde_json::from_value(json).expect("含 reads 的 manifest 应可解析");
    let state = m.state.as_ref().expect("state 段应存在");
    assert_eq!(
        state.reads,
        vec![
            "task.status".to_string(),
            "messages".to_string(),
            "messages_tail:50".to_string()
        ],
        "三种合法形态原样收录"
    );

    // roundtrip：序列化产物可重新解析且逐字节一致（reads 保留）
    let s = serde_json::to_string(&m).unwrap();
    assert!(
        s.contains("messages_tail:50"),
        "序列化应保留 reads 条目: {s}"
    );
    let m2: PluginManifest = serde_json::from_str(&s).expect("序列化产物应可重新解析");
    assert_eq!(m2.state.as_ref().unwrap().reads, state.reads);
    let s2 = serde_json::to_string(&m2).unwrap();
    assert_eq!(s, s2, "roundtrip 后序列化产物应逐字节一致");

    // 空 reads 不序列化输出（与既有字段风格一致）；旧 state 段
    //（只有 volatile_keys）→ reads 缺省为空
    let mut legacy = base_manifest_json();
    legacy["state"] = serde_json::json!({"volatile_keys": ["k"]});
    let m3: PluginManifest =
        serde_json::from_value(legacy).expect("旧 state 段（无 reads）应可解析");
    assert!(
        m3.state.as_ref().unwrap().reads.is_empty(),
        "缺省 reads = 空（向后兼容）"
    );
    let s3 = serde_json::to_string(&m3.state.as_ref().unwrap()).unwrap();
    assert!(!s3.contains("reads"), "空 reads 不应序列化输出: {s3}");
}

/// state.writes 持久写面声明契约（ADR 2026-10-02）：精确键与 `前缀.*`
/// 通配原样收录；roundtrip 幂等；空 writes 不序列化输出、旧 state 段
/// （无 writes）解析为空（向后兼容）。
#[test]
fn state_declaration_writes_serde_roundtrip() {
    let mut json = base_manifest_json();
    json["state"] = serde_json::json!({
        "writes": ["task.status", "task.owned.*", "trigger.registry.*"]
    });
    let m: PluginManifest = serde_json::from_value(json).expect("含 writes 的 manifest 应可解析");
    let state = m.state.as_ref().expect("state 段应存在");
    assert_eq!(
        state.writes,
        vec![
            "task.status".to_string(),
            "task.owned.*".to_string(),
            "trigger.registry.*".to_string()
        ],
        "精确键与通配声明原样收录"
    );

    let s = serde_json::to_string(&m).unwrap();
    assert!(s.contains("trigger.registry.*"), "序列化应保留 writes: {s}");
    let m2: PluginManifest = serde_json::from_str(&s).expect("序列化产物应可重新解析");
    assert_eq!(m2.state.as_ref().unwrap().writes, state.writes);
    let s2 = serde_json::to_string(&m2).unwrap();
    assert_eq!(s, s2, "roundtrip 后序列化产物应逐字节一致");

    let mut legacy = base_manifest_json();
    legacy["state"] = serde_json::json!({"volatile_keys": ["k"]});
    let m3: PluginManifest =
        serde_json::from_value(legacy).expect("旧 state 段（无 writes）应可解析");
    assert!(
        m3.state.as_ref().unwrap().writes.is_empty(),
        "缺省 writes = 空（向后兼容）"
    );
    let s3 = serde_json::to_string(&m3.state.as_ref().unwrap()).unwrap();
    assert!(!s3.contains("writes"), "空 writes 不应序列化输出: {s3}");
}

/// 向后兼容：旧 manifest 无 capabilities.steps 键 → 反序列化成功且 steps 为空
/// （serde(default) 兜底，存量 25+ 管道插件零迁移）。
#[test]
fn manifest_without_steps_backward_compat() {
    let json = serde_json::json!({
        "id": "legacy_pipeline",
        "name": "Legacy",
        "version": "1.0.0",
        "plugin_type": "pipeline",
        "language": "python",
        "host_type": "sidecar",
        "entry": "python server.py",
        "capabilities": {"services": [{"name": "svc.health"}]}
    });
    let m: PluginManifest =
        serde_json::from_value(json).expect("无 steps 键的旧 manifest 应可解析");
    assert!(
        m.capabilities.steps.is_empty(),
        "旧 manifest 缺省 steps 应为空（隐式默认注册兜底）"
    );
    // 无 steps 时不输出 steps 键（旧清单兼容输出）
    let s = serde_json::to_string(&m).unwrap();
    assert!(!s.contains("\"steps\""), "空 steps 不应序列化输出: {s}");
}

// ── trait 默认实现契约（具体实现覆盖后无人单调默认版，此处锁默认语义）──

use crate::types::{DispatchRequest, PendingInputRecord, PendingInputSource};

struct BareInvoker;

#[async_trait::async_trait]
impl PluginInvoker for BareInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        _ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        unreachable!("默认面测试不调用")
    }
    async fn invoke_tool(
        &self,
        _plugin_id: &str,
        _tool_name: &str,
        _inputs: &serde_json::Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        unreachable!("默认面测试不调用")
    }
    async fn send_lifecycle_hook(
        &self,
        _plugin_id: &str,
        _hook: LifecycleHook,
        _context: &HookContext,
    ) -> Result<(), PluginError> {
        unreachable!("默认面测试不调用")
    }
}

#[tokio::test]
async fn invoker_default_methods_contract() {
    let inv = BareInvoker;
    // force_unload 默认 Ok；reload_member 默认不支持（调用方须回退 force_unload）；
    // discover 默认空；list_plugin_tools 默认不支持；
    // shutdown_all / kill_sidecar_if_any 默认 no-op（调用即覆盖，无 panic 即契约）。
    assert!(inv.force_unload("p").await.is_ok());
    assert!(inv.reload_member("p").await.is_err());
    assert!(inv.discover_new_plugins().await.unwrap().is_empty());
    assert!(inv.list_plugin_tools("p").await.is_err());
    inv.shutdown_all().await;
    inv.kill_sidecar_if_any("p").await;
}

struct BareStore;

#[async_trait::async_trait]
impl StorageBackend for BareStore {
    async fn get_run(&self, _run_id: &str) -> Result<RunRecord, StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn get_messages_by_pipeline(
        &self,
        _pipeline_id: &str,
        _opts: MessageQueryOpts,
    ) -> Result<Vec<MessageRecord>, StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn get_blob(&self, _blob_id: &str) -> Result<Vec<u8>, StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn append_trace(&self, _entry: TraceEntry) -> Result<(), StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn store_blob(&self, _data: &[u8], _mime_type: &str) -> Result<String, StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn create_session(&self, _session: &SessionRecord) -> Result<(), StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn get_session(&self, _thread_id: &str) -> Result<Option<SessionRecord>, StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn list_sessions(
        &self,
        _filter: SessionListFilter,
    ) -> Result<Vec<SessionRecord>, StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn update_session(&self, _session: &SessionRecord) -> Result<(), StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn delete_session(&self, _thread_id: &str) -> Result<Vec<String>, StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn link_pipeline_session(
        &self,
        _pipeline_id: &str,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<(), StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn list_pipeline_ids_by_thread(
        &self,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<String>, StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn get_step_traces_by_thread(
        &self,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<TraceEntry>, StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn create_user(&self, _user: &UserRecord) -> Result<(), StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn get_user_by_id(&self, _user_id: &str) -> Result<Option<UserRecord>, StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn get_user_by_username(
        &self,
        _username: &str,
    ) -> Result<Option<UserRecord>, StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn list_users(&self) -> Result<Vec<UserRecord>, StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn update_last_login(&self, _user_id: &str) -> Result<(), StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn update_user_password(
        &self,
        _user_id: &str,
        _password_hash: &str,
        _must_change_password: bool,
    ) -> Result<bool, StorageError> {
        unreachable!("默认面测试不调用")
    }
    async fn delete_user(&self, _user_id: &str) -> Result<bool, StorageError> {
        unreachable!("默认面测试不调用")
    }
}

fn bare_pending_input() -> PendingInputRecord {
    PendingInputRecord {
        id: "i".into(),
        tenant_id: "t".into(),
        created_at: "2026-01-01T00:00:00Z".into(),
        request: DispatchRequest {
            thread_id: "th".into(),
            user_id: "u".into(),
            pipeline_id: "p".into(),
            content: "c".into(),
            client_message_id: String::new(),
            source: PendingInputSource::System,
            execution_context: None,
            state_overlay: None,
            pipeline_config_id: None,
        },
    }
}

#[tokio::test]
async fn storage_default_methods_contract() {
    let s = BareStore;
    // 运行投影族默认：record_run_start no-op = Ok；list_runs_by_pipeline 空集
    assert!(s
        .record_run_start("p", "t", "r", "hash", "2026-10-15T00:00:00+00:00")
        .await
        .is_ok());
    assert!(s.list_runs_by_pipeline("p", "t").await.unwrap().is_empty());
    assert!(s.list_pipelines("t", None, 10).await.unwrap().is_empty());
    assert!(s.delete_pipeline("p").await.is_ok());
    // state 投影族默认：no-op / 空 map / None
    assert!(s.apply_messages_ops_to_table("p", "t", &[]).await.is_ok());
    assert!(s
        .upsert_state_field("p", "t", "k", &serde_json::json!(1))
        .await
        .is_ok());
    assert!(s.load_pipeline_state("p", "t").await.unwrap().is_empty());
    assert!(s
        .save_checkpoint("p", "t", 1, &serde_json::json!({}))
        .await
        .is_ok());
    assert!(s.load_latest_checkpoint("p", "t").await.unwrap().is_none());
    assert!(s.list_state_pipeline_ids("t").await.unwrap().is_empty());
    assert!(s.load_message_history("p", "t").await.unwrap().is_empty());
    // pending 队列族默认：no-op / None / 空 / false / 0
    let rec = bare_pending_input();
    assert!(s.enqueue_pending_input("t", &rec).await.is_ok());
    assert!(s.pop_pending_input("t", "p").await.unwrap().is_none());
    assert!(s.list_pending_inputs("t", "p").await.unwrap().is_empty());
    assert!(!s
        .update_pending_input_content("t", "p", "i", "x")
        .await
        .unwrap());
    assert!(!s.delete_pending_input("t", "p", "i").await.unwrap());
    assert_eq!(s.clear_pending_inputs("t", "p").await.unwrap(), 0);
}

#[tokio::test]
async fn storage_remaining_default_methods_contract() {
    let s = BareStore;
    // 声明易变键注入：no-op 契约（调用即覆盖，不 panic 即契约）
    s.set_declared_volatile_keys(&["mood.notes".to_string(), "scratch".to_string()]);
    // B6 批量 upsert 默认退化为逐键单键 upsert：全成才 Ok
    let mut fields = serde_json::Map::new();
    fields.insert("track.total_tokens".into(), serde_json::json!(42));
    fields.insert("mood".into(), serde_json::json!("ok"));
    assert!(s.upsert_state_fields("p", "t", &fields).await.is_ok());
    // 会话坐标反查默认 None；管道 step 轨迹默认空
    assert!(s.get_thread_id_by_pipeline("p").await.unwrap().is_none());
    assert!(s
        .get_step_traces_by_pipeline("p", "t")
        .await
        .unwrap()
        .is_empty());
}

// ── serde 往返 / 声明面 / 默认值契约 ─────────────────────────────

/// serde 往返断言：序列化 → 反序列化 → 再序列化逐字节一致（线格式稳定）。
fn assert_serde_roundtrip<T>(value: &T)
where
    T: serde::Serialize + serde::de::DeserializeOwned,
{
    let text = serde_json::to_string(value).expect("序列化失败");
    let back: T = serde_json::from_str(&text).expect("反序列化失败");
    let text2 = serde_json::to_string(&back).expect("再序列化失败");
    assert_eq!(text2, text, "roundtrip 线格式不一致");
}

fn minimal_manifest() -> PluginManifest {
    serde_json::from_value(base_manifest_json()).expect("最小 manifest 应可解析")
}

#[test]
fn registration_guard_drop_revokes_exactly_once() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    let counter = std::sync::Arc::new(AtomicUsize::new(0));
    let c2 = counter.clone();
    let guard = RegistrationGuard::new(move || {
        c2.fetch_add(1, Ordering::SeqCst);
    });
    assert_eq!(counter.load(Ordering::SeqCst), 0, "构造不触发注销");
    drop(guard);
    assert_eq!(
        counter.load(Ordering::SeqCst),
        1,
        "drop 必须精确注销该条注册"
    );
}

#[test]
fn registration_guard_disarm_skips_revoke() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    let counter = std::sync::Arc::new(AtomicUsize::new(0));
    let c2 = counter.clone();
    let guard = RegistrationGuard::new(move || {
        c2.fetch_add(1, Ordering::SeqCst);
    });
    guard.disarm(); // 所有权转交 / 停机整体清算：不再逐条注销
    assert_eq!(
        counter.load(Ordering::SeqCst),
        0,
        "disarm 后 drop 不应再注销"
    );
}

#[test]
fn http_route_descriptor_defaults_and_accessors() {
    let endpoint = |timeout_ms: Option<u64>, max_concurrency: Option<u32>| HttpEndpoint {
        route_id: "r1".into(),
        method: "POST".into(),
        path: "/ext/p/callback".into(),
        auth: Some("none".into()),
        handler_capability: "http.handle".into(),
        timeout_ms,
        max_concurrency,
        description: None,
    };
    // 缺省：30000ms / 16 并发（附录 E.1.3）
    let d = HttpRouteDescriptor::new("p".into(), endpoint(None, None));
    assert_eq!(d.plugin_id, "p");
    assert_eq!(d.timeout_ms(), HTTP_ENDPOINT_DEFAULT_TIMEOUT_MS);
    assert_eq!(d.max_concurrency(), HTTP_ENDPOINT_DEFAULT_MAX_CONCURRENCY);
    // 显式声明优先生效
    let e = HttpRouteDescriptor::new("p".into(), endpoint(Some(1234), Some(3)));
    assert_eq!(e.timeout_ms(), 1234);
    assert_eq!(e.max_concurrency(), 3);
    // 运行时表示可经 JSON 传输且解析值保真
    let text = serde_json::to_string(&e).unwrap();
    let back: HttpRouteDescriptor = serde_json::from_str(&text).unwrap();
    assert_eq!(back.timeout_ms(), 1234);
    assert_eq!(back.max_concurrency(), 3);
    assert_eq!(back.endpoint, e.endpoint);
    assert_eq!(back.plugin_id, "p");
}

#[test]
fn http_endpoint_auth_contract() {
    let parse =
        |json: &str| -> Result<HttpEndpoint, serde_json::Error> { serde_json::from_str(json) };
    // 合法枚举：none / user / admin
    for v in ["none", "user", "admin"] {
        let ep = parse(&format!(
                r#"{{"route_id":"r","method":"GET","path":"/ext/p/x","auth":"{v}","handler_capability":"http.handle"}}"#
            ))
            .unwrap();
        assert_eq!(ep.auth.as_deref(), Some(v));
    }
    // 字段缺省 → None（无声明，dispatcher fail-closed）
    let missing = parse(
        r#"{"route_id":"r","method":"GET","path":"/ext/p/x","handler_capability":"http.handle"}"#,
    )
    .unwrap();
    assert_eq!(missing.auth, None);
    // 显式 null → None
    let null_ep = parse(
            r#"{"route_id":"r","method":"GET","path":"/ext/p/x","auth":null,"handler_capability":"http.handle"}"#,
        )
        .unwrap();
    assert_eq!(null_ep.auth, None);
    // 非法值 → 反序列化期拒绝（fail-closed）
    let err = parse(
            r#"{"route_id":"r","method":"GET","path":"/ext/p/x","auth":"root","handler_capability":"http.handle"}"#,
        )
        .unwrap_err();
    assert!(
        err.to_string().contains("must be one of none/user/admin"),
        "非法 auth 报错应指明合法值域: {err}"
    );
    // roundtrip（None 序列化为 null 后仍可解析回 None）
    assert_serde_roundtrip(&missing);
}

struct EchoHttpHandler;

#[async_trait::async_trait]
impl HttpHandleCapability for EchoHttpHandler {
    async fn handle(&self, req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
        if req.path.ends_with("/boom") {
            return Err("handler exploded".into());
        }
        Ok(HttpHandleResponse {
            status: 200,
            headers: HashMap::from([("x-echo-plugin".to_string(), req.plugin_id)]),
            body: req.raw_body,
            body_encoding: "base64".into(),
        })
    }
}

#[tokio::test]
async fn http_handle_capability_passthrough_contract() {
    let handler = EchoHttpHandler;
    // 铁律：raw_body 原文透传（base64），headers 全量透传
    let resp = handler
        .handle(HttpHandleRequest {
            method: "POST".into(),
            path: "/ext/wecom/callback".into(),
            plugin_id: "wecom".into(),
            raw_body: "aGVsbG8=".into(),
            headers: HashMap::from([("x-signature".to_string(), "sig".to_string())]),
            query: HashMap::from([("msg_signature".to_string(), "s".to_string())]),
            query_multi: HashMap::from([(
                "filter".to_string(),
                vec!["a".to_string(), "b".to_string()],
            )]),
        })
        .await
        .expect("透传请求应成功");
    assert_eq!(resp.status, 200);
    assert_eq!(resp.body, "aGVsbG8=", "raw_body 原文透传");
    assert_eq!(resp.body_encoding, "base64");
    assert_eq!(
        resp.headers.get("x-echo-plugin").map(String::as_str),
        Some("wecom"),
        "插件完全控制响应头"
    );
    // 错误是值：Err 字符串由 dispatcher 记 502
    let err = handler
        .handle(HttpHandleRequest {
            method: "POST".into(),
            path: "/ext/wecom/boom".into(),
            plugin_id: "wecom".into(),
            raw_body: String::new(),
            headers: HashMap::new(),
            query: HashMap::new(),
            query_multi: HashMap::new(),
        })
        .await
        .unwrap_err();
    assert_eq!(err, "handler exploded");
}

#[test]
fn http_handle_request_query_multi_backward_compat() {
    // 旧负载无 query_multi → 空 map（向后兼容，SDK 侧按 handler 签名过滤）
    let legacy: HttpHandleRequest = serde_json::from_str(
            r#"{"method":"GET","path":"/p","plugin_id":"x","raw_body":"","headers":{},"query":{"a":"1"}}"#,
        )
        .unwrap();
    assert!(legacy.query_multi.is_empty());
    assert_eq!(legacy.query.get("a").map(String::as_str), Some("1"));
    // 多值形态 roundtrip：query[k] == query_multi[k].last()
    let full = HttpHandleRequest {
        method: "GET".into(),
        path: "/p".into(),
        plugin_id: "x".into(),
        raw_body: String::new(),
        headers: HashMap::new(),
        query: HashMap::from([("filter".to_string(), "b".to_string())]),
        query_multi: HashMap::from([(
            "filter".to_string(),
            vec!["a".to_string(), "b".to_string()],
        )]),
    };
    assert_serde_roundtrip(&full);
    // 响应结构 roundtrip
    let resp = HttpHandleResponse {
        status: 502,
        headers: HashMap::from([("content-type".to_string(), "text/xml".to_string())]),
        body: "PGZvby8+".into(),
        body_encoding: "base64".into(),
    };
    let text = serde_json::to_string(&resp).unwrap();
    let back: HttpHandleResponse = serde_json::from_str(&text).unwrap();
    assert_eq!(back.status, 502);
    assert_eq!(back.body, "PGZvby8+");
}

#[test]
fn hook_context_tagged_context_contract() {
    let mut ctx = HookContext::new();
    assert!(ctx.get("session_id").is_none(), "空上下文查无");
    // 链式 set（Builder 模式）
    ctx.set("session_id", serde_json::json!("s-1"))
        .set("iteration", serde_json::json!(3));
    assert_eq!(ctx.get("session_id"), Some(&serde_json::json!("s-1")));
    // get_as 类型化读取：成功 + 类型不符静默 None（消费方按需处理）
    assert_eq!(ctx.get_as::<i64>("iteration"), Some(3));
    assert_eq!(ctx.get_as::<String>("iteration"), None, "类型不符静默降级");
    assert_eq!(ctx.get_as::<String>("missing"), None);
    // 同 key 后写覆盖
    ctx.set("iteration", serde_json::json!(4));
    assert_eq!(ctx.get_as::<i64>("iteration"), Some(4));
    assert_eq!(ctx.tags().len(), 2);
    // 上下文随钩子事件跨进程传输：serde 往返保真
    let text = serde_json::to_string(&ctx).unwrap();
    let back: HookContext = serde_json::from_str(&text).unwrap();
    assert_eq!(back.get_as::<String>("session_id"), Some("s-1".to_string()));
    assert_eq!(back.get_as::<i64>("iteration"), Some(4));
}

/// HookContext::default() 等价 new()（空上下文）——派生/默认构造路径
/// 与显式构造同语义，消费方可互换使用。
#[test]
fn hook_context_default_equals_new() {
    let d = HookContext::default();
    assert!(d.tags().is_empty(), "默认构造应为空标签表");
    assert!(d.get("any").is_none());
    assert_eq!(d.get_as::<String>("any"), None);
    let n = HookContext::new();
    assert_eq!(d.tags(), n.tags());
}

/// ActivationPolicy 的 Default 是 Lazy（多数 tool/评估/监控插件的默认
/// 激活策略）：manifest 未声明时不得默认 Eager。
#[test]
fn activation_policy_default_is_lazy() {
    assert_eq!(ActivationPolicy::default(), ActivationPolicy::Lazy);
    assert_eq!(
        serde_json::from_str::<ActivationPolicy>("\"lazy\"").unwrap(),
        ActivationPolicy::Lazy
    );
}

#[test]
fn lifecycle_hook_serde_roundtrip_all_variants() {
    for (value, wire) in [
        (LifecycleHook::OnLoad, "on_load"),
        (LifecycleHook::OnUnload, "on_unload"),
        (LifecycleHook::OnPipelineStart, "on_pipeline_start"),
        (LifecycleHook::OnPipelineEnd, "on_pipeline_end"),
        (LifecycleHook::OnError, "on_error"),
        (LifecycleHook::DomainEvent, "domain_event"),
    ] {
        let text = serde_json::to_string(&value).unwrap();
        assert_eq!(text, format!("\"{wire}\""));
        let back: LifecycleHook = serde_json::from_str(&text).unwrap();
        assert_eq!(back, value);
    }
    assert!(serde_json::from_str::<LifecycleHook>("\"on_boot\"").is_err());
}

#[test]
fn plugin_type_serde_roundtrip() {
    for (value, wire) in [
        (PluginType::Pipeline, "pipeline"),
        (PluginType::Tool, "tool"),
        (PluginType::System, "system"),
        (PluginType::Composite, "composite"),
    ] {
        let text = serde_json::to_string(&value).unwrap();
        assert_eq!(text, format!("\"{wire}\""));
        let back: PluginType = serde_json::from_str(&text).unwrap();
        assert_eq!(back, value);
    }
    // 畸形输入拒收
    assert!(serde_json::from_str::<PluginType>("\"hook\"").is_err());
}

#[test]
fn host_activation_status_defaults_and_serde() {
    // Default：sidecar / lazy / discovered
    assert_eq!(HostType::default(), HostType::Sidecar);
    assert_eq!(ActivationPolicy::default(), ActivationPolicy::Lazy);
    assert_eq!(PluginStatus::default(), PluginStatus::Discovered);

    for (text, expected) in [
        ("\"in_process\"", HostType::InProcess),
        ("\"sidecar\"", HostType::Sidecar),
    ] {
        assert_eq!(serde_json::from_str::<HostType>(text).unwrap(), expected);
    }
    for (text, expected) in [
        ("\"eager\"", ActivationPolicy::Eager),
        ("\"lazy\"", ActivationPolicy::Lazy),
        ("\"manual\"", ActivationPolicy::Manual),
    ] {
        assert_eq!(
            serde_json::from_str::<ActivationPolicy>(text).unwrap(),
            expected
        );
    }
    for (text, expected) in [
        ("\"discovered\"", PluginStatus::Discovered),
        ("\"loading\"", PluginStatus::Loading),
        ("\"active\"", PluginStatus::Active),
        ("\"idle\"", PluginStatus::Idle),
        ("\"draining\"", PluginStatus::Draining),
        ("\"unloaded\"", PluginStatus::Unloaded),
        ("\"crashed\"", PluginStatus::Crashed),
        ("\"failed\"", PluginStatus::Failed),
    ] {
        assert_eq!(
            serde_json::from_str::<PluginStatus>(text).unwrap(),
            expected
        );
    }
    // 畸形输入拒收（wasm 轨已关闭；无 auto 激活；无 ready 态）
    assert!(serde_json::from_str::<HostType>("\"wasm\"").is_err());
    assert!(serde_json::from_str::<ActivationPolicy>("\"auto\"").is_err());
    assert!(serde_json::from_str::<PluginStatus>("\"ready\"").is_err());
}

#[test]
fn provided_capability_host_and_declaration_serde() {
    // Default InProcess；kebab-case 线格式
    assert_eq!(
        ProvidedCapabilityHost::default(),
        ProvidedCapabilityHost::InProcess
    );
    assert_eq!(
        serde_json::to_string(&ProvidedCapabilityHost::InProcess).unwrap(),
        "\"in-process\""
    );
    assert_eq!(
        serde_json::to_string(&ProvidedCapabilityHost::Sidecar).unwrap(),
        "\"sidecar\""
    );
    // 缺省 host = InProcess；tool_prefix 缺省 None（从 namespace 派生）
    let cap: ProvidedCapability =
        serde_json::from_str(r#"{"namespace":"interaction-respond","methods":["respond"]}"#)
            .unwrap();
    assert_eq!(cap.host, ProvidedCapabilityHost::InProcess);
    assert_eq!(cap.tool_prefix, None);
    assert!(cap.protocol_roles.is_empty());
    // 完整形态：sidecar host + 显式前缀 + 协议角色（P1-3）
    let full = ProvidedCapability {
        namespace: "interaction-respond".into(),
        methods: vec!["respond".into()],
        host: ProvidedCapabilityHost::Sidecar,
        tool_prefix: Some("ix".into()),
        protocol_roles: vec![ProtocolRole {
            role: "interaction-respond".into(),
            method: "respond".into(),
        }],
    };
    assert_serde_roundtrip(&full);
    assert_eq!(
        full.protocol_roles[0].role, "interaction-respond",
        "角色绑定声明保真"
    );
}

#[test]
fn endpoint_auth_serde_defaults() {
    // type 缺省 api_key；header_name 缺省 Authorization；required 缺省 None（按必需处理）
    let a: EndpointAuth = serde_json::from_str(r#"{"value":"${K}"}"#).unwrap();
    assert_eq!(a.auth_type, AuthType::ApiKey);
    assert_eq!(a.header_name, "Authorization");
    assert_eq!(a.required, None);
    // 显式 bearer + required=false（可选凭据语义）
    let b: EndpointAuth = serde_json::from_str(
        r#"{"type":"bearer","header_name":"Authorization","value":"x","required":false}"#,
    )
    .unwrap();
    assert_eq!(b.auth_type, AuthType::Bearer);
    assert_eq!(b.required, Some(false));
    // AuthType 三态线格式（snake_case）
    assert_eq!(
        serde_json::to_string(&AuthType::ApiKey).unwrap(),
        "\"api_key\""
    );
    assert_eq!(
        serde_json::to_string(&AuthType::Bearer).unwrap(),
        "\"bearer\""
    );
    assert_eq!(serde_json::to_string(&AuthType::None).unwrap(), "\"none\"");
    // 畸形输入拒收
    assert!(serde_json::from_str::<EndpointAuth>(r#"{"value":"x","type":"oauth"}"#).is_err());
}

#[test]
fn mcp_endpoint_serde_defaults() {
    let e: McpEndpoint = serde_json::from_str("{}").unwrap();
    assert_eq!(e.url, None);
    assert!(e.headers.is_empty());
    assert!(e.auth.is_none());
    assert!(e.command.is_none());
    assert!(e.args.is_empty());
    assert!(e.env.is_empty());
    // stdio 形态（本地第三方命令）：字段保真 + ${ENV_VAR} 占位原样保留
    let stdio: McpEndpoint = serde_json::from_str(
        r#"{"command":"npx","args":["-y","mcp"],"env":{"K":"${V}"},"url":null}"#,
    )
    .unwrap();
    assert_eq!(stdio.command.as_deref(), Some("npx"));
    assert_eq!(stdio.args, vec!["-y".to_string(), "mcp".to_string()]);
    assert_eq!(stdio.env.get("K").map(String::as_str), Some("${V}"));
    assert_eq!(stdio.url, None);
    // 序列化再解析保真（invoker 构造客户端的输入面）
    assert_serde_roundtrip(&stdio);
}

#[test]
fn mcp_config_serde_defaults() {
    let c: McpConfig = serde_json::from_str(r#"{"transport":"stdio"}"#).unwrap();
    assert_eq!(c.transport, McpTransport::Stdio);
    assert_eq!(c.idle_timeout_secs, 300, "缺省空闲卸载 300s");
    assert_eq!(c.protocol_version, "2025-06-18");
    assert!(
        c.request_timeout_secs.is_none(),
        "缺省 = None（内核 mcp 客户端默认 86400s 兜底，BUG-60 审批族统一值）"
    );
    assert!(c.endpoint.is_none(), "stdio 无外部端点");
    // HTTP 形态：长等待显式声明保留
    let h: McpConfig = serde_json::from_str(
            r#"{"transport":"streamable_http","endpoint":{"url":"https://x"},"request_timeout_secs":86400}"#,
        )
        .unwrap();
    assert_eq!(h.transport, McpTransport::StreamableHttp);
    assert_eq!(
        h.endpoint.as_ref().unwrap().url.as_deref(),
        Some("https://x")
    );
    assert_eq!(h.request_timeout_secs, Some(86400));
    // 序列化 → 反序列化保真
    let back: McpConfig = serde_json::from_str(&serde_json::to_string(&h).unwrap()).unwrap();
    assert_eq!(back.request_timeout_secs, Some(86400));
    assert_eq!(back.idle_timeout_secs, 300);
    // 畸形 transport 拒收
    assert!(serde_json::from_str::<McpConfig>(r#"{"transport":"grpc"}"#).is_err());
}

#[test]
fn default_value_functions_contract() {
    assert_eq!(default_priority(), 100);
    assert_eq!(default_idle_timeout(), 300);
    assert_eq!(default_capability_timeout_ms(), 300_000);
    assert_eq!(HTTP_ENDPOINT_DEFAULT_TIMEOUT_MS, 30_000);
    assert_eq!(HTTP_ENDPOINT_DEFAULT_MAX_CONCURRENCY, 16);
}

#[test]
fn env_config_field_defaults_and_extra_flatten() {
    // 缺省：secret（保守默认，宁可掩码不可泄漏）+ 非必填
    let f: EnvConfigField = serde_json::from_str(r#"{"name":"K","label":"K"}"#).unwrap();
    assert_eq!(f.field_type, "secret");
    assert!(!f.required);
    assert_eq!(f.description, None);
    assert!(
        f.extra.as_ref().is_none_or(|m| m.is_empty()),
        "无 UI 词汇 → extra 为空"
    );
    // UI 词汇表平铺透传：序列化无 "extra" 键（谁的数据谁出表单）
    let typed: EnvConfigField = serde_json::from_str(
            r#"{"name":"model","label":"Model","type":"select","required":true,"options":["a","b"],"default":"a"}"#,
        )
        .unwrap();
    assert_eq!(typed.field_type, "select");
    assert!(typed.required);
    let extra = typed.extra.as_ref().expect("UI 词汇应进 extra");
    assert_eq!(extra.get("options"), Some(&serde_json::json!(["a", "b"])));
    assert_eq!(extra.get("default"), Some(&serde_json::json!("a")));
    let text = serde_json::to_string(&typed).unwrap();
    assert!(text.contains(r#""options":["a","b"]"#), "平铺输出: {text}");
    assert!(!text.contains(r#""extra""#), "不应产生 extra 键: {text}");
    // roundtrip 保真
    let back: EnvConfigField = serde_json::from_str(&text).unwrap();
    assert_eq!(back, typed);
}

#[test]
fn config_file_mapping_serde_roundtrip() {
    // env target：密钥表单（required 语义驱动 connect 硬失败/降级）
    let full = ConfigFileMapping {
        id: "api_key".into(),
        settings: Some(false),
        path: "config/models/llm.yaml".into(),
        label: "LLM Key".into(),
        target: Some("env".into()),
        fields: vec![EnvConfigField {
            name: "OPENAI_API_KEY".into(),
            label: "OpenAI Key".into(),
            field_type: "secret".into(),
            required: true,
            description: None,
            extra: None,
        }],
    };
    assert_serde_roundtrip(&full);
    // 内联形态：path 省略 → 空串（真值即 fields.default）
    let inline: ConfigFileMapping = serde_json::from_str(r#"{"id":"k","label":"K"}"#).unwrap();
    assert_eq!(inline.path, "");
    assert_eq!(inline.settings, None);
    assert_eq!(inline.target, None);
    assert!(inline.fields.is_empty());
}

#[test]
fn tool_capability_serde_roundtrip() {
    let t = ToolCapability {
        name: "search".into(),
        description: Some("web search".into()),
        input_schema: Some(serde_json::json!({"type": "object"})),
        output_schema: Some(serde_json::json!({"type": "object"})),
        category: Some(ToolCategory::Search),
        ui: Some(serde_json::json!({"card": "web"})),
        render: Some(serde_json::json!({"card": "search"})),
        isolation_capable: None,
        smoke: Some(true),
        timeout_ms: Some(0), // 0 哨兵 = 显式豁免总时长超时
    };
    assert_serde_roundtrip(&t);
    // None 字段不序列化（清单兼容输出）；反解析缺省 None
    let bare = ToolCapability {
        name: "t".into(),
        description: None,
        input_schema: None,
        output_schema: None,
        category: None,
        ui: None,
        render: None,
        isolation_capable: None,
        smoke: None,
        timeout_ms: None,
    };
    let s = serde_json::to_string(&bare).unwrap();
    assert!(!s.contains("timeout_ms") && !s.contains("render"), "{s}");
    let back: ToolCapability = serde_json::from_str(&s).unwrap();
    assert_eq!(back.timeout_ms, None);
    assert_eq!(back.smoke, None);
}

#[test]
fn service_and_step_capability_serde() {
    // 服务声明：input/output schema 可选（auto-backfill 前 None 容忍）
    let svc = ServiceCapability {
        name: "memory.search".into(),
        description: Some("语义检索".into()),
        input_schema: Some(serde_json::json!({"type": "object"})),
        output_schema: None,
    };
    let text = serde_json::to_string(&svc).unwrap();
    assert!(!text.contains("output_schema"), "None 不序列化: {text}");
    let back: ServiceCapability = serde_json::from_str(&text).unwrap();
    assert_eq!(back.output_schema, None);
    assert_eq!(back.name, "memory.search");
    // 步骤声明：三字段结构，roundtrip 保真
    let step = StepCapability {
        name: "task.inject_params".into(),
        description: None,
        input_schema: None,
    };
    let stext = serde_json::to_string(&step).unwrap();
    let sback: StepCapability = serde_json::from_str(&stext).unwrap();
    assert_eq!(sback, step);
}

#[test]
fn streaming_capability_serde_defaults() {
    let s: StreamingCapability = serde_json::from_str("{}").unwrap();
    assert!(
        !s.conduit,
        "缺省 false：非内核 LLM 器官，发射闸 fail-closed"
    );
    assert_eq!(s.events, None, "缺省 = 契约全部事件可发射");
    assert_eq!(s.part_types, None);
    assert_eq!(s.persist, None);
    let full = StreamingCapability {
        conduit: true,
        events: Some(vec!["message.delta".into()]),
        part_types: Some(vec!["custom_chart".into()]),
        persist: Some(true),
    };
    assert_serde_roundtrip(&full);
    // conduit=false 不输出（skip_serializing_if Not::not）
    let plain = StreamingCapability {
        conduit: false,
        events: None,
        part_types: None,
        persist: None,
    };
    assert!(!serde_json::to_string(&plain).unwrap().contains("conduit"));
}

#[test]
fn manifest_permissions_defaults_and_roundtrip() {
    let p = ManifestPermissions::default();
    assert!(p.env_vars.is_empty() && p.system_calls.is_empty());
    assert!(p.filesystem.read_paths.is_empty() && p.filesystem.write_paths.is_empty());
    assert!(p.network.allowed_hosts.is_empty());
    let full = ManifestPermissions {
        filesystem: FilesystemPermission {
            read_paths: vec!["config/**".into()],
            write_paths: vec!["workspace/**".into()],
        },
        network: NetworkPermission {
            allowed_hosts: vec!["api.openai.com".into()],
        },
        env_vars: vec!["OPENAI_API_KEY".into()],
        system_calls: vec!["exec".into()],
    };
    assert_serde_roundtrip(&full);
}

#[test]
fn manifest_permissions_rejects_unknown_fields() {
    // 声明面 fail-closed：权限键拼错必须解析期报错，不得静默丢成"空声明"。
    // 修复前 serde 无 deny_unknown_fields → typo 的清单解析成功但声明为空，
    // 而权限当前只靠声明留痕（不强制），声明失真即审计证据失真。
    let typo_group =
        serde_json::from_str::<ManifestPermissions>(r#"{"networks":{"allowed_hosts":["a.com"]}}"#)
            .expect_err("未知权限组键应拒绝");
    assert!(typo_group.to_string().contains("unknown"), "{typo_group}");

    let typo_net =
        serde_json::from_str::<ManifestPermissions>(r#"{"network":{"allow_hosts":["a.com"]}}"#)
            .expect_err("network 键拼错应拒绝");
    assert!(typo_net.to_string().contains("unknown"), "{typo_net}");

    let typo_fs = serde_json::from_str::<FilesystemPermission>(r#"{"read_path":["/tmp"]}"#)
        .expect_err("filesystem 键拼错应拒绝");
    assert!(typo_fs.to_string().contains("unknown"), "{typo_fs}");

    // 正对照：合法形态（含空对象，103 个现存 manifest 里 100 个是 `{}`）照常解析。
    let empty = serde_json::from_str::<ManifestPermissions>("{}").expect("空声明必须可解析");
    assert!(empty.network.allowed_hosts.is_empty());
    let full = serde_json::from_str::<ManifestPermissions>(
        r#"{"filesystem":{"read_paths":["*"],"write_paths":["*"]},
            "network":{"allowed_hosts":["registry.npmjs.org"]},
            "env_vars":["TOKEN"],"system_calls":["exec"]}"#,
    )
    .expect("全声明必须可解析");
    assert_eq!(
        full.network.allowed_hosts,
        vec!["registry.npmjs.org".to_string()]
    );
    assert_eq!(full.filesystem.read_paths, vec!["*".to_string()]);
}

#[test]
fn manifest_capabilities_rejects_unknown_fields() {
    // deny_unknown_fields：未知能力键解析期拒绝（fail-closed）
    let err = serde_json::from_str::<ManifestCapabilities>(r#"{"resources":[]}"#)
        .expect_err("未知键应拒绝");
    assert!(err.to_string().contains("unknown"), "{err}");
    let caps = ManifestCapabilities {
        tools: vec![ToolCapability {
            name: "t".into(),
            description: None,
            input_schema: None,
            output_schema: None,
            category: None,
            ui: None,
            render: None,
            isolation_capable: None,
            smoke: None,
            timeout_ms: None,
        }],
        services: vec![ServiceCapability {
            name: "s".into(),
            description: None,
            input_schema: None,
            output_schema: None,
        }],
        steps: vec![StepCapability {
            name: "st".into(),
            description: None,
            input_schema: None,
        }],
        route_signals: vec![RouteType::NextLlm],
        lifecycle_hooks: vec![LifecycleHook::OnLoad],
        metrics_class: Some("llm".to_string()),
        stop_reasons: vec!["task_evaluate_failed".to_string()],
        streaming: Some(StreamingCapability {
            conduit: false,
            events: None,
            part_types: None,
            persist: None,
        }),
        call_timeout_seconds: None,
    };
    assert_serde_roundtrip(&caps);
    // 空能力段 → 全缺省
    let empty: ManifestCapabilities = serde_json::from_str("{}").unwrap();
    assert!(
        empty.tools.is_empty()
            && empty.steps.is_empty()
            && empty.streaming.is_none()
            && empty.metrics_class.is_none()
            && empty.stop_reasons.is_empty()
    );
}

#[test]
fn plugin_manifest_full_serde_roundtrip() {
    // 顶层标量/简单字段先建基座，复合声明逐段并入（避免单个 json! 递归超限）
    let mut json = serde_json::json!({
        "id": "llm_core", "name": "LLM Core", "description": "LLM 执行器官",
        "version": "1.0.0", "plugin_type": "pipeline",
        "language": "python", "host_type": "sidecar", "host_group": "light",
        "entry": "python server.py",
        "requires_services": ["service-registry.search"],
        "priority": 200,
        "persistent_fields": ["track.total_tokens"],
        "state": {"volatile_keys": ["scratch"], "reads": ["messages_tail:20"]},
        "force_include_tools": ["forced_demo_tool"],
        "export_fields": ["task.owned.*"],
        "requires_content": 20,
        "invoke_entry": "llm_core.execute",
        "enabled": true,
        "activation": "eager"
    });
    let obj = json.as_object_mut().unwrap();
    obj.insert(
        "capabilities".into(),
        serde_json::json!({
            "tools": [{"name": "t", "smoke": true}],
            "services": [{"name": "llm.execute"}],
            "route_signals": ["next_llm"],
            "lifecycle_hooks": ["on_load", "domain_event"],
            "metrics_class": "llm",
            "streaming": {"conduit": true, "events": ["message.delta"], "persist": false}
        }),
    );
    obj.insert(
        "permissions".into(),
        serde_json::json!({
            "filesystem": {"read_paths": ["config/**"]},
            "network": {"allowed_hosts": ["api.x.com"]},
            "env_vars": ["K"],
            "system_calls": []
        }),
    );
    obj.insert(
        "mcp".into(),
        serde_json::json!({"transport": "stdio", "request_timeout_secs": 86400}),
    );
    obj.insert(
        "lifecycle".into(),
        serde_json::json!({"idle_timeout_secs": 0}),
    );
    obj.insert(
        "native".into(),
        serde_json::json!({"artifact": "libdemo.so"}),
    );
    obj.insert(
        "config_files".into(),
        serde_json::json!([{"id": "k", "path": "config/models/llm.yaml", "label": "K"}]),
    );
    obj.insert("ui_schema".into(), serde_json::json!({"type": "object"}));
    obj.insert("contributes".into(), serde_json::json!({"chatActions": []}));
    obj.insert(
        "http_endpoints".into(),
        serde_json::json!([{
            "route_id": "cb", "method": "POST", "path": "/ext/llm_core/cb",
            "auth": "none", "handler_capability": "http.handle",
            "timeout_ms": 5000, "max_concurrency": 4
        }]),
    );
    obj.insert(
        "provides".into(),
        serde_json::json!({"capabilities": [{
            "namespace": "interaction-respond", "methods": ["respond"],
            "host": "sidecar", "tool_prefix": "ix",
            "protocol_roles": [{"role": "interaction-respond", "method": "respond"}]
        }]}),
    );
    let m: PluginManifest = serde_json::from_value(json).expect("全量 manifest 应可解析");
    // 关键声明面字段逐项核验
    assert_eq!(m.priority, 200);
    assert_eq!(m.requires_content, Some(20));
    assert_eq!(m.activation, Some(ActivationPolicy::Eager));
    assert_eq!(m.enabled, Some(true));
    assert_eq!(m.host_group.as_deref(), Some("light"));
    assert_eq!(
        m.lifecycle.as_ref().unwrap().idle_timeout_secs,
        Some(0),
        "0 哨兵 = 永不空闲卸载"
    );
    assert_eq!(m.native.as_ref().unwrap().artifact, "libdemo.so");
    assert_eq!(
        m.provides.as_ref().unwrap().capabilities[0].namespace,
        "interaction-respond"
    );
    assert_eq!(m.http_endpoints[0].auth.as_deref(), Some("none"));
    assert_eq!(
        m.capabilities.metrics_class.as_deref(),
        Some("llm"),
        "capabilities.metrics_class 声明应可解析"
    );
    // roundtrip 幂等：序列化 → 反序列化 → 序列化逐字节一致
    let s = serde_json::to_string(&m).unwrap();
    let m2: PluginManifest = serde_json::from_str(&s).expect("序列化产物应可重解析");
    assert_eq!(
        serde_json::to_string(&m2).unwrap(),
        s,
        "roundtrip 逐字节一致"
    );
    // deny_unknown_fields：未知顶层字段拒绝
    let mut bogus = serde_json::to_value(&m).unwrap();
    bogus
        .as_object_mut()
        .unwrap()
        .insert("bogus_field".into(), serde_json::json!(1));
    assert!(serde_json::from_value::<PluginManifest>(bogus).is_err());
}

#[test]
fn plugin_manifest_minimal_parse_and_defaults() {
    let m = minimal_manifest();
    assert_eq!(m.priority, 100, "缺省 priority 100");
    assert_eq!(m.description, None);
    assert!(m.requires_services.is_empty());
    assert!(m.persistent_fields.is_empty());
    assert!(m.force_include_tools.is_empty());
    assert!(m.export_fields.is_empty());
    assert!(m.config_files.is_empty());
    assert!(m.http_endpoints.is_empty());
    assert!(m.state.is_none());
    assert!(m.lifecycle.is_none());
    assert!(m.mcp.is_none());
    assert!(m.provides.is_none());
    assert_eq!(m.requires_content, None);
    assert_eq!(m.activation, None);
    assert_eq!(m.enabled, None);
}

#[test]
fn plugin_lifecycle_serde() {
    let l: PluginLifecycle = serde_json::from_str("{}").unwrap();
    assert_eq!(l.idle_timeout_secs, None, "缺省 = 内核默认");
    let zero: PluginLifecycle = serde_json::from_str(r#"{"idle_timeout_secs":0}"#).unwrap();
    assert_eq!(zero.idle_timeout_secs, Some(0), "0 哨兵 = 永不空闲卸载");
    assert!(!serde_json::to_string(&l)
        .unwrap()
        .contains("idle_timeout_secs"));
}

#[test]
fn native_artifact_serde() {
    let n: NativeArtifact = serde_json::from_str(r#"{"artifact":"my.dll"}"#).unwrap();
    assert_eq!(n.artifact, "my.dll");
    assert_eq!(NativeArtifact::default().artifact, "");
    let text = serde_json::to_string(&n).unwrap();
    let back: NativeArtifact = serde_json::from_str(&text).unwrap();
    assert_eq!(back.artifact, "my.dll");
}

#[test]
fn loaded_plugin_serde_roundtrip() {
    let lp = LoadedPlugin {
        manifest: minimal_manifest(),
        status: PluginStatus::Active,
        loaded_at: Some(
            chrono::DateTime::parse_from_rfc3339("2026-09-13T00:00:00Z")
                .unwrap()
                .with_timezone(&chrono::Utc),
        ),
    };
    let text = serde_json::to_string(&lp).unwrap();
    assert!(text.contains("2026-09-13"), "{text}");
    let back: LoadedPlugin = serde_json::from_str(&text).unwrap();
    assert_eq!(back.status, PluginStatus::Active);
    assert_eq!(back.manifest.id, "p");
    assert_eq!(
        back.loaded_at.map(|t| t.to_rfc3339()),
        lp.loaded_at.map(|t| t.to_rfc3339())
    );
}

struct BareLoader;

#[async_trait::async_trait]
impl PluginLoader for BareLoader {
    async fn discover(&self, _root_paths: &[&str]) -> Result<Vec<PluginManifest>, PluginError> {
        Ok(Vec::new())
    }
    fn validate_manifest(&self, _manifest: &PluginManifest) -> Result<(), PluginError> {
        Ok(())
    }
    async fn load(&self, _plugin_id: &str) -> Result<LoadedPlugin, PluginError> {
        unreachable!("默认面测试不调用")
    }
    async fn unload(&self, _plugin_id: &str) -> Result<(), PluginError> {
        Ok(())
    }
    fn get_status(&self, _plugin_id: &str) -> PluginStatus {
        PluginStatus::Discovered
    }
}

#[tokio::test]
async fn loader_default_methods_contract() {
    let loader = BareLoader;
    // load_config 默认空配置；get_plugin_dir 默认 None（内核 CWD）；get_manifest 默认 None
    assert_eq!(loader.load_config().await.unwrap(), serde_json::json!({}));
    assert_eq!(loader.get_plugin_dir("p"), None);
    assert!(loader.get_manifest("p").is_none());
    assert_eq!(loader.get_status("p"), PluginStatus::Discovered);
}

#[tokio::test]
async fn invoker_host_boxes_snapshot_default_is_empty() {
    // 复用既有 BareInvoker 桩（无宿主概念实现走 trait 默认面）
    let snap = BareInvoker.host_boxes_snapshot().await;
    assert!(snap.hosts.is_empty(), "无宿主概念实现 = 无盒子");
    assert!(snap.pending_spawn.is_empty());
    // 序列化为空骨架（响应契约顶层形状）
    assert_eq!(
        serde_json::to_value(&snap).unwrap(),
        serde_json::json!({"hosts": [], "pending_spawn": []})
    );
}

#[test]
fn message_query_opts_and_session_filter_defaults() {
    let q = MessageQueryOpts::default();
    assert_eq!(q.before_sequence, None);
    assert_eq!(q.after_sequence, None);
    assert_eq!(q.limit, None);
    let f = SessionListFilter::default();
    assert_eq!(f.session_type, None);
    assert_eq!(f.limit, None);
}

// @feature: FP-0.2.一 插件协议 | @ci: rust-test
//! chat.send_message 管道创建契约单测。
//! 由 chat_send_handler.rs 的主 #[cfg(test)] 测试块体平移而来（保留私有项访问；
//! 文件长度机械闸触碰即拆——ADR 2026-10-12-file-length-mechanical-gate 同款
//! capability_router/tests.rs 先例）。
//! GAP-1 阶段 1：chat.send_message 管道创建契约单测。
//! 覆盖：创建分支 id 生成与响应、保留字拒绝、state overlay（含血缘扁平键）
//! 透传与出生落表——lineage 方案由调用方插件自持，内核零知识。

use super::*;
use serde_json::json;
use std::sync::{Arc, Mutex};

/// 单次 dispatch_user_input 记录（全参快照）：
/// (thread_id, user_id, content, pipeline_id, thinking, execution_context,
///  state_overlay, agent_id, pipeline_config_id)
type DispatchRecord = (
    String,
    String,
    String,
    String,
    String,
    Option<Value>,
    Option<Value>,
    String,
    Option<String>,
);

/// 记录型 mock dispatcher：捕获每次 dispatch 调用供断言。
struct RecordingDispatcher {
    calls: Mutex<Vec<DispatchRecord>>,
}

impl RecordingDispatcher {
    fn shared() -> Arc<Self> {
        Arc::new(Self {
            calls: Mutex::new(Vec::new()),
        })
    }
}

#[async_trait]
impl PipelineDispatcher for RecordingDispatcher {
    async fn dispatch_user_input(
        &self,
        thread_id: &str,
        user_id: &str,
        content: &str,
        pipeline_id: &str,
        thinking_strength: &str,
        execution_context: Option<&Value>,
        state_overlay: Option<&Value>,
        agent_id: &str,
        pipeline_config_id: Option<&str>,
        _cmid: &str,
        _source: PendingInputSource,
    ) -> Result<(), String> {
        self.calls.lock().unwrap().push((
            thread_id.to_string(),
            user_id.to_string(),
            content.to_string(),
            pipeline_id.to_string(),
            thinking_strength.to_string(),
            execution_context.cloned(),
            state_overlay.cloned(),
            agent_id.to_string(),
            pipeline_config_id.map(str::to_string),
        ));
        Ok(())
    }

    async fn dispatch_interaction_response(
        &self,
        _thread_id: &str,
        _request_id: &str,
        _response: &Value,
    ) -> Result<(), String> {
        Ok(())
    }

    async fn dispatch_stop(&self, _thread_id: &str, _pipeline_id: &str) -> Result<(), String> {
        Ok(())
    }
}

/// 短 id 格式性质断言：12 位 hex 无连字符（uuid v4 simple 前 12 位）。
fn assert_simple_uuid_v4(s: &str) {
    assert_eq!(s.len(), 12, "短 id 应为 12 位 hex 无连字符: {s}");
    assert!(
        s.chars().all(|c| c.is_ascii_hexdigit()),
        "应全为 hex 字符: {s}"
    );
}

fn handler() -> (ChatSendHandler, Arc<RecordingDispatcher>) {
    let d = RecordingDispatcher::shared();
    (ChatSendHandler::with_store(d.clone(), None), d)
}

/// 带存储构造：与 handler() 同一记录桩，另挂真实存储
/// （出生落表/坐标解析等需消费 store 的场景）。
fn handler_with_store(
    store: Option<Arc<dyn StorageBackend>>,
) -> (ChatSendHandler, Arc<RecordingDispatcher>) {
    let d = RecordingDispatcher::shared();
    (ChatSendHandler::with_store(d.clone(), store), d)
}

fn calls(d: &RecordingDispatcher) -> Vec<DispatchRecord> {
    d.calls.lock().unwrap().clone()
}

/// 断言协议错误且未派发（校验失败不得产生副作用）。
async fn expect_protocol_error(
    h: &ChatSendHandler,
    d: &RecordingDispatcher,
    params: Value,
    why: &str,
) {
    let err = h.handle("send_message", params).await.expect_err(why);
    assert!(
        matches!(err, McpError::Protocol { .. }),
        "{why}: 应为协议错误，实际 {err:?}"
    );
    assert!(
        d.calls.lock().unwrap().is_empty(),
        "{why}: 校验失败不得派发"
    );
}

// ── 注入分支（现状不变）──────────────────────────────────────

#[tokio::test]
async fn inject_existing_pipeline_keeps_current_contract() {
    // 无 store 构造（with_store 传 None，单测/兼容路径）：注入坐标回退
    // pipeline 兼作 thread 派发键。生产注册点走 with_store + Some(store)
    // （见下方坐标解析三连测试）。
    // 两组有区分度的输入：不同 pipeline_id 均按原样派发、响应 status=dispatched
    for pid in ["pipe_existing", "pipe_trigger_42"] {
        let (h, d) = handler();
        let res = h
            .handle(
                "send_message",
                json!({"pipeline_id": pid, "message": "hi", "user_id": "u1"}),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "dispatched");
        assert_eq!(res["pipeline_id"], pid);
        let c = calls(&d);
        assert_eq!(c.len(), 1);
        assert_eq!(c[0].0, pid, "无 store 回退：thread_id 与 pipeline_id 同值");
        assert_eq!(c[0].3, pid);
        assert!(c[0].6.is_none(), "无 state 参数时 overlay 为 None");
    }
}

#[tokio::test]
async fn inject_passes_state_overlay() {
    let (h, d) = handler();
    let res = h
        .handle(
            "send_message",
            json!({
                "pipeline_id": "pipe_1",
                "message": "更新任务状态",
                "user_id": "u1",
                "state": {"task.status": "running", "task.progress": 30}
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "dispatched");
    let overlay = calls(&d)[0]
        .6
        .clone()
        .expect("state overlay 应透传到派发层");
    assert_eq!(overlay["task.status"], "running");
    assert_eq!(overlay["task.progress"], 30);
}

// ── 注入分支坐标解析 ──────────────────────────────────────
// chat_send_handler 不得把同一个 pipeline_id 复制填进 dispatch 的
// thread_id 与 pipeline_id 两个槽位——事件会发到无人订阅的频道（12hex 键
// 在 registry 只注册 thread-xxx），表现为「LLM 日志有、前端收不到回复」。
// 坐标解析封装在接口黑盒内（pipeline_sessions 反查真实 thread），
// 解析失败即协议错误。三连负样本 = A.3 验收口径。

/// 生产构造（with_store）+ 会话映射命中：派发的 thread_id 必须是解析出的
/// 真实会话坐标（`thread-` 前缀），pipeline_id 槽位保持原值，绝不互填。
#[tokio::test]
async fn inject_with_store_resolves_real_thread_coordinate() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    store
        .link_pipeline_session(
            "a1b2c3d4e5f64789abcdef0123456789",
            "thread-trig-1",
            "default",
        )
        .await
        .unwrap();
    let d = RecordingDispatcher::shared();
    let h = ChatSendHandler::with_store(d.clone(), Some(store));
    let res = h
        .handle(
            "send_message",
            json!({
                "pipeline_id": "a1b2c3d4e5f64789abcdef0123456789",
                "message": "触发提醒", "user_id": "u1"
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "dispatched");
    assert_eq!(res["pipeline_id"], "a1b2c3d4e5f64789abcdef0123456789");
    let c = calls(&d);
    assert_eq!(c.len(), 1);
    assert_eq!(
        c[0].0, "thread-trig-1",
        "thread 槽位应是黑盒解析出的真实会话坐标，不是 pipeline_id"
    );
    assert_eq!(
        c[0].3, "a1b2c3d4e5f64789abcdef0123456789",
        "pipeline 槽位保持调用方原值"
    );
}

/// 有 store 无记录：孤儿/伪造 pipeline_id → 协议错误且未派发
/// （原 bug 的静默行为变成显式拒绝——宁可报错也不把回复发到无人频道）。
#[tokio::test]
async fn inject_orphan_pipeline_id_rejected_with_protocol_error() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    let d = RecordingDispatcher::shared();
    let h = ChatSendHandler::with_store(d.clone(), Some(store));
    expect_protocol_error(
        &h,
        &d,
        json!({"pipeline_id": "ffffffffffffffffffffffffffffffff", "message": "m", "user_id": "u1"}),
        "孤儿 pipeline_id（pipeline_sessions 未命中）应拒绝",
    )
    .await;
}

/// background 注入同刀：坐标解析在派发前完成，后台线程拿到的也是真实 thread。
/// 反调注入面形态（刀2）：调用方在桥身份 scope 内（`_user_id` 回带 →
/// `agentos_tenant::scope`），user 以注入身份为准——手写 user 形参被忽略，
/// 且 spawn 闭包重建 scope 后注入身份仍可达 dispatcher。
#[tokio::test]
async fn inject_background_resolves_real_thread_coordinate() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    store
        .link_pipeline_session(
            "b1b2c3d4e5f64789abcdef0123456789",
            "thread-trig-2",
            "default",
        )
        .await
        .unwrap();
    let d = RecordingDispatcher::shared();
    let h = ChatSendHandler::with_store(d.clone(), Some(store));
    let mut scope_ctx = agentos_core::types::TenantContext::new("default", "thread-trig-2");
    scope_ctx.user_id = Some("u-injected".to_string());
    let res = agentos_tenant::scope(
        scope_ctx,
        h.handle(
            "send_message",
            json!({
                "pipeline_id": "b1b2c3d4e5f64789abcdef0123456789",
                "message": "重跑一轮", "user_id": "task_manage", "background": true
            }),
        ),
    )
    .await
    .unwrap();
    assert_eq!(res["status"], "dispatched");
    // 后台派发最终执行，且 thread 槽位 = 解析出的真实坐标
    let mut rec: Option<DispatchRecord> = None;
    for _ in 0..40 {
        if let Some(r) = calls(&d).into_iter().next() {
            rec = Some(r);
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let rec = rec.expect("background 注入应最终派发");
    assert_eq!(
        rec.0, "thread-trig-2",
        "background 分支 thread 槽位同样解析"
    );
    assert_eq!(rec.3, "b1b2c3d4e5f64789abcdef0123456789");
}

/// 1017 事故回归（刀2 预检）：无注入身份 + user 不可解析 → background
/// 派发**同步**返回协议错误（调用方拿到失败，不再假成功留后台死信）。
#[tokio::test]
async fn inject_background_unknown_user_rejected_synchronously() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    store
        .link_pipeline_session(
            "c1c2c3d4e5f64789abcdef0123456789",
            "thread-trig-3",
            "default",
        )
        .await
        .unwrap();
    let d = RecordingDispatcher::shared();
    let h = ChatSendHandler::with_store(d.clone(), Some(store));
    let err = h
        .handle(
            "send_message",
            json!({
                "pipeline_id": "c1c2c3d4e5f64789abcdef0123456789",
                "message": "重新执行任务", "user_id": "task_manage", "background": true
            }),
        )
        .await
        .expect_err("不可解析 user 必须同步拒绝（1017 continue 假成功回归）");
    assert!(
        err.to_string().contains("预检失败"),
        "错误面须指向派发预检，实际 {err}"
    );
    assert!(
        calls(&d).is_empty(),
        "预检拒绝不得有任何后台派发（消灭假成功）"
    );
}

/// 反调注入面下 user 形参可整体省略：注入身份为唯一真值（1017 后插件
/// 侧去手写 user 的契约面）。
#[tokio::test]
async fn inject_background_omitted_user_uses_injected_identity() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    store
        .link_pipeline_session(
            "d1d2c3d4e5f64789abcdef0123456789",
            "thread-trig-4",
            "default",
        )
        .await
        .unwrap();
    let d = RecordingDispatcher::shared();
    let h = ChatSendHandler::with_store(d.clone(), Some(store));
    let mut scope_ctx = agentos_core::types::TenantContext::new("default", "thread-trig-4");
    scope_ctx.user_id = Some("u-injected".to_string());
    let res = agentos_tenant::scope(
        scope_ctx,
        h.handle(
            "send_message",
            json!({
                "pipeline_id": "d1d2c3d4e5f64789abcdef0123456789",
                "message": "重跑一轮", "background": true
            }),
        ),
    )
    .await
    .unwrap();
    assert_eq!(res["status"], "dispatched");
    let mut rec: Option<DispatchRecord> = None;
    for _ in 0..40 {
        if let Some(r) = calls(&d).into_iter().next() {
            rec = Some(r);
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let rec = rec.expect("省略 user 的注入派发应最终执行");
    assert_eq!(rec.1, "u-injected", "dispatcher 收到注入身份 user");
}

// ── 创建分支：id 生成与响应 ──────────────────────────────────

#[tokio::test]
async fn create_branch_persists_birth_overlay_to_state_table() {
    // 任务归属链语义：出生字段（lineage.*/task.* 扁平键）创建即落
    // pipeline_state 表——不落库则 registry 内存丢失后（重启）冷读
    // 无基线，任务面板归属链整行缺失。未知 user 回退 default 租户。
    let store: Arc<dyn StorageBackend> =
        Arc::new(agentos_engine::SqliteStore::open_memory().expect("open_memory"));
    seed_default_user(&*store, "user_birth_persist").await;
    let (h, _d) = handler_with_store(Some(store.clone()));
    let res = h
        .handle(
            "send_message",
            json!({
                "create": true,
                "background": true,
                "message": "执行任务「调研」。",
                "user_id": "user_birth_persist",
                "state": {"task.goal": "调研", "task.status": "pending",
                          "task.parent_project_id": "proj0011",
                          "lineage.parent_pipeline_id": "pipe_parent",
                          "lineage.origin_session_id": "th_1"},
            }),
        )
        .await
        .unwrap();
    let pid = res["pipeline_id"].as_str().unwrap().to_string();
    let fields = store
        .load_pipeline_state(&pid, "default")
        .await
        .expect("出生字段应已落 pipeline_state 表");
    let get = |k: &str| fields.get(k).expect(k);
    assert_eq!(get("task.goal"), "调研");
    assert_eq!(get("task.status"), "pending");
    assert_eq!(get("task.parent_project_id"), "proj0011");
    assert_eq!(get("lineage.parent_pipeline_id"), "pipe_parent");
    assert_eq!(get("lineage.origin_session_id"), "th_1");
}

// ── 执行 agent 身份（2026-08-31：身份属管道 state，派发只管簿记）──
// 事故回归锚 75a097118e75：task_manage 催促（无 agent_id）把 general_agent
// 子任务重跑成 agentos——run 执行身份不再取派发参数，改读管道 state 持久键
// agent.id（出生方经 state 透传，agent_config_load 与内核工具面消费）。

#[tokio::test]
async fn create_branch_persists_agent_identity_from_state_overlay() {
    // 出生身份由出生方经 state 透传（agent.id 自由字段，task_birth 单点
    // 写入）→ 内核零知识透传持久化；续跑由执行面从 state 读身份。
    let store: Arc<dyn StorageBackend> =
        Arc::new(agentos_engine::SqliteStore::open_memory().expect("open_memory"));
    seed_default_user(&*store, "u_birth_agent").await;
    let (h, _d) = handler_with_store(Some(store.clone()));
    let res = h
        .handle(
            "send_message",
            json!({
                "create": true,
                "message": "执行任务「测试工具」。",
                "user_id": "u_birth_agent",
                "state": {"task.status": "pending", "agent.id": "general_agent"},
            }),
        )
        .await
        .unwrap();
    let pid = res["pipeline_id"].as_str().unwrap().to_string();
    let fields = store
        .load_pipeline_state(&pid, "default")
        .await
        .expect("出生字段应已落表");
    assert_eq!(
        fields
            .get("agent.id")
            .expect("agent.id 应随出生 state 落快照"),
        "general_agent"
    );
}

#[tokio::test]
async fn create_branch_unspecified_agent_leaves_snapshot_absent() {
    // 出生方未透传 agent.id（会话类管道）→ 快照无该键；内核不得擅自写身份。
    // 身份缺省（主 agent）由消费面自持，不靠出生写面兜底。
    let store: Arc<dyn StorageBackend> =
        Arc::new(agentos_engine::SqliteStore::open_memory().expect("open_memory"));
    seed_default_user(&*store, "u_birth_default").await;
    let (h, _d) = handler_with_store(Some(store.clone()));
    let res = h
        .handle(
            "send_message",
            json!({
                "create": true,
                "message": "你好",
                "user_id": "u_birth_default",
            }),
        )
        .await
        .unwrap();
    let pid = res["pipeline_id"].as_str().unwrap().to_string();
    let fields = store.load_pipeline_state(&pid, "default").await.unwrap();
    assert!(
        !fields.contains_key("agent.id"),
        "内核不得擅自写身份键：{:?}",
        fields.get("agent.id")
    );
}

#[tokio::test]
async fn inject_branch_agent_param_is_bookkeeping_only() {
    // 注入分支（task_manage 催促形态：无 agent_id → 缺省 agentos）：
    // 该值只进派发簿记槽位，run 身份由管道 state agent.id 决定——
    // 催促方不带身份不再改变子任务的执行 agent。
    let (h, d) = handler();
    h.handle(
        "send_message",
        json!({
            "pipeline_id": "pipe_task_1",
            "message": "继续推进任务",
            "user_id": "task_manage",
        }),
    )
    .await
    .unwrap();
    assert_eq!(calls(&d)[0].7, "agentos");

    h.handle(
        "send_message",
        json!({
            "pipeline_id": "pipe_task_2",
            "message": "执行任务。",
            "user_id": "task_system",
            "agent_id": "general_agent",
        }),
    )
    .await
    .unwrap();
    assert_eq!(calls(&d)[1].7, "general_agent", "显式值原样透传簿记");
}

// ── pipeline_config_id：显式指定管道配置（按需加载编译缓存）──────────

#[tokio::test]
async fn pipeline_config_id_param_forwarded_to_dispatch() {
    // 两组有区分度输入：显式值原样透传；不同管道互不串扰
    for (pid, cfg) in [("pipe_cfg_1", "custom_pipe"), ("pipe_cfg_2", "other_pipe")] {
        let (h, d) = handler();
        let res = h
            .handle(
                "send_message",
                json!({
                    "pipeline_id": pid,
                    "message": "m",
                    "user_id": "u1",
                    "pipeline_config_id": cfg,
                }),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "dispatched");
        assert_eq!(calls(&d)[0].8.as_deref(), Some(cfg), "显式配置应透传");
    }
}

#[tokio::test]
async fn pipeline_config_id_invalid_or_absent_is_none() {
    let (h, d) = handler();
    // 不带 = None（autonomous 缺省路径，旧调用方零变化）
    h.handle(
        "send_message",
        json!({"pipeline_id": "pipe_x", "message": "m", "user_id": "u1"}),
    )
    .await
    .unwrap();
    assert!(calls(&d)[0].8.is_none(), "缺省必须为 None");

    // 形态非法（路径穿越/空白/超长）按缺失忽略，不报错
    for (why, bad) in [
        ("路径穿越", json!("../etc")),
        ("含空白", json!("a b")),
        ("超 128", json!("x".repeat(129))),
    ] {
        let (h2, d2) = handler();
        let res = h2
            .handle(
                "send_message",
                json!({
                    "pipeline_id": "pipe_x",
                    "message": "m",
                    "user_id": "u1",
                    "pipeline_config_id": bad,
                }),
            )
            .await
            .unwrap();
        assert_eq!(res["status"], "dispatched", "{why}: 非法值不阻断派发");
        assert!(
            calls(&d2)[0].8.is_none(),
            "{why}: 非法值必须按缺失 = autonomous"
        );
    }
}

#[tokio::test]
async fn create_with_flag_generates_engine_pipeline_id() {
    let (h, d) = handler();
    let res = h
        .handle(
            "send_message",
            json!({
                "create": true,
                "message": "执行任务",
                "user_id": "u1",
                "state": {"task.goal": "喝水提醒", "task.status": "pending",
                          "lineage.parent_pipeline_id": "pipe_parent",
                          "lineage.origin_session_id": "sess_root"}
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "created");
    let pid = res["pipeline_id"].as_str().unwrap().to_string();
    assert_simple_uuid_v4(&pid);
    // 一致性：响应返回的 id 即派发所用 id（thread 与 pipeline 同值——
    // resolve 链路对陌生 id 落回退分支 ③ 原样穿透）
    let c = calls(&d);
    assert_eq!(c.len(), 1);
    assert_eq!(c[0].0, pid);
    assert_eq!(c[0].3, pid);
    // overlay：task.* 自由键 + lineage 扁平键（调用方经 state 透传）
    let overlay = c[0].6.clone().expect("state overlay 应透传");
    assert_eq!(overlay["task.goal"], "喝水提醒");
    assert_eq!(overlay["task.status"], "pending");
    assert_eq!(overlay["lineage.parent_pipeline_id"], "pipe_parent");
    assert_eq!(overlay["lineage.origin_session_id"], "sess_root");
}

#[tokio::test]
async fn create_with_ownership_thread_links_real_session() {
    // 创建分支携带归属会话（任务管道透传用户会话）：thread 槽位 = 调用方
    // 声明会话（不再自环），pipeline_sessions 落真实映射可反查；pipeline
    // 槽位保持引擎生成值。缺省（不传）行为不变 = 自环（create_with_flag 用例）。
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    seed_default_user(&*store, "u1").await;
    let (h, d) = handler_with_store(Some(store.clone()));
    let res = h
        .handle(
            "send_message",
            json!({
                "create": true,
                "message": "执行任务",
                "user_id": "u1",
                "thread_id": "thread-user-1",
                "state": {"task.goal": "g"}
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "created");
    let pid = res["pipeline_id"].as_str().unwrap().to_string();
    let c = calls(&d);
    assert_eq!(c.len(), 1);
    assert_eq!(
        c[0].0, "thread-user-1",
        "thread 槽位应是调用方归属会话，不是管道自身（自环已按可选 thread_id 修正）"
    );
    assert_eq!(c[0].3, pid, "pipeline 槽位保持引擎生成值");
    // 映射落库：pipeline_sessions 反查命中真实会话（前端 runs 快照据此归属）
    let stored = store.get_thread_id_by_pipeline(&pid).await.unwrap();
    assert_eq!(stored.as_deref(), Some("thread-user-1"));
}

#[tokio::test]
async fn create_without_flag_when_pipeline_id_absent() {
    // pipeline_id 缺省即隐式创建（"或 pipeline_id 为空"）；血缘扁平键随
    // state 透传（根形式声明由调用方插件构造，内核不解释）
    let (h, d) = handler();
    let res = h
        .handle(
            "send_message",
            json!({
                "message": "m",
                "user_id": "u1",
                "state": {
                    "lineage.root": true,
                    "lineage.origin.kind": "channel",
                    "lineage.origin.source": "dingtalk"
                }
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "created");
    assert_simple_uuid_v4(res["pipeline_id"].as_str().unwrap());
    let overlay = calls(&d)[0].6.clone().unwrap();
    assert_eq!(overlay["lineage.root"], true);
    assert_eq!(overlay["lineage.origin.kind"], "channel");
    assert_eq!(overlay["lineage.origin.source"], "dingtalk");
}

#[tokio::test]
async fn create_ids_unique_and_well_formed() {
    // 性质断言：多次创建 id 两两不同（引擎生成身份，非常量）
    let (h, _d) = handler();
    let mut ids = Vec::new();
    for _ in 0..3 {
        let res = h
            .handle(
                "send_message",
                json!({"create": true, "message": "m", "user_id": "u1"}),
            )
            .await
            .unwrap();
        ids.push(res["pipeline_id"].as_str().unwrap().to_string());
    }
    for id in &ids {
        assert_simple_uuid_v4(id);
    }
    assert_eq!(
        ids.iter().collect::<std::collections::HashSet<_>>().len(),
        3,
        "三次创建应产生三个不同 id"
    );
}

#[tokio::test]
async fn create_rejects_caller_supplied_pipeline_id() {
    // 三次定案：创建路径不接受调用方传入 id（堵 id 冒占）
    let (h, d) = handler();
    expect_protocol_error(
        &h,
        &d,
        json!({
            "create": true, "pipeline_id": "pipe_mine",
            "message": "m", "user_id": "u1"
        }),
        "create 与显式 pipeline_id 互斥",
    )
    .await;
}

// ── 保留字保护 ──────────────────────────────────────────────

#[tokio::test]
async fn state_reserved_keys_rejected() {
    let (h, d) = handler();
    for key in [
        "pipeline_id",
        "message",
        "messages",
        "agent_id",
        "session_id",
        "thread_id",
        "user_id",
        "run_id",
        "execution_context",
        "message_id",
    ] {
        let mut state = serde_json::Map::new();
        state.insert(key.to_string(), json!("evil"));
        let mut params = json!({"pipeline_id": "pipe_1", "message": "m", "user_id": "u1"});
        params["state"] = Value::Object(state);
        expect_protocol_error(&h, &d, params, &format!("保留字 {key}")).await;
    }
}

#[tokio::test]
async fn state_lineage_prefixed_keys_accepted() {
    // 血缘方案归插件自持后，lineage.* 扁平键经 state 透传放行（内核不解释）
    let (h, d) = handler();
    let res = h
        .handle(
            "send_message",
            json!({
                "pipeline_id": "pipe_1", "message": "m", "user_id": "u1",
                "state": {"lineage.root": true,
                          "lineage.parent_pipeline_id": "pipe_parent",
                          "lineage.origin.kind": "plugin"}
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "dispatched");
    let overlay = calls(&d)[0].6.clone().expect("lineage.* 应透传");
    assert_eq!(overlay["lineage.root"], true);
    assert_eq!(overlay["lineage.parent_pipeline_id"], "pipe_parent");
}

#[tokio::test]
async fn state_must_be_object() {
    let (h, d) = handler();
    expect_protocol_error(
        &h,
        &d,
        json!({"pipeline_id": "pipe_1", "message": "m", "user_id": "u1", "state": "oops"}),
        "state 非对象",
    )
    .await;
    expect_protocol_error(
        &h,
        &d,
        json!({"pipeline_id": "pipe_1", "message": "m", "user_id": "u1", "state": [1, 2]}),
        "state 数组",
    )
    .await;
}

// ── 既有必选参数与 execution_context 兼容 ───────────────────

#[tokio::test]
async fn missing_required_params_still_error() {
    let (h, d) = handler();
    // 缺 message 仍应报协议错误。user_id 契约已放宽（刀2）：反调注入面
    // 以注入身份为准可整体省略；外部面缺 user 的守卫移至派发预检
    // （见 inject_background_unknown_user_rejected_synchronously）。
    expect_protocol_error(
        &h,
        &d,
        json!({"create": true, "user_id": "u1"}),
        "缺 message",
    )
    .await;
}

#[tokio::test]
async fn create_passes_execution_context_alongside_state() {
    let (h, d) = handler();
    let ec = json!({"workspace": {"mode": "worktree"}, "isolation": {"level": "plain"}});
    let res = h
        .handle(
            "send_message",
            json!({
                "create": true, "message": "m", "user_id": "u1",
                "execution_context": ec,
                "state": {"task.id": "t9"}
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "created");
    let c = calls(&d);
    assert_eq!(
        c[0].5,
        Some(json!({"workspace": {"mode": "worktree"}, "isolation": {"level": "plain"}}))
    );
    // task.id 调用方预传值原样透传（内核零解释）
    assert_eq!(c[0].6.as_ref().unwrap()["task.id"], "t9");
}

// ── GAP-1：background 参数（任务派发不阻塞等待任务完成） ──────────

/// 带闸门的派发器：dispatch_user_input 挂起直到 gate 收到信号——用于
/// 区分 background 语义（前台 = 调用方被阻塞；后台 = 立即返回）。
struct GatedDispatcher {
    calls: Mutex<Vec<Value>>,
    gate: tokio::sync::Notify,
}

#[async_trait]
impl PipelineDispatcher for GatedDispatcher {
    async fn dispatch_user_input(
        &self,
        _thread_id: &str,
        _user_id: &str,
        _content: &str,
        _pipeline_id: &str,
        _thinking_strength: &str,
        _execution_context: Option<&Value>,
        state_overlay: Option<&Value>,
        _agent_id: &str,
        _pipeline_config_id: Option<&str>,
        _cmid: &str,
        _source: PendingInputSource,
    ) -> Result<(), String> {
        self.gate.notified().await;
        self.calls
            .lock()
            .unwrap()
            .push(state_overlay.cloned().unwrap_or(Value::Null));
        Ok(())
    }
    async fn dispatch_interaction_response(
        &self,
        _thread_id: &str,
        _request_id: &str,
        _response: &Value,
    ) -> Result<(), String> {
        Ok(())
    }
    async fn dispatch_stop(&self, _thread_id: &str, _pipeline_id: &str) -> Result<(), String> {
        Ok(())
    }
}

#[tokio::test]
async fn create_with_background_returns_before_dispatch_completes() {
    // 任务派发场景：task_submit 需要立即拿到 pipeline_id 返回给 LLM，
    // 不能阻塞等待整条任务管道跑完。派发挂起（gate 未开）时响应仍立即返回。
    let d = Arc::new(GatedDispatcher {
        calls: Mutex::new(Vec::new()),
        gate: tokio::sync::Notify::new(),
    });
    let h = ChatSendHandler::with_store(d.clone(), None);
    let res = tokio::time::timeout(
        std::time::Duration::from_millis(300),
        h.handle(
            "send_message",
            json!({
                "create": true, "message": "m", "user_id": "u1",
                "background": true,
                "state": {"task.id": "t1"}
            }),
        ),
    )
    .await
    .expect("background 创建应在派发完成前返回（300ms 闸门未开）")
    .unwrap();
    assert_eq!(res["status"], "created");
    assert_simple_uuid_v4(res["pipeline_id"].as_str().unwrap());
    // 派发仍在挂起（gate 未开）——但调用方已拿到响应
    assert!(d.calls.lock().unwrap().is_empty());
    // 放行闸门 → 后台派发完成
    d.gate.notify_one();
    let mut dispatched = false;
    for _ in 0..40 {
        if !d.calls.lock().unwrap().is_empty() {
            dispatched = true;
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert!(dispatched, "background 派发应最终执行");
    assert_eq!(d.calls.lock().unwrap()[0]["task.id"], "t1", "任务域键透传");
}

#[tokio::test]
async fn create_foreground_waits_for_dispatch_by_default() {
    // 默认（background 缺省）：维持既有语义——await 派发完成再返回
    // （触发器通知等需要投递确认的调用方）。派发挂起 → 超时打红。
    let d = Arc::new(GatedDispatcher {
        calls: Mutex::new(Vec::new()),
        gate: tokio::sync::Notify::new(),
    });
    let h = ChatSendHandler::with_store(d.clone(), None);
    let outcome = tokio::time::timeout(
        std::time::Duration::from_millis(200),
        h.handle(
            "send_message",
            json!({
                "create": true, "message": "m", "user_id": "u1",
                "lineage": {"root": true, "origin": {"kind": "system", "source": "kernel"}}
            }),
        ),
    )
    .await;
    assert!(
        outcome.is_err(),
        "前台创建应等待派发完成（闸门未开 → 超时）"
    );
    d.gate.notify_one(); // 清理：放行挂起的派发
}

#[tokio::test]
async fn unknown_method_still_rejected() {
    let (h, _d) = handler();
    let err = h.handle("bogus", json!({})).await.unwrap_err();
    assert!(matches!(err, McpError::Protocol { .. }));
}

#[tokio::test]
async fn create_passes_caller_supplied_task_id_through() {
    // 内核对任务域键零知识：task.id 原样透传；身份权威（task.id == 引擎
    // pipeline_id）由输入阶段插件 agent_config_load 落实（插件侧测试覆盖）
    let (h, d) = handler();
    let res = h
        .handle(
            "send_message",
            json!({
                "create": true, "message": "m", "user_id": "u1",
                "state": {"task.id": "fake_id_999", "task.goal": "g"}
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "created");
    assert_eq!(
        calls(&d)[0].6.as_ref().unwrap()["task.id"],
        "fake_id_999",
        "内核不解释任务域键，原样透传"
    );
}

#[tokio::test]
async fn inject_background_returns_immediately() {
    // UI resume/retry 场景：注入已有管道也支持 background（fire-and-forget）
    let d = Arc::new(GatedDispatcher {
        calls: Mutex::new(Vec::new()),
        gate: tokio::sync::Notify::new(),
    });
    let h = ChatSendHandler::with_store(d.clone(), None);
    let res = tokio::time::timeout(
        std::time::Duration::from_millis(300),
        h.handle(
            "send_message",
            json!({
                "pipeline_id": "pipe_existing", "message": "重跑一轮",
                "user_id": "u1", "background": true,
            }),
        ),
    )
    .await
    .expect("background 注入应在派发完成前返回")
    .unwrap();
    assert_eq!(res["status"], "dispatched");
    assert_eq!(res["pipeline_id"], "pipe_existing");
    assert!(d.calls.lock().unwrap().is_empty(), "派发应仍在挂起");
    d.gate.notify_one();
    let mut done = false;
    for _ in 0..40 {
        if !d.calls.lock().unwrap().is_empty() {
            done = true;
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert!(done, "后台注入应最终执行");
}

// ── no_dispatch：只写 state 不派发（容器任务等"只登记不执行"的声明写入）──

/// no_dispatch 注入：state overlay 并入目标管道 state（registry 热路径），
/// 不派发执行、不产生消息；响应 status=recorded。
#[tokio::test]
async fn no_dispatch_writes_state_without_dispatch() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    seed_default_user(&*store, "u1").await;
    // 注入分支坐标解析：目标管道须已落 pipeline_sessions 映射（真实会话）
    store
        .link_pipeline_session(
            "a1b2c3d4e5f64789abcdef0123456789",
            "thread-owner-1",
            "default",
        )
        .await
        .unwrap();
    let (h, d) = handler_with_store(Some(store.clone()));
    let res = h
        .handle(
            "send_message",
            json!({
                "pipeline_id": "a1b2c3d4e5f64789abcdef0123456789",
                "message": "登记容器任务",
                "user_id": "u1",
                "no_dispatch": true,
                "state": {
                    "task.owned.c1d2e3f4a5b64789abcdef0123456789.title": "容器项目",
                    "task.owned.c1d2e3f4a5b64789abcdef0123456789.status": "active",
                    "task.owned.c1d2e3f4a5b64789abcdef0123456789.submitted_by": "u1",
                },
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "recorded");
    assert_eq!(res["pipeline_id"], "a1b2c3d4e5f64789abcdef0123456789");
    // 未派发：dispatcher 零调用
    assert!(d.calls.lock().unwrap().is_empty(), "no_dispatch 不得派发");
    // registry 热路径：state 已并入（get_or_init 冷注册后读回）
    let registry = agentos_session::pipeline_state_registry::global_registry();
    let entry = registry
        .get("default", "a1b2c3d4e5f64789abcdef0123456789")
        .expect("no_dispatch 应注册/更新 registry 条目");
    let st = entry.read();
    assert_eq!(
        st.state["task.owned.c1d2e3f4a5b64789abcdef0123456789.title"],
        "容器项目"
    );
    assert_eq!(
        st.state["task.owned.c1d2e3f4a5b64789abcdef0123456789.submitted_by"],
        "u1"
    );
    // 冷兜底：pipeline_state 表已持久化（重启后冷恢复读它）
    let fields = store
        .load_pipeline_state("a1b2c3d4e5f64789abcdef0123456789", "default")
        .unwrap();
    assert_eq!(
        fields["task.owned.c1d2e3f4a5b64789abcdef0123456789.title"],
        "容器项目"
    );
}

/// no_dispatch 与 background 互斥（no_dispatch 不派发，background 是派发语义）。
#[tokio::test]
async fn no_dispatch_conflicts_with_background() {
    let (h, d) = handler();
    expect_protocol_error(
        &h,
        &d,
        json!({
            "pipeline_id": "pipe_1", "message": "m", "user_id": "u1",
            "no_dispatch": true, "background": true,
            "state": {"task.owned.x.title": "t"}
        }),
        "no_dispatch 与 background 互斥",
    )
    .await;
}

/// no_dispatch 缺 state：无内容可写，协议错误。
#[tokio::test]
async fn no_dispatch_requires_state() {
    let (h, d) = handler();
    expect_protocol_error(
        &h,
        &d,
        json!({"pipeline_id": "pipe_1", "message": "m", "user_id": "u1", "no_dispatch": true}),
        "no_dispatch 必须携带 state",
    )
    .await;
}

// ── 统一错误模型：background 派发失败补报 system_notification（P1a）──

/// 恒失败派发器（模拟任务管道未启动）。
struct FailingDispatcher {
    err: String,
}

#[async_trait]
impl PipelineDispatcher for FailingDispatcher {
    async fn dispatch_user_input(
        &self,
        _t: &str,
        _u: &str,
        _c: &str,
        _p: &str,
        _ts: &str,
        _ec: Option<&Value>,
        _ov: Option<&Value>,
        _a: &str,
        _pipeline_config_id: Option<&str>,
        _cmid: &str,
        _source: PendingInputSource,
    ) -> Result<(), String> {
        Err(self.err.clone())
    }
    async fn dispatch_interaction_response(
        &self,
        _t: &str,
        _r: &str,
        _resp: &Value,
    ) -> Result<(), String> {
        Ok(())
    }
    async fn dispatch_stop(&self, _t: &str, _p: &str) -> Result<(), String> {
        Ok(())
    }
}

/// 捕获型 EventSink：记录收到的全部文本帧。
struct CapturingSink {
    frames: Arc<Mutex<Vec<String>>>,
}

#[async_trait]
impl agentos_session::EventSink for CapturingSink {
    async fn send_text(&self, text: &str) -> bool {
        self.frames.lock().unwrap().push(text.to_string());
        true
    }
    fn id(&self) -> u64 {
        42
    }
}

/// background 派发失败：调用方仍拿 dispatched（响应后失败无法携带），
/// 但经 WS system_notification 补报——前端实时可见"任务未启动"
/// （统一错误模型：假成功显式化）。
#[tokio::test]
async fn background_dispatch_failure_emits_system_notification() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    seed_default_user(&*store, "u1").await;
    let session = Arc::new(agentos_session::SessionCoordinator::new());
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    session.register_thread("thread-fail-1", "u1");
    session.register(
        "u1",
        Arc::new(CapturingSink {
            frames: frames.clone(),
        }),
    );

    let dispatcher: Arc<dyn PipelineDispatcher> = Arc::new(FailingDispatcher {
        err: "pipeline init failed: engine not available".into(),
    });
    let h = ChatSendHandler::with_session(dispatcher, Some(store), session);

    let res = h
        .handle(
            "send_message",
            json!({"create": true, "message": "m", "user_id": "u1",
                   "thread_id": "thread-fail-1",
                   "background": true,
                   "lineage": {"root": true, "origin": {"kind": "system", "source": "gate"}}}),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "created", "后台派发响应立即返回: {res}");
    // 等 spawn 的派发失败补报完成（真实异步：轮询帧到达）
    let frames_arc = frames.clone();
    tokio::time::timeout(std::time::Duration::from_secs(5), async {
        loop {
            if !frames_arc.lock().unwrap().is_empty() {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("5s 内应收到 system_notification 帧");

    let frames = frames.lock().unwrap();
    let last = frames.last().unwrap();
    let frame: Value = serde_json::from_str(last).unwrap();
    assert_eq!(frame["type"], "system_notification");
    let data = &frame["data"];
    assert_eq!(data["level"], "error");
    assert_eq!(data["notificationType"], "dispatch_failed");
    assert!(
        data["content"].as_str().unwrap().contains("管道未启动"),
        "content 应含失败原因: {data}"
    );
}

/// 成功派发不产生 system_notification（补报只对失败路径）。
#[tokio::test]
async fn background_dispatch_success_no_notification() {
    let session = Arc::new(agentos_session::SessionCoordinator::new());
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    session.register_thread("thread-ok-1", "u1");
    session.register(
        "u1",
        Arc::new(CapturingSink {
            frames: frames.clone(),
        }),
    );

    let d = RecordingDispatcher::shared();
    let dispatcher: Arc<dyn PipelineDispatcher> = d.clone();
    let h = ChatSendHandler::with_session(dispatcher, None, session);

    let res = h
        .handle(
            "send_message",
            json!({"create": true, "message": "m", "user_id": "u1",
                   "thread_id": "thread-ok-1",
                   "background": true,
                   "lineage": {"root": true, "origin": {"kind": "system", "source": "gate"}}}),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "created");
    // 给 spawn 留出执行窗口（成功路径无帧）
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    assert!(
        frames.lock().unwrap().is_empty(),
        "成功派发不应产生通知: {:?}",
        frames.lock().unwrap()
    );
}

/// 播种测试用户（tenant=default，对齐 admin 种子形态）：K8 修订
/// （ADR 2026-10-10）后租户解析按真值走、无降级，真实 SqliteStore
/// 场景的测试用户必须先在库。
async fn seed_default_user(store: &dyn StorageBackend, user_id: &str) {
    store
        .create_user(&agentos_core::types::UserRecord {
            user_id: user_id.to_string(),
            username: format!("u-{user_id}"),
            password: String::new(),
            email: None,
            role: "user".to_string(),
            tenant_id: "default".to_string(),
            created_at: "2026-10-10T00:00:00Z".to_string(),
            last_login_at: None,
            must_change_password: false,
        })
        .await
        .unwrap();
}

// ── B7：出生字段落库失败整体上抛（先于引擎执行，失败返回即安全终止）──

/// 出生字段批量 upsert 恒故障的存储 mock：link_pipeline_session 正常
///（warn 面）、用户解析面正常回 None（回退 default 租户）、批量出生字段
/// 恒失败。其余方法 unreachable 桩（意外耦合即炸出）。
struct FailingBirthStore;

#[async_trait]
impl StorageBackend for FailingBirthStore {
    async fn upsert_state_fields(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _fields: &serde_json::Map<String, serde_json::Value>,
    ) -> Result<(), agentos_core::types::StorageError> {
        Err(agentos_core::types::StorageError::Database(
            "injected birth field failure".to_string(),
        ))
    }
    async fn link_pipeline_session(
        &self,
        _pipeline_id: &str,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        Ok(())
    }
    async fn get_user_by_id(
        &self,
        _user_id: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        Ok(None)
    }
    async fn get_run(
        &self,
        _run_id: &str,
    ) -> Result<agentos_core::types::RunRecord, agentos_core::types::StorageError> {
        unreachable!("出生字段失败路径不应触碰其他存储方法")
    }
    async fn get_messages_by_pipeline(
        &self,
        _pipeline_id: &str,
        _opts: agentos_core::traits::MessageQueryOpts,
    ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError> {
        unreachable!("出生字段失败路径不应触碰其他存储方法")
    }
    async fn get_blob(&self, _blob_id: &str) -> Result<Vec<u8>, agentos_core::types::StorageError> {
        unreachable!("出生字段失败路径不应触碰其他存储方法")
    }
    async fn append_trace(
        &self,
        _entry: agentos_core::types::TraceEntry,
    ) -> Result<(), agentos_core::types::StorageError> {
        unreachable!("出生字段失败路径不应触碰其他存储方法")
    }
    async fn store_blob(
        &self,
        _data: &[u8],
        _mime_type: &str,
    ) -> Result<String, agentos_core::types::StorageError> {
        unreachable!("出生字段失败路径不应触碰其他存储方法")
    }
    async fn create_session(
        &self,
        _session: &agentos_core::types::SessionRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        unreachable!("出生字段失败路径不应触碰其他存储方法")
    }
    async fn get_session(
        &self,
        _thread_id: &str,
    ) -> Result<Option<agentos_core::types::SessionRecord>, agentos_core::types::StorageError> {
        unreachable!("出生字段失败路径不应触碰其他存储方法")
    }
    async fn list_sessions(
        &self,
        _filter: agentos_core::traits::SessionListFilter,
    ) -> Result<Vec<agentos_core::types::SessionRecord>, agentos_core::types::StorageError> {
        unreachable!("出生字段失败路径不应触碰其他存储方法")
    }
    async fn update_session(
        &self,
        _session: &agentos_core::types::SessionRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        unreachable!("出生字段失败路径不应触碰其他存储方法")
    }
    async fn delete_session(
        &self,
        _thread_id: &str,
    ) -> Result<Vec<String>, agentos_core::types::StorageError> {
        unreachable!("出生字段失败路径不应触碰其他存储方法")
    }
    async fn list_pipeline_ids_by_thread(
        &self,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<String>, agentos_core::types::StorageError> {
        unreachable!("出生字段失败路径不应触碰其他存储方法")
    }
    async fn get_step_traces_by_thread(
        &self,
        _thread_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError> {
        unreachable!("出生字段失败路径不应触碰其他存储方法")
    }
    async fn get_step_traces_by_pipeline(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError> {
        unreachable!("出生字段失败路径不应触碰其他存储方法")
    }
    async fn create_user(
        &self,
        _user: &agentos_core::types::UserRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        unreachable!("出生字段失败路径不应触碰其他存储方法")
    }
    async fn get_user_by_username(
        &self,
        _username: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        unreachable!("出生字段失败路径不应触碰其他存储方法")
    }
    async fn list_users(
        &self,
    ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        unreachable!("出生字段失败路径不应触碰其他存储方法")
    }
    async fn update_last_login(
        &self,
        _user_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        unreachable!("出生字段失败路径不应触碰其他存储方法")
    }
    async fn update_user_password(
        &self,
        _user_id: &str,
        _password_hash: &str,
        _must_change_password: bool,
    ) -> Result<bool, agentos_core::types::StorageError> {
        unreachable!("mock 不提供口令更新")
    }
    async fn delete_user(&self, _user_id: &str) -> Result<bool, agentos_core::types::StorageError> {
        unreachable!("出生字段失败路径不应触碰其他存储方法")
    }
}

#[tokio::test]
async fn create_branch_birth_field_failure_aborts_before_dispatch() {
    // B7 契约（对齐 task_birth「禁止部分成功」）：出生字段落库失败 →
    // 整体返回能力调用错误，且引擎未启动（派发零调用）——本段先于引擎
    // 执行，失败返回即安全终止，冷读不再丢出生基线。
    let store: Arc<dyn StorageBackend> = Arc::new(FailingBirthStore);
    let (h, d) = handler_with_store(Some(store));
    let err = h
        .handle(
            "send_message",
            json!({
                "create": true,
                "message": "执行任务「调研」。",
                "user_id": "user_birth_fail",
                "state": {"task.goal": "调研", "task.status": "pending"},
            }),
        )
        .await
        .expect_err("出生字段落库失败必须整体报错");
    assert!(
        matches!(err, McpError::Protocol { .. }),
        "应为能力调用协议错误，实际 {err:?}"
    );
    assert!(
        d.calls.lock().unwrap().is_empty(),
        "出生字段失败不得进入引擎执行（派发零调用）"
    );
}

#[tokio::test]
async fn create_branch_birth_success_still_dispatches_foreground() {
    // 对照组（第二组输入）：真实 store + 前台创建 → 出生字段落表且正常派发
    // （批量接口替换后成功路径行为不变）。
    let store: Arc<dyn StorageBackend> =
        Arc::new(agentos_engine::SqliteStore::open_memory().expect("open_memory"));
    seed_default_user(&*store, "user_birth_ok").await;
    let (h, d) = handler_with_store(Some(store.clone()));
    let res = h
        .handle(
            "send_message",
            json!({
                "create": true,
                "message": "前台任务",
                "user_id": "user_birth_ok",
                "state": {"task.status": "pending", "lineage.parent_pipeline_id": "pipe_p"},
            }),
        )
        .await
        .expect("出生字段成功应正常创建派发");
    assert_eq!(res["status"], "created");
    let pid = res["pipeline_id"].as_str().unwrap().to_string();
    let fields = store
        .load_pipeline_state(&pid, "default")
        .await
        .expect("出生字段应已落表");
    assert_eq!(
        fields.get("task.status"),
        Some(&serde_json::json!("pending"))
    );
    assert_eq!(
        fields.get("lineage.parent_pipeline_id"),
        Some(&serde_json::json!("pipe_p"))
    );
    assert_eq!(calls(&d).len(), 1, "成功路径前台派发恰好一次");
}
// ── 覆盖率补测批：no_dispatch 热路径/留痕 + background 失败面 + 出生落库 ──

/// no_dispatch 命中 registry 已有条目（热路径）→ 键并入既有 state，
/// 而非以 overlay 为基底重建（避免顶掉运行期累积的其它键）。
#[tokio::test]
async fn no_dispatch_merges_into_existing_registry_entry() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    seed_default_user(&*store, "u1").await;
    let pid = format!("pipe_merge_{}", uuid::Uuid::new_v4().simple());
    store
        .link_pipeline_session(&pid, "thread-merge-1", "default")
        .await
        .unwrap();
    // 预置热条目：已有运行期累积键（overlay 里没有）
    let registry = agentos_session::pipeline_state_registry::global_registry();
    registry.get_or_init(
        "default",
        &pid,
        "thread-merge-1",
        "agentos",
        json!({"task.status": "running", "turn": 7}),
    );

    let (h, d) = handler_with_store(Some(store.clone()));
    let res = h
        .handle(
            "send_message",
            json!({
                "pipeline_id": pid,
                "message": "更新登记态",
                "user_id": "u1",
                "no_dispatch": true,
                "state": {"task.result.summary": "阶段完成"},
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "recorded");
    assert!(d.calls.lock().unwrap().is_empty(), "no_dispatch 不得派发");

    let entry = registry.get("default", &pid).expect("条目应在");
    let st = entry.read();
    assert_eq!(
        st.state["task.result.summary"], "阶段完成",
        "overlay 键写入既有条目"
    );
    assert_eq!(
        st.state["task.status"], "running",
        "既有键保留（合并非替换）"
    );
    assert_eq!(st.state["turn"], 7, "运行期累积键不得被顶掉");
    registry.remove("default", &pid);
}

/// 只让 `upsert_state_field` 失败、其余转发真实库的探针 store。
struct UpsertFailStore {
    inner: Arc<agentos_engine::SqliteStore>,
}

#[async_trait]
impl StorageBackend for UpsertFailStore {
    async fn upsert_state_field(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _key: &str,
        _value: &Value,
    ) -> Result<(), agentos_core::types::StorageError> {
        Err(agentos_core::types::StorageError::Database(
            "injected upsert failure".to_string(),
        ))
    }
    async fn get_run(
        &self,
        run_id: &str,
    ) -> Result<agentos_core::types::RunRecord, agentos_core::types::StorageError> {
        self.inner.get_run(run_id).await
    }
    async fn get_messages_by_pipeline(
        &self,
        pipeline_id: &str,
        opts: agentos_core::traits::MessageQueryOpts,
    ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError> {
        self.inner.get_messages_by_pipeline(pipeline_id, opts).await
    }
    async fn get_blob(&self, blob_id: &str) -> Result<Vec<u8>, agentos_core::types::StorageError> {
        self.inner.get_blob(blob_id).await
    }
    async fn append_trace(
        &self,
        entry: agentos_core::types::TraceEntry,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.inner.append_trace(entry).await
    }
    async fn record_run_start(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        run_id: &str,
        config_hash: &str,
        started_at: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.inner
            .record_run_start(pipeline_id, tenant_id, run_id, config_hash, started_at)
    }
    async fn store_blob(
        &self,
        data: &[u8],
        mime_type: &str,
    ) -> Result<String, agentos_core::types::StorageError> {
        agentos_core::traits::StorageBackend::store_blob(self.inner.as_ref(), data, mime_type).await
    }
    async fn create_session(
        &self,
        session: &agentos_core::types::SessionRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.inner.create_session(session).await
    }
    async fn get_session(
        &self,
        thread_id: &str,
    ) -> Result<Option<agentos_core::types::SessionRecord>, agentos_core::types::StorageError> {
        self.inner.get_session(thread_id).await
    }
    async fn list_sessions(
        &self,
        filter: agentos_core::traits::SessionListFilter,
    ) -> Result<Vec<agentos_core::types::SessionRecord>, agentos_core::types::StorageError> {
        self.inner.list_sessions(filter).await
    }
    async fn update_session(
        &self,
        session: &agentos_core::types::SessionRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.inner.update_session(session).await
    }
    async fn delete_session(
        &self,
        thread_id: &str,
    ) -> Result<Vec<String>, agentos_core::types::StorageError> {
        self.inner.delete_session(thread_id).await
    }
    async fn link_pipeline_session(
        &self,
        pipeline_id: &str,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.inner
            .link_pipeline_session(pipeline_id, thread_id, tenant_id)
            .await
    }
    async fn list_pipeline_ids_by_thread(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<String>, agentos_core::types::StorageError> {
        self.inner
            .list_pipeline_ids_by_thread(thread_id, tenant_id)
            .await
    }
    async fn get_thread_id_by_pipeline(
        &self,
        pipeline_id: &str,
    ) -> Result<Option<String>, agentos_core::types::StorageError> {
        self.inner.get_thread_id_by_pipeline(pipeline_id).await
    }
    async fn get_step_traces_by_thread(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError> {
        self.inner
            .get_step_traces_by_thread(thread_id, tenant_id)
            .await
    }
    async fn create_user(
        &self,
        user: &agentos_core::types::UserRecord,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.inner.create_user(user).await
    }
    async fn get_user_by_id(
        &self,
        user_id: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        self.inner.get_user_by_id(user_id).await
    }
    async fn get_user_by_username(
        &self,
        username: &str,
    ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        self.inner.get_user_by_username(username).await
    }
    async fn list_users(
        &self,
    ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError> {
        self.inner.list_users().await
    }
    async fn update_last_login(
        &self,
        user_id: &str,
    ) -> Result<(), agentos_core::types::StorageError> {
        self.inner.update_last_login(user_id).await
    }
    async fn update_user_password(
        &self,
        user_id: &str,
        password_hash: &str,
        must_change_password: bool,
    ) -> Result<bool, agentos_core::types::StorageError> {
        self.inner
            .update_user_password(user_id, password_hash, must_change_password)
            .await
    }
    async fn delete_user(&self, user_id: &str) -> Result<bool, agentos_core::types::StorageError> {
        self.inner.delete_user(user_id).await
    }
}

/// no_dispatch 的 state 持久化失败 = 登记失败（fail-closed）：真值落库
/// 失败不得回 recorded——只回内存会让「已登记」的声明在重启后蒸发，且
/// registry 缓存不得领先真值。
#[tokio::test]
async fn no_dispatch_state_persist_failure_fails_request() {
    let (guard, logs) = crate::test_env::capture_logs();
    let _ = &guard;
    let inner = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    seed_default_user(&*inner, "u1").await;
    let pid = format!("pipe_nd_fail_{}", uuid::Uuid::new_v4().simple());
    inner
        .link_pipeline_session(&pid, "thread-nd-fail", "default")
        .await
        .unwrap();
    let store: Arc<dyn StorageBackend> = Arc::new(UpsertFailStore {
        inner: inner.clone(),
    });

    let d = RecordingDispatcher::shared();
    let h = ChatSendHandler::with_store(d.clone(), Some(store));
    let res = h
        .handle(
            "send_message",
            json!({
                "pipeline_id": pid,
                "message": "登记容器任务",
                "user_id": "u1",
                "no_dispatch": true,
                "state": {"task.owned.nodispatch_fail.title": "容器项目"},
            }),
        )
        .await;
    let text = logs.text();

    match res {
        Ok(v) => panic!("持久化失败不得伪造 recorded: {v}"),
        Err(e) => assert!(
            e.to_string().contains("no_dispatch state 持久化失败"),
            "错误应标明持久化失败（可重试）: {e}"
        ),
    }
    assert!(d.calls.lock().unwrap().is_empty(), "失败路径不得派发");
    let registry = agentos_session::pipeline_state_registry::global_registry();
    assert!(
        registry.get("default", &pid).is_none(),
        "真值落库失败不得写 registry 缓存（缓存不得领先真值）"
    );
    let fields = inner.load_pipeline_state(&pid, "default").unwrap();
    assert!(
        !fields.contains_key("task.owned.nodispatch_fail.title"),
        "失败键不得半落库: {fields:?}"
    );
    assert!(
        text.contains("no_dispatch state 持久化失败"),
        "持久化失败必须 error 留痕: {text}"
    );
}

/// 创建分支出生字段落库失败 → 显式终止本次发送（不得静默丢出生字段：
/// 任务血缘/归属依赖它，缺了后续轮次读不到）。
#[tokio::test]
async fn create_birth_persist_failure_terminates_send() {
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    seed_default_user(&*sqlite, "u1").await;
    sqlite
        .with_conn::<(), String>(|conn| {
            conn.execute("DROP TABLE pipeline_state", [])
                .map(|_| ())
                .map_err(|e| e.to_string())
        })
        .unwrap();
    let store: Arc<dyn StorageBackend> = sqlite;
    let d = RecordingDispatcher::shared();
    let h = ChatSendHandler::with_store(d.clone(), Some(store));
    let err = h
        .handle(
            "send_message",
            json!({
                "create": true,
                "message": "登记容器任务",
                "user_id": "u1",
                "state": {"task.owned.birth_fail.title": "容器项目"},
            }),
        )
        .await
        .expect_err("出生字段落库失败必须终止本次发送");
    assert!(
        format!("{err}").contains("出生字段落库失败"),
        "错误须指明失败环节: {err}"
    );
    assert!(d.calls.lock().unwrap().is_empty(), "终止即不得派发");
}

/// background 派发失败但 session 未接线：只 error 留痕，不 panic、无补报。
#[tokio::test]
async fn background_dispatch_failure_without_session_only_logs() {
    let (guard, logs) = crate::test_env::capture_logs();
    let _ = &guard;
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    seed_default_user(&*store, "u1").await;
    let dispatcher: Arc<dyn PipelineDispatcher> = Arc::new(FailingDispatcher {
        err: "engine not available".into(),
    });
    let h = ChatSendHandler::with_store(dispatcher, Some(store));
    let res = h
        .handle(
            "send_message",
            json!({"create": true, "message": "后台任务", "user_id": "u1",
                   "background": true}),
        )
        .await
        .unwrap();
    assert_eq!(
        res["status"], "created",
        "响应后失败无法携带，仍返回 created"
    );
    // 后台派发是 spawn：有界等待留痕落地（真实时钟，10ms 步长）
    let mut waited = 0;
    while !logs.text().contains("后台派发失败（任务管道未启动）") && waited < 200 {
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        waited += 1;
    }
    let text = logs.text();
    assert!(
        text.contains("chat.send_message 后台派发失败（任务管道未启动）"),
        "后台派发失败必须 error 留痕: {text}"
    );
}

/// state overlay 含空串键 → 协议错误（空键会污染点号键空间且无法表达）。
#[tokio::test]
async fn state_empty_string_key_rejected() {
    let (h, d) = handler();
    expect_protocol_error(
        &h,
        &d,
        json!({
            "create": true, "message": "m", "user_id": "u1",
            "state": {"": "x"},
        }),
        "state 键不得为空串",
    )
    .await;
    // 第二组（有区分度输入）：空键与合法键混排也必须整体拒绝（不得部分写入）
    expect_protocol_error(
        &h,
        &d,
        json!({
            "create": true, "message": "m", "user_id": "u1",
            "state": {"task.goal": "正常目标", "": "空"},
        }),
        "混排空键同样拒绝",
    )
    .await;
}

// ── BUG-45 断点2：归属锚点创建同步切线程活跃管道 ──────────────
//
// 消息查询端点回退链读 sessions.active_pipeline_id；归属锚点创建只落
// pipeline_sessions 映射不切指针时，线程视角恒停旧聊天管道——任务消息
// （如 roleplay 开场白）对用户不可见。语义：创建即切、失败保持（不切回
// ——失败任务的消息留在同管道继续可见；不为切回加 run 终态钩子）；
// 只切显式声明归属锚点（thread_id）的创建，无锚点管道保持独立。

/// sessions 行测试夹具：thread + 活跃管道指针。
async fn seed_session(store: &std::sync::Arc<dyn StorageBackend>, thread: &str, active: &str) {
    store
        .create_session(&agentos_core::types::SessionRecord {
            thread_id: thread.to_string(),
            title: None,
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: Some(active.to_string()),
            pipeline_ids: vec![active.to_string()],
            metadata: None,
            created_at: "2026-09-18T00:00:00Z".to_string(),
            updated_at: "2026-09-18T00:00:00Z".to_string(),
            last_active_at: None,
        })
        .await
        .unwrap();
}

/// 归属锚点创建：线程的 active_pipeline_id 必须切到新任务管道。
#[tokio::test]
async fn create_with_ownership_thread_switches_session_active_pipeline() {
    let store: Arc<dyn StorageBackend> =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    seed_default_user(&*store, "u1").await;
    seed_session(&store, "thread-anchor-1", "p_old_chat").await;
    let (h, _d) = handler_with_store(Some(store.clone()));
    let res = h
        .handle(
            "send_message",
            json!({
                "create": true, "message": "登记任务", "user_id": "u1",
                "thread_id": "thread-anchor-1",
                "state": {"task.goal": "g"}
            }),
        )
        .await
        .unwrap();
    let pid = res["pipeline_id"].as_str().unwrap().to_string();
    let sess = store.get_session("thread-anchor-1").await.unwrap().unwrap();
    assert_eq!(
        sess.active_pipeline_id.as_deref(),
        Some(pid.as_str()),
        "归属锚点创建必须把线程活跃管道切到新管道（对话在聊天区继续的前提）"
    );
}

/// 切换只作用于声明的锚点线程：其他线程指针不受牵连（两组有区分度输入）。
#[tokio::test]
async fn create_pointer_switch_is_scoped_to_anchor_thread() {
    let store: Arc<dyn StorageBackend> =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    seed_default_user(&*store, "u1").await;
    seed_session(&store, "thread-anchor-a", "p_chat_a").await;
    seed_session(&store, "thread-bystander-b", "p_chat_b").await;
    let (h, _d) = handler_with_store(Some(store.clone()));
    let res = h
        .handle(
            "send_message",
            json!({
                "create": true, "message": "登记任务", "user_id": "u1",
                "thread_id": "thread-anchor-a",
                "state": {"task.goal": "g"}
            }),
        )
        .await
        .unwrap();
    let pid = res["pipeline_id"].as_str().unwrap();
    let anchored = store.get_session("thread-anchor-a").await.unwrap().unwrap();
    assert_eq!(anchored.active_pipeline_id.as_deref(), Some(pid));
    let bystander = store
        .get_session("thread-bystander-b")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        bystander.active_pipeline_id.as_deref(),
        Some("p_chat_b"),
        "未锚定线程的活跃管道不得被牵连切换"
    );
}

/// 无锚点创建（后台自主任务）：任何 sessions 行都不得被改——
/// 独立任务不进用户会话视图。
#[tokio::test]
async fn create_without_ownership_thread_leaves_sessions_untouched() {
    let store: Arc<dyn StorageBackend> =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    seed_default_user(&*store, "u1").await;
    seed_session(&store, "thread-user-keep", "p_main").await;
    let (h, _d) = handler_with_store(Some(store.clone()));
    let res = h
        .handle(
            "send_message",
            json!({
                "create": true, "message": "自主任务", "user_id": "u1",
                "state": {"task.goal": "g", "lineage.root": true}
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "created");
    let sess = store
        .get_session("thread-user-keep")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        sess.active_pipeline_id.as_deref(),
        Some("p_main"),
        "无锚点创建不得切任何线程指针"
    );
}

/// 锚点线程无 sessions 行（陈旧锚/伪造）：跳过切换不阻断出生——
/// 任务执行不依赖该指针，不得凭空造会话行，也不得让出生失败。
#[tokio::test]
async fn create_with_unknown_anchor_thread_skips_pointer_switch() {
    let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    seed_default_user(&*store, "u1").await;
    let (h, _d) = handler_with_store(Some(store.clone()));
    let res = h
        .handle(
            "send_message",
            json!({
                "create": true, "message": "登记任务", "user_id": "u1",
                "thread_id": "thread-ghost-1",
                "state": {"task.goal": "g"}
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "created", "指针切换跳过不得阻断出生");
    assert!(
        store.get_session("thread-ghost-1").await.unwrap().is_none(),
        "不得凭空创建锚点线程的 sessions 行"
    );
}

/// 锚点线程会话读写失败（存储故障）：warn 留痕不阻断出生——指针是视图
/// 便利面，pipeline_sessions 映射与出生键才是 B7 契约面。
#[tokio::test]
async fn create_pointer_switch_storage_failure_does_not_abort_birth() {
    let (guard, logs) = crate::test_env::capture_logs();
    let _ = &guard;
    let sqlite = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    seed_default_user(&*sqlite, "u1").await;
    sqlite
        .with_conn::<(), String>(|conn| {
            conn.execute("DROP TABLE sessions", [])
                .map(|_| ())
                .map_err(|e| e.to_string())
        })
        .unwrap();
    let store: Arc<dyn StorageBackend> = sqlite;
    let d = RecordingDispatcher::shared();
    let h = ChatSendHandler::with_store(d.clone(), Some(store));
    let res = h
        .handle(
            "send_message",
            json!({
                "create": true, "message": "登记任务", "user_id": "u1",
                "thread_id": "thread-fail-sw",
                "state": {"task.goal": "g"}
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "created", "指针切换失败不得阻断出生");
    assert!(
        logs.text().contains("切线程活跃管道失败"),
        "指针切换失败必须 warn 留痕"
    );
}

/// 无 store 构造（单测/兼容路径）+ 归属锚点：无持久化面可切，安静返回，
/// 出生照常（响应 created）。
#[tokio::test]
async fn create_with_anchor_and_no_store_still_creates() {
    let (h, d) = handler(); // with_store(None)
    let res = h
        .handle(
            "send_message",
            json!({
                "create": true, "message": "登记任务", "user_id": "u1",
                "thread_id": "thread-nostore-1",
                "state": {"task.goal": "g"}
            }),
        )
        .await
        .unwrap();
    assert_eq!(res["status"], "created", "无 store 不得阻断出生");
    assert_eq!(calls(&d).len(), 1, "派发照常");
}

/// 语义钉子（创建即切、失败保持）：后台派发失败后指针保持在新任务管道——
/// 失败消息留在同管道对用户可见（错误可见性），不切回旧聊天管道。
#[tokio::test]
async fn pointer_stays_switched_when_background_dispatch_fails() {
    let store: Arc<dyn StorageBackend> =
        Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
    seed_default_user(&*store, "u1").await;
    seed_session(&store, "thread-fail-keep", "p_old_chat").await;
    let session = Arc::new(agentos_session::SessionCoordinator::new());
    let frames = Arc::new(Mutex::new(Vec::<String>::new()));
    session.register_thread("thread-fail-keep", "u1");
    session.register(
        "u1",
        Arc::new(CapturingSink {
            frames: frames.clone(),
        }),
    );
    let dispatcher: Arc<dyn PipelineDispatcher> = Arc::new(FailingDispatcher {
        err: "pipeline init failed".into(),
    });
    let h = ChatSendHandler::with_session(dispatcher, Some(store.clone()), session);
    let res = h
        .handle(
            "send_message",
            json!({
                "create": true, "message": "m", "user_id": "u1",
                "thread_id": "thread-fail-keep", "background": true,
                "state": {"task.goal": "g"}
            }),
        )
        .await
        .unwrap();
    let pid = res["pipeline_id"].as_str().unwrap().to_string();
    // 等后台派发失败补报完成（真实异步：轮询帧到达）
    let frames_arc = frames.clone();
    tokio::time::timeout(std::time::Duration::from_secs(5), async {
        loop {
            if !frames_arc.lock().unwrap().is_empty() {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("5s 内应收到 dispatch_failed 补报帧");
    let sess = store
        .get_session("thread-fail-keep")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        sess.active_pipeline_id.as_deref(),
        Some(pid.as_str()),
        "创建即切、失败保持：派发失败不得把指针切回旧聊天管道"
    );
}

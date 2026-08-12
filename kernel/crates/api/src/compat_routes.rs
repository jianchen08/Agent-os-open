//! 前端兼容端点（消除 0.2 内核相对 0.1 前端的 404）
//!
//! // COMPAT: temporary, migrate to /ext/{plugin_id} per ADR §3.3.
//! // 本文件是 0.1→0.2 迁移期的兼容垫片，**禁止新增业务端点**。
//! // 迁移进度见 docs/working/adr_plugin_capability_gap_and_plan.md 阶段 B。
//! // 已迁移：cost-control → plugins/shared/system/cost_control（/ext/cost_control/**）
//! // 已迁移：monitoring → plugins/shared/system/monitoring（/ext/monitoring/**）
//! // 已迁移：evaluation-metrics → plugins/shared/system/evaluation（/ext/evaluation_service/metrics）
//! // 已迁移：themes → 纯前端（Vite import.meta.glob，内核不参与）
//! // 已删死端点：evaluation 的 9 个无后端无消费常量（evaluate/profiles/reports/statistics/trends）
//! // 保留为内核职责（非迁移项，按边界原则归属内核）：
//! //   - plugins/status|history|reload*：loader 监管能力（属内核；reload 已实现 pull 热加载，
//! //     见 invoker.rs force_unload + 指纹检测；history 返回真实审计）
//! //   - threads/messages：会话传输宿主（WS/session 属内核）；messages 空 stub 待持久化
//!
//! 对齐 `plugins/shared/system/channel_api/routes_missing.py` 与
//! `routes_plugins.py` 的响应形状，让前端不再因缺失路由刷屏。
//! 能接真实数据源的接真实数据；否则返回结构正确的空/零值。

use std::sync::OnceLock;

use axum::extract::{Path, Query, State};
use axum::http::HeaderMap;
use axum::Json;
use parking_lot::RwLock;
use serde::Deserialize;
use serde_json::{json, Value};
use tracing::{info, warn};

use crate::routes::AppState;

/// 热重载审计历史（内存环形缓冲，进程级单例）。
/// 对照 0.1 的 PluginHotReloader 重载历史，前端 GET /api/v1/plugins/history 读取。
struct ReloadHistory {
    entries: std::collections::VecDeque<Value>,
}

const MAX_HISTORY: usize = 200;

static RELOAD_HISTORY: OnceLock<RwLock<ReloadHistory>> = OnceLock::new();

fn reload_history() -> &'static RwLock<ReloadHistory> {
    RELOAD_HISTORY.get_or_init(|| {
        RwLock::new(ReloadHistory {
            entries: std::collections::VecDeque::with_capacity(MAX_HISTORY),
        })
    })
}

/// 追加一条审计记录。
fn record_history(entry: Value) {
    let mut h = reload_history().write();
    if h.entries.len() >= MAX_HISTORY {
        h.entries.pop_front();
    }
    h.entries.push_back(entry);
}

/// 在 base（已是 json! 对象）上追加键值对，生成一条新的历史记录并入库。
/// 用于替代 `json!({...spread, ...})`（serde_json 宏不支持 spread 语法）。
fn record_history_ext(base: Value, kvs: &[(&str, Value)]) {
    let mut v = base;
    if let Some(obj) = v.as_object_mut() {
        for (k, val) in kvs {
            obj.insert((*k).to_string(), val.clone());
        }
    }
    record_history(v);
}

// ─── Threads ───────────────────────────────────────────────────────────────

/// GET /api/v1/threads — 会话列表。
///
/// 域2持久化：优先从 sessions 表读（重启后可恢复），DB 空时回退内存 registry
/// （兼容 store 未配置或历史场景）。返回结构对齐前端 ThreadStateResponse。
pub async fn list_threads_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Json<Value> {
    let now = chrono::Utc::now().to_rfc3339();
    let mut threads: Vec<Value> = Vec::new();

    // 优先读 DB（持久化会话列表）。list_sessions 按 task_local tenant 过滤，
    // 需在请求租户 scope 内执行（修复前直接调，多租户下永远只读 default）。
    let mut db_has_data = false;
    if let Some(store) = state.store.as_ref() {
        let tenant_ctx =
            crate::server::request_tenant_ctx(state.store.as_ref(), &headers, "").await;
        let store_clone = store.clone();
        let sessions_result = agentos_tenant::scope(tenant_ctx, async move {
            let filter = agentos_core::traits::SessionListFilter {
                session_type: Some("main_pipeline".to_string()),
                limit: Some(100),
            };
            store_clone.list_sessions(filter).await
        })
        .await;
        match sessions_result {
            Ok(sessions) if !sessions.is_empty() => {
                db_has_data = true;
                for s in sessions {
                    threads.push(session_to_thread_json(&s));
                }
            }
            Ok(_) => {}
            Err(e) => {
                tracing::warn!(error = %e, "list_sessions 查询失败，回退内存 registry");
            }
        }
    }

    // DB 无数据时回退内存 registry（兼容场景）
    if !db_has_data {
        if let Some(session) = &state.session {
            let registry = session.registry();
            for (thread_id, user_id) in session.list_threads() {
                let pipeline_id = registry.get_pipeline_for_thread(&thread_id);
                let agent_id = registry.get_agent_for_thread(&thread_id);
                let pipeline_ids = pipeline_id
                    .as_deref()
                    .map(|p| vec![p.to_string()])
                    .unwrap_or_default();
                threads.push(json!({
                    "thread_id": thread_id,
                    "title": null,
                    "current_state": "active",
                    "intent": null,
                    "created_at": now,
                    "updated_at": now,
                    "agent_id": agent_id,
                    "message_count": 0,
                    "pipeline_ids": pipeline_ids,
                    "active_pipeline_id": pipeline_id,
                    "metadata": { "user_id": user_id },
                }));
            }
        }
    }

    let total = threads.len();
    Json(json!({ "threads": threads, "total": total }))
}

/// SessionRecord → 前端 ThreadStateResponse JSON（对齐 mappers.ts:12-35）。
fn session_to_thread_json(s: &agentos_core::types::SessionRecord) -> Value {
    json!({
        "thread_id": s.thread_id,
        "title": s.title,
        "current_state": s.current_state,
        "intent": s.intent,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
        "agent_id": s.agent_id,
        "message_count": 0,
        "pipeline_ids": s.pipeline_ids,
        "active_pipeline_id": s.active_pipeline_id,
        "metadata": s.metadata.clone().unwrap_or_else(|| json!({})),
    })
}

/// POST /api/v1/threads — 创建会话（内存登记，返回新 thread_id）。
pub async fn create_thread_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Json<Value> {
    let now = chrono::Utc::now().to_rfc3339();
    let thread_id = format!("thread-{}", uuid::Uuid::new_v4());
    // 生成主管道 ID（对照 0.1 routes_threads.py:267 的 uuid4().hex[:12]）。
    // 前端发消息时用它作 WS 路由键（前端 activeTab.pipelineRunId ← session.pipelineIds[0]），
    // stream_start/stream_chunk/new_message 也用它匹配占位气泡。不生成则前端发消息被静默拦截。
    let pipeline_id = uuid::Uuid::new_v4().simple().to_string();
    let title = body.get("title").and_then(|v| v.as_str()).unwrap_or("");
    let intent = body
        .get("intent")
        .and_then(|v| v.as_str())
        .or(if title.is_empty() { None } else { Some(title) });
    let agent_id = body.get("agent_id").cloned().unwrap_or(json!("agentos"));
    let user_id = body
        .get("user_id")
        .and_then(|v| v.as_str())
        .unwrap_or("anonymous");

    // 注册 thread → user 映射（WS 流式推送据此定位连接）+ thread → pipeline 映射
    if let Some(session) = &state.session {
        session.registry().register_thread(&thread_id, user_id);
        session.registry().register_thread_pipeline(&thread_id, &pipeline_id);
    }

    // 域2持久化：会话落 sessions 表（对齐 0.1 SessionModel）。
    // 内存在 registry 仍保留（WS 路由用），DB 负责跨重启恢复，create 时双写。
    if let Some(store) = state.store.as_ref() {
        let metadata = body.get("metadata").cloned().unwrap_or_else(|| {
            json!({ "session_type": "main_pipeline", "user_id": user_id })
        });
        let agent_id_str = agent_id.as_str().and_then(|s| if s.is_empty() { None } else { Some(s) });
        let session_rec = agentos_core::types::SessionRecord {
            thread_id: thread_id.clone(),
            title: if title.is_empty() { None } else { Some(title.to_string()) },
            intent: intent.map(|s| s.to_string()),
            current_state: "active".to_string(),
            agent_id: agent_id_str.map(|s| s.to_string()),
            active_pipeline_id: Some(pipeline_id.clone()),
            pipeline_ids: vec![pipeline_id.clone()],
            metadata: Some(metadata.clone()),
            created_at: now.clone(),
            updated_at: now.clone(),
            last_active_at: Some(now.clone()),
        };
        // create_session 按 task_local tenant 落库，需在请求租户 scope 内执行
        // （修复前直接调，多租户下会话落到 default 而非请求用户的租户）。
        let tenant_ctx =
            crate::server::request_tenant_ctx(state.store.as_ref(), &headers, &thread_id).await;
        let store_clone = store.clone();
        let pid_clone = pipeline_id.clone();
        let tid_clone = thread_id.clone();
        let create_result = agentos_tenant::scope(tenant_ctx, async move {
            store_clone.create_session(&session_rec).await?;
            // 主管道兜底：创建会话即写映射，保证主管道即使未跑过也有映射（删会话级联不漏）。
            let tenant_id =
                agentos_tenant::current_or_default("default").tenant_id;
            store_clone.link_pipeline_session(&pid_clone, &tid_clone, &tenant_id).await?;
            Ok::<(), agentos_core::types::StorageError>(())
        })
        .await;
        if let Err(e) = create_result {
            tracing::warn!(thread_id = %thread_id, error = %e, "create_session 落库失败（继续返回）");
        }
    }

    Json(json!({
        "thread_id": thread_id,
        "created_at": now,
        "updated_at": now,
        "current_state": "active",
        "intent": intent,
        "title": if title.is_empty() { Value::Null } else { json!(title) },
        "agent_id": agent_id,
        "pipeline_ids": [pipeline_id],
        "active_pipeline_id": pipeline_id,
        "message_count": 0,
        "metadata": body.get("metadata").cloned().unwrap_or_else(|| json!({})),
    }))
}

/// GET /api/v1/threads/{id}
///
/// 域2持久化：优先从 DB 读（含 pipeline_ids），回退内存 registry。
pub async fn get_thread_handler(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Json<Value> {
    // 优先读 DB
    if let Some(store) = state.store.as_ref() {
        if let Ok(Some(s)) = store.get_session(&id).await {
            return Json(session_to_thread_json(&s));
        }
    }

    // 回退内存 registry
    let now = chrono::Utc::now().to_rfc3339();
    let (user_id, pipeline_id, agent_id) = state
        .session
        .as_ref()
        .map(|s| {
            let r = s.registry();
            (
                r.get_user_for_thread(&id),
                r.get_pipeline_for_thread(&id),
                r.get_agent_for_thread(&id),
            )
        })
        .unwrap_or((None, None, None));
    let pipeline_ids = pipeline_id
        .as_deref()
        .map(|p| vec![p.to_string()])
        .unwrap_or_default();

    Json(json!({
        "thread_id": id,
        "title": null,
        "current_state": if user_id.is_some() { "active" } else { "unknown" },
        "intent": null,
        "created_at": now,
        "updated_at": now,
        "agent_id": agent_id,
        "message_count": 0,
        "pipeline_ids": pipeline_ids,
        "active_pipeline_id": pipeline_id,
        "metadata": { "user_id": user_id },
    }))
}

/// PATCH /api/v1/threads/{id}/agent — 绑定/切换会话的主 Agent。
///
/// 前端 AgentSelector.handleSelect 调此端点(session.ts:489 用 PATCH)。
/// body: { "agent_id": "<id>" }。响应返回完整 thread 状态(含新 agent_id)。
/// 域2持久化：更新 sessions 表 agent_id（DB 无记录则跳过 DB，仅写内存）。
pub async fn update_thread_agent_handler(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> Json<Value> {
    let agent_id = body
        .get("agent_id")
        .and_then(|v| v.as_str())
        .unwrap_or("agentos")
        .to_string();

    // 内存 registry 更新（WS 路由仍用它）
    if let Some(session) = &state.session {
        let registry = session.registry();
        registry.register_thread_agent(&id, &agent_id);
    }

    let now = chrono::Utc::now().to_rfc3339();

    // DB 更新：若存在会话记录则更新 agent_id + updated_at
    if let Some(store) = state.store.as_ref() {
        if let Ok(Some(mut s)) = store.get_session(&id).await {
            s.agent_id = Some(agent_id.clone());
            s.updated_at = now.clone();
            if let Err(e) = store.update_session(&s).await {
                tracing::warn!(thread_id = %id, error = %e, "update_session(agent) 落库失败");
            }
            return Json(session_to_thread_json(&s));
        }
    }

    // DB 无记录：回退内存构造响应
    let pipeline_id = state
        .session
        .as_ref()
        .and_then(|s| s.registry().get_pipeline_for_thread(&id));
    let pipeline_ids = pipeline_id
        .as_deref()
        .map(|p| vec![p.to_string()])
        .unwrap_or_default();

    Json(json!({
        "thread_id": id,
        "title": null,
        "current_state": "active",
        "intent": null,
        "created_at": now,
        "updated_at": now,
        "agent_id": agent_id,
        "message_count": 0,
        "pipeline_ids": pipeline_ids,
        "active_pipeline_id": pipeline_id,
        "metadata": {},
    }))
}

/// PATCH /api/v1/threads/{id} — 重命名/更新会话（title/intent）。
///
/// 前端 `updateSession()` 调此端点（session.ts:530 用 PATCH）。
/// body: { "intent": "<title>" }（前端把 title 映射成 intent，见 session.ts:521），
/// 也兼容旧字段名 "title"。响应返回完整 thread 状态。
/// 域2持久化：更新 sessions 表 title/intent + updated_at（DB 无记录则仅回退内存响应）。
pub async fn update_thread_handler(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> Json<Value> {
    // 前端 updateSession 把 title 放进 intent 字段；同时兼容直传 title。
    let new_intent = body
        .get("intent")
        .and_then(|v| v.as_str())
        .or_else(|| body.get("title").and_then(|v| v.as_str()))
        .map(|s| s.to_string());

    let now = chrono::Utc::now().to_rfc3339();

    // DB 更新：若存在会话记录则更新 title/intent + updated_at
    if let Some(store) = state.store.as_ref() {
        if let Ok(Some(mut s)) = store.get_session(&id).await {
            if let Some(ref intent) = new_intent {
                s.title = Some(intent.clone());
                s.intent = Some(intent.clone());
            }
            s.updated_at = now.clone();
            if let Err(e) = store.update_session(&s).await {
                tracing::warn!(thread_id = %id, error = %e, "update_session(title/intent) 落库失败");
            }
            return Json(session_to_thread_json(&s));
        }
    }

    // DB 无记录：回退内存构造响应（保留 agent_id/pipeline_id 等已知状态）
    let agent_id = state
        .session
        .as_ref()
        .and_then(|s| s.registry().get_agent_for_thread(&id));
    let pipeline_id = state
        .session
        .as_ref()
        .and_then(|s| s.registry().get_pipeline_for_thread(&id));
    let pipeline_ids = pipeline_id
        .as_deref()
        .map(|p| vec![p.to_string()])
        .unwrap_or_default();

    Json(json!({
        "thread_id": id,
        "title": new_intent,
        "current_state": "active",
        "intent": new_intent,
        "created_at": now,
        "updated_at": now,
        "agent_id": agent_id,
        "message_count": 0,
        "pipeline_ids": pipeline_ids,
        "active_pipeline_id": pipeline_id,
        "metadata": {},
    }))
}

/// DELETE /api/v1/threads/{id} — 删除会话（级联）。
///
/// 前端 `deleteSession()` 调此端点（session.ts:398 用 DELETE）。
/// 级联删除该会话下全部管道（主管道 + 子任务管道，经 pipeline_sessions 映射表按
/// thread_id 定位）的 messages / execution_records / traces / branches /
/// pipeline_run_summaries / runs，最后清映射表与 sessions 行。blobs 不删（内容寻址去重）。
/// store 不可用或无记录仍返回 200（幂等，对齐 REST 删除语义）。
pub async fn delete_thread_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Json<Value> {
    if let Some(store) = state.store.as_ref() {
        // 多租户：级联删除按 task_local tenant 过滤，需在请求租户 scope 内执行。
        // 修复前直接调（task_local 未设 → current_or_default("default")），多租户下
        // delete_session_inner 查 pipeline_sessions WHERE tenant_id='default' 查空 → 啥也没删。
        let tenant_ctx =
            crate::server::request_tenant_ctx(state.store.as_ref(), &headers, &id).await;
        let store_clone = store.clone();
        let id_for_scope = id.clone();
        let del_result = agentos_tenant::scope(tenant_ctx, async move {
            store_clone.delete_session(&id_for_scope).await
        })
        .await;
        if let Err(e) = del_result {
            tracing::warn!(thread_id = %id, error = %e, "delete_session 落库失败（仍返回 200）");
        }
    }
    Json(json!({ "thread_id": id, "deleted": true }))
}
///
/// 按管道查询历史消息（消息层自治查询，对齐 0.1 list_by_pipeline）。
///
/// 消息层只认 pipeline_id（= 其他项目的会话 id），不关心会话（thread）归属。
/// 前端 `getMessages` 已通过 `pipeline_run_id` 查询参数传管道 ID（session.ts:425），
/// 未传时回退到路径 id（兼容 thread_id == pipeline_id 的旧数据，对齐 0.1
/// routes_threads.py:792 `target_pid = pipeline_run_id or thread_id`）。
///
/// sequence 按 pipeline_id 维度连续递增，支持 before_sequence/after_sequence 游标分页。
pub async fn list_thread_messages_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    Query(q): Query<MessageListQuery>,
) -> Json<Value> {
    let Some(store) = state.store.as_ref() else {
        return Json(json!({ "messages": [], "total": 0, "has_more": false }));
    };

    // 对齐 0.1：传了 pipeline_run_id 就用它，否则回退路径 id
    let target_pid = q.pipeline_run_id.unwrap_or_else(|| id.clone());

    let opts = agentos_core::traits::MessageQueryOpts {
        before_sequence: q.before_sequence,
        after_sequence: q.after_sequence,
        limit: q.limit,
    };

    // 多租户：get_messages_by_pipeline 按 task_local tenant 过滤，需在请求租户 scope 内执行。
    // 修复前直接调（task_local 未设 → current_or_default("default")），多租户下永远只读 default。
    let tenant_ctx = crate::server::request_tenant_ctx(state.store.as_ref(), &headers, &id).await;
    let store_clone = store.clone();
    let target_pid_for_scope = target_pid.clone();
    let records = match agentos_tenant::scope(tenant_ctx, async move {
        store_clone.get_messages_by_pipeline(&target_pid_for_scope, opts).await
    })
    .await
    {
        Ok(r) => r,
        Err(e) => {
            tracing::warn!(pipeline_id = %target_pid, error = %e, "get_messages_by_pipeline 查询失败");
            return Json(json!({ "messages": [], "total": 0, "has_more": false }));
        }
    };

    // 联查 blobs 取完整内容（content_preview 只是摘要，完整内容在 blobs 表）
    // get_blob 内容寻址，不依赖 tenant，scope 外调用即可
    let mut messages: Vec<Value> = Vec::with_capacity(records.len());
    for rec in records {
        let content = if let Some(bid) = rec.blob_id.as_deref() {
            match store.get_blob(bid).await {
                Ok(bytes) => String::from_utf8_lossy(&bytes).to_string(),
                Err(_) => rec.content_preview.clone().unwrap_or_default(),
            }
        } else {
            rec.content_preview.clone().unwrap_or_default()
        };
        let mut msg = json!({
            // 字段命名对齐前端 BackendMessageResponse（session.ts:53-79）
            "id": rec.message_id,
            "thread_id": id,            // 回填路径 id（满足前端 mapper，不参与查询过滤）
            "sequence": rec.seq_in_branch,
            "role": rec.role,
            "content": content,
            "timestamp": rec.created_at,
            // 工具结果状态：用持久化的真实值（completed/failed），不再硬编码。
            // 非 tool 消息无 status 列值 → 回退 completed（保持历史行为）。
            // 与流式 tool_result 事件的 success 信号统一，前端刷新后可还原失败态。
            "status": rec.status.clone().unwrap_or_else(|| "completed".to_string()),
        });
        // 工具失败文本：role=tool 且 status=failed 时附带，前端据此与流式渲染保持一致。
        if let Some(err) = rec.error.as_deref() {
            if !err.is_empty() {
                msg.as_object_mut()
                    .expect("msg is object")
                    .insert("error".into(), Value::String(err.to_string()));
            }
        }
        // 工具调用相关：assistant 的 tool_calls（反序列化为数组）、tool 消息的 tool_call_id。
        // 分层持久化补全：让前端能还原完整多轮工具调用对话，而非只存扁平文本。
        // 字段名 camelCase 对齐前端 BackendMessageResponse（toolCalls / toolCallId）。
        if let Some(tc_json) = rec.tool_calls_json.as_deref() {
            if let Ok(tool_calls) = serde_json::from_str::<Value>(tc_json) {
                msg.as_object_mut()
                    .expect("msg is object")
                    .insert("toolCalls".into(), tool_calls);
            }
        }
        if let Some(tc_id) = rec.tool_call_id.as_deref() {
            msg.as_object_mut()
                .expect("msg is object")
                .insert("toolCallId".into(), Value::String(tc_id.to_string()));
        }
        // 思考内容：assistant 的 reasoning_content（LLM reasoning/chain-of-thought）。
        // 前端据此渲染思考过程折叠区。字段名 camelCase 对齐前端 BackendMessageResponse。
        if let Some(reasoning) = rec.reasoning_content.as_deref() {
            if !reasoning.is_empty() {
                msg.as_object_mut()
                    .expect("msg is object")
                    .insert("reasoningContent".into(), Value::String(reasoning.to_string()));
            }
        }
        messages.push(msg);
    }

    let total = messages.len();
    // has_more：若带 limit 且返回条数等于 limit，则可能还有更多
    let has_more = q.limit.map(|lim| total >= lim).unwrap_or(false);
    Json(json!({ "messages": messages, "total": total, "has_more": has_more }))
}

/// `GET /api/v1/threads/{id}/messages` 查询参数。
///
/// 对齐前端 getMessages 的查询参数（session.ts:421-432）：
/// - pipeline_run_id：管道 ID（消息层查询主键）
/// - before_sequence / after_sequence：游标分页
/// - limit：最多返回条数
#[derive(Debug, Deserialize)]
pub struct MessageListQuery {
    pub pipeline_run_id: Option<String>,
    pub before_sequence: Option<u32>,
    pub after_sequence: Option<u32>,
    pub limit: Option<usize>,
}

// ─── Plugins 顶层管理 ──────────────────────────────────────────────────────

/// GET /api/v1/plugins/status — 从 manifests 派生状态列表。
pub async fn plugins_status_handler(State(state): State<AppState>) -> Json<Value> {
    let enabled_ids = state.enabled_plugin_ids.read().await;
    let items: Vec<Value> = state
        .manifests
        .iter()
        .map(|m| {
            let config_type = match m.plugin_type {
                agentos_core::traits::PluginType::System => "system",
                agentos_core::traits::PluginType::Pipeline => "pipeline",
                agentos_core::traits::PluginType::Tool => "tool",
                agentos_core::traits::PluginType::Composite => "composite",
            };
            let enabled = enabled_ids.contains(&m.id);
            // 运行态：enabled 且是 sidecar → Active（按需 lazy 时实为 Idle，但无法从静态状态区分，
            // 统一标 active）；enabled 非 sidecar → active；disabled → disabled
            let run_status = if enabled { "active" } else { "disabled" };
            let activation = match m.activation {
                Some(agentos_core::traits::ActivationPolicy::Eager) => "eager",
                Some(agentos_core::traits::ActivationPolicy::Manual) => "manual",
                _ => "lazy", // None = 走 default_profile 默认 lazy
            };
            let host_type = match m.host_type {
                agentos_core::traits::HostType::InProcess => "in_process",
                agentos_core::traits::HostType::Sidecar => "sidecar",
                agentos_core::traits::HostType::Wasm => "wasm",
            };
            json!({
                "plugin_id": m.id,
                "name": m.name,
                "config_type": config_type,
                "host_type": host_type,
                "version": m.version,
                "enabled": enabled,
                "activation": activation,
                "status": run_status,
                "config_files": m.config_files.iter().map(|c| json!({
                    "id": c.id,
                    "label": c.label,
                    "path": c.path,
                })).collect::<Vec<_>>(),
                "has_contributes": m.contributes.is_some(),
                "has_http_endpoints": !m.http_endpoints.is_empty(),
                "error": null,
            })
        })
        .collect();
    Json(json!(items))
}

/// GET /api/v1/plugins/history — 热重载审计历史（内存环形缓冲）。
pub async fn plugins_history_handler() -> Json<Value> {
    let h = reload_history().read();
    Json(json!(h.entries.iter().cloned().collect::<Vec<_>>()))
}

/// 通过 plugin_id 在 manifests 里查 manifest，返回 host_type 字符串。
fn host_type_of(state: &AppState, plugin_id: &str) -> Option<&'static str> {
    let m = state.manifests.iter().find(|m| m.id == plugin_id)?;
    Some(match m.host_type {
        agentos_core::traits::HostType::Sidecar => "sidecar",
        agentos_core::traits::HostType::InProcess => "inprocess",
        agentos_core::traits::HostType::Wasm => "wasm",
    })
}

/// POST /api/v1/plugins/reload — 热重载单个插件。
///
/// 行为按 host_type 分流（对照 ADR §插件热加载）：
/// - sidecar：调 invoker.force_unload（kill 旧进程 + 清缓存），下次调用自动 respawn 加载新代码。
/// - wasm：同上（wasm runtime 会重新编译 .wasm）。
/// - inprocess(cdylib)：不支持热加载（Windows dlclose 限制），返回 restart_required。
/// - 未知插件：返回 not_found（提示用户：新插件需重启 kernel 才能首次发现）。
pub async fn plugins_reload_handler(
    State(state): State<AppState>,
    Query(params): Query<ReloadQuery>,
) -> Json<Value> {
    let now = chrono::Utc::now().to_rfc3339();
    // 解析 plugin_id：优先 config_path（按插件目录名定位），其次 query 无则报错。
    let plugin_id = match params.config_path.as_deref() {
        Some(p) => {
            // config_path 可能是 config/agents/xxx.yaml 或插件目录名；取最后一段作 plugin_id 线索
            let seg = p
                .trim_end_matches('/')
                .rsplit(['/', '\\'])
                .next()
                .unwrap_or(p);
            seg.trim_end_matches(".yaml")
                .trim_end_matches(".yml")
                .to_string()
        }
        None => {
            return Json(json!({
                "config_path": null,
                "success": false,
                "error": "缺少 plugin_id：请用 ?config_path=<plugin_id> 或 <插件目录> 指定",
                "rolled_back": false,
            }))
        }
    };

    let host_type = host_type_of(&state, &plugin_id);
    let entry_base = json!({
        "timestamp": now,
        "plugin_id": plugin_id,
        "config_path": params.config_path,
    });

    // 未知插件：当前运行期的 manifest 缓存里没有。新插件需重启 kernel 才能首次发现。
    let host = match host_type {
        Some(h) => h,
        None => {
            let resp = json!({
                "config_path": params.config_path,
                "config_type": "unknown",
                "success": false,
                "error": format!("插件 {} 不在当前运行期 manifest，新插件需重启内核首次发现", plugin_id),
                "rolled_back": false,
                "restart_required": true,
            });
            record_history_ext(entry_base.clone(), &[
                ("success", json!(false)), ("event_type", json!("reload_unknown")),
            ]);
            return Json(resp);
        }
    };

    // inprocess(cdylib)：不支持热加载
    if host == "inprocess" {
        let resp = json!({
            "config_path": params.config_path,
            "config_type": host,
            "success": false,
            "error": "cdylib(inprocess) 插件不支持热加载（Windows dlclose 限制），需重启内核",
            "rolled_back": false,
            "restart_required": true,
        });
        record_history_ext(entry_base.clone(), &[
            ("success", json!(false)), ("event_type", json!("reload_cdylib_skip")),
        ]);
        return Json(resp);
    }

    // sidecar / wasm：force_unload → 下次调用 respawn 加载新代码/配置
    let invoker = match state.invoker.as_ref() {
        Some(i) => i,
        None => {
            return Json(json!({
                "config_path": params.config_path, "config_type": host,
                "success": false, "error": "invoker 未配置", "rolled_back": false,
            }))
        }
    };
    match invoker.force_unload(&plugin_id).await {
        Ok(()) => {
            info!(plugin_id = %plugin_id, host_type = host, "Plugin hot-reloaded via reload endpoint");
            let resp = json!({
                "config_path": params.config_path,
                "config_type": host,
                "success": true,
                "message": format!("插件 {} 已卸载，下次调用自动加载最新代码/配置", plugin_id),
                "rolled_back": false,
                "restart_required": false,
            });
            record_history_ext(entry_base.clone(), &[
                ("success", json!(true)), ("event_type", json!("reload_ok")),
                ("host_type", json!(host)),
            ]);
            Json(resp)
        }
        Err(e) => {
            warn!(plugin_id = %plugin_id, error = %e, "Plugin reload force_unload failed");
            let resp = json!({
                "config_path": params.config_path,
                "config_type": host,
                "success": false,
                "error": format!("force_unload 失败: {}", e),
                "rolled_back": false,
            });
            record_history_ext(entry_base.clone(), &[
                ("success", json!(false)), ("event_type", json!("reload_failed")),
                ("error", json!(e.to_string())),
            ]);
            Json(resp)
        }
    }
}

/// POST /api/v1/plugins/reload-all — 热重载所有插件 + 发现新增插件（运行时懒加载入口）。
///
/// 两件事合一：
/// 1. **发现新增插件**：调 invoker.discover_new_plugins() 重扫插件目录（幂等，不杀进程），
///    对比已知 id 集合，把新插件的 tools/route_signals 注册到 capability_registry。
///    新插件之后走懒加载（首次调用才 spawn）。
/// 2. **热重载已有插件**：对已加载的 sidecar/wasm 调 force_unload（下次调用 respawn 新代码）。
///
/// 新增带 http_endpoints 的插件：tools 立即生效，但 axum 路由树需重启挂载 → restart_required。
pub async fn plugins_reload_all_handler(State(state): State<AppState>) -> Json<Value> {
    let now = chrono::Utc::now().to_rfc3339();
    let invoker = match state.invoker.as_ref() {
        Some(i) => i.clone(),
        None => return Json(json!([])),
    };

    // ── 1. 发现并注册新增插件 ──
    let mut discovered: Vec<Value> = Vec::new();
    let mut restart_required_ids: Vec<String> = Vec::new();
    let discover_err = match invoker.discover_new_plugins().await {
        Ok(all_manifests) => {
            // 已知 id 集合（启动期 manifests 快照）
            let existing: std::collections::HashSet<String> =
                state.manifests.iter().map(|m| m.id.clone()).collect();
            if let Some(registry) = state.capability_registry.as_ref() {
                let (new_ids, tool_count) =
                    crate::plugin_lifecycle::register_new_plugins(&all_manifests, &existing, registry);
                // 检查新增插件是否有 http_endpoints（需重启挂载路由）
                for m in &all_manifests {
                    if new_ids.contains(&m.id) && crate::plugin_lifecycle::has_http_endpoints(m) {
                        restart_required_ids.push(m.id.clone());
                    }
                }
                discovered = new_ids
                    .iter()
                    .map(|id| json!({"plugin_id": id, "discovered": true}))
                    .collect();
                info!(new_plugins = new_ids.len(), tools = tool_count, "reload-all: discovered new plugins");
            }
            None
        }
        Err(e) => Some(e.to_string()),
    };

    // ── 2. 热重载已有插件（force_unload，下次调用 respawn）──
    let mut results: Vec<Value> = Vec::new();
    for m in state.manifests.iter() {
        let host = match m.host_type {
            agentos_core::traits::HostType::Sidecar => "sidecar",
            agentos_core::traits::HostType::Wasm => "wasm",
            agentos_core::traits::HostType::InProcess => "inprocess",
        };
        if host == "inprocess" {
            results.push(json!({
                "plugin_id": m.id, "host_type": host,
                "success": false, "skipped": true,
                "error": "cdylib 不支持热加载，需重启",
            }));
            continue;
        }
        match invoker.force_unload(&m.id).await {
            Ok(()) => results.push(json!({
                "plugin_id": m.id, "host_type": host, "success": true,
            })),
            Err(e) => results.push(json!({
                "plugin_id": m.id, "host_type": host,
                "success": false, "error": e.to_string(),
            })),
        }
    }
    info!(reloaded = results.len(), discovered = discovered.len(), "reload-all completed");
    record_history(json!({
        "timestamp": now, "event_type": "reload_all",
        "success": true, "reloaded": results.len(), "discovered": discovered.len(),
        "discover_error": discover_err,
    }));
    Json(json!({
        "reloaded": results,
        "discovered": discovered,
        "restart_required": restart_required_ids,
        "discover_error": discover_err,
    }))
}

#[derive(Debug, Deserialize, Default)]
pub struct ReloadQuery {
    pub config_path: Option<String>,
}

/// POST /api/v1/plugins/{id}/reload — 按 plugin_id 直接热重载（REST 风格，比 query 参数更直观）。
///
/// 与 `plugins_reload_handler` 等价，但 plugin_id 从 path 取。行为按 host_type 分流
/// （sidecar/wasm → force_unload；inprocess → 不支持；未知 → 提示需重启）。
pub async fn plugins_reload_by_id_handler(
    State(state): State<AppState>,
    Path(plugin_id): Path<String>,
) -> Json<Value> {
    let now = chrono::Utc::now().to_rfc3339();
    let host = host_type_of(&state, &plugin_id);
    let entry_base = json!({
        "timestamp": now, "plugin_id": plugin_id,
    });

    let host = match host {
        Some(h) => h,
        None => {
            let resp = json!({
                "plugin_id": plugin_id, "success": false,
                "error": format!("插件 {} 不在当前运行期 manifest，新插件需重启内核首次发现", plugin_id),
                "restart_required": true,
            });
            record_history_ext(entry_base.clone(), &[
                ("success", json!(false)), ("event_type", json!("reload_unknown")),
            ]);
            return Json(resp);
        }
    };

    if host == "inprocess" {
        let resp = json!({
            "plugin_id": plugin_id, "host_type": host, "success": false,
            "error": "cdylib(inprocess) 插件不支持热加载（Windows dlclose 限制），需重启内核",
            "restart_required": true,
        });
        record_history_ext(entry_base.clone(), &[
            ("success", json!(false)), ("event_type", json!("reload_cdylib_skip")),
        ]);
        return Json(resp);
    }

    let invoker = match state.invoker.as_ref() {
        Some(i) => i,
        None => {
            return Json(json!({
                "plugin_id": plugin_id, "host_type": host,
                "success": false, "error": "invoker 未配置",
            }))
        }
    };
    match invoker.force_unload(&plugin_id).await {
        Ok(()) => {
            info!(plugin_id = %plugin_id, host_type = host, "Plugin hot-reloaded via by-id endpoint");
            record_history_ext(entry_base.clone(), &[
                ("success", json!(true)), ("event_type", json!("reload_ok")),
                ("host_type", json!(host)),
            ]);
            Json(json!({
                "plugin_id": plugin_id, "host_type": host, "success": true,
                "message": format!("插件 {} 已卸载，下次调用自动加载最新代码/配置", plugin_id),
                "restart_required": false,
            }))
        }
        Err(e) => {
            warn!(plugin_id = %plugin_id, error = %e, "reload-by-id force_unload failed");
            record_history_ext(entry_base.clone(), &[
                ("success", json!(false)), ("event_type", json!("reload_failed")),
                ("error", json!(e.to_string())),
            ]);
            Json(json!({
                "plugin_id": plugin_id, "host_type": host,
                "success": false, "error": format!("force_unload 失败: {}", e),
            }))
        }
    }
}

/// PUT /api/v1/plugins/{id}/enabled — 切换插件启用状态（写 default_profile.yaml）
///
/// 安装触发模型 L1：改 profile 文件后**需重启内核生效**（enablement 在启动期读）。
/// 返回 {success, enabled, restart_required}。
pub async fn plugins_set_enabled_handler(
    Path(plugin_id): Path<String>,
    State(state): State<AppState>,
    Json(body): Json<EnabledBody>,
) -> Json<Value> {
    let new_enabled = body.enabled;
    let project_root = match &state.project_root {
        Some(p) => p,
        None => {
            return Json(json!({
                "success": false,
                "error": "project_root not available",
            }))
        }
    };
    let profile_path = project_root.join("config").join("plugins").join("default_profile.yaml");

    // 读现有 profile（不存在则用空结构）
    let raw = std::fs::read_to_string(&profile_path).unwrap_or_default();
    let mut doc: serde_yaml::Value = serde_yaml::from_str(&raw).unwrap_or_else(|_| {
        serde_yaml::from_str("version: 1\nplugins:\ndefaults:\n  enabled: true\n  activation: lazy\n")
            .unwrap()
    });

    // 改 plugins.<id>.enabled（手动操作 serde_yaml Mapping）
    if let serde_yaml::Value::Mapping(ref mut top) = doc {
        // 确保 plugins 键存在且是 Mapping
        let plugins_key = serde_yaml::Value::String("plugins".into());
        if !top.contains_key(&plugins_key) {
            top.insert(plugins_key.clone(), serde_yaml::Value::Mapping(serde_yaml::Mapping::new()));
        }
        if let Some(serde_yaml::Value::Mapping(ref mut plugins_map)) = top.get_mut(&plugins_key) {
            let pid_key = serde_yaml::Value::String(plugin_id.clone());
            // 确保该插件条目存在
            if !plugins_map.contains_key(&pid_key) {
                plugins_map.insert(pid_key.clone(), serde_yaml::Value::Mapping(serde_yaml::Mapping::new()));
            }
            if let Some(serde_yaml::Value::Mapping(ref mut entry)) = plugins_map.get_mut(&pid_key) {
                entry.insert(
                    serde_yaml::Value::String("enabled".into()),
                    serde_yaml::Value::Bool(new_enabled),
                );
            }
        }
    }

    // 写回
    let new_raw = serde_yaml::to_string(&doc).unwrap_or_default();
    match std::fs::write(&profile_path, new_raw) {
        Ok(_) => {
            // ── 热加载：立即改内存状态，不用重启 ──
            // 1) 改 enabled_plugin_ids（schema 出口的 contributes/configs 立即生效）
            {
                let mut ids = state.enabled_plugin_ids.write().await;
                if new_enabled {
                    ids.insert(plugin_id.clone());
                } else {
                    ids.remove(&plugin_id);
                }
            }
            // 2) 禁用时立即从 CapabilityRegistry 摘掉（tools/http_routes 立即不可用）
            //    启用时重新注册需要重启（axum 路由树在启动期固定，运行时无法动态加路由）
            if !new_enabled {
                if let Some(registry) = &state.capability_registry {
                    use agentos_core::traits::CapabilityRegistry;
                    registry.clear_plugin(&plugin_id);
                }
            }
            let restart_needed = new_enabled; // 启用需重启（路由重建），禁用立即生效
            tracing::info!(
                target: "plugin-enablement",
                "plugin {} enabled={} (hot-reloaded: contributes + registry updated, restart={})",
                plugin_id, new_enabled, restart_needed
            );
            Json(json!({
                "success": true,
                "plugin_id": plugin_id,
                "enabled": new_enabled,
                "restart_required": restart_needed,
                "message": if restart_needed {
                    format!("已启用插件 {}（重启后完全生效，contributes 已立即更新）", plugin_id)
                } else {
                    format!("已禁用插件 {}（立即生效）", plugin_id)
                },
            }))
        }
        Err(e) => Json(json!({
            "success": false,
            "error": format!("写入 profile 失败: {}", e),
        })),
    }
}

#[derive(Debug, Deserialize)]
pub struct EnabledBody {
    pub enabled: bool,
}

// ─── helpers ───────────────────────────────────────────────────────────────

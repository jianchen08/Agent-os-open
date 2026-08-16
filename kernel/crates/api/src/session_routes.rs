//! 会话管理原生端点（/api/v1/sessions/*）——0.2 会话域唯一实现
//!
//! 转正说明（task_kernel_cleanup_and_split 任务 2）：本模块由 compat_routes.rs
//! 的 threads 部分平移而来——原 `/api/v1/threads*` 是 0.1 迁移期的路径命名，
//! 但实现深度绑定 0.2 存储层（SQLite sessions 表 / ExecutionRecord / blobs /
//! pipeline_sessions 映射，非空 stub）。路径改为 0.2 语义的 `/api/v1/sessions*`，
//! 实现与响应形状原样保留（前端 mappers.ts 契约无感知）。
//!
//! 边界原则：会话传输宿主（WS/session 属内核），会话 CRUD 是内核职责。
//! 已删死端点：GET /api/v1/sessions/{id}（无生产消费者，见
//! task_kernel_cleanup_and_split 任务 2 死代码清单）。

use axum::extract::{Path, Query, State};
use axum::http::HeaderMap;
use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::routes::AppState;

// ─── Sessions ─────────────────────────────────────────────────────────────

/// GET /api/v1/sessions — 会话列表。
///
/// 域2持久化：优先从 sessions 表读（重启后可恢复），DB 空时回退内存 registry
/// （兼容 store 未配置或历史场景）。返回结构对齐前端 Session 类型。
pub async fn list_sessions_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Json<Value> {
    let now = chrono::Utc::now().to_rfc3339();
    let mut threads: Vec<Value> = Vec::new();

    // 优先读 DB（持久化会话列表）。list_sessions 按 task_local tenant 过滤，
    // 需在请求租户 scope 内执行——scope 缺失回退 default 租户，跨租户读错位。
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
                    threads.push(session_to_session_json(&s));
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

/// SessionRecord → 前端 Session JSON（对齐 mappers.ts:12-35）。
fn session_to_session_json(s: &agentos_core::types::SessionRecord) -> Value {
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

/// POST /api/v1/sessions — 创建会话（内存登记 + 落库，返回新 thread_id）。
pub async fn create_session_handler(
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

    // A14：user_id 以 token（resolve_request_user）解析为准——body 里的 user_id
    // 是客户端可伪造字段，不得作为事件路由（WS 推送定位）的信任源；仅在无 token
    // 用户时作显式降级（warn 标记，供审计区分）。
    let token_user = crate::auth::resolve_request_user(state.store.as_ref(), &headers)
        .await
        .map(|(uid, _, _, _)| uid)
        .ok();
    let body_user = body
        .get("user_id")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(str::to_string);
    let user_id = match (&token_user, &body_user) {
        (Some(token_uid), Some(body_uid)) if token_uid != body_uid => {
            tracing::warn!(
                token_user = %token_uid,
                body_user = %body_uid,
                "create_session：body user_id 与 token 用户不一致，事件路由按 token 用户"
            );
            token_uid.clone()
        }
        (Some(token_uid), _) => token_uid.clone(),
        (None, Some(body_uid)) => {
            tracing::warn!(
                body_user = %body_uid,
                "create_session：无 token 用户，降级使用 body user_id（可伪造，仅测试/嵌入式场景）"
            );
            body_uid.clone()
        }
        (None, None) => "anonymous".to_string(),
    };

    // 注册 thread → user 映射（WS 流式推送据此定位连接）+ thread → pipeline 映射
    if let Some(session) = &state.session {
        session.registry().register_thread(&thread_id, &user_id);
        session
            .registry()
            .register_thread_pipeline(&thread_id, &pipeline_id);
    }

    // 域2持久化：会话落 sessions 表（对齐 0.1 SessionModel）。
    // 内存在 registry 仍保留（WS 路由用），DB 负责跨重启恢复，create 时双写。
    if let Some(store) = state.store.as_ref() {
        let metadata = body
            .get("metadata")
            .cloned()
            .unwrap_or_else(|| json!({ "session_type": "main_pipeline", "user_id": user_id }));
        let agent_id_str = agent_id
            .as_str()
            .and_then(|s| if s.is_empty() { None } else { Some(s) });
        let session_rec = agentos_core::types::SessionRecord {
            thread_id: thread_id.clone(),
            title: if title.is_empty() {
                None
            } else {
                Some(title.to_string())
            },
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
        // ——scope 缺失回退 default 租户，会话会落到 default 而非请求用户的租户。
        let tenant_ctx =
            crate::server::request_tenant_ctx(state.store.as_ref(), &headers, &thread_id).await;
        let store_clone = store.clone();
        let pid_clone = pipeline_id.clone();
        let tid_clone = thread_id.clone();
        let create_result = agentos_tenant::scope(tenant_ctx, async move {
            store_clone.create_session(&session_rec).await?;
            // 主管道兜底：创建会话即写映射，保证主管道即使未跑过也有映射（删会话级联不漏）。
            let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
            store_clone
                .link_pipeline_session(&pid_clone, &tid_clone, &tenant_id)
                .await?;
            Ok::<(), agentos_core::types::StorageError>(())
        })
        .await;
        if let Err(e) = create_result {
            tracing::warn!(thread_id = %thread_id, error = %e, "create_session 落库失败（继续返回）");
        }
    }

    // 域事件插座：session.created → 观察总线（audit/metrics）+ 声明订阅的插件
    // （capabilities.lifecycle_hooks 含 domain_event，fire-and-forget 不阻塞响应）。
    crate::plugin_lifecycle::broadcast_domain_event(
        &state,
        "session.created",
        vec![
            ("session_id", json!(thread_id.as_str())),
            ("pipeline_id", json!(pipeline_id.as_str())),
        ],
    )
    .await;

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

/// PATCH /api/v1/sessions/{id}/agent — 绑定/切换会话的主 Agent。
///
/// 前端 SessionEditModal 编辑会话时调此端点。
/// body: { "agent_id": "<id>" }。响应返回完整会话状态(含新 agent_id)。
/// 域2持久化：更新 sessions 表 agent_id（DB 无记录则跳过 DB，仅写内存）。
pub async fn update_session_agent_handler(
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
            return Json(session_to_session_json(&s));
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

/// PATCH /api/v1/sessions/{id} — 重命名/更新会话（title/intent/metadata）。
///
/// 前端 `updateSession()` 调此端点（session.ts 用 PATCH）。
/// body: { "intent": "<title>" }（前端把 title 映射成 intent），
/// 也兼容旧字段名 "title"。响应返回完整会话状态。
/// 域2持久化：更新 sessions 表 title/intent + updated_at（DB 无记录则仅回退内存响应）。
pub async fn update_session_handler(
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
            return Json(session_to_session_json(&s));
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

/// DELETE /api/v1/sessions/{id} — 删除会话（级联）。
///
/// 前端 `deleteSession()` 调此端点。
/// 级联删除该会话下全部管道（主管道 + 子任务管道，经 pipeline_sessions 映射表按
/// thread_id 定位）的 messages / execution_records / traces / branches /
/// pipeline_run_summaries / runs，最后清映射表与 sessions 行。blobs 不删（内容寻址去重）。
/// store 不可用或无记录仍返回 200（幂等，对齐 REST 删除语义）。
pub async fn delete_session_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Json<Value> {
    if let Some(store) = state.store.as_ref() {
        // 多租户：级联删除按 task_local tenant 过滤，需在请求租户 scope 内执行。
        // scope 缺失时（task_local 未设 → current_or_default("default")），
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

    // 域事件插座：session.deleted（语义：删除请求已受理；级联清理见 store）。
    crate::plugin_lifecycle::broadcast_domain_event(
        &state,
        "session.deleted",
        vec![("session_id", json!(id.as_str()))],
    )
    .await;

    Json(json!({ "thread_id": id, "deleted": true }))
}

/// GET /api/v1/sessions/{id}/messages — 按管道查询历史消息（消息层自治查询）。
///
/// 消息层只认 pipeline_id（= 其他项目的会话 id），不关心会话（thread）归属。
/// 前端 `getMessages` 已通过 `pipeline_run_id` 查询参数传管道 ID（session.ts），
/// 未传时回退到路径 id（兼容 thread_id == pipeline_id 的旧数据）。
///
/// sequence 按 pipeline_id 维度连续递增，支持 before_sequence/after_sequence 游标分页。
pub async fn list_session_messages_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    Query(q): Query<MessageListQuery>,
) -> Json<Value> {
    let Some(store) = state.store.as_ref() else {
        return Json(json!({ "messages": [], "total": 0, "has_more": false }));
    };

    // 传了 pipeline_run_id 就用它，否则回退路径 id
    let target_pid = q.pipeline_run_id.unwrap_or_else(|| id.clone());

    let opts = agentos_core::traits::MessageQueryOpts {
        before_sequence: q.before_sequence,
        after_sequence: q.after_sequence,
        limit: q.limit,
    };

    // 多租户：get_messages_by_pipeline 按 task_local tenant 过滤，需在请求租户 scope 内执行。
    // scope 缺失时（task_local 未设 → current_or_default("default")），永远只读 default 租户。
    let tenant_ctx = crate::server::request_tenant_ctx(state.store.as_ref(), &headers, &id).await;
    let store_clone = store.clone();
    let target_pid_for_scope = target_pid.clone();
    let records = match agentos_tenant::scope(tenant_ctx, async move {
        store_clone
            .get_messages_by_pipeline(&target_pid_for_scope, opts)
            .await
    })
    .await
    {
        Ok(r) => r,
        Err(e) => {
            tracing::warn!(pipeline_id = %target_pid, error = %e, "get_messages_by_pipeline 查询失败");
            return Json(json!({ "messages": [], "total": 0, "has_more": false }));
        }
    };

    // content 直接取 slot 行读时重建的 content_preview：存储收敛后（任务 7）
    // blob 存的是整条消息 JSON（含 role/content/tool_calls/reasoning_content 等），
    // slot_row_to_record 已从其中提取 content 字符串（content_preview 即全文，无截断），
    // 裸读 blob 字节会拿到整条消息 envelope（如 {"content":"...","role":"user"}）。
    let mut messages: Vec<Value> = Vec::with_capacity(records.len());
    for rec in records {
        let content = rec.content_preview.clone().unwrap_or_default();
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
        // 工具结果 envelope（role=tool）：解析 tool_result_json 返回结构化字段，
        // 前端据此还原 resultData（diff 徽标）/ durationMs / toolName /
        // containerTaskId——与流式 tool_result 事件的 result_data/duration_ms
        // 同源，保证冷热路径数据结构一致。历史行无 envelope 时不附带（前端回退 content）。
        if let Some(tr_json) = rec.tool_result_json.as_deref() {
            if let Ok(envelope) = serde_json::from_str::<Value>(tr_json) {
                let obj = msg.as_object_mut().expect("msg is object");
                obj.insert("toolResultData".into(), envelope["data"].clone());
                if let Some(dur) = envelope["duration_ms"].as_f64() {
                    obj.insert("toolDurationMs".into(), serde_json::json!(dur));
                }
                if let Some(name) = envelope["tool_name"].as_str() {
                    if !name.is_empty() {
                        obj.insert("toolName".into(), Value::String(name.to_string()));
                    }
                }
                if let Some(ctid) = envelope["metadata"]["container_task_id"].as_str() {
                    obj.insert("containerTaskId".into(), Value::String(ctid.to_string()));
                }
            }
        }
        // 思考内容：assistant 的 reasoning_content（LLM reasoning/chain-of-thought）。
        // 前端据此渲染思考过程折叠区。字段名 camelCase 对齐前端 BackendMessageResponse。
        if let Some(reasoning) = rec.reasoning_content.as_deref() {
            if !reasoning.is_empty() {
                msg.as_object_mut().expect("msg is object").insert(
                    "reasoningContent".into(),
                    Value::String(reasoning.to_string()),
                );
            }
        }
        messages.push(msg);
    }

    let total = messages.len();
    // has_more：若带 limit 且返回条数等于 limit，则可能还有更多
    let has_more = q.limit.map(|lim| total >= lim).unwrap_or(false);
    Json(json!({ "messages": messages, "total": total, "has_more": has_more }))
}

/// `GET /api/v1/sessions/{id}/messages` 查询参数。
///
/// 对齐前端 getMessages 的查询参数（session.ts）：
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

// ─── Sessions schema ──────────────────────────────────────────────────────

/// GET /api/v1/sessions/schema — 会话创建表单字段 schema（内置 + 插件聚合）。
///
/// 内置字段（标题/意图）+ 各 enabled 插件 `contributes.thread_fields` 聚合
/// （如 isolation 插件贡献 workspace / isolationMode）。前端新建会话表单
/// （SessionEditModal）据此渲染插件贡献字段，取值随线程创建写入 thread
/// metadata → execution_context。新增会话级字段 = 插件声明，前端无需改代码。
///
/// 路径挂 /api/v1/sessions/schema（threads→sessions 转正后 0.2 语义）；
/// 原 0.1 残留的 channel_api routes_threads `/api/v1/threads/schema` 无处理器。
pub async fn sessions_schema_handler(State(state): State<AppState>) -> Json<Value> {
    let mut fields: Vec<Value> = vec![
        json!({"name": "title", "type": "string", "label": "会话标题", "required": true}),
        json!({"name": "intent", "type": "string", "label": "意图描述"}),
    ];

    let enabled_ids = state.enabled_plugin_ids.read().await;
    let manifests = state.manifests.read().await;
    for m in manifests.iter() {
        if !enabled_ids.contains(&m.id) {
            continue;
        }
        let Some(contributes) = m.contributes.as_ref() else {
            continue;
        };
        let Some(list) = contributes.get("thread_fields").and_then(|v| v.as_array()) else {
            continue;
        };
        for f in list {
            // 只收带 name 的合法声明项，与 channel_api _collect_plugin_thread_fields 对齐
            if f.get("name").and_then(|n| n.as_str()).is_some() {
                fields.push(f.clone());
            }
        }
    }

    Json(json!({ "fields": fields }))
}

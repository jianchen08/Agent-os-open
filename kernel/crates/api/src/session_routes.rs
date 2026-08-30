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
use agentos_http::error::ApiError;

// ─── Sessions ─────────────────────────────────────────────────────────────

/// GET /api/v1/sessions — 会话列表。
///
/// 域2持久化：优先从 sessions 表读（重启后可恢复）。DB 查询失败 → 503
/// （对齐同文件 [`map_messages_query_failure`] 先例——「存储挂了」不得回退内存
/// registry 伪装成 200 陈旧列表，前端必须能区分故障与空）；**store 存在即权威**：
/// DB 空就是空列表，不回退内存 registry（清空执行数据后 sessions 表清空——回退
/// 会把内存"曾注册线程"以 null 标题出口，前端渲染成「未命名会话」幽灵）。
/// 仅 store 未配置（无持久化会话数据源）时回退内存 registry（显式兼容契约非吞错）。
/// 返回结构对齐前端 Session 类型。
pub async fn list_sessions_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let now = chrono::Utc::now().to_rfc3339();
    let mut threads: Vec<Value> = Vec::new();

    // 优先读 DB（持久化会话列表）。list_sessions 按 task_local tenant 过滤，
    // 需在请求租户 scope 内执行——scope 缺失回退 default 租户，跨租户读错位。
    // store 存在即权威：DB 空 = 无会话（不回退内存 registry 出口幽灵）。
    let mut db_authoritative = false;
    if let Some(store) = state.store.as_ref() {
        db_authoritative = true;
        let tenant_ctx =
            crate::server::request_tenant_ctx(state.store.as_ref(), &headers, "").await;
        let store_clone = store.clone();
        let tenant_id = tenant_ctx.tenant_id.clone();
        let sessions_result = agentos_tenant::scope(tenant_ctx, async move {
            let filter = agentos_core::traits::SessionListFilter {
                session_type: Some("main_pipeline".to_string()),
                limit: Some(100),
            };
            let sessions = store_clone.list_sessions(filter).await?;
            // 读面补全（ADR 2026-08-21）：子管道/任务管道创建时已按归属会话 link
            // 进 pipeline_sessions——把映射表 id 并入 pipeline_ids（保序、去重，
            // 主管道仍在前）。前端 findPipelineLocation 第二级"枚举 sessions[].pipelineIds
            // 找管道归属"对子管道至此也能命中，不再退化到当前会话误开标签。
            let mut with_children: Vec<agentos_core::types::SessionRecord> =
                Vec::with_capacity(sessions.len());
            for mut s in sessions {
                match store_clone
                    .list_pipeline_ids_by_thread(&s.thread_id, &tenant_id)
                    .await
                {
                    Ok(extras) => {
                        for extra in extras {
                            if !s.pipeline_ids.contains(&extra) {
                                s.pipeline_ids.push(extra);
                            }
                        }
                    }
                    Err(e) => {
                        tracing::warn!(
                            thread_id = %s.thread_id,
                            error = %e,
                            "list_pipeline_ids_by_thread 读映射表失败，pipeline_ids 不完整"
                        );
                    }
                }
                with_children.push(s);
            }
            Ok::<_, agentos_core::types::StorageError>(with_children)
        })
        .await;
        match sessions_result {
            Ok(sessions) if !sessions.is_empty() => {
                for s in sessions {
                    threads.push(session_to_session_json(&s));
                }
            }
            Ok(_) => {}
            Err(e) => return Err(map_sessions_list_failure(e)),
        }
    }

    // 仅 store 未配置（无持久化会话数据源）时回退内存 registry（兼容场景）
    if !db_authoritative {
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
    Ok(Json(json!({ "threads": threads, "total": total })))
}

/// 会话列表查询失败 → ApiError（对齐 [`map_messages_query_failure`] 先例：DB
/// 故障不得回退内存 registry 把陈旧列表伪装成 200 假成功）。
///
/// 存储依赖故障 → 503（前端可渲染错误态并重试）；细节留服务端 warn（含底层
/// 错误串），对外文案不泄漏内部结构。
fn map_sessions_list_failure(e: agentos_core::types::StorageError) -> ApiError {
    tracing::warn!(
        error = %e,
        "list_sessions 查询失败，返回 503（不再回退内存 registry 假成功）"
    );
    ApiError::ServiceUnavailable {
        message: "会话列表暂时不可用（存储异常），请稍后重试".to_string(),
    }
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
    // 取前 12 位 hex：与任务管道（chat_send_handler）同格式，全链路短 id 统一。
    let pipeline_id = uuid::Uuid::new_v4().simple().to_string()[..12].to_string();
    let title = body.get("title").and_then(|v| v.as_str()).unwrap_or("");
    let intent = body
        .get("intent")
        .and_then(|v| v.as_str())
        .or(if title.is_empty() { None } else { Some(title) });
    let agent_id = body.get("agent_id").cloned().unwrap_or(json!("agentos"));

    // A14：user_id 以 token（resolve_request_user）解析为准——body 里的 user_id
    // 是客户端可伪造字段，不得作为事件路由（WS 推送定位）的信任源；仅在无 token
    // 用户时作显式降级（warn 标记，供审计区分）。
    let token_user = agentos_http::auth::resolve_request_user(state.store.as_ref(), &headers)
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
    // + thread → agent 绑定（热路径解析；2026-08-24 阶段1：创建即绑定，
    // PATCH 切换同步三写）
    if let Some(session) = &state.session {
        session.registry().register_thread(&thread_id, &user_id);
        session
            .registry()
            .register_thread_pipeline(&thread_id, &pipeline_id);
        session
            .registry()
            .register_thread_agent(&thread_id, agent_id.as_str().unwrap_or("agentos"));
    }

    // 域2持久化：会话落 sessions 表（对齐 0.1 SessionModel）。
    // 内存在 registry 仍保留（WS 路由用），DB 负责跨重启恢复，create 时双写。
    // metadata 与默认值合并（body 字段优先，缺失补默认）：前端会传
    // isolation_mode 等自定义字段，整体替换会丢掉 session_type，导致
    // 会话列表按 session_type='main_pipeline' 过滤时看不到新会话。
    let mut metadata = body
        .get("metadata")
        .cloned()
        .unwrap_or_else(|| json!({ "session_type": "main_pipeline", "user_id": user_id }));
    if let Some(m) = metadata.as_object_mut() {
        m.entry("session_type")
            .or_insert_with(|| json!("main_pipeline"));
        m.entry("user_id").or_insert_with(|| json!(user_id));
    }
    if let Some(store) = state.store.as_ref() {
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
            // 出生绑定落管道 state（2026-08-24 阶段1：agent.id 真值随会话出生
            // 即落库，创建时指定 agent 或默认 agentos 都持久化）
            if let Some(aid) = agent_id_str {
                store_clone
                    .upsert_state_field(&pid_clone, &tenant_id, "agent.id", &json!(aid))
                    .await?;
            }
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
            ("session_id".to_string(), json!(thread_id.as_str())),
            ("pipeline_id".to_string(), json!(pipeline_id.as_str())),
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
        "metadata": metadata,
    }))
}

/// PATCH /api/v1/sessions/{id}/agent — 绑定/切换会话的主 Agent。
///
/// 前端 SessionEditModal 编辑会话时调此端点。
/// body: { "agent_id": "<id>" }。响应返回完整会话状态(含新 agent_id)。
/// 域2持久化（2026-08-24 阶段1：三写）：
/// ① registry 线程绑定（内存热路径消费）；
/// ② sessions 表 agent_id（跨重启 DB 冷兜底，DB 无记录则跳过 DB）；
/// ③ 主管道 state `agent.id`（绑定真值落管道 state，管道自足）。
/// ③ 失败仅 warn 不阻断——registry/DB 已更新，执行面仍可解析。
pub async fn update_session_agent_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
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

    // ③ 主管道 state 持久化真值（2026-08-24 阶段1：绑定真值落管道 state，
    // 管道自足——执行面冷恢复/未来动态管道消费 agent.id）。失败仅 warn
    // 不阻断：registry/DB 已更新，执行面解析仍可命中。
    if let Some(store) = state.store.as_ref() {
        let tenant_id = agentos_http::auth::resolve_request_tenant_id(Some(store), &headers).await;
        let pipeline_id = state
            .session
            .as_ref()
            .and_then(|s| s.registry().get_pipeline_for_thread(&id));
        if let Some(pid) = pipeline_id {
            if let Err(e) = store
                .upsert_state_field(&pid, &tenant_id, "agent.id", &json!(agent_id))
                .await
            {
                tracing::warn!(
                    thread_id = %id,
                    pipeline = %pid,
                    error = %e,
                    "会话 agent 绑定写入管道 state 失败（registry/DB 已更新）"
                );
            }
        }
    }

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
/// body: { "intent": "<title>" }（前端把 title 映射成 intent）。响应返回完整会话状态。
/// 域2持久化：更新 sessions 表 title/intent + updated_at（DB 无记录则仅回退内存响应）。
pub async fn update_session_handler(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> Json<Value> {
    // 前端 updateSession 把 title 放进 intent 字段。
    let new_intent = body
        .get("intent")
        .and_then(|v| v.as_str())
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
            let deleted = store_clone.delete_session(&id_for_scope).await;
            // DB 已净而内存 state 注册表残留会让 /pipelines/state 继续出口幽灵行
            // （管理页「删了还在」的另一半）——按实际删除清单逐出。租户解析与
            // store 同源（scope 内 current_or_default）。
            if let Ok(deleted_ids) = &deleted {
                let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
                let registry = agentos_session::pipeline_state_registry::global_registry();
                for pid in deleted_ids {
                    registry.remove(&tenant_id, pid);
                }
            }
            deleted
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
        vec![("session_id".to_string(), json!(id.as_str()))],
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
) -> Result<Json<Value>, ApiError> {
    let Some(store) = state.store.as_ref() else {
        return Ok(Json(
            json!({ "messages": [], "total": 0, "has_more": false }),
        ));
    };

    // 传了 pipeline_run_id 就用它，否则回退路径 id；再回退线程的 active_pipeline_id
    // （GAP-1 统一修复：thread_id 与 pipeline_id 不同值时按 active 管道查——
    // 引擎落库用 resolve 后的 active_pipeline_id，直接拿 thread_id 查会落空）。
    // 会话查询按租户过滤：request_tenant_ctx 之后的 scope 内执行（task_local 缺失
    // 时 current_or_default("default") 会把非 default 租户的会话查成 None）。
    let tenant_ctx = crate::server::request_tenant_ctx(state.store.as_ref(), &headers, &id).await;
    let explicit_pid = q.pipeline_run_id.clone();
    let mut target_pid = explicit_pid.unwrap_or_else(|| id.clone());
    if q.pipeline_run_id.is_none() {
        let store_for_sess = store.clone();
        let id_for_sess = id.clone();
        let sess = agentos_tenant::scope(tenant_ctx.clone(), async move {
            store_for_sess.get_session(&id_for_sess).await
        })
        .await;
        if let Ok(Some(sess)) = sess {
            if let Some(active) = sess.active_pipeline_id.as_deref() {
                if !active.is_empty() {
                    target_pid = active.to_string();
                }
            }
        }
    }

    let opts = agentos_core::traits::MessageQueryOpts {
        before_sequence: q.before_sequence,
        after_sequence: q.after_sequence,
        limit: q.limit,
    };

    // 多租户：get_messages_by_pipeline 按 task_local tenant 过滤，需在请求租户 scope 内执行。
    // scope 缺失时（task_local 未设 → current_or_default("default")），永远只读 default 租户。
    let tenant_id = tenant_ctx.tenant_id.clone();
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
        Err(e) => return Err(map_messages_query_failure(&target_pid, e)),
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
        // 自定义元数据原样回显（ADR 2026-08-21 消息幂等契约）：user 消息的
        // metadata.client_message_id 是前端乐观消息对账去重的唯一桥接键。
        if let Some(meta) = rec.metadata.as_ref() {
            if meta.is_object() {
                msg.as_object_mut()
                    .expect("msg is object")
                    .insert("metadata".into(), meta.clone());
            }
        }
        messages.push(msg);
    }

    let total = messages.len();
    // has_more：若带 limit 且返回条数等于 limit，则可能还有更多
    let has_more = q.limit.map(|lim| total >= lim).unwrap_or(false);

    // 存活中间态（ADR 2026-08-27 §2.6 前端刷新恢复）：F5 后从后端内存寄存器
    // 恢复流式中间态（重建占位气泡）。纯增字段向后兼容；寄存器无该管道
    // 条目时为空数组（不改变既有响应形状）。租户 = 请求解析租户（与
    // get_messages_by_pipeline 同源）。
    let transient_states: Vec<Value> = agentos_engine::global_registry()
        .list(&tenant_id, &target_pid)
        .into_iter()
        .map(|(key, value, _updated_at)| json!({ "key": key, "value": value }))
        .collect();

    Ok(Json(json!({
        "messages": messages,
        "total": total,
        "has_more": has_more,
        "transient_states": transient_states,
    })))
}

/// 历史消息查询失败 → ApiError（K3：不得转成 200 空数组，否则前端无法区分
/// 「新会话无历史」与「查询挂了」，对话历史"消失"无迹可查）。
///
/// DB 断连/租户错配属存储依赖故障 → 503（前端可渲染错误态并重试）；
/// 细节留服务端 warn（含 pipeline_id + 底层错误串），对外文案不泄漏内部结构。
fn map_messages_query_failure(pipeline_id: &str, e: agentos_core::types::StorageError) -> ApiError {
    tracing::warn!(
        pipeline_id = %pipeline_id,
        error = %e,
        "get_messages_by_pipeline 查询失败，返回 503（不再伪装成空历史）"
    );
    ApiError::ServiceUnavailable {
        message: "消息历史查询暂时不可用（存储异常），请稍后重试".to_string(),
    }
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

#[cfg(test)]
mod agent_binding_tests {
    //! 会话 agent 绑定三写集成测试（2026-08-24 阶段1：绑定真值落管道 state）。
    //!
    //! 设计依据：docs/working/管道配置输入契约与动态管道能力设计_20260824.md
    //! §4.3——PATCH /sessions/{id}/agent 必须三写：registry 线程绑定（热路径
    //! 消费）+ sessions 表 agent_id（冷路径 DB 兜底）+ 主管道 state `agent.id`
    //! （持久化真值）。执行面解析（显式 → registry → DB → agentos）由
    //! ws_session::resolve_dispatch_agent 单测覆盖，本模块验证写入面三方落齐。

    use super::*;
    use agentos_core::traits::StorageBackend;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    const ADMIN_USER_ID: &str = "00000000-0000-0000-0000-000000000001";

    async fn setup() -> (
        axum::Router,
        AppState,
        std::sync::Arc<agentos_engine::SqliteStore>,
    ) {
        let store = std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        store
            .create_user(&agentos_core::types::UserRecord {
                user_id: ADMIN_USER_ID.to_string(),
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
        state.db = Some(store.clone());
        state.session = Some(std::sync::Arc::new(
            agentos_session::SessionCoordinator::new(),
        ));
        (crate::server::build_router(state.clone()), state, store)
    }

    async fn admin_token(router: &axum::Router) -> String {
        let resp = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
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
        let json: Value = serde_json::from_slice(&body).unwrap();
        json["access_token"].as_str().unwrap().to_string()
    }

    async fn create_and_patch_agent(
        router: &axum::Router,
        state: &AppState,
        token: &str,
        create_agent: Option<&str>,
        patch_agent: &str,
    ) -> (String, String) {
        let body = match create_agent {
            Some(a) => json!({"agent_id": a, "intent": "t"}),
            None => json!({"intent": "t"}),
        };
        let create = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/sessions")
                    .header("content-type", "application/json")
                    .header("authorization", format!("Bearer {token}"))
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(create.status(), 200, "create_session 应成功");
        let created: Value = serde_json::from_slice(
            &axum::body::to_bytes(create.into_body(), 8192)
                .await
                .unwrap(),
        )
        .unwrap();
        let thread_id = created["thread_id"].as_str().unwrap().to_string();
        let pipeline_id = created["active_pipeline_id"]
            .as_str()
            .unwrap_or_else(|| created["pipeline_ids"][0].as_str().unwrap())
            .to_string();
        // 补注册 pipeline↔thread 映射（生产路径 F8 link 已落，here 同步 registry）
        let registry = state.session.as_ref().unwrap().registry().clone();
        registry.register_thread(&thread_id, ADMIN_USER_ID);
        registry.register_thread_pipeline(&thread_id, &pipeline_id);

        let patch = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("PATCH")
                    .uri(format!("/api/v1/sessions/{thread_id}/agent"))
                    .header("content-type", "application/json")
                    .header("authorization", format!("Bearer {token}"))
                    .body(Body::from(json!({"agent_id": patch_agent}).to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(patch.status(), 200, "PATCH agent 应成功");
        let patched: Value =
            serde_json::from_slice(&axum::body::to_bytes(patch.into_body(), 8192).await.unwrap())
                .unwrap();
        assert_eq!(patched["agent_id"].as_str(), Some(patch_agent));
        (thread_id, pipeline_id)
    }

    #[tokio::test]
    async fn create_then_patch_agent_writes_registry_db_and_pipeline_state() {
        let (router, state, store) = setup().await;
        let token = admin_token(&router).await;
        let (thread_id, pipeline_id) =
            create_and_patch_agent(&router, &state, &token, None, "general_agent").await;

        // ① registry 热绑定
        let registry = state.session.as_ref().unwrap().registry();
        assert_eq!(
            registry.get_agent_for_thread(&thread_id).as_deref(),
            Some("general_agent"),
            "registry 线程绑定必须同步"
        );

        // ② sessions 表冷兜底
        let session = store.get_session(&thread_id).await.unwrap().unwrap();
        assert_eq!(
            session.agent_id.as_deref(),
            Some("general_agent"),
            "sessions 表 agent_id 必须同步"
        );

        // ③ 主管道 state 持久化真值（阶段 1 核心断言）
        let store_obj: std::sync::Arc<dyn StorageBackend> = store.clone();
        let tenant_id =
            agentos_http::auth::resolve_tenant_id_by_user(Some(&store_obj), ADMIN_USER_ID).await;
        let state_fields = store_obj
            .load_pipeline_state(&pipeline_id, &tenant_id)
            .await
            .unwrap();
        assert_eq!(
            state_fields.get("agent.id").and_then(|v| v.as_str()),
            Some("general_agent"),
            "主管道 state agent.id 必须落库（绑定真值进管道 state）"
        );
    }

    #[tokio::test]
    async fn create_session_seeds_agent_id_into_pipeline_state() {
        // 创建时即选 agent → 出生绑定直接落 state（不必等 PATCH）
        let (router, state, store) = setup().await;
        let token = admin_token(&router).await;
        let (_, pipeline_id) = create_and_patch_agent(
            &router,
            &state,
            &token,
            Some("general_agent"),
            "general_agent",
        )
        .await;

        let store_obj: std::sync::Arc<dyn StorageBackend> = store.clone();
        let tenant_id =
            agentos_http::auth::resolve_tenant_id_by_user(Some(&store_obj), ADMIN_USER_ID).await;
        let state_fields = store_obj
            .load_pipeline_state(&pipeline_id, &tenant_id)
            .await
            .unwrap();
        assert_eq!(
            state_fields.get("agent.id").and_then(|v| v.as_str()),
            Some("general_agent"),
            "出生绑定（创建时指定的 agent_id）必须落管道 state"
        );
    }
}

#[cfg(test)]
mod messages_query_tests {
    //! K3：历史消息查询失败 → ApiError::ServiceUnavailable（503），不再伪装成
    //! 200 空数组（前端无法区分「新会话无历史」与「查询挂了」）。

    use super::*;

    #[test]
    fn map_messages_query_failure_yields_service_unavailable() {
        let e = map_messages_query_failure(
            "pipe-1",
            agentos_core::types::StorageError::Database("connection lost".to_string()),
        );
        match e {
            ApiError::ServiceUnavailable { message } => {
                assert!(!message.is_empty(), "对外文案非空（前端渲染错误态）");
            }
            other => panic!("DB 查询失败应映射 503，实际 {other:?}"),
        }
    }

    #[test]
    fn map_messages_query_failure_covers_all_storage_error_kinds() {
        // 各类存储错误（NotFound 之外的故障形态）都不得回落 200 空数组
        for e in [
            agentos_core::types::StorageError::Database("db".to_string()),
            agentos_core::types::StorageError::Io("io".to_string()),
            agentos_core::types::StorageError::Serialization("ser".to_string()),
        ] {
            assert!(matches!(
                map_messages_query_failure("p", e),
                ApiError::ServiceUnavailable { .. }
            ));
        }
    }
}

#[cfg(test)]
mod sessions_list_tests {
    //! 扫描 2026-08-27 辖区一 Should#6：list_sessions DB 故障 → 503，不再回退
    //! 内存 registry 把「存储挂了」伪装成 200 陈旧列表（假成功）；store 存在即
    //! 权威——DB 空 = 空列表（清空执行数据后不回退内存 registry 出口未命名幽灵，
    //! 2026-08-30 修正），仅 store 未配置（无持久化数据源）时回退内存 registry
    //! （显式兼容语义非吞错）。

    use super::*;
    use agentos_core::traits::StorageBackend;
    use axum::extract::State;

    /// list_sessions 恒故障的存储 mock。其余必需方法以 unreachable! 桩实现——
    /// 错误路径若意外耦合了其他存储调用会在此炸出（比静默 NotFound 更诚实）。
    struct ErrListStore;

    #[async_trait::async_trait]
    impl StorageBackend for ErrListStore {
        async fn list_sessions(
            &self,
            _filter: agentos_core::traits::SessionListFilter,
        ) -> Result<Vec<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            Err(agentos_core::types::StorageError::Database(
                "injected sessions list failure".to_string(),
            ))
        }
        async fn get_run(
            &self,
            _run_id: &str,
        ) -> Result<agentos_core::types::RunRecord, agentos_core::types::StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn get_messages_by_pipeline(
            &self,
            _pipeline_id: &str,
            _opts: agentos_core::traits::MessageQueryOpts,
        ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError>
        {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn get_blob(
            &self,
            _blob_id: &str,
        ) -> Result<Vec<u8>, agentos_core::types::StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn append_trace(
            &self,
            _entry: agentos_core::types::TraceEntry,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn create_branch(
            &self,
            _branch: agentos_core::types::Branch,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn update_run_status(
            &self,
            _run_id: &str,
            _status: agentos_core::types::RunStatus,
            _branch: Option<&str>,
            _seq: Option<u32>,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn create_run(
            &self,
            _run_id: &str,
            _config_hash: &str,
            _tenant_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn store_blob(
            &self,
            _data: &[u8],
            _mime_type: &str,
        ) -> Result<String, agentos_core::types::StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn create_session(
            &self,
            _session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn get_session(
            &self,
            _thread_id: &str,
        ) -> Result<Option<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn update_session(
            &self,
            _session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn delete_session(
            &self,
            _thread_id: &str,
        ) -> Result<Vec<String>, agentos_core::types::StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn link_pipeline_session(
            &self,
            _pipeline_id: &str,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn list_pipeline_ids_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<String>, agentos_core::types::StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn get_step_traces_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError>
        {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn create_user(
            &self,
            _user: &agentos_core::types::UserRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn get_user_by_id(
            &self,
            _user_id: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn get_user_by_username(
            &self,
            _username: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn list_users(
            &self,
        ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn update_last_login(
            &self,
            _user_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
        async fn delete_user(
            &self,
            _user_id: &str,
        ) -> Result<bool, agentos_core::types::StorageError> {
            unreachable!("list_sessions 错误路径不应触碰其他存储方法")
        }
    }

    #[tokio::test]
    async fn db_error_returns_503_not_stale_registry_list() {
        // registry 预置陈旧线程：修复前该数据会以 200「成功」出口。
        let mut state = AppState::new();
        state.store = Some(std::sync::Arc::new(ErrListStore) as std::sync::Arc<dyn StorageBackend>);
        state.session = Some(std::sync::Arc::new(
            agentos_session::SessionCoordinator::new(),
        ));
        state
            .session
            .as_ref()
            .unwrap()
            .registry()
            .register_thread("thread-stale-1", "u1");

        let resp = list_sessions_handler(State(state), HeaderMap::new()).await;

        match resp {
            Ok(body) => panic!("DB 故障不得回退内存 registry 假成功，实际 {:?}", body.0),
            Err(e) => assert!(
                matches!(e, ApiError::ServiceUnavailable { .. }),
                "DB 故障应映射 503，实际 {e:?}"
            ),
        }
    }

    #[tokio::test]
    async fn empty_db_returns_empty_list_with_200() {
        // DB 正常但空 = 无会话（store 存在即权威，不回退内存 registry 出口
        // title=null 幽灵——清空执行数据后 sessions 表清空即此场景）。
        let store = std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let mut state = AppState::new();
        state.store = Some(store as std::sync::Arc<dyn StorageBackend>);
        state.session = Some(std::sync::Arc::new(
            agentos_session::SessionCoordinator::new(),
        ));
        state
            .session
            .as_ref()
            .unwrap()
            .registry()
            .register_thread("thread-mem-1", "u1");

        let resp = list_sessions_handler(State(state), HeaderMap::new())
            .await
            .expect("DB 空属正常场景应 200");

        assert_eq!(resp.0["total"], 0, "DB 空不得回退内存 registry: {}", resp.0);
        assert!(
            resp.0["threads"].as_array().unwrap().is_empty(),
            "threads 应为空: {}",
            resp.0
        );
    }

    #[test]
    fn map_sessions_list_failure_covers_storage_error_kinds() {
        for e in [
            agentos_core::types::StorageError::Database("db".to_string()),
            agentos_core::types::StorageError::Io("io".to_string()),
            agentos_core::types::StorageError::Serialization("ser".to_string()),
        ] {
            assert!(matches!(
                map_sessions_list_failure(e),
                ApiError::ServiceUnavailable { .. }
            ));
        }
    }
}

#[cfg(test)]
mod transient_states_query_tests {
    //! ADR 2026-08-27 §2.6：消息读取接口带出存活中间态（前端 F5 刷新恢复）。
    //! 纯增字段向后兼容：寄存器无该管道条目时为空数组，不改变既有响应形状。

    use super::*;

    #[tokio::test]
    async fn messages_response_carries_live_transient_states() {
        let store = std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let mut state = AppState::new();
        state.store = Some(store.clone());
        state.db = Some(store.clone());
        let reg = agentos_engine::global_registry();
        let pipe = "pipe_transient_msg";
        reg.set("default", pipe, "chunk:a_abc", json!({"text_len": 12}));
        reg.set("default", pipe, "progress:1", json!({"pct": 40}));

        let resp = list_session_messages_handler(
            State(state),
            HeaderMap::new(),
            Path(pipe.to_string()),
            Query(MessageListQuery {
                pipeline_run_id: Some(pipe.to_string()),
                before_sequence: None,
                after_sequence: None,
                limit: None,
            }),
        )
        .await
        .unwrap();
        let body = resp.0;
        assert_eq!(body["total"], json!(0), "无历史消息");
        assert_eq!(body["has_more"], json!(false));
        // 存活中间态带出（key/value 对，供前端重建流式占位）
        let states = body["transient_states"].as_array().unwrap();
        assert_eq!(states.len(), 2);
        let keys: Vec<&str> = states.iter().filter_map(|s| s["key"].as_str()).collect();
        assert!(keys.contains(&"chunk:a_abc"));
        assert!(keys.contains(&"progress:1"));
        let chunk = states.iter().find(|s| s["key"] == "chunk:a_abc").unwrap();
        assert_eq!(chunk["value"]["text_len"], json!(12));
        reg.clear_pipeline("default", pipe);
    }

    #[tokio::test]
    async fn messages_response_empty_transient_when_none() {
        let store = std::sync::Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        let mut state = AppState::new();
        state.store = Some(store.clone());
        state.db = Some(store.clone());
        // 未写任何中间态的管道 → 空数组（纯增字段向后兼容）
        let resp = list_session_messages_handler(
            State(state),
            HeaderMap::new(),
            Path("pipe_empty_transient".to_string()),
            Query(MessageListQuery {
                pipeline_run_id: Some("pipe_empty_transient".to_string()),
                before_sequence: None,
                after_sequence: None,
                limit: None,
            }),
        )
        .await
        .unwrap();
        let states = resp.0["transient_states"].as_array().unwrap();
        assert!(states.is_empty(), "无中间态时为空数组而非缺字段");
    }
}

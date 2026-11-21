// @feature: FP-0.2.WS停止清除 派发链收尾清除 pending_cancel（session_routes 会话面） | @ci: rust-test
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

use agentos_core::traits::{MessageQueryOpts, SessionListFilter};
#[cfg(test)]
use agentos_core::types::{MessageRecord, RunRecord, TraceEntry, UserRecord};
use agentos_core::types::{SessionRecord, StorageError};
use agentos_http::error::ApiError;
use axum::{
    extract::{Path, Query, State},
    http::HeaderMap,
    Json,
};
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::Arc;

use crate::routes::AppState;

// ─── Sessions ─────────────────────────────────────────────────────────────

/// 会话创建公共核的产物坐标（HTTP 面与能力面共用，ADR
/// 2026-11-03-pim-trigger-anchor：日程承载兜底需要 sidecar 可调的
/// session.create 能力——创建逻辑单源，两入口不得各持一份漂移）。
pub(crate) struct SessionCreateOutcome {
    pub thread_id: String,
    pub pipeline_id: String,
    pub created_at: String,
    pub metadata: Value,
    pub title: String,
    pub intent: Option<String>,
    pub agent_id: Value,
}

/// 会话创建公共核：thread/pipeline 生成 → registry 三注册 → sessions 表双写
/// → 主管道映射 → agent.id 出生绑定（fail-closed）→ 标题出生落键 → 域事件广播。
///
/// 调用方（HTTP handler / capability 臂 `session.create`）只差身份来源：
/// HTTP 面 user/tenant 由 token 解析，能力面由执行上下文注入（反调面身份
/// 契约，ADR 2026-10-17；空身份由调用方 fail-fast 拦截）。
///
/// 错误口径：agent.id 绑定写失败（fail-closed）→ Err；sessions 落库失败
/// （registry 内存会话可用，warn 继续）→ Ok——与既有 HTTP 语义逐条对齐。
pub(crate) async fn create_session_core(
    store: Option<&Arc<dyn agentos_core::traits::StorageBackend>>,
    session: Option<&Arc<agentos_session::SessionCoordinator>>,
    tenant_id: &str,
    user_id: &str,
    body: &Value,
    broadcast: Option<&crate::capability_router::DomainBroadcaster>,
) -> Result<SessionCreateOutcome, String> {
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
        .or(if title.is_empty() { None } else { Some(title) })
        .map(str::to_string);
    let agent_id = body.get("agent_id").cloned().unwrap_or(json!("agentos"));

    // 注册 thread → user 映射（WS 流式推送据此定位连接）+ thread → pipeline 映射
    // + thread → agent 绑定（热路径解析；2026-08-24 阶段1：创建即绑定，
    // PATCH 切换同步三写）
    if let Some(session) = session {
        session.registry().register_thread(&thread_id, user_id);
        session
            .registry()
            .register_thread_pipeline(&thread_id, &pipeline_id);
        session
            .registry()
            .register_thread_agent(&thread_id, agent_id.as_str().unwrap_or("agentos"));
    }

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
    if let Some(store) = store {
        let agent_id_str = agent_id
            .as_str()
            .and_then(|s| if s.is_empty() { None } else { Some(s) });
        let session_rec = SessionRecord {
            thread_id: thread_id.clone(),
            title: if title.is_empty() {
                None
            } else {
                Some(title.to_string())
            },
            intent: intent.clone(),
            current_state: "active".to_string(),
            agent_id: agent_id_str.map(|s| s.to_string()),
            active_pipeline_id: Some(pipeline_id.clone()),
            pipeline_ids: vec![pipeline_id.clone()],
            metadata: Some(metadata.clone()),
            created_at: now.clone(),
            updated_at: now.clone(),
            last_active_at: Some(now.clone()),
        };
        // 创建时已带标题 → 管道标题出生写入的源值（闭包 move session_rec，先取）
        let initial_title = session_rec.title.clone();
        let store_clone = store.clone();
        let pid_clone = pipeline_id.clone();
        let tid_clone = thread_id.clone();
        let tenant_ctx = agentos_core::types::TenantContext::new(tenant_id, &thread_id);
        let tenant_ctx_for_bind = tenant_ctx.clone();
        let tenant_ctx_for_title = tenant_ctx.clone();
        let create_result = agentos_tenant::scope(tenant_ctx, async move {
            store_clone.create_session(&session_rec).await?;
            // 主管道兜底：创建会话即写映射，保证主管道即使未跑过也有映射（删会话级联不漏）。
            let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
            store_clone
                .link_pipeline_session(&pid_clone, &tid_clone, &tenant_id)
                .await?;
            Ok::<(), StorageError>(())
        })
        .await;
        match create_result {
            Err(e) => {
                // 会话行/映射未落库：冷恢复后本就不存在，无绑定真值可丢——
                // 创建可用性按既有语义 warn 继续返回（registry 内存会话可用）。
                tracing::warn!(
                    thread_id = %thread_id,
                    error = %e,
                    "create_session 落库失败（继续返回）"
                );
            }
            Ok(()) => {
                // 出生绑定真值落管道 state（2026-08-24 阶段1：agent.id 随会话
                // 出生即落库，创建时指定 agent 或默认 agentos 都持久化）。F2
                // 收敛为 fail-closed：会话行已持久化而绑定真值缺失 = 重启后冷
                // 恢复丢绑定，故写失败即创建失败（503 可重试——重试得到新会话，
                // 已落库行为孤立行）。
                if let Some(aid) = agent_id_str {
                    let store_bind = store.clone();
                    let pid_bind = pipeline_id.clone();
                    let bound = agentos_tenant::scope(tenant_ctx_for_bind, async move {
                        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
                        store_bind
                            .upsert_state_field(&pid_bind, &tenant_id, "agent.id", &json!(aid))
                            .await
                    })
                    .await;
                    if let Err(e) = bound {
                        tracing::error!(
                            thread_id = %thread_id,
                            pipeline = %pipeline_id,
                            error = %e,
                            "出生 agent 绑定写入管道 state 失败（创建失败，请重试）"
                        );
                        return Err("会话创建失败（agent 绑定持久化异常），请稍后重试".to_string());
                    }
                }
                // 管道标题出生写入（ADR 2026-10-27-pipeline-title-field）：
                // 统一管道标题键（state.title，引擎域），创建即带标题时随出生
                // 落键；空标题不写——首条消息派生标题经 update_session 传播。
                // 显示元数据非可用性关键路径：写失败显式 warn 留痕（面板回退
                // ID 显示），不 fail 创建。
                if let Some(t) = initial_title.filter(|s| !s.is_empty()) {
                    let store_title = store.clone();
                    let pid_title = pipeline_id.clone();
                    let wrote = agentos_tenant::scope(tenant_ctx_for_title, async move {
                        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
                        store_title
                            .upsert_state_field(&pid_title, &tenant_id, "title", &json!(t))
                            .await
                    })
                    .await;
                    if let Err(e) = wrote {
                        tracing::warn!(
                            thread_id = %thread_id,
                            pipeline = %pipeline_id,
                            error = %e,
                            "管道标题出生写入失败（面板暂回退 ID 显示）"
                        );
                    }
                }
            }
        }
    }

    // 域事件插座：session.created → 观察总线（audit/metrics）+ 声明订阅的插件
    // （capabilities.lifecycle_hooks 含 domain_event，fire-and-forget 不阻塞响应）。
    if let Some(emit) = broadcast {
        emit(
            "session.created",
            vec![
                ("session_id".to_string(), json!(thread_id.as_str())),
                ("pipeline_id".to_string(), json!(pipeline_id.as_str())),
            ],
        );
    }

    Ok(SessionCreateOutcome {
        thread_id,
        pipeline_id,
        created_at: now,
        metadata,
        title: title.to_string(),
        intent,
        agent_id,
    })
}

/// POST /api/v1/sessions — 创建会话（HTTP 面薄壳：token 解析身份后走
/// [`create_session_core`]，与能力面 `session.create` 单源）。
pub async fn create_session_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Result<Json<Value>, ApiError> {
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
                token_user = ?token_user,
                body_user = ?body_uid,
                "create_session：body user_id 与 token 用户不一致，事件路由按 token 用户"
            );
            token_user.clone().unwrap_or_default()
        }
        (Some(_token_uid), _) => token_user.clone().unwrap_or_default(),
        (None, Some(body_uid)) => {
            tracing::warn!(
                body_user = %body_uid,
                "create_session：无 token 用户，降级使用 body user_id（可伪造，仅测试/嵌入式场景）"
            );
            body_uid.clone()
        }
        (None, None) => "anonymous".to_string(),
    };
    let tenant_ctx = crate::server::request_tenant_ctx(state.store.as_ref(), &headers, "").await?;
    let outcome = create_session_core(
        state.store.as_ref(),
        state.session.as_ref(),
        &tenant_ctx.tenant_id,
        &user_id,
        &body,
        None,
    )
    .await
    .map_err(|message| ApiError::ServiceUnavailable { message })?;

    // 域事件插座（HTTP 面原位广播，async emit；能力面走 core 的 broadcast 闭包）
    crate::plugin_lifecycle::broadcast_domain_event(
        &state,
        "session.created",
        vec![
            ("session_id".to_string(), json!(outcome.thread_id.as_str())),
            (
                "pipeline_id".to_string(),
                json!(outcome.pipeline_id.as_str()),
            ),
        ],
    )
    .await;

    Ok(Json(json!({
        "thread_id": outcome.thread_id,
        "created_at": outcome.created_at,
        "updated_at": outcome.created_at,
        "current_state": "active",
        "intent": outcome.intent,
        "title": if outcome.title.is_empty() { Value::Null } else { json!(outcome.title) },
        "agent_id": outcome.agent_id,
        "pipeline_ids": [outcome.pipeline_id],
        "active_pipeline_id": outcome.pipeline_id,
        "message_count": 0,
        "metadata": outcome.metadata,
    })))
}
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
            crate::server::request_tenant_ctx(state.store.as_ref(), &headers, "").await?;
        let store_clone = store.clone();
        let tenant_id = tenant_ctx.tenant_id.clone();
        let sessions_result = agentos_tenant::scope(tenant_ctx, async move {
            let filter = SessionListFilter {
                session_type: Some("main_pipeline".to_string()),
                limit: Some(100),
            };
            let sessions = store_clone.list_sessions(filter).await?;
            // 读面补全（ADR 2026-08-21）：子管道/任务管道创建时已按归属会话 link
            // 进 pipeline_sessions——把映射表 id 并入 pipeline_ids（保序、去重，
            // 主管道仍在前）。前端 findPipelineLocation 第二级"枚举 sessions[].pipelineIds
            // 找管道归属"对子管道至此也能命中，不再退化到当前会话误开标签。
            let mut with_children: Vec<SessionRecord> = Vec::with_capacity(sessions.len());
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
            Ok::<_, StorageError>(with_children)
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
fn map_sessions_list_failure(e: StorageError) -> ApiError {
    tracing::warn!(
        error = %e,
        "list_sessions 查询失败，返回 503（不再回退内存 registry 假成功）"
    );
    ApiError::ServiceUnavailable {
        message: "会话列表暂时不可用（存储异常），请稍后重试".to_string(),
    }
}

/// SessionRecord → 前端 Session JSON（对齐 mappers.ts:12-35）。
fn session_to_session_json(s: &SessionRecord) -> Value {
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

/// PATCH /api/v1/sessions/{id}/agent — 绑定/切换会话的主 Agent。
///
/// 前端 SessionEditModal 编辑会话时调此端点。
/// body: { "agent_id": "<id>" }。响应返回完整会话状态(含新 agent_id)。
/// 域2持久化（绑定单真值 + 投影，F2 收敛）：
/// ③ 主管道 state `agent.id` 是绑定真值（管道自足，冷恢复唯一读源）——
///    写失败 = 绑定更新失败（503 可重试），①② 均不写（真值缺失时放行
///    会让重启后绑定丢失，且 registry 与 state 各持一值）；
///    真值落点（主管道）解析不以热 registry 为前提（X5-F1）：registry 命中
///    仅作加速路径；未命中（内核重启后 registry 冷）改从持久真值解析——
///    sessions.active_pipeline_id 定候选（冷恢复读面同源），pipeline_sessions
///    映射（创建时 link_pipeline_session 兜底写入）作存在校验；持久真值也
///    缺失 → fail-closed 503，绝不只更新投影就返回成功；
/// ② sessions 表 agent_id 是 ③ 成功后的投影（会话列表回显用，不作绑定
///    读源，写失败仅 warn）；
/// ① registry 线程绑定（内存热路径缓存，校验通过后写）。
/// 多租户：与同文件 create/list/delete 同语义——request_tenant_ctx 解析请求租户，
/// ②③ 在 scope 内读写；非本租户/不存在的会话 → 404 且零副作用（①不写、DB 不落，
/// 与 list 语义一致，不伪造内存回退 200）；仅 store 未配置（无持久化数据源）时
/// 回退内存响应。
pub async fn update_session_agent_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> Result<Json<Value>, ApiError> {
    let agent_id = body
        .get("agent_id")
        .and_then(|v| v.as_str())
        .unwrap_or("agentos")
        .to_string();

    let now = chrono::Utc::now().to_rfc3339();

    // ②③ 多租户：sessions 表与管道 state 按 task_local tenant 过滤/落库，需在
    // 请求租户 scope 内执行——scope 缺失回退 default 租户（非 default 租户用户
    // 改自己会话查空、改 default 租户会话误命中）。先确认会话在请求租户内存在
    // （不存在 → 404），存在才继续写入。
    if let Some(store) = state.store.as_ref() {
        let tenant_ctx =
            crate::server::request_tenant_ctx(state.store.as_ref(), &headers, &id).await?;
        let tenant_id = tenant_ctx.tenant_id.clone();
        // ③ 真值落点解析：热 registry 命中仅作加速路径；未命中（内核重启后
        // registry 冷——thread→pipeline 映射只在创建时注册）在下方 scope 内
        // 改从持久真值解析（X5-F1：修复前未命中即静默跳过真值写）。
        let registry_pipeline = state
            .session
            .as_ref()
            .and_then(|s| s.registry().get_pipeline_for_thread(&id));
        let store_clone = store.clone();
        let id_for_scope = id.clone();
        let agent_for_scope = agent_id.clone();
        let now_for_scope = now.clone();
        let updated = agentos_tenant::scope(tenant_ctx, async move {
            let mut s = store_clone
                .get_session(&id_for_scope)
                .await
                .map_err(|e| {
                    tracing::warn!(
                        thread_id = %id_for_scope,
                        error = %e,
                        "get_session 查询失败，返回 503（不伪造 404/200）"
                    );
                    ApiError::ServiceUnavailable {
                        message: "会话查询暂时不可用（存储异常），请稍后重试".to_string(),
                    }
                })?
                .ok_or_else(|| ApiError::NotFound {
                    message: format!("会话不存在或不可访问: {id_for_scope}"),
                })?;

            // ③ 绑定真值落主管道 state（2026-08-24 阶段1；F2 收敛为 fail-closed：
            // 真值写失败 = 绑定更新失败，503 可重试——静默放行会让重启后冷恢复
            // 读 state 时丢绑定，且 registry 与 state 各持一值）。失败时 ②① 不写
            // （投影/缓存不得领先真值）。
            let truth_pipeline = match registry_pipeline {
                Some(pid) => pid,
                None => {
                    // 持久真值解析：sessions.active_pipeline_id 定候选（冷恢复
                    // 读面 resolve_dispatch_agent 即从该管道读 agent.id，写读
                    // 同源），pipeline_sessions 映射（创建时 link_pipeline_session
                    // 兜底写入）作存在校验。
                    let candidate = s
                        .active_pipeline_id
                        .clone()
                        .filter(|p| !p.is_empty())
                        .ok_or_else(|| {
                            tracing::error!(
                                thread_id = %id_for_scope,
                                "会话无持久化主管道（active 缺失），agent 绑定 fail-closed"
                            );
                            ApiError::ServiceUnavailable {
                                message: "会话 agent 绑定失败（会话缺少持久化主管道），请稍后重试"
                                    .to_string(),
                            }
                        })?;
                    match store_clone
                        .list_pipeline_ids_by_thread(&id_for_scope, &tenant_id)
                        .await
                    {
                        Ok(pids) if pids.contains(&candidate) => candidate,
                        Ok(_) => {
                            tracing::error!(
                                thread_id = %id_for_scope,
                                pipeline = %candidate,
                                "pipeline_sessions 映射缺失，agent 绑定 fail-closed（不落投影）"
                            );
                            return Err(ApiError::ServiceUnavailable {
                                message: "会话 agent 绑定失败（缺少主管道映射），请稍后重试"
                                    .to_string(),
                            });
                        }
                        Err(e) => {
                            tracing::error!(
                                thread_id = %id_for_scope,
                                error = %e,
                                "pipeline_sessions 映射查询失败，agent 绑定 fail-closed（不落投影）"
                            );
                            return Err(ApiError::ServiceUnavailable {
                                message: "会话 agent 绑定写入失败（存储异常），请稍后重试"
                                    .to_string(),
                            });
                        }
                    }
                }
            };
            store_clone
                .upsert_state_field(
                    &truth_pipeline,
                    &tenant_id,
                    "agent.id",
                    &json!(agent_for_scope),
                )
                .await
                .map_err(|e| {
                    tracing::error!(
                        thread_id = %id_for_scope,
                        pipeline = %truth_pipeline,
                        error = %e,
                        "会话 agent 绑定写入管道 state 失败（操作失败，请重试）"
                    );
                    ApiError::ServiceUnavailable {
                        message: "会话 agent 绑定写入失败（存储异常），请稍后重试".to_string(),
                    }
                })?;

            // ② sessions 表投影：③ 成功后随写（会话列表回显用）。投影写失败
            // 仅 warn 不阻断——绑定真值已落 state，投影滞后由冷读单源 state 兜住
            // （③ fail-closed 后走到此处必有真值落点，本列不再承担唯一持久化面）。
            s.agent_id = Some(agent_for_scope.clone());
            s.updated_at = now_for_scope;
            if let Err(e) = store_clone.update_session(&s).await {
                tracing::warn!(
                    thread_id = %id_for_scope,
                    error = %e,
                    "update_session(agent) 投影落库失败"
                );
            }
            Ok(s)
        })
        .await?;

        // ① registry 热绑定（WS 路由仍用它）——存在性确认后执行，404 路径零副作用
        if let Some(session) = &state.session {
            session.registry().register_thread_agent(&id, &agent_id);
        }

        return Ok(Json(session_to_session_json(&updated)));
    }

    // store 未配置（无持久化会话数据源）：内存 registry 更新 + 回退内存构造响应
    if let Some(session) = &state.session {
        let registry = session.registry();
        registry.register_thread_agent(&id, &agent_id);
    }

    let pipeline_id = state
        .session
        .as_ref()
        .and_then(|s| s.registry().get_pipeline_for_thread(&id));
    let pipeline_ids = pipeline_id
        .as_deref()
        .map(|p| vec![p.to_string()])
        .unwrap_or_default();

    Ok(Json(json!({
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
    })))
}

/// PATCH /api/v1/sessions/{id} — 重命名/更新会话（title/intent/metadata）。
///
/// 前端 `updateSession()` 调此端点（session.ts 用 PATCH）。
/// body: { "intent": "<title>" }（前端把 title 映射成 intent）。响应返回完整会话状态。
/// 域2持久化：更新 sessions 表 title/intent + updated_at。
/// 多租户：与同文件 create/list/delete 同语义——request_tenant_ctx 解析请求租户，
/// 读改写在 scope 内执行；非本租户/不存在的会话 → 404（与 list 语义一致，
/// 不伪造内存回退 200）；仅 store 未配置（无持久化数据源）时回退内存响应。
pub async fn update_session_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> Result<Json<Value>, ApiError> {
    // 前端 updateSession 把 title 放进 intent 字段。
    let new_intent = body
        .get("intent")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());
    let metadata_patch = body.get("metadata").and_then(|m| m.as_object()).cloned();

    let now = chrono::Utc::now().to_rfc3339();

    // DB 读改写：按 task_local tenant 过滤，需在请求租户 scope 内执行——
    // scope 缺失回退 default 租户（非 default 租户用户改自己会话查空、
    // 改 default 租户会话误命中）。会话不存在 → 404。
    if let Some(store) = state.store.as_ref() {
        let tenant_ctx =
            crate::server::request_tenant_ctx(state.store.as_ref(), &headers, &id).await?;
        let store_clone = store.clone();
        let id_for_scope = id.clone();
        let now_for_scope = now.clone();
        let updated = agentos_tenant::scope(tenant_ctx, async move {
            let mut s = store_clone
                .get_session(&id_for_scope)
                .await
                .map_err(|e| {
                    tracing::warn!(
                        thread_id = %id_for_scope,
                        error = %e,
                        "get_session 查询失败，返回 503（不伪造 404/200）"
                    );
                    ApiError::ServiceUnavailable {
                        message: "会话查询暂时不可用（存储异常），请稍后重试".to_string(),
                    }
                })?
                .ok_or_else(|| ApiError::NotFound {
                    message: format!("会话不存在或不可访问: {id_for_scope}"),
                })?;
            let old_title = s.title.clone();
            if let Some(ref intent) = new_intent {
                s.title = Some(intent.clone());
                s.intent = Some(intent.clone());
            }
            // body.metadata 按 key 合并进既有 metadata（body 优先），不整体替换
            // ——整体替换会抹掉 session_type/user_id（会话列表过滤/租户归属失真，
            // 与 create_session 的默认值合并同一保全逻辑）。前端置顶/星标切换
            // （sessionListStore toggleSessionPin/toggleSessionStar）走本字段落库。
            if let Some(patch) = &metadata_patch {
                let mut merged = s.metadata.take().unwrap_or_else(|| json!({}));
                if let Some(obj) = merged.as_object_mut() {
                    for (k, v) in patch {
                        obj.insert(k.clone(), v.clone());
                    }
                }
                s.metadata = Some(merged);
            }
            s.updated_at = now_for_scope;
            if let Err(e) = store_clone.update_session(&s).await {
                tracing::warn!(thread_id = %id_for_scope, error = %e, "update_session(title/intent/metadata) 落库失败");
            }
            // 管道标题传播（ADR 2026-10-27-pipeline-title-field）：标题属会话
            // 域，落定/变更时刷新主管道 state.title（统一管道标题键）——面板
            // 单字段消费。仅真变更且非空才写；写失败显式 warn（会话标题已更
            // 新，真值在会话域；面板暂回退 ID 显示，不阻断请求）。
            if let (Some(new_title), Some(main_pid)) = (
                new_intent.as_deref().filter(|t| !t.is_empty()),
                s.pipeline_ids.first(),
            ) {
                if old_title.as_deref() != Some(new_title) {
                    if let Err(e) = store_clone
                        .upsert_state_field(main_pid, &agentos_tenant::current_or_default("default").tenant_id, "title", &json!(new_title))
                        .await
                    {
                        tracing::warn!(
                            thread_id = %id_for_scope,
                            pipeline = %main_pid,
                            error = %e,
                            "主管道标题传播写入失败（面板暂回退 ID 显示）"
                        );
                    }
                }
            }
            Ok(s)
        })
        .await?;
        return Ok(Json(session_to_session_json(&updated)));
    }

    // store 未配置（无持久化会话数据源）：回退内存构造响应
    // （保留 agent_id/pipeline_id 等已知状态）
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

    Ok(Json(json!({
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
    })))
}

/// DELETE /api/v1/sessions/{id} — 删除会话（级联）。
///
/// 前端 `deleteSession()` 调此端点。
/// 级联删除该会话下全部管道（主管道 + 子任务管道，经 pipeline_sessions 映射表按
/// thread_id 定位）的 messages / execution_records / traces / branches /
/// pipeline_run_summaries / runs，最后清映射表与 sessions 行。blobs 不删（内容寻址去重）。
/// 落库失败 → 503 统一错误信封（B1：删除未成立不得 `deleted:true` 假成功，
/// 域事件与成功响应只在删除已成立的 Ok 路径发出）；store 未配置或无记录仍
/// 返回 200（幂等，对齐 REST 删除语义）。
pub async fn delete_session_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    if let Some(store) = state.store.as_ref() {
        // 多租户：级联删除按 task_local tenant 过滤，需在请求租户 scope 内执行。
        // scope 缺失时（task_local 未设 → current_or_default("default")），
        // delete_session_inner 查 pipeline_sessions WHERE tenant_id='default' 查空 → 啥也没删。
        let tenant_ctx =
            crate::server::request_tenant_ctx(state.store.as_ref(), &headers, &id).await?;
        let store_clone = store.clone();
        let id_for_scope = id.clone();
        let del_result = agentos_tenant::scope(tenant_ctx, async move {
            // 删除前先挂起在途 run：删除只清数据，不会让 run 立刻停手——不挂起
            // 时它会在删后继续回写 state / 用已删会话 id 派生新管道（孤儿根源，
            // 2026-11-10 装机实证）。挂起失败不阻断删除：数据面由 store 删除
            // 墓碑写屏障兜底（迟到写入一律丢弃）。
            match store_clone.suspend_cascade_runs(&id_for_scope).await {
                Ok(run_ids) => {
                    let hub = crate::run_signal::RunSignalHub::global();
                    for run_id in &run_ids {
                        hub.signal(run_id);
                    }
                }
                Err(e) => {
                    tracing::warn!(
                        thread_id = %id_for_scope,
                        error = %e,
                        "删除会话前挂起在途 run 失败（继续删除，写屏障兜底）"
                    );
                }
            }
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
            // B1：删除未落库 = 数据原封未动，必须让前端能据状态码区分
            // 「已删除」与「根本没删进去」（对齐同文件 list_sessions 的
            // 503 存储故障语义），不发 session.deleted、不回 deleted:true。
            tracing::error!(thread_id = %id, error = %e, "delete_session 落库失败");
            return Err(ApiError::ServiceUnavailable {
                message: format!("删除会话失败（数据未删除）: {e}"),
            });
        }
    }

    // 域事件插座：session.deleted（语义：删除已成立；级联清理见 store）。
    crate::plugin_lifecycle::broadcast_domain_event(
        &state,
        "session.deleted",
        vec![("session_id".to_string(), json!(id.as_str()))],
    )
    .await;

    Ok(Json(json!({ "thread_id": id, "deleted": true })))
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
    let tenant_ctx = crate::server::request_tenant_ctx(state.store.as_ref(), &headers, &id).await?;
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

    let opts = MessageQueryOpts {
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
        // 执行身份戳记（assistant blob agent_id 读时提取）：消息生成时的管道
        // 执行身份，前端气泡卡名/卡头像的数据源（扮演场景）。无戳记不写字段
        // （缺省不污染；camelCase 对齐前端 BackendMessageResponse.agentId）。
        if let Some(aid) = rec.agent_id.as_deref() {
            msg.as_object_mut()
                .expect("msg is object")
                .insert("agentId".into(), Value::String(aid.to_string()));
        }
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
fn map_messages_query_failure(pipeline_id: &str, e: StorageError) -> ApiError {
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
            // 只收带 name 的合法声明项（无 name 的声明不构成字段）
            if f.get("name").and_then(|n| n.as_str()).is_some() {
                fields.push(f.clone());
            }
        }
    }

    Json(json!({ "fields": fields }))
}

#[cfg(test)]
mod tests;

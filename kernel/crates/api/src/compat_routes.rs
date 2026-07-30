//! 前端兼容端点（消除 0.2 内核相对 0.1 前端的 404）
//!
//! // COMPAT: temporary, migrate to /ext/{plugin_id} per ADR §3.3.
//! // 本文件是 0.1→0.2 迁移期的兼容垫片，**禁止新增业务端点**。
//! // 迁移进度见 docs/working/adr_plugin_capability_gap_and_plan.md 阶段 B。
//! // 已迁移：cost-control → plugins/shared/system/cost_control（/ext/cost_control/**）
//! // 已迁移：monitoring → plugins/shared/system/monitoring（/ext/monitoring/**）
//! // 已迁移：themes → 纯前端（Vite import.meta.glob，内核不参与）
//! // 已删死端点：evaluation 的 9 个无后端无消费常量（evaluate/profiles/reports/statistics/trends）
//! // 保留为内核职责（非迁移项，按边界原则归属内核）：
//! //   - evaluation-metrics：config 域只读查询（待 evaluation 插件声明 config_files 后迁出）
//! //   - plugins/status|history|reload*：loader 监管能力（属内核；reload 未实现，诚实返回 false）
//! //   - threads/messages：会话传输宿主（WS/session 属内核）；messages 空 stub 待持久化
//!
//! 对齐 `plugins/shared/system/channel_api/routes_missing.py` 与
//! `routes_plugins.py` 的响应形状，让前端不再因缺失路由刷屏。
//! 能接真实数据源的接真实数据；否则返回结构正确的空/零值。

use std::fs;
use std::path::PathBuf;

use axum::extract::{Path, Query, State};
use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::routes::AppState;

// ─── Threads ───────────────────────────────────────────────────────────────

/// GET /api/v1/threads — 会话列表。
///
/// 域2持久化：优先从 sessions 表读（重启后可恢复），DB 空时回退内存 registry
/// （兼容 store 未配置或历史场景）。返回结构对齐前端 ThreadStateResponse。
pub async fn list_threads_handler(State(state): State<AppState>) -> Json<Value> {
    let now = chrono::Utc::now().to_rfc3339();
    let mut threads: Vec<Value> = Vec::new();

    // 优先读 DB（持久化会话列表）
    let mut db_has_data = false;
    if let Some(store) = state.store.as_ref() {
        let filter = agentos_core::traits::SessionListFilter {
            session_type: Some("main_pipeline".to_string()),
            limit: Some(100),
        };
        match store.list_sessions(filter).await {
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
        if let Err(e) = store.create_session(&session_rec).await {
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

    let records = match store.get_messages_by_pipeline(&target_pid, opts).await {
        Ok(r) => r,
        Err(e) => {
            tracing::warn!(pipeline_id = %target_pid, error = %e, "get_messages_by_pipeline 查询失败");
            return Json(json!({ "messages": [], "total": 0, "has_more": false }));
        }
    };

    // 联查 blobs 取完整内容（content_preview 只是摘要，完整内容在 blobs 表）
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
        messages.push(json!({
            // 字段命名对齐前端 BackendMessageResponse（session.ts:53-79）
            "id": rec.message_id,
            "thread_id": id,            // 回填路径 id（满足前端 mapper，不参与查询过滤）
            "sequence": rec.seq_in_branch,
            "role": rec.role,
            "content": content,
            "timestamp": rec.created_at,
            "status": "completed",
        }));
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

/// GET /api/v1/plugins/history
pub async fn plugins_history_handler() -> Json<Value> {
    Json(json!([]))
}

/// POST /api/v1/plugins/reload
///
/// 热重载未实现（诚实返回 false）。热重载能力见 `agentos-config::config_center`——
/// 已实现 notify 文件监听但**未接线**（详见该模块顶部说明）。管道配置目前启动期
/// 一次性加载到 `Arc<PipelineConfig>`，运行期不可变，修改需重启内核。
pub async fn plugins_reload_handler(Query(params): Query<ReloadQuery>) -> Json<Value> {
    Json(json!({
        "config_path": params.config_path.unwrap_or_default(),
        "config_type": "unknown",
        "success": false,
        "error": "0.2 内核暂未实现热重载（sidecar 监管尚未接入）",
        "rolled_back": false,
    }))
}

/// POST /api/v1/plugins/reload-all
///
/// 同 `plugins_reload_handler`，热重载未实现，返回空数组。
pub async fn plugins_reload_all_handler() -> Json<Value> {
    Json(json!([]))
}

#[derive(Debug, Deserialize, Default)]
pub struct ReloadQuery {
    pub config_path: Option<String>,
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

// ─── Evaluation metrics ────────────────────────────────────────────────────

/// GET /api/v1/evaluation-metrics — 扫描 config/evaluation_metrics/*.yaml。
pub async fn evaluation_metrics_handler(State(state): State<AppState>) -> Json<Value> {
    let dir = resolve_project_path(&state, &["config", "evaluation_metrics"]);
    let mut metrics: Vec<Value> = Vec::new();

    if let Ok(entries) = fs::read_dir(&dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path
                .extension()
                .and_then(|e| e.to_str())
                .map(|e| e == "yaml" || e == "yml")
                != Some(true)
            {
                continue;
            }
            let id = path
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("unknown")
                .to_string();
            let content = fs::read_to_string(&path).unwrap_or_default();
            let parsed: Value = serde_yaml::from_str(&content).unwrap_or(json!({}));
            let name = parsed
                .get("name")
                .or_else(|| parsed.get("id"))
                .and_then(|v| v.as_str())
                .unwrap_or(&id);
            let description = parsed
                .get("description")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let metric_type = parsed
                .get("metric_type")
                .or_else(|| parsed.get("type"))
                .and_then(|v| v.as_str())
                .unwrap_or("unknown");
            metrics.push(json!({
                "id": id,
                "name": name,
                "description": description,
                "metric_type": metric_type,
                "tags": parsed.get("tags").cloned().unwrap_or(json!([])),
                "is_red_line": parsed.get("is_red_line").and_then(|v| v.as_bool()).unwrap_or(false),
            }));
        }
    }

    let total = metrics.len();
    Json(json!({ "metrics": metrics, "total": total }))
}


// ─── helpers ───────────────────────────────────────────────────────────────

fn resolve_project_path(state: &AppState, segments: &[&str]) -> PathBuf {
    let mut path = state
        .project_root
        .clone()
        .unwrap_or_else(|| PathBuf::from("."));
    for seg in segments {
        path.push(seg);
    }
    path
}

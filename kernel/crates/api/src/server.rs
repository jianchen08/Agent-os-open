//! Axum HTTP/WebSocket API 服务器
//!
//! 提供 RESTful API 端点和 WebSocket 流式通信。
//! AC-06-3: /health 返回 200
//! AC-06-4: WebSocket 可连接收发消息
//! AC-06-5: Schema 聚合端点
//!
//! [来源: docs/tasks/task_07_llm_api.md]

use std::net::SocketAddr;
use std::sync::Arc;

use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        State,
    },
    http::HeaderMap,
    response::IntoResponse,
    routing::{get, post},
    Router,
};
use agentos_core::traits::CapabilityRegistry;
use agentos_core::types::TenantContext;
use serde::{Deserialize, Serialize};
use tracing::{info, warn};

use crate::auth::{
    login_handler, logout_handler, me_handler, refresh_handler, register_handler,
    resolve_request_tenant_id,
};
use crate::error::ApiError;
use crate::compat_routes::{
    create_thread_handler,
    evaluation_metrics_handler, get_thread_handler, list_thread_messages_handler,
    list_threads_handler, plugins_history_handler,
    plugins_reload_all_handler, plugins_reload_handler, plugins_set_enabled_handler,
    plugins_status_handler, update_thread_agent_handler,
};
use crate::routes::{
    agents_handler, get_plugin_config_with_etag, health_handler, metrics_prometheus_handler,
    metrics_query_handler, pipelines_handler, put_plugin_config_handler, schema_handler,
    tools_handler, AppState,
};

/// WebSocket 消息请求体。
#[derive(Debug, Deserialize, Serialize)]
pub struct WsRequest {
    #[serde(default)]
    pub message: String,
    #[serde(default)]
    pub session_id: String,
    /// 可选对话历史（多轮上下文）。客户端传入前几轮的 messages（OpenAI 格式），
    /// 内核注入 state.messages 供 LLM 看到上下文。0.2 内核暂不自动持久化历史，
    /// 由客户端维护会话历史并每轮带上（与 0.1 文件存储的按 session 加载等价）。
    #[serde(default)]
    pub history: Vec<serde_json::Value>,
    /// 可选 agent_id（默认 agentos）。指定执行 agent（如 general_agent 触发 bash 隔离）。
    #[serde(default)]
    pub agent_id: String,
}

/// WebSocket 消息响应体。
#[derive(Debug, Serialize)]
pub struct WsResponse {
    pub r#type: String,
    pub content: String,
    pub session_id: String,
    pub timestamp: String,
}

/// 构建 API 路由树。
///
/// P3（ADR §3.3）：内核静态路由 + 扫描 `http_routes` 动态挂载插件端点。
/// 动态端点统一走 `dispatch_http`（raw body/headers 透传 + 插件自定义响应 +
/// per-endpoint timeout/concurrency）。
pub fn build_router(state: AppState) -> Router {
    let static_router = Router::new()
        // AC-06-3: 健康检查
        .route("/health", get(health_handler))
        // AC-06-5: Schema 聚合端点
        .route("/api/v1/schema", get(schema_handler))
        .route("/api/v1/agents", get(agents_handler))
        .route("/api/v1/pipelines", get(pipelines_handler))
        .route("/api/v1/tools", get(tools_handler))
        // P1-4: 插件配置读写端点（manifest config_files 映射）
        .route(
            "/api/v1/plugins/{id}/config/{file_id}",
            get(get_plugin_config_with_etag).put(put_plugin_config_handler),
        )
        // 前端兼容端点（对齐 0.1 channel_api，消除 404）
        .route(
            "/api/v1/threads",
            get(list_threads_handler).post(create_thread_handler),
        )
        .route("/api/v1/threads/{id}", get(get_thread_handler))
        .route(
            "/api/v1/threads/{id}/agent",
            axum::routing::patch(update_thread_agent_handler),
        )
        .route(
            "/api/v1/threads/{id}/messages",
            get(list_thread_messages_handler),
        )
        .route("/api/v1/plugins/status", get(plugins_status_handler))
        .route("/api/v1/plugins/history", get(plugins_history_handler))
        .route("/api/v1/plugins/reload", post(plugins_reload_handler))
        .route("/api/v1/plugins/{id}/enabled", axum::routing::put(plugins_set_enabled_handler))
        .route(
            "/api/v1/plugins/reload-all",
            post(plugins_reload_all_handler),
        )
        .route(
            "/api/v1/evaluation-metrics",
            get(evaluation_metrics_handler),
        )
        // 监控 M5/M5b：指标查询 + Prometheus 导出（监控设计 §五/§十一）
        .route("/api/v1/metrics", get(metrics_query_handler))
        .route("/metrics", get(metrics_prometheus_handler))
        // AC-06-4: WebSocket 端点
        .route("/ws", get(ws_handler))
        // task_11 A2：前端写死连 /ws/chat（0.1 路径格式），加别名指向同一 handler，
        // 保证 0.2 模式下前端直连内核可用；/ws 保留给新客户端。
        .route("/ws/chat", get(ws_handler))
        // 消息发送端点（REST fallback for WS）
        .route("/api/v1/chat", post(chat_handler))
        // Auth 端点
        .route("/api/v1/auth/login", post(login_handler))
        .route("/api/v1/auth/me", get(me_handler))
        .route("/api/v1/auth/refresh", post(refresh_handler))
        .route("/api/v1/auth/logout", post(logout_handler))
        .route("/api/v1/auth/register", post(register_handler));

    // P3：动态挂载插件 HTTP 端点（http_routes → dispatcher）
    let router = crate::http_dispatcher::build_router_with_http_routes(state.clone(), static_router);
    router.with_state(state)
}

/// WebSocket 连接处理器（AC-06-4）。
///
/// P2：若 AppState 启用了 session（`enable_session`），走内核化会话路径
/// （握手鉴权 + 连接注册 + 入站路由）；否则降级为旧 echo/engine 路径（兼容）。
async fn ws_handler(
    ws: WebSocketUpgrade,
    headers: HeaderMap,
    axum::extract::Query(params): axum::extract::Query<std::collections::HashMap<String, String>>,
    State(state): State<AppState>,
) -> impl IntoResponse {
    // P2 内核化路径
    if let (Some(session), Some(router)) = (state.session.clone(), state.inbound_router.clone()) {
        let token = params.get("token").cloned();
        return ws
            .on_upgrade(move |socket| run_p2_ws_session(socket, session, router, token));
    }
    // 降级路径（旧 echo/engine，未启用 session 时）
    ws.on_upgrade(move |socket| handle_ws_connection(socket, state, headers))
}

/// P2 内核化 WS 会话包装：握手鉴权 + 会话运行 + 拒绝时 accept+close。
async fn run_p2_ws_session(
    socket: WebSocket,
    session: Arc<agentos_session::SessionCoordinator>,
    router: Arc<agentos_session::router::InboundRouter>,
    token: Option<String>,
) {
    let mut user_id = None;
    let (code, reason) = crate::ws_session::run_ws_session(
        socket,
        session,
        router,
        token.as_deref(),
        &mut user_id,
    )
    .await;
    if code != 1000 {
        info!(code, reason = %reason, "WS 握手拒绝（P2 内核化路径）");
    }
    // 握手拒绝时 run_ws_session 已提前返回，socket 尚未 accept；
    // axum WebSocketUpgrade 在 on_upgrade 回调里已 accept，拒绝码仅作日志。
}

/// 根据请求头解析当前租户上下文。
///
/// 多租户 P0-4：从 Authorization token 解析（或回退到默认租户），
/// 注入到 [`agentos_tenant::scope`] 后，engine/store 通过 task_local 读取。
pub(crate) fn request_tenant_ctx(headers: &HeaderMap, session_id: &str) -> TenantContext {
    TenantContext::new(resolve_request_tenant_id(headers), session_id)
}

/// 通过 0.2 配置驱动管道引擎处理消息。
///
/// 替代旧的"遍历全部 pipeline 插件"placeholder：改为构造
/// [`agentos_engine::PipelineExecutor`]，读取 AppState 中的 `pipeline_config`
/// + `step_library`，按 YAML 定义的 step 顺序执行（三级命中规则）。
///
/// 流程：
/// 1. 构造初始 state（含 `message` / 默认 `agent_id` / `core_type` 等）
/// 2. 加载 Agent 配置注入 state（system_prompt / tool_ids / model_tier / max_iterations）
/// 3. 构造 PipelineExecutor 并执行 `run`
/// 4. 从最终 state 提取响应（优先 `raw_result`，回退 `message`，再回退原消息）
///
/// 降级条件：AppState 缺少 invoker / store / project_root（典型为测试或老式构造）
/// 时走 echo-fallback，标注降级原因。

/// 默认核心管道插件 id（可被 agent 配置 config/agents/<id>.yaml 的 core_plugin 覆盖）。
/// 历史上硬编码 "pipeline_llm_core" 写在 initial_state，现提取为常量便于发现与替换。
const DEFAULT_CORE_PLUGIN: &str = "pipeline_llm_core";

pub(crate) async fn process_via_engine(
    state: &AppState,
    message: &str,
    agent_id: &str,
    history: &[serde_json::Value],
    pipeline_id: &str,
    thread_id: &str,
    message_id: &str,
) -> String {
    // 整个函数体 Box::pin 到堆上：process_via_engine 是大 async 函数（多 await 点 +
    // 大 state 构造），其 Future 状态机在 debug 构建下体积巨大，直接 await 会撑爆
    // tokio worker 线程栈（默认 2MB）。Box::pin 让 Future 分配在堆上，规避 debug 栈溢出。
    // release 下栈帧小不溢出，但 Box::pin 开销可忽略，统一使用保持一致。
    Box::pin(process_via_engine_inner(
        state, message, agent_id, history, pipeline_id, thread_id, message_id,
    ))
    .await
}

#[inline(never)]
async fn process_via_engine_inner(
    state: &AppState,
    message: &str,
    agent_id: &str,
    history: &[serde_json::Value],
    pipeline_id: &str,
    thread_id: &str,
    message_id: &str,
) -> String {
    let invoker = match state.invoker.clone() {
        Some(i) => i,
        None => {
            return format!(
                "[echo-fallback: engine not available] {}",
                message
            );
        }
    };
    let store = match state.store.clone() {
        Some(s) => s,
        None => {
            return format!(
                "[echo-fallback: store not available] {}",
                message
            );
        }
    };
    let project_root = match state.project_root.clone() {
        Some(p) => p,
        None => {
            return format!(
                "[echo-fallback: project_root not configured] {}",
                message
            );
        }
    };

    // 租户上下文（state 构造与 registry 主键都需要，提前取）。
    let tenant =
        agentos_tenant::current().unwrap_or_else(|| TenantContext::new("default", "kernel"));

    // 1. 构造本轮 state——热内存为主、冷 DB 为辅（对齐 0.1 EngineRegistry state 常驻）。
    //    管道引擎是无状态一次性执行器，真正跨轮延续的是 state。PipelineStateRegistry
    //    按 (tenant_id, pipeline_id) 常驻 state：
    //    - 热路径（命中）：复用上一轮 final_state，只覆盖本轮输入字段 + 追加新 user 消息。
    //    - 冷启动（未命中，进程重启/新会话首条）：从 DB 按 pipeline_id 重建历史（兜底）。
    use serde_json::json;
    // 全局 PipelineStateRegistry 单例（不放进 AppState，避免栈溢出，见模块说明）。
    let reg = agentos_session::global_registry();
    let mut initial_state = if reg.contains(&tenant.tenant_id, pipeline_id) {
        // ── 热路径：复用上一轮 final_state，只覆盖本轮输入字段 + 追加新 user ──
        let entry = reg.get(&tenant.tenant_id, pipeline_id).unwrap();
        let mut s = entry.read().state.clone();
        // 先算去重判定（只读 s），避免与下方 as_object_mut 的可变借用冲突
        let need_append = s.get("messages")
            .and_then(|m| m.as_array())
            .and_then(|arr| arr.last())
            .and_then(|m| m.get("content").and_then(|c| c.as_str()))
            .map(|last| last != message)
            .unwrap_or(true);
        if let Some(obj) = s.as_object_mut() {
            obj.insert("message".to_string(), json!(message));
            obj.insert("input".to_string(), json!(message));
            obj.insert("message_id".to_string(), json!(message_id));
            obj.insert("ended".to_string(), json!(false));
            obj.insert("suspended".to_string(), json!(false));
            obj.insert("pipeline_id".to_string(), json!(pipeline_id));
            obj.insert("session_id".to_string(), json!(thread_id));
            if need_append {
                if let Some(arr) = obj.get_mut("messages").and_then(|m| m.as_array_mut()) {
                    arr.push(json!({"role": "user", "content": message}));
                } else {
                    obj.insert("messages".to_string(), json!([{"role":"user","content":message}]));
                }
            }
        }
        s
    } else {
        // ── 冷启动：从 DB 重建历史（对齐 0.1 resolve_conversation_history 兜底）──
        let mut msgs = if !history.is_empty() {
            history.to_vec()
        } else {
            resolve_history_from_store(state, pipeline_id).await
        };
        let need_append = msgs.last()
            .and_then(|m| m.get("content").and_then(|c| c.as_str()))
            .map(|last| last != message)
            .unwrap_or(true);
        if need_append {
            msgs.push(json!({"role": "user", "content": message}));
        }
        json!({
            "message": message, "input": message, "agent_id": agent_id,
            "core_type": "llm_call", "core_plugin": DEFAULT_CORE_PLUGIN,
            "ended": false, "suspended": false,
            "pipeline_id": pipeline_id, "session_id": thread_id, "message_id": message_id,
            "messages": msgs,
        })
    };

    // 2. 加载 Agent 配置注入 state（读 config/agents/<agent_id>.yaml，不存在跳过）
    load_agent_config_into_state(&mut initial_state, agent_id, &project_root);

    // 2b. 注入工具 schema 到 state（0.2 sidecar 架构适配）。
    // 0.1 单进程时 tool_schema 插件经 ctx.get_service("tool_registry") 直接访问内核
    // ToolRegistry；0.2 sidecar 是独立进程拿不到该 service。改为内核侧在管道启动前
    // 按 agent tool_ids 过滤、转成 OpenAI function-calling 格式注入 state["tool_schemas"]，
    // 这样 prepare 阶段的 tool_schema 插件读到非空 schema（它优先用 state 里的值），
    // LLM 即可看到工具并调用（tool_core 执行时内核 invoke_tool 经 MCP 调 sidecar）。
    inject_tool_schemas(&mut initial_state, &state);

    // 3. 构造 PipelineExecutor 并执行
    //    run_id / branch_id 用 uuid 保证多请求隔离；租户上下文从 task_local 读取
    //    （多租户 P0-4：本函数已在 agentos_tenant::scope 内调用）。
    // 3. 构造 PipelineExecutor 并执行
    //    run_id / branch_id 用 uuid 保证多请求隔离；租户上下文在上方已取。
    //    tenant 会被 move 进 executor，先克隆 tenant_id 供下方 registry 回写用。
    let tenant_id = tenant.tenant_id.clone();
    let run_id = uuid::Uuid::new_v4().to_string();
    let branch_id = "main".to_string();
    let executor = agentos_engine::PipelineExecutor::new(
        invoker,
        project_root,
        tenant,
        state.plugin_ids.iter().cloned(),
        store,
        run_id.clone(),
        branch_id,
    );

    info!(run_id = %run_id, agent_id = %agent_id, "Pipeline run started");

    let final_state = match executor
        .run(&state.pipeline_config, &state.step_library, initial_state)
        .await
    {
        Ok(s) => s,
        Err(e) => {
            warn!(run_id = %run_id, error = %e, "PipelineExecutor run failed");
            return format!("[engine-run-failed] {}", message);
        }
    };

    // 3b. 回写 final_state 到全局 registry（热路径延续，对齐 0.1 _current_state 跨轮保留）。
    //     下一轮命中时即读到这份 state，messages 历史自然延续。
    {
        let reg = agentos_session::global_registry();
        if !reg.contains(&tenant_id, pipeline_id) {
            reg.get_or_init(
                &tenant_id, pipeline_id, thread_id, agent_id,
                final_state.clone(),
            );
        } else {
            reg.update_state(&tenant_id, pipeline_id, final_state.clone());
        }
    }

    // 4. 提取响应：优先 raw_result，回退 state.message，再回退原消息
    if let Some(raw) = final_state.get("raw_result").and_then(|v| v.as_str()) {
        return raw.to_string();
    }
    if let Some(msg) = final_state.get("message").and_then(|v| v.as_str()) {
        return msg.to_string();
    }
    // 没有 raw_result / message 字段：pretty-print 整个 state（便于调试）
    serde_json::to_string_pretty(&final_state).unwrap_or_else(|_| message.to_string())
}

/// 从存储按 pipeline_id 加载历史对话，转 OpenAI {role, content} 格式。
///
/// 对齐 0.1 `resolve_conversation_history`（state_builder.py:102-145）的存储兜底分支：
/// 调用方未传 history 时，自动 `list_by_pipeline(pipeline_id)` 恢复完整历史。
/// 这是保证大模型看到多轮上下文的关键——0.2 的 WS/HTTP 入口均传空 history。
///
/// role 映射对齐 0.1 `record_role_for_llm`：system 降级为 user（多数模型拒绝多轮穿插
/// system），其余 user/assistant/tool 原样。完整内容在 blobs 表，逐条联查取回。
async fn resolve_history_from_store(
    state: &AppState,
    pipeline_id: &str,
) -> Vec<serde_json::Value> {
    let Some(store) = state.store.as_ref() else {
        return Vec::new();
    };
    if pipeline_id.is_empty() {
        return Vec::new();
    }
    let opts = agentos_core::traits::MessageQueryOpts::default();
    let records = match store.get_messages_by_pipeline(pipeline_id, opts).await {
        Ok(r) => r,
        Err(e) => {
            warn!(pipeline_id = %pipeline_id, error = %e, "加载历史对话失败，state.messages 将只有当前一句");
            return Vec::new();
        }
    };
    let mut msgs = Vec::with_capacity(records.len());
    for rec in records {
        // 取完整内容：优先 blob，回退 content_preview
        let content = if let Some(bid) = rec.blob_id.as_deref() {
            match store.get_blob(bid).await {
                Ok(bytes) => String::from_utf8_lossy(&bytes).to_string(),
                Err(_) => rec.content_preview.clone().unwrap_or_default(),
            }
        } else {
            rec.content_preview.clone().unwrap_or_default()
        };
        // role 映射：system 降级 user（对齐 record_role_for_llm）
        let role = match rec.role.as_str() {
            "assistant" => "assistant",
            "tool" => "tool",
            "system" => "user",
            _ => "user",
        };
        msgs.push(serde_json::json!({"role": role, "content": content}));
    }
    msgs
}

/// 注入工具 schema 到 state["tool_schemas"]（0.2 sidecar 架构适配）。
///
/// 按 state["tool_ids"] 过滤 capability_registry 的工具，转成 OpenAI function-calling
/// 格式（`{type:"function", function:{name, description, parameters}}`）。tool_ids
/// 缺失时注入全部工具（兜底）。registry 不可用时注入空列表（LLM 无工具可用）。
fn inject_tool_schemas(state: &mut serde_json::Value, app_state: &AppState) {
    let Some(registry) = app_state.capability_registry.as_ref() else {
        return;
    };
    let all_tools = registry.list_tools();

    // 按 agent 的 tool_ids 过滤；缺失则用全部（兜底，避免无工具可用）
    let wanted_ids: Option<Vec<String>> = state
        .get("tool_ids")
        .and_then(|v| v.as_array())
        .map(|arr| arr.iter().filter_map(|t| t.as_str().map(String::from)).collect());
    let wanted: Option<std::collections::HashSet<String>> =
        wanted_ids.map(|ids| ids.into_iter().collect());
    let schemas: Vec<serde_json::Value> = all_tools
        .iter()
        .filter(|t| match &wanted {
            Some(ids) => ids.contains(&t.name),
            None => true,
        })
        .filter(|t| {
            // LLM 严格校验工具 schema:parameters 必须是 type:object 的 JSON Schema。
            // 过滤掉 input_schema 缺失/非 object 的工具(如 simple_tools 的部分工具
            // manifest 未声明 input_schema),否则 DeepSeek/OpenAI 拒绝整个请求
            // (BadRequest: schema must be a JSON Schema of 'type: "object"')。
            t.input_schema.is_object()
        })
        .map(|t| {
            serde_json::json!({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                }
            })
        })
        .collect();

    if let Some(obj) = state.as_object_mut() {
        obj.insert(
            "tool_schemas".to_string(),
            serde_json::Value::Array(schemas),
        );
    }
}

/// 在 agents 目录（含分类子目录）递归查找 `<agent_id>.yaml`。
///
/// agents/ 按分类组织为 `agents/<category>/<id>.yaml`（main/orchestrator/
/// executor/system/task/test），顶层不再放单文件。返回首个匹配路径。
fn find_agent_yaml(dir: &std::path::Path, agent_id: &str) -> Option<std::path::PathBuf> {
    let target = format!("{}.yaml", agent_id);
    let Ok(entries) = std::fs::read_dir(dir) else {
        return None;
    };
    for entry in entries.flatten() {
        let p = entry.path();
        if p.is_dir() {
            if let Some(found) = find_agent_yaml(&p, agent_id) {
                return Some(found);
            }
        } else if p.file_name().map(|n| n == target.as_str()).unwrap_or(false) {
            return Some(p);
        }
    }
    None
}

/// 加载 Agent 配置注入到管道 state。
///
/// 简化语义（[来源: 任务 §load_agent_config_into_state]）：只读 `system_prompt`
/// / `tool_ids` / `model_tier` / `max_iterations` 几个字段，不解析复杂结构。
/// 文件不存在跳过（用 state 已有的默认值）。
///
/// 设计取舍：字段冲突时不覆盖 state 中已有的值（agent 调用方注入优先级高于配置默认），
/// 仅在缺失时补。`max_iterations` 同时覆写 `pipeline_config.loop_config.max_iterations`
/// 的运行期语义（由 PipelineExecutor 在每次 run 时读取 state，而非 config）。
fn load_agent_config_into_state(
    state: &mut serde_json::Value,
    agent_id: &str,
    project_root: &std::path::Path,
) {
    // Agent 配置在 config/agents/ 下（按分类子目录 main/orchestrator/executor/…）。
    // project_root 是项目根（config/ 的父目录），拼 config/agents。
    let agents_dir = project_root.join("config").join("agents");
    let top = agents_dir.join(format!("{}.yaml", agent_id));
    let path = if top.is_file() {
        top
    } else {
        match find_agent_yaml(&agents_dir, agent_id) {
            Some(p) => p,
            None => {
                tracing::debug!(
                    agent_id = %agent_id,
                    "Agent config not found under {}, using defaults",
                    agents_dir.display()
                );
                return;
            }
        }
    };
    let raw = match std::fs::read_to_string(&path) {
        Ok(s) => s,
        Err(_) => {
            tracing::debug!(
                agent_id = %agent_id,
                "Agent config not readable at {}, using defaults",
                path.display()
            );
            return;
        }
    };
    let parsed: serde_yaml::Value = match serde_yaml::from_str(&raw) {
        Ok(v) => v,
        Err(e) => {
            warn!(
                agent_id = %agent_id,
                error = %e,
                "Failed to parse agent config, using defaults"
            );
            return;
        }
    };

    let obj = match state.as_object_mut() {
        Some(o) => o,
        None => return,
    };
    let entry = |key: &str| -> Option<serde_json::Value> {
        parsed
            .get(key)
            .cloned()
            .and_then(|v| serde_yaml::from_value(v).ok())
    };

    if let Some(v) = entry("system_prompt") {
        obj.entry("system_prompt").or_insert(v);
    }
    if let Some(v) = entry("tool_ids") {
        obj.entry("tool_ids").or_insert(v);
    }
    if let Some(v) = entry("model_tier") {
        obj.entry("model_tier").or_insert(v);
    }
    if let Some(v) = entry("max_iterations") {
        obj.entry("max_iterations").or_insert(v);
    }
    // core_plugin：agent 配置优先于内核默认值（DEFAULT_CORE_PLUGIN）。
    // 用 insert 直接覆盖，使 agent 能切换核心插件（如换 LLM 提供商）。
    if let Some(v) = entry("core_plugin") {
        obj.insert("core_plugin".to_string(), v);
    }
}

/// 处理 WebSocket 连接——收发消息循环。
async fn handle_ws_connection(socket: WebSocket, state: AppState, headers: HeaderMap) {
    let (mut sender, mut receiver) = socket.split();

    // 发送欢迎消息
    let welcome = WsResponse {
        r#type: "connected".to_string(),
        content: "WebSocket connected to Lingxi AgentOS 0.2".to_string(),
        session_id: Uuid::new_v4().to_string(),
        timestamp: chrono::Utc::now().to_rfc3339(),
    };
    let welcome_json = serde_json::to_string(&welcome).unwrap_or_default();
    let _ = sender.send(Message::Text(welcome_json.into())).await;

    info!("WebSocket connection established");

    // 收发循环
    while let Some(Ok(msg)) = receiver.next().await {
        match msg {
            Message::Text(text) => {
                // 解析客户端消息
                let req: WsRequest = serde_json::from_str(&text).unwrap_or(WsRequest {
                    message: text.to_string(),
                    session_id: String::new(),
                    history: Vec::new(),
                    agent_id: String::new(),
                });

                // 在租户上下文内通过管道引擎处理消息（多租户 P0-4）
                // TODO: agent_id 暂用默认（chat 协议暂未携带；后续从请求体取）
                let tenant_ctx = request_tenant_ctx(&headers, &req.session_id);
                let content =
                    agentos_tenant::scope(tenant_ctx, process_via_engine(&state, &req.message, if req.agent_id.is_empty() { "agentos" } else { req.agent_id.as_str() }, &req.history, "", "", ""))
                        .await;

                // 构造响应
                let response = WsResponse {
                    r#type: "message".to_string(),
                    content,
                    session_id: req.session_id,
                    timestamp: chrono::Utc::now().to_rfc3339(),
                };

                let response_json = serde_json::to_string(&response).unwrap_or_default();
                if sender
                    .send(Message::Text(response_json.into()))
                    .await
                    .is_err()
                {
                    break;
                }
            }
            Message::Binary(_) => {
                // 忽略二进制消息
            }
            Message::Close(_) => {
                info!("WebSocket connection closed");
                break;
            }
            _ => {}
        }
    }
}

/// /api/v1/chat POST 端点——通过管道引擎处理消息。
async fn chat_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
    axum::Json(req): axum::Json<WsRequest>,
) -> Result<axum::Json<WsResponse>, ApiError> {
    // 在租户上下文内通过管道引擎处理消息（多租户 P0-4）
    // TODO: agent_id 暂用默认（chat 协议暂未携带；后续从请求体取）
    let tenant_ctx = request_tenant_ctx(&headers, &req.session_id);
    let content =
        agentos_tenant::scope(tenant_ctx, process_via_engine(&state, &req.message, if req.agent_id.is_empty() { "agentos" } else { req.agent_id.as_str() }, &req.history, "", &req.session_id, ""))
            .await;

    let response = WsResponse {
        r#type: "message".to_string(),
        content,
        session_id: req.session_id,
        timestamp: chrono::Utc::now().to_rfc3339(),
    };
    Ok(axum::Json(response))
}

/// 启动 API 服务器。
pub async fn start_server(addr: SocketAddr, state: AppState) -> Result<(), ApiError> {
    let app = build_router(state);
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("Failed to bind {}: {}", addr, e),
        })?;
    info!("API server starting on {}", addr);
    axum::serve(listener, app)
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("Server error: {}", e),
        })?;
    Ok(())
}

use futures_util::{SinkExt, StreamExt};
use uuid::Uuid;

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use serde_json::json;
    use tower::ServiceExt;

    #[tokio::test]
    async fn test_health_returns_200() {
        let app = build_router(AppState::new());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/health")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn test_schema_returns_200() {
        let app = build_router(AppState::new());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/schema")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn test_agents_returns_200() {
        let app = build_router(AppState::new());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/agents")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn test_pipelines_returns_200() {
        let app = build_router(AppState::new());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/pipelines")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn test_tools_returns_200() {
        let app = build_router(AppState::new());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/tools")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn test_chat_post_returns_200() {
        let app = build_router(AppState::new());
        let body = serde_json::to_string(&WsRequest {
            message: "hello".to_string(),
            session_id: "s1".to_string(),
            history: Vec::new(),
            agent_id: String::new(),
        })
        .unwrap();
        let response = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/chat")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn test_health_response_body() {
        let app = build_router(AppState::new());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/health")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(response.into_body(), 4096)
            .await
            .unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(json["status"], "ok");
        assert!(json["version"].is_string());
    }

    #[tokio::test]
    async fn test_chat_uses_engine_not_echo() {
        // 验证 chat 响应不再是简单的 "Response to: xxx"
        let app = build_router(AppState::new());
        let body = serde_json::to_string(&WsRequest {
            message: "hello world".to_string(),
            session_id: "test_session".to_string(),
            history: Vec::new(),
            agent_id: String::new(),
        })
        .unwrap();
        let response = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/chat")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(response.into_body(), 8192)
            .await
            .unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(json["type"], "message");
        // 响应内容不应再是 "Response to: hello world"（echo 模式）
        let content = json["content"].as_str().unwrap();
        assert!(
            !content.starts_with("Response to:"),
            "Chat should not be in echo mode, got: {}",
            content
        );
        assert_eq!(json["session_id"], "test_session");
    }

    #[tokio::test]
    async fn test_schema_with_config() {
        let config = json!({
            "agents": [{"id": "agent1", "name": "Test Agent"}],
            "pipelines": [{"id": "default", "name": "Default Pipeline"}],
            "tools": [{"name": "search", "description": "Search tool"}],
            "routes": {"input": ["plugin1"], "output": ["plugin2"]}
        });
        let app = build_router(AppState::with_config(config));
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/schema")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(response.into_body(), 4096)
            .await
            .unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        // config-based 模式下 agents 为空（因为 manifests 为空）
        assert_eq!(json["agents"].as_array().unwrap().len(), 0);
        // tools 来自 config（capability_registry 为 None 时 fallback 到 config）
        assert_eq!(json["tools"].as_array().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn test_tools_handler_returns_tools_list() {
        // 验证 tools handler 从 config 返回工具列表（无 registry 时）
        let config = json!({
            "tools": [{"name": "calculator", "description": "A calculator"}],
        });
        let app = build_router(AppState::with_config(config));
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/tools")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(response.into_body(), 4096)
            .await
            .unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let tools = json.as_array().unwrap();
        assert_eq!(tools.len(), 1);
        assert_eq!(tools[0]["name"], "calculator");
    }

    // ── 监控 M5/M5b：指标查询端点 + Prometheus 导出端点 ──

    fn state_with_metrics() -> AppState {
        use crate::metrics::{Labels, MetricType, MetricsAggregator};
        let agg = MetricsAggregator::new();
        let mut labels = Labels::new();
        labels.insert("model".to_string(), "deepseek".to_string());
        agg.record(
            "llm_service",
            "tokens_used",
            MetricType::Counter,
            12800.0,
            &labels,
            Some("tokens"),
            Some("Total tokens used"),
        );
        agg.record(
            "llm_service",
            "latency",
            MetricType::Histogram,
            0.02,
            &Labels::new(),
            Some("seconds"),
            Some("LLM latency"),
        );
        AppState::new().with_metrics(agg)
    }

    #[tokio::test]
    async fn test_metrics_query_endpoint_returns_data() {
        let app = build_router(state_with_metrics());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/metrics?plugin=llm_service")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = axum::body::to_bytes(response.into_body(), 8192)
            .await
            .unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let metrics = json["metrics"].as_array().unwrap();
        assert!(!metrics.is_empty());
        assert_eq!(metrics[0]["plugin_id"], "llm_service");
    }

    #[tokio::test]
    async fn test_metrics_query_filter_by_metric_name() {
        let app = build_router(state_with_metrics());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/metrics?plugin=llm_service&metric=tokens_used")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(response.into_body(), 8192)
            .await
            .unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let metrics = json["metrics"].as_array().unwrap();
        assert_eq!(metrics.len(), 1);
        assert_eq!(metrics[0]["name"], "tokens_used");
    }

    #[tokio::test]
    async fn test_metrics_query_filter_by_labels() {
        let app = build_router(state_with_metrics());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/metrics?labels=model:deepseek")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(response.into_body(), 8192)
            .await
            .unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let metrics = json["metrics"].as_array().unwrap();
        // 只有 tokens_used 带 model 标签
        assert!(metrics.iter().all(|m| m["name"] == "tokens_used"));
    }

    #[tokio::test]
    async fn test_metrics_query_no_aggregator_404() {
        // 未注入 metrics → 404
        let app = build_router(AppState::new());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/metrics")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn test_metrics_prometheus_endpoint() {
        let app = build_router(state_with_metrics());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/metrics")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = axum::body::to_bytes(response.into_body(), 8192)
            .await
            .unwrap();
        let text = String::from_utf8(body.to_vec()).unwrap();
        // counter 导出
        assert!(text.contains("# HELP llm_service_tokens_used Total tokens used"));
        assert!(text.contains("# TYPE llm_service_tokens_used counter"));
        assert!(text.contains("llm_service_tokens_used{model=\"deepseek\"}"));
        // histogram 导出
        assert!(text.contains("# TYPE llm_service_latency histogram"));
        assert!(text.contains("llm_service_latency_bucket{le=\"0.025\"}"));
        assert!(text.contains("llm_service_latency_bucket{le=\"+Inf\"}"));
        assert!(text.contains("llm_service_latency_count"));
    }

    #[tokio::test]
    async fn test_metrics_prometheus_no_aggregator_404() {
        let app = build_router(AppState::new());
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/metrics")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_FOUND);
    }
}

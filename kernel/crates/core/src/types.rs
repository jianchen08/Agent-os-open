//! 核心类型定义
//!
//! 对应 0.1 的 `pipeline/types.py`，0.2 将 RouteSignal 精简为 4 种
//! （移除 Delegate / Fork / Decision，详见方案总纲 §3.5）。
//!
//! ADR 修订新增（v2.0）：
//! - SQLite 四表模型类型（ADR ④）：RunRecord / MessageRecord / TraceEntry / BlobRecord
//! - 多分支模型类型（ADR ⑤）：Branch / RunStatus / PatchType
//! - 内容懒加载（ADR ⑦）：ContentLoader
//! - 引擎结果类型（ADR ①）：StepResult / SuspendHandle / WakeEvent / EngineError

use std::collections::HashMap;
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::traits::StorageBackend;

// ── 路由信号 ──────────────────────────────────────────────────

/// 路由类型枚举（0.2 精简为 4 种）。
///
/// 移除决策依据：
/// - `delegate` 语义被工具调用覆盖（子管道触发走专门服务的工具调用）
/// - `fork` 与 state 隔离原则冲突（子管道独立 state 互不共享）
/// - `decision` 下沉为路由表条件分支（引擎不作为独立信号消费）
///
/// [来源: docs/0.2_rust_plugin_solution.md §3.5]
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RouteType {
    /// 下一轮调用 LLM
    NextLlm,
    /// 执行工具
    NextTool,
    /// 结束管道
    End,
    /// 挂起等待外部事件
    Wait,
}

/// 路由信号：由输出插件产生，经输出路由表仲裁后决定管道下一步走向。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RouteSignal {
    /// 路由类型
    pub route_type: RouteType,
    /// 路由目标（工具名、LLM 标识等），可为 None
    #[serde(skip_serializing_if = "Option::is_none")]
    pub target: Option<Vec<String>>,
    /// 路由原因描述
    #[serde(default)]
    pub reason: String,
    /// 附加数据
    #[serde(skip_serializing_if = "Option::is_none")]
    pub payload: Option<serde_json::Value>,
}

impl RouteSignal {
    pub fn new(route_type: RouteType) -> Self {
        Self {
            route_type,
            target: None,
            reason: String::new(),
            payload: None,
        }
    }

    pub fn with_reason(mut self, reason: impl Into<String>) -> Self {
        self.reason = reason.into();
        self
    }

    pub fn with_target(mut self, target: Vec<String>) -> Self {
        self.target = Some(target);
        self
    }
}

// ── 插件结果 ──────────────────────────────────────────────────

/// 插件执行结果。
///
/// 对应 0.1 的 `pipeline/plugin.py PluginResult`。
///
/// **ADR ③ 关键设计**：`state_updates` 本质就是 Patch——插件返回"我想改什么"，
/// 引擎收到后决定是否应用、怎么应用。插件不直接操作引擎存储。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PluginResult {
    /// 需要合并到管道状态的更新（本质是 Patch，ADR ③）
    #[serde(default)]
    pub state_updates: HashMap<String, serde_json::Value>,
    /// 路由信号（仅输出插件有效）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub route_signal: Option<RouteSignal>,
    /// 是否跳过后续插件
    #[serde(default)]
    pub skip_remaining: bool,
    /// 执行过程中的异常信息（None 表示成功）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<PluginError>,
}

impl PluginResult {
    pub fn with_state_updates(mut self, updates: HashMap<String, serde_json::Value>) -> Self {
        self.state_updates = updates;
        self
    }

    pub fn with_route_signal(mut self, signal: RouteSignal) -> Self {
        self.route_signal = Some(signal);
        self
    }

    pub fn with_error(mut self, error: PluginError) -> Self {
        self.error = Some(error);
        self
    }
}

/// 插件执行错误。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PluginError {
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub code: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
}

impl std::fmt::Display for PluginError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match &self.code {
            Some(code) => write!(f, "[{}] {}", code, self.message),
            None => write!(f, "{}", self.message),
        }
    }
}

impl std::error::Error for PluginError {}

// ── 插件上下文（ADR ⑦ 新增 ContentLoader） ──────────────────────

/// 插件执行上下文。
///
/// 对应 0.1 的 `pipeline/plugin.py PluginContext`。
/// 封装管道状态、插件配置和服务访问能力，传递给每个插件的 execute 方法。
///
/// **ADR ⑦ 改造**：新增 `content_loader` 字段，实现内容懒加载。
/// `state` 字段不再包含完整消息内容——只存摘要（role、content_preview、blob_id）。
/// 插件需要完整内容时，通过 `content_loader` 按需从 blobs 表加载。
#[derive(Clone)]
pub struct PluginContext {
    /// 管道当前状态（JSON Value 形式，支持嵌套）
    ///
    /// ADR ⑦：状态摘要（不含完整消息内容），完整内容通过 content_loader 按需加载
    pub state: serde_json::Value,
    /// 插件配置
    pub config: serde_json::Value,
    /// 当前租户上下文
    pub tenant: TenantContext,
    /// 管道 ID
    pub pipeline_id: Uuid,
    /// 会话 ID
    pub session_id: String,
    /// 任务 ID
    pub task_id: String,
    /// 内容懒加载句柄（ADR ⑦）
    ///
    /// 引擎注入的 BLOB 加载器，插件按需调用加载消息完整内容
    pub content_loader: ContentLoader,
}

impl std::fmt::Debug for PluginContext {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PluginContext")
            .field("state", &self.state)
            .field("config", &self.config)
            .field("tenant", &self.tenant)
            .field("pipeline_id", &self.pipeline_id)
            .field("session_id", &self.session_id)
            .field("task_id", &self.task_id)
            .field("content_loader", &self.content_loader)
            .finish()
    }
}

impl PluginContext {
    pub fn new(
        state: serde_json::Value,
        config: serde_json::Value,
        tenant: TenantContext,
        pipeline_id: Uuid,
        content_loader: ContentLoader,
    ) -> Self {
        Self {
            state,
            config,
            tenant,
            pipeline_id,
            session_id: String::new(),
            task_id: String::new(),
            content_loader,
        }
    }
}

// ── 内容懒加载（ADR ⑦） ───────────────────────────────────────

/// 内容懒加载句柄（ADR ⑦）。
///
/// 引擎在构造 PluginContext 时注入此对象。插件通过它按需从 SQLite blobs 表
/// 加载消息完整内容，避免全量加载到内存。
///
/// [来源: docs/working/adr_engine_design.md §5.3]
pub struct ContentLoader {
    /// SQLite 存储句柄（四表：runs/messages/traces/blobs）
    store: Arc<dyn StorageBackend>,
    /// 当前运行实例 ID
    run_id: String,
    /// 当前分支 ID（ADR ⑤）
    branch_id: String,
}

impl ContentLoader {
    /// 创建内容加载器。
    ///
    /// # Arguments
    /// * `store` - SQLite 存储后端
    /// * `run_id` - 运行实例 ID
    /// * `branch_id` - 当前分支 ID
    pub fn new(store: Arc<dyn StorageBackend>, run_id: String, branch_id: String) -> Self {
        Self {
            store,
            run_id,
            branch_id,
        }
    }
}

impl std::fmt::Debug for ContentLoader {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ContentLoader")
            .field("run_id", &self.run_id)
            .field("branch_id", &self.branch_id)
            .finish()
    }
}

impl Clone for ContentLoader {
    fn clone(&self) -> Self {
        Self {
            store: Arc::clone(&self.store),
            run_id: self.run_id.clone(),
            branch_id: self.branch_id.clone(),
        }
    }
}

// ── 租户上下文 ──────────────────────────────────────────────────

/// 多租户上下文。
///
/// 通过 `tokio::task_local!` 穿透整个异步调用栈，插件代码无需感知租户参数。
/// [来源: docs/0.2_rust_plugin_solution.md §3.4]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TenantContext {
    pub tenant_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub user_id: Option<String>,
    pub session_id: String,
    /// 用户角色（admin / member / ...）。None 表示未指定。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
    /// 权限列表（如 ["pipeline:run", "tool:invoke"]）。
    #[serde(default)]
    pub permissions: Vec<String>,
    /// 该租户下启用的插件 ID 白名单；空表示无限制。
    #[serde(default)]
    pub enabled_plugins: Vec<String>,
    /// 凭证句柄（指向密钥库中的条目），由 HTTP 入口注入，插件按需引用。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub credential_handle: Option<String>,
}

impl TenantContext {
    pub fn new(tenant_id: impl Into<String>, session_id: impl Into<String>) -> Self {
        Self {
            tenant_id: tenant_id.into(),
            user_id: None,
            session_id: session_id.into(),
            role: None,
            permissions: Vec::new(),
            enabled_plugins: Vec::new(),
            credential_handle: None,
        }
    }

    pub fn with_role(mut self, role: impl Into<String>) -> Self {
        self.role = Some(role.into());
        self
    }

    pub fn with_permissions(mut self, permissions: Vec<String>) -> Self {
        self.permissions = permissions;
        self
    }

    pub fn with_enabled_plugins(mut self, enabled_plugins: Vec<String>) -> Self {
        self.enabled_plugins = enabled_plugins;
        self
    }

    pub fn with_credential_handle(mut self, handle: impl Into<String>) -> Self {
        self.credential_handle = Some(handle.into());
        self
    }
}

// ── 工具元信息 ──────────────────────────────────────────────────

/// 工具分类（与 Manifest capabilities.tools[].category 枚举对齐）。
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ToolCategory {
    File,
    FileSystem,
    Search,
    Web,
    Memory,
    Task,
    System,
    Execution,
    Analysis,
    Evaluation,
    Agent,
    Monitoring,
}

/// 工具来源。
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ToolSource {
    /// 内置工具（Rust 原生实现）
    Builtin,
    /// MCP 协议接入
    Mcp,
    /// 用户自定义
    Custom,
    /// 数据库配置
    Database,
    /// 运行时动态注册（G3：插件经 registry.register_tool capability 注册，
    /// 非 manifest 静态声明；进程内注册表 + scope 收回，不落内核存储——
    /// dynamic_tools 表已于 2026-08-19 退役，跨重启由插件自持 state/config 重建）。
    Dynamic,
}

/// 工具执行结果。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolExecutionResult {
    /// 是否成功
    pub success: bool,
    /// 输出数据
    pub data: serde_json::Value,
    /// 错误信息（success=false 时有值）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    /// 执行耗时（毫秒）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub duration_ms: Option<u64>,
}

impl ToolExecutionResult {
    pub fn success(data: serde_json::Value) -> Self {
        Self {
            success: true,
            data,
            error: None,
            duration_ms: None,
        }
    }

    pub fn failure(error: impl Into<String>) -> Self {
        Self {
            success: false,
            data: serde_json::Value::Null,
            error: Some(error.into()),
            duration_ms: None,
        }
    }
}

// ═════════════════════════════════════════════════════════════════
// ADR ④⑤：SQLite 四表模型 + 多分支模型
// ═════════════════════════════════════════════════════════════════

/// 运行实例状态（runs 表 status 字段）。
///
/// [来源: docs/working/adr_engine_design.md §4.2 表1]
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RunStatus {
    /// 运行中
    Running,
    /// 已挂起（ADR ⑤：保存分支状态，等待外部事件）
    Suspended,
    /// 已完成
    Completed,
    /// 已失败
    Failed,
}

/// runs 表记录——运行实例元数据。
///
/// 对应 SQLite 四表中的 `runs` 表。
///
/// [来源: docs/working/adr_engine_design.md §4.2 表1]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunRecord {
    /// 运行实例唯一 ID（UUID）
    pub run_id: String,
    /// YAML 配置哈希（配置即产品，ADR ⑪）
    pub config_hash: String,
    /// 运行状态
    pub status: RunStatus,
    /// 多租户隔离
    pub tenant_id: String,
    /// 创建时间（ISO8601）
    pub created_at: String,
    /// 结束时间（None = 未结束）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ended_at: Option<String>,
    /// 当前活跃分支 ID（ADR ⑤）
    pub current_branch: String,
    /// 当前分支内序列号（ADR ⑤）
    pub current_seq: u32,
    /// 附加元数据（JSON：Agent ID、会话 ID 等）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub metadata: Option<serde_json::Value>,
}

/// 管道运行快照（统一管道管理查询：`GET /api/v1/pipelines/runs`）。
///
/// runs × message_slots × pipeline_sessions 三表联结：
/// - run → pipeline 映射经 message_slots.run_id（op-based 落槽时写入）；
/// - pipeline → 会话映射经 pipeline_sessions；
/// - 消耗账本真值在 state 的 track.total_tokens（0.1 的 pipeline_run_summaries
///   投影表已退役，2026-08-19）。
///
/// 无消息槽的 run（旧引擎 start_run 占位）在查询层被过滤，只呈现真实执行的管道。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PipelineRunInfo {
    /// 运行实例唯一 ID（UUID）
    pub run_id: String,
    /// 所属管道 ID（消息层主键，可空——理论上查询层已过滤）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pipeline_id: Option<String>,
    /// 归属会话（thread）ID，可空（pipeline_sessions 未建映射的历史数据）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thread_id: Option<String>,
    /// 运行状态
    pub status: RunStatus,
    /// 创建时间（ISO8601，即开始时间）
    pub started_at: String,
    /// 结束时间（None = 未结束）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ended_at: Option<String>,
}

/// messages 表记录——消息表（含分支标识）。
///
/// 对应 SQLite 四表中的 `messages` 表。
///
/// **关键设计**（ADR ⑤⑦）：
/// - `branch_id` + `seq_in_branch` 实现多分支模型
/// - `blob_id` 指向 blobs 表，内容懒加载
/// - `content_preview` 仅存极短摘要（ADR ④废除完整 preview 机制）
///
/// [来源: docs/working/adr_engine_design.md §4.2 表2]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MessageRecord {
    /// 消息唯一 ID（UUID）
    pub message_id: String,
    /// 所属运行实例 ID
    pub run_id: String,
    /// 分支标识（ADR ⑤）
    pub branch_id: String,
    /// 分支内序列号（ADR ⑤）
    pub seq_in_branch: u32,
    /// 消息角色（system / user / assistant / tool）
    pub role: String,
    /// 内容指向 BLOB（ADR ⑦ 懒加载）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub blob_id: Option<String>,
    /// 内容预览（ADR ④：仅存极短摘要，非完整内容）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content_preview: Option<String>,
    /// 创建时间（ISO8601）
    pub created_at: String,
    /// 所属管道 ID（= 其他项目的会话 id）。消息层查询主键，对齐 0.1 pipeline_run_id。
    /// 可空，兼容迁移前的历史数据。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pipeline_id: Option<String>,
    /// assistant 消息的工具调用数组（JSON 序列化）。
    /// 仅 role=assistant 且携带 tool_calls 时非空，对齐 OpenAI tool_calls 结构。
    /// 投影层把 messages 数组里 assistant 的 tool_calls 序列化进此列，
    /// 使消息表能完整表达多轮工具调用，而非只存扁平文本。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_calls_json: Option<String>,
    /// 工具结果消息（role=tool）对应的工具调用 ID。
    /// 与 tool_calls_json 配合还原完整的"调用-结果"配对。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
    /// assistant 消息的思考内容（LLM reasoning/chain-of-thought）。
    /// 前端据此渲染"思考过程"折叠区。仅 role=assistant 且模型输出思考时非空。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reasoning_content: Option<String>,
    /// 工具结果消息（role=tool）的执行状态：completed / failed。
    /// 非 tool 消息为 None。前端刷新后据此（与 error）还原失败态，
    /// 与流式 tool_result 事件的 success 信号统一。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub status: Option<String>,
    /// 工具结果消息（role=tool）的错误文本。status=failed 时非空。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    /// 工具结果消息（role=tool）的结构化工具结果 envelope（JSON 序列化）。
    /// 含 call_id/tool_name/success/error/data/metadata/duration_ms。
    /// 由投影层从消息数组的 `tool_result` 字段序列化而来（messages 数组本身是
    /// LLM 上下文协议不携带该字段）；HTTP 读侧解析后以 toolResultData 等
    /// camelCase 字段返回，前端据此还原 resultData/durationMs——冷热路径一致。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_result_json: Option<String>,
    /// 消息自定义元数据（blob 全文原样提取）。user 消息携带
    /// `client_message_id`（前端幂等键，ADR 2026-08-21 消息幂等契约）——
    /// GET messages 原样回显，前端据此把乐观消息与权威记录对账去重。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub metadata: Option<serde_json::Value>,
}

/// sessions 表记录——会话标签夹（域2，对齐 0.1 SessionModel）。
///
/// **解耦设计**：会话只是一个聚合管道引用的标签夹，自身不存储消息。
/// `pipeline_ids` 是 JSON 引用列表（对齐 0.1 `SessionModel.pipeline_ids`），
/// 消息按 pipeline_id 在 messages 表自治存储，会话层不反向 join。
///
/// [来源: src/infrastructure/session/models.py SessionModel]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionRecord {
    /// 会话 ID（= thread_id = 0.1 session_id）
    pub thread_id: String,
    /// 会话标题（可空）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    /// 意图描述（可空）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub intent: Option<String>,
    /// 当前状态（active/idle 等），默认 active
    pub current_state: String,
    /// 关联 agent ID（可空）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub agent_id: Option<String>,
    /// 最近活跃的 pipeline_id（仅引用；子管道注册时不覆盖它，对齐 0.1 set_active=False）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub active_pipeline_id: Option<String>,
    /// 属于本会话的 pipeline_id 引用列表（对齐 0.1 SessionModel.pipeline_ids）
    #[serde(default)]
    pub pipeline_ids: Vec<String>,
    /// 元数据（session_type/pinned/starred 等，JSON）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub metadata: Option<serde_json::Value>,
    /// 创建时间（ISO8601）
    pub created_at: String,
    /// 更新时间（ISO8601）
    pub updated_at: String,
    /// 最近活跃时间（可空）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_active_at: Option<String>,
}

/// users 表记录——持久化用户（0.5.0 完整用户系统的最小持久化地基）。
///
/// 0.2.0 auth 为硬编码单 admin 占位实现；本次为多租户隔离测试落地最小持久化：
/// register 真实建用户，login/me/refresh/WS 查 DB。RBAC/JWT 签名/bcrypt 哈希/
/// 凭据保险库留给 0.5.0（见 auth.rs DEBT 标注 + ROADMAP §0.5）。
///
/// **租户粒度**：一用户一租户——注册时 `tenant_id = user_id`，保证不同用户数据隔离。
/// admin 种子用户 tenant_id = "default"。
///
/// 密码明文存储（演示环境，DEBT 标注待 0.5.0 替换为哈希）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UserRecord {
    /// 用户 ID（uuid；admin 种子为固定值）
    pub user_id: String,
    /// 用户名（跨租户全局唯一，登录键）
    pub username: String,
    /// 密码（明文，DEBT: 0.5.0 替换为哈希）
    pub password: String,
    /// 邮箱（可空）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub email: Option<String>,
    /// 角色（admin/user；RBAC 完整化留给 0.5.0）
    pub role: String,
    /// 归属租户 ID（一用户一租户：注册时 = user_id；admin = "default"）
    pub tenant_id: String,
    /// 创建时间（ISO8601）
    pub created_at: String,
    /// 最近登录时间（可空）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_login_at: Option<String>,
}

/// traces 表 Patch 类型（traces 表 patch_type 字段）。
///
/// [来源: docs/working/adr_engine_design.md §4.2 表3]
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PatchType {
    /// 状态更新 Patch（对应 PluginResult.state_updates）
    StateUpdate,
    /// 路由信号 Patch（对应 PluginResult.route_signal）
    RouteSignal,
    /// 错误 Patch（对应 PluginResult.error）
    Error,
    /// 生命周期事件 Patch
    Lifecycle,
    /// 回滚操作 Patch（ADR ⑤：回滚操作本身也记录在 traces 表）
    Rollback,
}

/// traces 表记录——状态变更日志（Append-Only Patch）。
///
/// 对应 SQLite 四表中的 `traces` 表。
///
/// **关键设计**（ADR ③）：
/// - **Append-Only**：只追加，永不修改、永不删除
/// - **Patch 记录**：每条 trace 记录一个 PluginResult 的变更
/// - **正向重放**：回滚时按 branch_id + seq_in_branch 正向重放 Patch 恢复状态
///
/// [来源: docs/working/adr_engine_design.md §4.2 表3]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TraceEntry {
    /// 日志条目唯一 ID
    pub trace_id: String,
    /// 所属运行实例 ID
    pub run_id: String,
    /// 所属分支
    pub branch_id: String,
    /// 分支内序列号
    pub seq_in_branch: u32,
    /// 产生此 Patch 的插件 ID
    pub plugin_id: String,
    /// Patch 类型
    pub patch_type: PatchType,
    /// Patch 内容（JSON，对应 PluginResult 的字段）
    pub patch_data: serde_json::Value,
    /// 创建时间（ISO8601）
    pub created_at: String,
}

/// blobs 表记录——不可变原始数据。
///
/// 对应 SQLite 四表中的 `blobs` 表。
///
/// **关键设计**（ADR ③⑦）：
/// - **不可变**：只增不改不删
/// - **内容寻址**：blob_id = 内容哈希，相同内容自动去重
/// - **懒加载**：messages 表只存 blob_id，引擎按需加载
///
/// [来源: docs/working/adr_engine_design.md §4.2 表4]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BlobRecord {
    /// BLOB 唯一 ID（内容哈希）
    pub blob_id: String,
    /// MIME 类型（text/plain / application/json / image/png ...）
    pub mime_type: String,
    /// 数据大小（字节）
    pub size_bytes: u64,
    /// 创建时间（ISO8601）
    pub created_at: String,
}

// ── ADR ⑤：多分支模型 ───────────────────────────────────────

/// 分支模型（ADR ⑤）。
///
/// 回滚通过创建新分支 + 正向重放 Patch 恢复状态，不删除不逆操作。
///
/// **分支模型**：
/// ```text
/// 主分支 (branch_id = "main")
///   seq=1  消息1  Patch1
///   seq=2  消息2  Patch2
///   seq=3  消息3  Patch3  ← 发现问题，需要回滚到 seq=1
///   │
///   └─ 创建新分支 (branch_id = "main.rollback.001")
///       parent_branch = "main"
///       parent_seq = 1                    ← 回滚目标
///       │ 正向重放 main 分支 seq=1 的 Patch
///       │ → 恢复状态到 seq=1 的快照
///       │
///       seq=1  消息4  Patch4  ← 新的执行从这里开始
/// ```
///
/// [来源: docs/working/adr_engine_design.md §4.3]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Branch {
    /// 分支唯一 ID（如 "main"、"main.rollback.001"）
    pub branch_id: String,
    /// 所属运行实例 ID
    pub run_id: String,
    /// 父分支 ID（根分支为 None）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parent_branch: Option<String>,
    /// 父分支回滚目标序列号（根分支为 None）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parent_seq: Option<u32>,
    /// 创建时间（ISO8601）
    pub created_at: String,
}

// ── ADR ①：引擎结果类型 ──────────────────────────────────────

/// 挂起句柄（旧引擎 AdrEngine::suspend 返回；审批闭环 resume 协议沿用）。
///
/// 保存当前分支状态，等待外部事件恢复执行。
/// [来源: docs/working/adr_engine_design.md §3.3]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SuspendHandle {
    /// 运行实例 ID
    pub run_id: String,
    /// 当前分支 ID
    pub branch_id: String,
    /// 当前序列号
    pub seq: u32,
}

/// 引擎错误。
///
/// [来源: docs/working/adr_engine_design.md §3.3]
#[derive(Debug, Clone, thiserror::Error)]
pub enum EngineError {
    /// 运行实例不存在
    #[error("run not found: {run_id}")]
    RunNotFound { run_id: String },

    /// 运行实例状态不允许此操作
    #[error("run '{run_id}' is in invalid state: {reason}")]
    InvalidState { run_id: String, reason: String },

    /// 存储错误
    #[error("storage error: {0}")]
    Storage(#[from] StorageError),

    /// 插件调用错误
    #[error("plugin error: {0}")]
    Plugin(#[from] PluginError),

    /// 配置错误
    #[error("config error: {message}")]
    Config { message: String },

    /// 其他错误
    #[error("engine error: {message}")]
    Other { message: String },
}

/// 存储错误。
///
/// [来源: docs/working/adr_engine_design.md §4.2]
#[derive(Debug, Clone, thiserror::Error)]
pub enum StorageError {
    /// 记录不存在
    #[error("not found: {0}")]
    NotFound(String),

    /// 序列化/反序列化错误
    #[error("serialization error: {0}")]
    Serialization(String),

    /// 数据库错误
    #[error("database error: {0}")]
    Database(String),

    /// IO 错误
    #[error("io error: {0}")]
    Io(String),
}

/// 自动将 [`rusqlite::Error`] 转为 [`StorageError::Database`]，
/// 消息格式为 `database error: <rusqlite>`，与 `StorageError::Database` 的 Display 契约一致。
/// 这样子调用处可直接用 `?` 自动转换，消除重复样板。
impl From<rusqlite::Error> for StorageError {
    fn from(e: rusqlite::Error) -> Self {
        StorageError::Database(e.to_string())
    }
}

// ── 配置驱动的管道配置类型（统一 step 模型）──────────────────

/// 路由跳转目标。
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RouteNext {
    /// 继续循环（管道级 loop 的下一轮）
    Loop,
    /// 结束管道
    End,
    /// 挂起等待
    Wait,
    /// 跳转到指定 step id
    Step(String),
    /// 转移到指定循环体（`exit_routes` / step 级路由使用；step 级路由设置后
    /// 在本循环体结束时生效）
    Phase(String),
}

/// 路由分支的 then 动作。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RouteAction {
    /// 跳转目标
    pub next: RouteNext,
    /// 设置 state 字段（merge 进 state）
    #[serde(default)]
    pub set: HashMap<String, serde_json::Value>,
}

/// 路由分支：when 条件 → then 动作。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Route {
    /// 条件表达式字符串（如 "raw_tool_calls != []"），空串或 "True" 视为始终匹配
    pub when: String,
    /// 匹配时执行的动作
    pub then: RouteAction,
}

/// 循环配置。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LoopConfig {
    /// 是否启用循环
    #[serde(default)]
    pub enabled: bool,
    /// 最大迭代次数（-1=无限循环；>0=安全阀）
    #[serde(default = "default_max_iterations")]
    pub max_iterations: i32,
}

fn default_max_iterations() -> i32 {
    -1
}

impl Default for LoopConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            max_iterations: -1,
        }
    }
}

/// 检查点配置：每 N 个配置 step（实际执行）把当时的完整 state 复制一份到
/// pipeline_checkpoints 表，作为留档快照。冷启动重建时优先从最近的 checkpoint
/// 恢复（O(1) 取基线），再回放其后 traces 的增量 ops，避免长会话全量回放。
///
/// 计数单位 = **配置 step**（引擎在 persist_step_trace 起始推进，与轨迹同为
/// 配置 step 边界；组级 when 跳过的 step 不执行不计步，step 内部循环一次计一步）。
///
/// checkpoint 存全量 state（非 diff）：用存储换 O(1) 恢复速度，
/// 与 traces 的增量化（省存储）配套——traces 变薄后，checkpoint 补偿重建速度。
/// checkpoint 表是状态表的留档副本，刻意冗余，N 步才产生一份，稀疏。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CheckpointConfig {
    /// 是否启用定期 checkpoint。
    #[serde(default = "default_checkpoint_enabled")]
    pub enabled: bool,
    /// 每隔多少个配置 step 打一次全量快照（引擎可配）。0 或负数 = 禁用。
    /// 默认 1000：长会话每千步留一份基线，重建成本可控。
    #[serde(default = "default_checkpoint_interval")]
    pub interval_steps: i64,
}

fn default_checkpoint_enabled() -> bool {
    true
}

fn default_checkpoint_interval() -> i64 {
    1000
}

impl Default for CheckpointConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            interval_steps: 1000,
        }
    }
}

/// step 列表项：step id / step 库 id / 插件名的引用，可带可选 when 门。
///
/// YAML 两种形态（untagged，同一字段集）：
/// - 裸字符串：`- pipeline_tool_schema`（无 when 门，缺省 True）；
/// - 对象：`- name: pipeline_godot_context` + `when: "state.selected != ''"`。
///
/// 门语义（G9）：引擎在该项 invoke **前**对 state 求值（复用 `eval_condition`
/// 安全求值器，与路由 when 同语法同求值器），假则整项跳过（零调用）；
/// 表达式非法按求值器现行兜底返回 false = 跳过。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum StepItem {
    /// 裸引用（三级命中：当前管道 step id → 公共 step 库 → 插件名）。
    Bare(String),
    /// 带 when 门的引用。
    Gated {
        /// 引用名（三级命中，同 Bare）。
        name: String,
        /// 进入门条件；None 等价 Bare。
        #[serde(default)]
        when: Option<String>,
        /// 本项针对该插件的输入参数（per-plugin inputs，2026-08-18 新增）。
        ///
        /// 仅对**插件原子项**（三级命中③）生效：经既有 config 通道传给插件
        /// （载荷 `config.inputs`），**不 merge 进 state、不落 trace**。
        /// Composite（命中①②）/ Dynamic 项忽略该字段。空 = 等价旧行为。
        #[serde(default, skip_serializing_if = "HashMap::is_empty")]
        inputs: HashMap<String, serde_json::Value>,
    },
}

impl StepItem {
    /// 引用名（三级命中目标）。
    pub fn name(&self) -> &str {
        match self {
            StepItem::Bare(n) => n,
            StepItem::Gated { name, .. } => name,
        }
    }

    /// when 门条件（None = 无条件执行）。
    pub fn when(&self) -> Option<&str> {
        match self {
            StepItem::Bare(_) => None,
            StepItem::Gated { when, .. } => when.as_deref(),
        }
    }

    /// per-plugin inputs（仅 Gated 形态携带；Bare 恒空）。编译期复制一次，
    /// 供构建 `CompiledItem::Plugin`；运行时通过 config 通道传给插件。
    pub fn inputs(&self) -> HashMap<String, serde_json::Value> {
        match self {
            StepItem::Bare(_) => HashMap::new(),
            StepItem::Gated { inputs, .. } => inputs.clone(),
        }
    }
}

impl From<&str> for StepItem {
    fn from(s: &str) -> Self {
        StepItem::Bare(s.to_string())
    }
}

impl From<String> for StepItem {
    fn from(s: String) -> Self {
        StepItem::Bare(s)
    }
}

/// 管道步骤（统一 step 模型：原子插件和组合节点都是 step）。
///
/// `steps` 字段引用的内容按三级命中规则解析：
/// ① 当前管道 step id → 组合节点递归
/// ② 公共 step 库 id → 组合节点递归
/// ③ 插件名 → 原子插件 invoker 调用
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PipelineStep {
    /// 步骤标识（可被其他 step 的 steps 引用）
    pub id: String,
    /// 要执行的内容（step id 或插件名，三级命中）；项可带 when 门（G9）
    #[serde(default)]
    pub steps: Vec<StepItem>,
    /// 本步骤的进入门（G9）：对 state 求值，假则整组跳过（组内零调用）。
    /// 缺省 None = 无条件执行。语法/求值器与列表项 when、路由 when 同源。
    #[serde(default)]
    pub when: Option<String>,
    /// 上下文注入（自由 key-value，执行时 merge 进 state 供插件读取）
    #[serde(default)]
    pub context: HashMap<String, serde_json::Value>,
    /// 该步骤的路由分支（无则顺序执行下一步）
    #[serde(default)]
    pub routes: Vec<Route>,
    /// 该步骤的循环配置（组合节点可自带循环，如批量处理）
    #[serde(default)]
    pub loop_config: Option<LoopConfig>,
}

/// 管道循环体：管道由多个循环体顺序组成（如 init → main → exit）。
///
/// 每个循环体拥有独立的 steps 与循环配置：
/// - `loop_config` 缺省/disabled 且无 `while_cond` → 单次执行（前处理/后处理体，如 init/exit）；
/// - `loop_config` enabled 或 `while_cond` 存在 → 循环执行直至 `ended` / `suspended` /
///   `while_cond` 为假 / `max_iterations`；
/// - 循环体结束后的转移：`exit_routes` 命中（`RouteNext::Phase`）→ 跳转到指定循环体；
///   未命中/未声明 → 默认顺序进入下一个循环体；最后一个循环体结束 = run 结束。
/// - `run_on_error`：管道提前终止（`ended` / 出错）时仍执行本循环体（收尾语义，
///   如 exit 体的 workspace 合并与环境释放）。挂起（`suspended`）不触发。
///
/// `while_cond` 与 `loop_config` 的关系（G10 统一 DSL）：`while: "expr"` 是循环体
/// 循环继续条件的表达式形态（条件永远 `when`/`while`、目标永远 `then`、缺省顺序
/// 推进的单一风格），与 `loop_config.enabled` 兼容并存——任一开启即循环模式，
/// 每轮循环开头对 `while_cond` 求值（同一 `eval_condition` 求值器），假则退出循环。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LoopBody {
    /// 循环体标识（执行期间写入 `state["current_phase"]`，插件据此分发）
    pub id: String,
    /// 循环体内的步骤（三级命中：当前管道 step id / 公共 step 库 / 插件名）
    #[serde(default)]
    pub steps: Vec<PipelineStep>,
    /// 本循环体的循环配置；None/disabled 且无 while_cond = 单次执行
    #[serde(default)]
    pub loop_config: Option<LoopConfig>,
    /// 循环继续条件表达式（G10 新 DSL `while: "expr"`）；None = 无条件。
    /// YAML 书写键为 `while`（Rust 关键字规避，serde rename）。
    #[serde(default, skip_serializing_if = "Option::is_none", rename = "while")]
    pub while_cond: Option<String>,
    /// 循环体结束后的转移路由（默认顺序进入下一个循环体）
    #[serde(default)]
    pub exit_routes: Vec<Route>,
    /// 提前终止后仍执行本循环体（收尾语义）
    #[serde(default)]
    pub run_on_error: bool,
}

/// 管道配置（配置驱动的执行流程定义）。
///
/// 引擎作为配置解释执行器：读 PipelineConfig，按 `loop_bodies` 顺序执行每个
/// 循环体（各自独立的 steps 与循环配置），据 exit_routes/routes 决定循环、
/// 分支与循环体间转移。一套引擎 + 不同 YAML = 不同行为。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PipelineConfig {
    /// 管道名
    pub name: String,
    /// 有序循环体序列（默认顺序推进；`exit_routes` / step 级 `RouteNext::Phase`
    /// 可显式转移到指定循环体）
    #[serde(default)]
    pub loop_bodies: Vec<LoopBody>,
    /// 检查点配置：每 N 步把完整 state 复制到 pipeline_checkpoints 留档，
    /// 供冷启动重建时优先恢复（O(1) 取基线 + 回放其后增量）。
    #[serde(default)]
    pub checkpoint: CheckpointConfig,
}

impl PipelineConfig {
    /// 按 id 查找步骤（命中规则①：当前管道 step id，跨全部循环体）。
    pub fn find_step(&self, id: &str) -> Option<&PipelineStep> {
        self.loop_bodies
            .iter()
            .flat_map(|b| b.steps.iter())
            .find(|s| s.id == id)
    }

    /// 收集所有 step id（用于重名检测，跨全部循环体）。
    pub fn step_ids(&self) -> Vec<&str> {
        self.loop_bodies
            .iter()
            .flat_map(|b| b.steps.iter())
            .map(|s| s.id.as_str())
            .collect()
    }

    /// 按 id 定位循环体下标（供转移跳转用）。
    pub fn body_index(&self, id: &str) -> Option<usize> {
        self.loop_bodies.iter().position(|b| b.id == id)
    }

    /// 单循环体便捷构造（测试用）：一个 main 体承载全部 steps。
    pub fn single_body(
        name: impl Into<String>,
        loop_config: LoopConfig,
        steps: Vec<PipelineStep>,
    ) -> Self {
        Self {
            name: name.into(),
            loop_bodies: vec![LoopBody {
                id: "main".to_string(),
                steps,
                loop_config: Some(loop_config),
                while_cond: None,
                exit_routes: vec![],
                run_on_error: false,
            }],
            checkpoint: Default::default(),
        }
    }
}

/// 公共 step 库（config/steps/*.yaml 加载的可复用 step 定义）。
#[derive(Debug, Clone, Default)]
pub struct StepLibrary {
    /// id → step 定义
    pub steps: HashMap<String, PipelineStep>,
}

impl StepLibrary {
    /// 按 id 查找公共 step（命中规则②）。
    pub fn find(&self, id: &str) -> Option<&PipelineStep> {
        self.steps.get(id)
    }

    /// 收集所有 id（用于重名检测）。
    pub fn ids(&self) -> Vec<&str> {
        self.steps.keys().map(|s| s.as_str()).collect()
    }
}

//! 核心类型定义
//!
//! 对应 0.1 的 `pipeline/types.py`，0.2 将 RouteSignal 精简为 4 种
//! （移除 Delegate / Fork / Decision，详见方案总纲 §3.5）。
//!
//! ADR 修订新增（v2.0）：
//! - SQLite 四表模型类型（ADR ④）：RunRecord / MessageRecord / TraceEntry / BlobRecord
//! - 多分支模型类型（ADR ⑤）：Branch / RunStatus / PatchType
//! - 内容懒加载（ADR ⑦）：ContentLoader / Message
//! - 组合插件配置类型（ADR ⑥）：CompositeStep / CompositePluginConfig
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

// ── 错误策略 ──────────────────────────────────────────────────

/// 插件错误处理策略（与 0.1 对等）。
///
/// [来源: pipeline/types.py ErrorPolicy]
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum ErrorPolicy {
    /// 立即终止后续插件
    #[default]
    Abort,
    /// 记录警告继续
    Skip,
    /// 调用方实现重试循环
    Retry,
    /// 用兜底结果替代
    Fallback,
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
/// `requires_content: N` 从 manifest 读取，声明插件需要多少条最近消息的完整内容。
/// 引擎可据此预加载，也可由插件在运行时自行调用 `load_recent_messages`。
///
/// [来源: docs/working/adr_engine_design.md §5.3]
pub struct ContentLoader {
    /// SQLite 存储句柄（四表：runs/messages/traces/blobs）
    store: Arc<dyn StorageBackend>,
    /// 当前运行实例 ID
    run_id: String,
    /// 当前分支 ID（ADR ⑤）
    branch_id: String,
    /// 插件声明需要的最近消息条数（从 manifest requires_content 读取）
    pub requires_content: usize,
}

impl ContentLoader {
    /// 创建内容加载器。
    ///
    /// # Arguments
    /// * `store` - SQLite 存储后端
    /// * `run_id` - 运行实例 ID
    /// * `branch_id` - 当前分支 ID
    /// * `requires_content` - 需要预加载的最近消息条数
    pub fn new(
        store: Arc<dyn StorageBackend>,
        run_id: String,
        branch_id: String,
        requires_content: usize,
    ) -> Self {
        Self {
            store,
            run_id,
            branch_id,
            requires_content,
        }
    }

    /// 按需加载最近 N 条消息的完整内容。
    ///
    /// 从 messages 表查询最近 N 条消息的 blob_id，再从 blobs 表加载完整内容。
    pub async fn load_recent_messages(&self, n: usize) -> Result<Vec<Message>, StorageError> {
        self.store
            .get_recent_messages(&self.run_id, &self.branch_id, n)
            .await
    }

    /// 按需加载指定 blob_id 的内容。
    pub async fn load_blob(&self, blob_id: &str) -> Result<Vec<u8>, StorageError> {
        self.store.get_blob(blob_id).await
    }
}

impl std::fmt::Debug for ContentLoader {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ContentLoader")
            .field("run_id", &self.run_id)
            .field("branch_id", &self.branch_id)
            .field("requires_content", &self.requires_content)
            .finish()
    }
}

impl Clone for ContentLoader {
    fn clone(&self) -> Self {
        Self {
            store: Arc::clone(&self.store),
            run_id: self.run_id.clone(),
            branch_id: self.branch_id.clone(),
            requires_content: self.requires_content,
        }
    }
}

/// 消息完整内容（ContentLoader 返回）。
///
/// 包含消息 ID、角色和完整内容文本。
/// 对应 messages 表 + blobs 表联查的结果。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Message {
    /// 消息唯一 ID
    pub message_id: String,
    /// 消息角色（system / user / assistant / tool）
    pub role: String,
    /// 完整内容文本（从 blobs 表加载）
    pub content: String,
    /// 对应的 blob_id（内容寻址）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub blob_id: Option<String>,
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
}

impl TenantContext {
    pub fn new(tenant_id: impl Into<String>, session_id: impl Into<String>) -> Self {
        Self {
            tenant_id: tenant_id.into(),
            user_id: None,
            session_id: session_id.into(),
        }
    }
}

// ── 执行目标类型 ──────────────────────────────────────────────────

/// 核心执行目标类型。
///
/// 对应 0.1 的 `pipeline/types.py TargetType`。
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TargetType {
    /// LLM 调用
    LlmCall,
    /// 工具执行
    ToolExecute,
}

impl Default for TargetType {
    fn default() -> Self {
        Self::LlmCall
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

// ── ADR ⑥：组合插件配置类型 ───────────────────────────────────

/// 组合插件步骤配置（ADR ⑥）。
///
/// 每个步骤引用一个原子插件，走统一的 `execute(ctx) -> Result<PluginResult>` 接口。
/// 引擎解释执行时按步骤顺序调用原子插件，将输出写入 state。
///
/// [来源: docs/tasks/task_02_contract_definition.md §组合插件 YAML 配置示例]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CompositeStep {
    /// 步骤名称
    pub name: String,
    /// 引用的原子插件 ID
    pub plugin: String,
    /// 输入参数（支持变量插值 {{state.xxx}}）
    pub inputs: serde_json::Value,
    /// 输出映射（key → state 字段名, value → 模板表达式）
    #[serde(default)]
    pub outputs: HashMap<String, String>,
}

/// 组合插件配置（ADR ⑥）。
///
/// 组合插件由 YAML 配置编排步骤，引擎解释执行。
/// 组合插件不是新的 Rust trait——它是引擎层的"解释执行器"职责。
///
/// [来源: docs/tasks/task_02_contract_definition.md §组合插件 YAML 配置示例]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CompositePluginConfig {
    /// 步骤序列
    pub steps: Vec<CompositeStep>,
}

// ── ADR ①：引擎结果类型 ──────────────────────────────────────

/// 步骤执行结果（AdrEngine::execute_step 返回）。
///
/// [来源: docs/working/adr_engine_design.md §3.3]
#[derive(Debug, Clone)]
pub struct StepResult {
    /// 状态更新 Patch（追加到 traces 表）
    pub state_updates: HashMap<String, serde_json::Value>,
    /// 路由信号（决定下一步走向）
    pub route_signal: Option<RouteSignal>,
}

/// 挂起句柄（AdrEngine::suspend 返回）。
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

/// 唤醒事件（AdrEngine::resume 参数）。
///
/// [来源: docs/working/adr_engine_design.md §3.3]
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WakeEvent {
    /// 手动唤醒
    Manual,
    /// 定时器触发
    Timer,
    /// 外部 API 调用触发
    External,
    /// 工具执行完成
    ToolCompleted,
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

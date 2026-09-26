//! 核心类型定义
//!
//! 对应 0.1 的 `pipeline/types.py`。路由经 DSL 条件表达（G10 单轨裁定），
//! 控制流由插件写状态键、引擎轮边界消费
//! （控制状态键契约 ADR 2026-08-30：RunStatus::from_control_state 终态映射单点）。
//!
//! ADR 修订新增（v2.0）：
//! - SQLite 四表模型类型（ADR ④）：RunRecord / MessageRecord / TraceEntry
//! - 多分支模型类型（ADR ⑤）：RunStatus / PatchType（分支创建面已随 replay
//!   补偿写面退役，见 docs/decisions/2026-09-09-kernel-dead-layer-adjudication.md）
//! - 内容懒加载（ADR ⑦）：ContentLoader
//! - 引擎结果类型（ADR ①）：StepResult / WakeEvent / EngineError

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
///
/// `state` 为借用引用：管道步骤串行 await，invoke 期间引擎不写 state
/// （插件结果在返回后 merge），纯借用无并发冲突——消除每步调用的整包深拷。
#[derive(Clone)]
pub struct PluginContext<'a> {
    /// 管道当前状态（JSON Value 形式，支持嵌套）
    ///
    /// ADR ⑦：状态摘要（不含完整消息内容），完整内容通过 content_loader 按需加载
    pub state: &'a serde_json::Value,
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

impl std::fmt::Debug for PluginContext<'_> {
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

impl<'a> PluginContext<'a> {
    pub fn new(
        state: &'a serde_json::Value,
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
    /// SQLite 存储句柄（blobs/state/traces 懒加载）
    store: Arc<dyn StorageBackend>,
    /// 当前运行实例 ID（关联标签，state.run_id 同源）
    run_id: String,
    /// 所属管道 ID（内容按管道定位）
    pipeline_id: String,
}

impl ContentLoader {
    /// 创建内容加载器。
    ///
    /// # Arguments
    /// * `store` - SQLite 存储后端
    /// * `run_id` - 运行实例 ID（关联标签）
    /// * `pipeline_id` - 所属管道 ID
    pub fn new(store: Arc<dyn StorageBackend>, run_id: String, pipeline_id: String) -> Self {
        Self {
            store,
            run_id,
            pipeline_id,
        }
    }
}

impl std::fmt::Debug for ContentLoader {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ContentLoader")
            .field("run_id", &self.run_id)
            .field("pipeline_id", &self.pipeline_id)
            .finish()
    }
}

impl Clone for ContentLoader {
    fn clone(&self) -> Self {
        Self {
            store: Arc::clone(&self.store),
            run_id: self.run_id.clone(),
            pipeline_id: self.pipeline_id.clone(),
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
    /// 跨重启由插件自持 state/config 重建）。
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
    /// 工具自报扩展元数据（SDK ToolExecutionResult.metadata 顶层透传；
    /// task_failed / result=completed 等副作用信号的载体，tool_core 侧效
    /// 派生消费）。字段只增不删：缺省 None 兼容既有信封与构造点。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub metadata: Option<serde_json::Value>,
}

impl ToolExecutionResult {
    pub fn success(data: serde_json::Value) -> Self {
        Self {
            success: true,
            data,
            error: None,
            duration_ms: None,
            metadata: None,
        }
    }

    pub fn failure(error: impl Into<String>) -> Self {
        Self {
            success: false,
            data: serde_json::Value::Null,
            error: Some(error.into()),
            duration_ms: None,
            metadata: None,
        }
    }
}

// ═════════════════════════════════════════════════════════════════
// ADR ④⑤：SQLite 四表模型 + 多分支模型
// ═════════════════════════════════════════════════════════════════

/// pending 输入来源（等待队列条目前端标注用，四入口统一）。
///
/// 前端发送 = user；触发器注入 = trigger；任务/工具派发 = task；HTTP 直连 = http；
/// 内核/系统内部 = system。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PendingInputSource {
    /// 前端聊天发送
    User,
    /// 触发器（trigger_setup_tool 到期）注入
    Trigger,
    /// 任务系统派发（task_submit / create_root_task / resume 等）
    Task,
    /// HTTP /api/v1/chat 直连
    Http,
    /// 内核/系统内部
    System,
}

/// pending 输入条目（pipeline_pending_inputs 表行）。
///
/// 消息在"入队→激活"之间停留在表中，等待窗口内可修改/删除/清空
/// （ADR-2026-08-26）；激活时消费任务从表取参数执行——内容不再被闭包捕获。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PendingInputRecord {
    /// 队列条目 ID（12hex，uuid v4 simple 前 12 位，与管道 id 同式）
    pub id: String,
    /// 所属管道 ID（FIFO 键）
    pub pipeline_id: String,
    /// 租户（隔离键）
    pub tenant_id: String,
    /// 发送者
    pub user_id: String,
    /// 消息正文（等待窗口内可经 PUT 修改）
    pub content: String,
    /// 派发坐标（注入 = 真实会话 thread；创建 = 管道自身）
    pub thread: String,
    /// 来源标注
    pub source: PendingInputSource,
    /// 执行 agent（任务派发显式指定；主会话路径按线程绑定解析）
    pub agent_id: String,
    /// 消费时派发的路由键（resolve_pipeline_id_for_thread 结果——入队时解析，
    /// 消费时直接用；与链 key 同值）
    pub route_id: String,
    /// 思考强度（off/low/medium/high；引擎透传 llm_core）
    pub thinking_strength: String,
    /// 前端幂等键（ADR-2026-08-21：随 user 消息 metadata 落库回显，认领用）
    pub client_message_id: String,
    /// 任务级 execution_context（workspace_mode/isolation_level 等）
    pub execution_context: Option<serde_json::Value>,
    /// 出生注入 overlay（lineage.* / task.* 等扁平键）
    pub state_overlay: Option<serde_json::Value>,
    /// 管道配置 ID（config/pipelines/{id}.yaml）：显式指定 = 按需加载编译该配置
    /// 执行；None = autonomous（缺省路径与既有行为一致）
    pub pipeline_config_id: Option<String>,
    /// 创建时间（RFC3339；FIFO 序 = created_at, id 升序）
    pub created_at: String,
}

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
    /// 已取消（用户主动停止；非完成非失败，任务语义上未到达终态）
    Cancelled,
}

impl RunStatus {
    /// Failed 终态署名词表（`router.stop_reason` 取值）：命中即映射 Failed。
    /// 单点权威——from_control_state 查此表，词表测试遍历此表防漏词；
    /// 新增署名只改此表，落库、域事件派生与词表测试同步生效。
    pub const FAILED_STOP_REASONS: &[&str] = &[
        "task_failed",
        "task_evaluate_failed",
        "max_iterations",
        "timeout",
        "budget_exhausted",
        "elapsed_cap",
        "iteration_cap",
        "stalled",
        "duplicate_loop",
        "tool_fail_loop",
    ];

    /// run 终态映射（控制状态键契约 ADR 2026-08-30）：挂起优先；其余按
    /// `router.stop_reason` 署名查表——谁写终止谁署名，引擎只查表不猜；
    /// 未署名（含正常收束）→ Completed。引擎落库（update_run_status）与
    /// 域事件派生（run.*）共用此单点，两处不得各持一套词汇。
    pub fn from_control_state(final_state: &serde_json::Value) -> Self {
        let truthy = |key: &str| {
            final_state
                .get(key)
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
        };
        if truthy("suspended") {
            return Self::Suspended;
        }
        match final_state
            .get("router.stop_reason")
            .and_then(|v| v.as_str())
        {
            Some("user_requested") | Some("task_cancelled") | Some("task_deleted") => {
                Self::Cancelled
            }
            Some(reason) if Self::FAILED_STOP_REASONS.contains(&reason) => Self::Failed,
            _ => Self::Completed,
        }
    }
}

/// 运行记录——由 state 投影合成的运行元数据（runs 表已退役，ADR 2026-09-18）。
///
/// 结构保留以维持 capability `pipeline-executor.get_run_status` 等对外形状；
/// 字段来源：pipeline_state 标量键（run_id/run_status/run_started_at/
/// run_ended_at/run_config_hash/suspend_request_id）。current_branch/current_seq
/// 为导航指针退役后的恒定兼容值（"main"/0），无独立真值。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunRecord {
    /// 运行实例唯一 ID（UUID；state.run_id 同源）
    pub run_id: String,
    /// YAML 配置哈希（配置即产品，ADR ⑪）
    pub config_hash: String,
    /// 运行状态（state.run_status 权威）
    pub status: RunStatus,
    /// 多租户隔离
    pub tenant_id: String,
    /// 开始时间（ISO8601；state.run_started_at）
    pub created_at: String,
    /// 结束时间（None = 未结束；state.run_ended_at）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ended_at: Option<String>,
    /// 兼容字段（恒 "main"，导航指针已退役）
    pub current_branch: String,
    /// 兼容字段（恒 0，导航指针已退役）
    pub current_seq: u32,
    /// 兼容元数据（挂起凭据 suspend_request_id 映射回 pending_interaction_request_id）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub metadata: Option<serde_json::Value>,
    /// 所属管道 ID（state 反查所得；None = 投影未携带）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pipeline_id: Option<String>,
}

/// 管道运行快照（统一管道管理查询：`GET /api/v1/pipelines/runs`）。
///
/// runs 表退役后由 pipeline_state 运行簿记键（run_status/run_started_at/
/// run_ended_at/run_id）× pipeline_sessions 合成：
/// - 每个执行过的管道恰一条快照（当前运行的投影）；
/// - pipeline → 会话映射经 pipeline_sessions。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PipelineRunInfo {
    /// 运行实例唯一 ID（UUID；state.run_id）
    pub run_id: String,
    /// 所属管道 ID
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pipeline_id: Option<String>,
    /// 归属会话（thread）ID，可空（pipeline_sessions 未建映射的历史数据）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thread_id: Option<String>,
    /// 运行状态（state.run_status）
    pub status: RunStatus,
    /// 开始时间（ISO8601；state.run_started_at）
    pub started_at: String,
    /// 结束时间（None = 未结束）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ended_at: Option<String>,
}

/// 消息槽位记录（message_slots 投影）。
///
/// **关键设计**：
/// - `seq` 是稳定逻辑槽位（≠ 数组下标），删除留 gap；定位 = (pipeline_id, seq)
/// - `blob_id` 指向 blobs 表，内容懒加载
/// - `run_id` 为产出该消息的运行关联标签（state.run_id 同源，无 runs 表）
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MessageRecord {
    /// 消息唯一 ID（UUID）
    pub message_id: String,
    /// 产出该消息的运行关联标签（state.run_id 同源）
    pub run_id: String,
    /// 消息槽位序号（稳定逻辑槽位，删除留 gap）
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
    /// 产出该消息的管道执行身份（assistant blob `agent_id` 戳记读时提取）：
    /// 消息生成时的管道 state `agent.id`，前端气泡卡名/卡头像的数据源。
    /// 仅 llm_core 戳记的 assistant 消息非空，其余角色/旧消息为 None。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_id: Option<String>,
}

/// 消息段记录（message_segments 投影）——替换事件的冻结内容。
///
/// 段 = 消息引用的有序列表（成员本体是内容寻址 blob，一条一存），
/// 存储粒度 = 消息、分支/可见性粒度 = 段（消息段模型方案 §2/§4）：
/// - `base_seq`/`base_len` 定位冻结区间 `[base_seq, base_seq + base_len)`；
/// - `members_blob_id` 指向「成员 blob_id 有序数组」的 blob（引用列表，非全文）；
/// - `visible_to` 是非启用段的可消费者标签（空串 = 全可见）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SegmentRecord {
    /// 段唯一 ID（`seg_<uuid>`，写入方生成；段行按 id 幂等）
    pub id: String,
    /// 归属租户（一用户一租户）
    pub tenant_id: String,
    /// 所属管道 ID
    pub pipeline_id: String,
    /// 区间起点（插入后移时同步 +1）
    pub base_seq: i64,
    /// 区间长度（= 成员数；冻结时确定）
    pub base_len: i64,
    /// 成员引用列表 blob（成员 blob_id 的 JSON 数组）
    pub members_blob_id: String,
    /// 非启用段的可消费者标签集；空串 = 全可见（启用内容无标签）
    pub visible_to: String,
    /// 首条 content 截断预览（‹i/n› 代际预览免解 blob）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub preview: Option<String>,
    /// 创建时间（ISO8601）
    pub created_at: String,
    /// 成员消息全文（get 路径解析引用列表后填充；list 路径 None 不解 blob）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub members: Option<Vec<serde_json::Value>>,
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
    /// 最近活跃的 pipeline_id（仅引用；普通子管道注册时不覆盖它——归属锚点
    /// 创建除外：chat.send_message 创建分支带 thread_id 时同步切到新管道，
    /// 使「任务落用户会话线程，对话在聊天区继续」成立）
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
/// register 真实建用户，login/me/refresh/WS 查 DB。RBAC/凭据保险库留给 0.5.0。
///
/// **租户粒度**：一用户一租户——注册时 `tenant_id = user_id`，保证不同用户数据隔离。
/// admin 种子用户 tenant_id = "default"。
///
/// `password` 存 argon2id 哈希（D1：明文行由内核启动迁移自动哈希化回写）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UserRecord {
    /// 用户 ID（uuid；admin 种子为固定值）
    pub user_id: String,
    /// 用户名（跨租户全局唯一，登录键）
    pub username: String,
    /// 密码（argon2id 哈希，PHC 字符串，以 `$argon2` 起始）
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
    /// 首登强制改密标记（D1）：播种账号置 true，改密成功后清除。
    #[serde(default)]
    pub must_change_password: bool,
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

/// traces 表记录——状态变更日志（Append-Only Patch，pipeline 级 op 流）。
///
/// 对应 SQLite `traces` 表（ADR 2026-09-18：runs/branches 退役与 state 唯一真值）。
///
/// **关键设计**：
/// - **Append-Only**：只追加，永不修改、永不删除（保留期清扫除外）
/// - **Patch 记录**：每条 trace 记录一个状态窗口的变更
/// - **定位 = (pipeline_id, seq)**：seq 由存储层写入时分配（MAX(seq)+1），
///   即日志位置——「当前在哪」= 日志末端，无需独立导航指针
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TraceEntry {
    /// 日志条目唯一 ID
    pub trace_id: String,
    /// 所属管道 ID（op 流主键）
    pub pipeline_id: String,
    /// 日志序号（存储层写入时分配 = MAX(seq)+1；构造方可填 0，入库前被覆盖）
    pub seq: u32,
    /// 产生此 Patch 的插件 ID
    pub plugin_id: String,
    /// Patch 类型
    pub patch_type: PatchType,
    /// Patch 内容（JSON，对应状态窗口的变更）
    pub patch_data: serde_json::Value,
    /// 创建时间（ISO8601）
    pub created_at: String,
}

// ── ADR ①：引擎结果类型 ──────────────────────────────────────

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

/// 循环配置（step 级：组合节点自带循环，如批量处理；循环体级循环用 `while`）。
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
/// - 裸字符串：`- <step或插件名>`（无 when 门，缺省 True）；
/// - 对象：`- name: <step或插件名>` + `when: "state.xxx != ''"`。
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
/// 每个循环体拥有独立的 steps 与循环条件（G10 统一 DSL 单轨）：
/// - `while_cond` 缺省 → 单次执行（前处理/后处理体，如 init/exit）；
/// - `while_cond` 存在 → 循环执行，每轮开头求值（同一 `eval_condition` 求值器），
///   假则退出循环；`ended` / `suspended` 亦可终止。迭代上限不在管道 DSL 表达——
///   生产阀门是 post 链终止检查插件按 Agent 配置 `max_iterations` 兜底；
/// - 循环体结束后的转移：`next` 命中（`RouteNext::Phase`）→ 跳转到指定循环体；
///   未命中/未声明 → 默认顺序进入下一个循环体；最后一个循环体结束 = run 结束。
/// - `run_on_error`：管道提前终止（`ended` / 出错）时仍执行本循环体（收尾语义，
///   如 exit 体的 workspace 合并与环境释放）。挂起（`suspended`）不触发。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LoopBody {
    /// 循环体标识（执行期间写入 `state["current_phase"]`，插件据此分发）
    pub id: String,
    /// 循环体内的步骤（三级命中：当前管道 step id / 公共 step 库 / 插件名）
    #[serde(default)]
    pub steps: Vec<PipelineStep>,
    /// 循环继续条件表达式（G10 DSL `while: "expr"`）；None = 单次执行。
    /// YAML 书写键为 `while`（Rust 关键字规避，serde rename）。
    #[serde(default, skip_serializing_if = "Option::is_none", rename = "while")]
    pub while_cond: Option<String>,
    /// 循环体结束后的转移路由（YAML `next` 归一；默认顺序进入下一个循环体）
    #[serde(default)]
    pub exit_routes: Vec<Route>,
    /// 提前终止后仍执行本循环体（收尾语义）
    #[serde(default)]
    pub run_on_error: bool,
}

/// 管道配置（配置驱动的执行流程定义）。
///
/// 引擎作为配置解释执行器：读 PipelineConfig，按 `loop_bodies` 顺序执行每个
/// 循环体（各自独立的 steps 与循环条件），据 next 转移（归一为内部
/// exit_routes/routes）决定循环、分支与循环体间转移。一套引擎 + 不同 YAML = 不同行为。
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
    /// 管道级初始 state 缺省（命令声明驱动缺省：开轮前种入的标量键值）。
    ///
    /// 由管道 YAML 顶层 `initial_state:` 声明（如 core 器官执行缺省
    /// core_plugin/core_type）——内核不预知具体键面，声明什么种什么；
    /// 已有值不覆盖（本轮值优先），缺省只补缺。
    #[serde(default)]
    pub initial_state: HashMap<String, serde_json::Value>,
    /// 主轮循环轮数硬上限（per-run 全部循环体 while 轮累计，ADR
    /// 2026-09-11-engine-loop-cap）。由管道 YAML 顶层 `max_rounds:` 声明；
    /// `None` = 未声明，引擎用缺省 200（旧 YAML 行为不变）。`0` 非法
    /// （run 入口报错拒绝，0 = 无限轮已被禁用）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_rounds: Option<usize>,
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
        while_cond: Option<String>,
        steps: Vec<PipelineStep>,
    ) -> Self {
        Self {
            name: name.into(),
            loop_bodies: vec![LoopBody {
                id: "main".to_string(),
                steps,
                while_cond,
                exit_routes: vec![],
                run_on_error: false,
            }],
            checkpoint: Default::default(),
            initial_state: HashMap::new(),
            max_rounds: None,
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

// ── 单元测试（serde 往返 / 构造器 / 终态映射 / Display 契约）──────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::traits::{MessageQueryOpts, SessionListFilter};

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

    /// 畸形输入拒收：目标类型的反序列化必须报错（fail-closed）。
    fn assert_serde_rejects<T: serde::de::DeserializeOwned + std::fmt::Debug>(raw: &str) {
        assert!(
            serde_json::from_str::<T>(raw).is_err(),
            "畸形输入应被拒收: {raw}"
        );
    }

    /// 存储后端替身：仅供 ContentLoader/PluginContext 构造（这些用例零存储调用）。
    struct NilStore;

    #[async_trait::async_trait]
    impl StorageBackend for NilStore {
        async fn get_run(&self, _run_id: &str) -> Result<RunRecord, StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn get_messages_by_pipeline(
            &self,
            _pipeline_id: &str,
            _opts: MessageQueryOpts,
        ) -> Result<Vec<MessageRecord>, StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn get_blob(&self, _blob_id: &str) -> Result<Vec<u8>, StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn append_trace(&self, _entry: TraceEntry) -> Result<(), StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn store_blob(&self, _data: &[u8], _mime_type: &str) -> Result<String, StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn create_session(&self, _session: &SessionRecord) -> Result<(), StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn get_session(
            &self,
            _thread_id: &str,
        ) -> Result<Option<SessionRecord>, StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn list_sessions(
            &self,
            _filter: SessionListFilter,
        ) -> Result<Vec<SessionRecord>, StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn update_session(&self, _session: &SessionRecord) -> Result<(), StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn delete_session(&self, _thread_id: &str) -> Result<Vec<String>, StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn link_pipeline_session(
            &self,
            _pipeline_id: &str,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<(), StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn list_pipeline_ids_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<String>, StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn get_step_traces_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<TraceEntry>, StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn create_user(&self, _user: &UserRecord) -> Result<(), StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn get_user_by_id(&self, _user_id: &str) -> Result<Option<UserRecord>, StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn get_user_by_username(
            &self,
            _username: &str,
        ) -> Result<Option<UserRecord>, StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn list_users(&self) -> Result<Vec<UserRecord>, StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn update_last_login(&self, _user_id: &str) -> Result<(), StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn update_user_password(
            &self,
            _user_id: &str,
            _password_hash: &str,
            _must_change_password: bool,
        ) -> Result<bool, StorageError> {
            unreachable!("本用例不触发存储调用")
        }
        async fn delete_user(&self, _user_id: &str) -> Result<bool, StorageError> {
            unreachable!("本用例不触发存储调用")
        }
    }

    fn nil_loader() -> ContentLoader {
        ContentLoader::new(
            std::sync::Arc::new(NilStore),
            "run-1".to_string(),
            "main".to_string(),
        )
    }

    // ── 路由信号与插件结果 ────────────────────────────────────────

    #[test]
    fn route_type_serde_roundtrip_all_variants() {
        for (value, wire) in [
            (RouteType::NextLlm, "next_llm"),
            (RouteType::NextTool, "next_tool"),
            (RouteType::End, "end"),
            (RouteType::Wait, "wait"),
        ] {
            assert_eq!(
                serde_json::to_string(&value).unwrap(),
                format!("\"{wire}\"")
            );
            let back: RouteType = serde_json::from_str(&format!("\"{wire}\"")).unwrap();
            assert_eq!(back, value);
        }
        assert_serde_rejects::<RouteType>("\"delegate\"");
        assert_serde_rejects::<RouteType>("\"bogus\"");
    }

    #[test]
    fn plugin_result_default_builders_and_serde() {
        // Default：空更新、不跳过、无错误
        let empty = PluginResult::default();
        assert!(empty.state_updates.is_empty());
        assert!(!empty.skip_remaining);
        assert!(empty.error.is_none());
        // 反序列化缺省字段 → 同一 Default 语义
        let parsed: PluginResult = serde_json::from_str("{}").unwrap();
        assert_eq!(parsed.state_updates.len(), 0);
        assert!(!parsed.skip_remaining);

        // builder：with_state_updates / with_error
        let mut updates = HashMap::new();
        updates.insert("k".to_string(), serde_json::json!(42));
        let with_updates = PluginResult::default().with_state_updates(updates.clone());
        assert_eq!(with_updates.state_updates, updates);
        let err = PluginError {
            message: "boom".into(),
            code: Some("E1".into()),
            source: None,
        };
        let with_err = PluginResult::default().with_error(err.clone());
        assert_eq!(with_err.error.as_ref().unwrap().message, "boom");

        // roundtrip 两种形态
        assert_serde_roundtrip(&with_updates);
        assert_serde_roundtrip(&with_err);
    }

    #[test]
    fn plugin_error_display_and_std_error() {
        let coded = PluginError {
            message: "disk full".into(),
            code: Some("E_IO".into()),
            source: Some("sqlite".into()),
        };
        assert_eq!(coded.to_string(), "[E_IO] disk full");
        let bare = PluginError {
            message: "disk full".into(),
            code: None,
            source: None,
        };
        assert_eq!(bare.to_string(), "disk full");
        assert_serde_roundtrip(&coded);
        // std::error::Error trait 实现（编译期即验证 trait 约束）
        let boxed: Box<dyn std::error::Error> = Box::new(coded);
        assert!(boxed.to_string().contains("disk full"));
    }

    #[test]
    fn plugin_context_and_content_loader_debug() {
        let state = serde_json::json!({"messages": []});
        let loader = nil_loader();
        let ctx = PluginContext::new(
            &state,
            serde_json::json!({"model": "gpt"}),
            TenantContext::new("tenant-a", "sess-1"),
            uuid::Uuid::new_v4(),
            loader.clone(),
        );
        // new 构造的缺省坐标
        assert_eq!(ctx.session_id, "");
        assert_eq!(ctx.task_id, "");
        assert_eq!(ctx.tenant.tenant_id, "tenant-a");
        // Debug 面包含关键字段（消费方日志可观测性契约）
        let dbg = format!("{ctx:?}");
        for needle in ["PluginContext", "tenant-a", "sess-1", "run-1", "main"] {
            assert!(dbg.contains(needle), "Debug 缺少 {needle}: {dbg}");
        }
        // ContentLoader Clone/Debug：clone 与本体可观测字段一致
        let loader_dbg = format!("{loader:?}");
        assert!(loader_dbg.contains("run-1") && loader_dbg.contains("main"));
        let cloned = loader.clone();
        assert_eq!(format!("{cloned:?}"), loader_dbg);
    }

    #[test]
    fn tenant_context_new_defaults_and_serde() {
        let ctx = TenantContext::new("t1", "s1");
        assert_eq!(ctx.tenant_id, "t1");
        assert_eq!(ctx.session_id, "s1");
        assert_eq!(ctx.user_id, None);
        assert_eq!(ctx.role, None);
        assert!(ctx.permissions.is_empty());
        assert!(ctx.enabled_plugins.is_empty());
        assert_eq!(ctx.credential_handle, None);

        // 完整形态 roundtrip
        let full = TenantContext {
            tenant_id: "t1".into(),
            user_id: Some("u1".into()),
            session_id: "s1".into(),
            role: Some("admin".into()),
            permissions: vec!["pipeline:run".into(), "tool:invoke".into()],
            enabled_plugins: vec!["p1".into()],
            credential_handle: Some("vault:key1".into()),
        };
        assert_serde_roundtrip(&full);
        // 最小形态：可选字段缺省、permissions/enabled_plugins 缺省空
        let minimal: TenantContext =
            serde_json::from_str(r#"{"tenant_id":"t","session_id":"s"}"#).unwrap();
        assert_eq!(minimal.user_id, None);
        assert!(minimal.permissions.is_empty());
        assert!(minimal.enabled_plugins.is_empty());
        // None 可选字段不序列化输出
        let s = serde_json::to_string(&ctx).unwrap();
        assert!(!s.contains("user_id") && !s.contains("role"), "{s}");
    }

    // ── 工具元信息 ────────────────────────────────────────────────

    #[test]
    fn tool_category_serde_roundtrip_all_variants() {
        for (value, wire) in [
            (ToolCategory::File, "file"),
            (ToolCategory::FileSystem, "file_system"),
            (ToolCategory::Search, "search"),
            (ToolCategory::Web, "web"),
            (ToolCategory::Memory, "memory"),
            (ToolCategory::Task, "task"),
            (ToolCategory::System, "system"),
            (ToolCategory::Execution, "execution"),
            (ToolCategory::Analysis, "analysis"),
            (ToolCategory::Evaluation, "evaluation"),
            (ToolCategory::Agent, "agent"),
            (ToolCategory::Monitoring, "monitoring"),
        ] {
            let text = serde_json::to_string(&value).unwrap();
            assert_eq!(text, format!("\"{wire}\""), "{value:?} wire 形态");
            let back: ToolCategory = serde_json::from_str(&text).unwrap();
            assert_eq!(back, value);
        }
        assert_serde_rejects::<ToolCategory>("\"gpu\"");
    }

    #[test]
    fn tool_source_serde_roundtrip_all_variants() {
        for (value, wire) in [
            (ToolSource::Builtin, "builtin"),
            (ToolSource::Mcp, "mcp"),
            (ToolSource::Custom, "custom"),
            (ToolSource::Database, "database"),
            (ToolSource::Dynamic, "dynamic"),
        ] {
            let text = serde_json::to_string(&value).unwrap();
            assert_eq!(text, format!("\"{wire}\""));
            let back: ToolSource = serde_json::from_str(&text).unwrap();
            assert_eq!(back, value);
        }
        assert_serde_rejects::<ToolSource>("\"hardcoded\"");
    }

    #[test]
    fn tool_execution_result_constructors_and_serde() {
        let ok = ToolExecutionResult::success(serde_json::json!({"rows": [1, 2]}));
        assert!(ok.success);
        assert_eq!(ok.data["rows"][1], 2);
        assert_eq!(ok.error, None);
        assert_eq!(ok.duration_ms, None);
        assert_eq!(ok.metadata, None);

        let bad = ToolExecutionResult::failure("timeout after 30s");
        assert!(!bad.success);
        assert_eq!(bad.data, serde_json::Value::Null);
        assert_eq!(bad.error.as_deref(), Some("timeout after 30s"));

        // roundtrip：成功与失败两种形态
        assert_serde_roundtrip(&ok);
        assert_serde_roundtrip(&bad);
        // None 可选字段不序列化；metadata 显式携带时保留（副作用信号载体）
        let s = serde_json::to_string(&ok).unwrap();
        assert!(!s.contains("error") && !s.contains("metadata"), "{s}");
        let with_meta = ToolExecutionResult {
            metadata: Some(serde_json::json!({"result": "completed"})),
            duration_ms: Some(120),
            ..ok.clone()
        };
        assert_serde_roundtrip(&with_meta);
    }

    // ── pending 输入与运行状态 ────────────────────────────────────

    #[test]
    fn pending_input_source_serde_roundtrip_all_variants() {
        for (value, wire) in [
            (PendingInputSource::User, "user"),
            (PendingInputSource::Trigger, "trigger"),
            (PendingInputSource::Task, "task"),
            (PendingInputSource::Http, "http"),
            (PendingInputSource::System, "system"),
        ] {
            let text = serde_json::to_string(&value).unwrap();
            assert_eq!(text, format!("\"{wire}\""));
            let back: PendingInputSource = serde_json::from_str(&text).unwrap();
            assert_eq!(back, value);
        }
        assert_serde_rejects::<PendingInputSource>("\"cron\"");
    }

    fn pending_record(source: PendingInputSource) -> PendingInputRecord {
        PendingInputRecord {
            id: "abc123def456".into(),
            pipeline_id: "p1".into(),
            tenant_id: "t1".into(),
            user_id: "u1".into(),
            content: "hello".into(),
            thread: "th1".into(),
            source,
            agent_id: "main".into(),
            route_id: "r1".into(),
            thinking_strength: "low".into(),
            client_message_id: "cmid-1".into(),
            execution_context: Some(serde_json::json!({"workspace_mode": "isolated"})),
            state_overlay: None,
            pipeline_config_id: None,
            created_at: "2026-09-13T00:00:00Z".into(),
        }
    }

    #[test]
    fn pending_input_record_serde_roundtrip() {
        assert_serde_roundtrip(&pending_record(PendingInputSource::User));
        assert_serde_roundtrip(&pending_record(PendingInputSource::Task));
    }

    #[test]
    fn run_status_serde_roundtrip_all_variants() {
        for (value, wire) in [
            (RunStatus::Running, "running"),
            (RunStatus::Suspended, "suspended"),
            (RunStatus::Completed, "completed"),
            (RunStatus::Failed, "failed"),
            (RunStatus::Cancelled, "cancelled"),
        ] {
            let text = serde_json::to_string(&value).unwrap();
            assert_eq!(text, format!("\"{wire}\""));
            let back: RunStatus = serde_json::from_str(&text).unwrap();
            assert_eq!(back, value);
        }
        assert_serde_rejects::<RunStatus>("\"pending\"");
    }

    #[test]
    fn run_status_from_control_state_contract() {
        // 挂起优先：即使同时带失败署名，suspended=true 也判挂起
        let suspended = serde_json::json!({"suspended": true, "router.stop_reason": "timeout"});
        assert_eq!(
            RunStatus::from_control_state(&suspended),
            RunStatus::Suspended
        );

        // 用户主动终止词表 → Cancelled
        for reason in ["user_requested", "task_cancelled", "task_deleted"] {
            let state = serde_json::json!({"router.stop_reason": reason});
            assert_eq!(
                RunStatus::from_control_state(&state),
                RunStatus::Cancelled,
                "{reason} 应映射 Cancelled"
            );
        }
        // 失败署名词表逐词映射 Failed（词表即契约，遍历防漏词）
        for reason in RunStatus::FAILED_STOP_REASONS {
            let state = serde_json::json!({"router.stop_reason": reason});
            assert_eq!(
                RunStatus::from_control_state(&state),
                RunStatus::Failed,
                "{reason} 应映射 Failed"
            );
        }
        // 未署名 / 未知署名 / 空 state → Completed（正常收束）
        for state in [
            serde_json::json!({}),
            serde_json::json!({"router.stop_reason": "task_completed"}),
            serde_json::json!({"suspended": false, "router.stop_reason": "task_completed"}),
            serde_json::json!({"suspended": "yes"}),
        ] {
            assert_eq!(
                RunStatus::from_control_state(&state),
                RunStatus::Completed,
                "未署名应映射 Completed: {state}"
            );
        }
    }

    // ── 四表模型记录 ──────────────────────────────────────────────

    #[test]
    fn run_record_serde_roundtrip() {
        let running = RunRecord {
            run_id: "r-1".into(),
            config_hash: "h1".into(),
            status: RunStatus::Running,
            tenant_id: "t1".into(),
            created_at: "2026-09-13T00:00:00Z".into(),
            ended_at: None,
            current_branch: "main".into(),
            current_seq: 3,
            metadata: Some(serde_json::json!({"agent_id": "main"})),
            pipeline_id: Some("p-1".into()),
        };
        assert_serde_roundtrip(&running);
        // ended_at None 不序列化；结束形态携带 ended_at
        let s = serde_json::to_string(&running).unwrap();
        assert!(!s.contains("ended_at"), "{s}");
        let finished = RunRecord {
            status: RunStatus::Completed,
            ended_at: Some("2026-09-13T01:00:00Z".into()),
            ..running
        };
        assert_serde_roundtrip(&finished);
    }

    #[test]
    fn pipeline_run_info_serde_roundtrip() {
        let full = PipelineRunInfo {
            run_id: "r-1".into(),
            pipeline_id: Some("p-1".into()),
            thread_id: Some("th-1".into()),
            status: RunStatus::Failed,
            started_at: "2026-09-13T00:00:00Z".into(),
            ended_at: Some("2026-09-13T01:00:00Z".into()),
        };
        assert_serde_roundtrip(&full);
        let bare = PipelineRunInfo {
            pipeline_id: None,
            thread_id: None,
            ended_at: None,
            ..full
        };
        let s = serde_json::to_string(&bare).unwrap();
        assert!(
            !s.contains("pipeline_id") && !s.contains("thread_id"),
            "{s}"
        );
        assert_serde_roundtrip(&bare);
    }

    #[test]
    fn message_record_serde_roundtrip() {
        let minimal = MessageRecord {
            message_id: "m-1".into(),
            run_id: "r-1".into(),
            seq_in_branch: 0,
            role: "user".into(),
            blob_id: None,
            content_preview: None,
            created_at: "2026-09-13T00:00:00Z".into(),
            pipeline_id: None,
            tool_calls_json: None,
            tool_call_id: None,
            reasoning_content: None,
            status: None,
            error: None,
            tool_result_json: None,
            metadata: None,
            agent_id: None,
        };
        assert_serde_roundtrip(&minimal);
        let full = MessageRecord {
            role: "assistant".into(),
            blob_id: Some("b-1".into()),
            content_preview: Some("hello…".into()),
            pipeline_id: Some("p-1".into()),
            tool_calls_json: Some("[]".into()),
            reasoning_content: Some("thinking".into()),
            metadata: Some(serde_json::json!({"client_message_id": "cmid-1"})),
            agent_id: Some("general_agent".into()),
            ..minimal.clone()
        };
        assert_serde_roundtrip(&full);
        // tool 结果消息形态（role=tool 携带配对字段）
        let tool_msg = MessageRecord {
            role: "tool".into(),
            tool_call_id: Some("call-1".into()),
            status: Some("failed".into()),
            error: Some("boom".into()),
            tool_result_json: Some("{\"success\":false}".into()),
            ..minimal
        };
        assert_serde_roundtrip(&tool_msg);
    }

    #[test]
    fn session_record_serde_roundtrip() {
        let full = SessionRecord {
            thread_id: "th-1".into(),
            title: Some("demo".into()),
            intent: Some("answer question".into()),
            current_state: "active".into(),
            agent_id: Some("main".into()),
            active_pipeline_id: Some("p-1".into()),
            pipeline_ids: vec!["p-1".into(), "p-2".into()],
            metadata: Some(serde_json::json!({"session_type": "main_pipeline"})),
            created_at: "2026-09-13T00:00:00Z".into(),
            updated_at: "2026-09-13T00:10:00Z".into(),
            last_active_at: Some("2026-09-13T00:10:00Z".into()),
        };
        assert_serde_roundtrip(&full);
        // pipeline_ids 缺省空（旧数据兼容）
        let parsed: SessionRecord = serde_json::from_str(
            r#"{"thread_id":"th","current_state":"idle","created_at":"t","updated_at":"t"}"#,
        )
        .unwrap();
        assert!(parsed.pipeline_ids.is_empty());
        assert_eq!(parsed.current_state, "idle");
    }

    #[test]
    fn user_record_serde_roundtrip_and_defaults() {
        let user = UserRecord {
            user_id: "u-1".into(),
            username: "alice".into(),
            password: "$argon2id$v=19$m=19456,t=2,p=1$c29tZXNhbHQ".into(),
            email: Some("a@b.c".into()),
            role: "admin".into(),
            tenant_id: "default".into(),
            created_at: "2026-09-13T00:00:00Z".into(),
            last_login_at: None,
            must_change_password: true,
        };
        assert_serde_roundtrip(&user);
        // must_change_password 缺省 false（旧数据零迁移）
        let legacy: UserRecord = serde_json::from_str(
            r#"{"user_id":"u","username":"bob","password":"$argon2","role":"user","tenant_id":"u","created_at":"t"}"#,
        )
        .unwrap();
        assert!(!legacy.must_change_password);
        assert_eq!(legacy.last_login_at, None);
    }

    #[test]
    fn patch_type_serde_roundtrip_all_variants() {
        for (value, wire) in [
            (PatchType::StateUpdate, "state_update"),
            (PatchType::RouteSignal, "route_signal"),
            (PatchType::Error, "error"),
            (PatchType::Lifecycle, "lifecycle"),
            (PatchType::Rollback, "rollback"),
        ] {
            let text = serde_json::to_string(&value).unwrap();
            assert_eq!(text, format!("\"{wire}\""));
            let back: PatchType = serde_json::from_str(&text).unwrap();
            assert_eq!(back, value);
        }
        assert_serde_rejects::<PatchType>("\"merge\"");
    }

    #[test]
    fn trace_entry_serde_roundtrip() {
        let entry = TraceEntry {
            trace_id: "tr-1".into(),
            pipeline_id: "p-1".into(),
            seq: 7,
            plugin_id: "llm_core".into(),
            patch_type: PatchType::StateUpdate,
            patch_data: serde_json::json!({"state_updates": {"k": 1}}),
            created_at: "2026-09-13T00:00:00Z".into(),
        };
        assert_serde_roundtrip(&entry);
    }

    // ── 引擎/存储错误 ─────────────────────────────────────────────

    #[test]
    fn engine_error_display_and_conversions() {
        let cases: Vec<(EngineError, &str)> = vec![
            (
                EngineError::RunNotFound {
                    run_id: "r-1".into(),
                },
                "run not found: r-1",
            ),
            (
                EngineError::InvalidState {
                    run_id: "r-1".into(),
                    reason: "already ended".into(),
                },
                "run 'r-1' is in invalid state: already ended",
            ),
            (
                EngineError::Storage(StorageError::NotFound("x".into())),
                "storage error: not found: x",
            ),
            (
                EngineError::Plugin(PluginError {
                    message: "boom".into(),
                    code: None,
                    source: None,
                }),
                "plugin error: boom",
            ),
            (
                EngineError::Config {
                    message: "bad yaml".into(),
                },
                "config error: bad yaml",
            ),
            (
                EngineError::Other {
                    message: "misc".into(),
                },
                "engine error: misc",
            ),
        ];
        for (err, expected) in cases {
            assert_eq!(&err.to_string(), expected, "{err:?}");
        }
        // From 转换：? 传播链契约
        let from_storage: EngineError = StorageError::Io("disk".into()).into();
        assert!(matches!(
            from_storage,
            EngineError::Storage(StorageError::Io(_))
        ));
        let from_plugin: EngineError = PluginError {
            message: "p".into(),
            code: None,
            source: None,
        }
        .into();
        assert!(matches!(from_plugin, EngineError::Plugin(_)));
    }

    #[test]
    fn storage_error_display_and_rusqlite_conversion() {
        let cases: Vec<(StorageError, &str)> = vec![
            (StorageError::NotFound("r-9".into()), "not found: r-9"),
            (
                StorageError::Serialization("bad json".into()),
                "serialization error: bad json",
            ),
            (
                StorageError::Database("locked".into()),
                "database error: locked",
            ),
            (StorageError::Io("eof".into()), "io error: eof"),
        ];
        for (err, expected) in cases {
            assert_eq!(&err.to_string(), expected, "{err:?}");
        }
        // rusqlite::Error 自动转 Database（消息携带原始错误文本）
        let converted = StorageError::from(rusqlite::Error::InvalidColumnName("col_x".into()));
        match converted {
            StorageError::Database(msg) => assert!(msg.contains("col_x"), "{msg}"),
            other => panic!("应转为 Database，实际 {other:?}"),
        }
    }

    // ── 配置驱动的管道配置类型 ────────────────────────────────────

    #[test]
    fn route_next_serde_roundtrip_all_variants() {
        // 标量变体：lowercase 线格式
        for (value, wire) in [
            (RouteNext::Loop, "loop"),
            (RouteNext::End, "end"),
            (RouteNext::Wait, "wait"),
        ] {
            let text = serde_json::to_string(&value).unwrap();
            assert_eq!(text, format!("\"{wire}\""));
            let back: RouteNext = serde_json::from_str(&text).unwrap();
            assert_eq!(back, value);
        }
        // 带载荷变体：step / phase
        assert_eq!(
            serde_json::to_string(&RouteNext::Step("s2".into())).unwrap(),
            r#"{"step":"s2"}"#
        );
        let step: RouteNext = serde_json::from_str(r#"{"step":"s2"}"#).unwrap();
        assert_eq!(step, RouteNext::Step("s2".into()));
        assert_eq!(
            serde_json::to_string(&RouteNext::Phase("exit".into())).unwrap(),
            r#"{"phase":"exit"}"#
        );
        let phase: RouteNext = serde_json::from_str(r#"{"phase":"exit"}"#).unwrap();
        assert_eq!(phase, RouteNext::Phase("exit".into()));
        assert_serde_rejects::<RouteNext>("\"jump\"");
    }

    #[test]
    fn route_and_action_serde_roundtrip() {
        let mut set = HashMap::new();
        set.insert("route".to_string(), serde_json::json!("done"));
        let route = Route {
            when: "state.x != ''".into(),
            then: RouteAction {
                next: RouteNext::Step("s3".into()),
                set,
            },
        };
        assert_serde_roundtrip(&route);
        // set 缺省空 map
        let parsed: Route =
            serde_json::from_str(r#"{"when":"True","then":{"next":"end"}}"#).unwrap();
        assert_eq!(parsed.then.next, RouteNext::End);
        assert!(parsed.then.set.is_empty());
        assert_eq!(parsed.when, "True");
    }

    #[test]
    fn loop_config_defaults_and_serde() {
        // Default：关闭 + 无限循环
        let d = LoopConfig::default();
        assert!(!d.enabled);
        assert_eq!(d.max_iterations, -1);
        // 缺省字段补默认（YAML 只写 enabled 也能解析）
        let parsed: LoopConfig = serde_json::from_str("{\"enabled\":true}").unwrap();
        assert!(parsed.enabled);
        assert_eq!(parsed.max_iterations, -1);
        // 显式安全阀 roundtrip
        let explicit: LoopConfig =
            serde_json::from_str("{\"enabled\":true,\"max_iterations\":5}").unwrap();
        assert_serde_roundtrip(&explicit);
    }

    #[test]
    fn checkpoint_config_defaults_and_serde() {
        let d = CheckpointConfig::default();
        assert!(d.enabled);
        assert_eq!(d.interval_steps, 1000);
        let parsed: CheckpointConfig = serde_json::from_str("{}").unwrap();
        assert!(parsed.enabled);
        assert_eq!(parsed.interval_steps, 1000);
        let explicit: CheckpointConfig =
            serde_json::from_str("{\"enabled\":false,\"interval_steps\":50}").unwrap();
        assert!(!explicit.enabled);
        assert_serde_roundtrip(&explicit);
    }

    #[test]
    fn step_item_conversions_accessors_and_serde() {
        // From 转换：&str / String → Bare
        let bare = StepItem::from("build");
        assert_eq!(bare, StepItem::Bare("build".into()));
        let owned = StepItem::from(String::from("build"));
        assert_eq!(owned, StepItem::Bare("build".into()));
        // Bare 访问器：无 when、无 inputs
        assert_eq!(bare.name(), "build");
        assert_eq!(bare.when(), None);
        assert!(bare.inputs().is_empty());

        // Gated 访问器
        let mut inputs = HashMap::new();
        inputs.insert("q".to_string(), serde_json::json!("rust"));
        let gated = StepItem::Gated {
            name: "search".into(),
            when: Some("state.q != ''".into()),
            inputs: inputs.clone(),
        };
        assert_eq!(gated.name(), "search");
        assert_eq!(gated.when(), Some("state.q != ''"));
        assert_eq!(gated.inputs(), inputs);

        // untagged 反序列化：裸串 → Bare；对象 → Gated（缺省 when=None/inputs=空）
        let b: StepItem = serde_json::from_str("\"build\"").unwrap();
        assert_eq!(b, StepItem::Bare("build".into()));
        let g: StepItem = serde_json::from_str(r#"{"name":"search"}"#).unwrap();
        assert_eq!(
            g,
            StepItem::Gated {
                name: "search".into(),
                when: None,
                inputs: HashMap::new()
            }
        );
        assert_serde_roundtrip(&gated);
    }

    #[test]
    fn pipeline_step_serde_defaults_and_roundtrip() {
        let mut context = HashMap::new();
        context.insert("lang".to_string(), serde_json::json!("rust"));
        let step = PipelineStep {
            id: "s1".into(),
            steps: vec![StepItem::from("build"), StepItem::from("test")],
            when: Some("state.ready".into()),
            context: context.clone(),
            routes: vec![Route {
                when: "state.x".into(),
                then: RouteAction {
                    next: RouteNext::End,
                    set: HashMap::new(),
                },
            }],
            loop_config: Some(LoopConfig::default()),
        };
        assert_serde_roundtrip(&step);
        // 最小形态：全部缺省字段兜底
        let minimal: PipelineStep = serde_json::from_str(r#"{"id":"s2"}"#).unwrap();
        assert!(minimal.steps.is_empty());
        assert_eq!(minimal.when, None);
        assert!(minimal.context.is_empty());
        assert!(minimal.routes.is_empty());
        assert!(minimal.loop_config.is_none());
    }

    #[test]
    fn loop_body_while_rename_serde_roundtrip() {
        let body = LoopBody {
            id: "main".into(),
            steps: vec![PipelineStep {
                id: "s1".into(),
                steps: vec![],
                when: None,
                context: HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
            while_cond: Some("state.more == true".into()),
            exit_routes: vec![Route {
                when: "state.done".into(),
                then: RouteAction {
                    next: RouteNext::Phase("exit".into()),
                    set: HashMap::new(),
                },
            }],
            run_on_error: true,
        };
        // YAML 键为 while（Rust 关键字规避），往返保真
        let text = serde_json::to_string(&body).unwrap();
        assert!(text.contains(r#""while":"state.more == true""#), "{text}");
        let back: LoopBody = serde_json::from_str(&text).unwrap();
        assert_eq!(
            serde_json::to_string(&back).unwrap(),
            text,
            "LoopBody roundtrip 线格式不一致"
        );
        // while 缺省 None（单次执行体）
        let plain: LoopBody =
            serde_json::from_str(r#"{"id":"init","steps":[],"run_on_error":false}"#).unwrap();
        assert_eq!(plain.while_cond, None);
        assert!(!plain.run_on_error);
    }

    #[test]
    fn pipeline_config_lookup_helpers() {
        let step_of = |id: &str| PipelineStep {
            id: id.into(),
            steps: vec![],
            when: None,
            context: HashMap::new(),
            routes: vec![],
            loop_config: None,
        };
        let config = PipelineConfig {
            name: "demo".into(),
            loop_bodies: vec![
                LoopBody {
                    id: "init".into(),
                    steps: vec![step_of("prepare")],
                    while_cond: None,
                    exit_routes: vec![],
                    run_on_error: false,
                },
                LoopBody {
                    id: "main".into(),
                    steps: vec![step_of("think"), step_of("act")],
                    while_cond: Some("state.more".into()),
                    exit_routes: vec![],
                    run_on_error: false,
                },
            ],
            checkpoint: CheckpointConfig::default(),
            initial_state: HashMap::new(),
            max_rounds: Some(50),
        };
        // find_step：跨循环体命中① + 未命中
        assert_eq!(config.find_step("think").unwrap().id, "think");
        assert_eq!(config.find_step("prepare").unwrap().id, "prepare");
        assert!(config.find_step("missing").is_none());
        // step_ids：跨全部循环体聚合
        assert_eq!(config.step_ids(), vec!["prepare", "think", "act"]);
        // body_index：命中 + 未命中
        assert_eq!(config.body_index("main"), Some(1));
        assert_eq!(config.body_index("exit"), None);
        // max_rounds 显式声明保留
        assert_eq!(config.max_rounds, Some(50));
        // single_body 便捷构造：单 main 体承载全部 steps
        let single =
            PipelineConfig::single_body("t", Some("state.more".into()), vec![step_of("s")]);
        assert_eq!(single.loop_bodies.len(), 1);
        assert_eq!(single.loop_bodies[0].id, "main");
        assert_eq!(
            single.loop_bodies[0].while_cond.as_deref(),
            Some("state.more")
        );
        assert_eq!(single.find_step("s").unwrap().id, "s");
        assert_eq!(single.max_rounds, None);
    }

    #[test]
    fn step_library_find_and_ids() {
        let step_of = |id: &str| PipelineStep {
            id: id.into(),
            steps: vec![],
            when: None,
            context: HashMap::new(),
            routes: vec![],
            loop_config: None,
        };
        let mut lib = StepLibrary::default();
        assert!(lib.find("any").is_none(), "空库查无");
        lib.steps.insert("review".into(), step_of("review"));
        lib.steps.insert("commit".into(), step_of("commit"));
        assert_eq!(lib.find("review").unwrap().id, "review");
        assert!(lib.find("nope").is_none());
        let mut ids = lib.ids();
        ids.sort_unstable();
        assert_eq!(ids, vec!["commit", "review"]);
    }
}

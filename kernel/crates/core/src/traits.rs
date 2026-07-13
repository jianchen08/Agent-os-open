//! 内核核心 Trait 定义
//!
//! 这是 0.2 架构的"宪法"层接口契约。所有内核组件和插件都围绕这些 trait 构建。
//!
//! 设计决策来源：
//! - 管道插件混合方案（Rust 原生 + MCP 边车）：[来源: docs/0.2_rust_plugin_solution.md §3.2]
//! - 路由信号精简为 4 种：[来源: docs/0.2_rust_plugin_solution.md §3.5]
//! - 按需加载全局原则：[来源: docs/0.2_rust_plugin_solution.md §3.7]
//! - 多租户上下文穿透：[来源: docs/0.2_rust_plugin_solution.md §3.4]

use std::any::Any;
use std::collections::HashMap;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::types::*;

// ── 1. 插件基础 Trait ───────────────────────────────────────────

/// 插件元信息——所有插件（管道/工具/系统）共有的标识与描述。
///
/// 对应 Manifest V2.0 的核心字段，内核在加载时从 manifest 中提取。
pub trait PluginMeta: Send + Sync {
    /// 插件唯一标识符（对应 manifest.id）
    fn id(&self) -> &str;

    /// 插件人类可读名称（对应 manifest.name）
    fn name(&self) -> &str;

    /// 插件版本（对应 manifest.version）
    fn version(&self) -> &str;

    /// 插件类型（对应 manifest.type）
    fn plugin_type(&self) -> PluginType;

    /// 插件错误处理策略（对应 manifest.error_policy）
    fn error_policy(&self) -> ErrorPolicy {
        ErrorPolicy::default()
    }

    /// 插件执行优先级，数值越小越先执行（对应 manifest.priority）
    fn priority(&self) -> u32 {
        100
    }
}

/// 插件类型枚举。
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PluginType {
    /// 管道插件（Input / Core / Output 三阶段）
    Pipeline,
    /// 工具插件（提供 MCP 工具）
    Tool,
    /// 系统插件（记忆/审批/评估等内核级服务）
    System,
}

/// 管道插件角色（仅 type=Pipeline 时有效）。
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PipelineRole {
    /// 输入阶段：参数校验、上下文注入、权限检查
    Input,
    /// 核心阶段：LLM 调用或工具执行
    Core,
    /// 输出阶段：结果格式化、后处理、路由信号生成
    Output,
}

// ── 2. 管道插件 Trait ───────────────────────────────────────────

/// 管道插件统一接口（Rust 原生，host_type = in_process）。
///
/// **混合方案**（[来源: docs/0.2_rust_plugin_solution.md §3.2]）：
/// - 高频管道插件用 Rust 原生实现（热路径零 IPC 开销）
/// - 低频管道插件可用 MCP 边车（通过 PluginInvoker 透明分发）
///
/// 对应 0.1 的 `pipeline/plugin.py IPlugin`。
#[async_trait]
pub trait PipelinePlugin: PluginMeta + Any {
    /// 管道角色（Input / Core / Output）。
    fn role(&self) -> PipelineRole;

    /// 执行插件逻辑。
    ///
    /// # Arguments
    /// * `ctx` - 插件执行上下文，包含管道状态、配置和租户信息
    ///
    /// # Returns
    /// 插件执行结果（状态更新 + 可能的路由信号）
    async fn execute(&self, ctx: &PluginContext) -> Result<PluginResult, PluginError>;

    /// 本插件可能产出的路由信号类型列表（仅 Output 角色有效）。
    ///
    /// 用于路由表配置校验，内核据此验证路由表引用的信号是否被某插件声明。
    fn route_signals(&self) -> Vec<RouteType> {
        Vec::new()
    }

    /// 生命周期钩子：插件加载时调用。
    async fn on_load(&self) -> Result<(), PluginError> {
        Ok(())
    }

    /// 生命周期钩子：插件卸载时调用。
    async fn on_unload(&self) -> Result<(), PluginError> {
        Ok(())
    }
}

/// 输入管道插件（Input 阶段）。
///
/// 负责在管道循环的输入阶段对状态进行预处理：
/// 参数校验、上下文注入、权限检查等。
///
/// 对应 0.1 的 `pipeline/plugin.py IInputPlugin`。
#[async_trait]
pub trait InputPipelinePlugin: PipelinePlugin {
    /// Input 插件固定返回 Input 角色。
    fn role(&self) -> PipelineRole {
        PipelineRole::Input
    }
}

/// 核心管道插件（Core 阶段）。
///
/// 负责执行核心逻辑（LLM 调用或工具执行）。
///
/// 对应 0.1 的 `pipeline/plugin.py ICorePlugin`。
#[async_trait]
pub trait CorePipelinePlugin: PipelinePlugin {
    /// Core 插件固定返回 Core 角色。
    fn role(&self) -> PipelineRole {
        PipelineRole::Core
    }

    /// 错误策略为 Fallback 时使用的默认状态更新。
    fn fallback_state(&self) -> HashMap<String, serde_json::Value> {
        HashMap::new()
    }
}

/// 输出管道插件（Output 阶段）。
///
/// 负责在管道循环的输出阶段处理核心结果：
/// 结果格式化、后处理、路由信号生成等。
///
/// 对应 0.1 的 `pipeline/plugin.py IOutputPlugin`。
#[async_trait]
pub trait OutputPipelinePlugin: PipelinePlugin {
    /// Output 插件固定返回 Output 角色。
    fn role(&self) -> PipelineRole {
        PipelineRole::Output
    }
}

// ── 3. PluginInvoker（插件调用器） ──────────────────────────────

/// 插件调用器：按 host_type 透明分发调用。
///
/// 核心设计（[来源: docs/0.2_rust_plugin_solution.md §3.2]）：
/// - `in_process`：直接调用 `dyn PipelinePlugin` 的 execute 方法（零 IPC 开销）
/// - `sidecar`：通过 rmcp 客户端走 MCP 协议调用（进程隔离）
/// - 两种路径对管道引擎透明——统一返回 `PluginResult`
#[async_trait]
pub trait PluginInvoker: Send + Sync {
    /// 调用管道插件执行。
    ///
    /// 内核根据插件的 `host_type` 字段选择调用路径：
    /// - InProcess: 直接 dyn PipelinePlugin::execute
    /// - McpSidecar: rmcp tools/call("execute", {state, config})
    async fn invoke_pipeline_plugin(
        &self,
        plugin_id: &str,
        ctx: &PluginContext,
    ) -> Result<PluginResult, PluginError>;

    /// 调用工具插件执行。
    async fn invoke_tool(
        &self,
        plugin_id: &str,
        tool_name: &str,
        inputs: &serde_json::Value,
    ) -> Result<ToolExecutionResult, PluginError>;

    /// 发送生命周期钩子事件到指定插件。
    async fn send_lifecycle_hook(
        &self,
        plugin_id: &str,
        hook: LifecycleHook,
        context: &HookContext,
    ) -> Result<(), PluginError>;
}

/// 生命周期钩子类型。
///
/// 对应 MCP 扩展协议中的 `__kernel_lifecycle_hook`。
/// [来源: .project/mcp_extension_protocol.md §2.2]
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LifecycleHook {
    OnLoad,
    OnUnload,
    OnPipelineStart,
    OnPipelineEnd,
    OnError,
}

/// 生命周期钩子上下文。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HookContext {
    pub session_id: String,
    pub task_id: String,
    pub tenant_id: String,
    pub pipeline_id: Uuid,
    pub iteration: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub state_snapshot: Option<serde_json::Value>,
}

impl HookContext {
    pub fn new(
        session_id: impl Into<String>,
        task_id: impl Into<String>,
        tenant_id: impl Into<String>,
        pipeline_id: Uuid,
    ) -> Self {
        Self {
            session_id: session_id.into(),
            task_id: task_id.into(),
            tenant_id: tenant_id.into(),
            pipeline_id,
            iteration: 0,
            state_snapshot: None,
        }
    }

    pub fn with_iteration(mut self, iteration: u32) -> Self {
        self.iteration = iteration;
        self
    }

    pub fn with_state_snapshot(mut self, snapshot: serde_json::Value) -> Self {
        self.state_snapshot = Some(snapshot);
        self
    }
}

// ── 4. CapabilityRegistry（能力注册表） ────────────────────────

/// 能力注册表：内核在加载插件后构建的全局能力索引。
///
/// 管理三类能力：
/// 1. **Tools**: 工具插件/系统插件提供的工具（供 LLM 选择和调用）
/// 2. **Resources**: 插件暴露的数据源（MCP resources 机制）
/// 3. **RouteSignals**: 管道插件声明的可能路由信号（供路由表校验）
#[async_trait]
pub trait CapabilityRegistry: Send + Sync {
    /// 注册插件提供的工具。
    fn register_tool(&self, plugin_id: &str, tool: ToolDescriptor);

    /// 注销插件提供的工具。
    fn unregister_tools(&self, plugin_id: &str);

    /// 按名称查询工具描述符。
    fn get_tool(&self, name: &str) -> Option<ToolDescriptor>;

    /// 获取所有已注册工具（供 LLM 选择）。
    fn list_tools(&self) -> Vec<ToolDescriptor>;

    /// 按分类筛选工具。
    fn list_tools_by_category(&self, category: &ToolCategory) -> Vec<ToolDescriptor>;

    /// 注册插件暴露的 MCP resource。
    fn register_resource(&self, plugin_id: &str, resource: ResourceDescriptor);

    /// 注销插件的 resources。
    fn unregister_resources(&self, plugin_id: &str);

    /// 列出所有已注册 resources。
    fn list_resources(&self) -> Vec<ResourceDescriptor>;

    /// 注册管道插件的路由信号声明。
    fn register_route_signals(&self, plugin_id: &str, signals: Vec<RouteType>);

    /// 检查某个路由信号是否被任何插件声明（路由表配置校验用）。
    fn has_route_signal(&self, signal: &RouteType) -> bool;

    /// 清除指定插件的所有注册项。
    fn clear_plugin(&self, plugin_id: &str);
}

/// 工具描述符（对内表示）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolDescriptor {
    pub name: String,
    pub description: String,
    pub plugin_id: String,
    pub input_schema: serde_json::Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub output_schema: Option<serde_json::Value>,
    pub category: ToolCategory,
    pub source: ToolSource,
}

/// Resource 描述符（对内表示）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResourceDescriptor {
    pub uri: String,
    pub name: String,
    pub plugin_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    #[serde(default = "default_mime")]
    pub mime_type: String,
}

fn default_mime() -> String {
    "application/json".to_string()
}

// ── 5. DependencyResolver（依赖解析器） ────────────────────────

/// 插件依赖解析器：解析插件间的依赖拓扑并按序加载。
///
/// 内核在实例化插件前，根据 manifest 中的 `dependencies` 字段
/// 构建依赖图并执行拓扑排序，确保被依赖的插件先加载。
pub trait DependencyResolver: Send + Sync {
    /// 添加插件依赖声明。
    fn add_dependency(&self, plugin_id: &str, dep: &Dependency);

    /// 构建依赖图并返回拓扑排序结果。
    ///
    /// # Returns
    /// Ok(plugin_ids) - 按加载顺序排列的插件 ID 列表
    /// Err(cycle) - 检测到循环依赖时返回参与环路的插件 ID 列表
    fn resolve(&self) -> Result<Vec<String>, DependencyError>;
}

/// 插件依赖声明（对应 manifest.dependencies[]）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Dependency {
    pub plugin_id: String,
    #[serde(default)]
    pub optional: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub min_version: Option<String>,
}

/// 依赖解析错误。
#[derive(Debug, Clone, thiserror::Error)]
pub enum DependencyError {
    /// 循环依赖
    #[error("circular dependency detected: {cycle:?}")]
    Circular { cycle: Vec<String> },

    /// 缺少必需依赖
    #[error("missing required dependency '{plugin_id}' for '{dependent}'")]
    MissingRequired {
        plugin_id: String,
        dependent: String,
    },

    /// 版本不兼容
    #[error("dependency version mismatch: '{plugin_id}' requires >= {required}, but found {actual}")]
    VersionMismatch {
        plugin_id: String,
        required: String,
        actual: String,
    },
}

// ── 6. LlmProvider（LLM 抽象层） ──────────────────────────────

/// LLM 服务提供者抽象（抽象层 + 可替换实现模式）。
///
/// 设计原则（[来源: docs/0.2_rust_plugin_solution.md §3.5]）：
/// - LLM Provider 实现会变（新增厂商、切换 API），但"调用 LLM 返回文本"这个动作不变
/// - 抽象层长期保留，具体实现藏在各自模块内部
/// - 外部（管道引擎 Core 插件）只看到统一的调用接口
#[async_trait]
pub trait LlmProvider: Send + Sync {
    /// 非流式补全调用。
    ///
    /// # Arguments
    /// * `model` - 模型标识
    /// * `messages` - 消息列表
    /// * `options` - 调用选项（temperature、max_tokens 等）
    async fn complete(
        &self,
        model: &str,
        messages: &[LlmMessage],
        options: &LlmOptions,
    ) -> Result<LlmResponse, LlmError>;

    /// 流式补全调用。
    ///
    /// 通过 channel 推送流式 chunk，调用方从 channel 接收。
    /// 对应 0.1 的流式响应机制（管道引擎通过 stream bridge 推送到前端）。
    async fn complete_stream(
        &self,
        model: &str,
        messages: &[LlmMessage],
        options: &LlmOptions,
    ) -> Result<tokio::sync::mpsc::Receiver<LlmStreamChunk>, LlmError>;

    /// 获取可用模型列表。
    async fn list_models(&self) -> Result<Vec<ModelInfo>, LlmError>;
}

/// LLM 消息。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmMessage {
    pub role: MessageRole,
    pub content: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_calls: Option<Vec<ToolCallRequest>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
}

/// 消息角色。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum MessageRole {
    System,
    User,
    Assistant,
    Tool,
}

/// 工具调用请求（LLM 返回的 function_call）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolCallRequest {
    pub id: String,
    pub name: String,
    pub arguments: serde_json::Value,
}

/// LLM 调用选项。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct LlmOptions {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub temperature: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_tokens: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub top_p: Option<f64>,
    /// 允许 LLM 调用的工具列表（function calling）
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub tools: Vec<ToolCallDefinition>,
}

/// 工具调用定义（传给 LLM 的 function schema）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolCallDefinition {
    pub name: String,
    pub description: String,
    pub input_schema: serde_json::Value,
}

/// LLM 非流式响应。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmResponse {
    pub content: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_calls: Option<Vec<ToolCallRequest>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thinking: Option<String>,
    pub usage: TokenUsage,
    pub model: String,
    pub finish_reason: FinishReason,
}

/// Token 用量统计。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct TokenUsage {
    pub prompt_tokens: u64,
    pub completion_tokens: u64,
    pub total_tokens: u64,
}

/// 完成原因。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FinishReason {
    Stop,
    Length,
    ToolCalls,
    ContentFilter,
    Error,
}

/// LLM 流式 chunk。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmStreamChunk {
    /// 文本增量
    #[serde(skip_serializing_if = "Option::is_none")]
    pub delta: Option<String>,
    /// 工具调用增量
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_call_delta: Option<ToolCallRequest>,
    /// 思考过程增量
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thinking_delta: Option<String>,
    /// 是否结束
    pub done: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub finish_reason: Option<FinishReason>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub usage: Option<TokenUsage>,
}

/// 模型信息。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelInfo {
    pub id: String,
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub context_window: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_output_tokens: Option<u32>,
}

/// LLM 调用错误。
#[derive(Debug, Clone, thiserror::Error)]
pub enum LlmError {
    /// 网络错误
    #[error("network error: {message}")]
    Network { message: String },

    /// 认证失败
    #[error("authentication failed: {message}")]
    Auth { message: String },

    /// 速率限制
    #[error("rate limited, retry after {retry_after_secs:?}s")]
    RateLimited { retry_after_secs: Option<u64> },

    /// 模型不可用
    #[error("model '{model}' not available: {reason}")]
    ModelUnavailable { model: String, reason: String },

    /// 上下文超长
    #[error("context length exceeded: prompt {prompt_tokens} > limit {max_tokens}")]
    ContextLength { prompt_tokens: u64, max_tokens: u64 },

    /// 内容过滤
    #[error("content filtered: {reason}")]
    ContentFiltered { reason: String },

    /// 其他错误
    #[error("LLM error: {message}")]
    Other { message: String },
}

// ── 7. PluginLoader（插件加载器） ──────────────────────────────

/// 插件加载器：负责从文件系统发现、解析 manifest、加载插件实例。
///
/// 遵循按需加载全局原则（[来源: docs/0.2_rust_plugin_solution.md §3.7]）：
/// - 插件进程按需启动：首次被调用时才启动 MCP 边车进程
/// - 空闲超时自动卸载
/// - Rust 原生管道插件按需注册：manifest 声明但不立即实例化
#[async_trait]
pub trait PluginLoader: Send + Sync {
    /// 扫描指定根目录，发现所有 plugin.json manifest。
    ///
    /// # Returns
    /// 发现的插件 manifest 列表（尚未实例化）
    async fn discover(&self, root_paths: &[&str]) -> Result<Vec<PluginManifest>, PluginError>;

    /// 验证 manifest 是否符合 Schema（使用 manifest_v2_schema.json）。
    fn validate_manifest(&self, manifest: &PluginManifest) -> Result<(), PluginError>;

    /// 按需加载（实例化）指定插件。
    ///
    /// 如果插件已加载则直接返回引用；如果未加载则首次实例化。
    async fn load(&self, plugin_id: &str) -> Result<LoadedPlugin, PluginError>;

    /// 卸载插件（释放进程/资源）。
    async fn unload(&self, plugin_id: &str) -> Result<(), PluginError>;

    /// 查询插件当前加载状态。
    fn get_status(&self, plugin_id: &str) -> PluginStatus;
}

/// 已解析的插件 Manifest（运行时表示）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PluginManifest {
    pub id: String,
    pub name: String,
    pub version: String,
    pub plugin_type: PluginType,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pipeline_role: Option<PipelineRole>,
    pub language: String,
    pub host_type: HostType,
    pub entry: String,
    pub capabilities: ManifestCapabilities,
    #[serde(default)]
    pub dependencies: Vec<Dependency>,
    #[serde(default)]
    pub permissions: ManifestPermissions,
    #[serde(default)]
    pub error_policy: ErrorPolicy,
    #[serde(default = "default_priority")]
    pub priority: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mcp: Option<McpConfig>,
}

fn default_priority() -> u32 {
    100
}

/// 宿主类型。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum HostType {
    /// Rust 原生进程内调用（零 IPC 开销）
    InProcess,
    /// 独立进程通过 MCP 协议通信
    #[default]
    Sidecar,
}

/// Manifest 能力声明（运行时表示）。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ManifestCapabilities {
    #[serde(default)]
    pub tools: Vec<ToolCapability>,
    #[serde(default)]
    pub resources: Vec<ResourceCapability>,
    #[serde(default)]
    pub route_signals: Vec<RouteType>,
    #[serde(default)]
    pub lifecycle_hooks: Vec<LifecycleHook>,
}

/// 工具能力声明。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolCapability {
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub input_schema: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub output_schema: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub category: Option<ToolCategory>,
}

/// Resource 能力声明。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResourceCapability {
    pub uri: String,
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    #[serde(default = "default_mime")]
    pub mime_type: String,
}

/// Manifest 权限声明。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ManifestPermissions {
    #[serde(default)]
    pub filesystem: FilesystemPermission,
    #[serde(default)]
    pub network: NetworkPermission,
    #[serde(default)]
    pub env_vars: Vec<String>,
    #[serde(default)]
    pub system_calls: Vec<String>,
}

/// 文件系统权限。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct FilesystemPermission {
    #[serde(default)]
    pub read_paths: Vec<String>,
    #[serde(default)]
    pub write_paths: Vec<String>,
}

/// 网络权限。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct NetworkPermission {
    #[serde(default)]
    pub allowed_hosts: Vec<String>,
}

/// MCP 配置。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct McpConfig {
    pub transport: McpTransport,
    #[serde(default = "default_idle_timeout")]
    pub idle_timeout_secs: u64,
    #[serde(default = "default_protocol_version")]
    pub protocol_version: String,
}

fn default_idle_timeout() -> u64 {
    300
}

fn default_protocol_version() -> String {
    "2025-06-18".to_string()
}

/// MCP 传输方式。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum McpTransport {
    #[default]
    Stdio,
    StreamableHttp,
}

/// 已加载的插件实例。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LoadedPlugin {
    pub manifest: PluginManifest,
    pub status: PluginStatus,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub loaded_at: Option<chrono::DateTime<chrono::Utc>>,
}

/// 插件加载状态。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum PluginStatus {
    /// 已发现但未加载
    #[default]
    Discovered,
    /// 正在加载
    Loading,
    /// 已加载，就绪
    Active,
    /// 空闲等待中（按需加载策略）
    Idle,
    /// 正在卸载
    Draining,
    /// 已卸载
    Unloaded,
    /// 进程崩溃
    Crashed,
    /// 加载失败
    Failed,
}

//! 内核核心 Trait 定义
//!
//! 这是 0.2 架构的"宪法"层接口契约。所有内核组件和插件都围绕这些 trait 构建。
//!
//! 设计决策来源：
//! - 管道插件混合方案（Rust 原生 + MCP 边车）：[来源: docs/0.2_rust_plugin_solution.md §3.2]
//! - 路由信号精简为 4 种：[来源: docs/0.2_rust_plugin_solution.md §3.5]
//! - 按需加载全局原则：[来源: docs/0.2_rust_plugin_solution.md §3.7]
//! - 多租户上下文穿透：[来源: docs/0.2_rust_plugin_solution.md §3.4]
//!
//! ADR 修订（v2.0）：
//! - HookContext 改为标签化动态上下文 HashMap（ADR ⑨）
//! - PluginType 新增 Composite 组合插件类型（ADR ⑥）
//! - 所有插件均支持 InProcess + Sidecar 双路径（ADR ⑧）
//! - 新增 StorageBackend trait——SQLite 四表存储抽象（ADR ③④）
//! - 新增 AdrEngine trait——极简调度器 + 状态账本（ADR ①）

use std::any::Any;
use std::collections::HashMap;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};

use crate::types::{
    Branch, CompositeStep, EngineError, ErrorPolicy, Message, MessageRecord, PluginContext,
    PluginError, PluginResult, RouteType, RunRecord, RunStatus, StepResult, StorageError,
    SuspendHandle, ToolCategory, ToolExecutionResult, ToolSource, TraceEntry, WakeEvent,
};

// ── 1. 插件基础 Trait ───────────────────────────────────────────

/// 插件元信息——所有插件（管道/工具/系统/组合）共有的标识与描述。
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
///
/// ADR ⑥ 新增 `Composite` 变体——组合插件由 YAML 配置编排步骤，引擎解释执行。
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PluginType {
    /// 管道插件（Input / Core / Output 三阶段）
    Pipeline,
    /// 工具插件（提供 MCP 工具）
    Tool,
    /// 系统插件（记忆/审批/评估等内核级服务）
    System,
    /// 组合插件（ADR ⑥：YAML 配置编排步骤，引擎解释执行）
    Composite,
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

/// 管道插件统一接口。
///
/// **ADR ⑧ 更新**：所有插件（含工具插件、系统插件）均支持 InProcess（Rust 原生）
/// 和 Sidecar（MCP 边车）两种执行路径，由开发者根据性能需求自行选择，
/// 不因插件类型限制可选路径。
///
/// **混合方案**（[来源: docs/0.2_rust_plugin_solution.md §3.2]）：
/// - 高频管道插件用 Rust 原生实现（热路径零 IPC 开销）
/// - 低频管道插件可用 MCP 边车（通过 PluginInvoker 透明分发）
/// - 两种路径对管道引擎透明——统一返回 `PluginResult`
///
/// **ADR ② 约束**：插件无状态不持久化——trait 签名是 `&self`（不可变引用），
/// 插件不持有可变状态，不直接操作引擎存储。`PluginResult.state_updates` 是 Patch
/// 而非直接写存储，引擎收到后决定是否应用。
///
/// 对应 0.1 的 `pipeline/plugin.py IPlugin`。
#[async_trait]
pub trait PipelinePlugin: PluginMeta + Any {
    /// 管道角色（Input / Core / Output）。
    fn role(&self) -> PipelineRole;

    /// 执行插件逻辑。
    ///
    /// # Arguments
    /// * `ctx` - 插件执行上下文，包含管道状态、配置、租户信息和内容懒加载句柄
    ///
    /// # Returns
    /// 插件执行结果（状态更新 Patch + 可能的路由信号）
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
///
/// **ADR ⑧ 更新**：所有插件（含工具插件、系统插件）均支持 InProcess 和 Sidecar
/// 两种执行路径。原"工具/系统插件推荐用 Python 边车"的措辞已废除。
/// 由开发者根据性能需求自行选择，不因插件类型限制可选路径。
#[async_trait]
pub trait PluginInvoker: Send + Sync {
    /// 调用管道插件执行。
    ///
    /// 内核根据插件的 `host_type` 字段选择调用路径：
    /// - InProcess: 直接 dyn PipelinePlugin::execute
    /// - Sidecar: rmcp tools/call("execute", {state, config})
    async fn invoke_pipeline_plugin(
        &self,
        plugin_id: &str,
        ctx: &PluginContext,
    ) -> Result<PluginResult, PluginError>;

    /// 调用工具插件执行。
    ///
    /// 工具插件同样支持 InProcess 和 Sidecar 两种路径（ADR ⑧）。
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

/// 生命周期钩子上下文（ADR ⑨ 标签化动态上下文）。
///
/// **ADR ⑨ 改造**：从固定 6 字段结构体改为 `HashMap<String, serde_json::Value>`
/// 标签化动态上下文。内核和插件可以自由写入任意标签，消费方按需读取。
/// 新增上下文信息只需 `ctx.set("key", value)`，不需改 struct 定义。
///
/// 常用标签键（非强制，仅为约定）：
/// - `session_id`: 会话 ID
/// - `task_id`: 任务 ID
/// - `tenant_id`: 租户 ID
/// - `pipeline_id`: 管道 ID
/// - `iteration`: 迭代轮次
/// - `branch_id`: 分支 ID（ADR ⑤）
/// - `seq_in_branch`: 分支内序列号（ADR ⑤）
/// - `state_snapshot`: 状态快照（可选）
///
/// [来源: docs/working/adr_engine_design.md §7.3]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HookContext {
    /// 标签集合：key → value
    tags: HashMap<String, serde_json::Value>,
}

impl HookContext {
    /// 创建空的标签化上下文。
    pub fn new() -> Self {
        Self {
            tags: HashMap::new(),
        }
    }

    /// 写入标签（Builder 模式，支持链式调用）。
    pub fn set(&mut self, key: impl Into<String>, value: serde_json::Value) -> &mut Self {
        self.tags.insert(key.into(), value);
        self
    }

    /// 读取标签（返回 serde_json::Value 引用）。
    pub fn get(&self, key: &str) -> Option<&serde_json::Value> {
        self.tags.get(key)
    }

    /// 读取标签并尝试转换为目标类型。
    ///
    /// 利用 serde 反序列化将 `serde_json::Value` 转换为指定类型。
    /// 转换失败返回 None（静默降级，消费方按需处理）。
    pub fn get_as<T: serde::de::DeserializeOwned>(&self, key: &str) -> Option<T> {
        self.tags
            .get(key)
            .and_then(|v| serde_json::from_value(v.clone()).ok())
    }

    /// 获取所有标签的只读引用。
    pub fn tags(&self) -> &HashMap<String, serde_json::Value> {
        &self.tags
    }
}

impl Default for HookContext {
    fn default() -> Self {
        Self::new()
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

    /// 注册插件贡献的 HTTP 端点（ADR §3.3）。
    ///
    /// 注册期执行路由治理（附录 E.1.3）：
    /// - 命名空间校验：path 必须以 `/ext/{plugin_id}/**` 为前缀；
    /// - denylist：path 不得含内核保留段（/ws、/api/v1/*、/health）；
    /// - 冲突检测：同 path+method 冲突 fail-closed（返回错误，不静默覆盖）。
    ///
    /// 校验通过后写入 `http_routes` 维；失败返回聚合错误信息。
    fn register_http_route(
        &self,
        plugin_id: &str,
        endpoint: HttpEndpoint,
    ) -> Result<HttpRouteDescriptor, String>;

    /// 列出所有已注册 HTTP 路由（dispatcher 据此动态挂载）。
    fn list_http_routes(&self) -> Vec<HttpRouteDescriptor>;

    /// 按 path+method 查询单个路由（dispatcher 请求分发用）。
    fn find_http_route(&self, path: &str, method: &str) -> Option<HttpRouteDescriptor>;

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
    #[error(
        "dependency version mismatch: '{plugin_id}' requires >= {required}, but found {actual}"
    )]
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

    /// 加载配置文件，返回合并后的配置 JSON。
    ///
    /// 扫描配置目录下的所有 YAML 文件，解析并合并为一个 JSON 对象。
    /// 文件名（不含扩展名）作为 key，文件内容解析后的 JSON 作为 value。
    ///
    /// 默认返回空 `{}`，由具体实现覆盖。
    async fn load_config(&self) -> Result<serde_json::Value, PluginError> {
        Ok(serde_json::json!({}))
    }

    /// 获取插件的目录路径（包含 plugin.json/server.py 的目录）。
    ///
    /// 用于 PluginInvokerImpl 设置 sidecar 进程的 working_dir，
    /// 确保插件代码中的相对路径（如 `python3 server.py`）能正确解析。
    ///
    /// 默认返回 None，表示使用内核进程的 CWD。
    fn get_plugin_dir(&self, _plugin_id: &str) -> Option<String> {
        None
    }
}

/// 已解析的插件 Manifest（运行时表示）。
///
/// **ADR ⑦ 新增**：`requires_content` 字段声明插件需要的最近消息条数，
/// 引擎据此从 blobs 表按需加载消息内容。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PluginManifest {
    pub id: String,
    pub name: String,
    pub version: String,
    pub plugin_type: PluginType,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pipeline_role: Option<PipelineRole>,
    pub language: String,
    /// 宿主类型——所有插件均支持 InProcess 和 Sidecar（ADR ⑧）
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
    /// 内容懒加载声明（ADR ⑦）。
    ///
    /// 声明插件需要多少条最近消息的完整内容。
    /// 引擎据此从 blobs 表预加载，插件也可通过 ContentLoader 运行时按需加载。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub requires_content: Option<u32>,
    /// 配置按需注入声明（ADR 配置统一）。
    ///
    /// 声明该插件需要读取哪些配置节（如 `["models", "memory_storage"]`）。
    /// 内核 `load_config` 据此过滤，只投递声明的配置节（而非全量）。
    /// 未声明时注入全量配置（向后兼容）。
    ///
    /// **迁移期保留（ADR §8.2 step1）**：P1~P5 期间与 `config_files` 并存，
    /// loader 优先 `config_files`，无则回退 `config_refs`；P6 才删除。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub config_refs: Vec<String>,
    /// 配置文件显式映射（ADR §4.2/§4.3 config_files）。
    ///
    /// 每项把一个配置文件（id/path/label）显式映射到现有 `config/` 子树下的文件。
    /// P1 新增字段，与 `config_refs` 并存（迁移期 loader 优先 config_files）。
    /// 未声明时为空 vec（向后兼容）。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub config_files: Vec<ConfigFileMapping>,
    /// 前端 UI Schema 声明（ADR 前端 schema 驱动）。
    ///
    /// 声明该插件要呈现的前端界面（用哪些 widget type、渲染空间、触发时机）。
    /// 内核 schema 端点将 ui_schema 一并暴露给前端，前端据此自动渲染界面，
    /// 新增插件无需手写前端代码。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ui_schema: Option<serde_json::Value>,
    /// 前端贡献点声明（ADR §3.4/§六 contributes，task_11 P4/P5）。
    ///
    /// 声明该插件向前端贡献的 UI 插槽内容：viewsContainers/views/workspaceTabs/
    /// dockItems/floating/modal/statusBarItems/menus/commands/shortcuts/
    /// chatMessages/chatInteractions/chatActions/settingsPanels/widgets。
    /// 内核不解释其结构，仅在 /api/v1/schema 透传聚合（`plugin_contributes`），
    /// 由前端 ContributionRegistry 作为唯一真相源消费（与 ui_schema 透传同理）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub contributes: Option<serde_json::Value>,
    /// HTTP 端点贡献声明（ADR §3.3）。
    ///
    /// 插件可向内核统一 HTTP server 贡献端点（如企微 webhook 回调）。
    /// 每项声明一个 route（path/method/auth/handler_capability/timeout/concurrency），
    /// dispatcher 据此动态挂载路由，经 capability RPC 调插件的 `http.handle`。
    /// 路由治理见附录 E.1.3（强制 /ext/{plugin_id}/** 命名空间 + 内核 denylist）。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub http_endpoints: Vec<HttpEndpoint>,
}

/// HTTP 端点声明项（ADR §3.3 / E.1.2）。
///
/// 一个插件端点 = 一个 (path, method) 路由。企微 webhook 双方法（GET 验证 +
/// POST 回调）声明为两条同 path 不同 method 的记录（path+method 才算冲突）。
///
/// `auth` 字段经自定义反序列化校验：仅接受 `none` / `user` / `admin`
/// （ADR §3.3 auth 枚举），非法值在反序列化期即被拒绝。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct HttpEndpoint {
    /// 路由标识（插件内唯一）。
    pub route_id: String,
    /// HTTP 方法（GET/POST/...）。
    pub method: String,
    /// 完整路径，必须落在 `/ext/{plugin_id}/**` 命名空间下（注册期校验）。
    pub path: String,
    /// 鉴权模式：`none`（webhook 验签自管）/ `user`（内核 JWT）/ `admin`。
    #[serde(deserialize_with = "deserialize_http_auth")]
    pub auth: String,
    /// 处理该端点的 capability 名（统一 `http.handle`）。
    pub handler_capability: String,
    /// 单请求超时上限（毫秒），默认 30000（附录 E.1.3）。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timeout_ms: Option<u64>,
    /// 并发上限，超限返回 503，默认 16（附录 E.1.3）。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_concurrency: Option<u32>,
    /// 人类可读描述。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
}

/// 校验 HttpEndpoint.auth 仅接受 none/user/admin（ADR §3.3 auth 枚举）。
fn deserialize_http_auth<'de, D>(deserializer: D) -> Result<String, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let raw = String::deserialize(deserializer)?;
    match raw.as_str() {
        "none" | "user" | "admin" => Ok(raw),
        other => Err(serde::de::Error::custom(format!(
            "invalid http_endpoint auth '{other}': must be one of none/user/admin"
        ))),
    }
}

/// HTTP 端点默认超时（毫秒），ADR 附录 E.1.3。
pub const HTTP_ENDPOINT_DEFAULT_TIMEOUT_MS: u64 = 30_000;
/// HTTP 端点默认并发上限，ADR 附录 E.1.3。
pub const HTTP_ENDPOINT_DEFAULT_MAX_CONCURRENCY: u32 = 16;

/// 已注册的 HTTP 路由描述符（运行时表示）。
///
/// 由 [`CapabilityRegistry`] 在注册期校验命名空间/denylist/冲突后产出，
/// dispatcher（ADR §3.3）据此动态挂载 axum 路由并查询路由归属。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct HttpRouteDescriptor {
    /// 所属插件 id（命名空间归属，dispatcher 据此路由到该插件的 http.handle）。
    pub plugin_id: String,
    /// 原始端点声明。
    pub endpoint: HttpEndpoint,
    /// 解析后的超时（声明值或默认 30000ms）。
    timeout_ms: u64,
    /// 解析后的并发上限（声明值或默认 16）。
    max_concurrency: u32,
}

impl HttpRouteDescriptor {
    /// 创建描述符，应用 timeout/concurrency 默认值。
    pub fn new(plugin_id: String, endpoint: HttpEndpoint) -> Self {
        let timeout_ms = endpoint
            .timeout_ms
            .unwrap_or(HTTP_ENDPOINT_DEFAULT_TIMEOUT_MS);
        let max_concurrency = endpoint
            .max_concurrency
            .unwrap_or(HTTP_ENDPOINT_DEFAULT_MAX_CONCURRENCY);
        Self {
            plugin_id,
            endpoint,
            timeout_ms,
            max_concurrency,
        }
    }

    /// 解析后的单请求超时（毫秒）。
    pub fn timeout_ms(&self) -> u64 {
        self.timeout_ms
    }

    /// 解析后的并发上限。
    pub fn max_concurrency(&self) -> u32 {
        self.max_concurrency
    }
}

/// HTTP 端点入站请求（HTTP → 插件，ADR 附录 E.1.2）。
///
/// **铁律**：`raw_body` 是原始字节的 base64 编码，内核绝不反序列化 body 再转发。
/// headers/query 全量透传，插件据此做验签（如企微 SHA1）与解密。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HttpHandleRequest {
    /// HTTP 方法。
    pub method: String,
    /// 完整路径。
    pub path: String,
    /// 目标插件 id（dispatcher 路由查找后填入，生产 multiplexer 据此调对应插件 http.handle）。
    pub plugin_id: String,
    /// 原始 body 字节的 base64 编码（不做反序列化）。
    pub raw_body: String,
    /// 全量原始 headers（key → value，多值用逗号拼接或取首个）。
    pub headers: HashMap<String, String>,
    /// 查询参数（key → value）。
    pub query: HashMap<String, String>,
}

/// HTTP 端点出站响应（插件 → HTTP 响应，ADR 附录 E.1.2）。
///
/// **铁律**：插件完全控制 status/headers/body（不限 JSON）。dispatcher 只透传，
/// 不做内容包装。企微回包经 `encrypt_response` 产出加密 XML，由插件作为 body 返回。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HttpHandleResponse {
    /// HTTP 状态码（插件控制）。
    pub status: u16,
    /// 响应 headers（插件控制）。
    pub headers: HashMap<String, String>,
    /// 响应 body（base64 编码的字节）。
    pub body: String,
    /// body 编码（统一 `base64`）。
    pub body_encoding: String,
}

/// 插件 HTTP 处理能力（capability RPC `http.handle` 的进程内抽象）。
///
/// dispatcher（ADR §3.3）经此 trait 把入站请求交给插件。生产实现走 sidecar
/// MCP（`tools/call("http.handle", ...)`）；测试用进程内实现验证透传链路。
#[async_trait]
pub trait HttpHandleCapability: Send + Sync {
    /// 处理一个 HTTP 请求，返回插件自定义响应；出错返回错误字符串（dispatcher 记 502）。
    async fn handle(&self, req: HttpHandleRequest) -> Result<HttpHandleResponse, String>;
}

fn default_priority() -> u32 {
    100
}

/// 宿主类型。
///
/// **ADR ⑧**：所有插件（含工具插件、系统插件）均支持以下两种执行路径，
/// 由开发者根据性能需求自行选择，不因插件类型限制可选路径：
/// - `InProcess`：Rust 原生进程内调用，零 IPC 开销，适合高频热路径
/// - `Sidecar`：独立进程通过 MCP 协议通信，进程隔离，适合低频或第三方插件
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
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

/// 配置文件映射项（ADR §4.2/§4.3 `config_files[]`）。
///
/// 把一个现有 `config/` 子树下的文件显式映射给插件。每项三要素：
/// - `id`：该配置子项的标识（插件内唯一，作为注入命名空间 key 与 API file_id）；
/// - `path`：相对 `config/` 根的路径（如 `config/models/llm.yaml`）；
/// - `label`：前端展示用的名称。
///
/// path 安全校验见 loader 的 B1 实现（归一化 + 落 config/ 子树 + denylist）。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ConfigFileMapping {
    /// 配置子项标识（插件内唯一）。
    pub id: String,
    /// 相对 config/ 根的文件路径（含 config/ 前缀或相对形式均可，loader 归一化）。
    pub path: String,
    /// 前端展示名称。
    pub label: String,
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

// ═════════════════════════════════════════════════════════════════
// ADR ③④：StorageBackend trait——SQLite 四表存储抽象
// ═════════════════════════════════════════════════════════════════

/// 存储后端抽象（ADR ③④）。
///
/// SQLite 四表模型的 trait 抽象，供 ContentLoader 和 AdrEngine 使用。
/// 具体实现为 SQLite，但 trait 层不绑定具体数据库——便于测试时 mock。
///
/// **四表模型**：
/// - `runs`：运行实例元数据
/// - `messages`：消息表（含分支标识）
/// - `traces`：状态变更日志（Append-Only Patch）
/// - `blobs`：不可变原始数据
///
/// [来源: docs/working/adr_engine_design.md §4.2]
#[async_trait]
pub trait StorageBackend: Send + Sync {
    /// 获取运行实例记录。
    async fn get_run(&self, run_id: &str) -> Result<RunRecord, StorageError>;

    /// 获取指定分支的所有消息记录。
    async fn get_messages(
        &self,
        run_id: &str,
        branch_id: &str,
    ) -> Result<Vec<MessageRecord>, StorageError>;

    /// 获取最近 N 条消息的完整内容（联查 messages + blobs 表）。
    ///
    /// 这是 ContentLoader 的底层调用——从 messages 表查询最近 N 条消息的
    /// blob_id，再从 blobs 表加载完整内容，组装成 Message 返回。
    async fn get_recent_messages(
        &self,
        run_id: &str,
        branch_id: &str,
        n: usize,
    ) -> Result<Vec<Message>, StorageError>;

    /// 获取指定 blob_id 的原始数据。
    async fn get_blob(&self, blob_id: &str) -> Result<Vec<u8>, StorageError>;

    /// 追加一条状态变更日志到 traces 表（Append-Only，ADR ③）。
    async fn append_trace(&self, entry: TraceEntry) -> Result<(), StorageError>;

    /// 创建新分支（ADR ⑤：回滚 = 创建新分支 + 正向重放 Patch）。
    async fn create_branch(&self, branch: Branch) -> Result<(), StorageError>;

    /// 更新运行实例状态。
    async fn update_run_status(
        &self,
        run_id: &str,
        status: RunStatus,
        current_branch: Option<&str>,
        current_seq: Option<u32>,
    ) -> Result<(), StorageError>;
}

// ═════════════════════════════════════════════════════════════════
// ADR ①：AdrEngine trait——极简调度器 + 状态账本
// ═════════════════════════════════════════════════════════════════

/// ADR 引擎：调度器 + 状态账本（ADR ①）。
///
/// 设计原则（ADR ①）：
/// - 引擎不含业务逻辑，只负责按配置顺序调用插件、维护状态一致性、记录变更日志
/// - 状态以 SQLite 为正本（ADR ③④），所有变更以追加 Patch 记录（ADR ③）
/// - 回滚通过创建新分支 + 正向重放 Patch（ADR ⑤）
///
/// 引擎核心循环：
/// ```text
/// 1. 从 config 加载步骤序列（YAML 定义）
/// 2. for each step in steps:
///    a. 构造 PluginContext（从 SQLite 读取当前状态 + 按需加载 BLOB 内容）
///    b. 通过 PluginInvoker 调用插件 execute(ctx) -> PluginResult
///    c. 将 PluginResult.state_updates 作为 Patch 追加到 traces 表
///    d. 如果 PluginResult 有 route_signal：
///       - NextLlm → 下一步调用 LLM 原子插件
///       - NextTool → 下一步调用 Tool 原子插件
///       - End → 结束循环
///       - Wait → 挂起，保存分支状态
///    e. 如果出错：按 ErrorPolicy 处理（Abort/Skip/Retry/Fallback）
/// 3. 记录运行结束到 runs 表
/// ```
///
/// [来源: docs/working/adr_engine_design.md §3.3]
#[async_trait]
pub trait AdrEngine: Send + Sync {
    /// 启动一次运行实例。
    ///
    /// 在 runs 表创建记录，初始化主分支。
    ///
    /// # Returns
    /// 运行实例 ID
    async fn start_run(&self, config: &serde_json::Value) -> Result<String, EngineError>;

    /// 执行一个步骤（原子插件或组合插件中的一个 step）。
    ///
    /// 1. 构造 PluginContext（从 SQLite 读取当前状态 + 按需加载 BLOB 内容）
    /// 2. 通过 PluginInvoker 调用插件 execute(ctx) -> PluginResult
    /// 3. 将 PluginResult.state_updates 作为 Patch 追加到 traces 表
    async fn execute_step(
        &self,
        run_id: &str,
        step: &CompositeStep,
    ) -> Result<StepResult, EngineError>;

    /// 挂起运行（ADR ⑤：保存分支状态，等待外部事件）。
    ///
    /// 将 runs 表状态更新为 Suspended，保存当前分支和序列号。
    async fn suspend(&self, run_id: &str) -> Result<SuspendHandle, EngineError>;

    /// 恢复运行（ADR ⑤：从分支状态恢复）。
    ///
    /// 将 runs 表状态更新为 Running，根据唤醒事件继续执行。
    async fn resume(&self, handle: &SuspendHandle, event: WakeEvent) -> Result<(), EngineError>;

    /// 回滚（ADR ⑤：创建新分支 + 正向重放 Patch 恢复状态）。
    ///
    /// 1. 创建新分支（branch_id = "{parent}.rollback.{n}"）
    /// 2. 从 parent_branch 的 seq=0 到 target_seq 正向重放 Patch
    /// 3. 恢复状态到 target_seq 的快照
    ///
    /// # Returns
    /// 新分支 ID
    async fn rollback(&self, run_id: &str, target_seq: u32) -> Result<String, EngineError>;

    /// 结束运行。
    ///
    /// 将 runs 表状态更新为 Completed/Failed，记录结束时间。
    async fn end_run(&self, run_id: &str) -> Result<(), EngineError>;
}

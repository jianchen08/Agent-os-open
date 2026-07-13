//! 核心类型定义
//!
//! 对应 0.1 的 `pipeline/types.py`，0.2 将 RouteSignal 精简为 4 种
//! （移除 Delegate / Fork / Decision，详见方案总纲 §3.5）。

use std::collections::HashMap;

use serde::{Deserialize, Serialize};
use uuid::Uuid;

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
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PluginResult {
    /// 需要合并到管道状态的更新
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

impl Default for PluginResult {
    fn default() -> Self {
        Self {
            state_updates: HashMap::new(),
            route_signal: None,
            skip_remaining: false,
            error: None,
        }
    }
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

// ── 插件上下文 ──────────────────────────────────────────────────

/// 插件执行上下文。
///
/// 对应 0.1 的 `pipeline/plugin.py PluginContext`。
/// 封装管道状态、插件配置和服务访问能力，传递给每个插件的 execute 方法。
#[derive(Debug, Clone)]
pub struct PluginContext {
    /// 管道当前状态（JSON Value 形式，支持嵌套）
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
}

impl PluginContext {
    pub fn new(
        state: serde_json::Value,
        config: serde_json::Value,
        tenant: TenantContext,
        pipeline_id: Uuid,
    ) -> Self {
        Self {
            state,
            config,
            tenant,
            pipeline_id,
            session_id: String::new(),
            task_id: String::new(),
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

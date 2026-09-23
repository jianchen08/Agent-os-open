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

use std::collections::HashMap;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};

use crate::types::{
    MessageRecord, PendingInputRecord, PipelineRunInfo, PluginContext, PluginError, PluginResult,
    RouteType, RunRecord, SegmentRecord, SessionRecord, StorageError, ToolCategory,
    ToolExecutionResult, ToolSource, TraceEntry, UserRecord,
};

// ── 1. 插件类型 ────────────────────────────────────────────────

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

// ── 3. PluginInvoker（插件调用器） ──────────────────────────────

/// 插件调用器：按 host_type 透明分发调用。
///
/// 核心设计（[来源: docs/0.2_rust_plugin_solution.md §3.2]）：
/// - `in_process`：直接调用 `agentos_native_sdk::PipelinePlugin` 的 execute 方法（零 IPC 开销）
/// - `sidecar`：通过 rmcp 客户端走 MCP 协议调用（进程隔离）
/// - 两种路径对管道引擎透明——统一返回 `PluginResult`
///
/// 所有插件（含工具插件、系统插件）均支持 InProcess 和 Sidecar 两种执行路径
/// （ADR ⑧），由开发者根据性能需求自行选择，不因插件类型限制可选路径。
#[async_trait]
pub trait PluginInvoker: Send + Sync {
    /// 调用管道插件执行。
    ///
    /// 内核根据插件的 `host_type` 字段选择调用路径：
    /// - InProcess: 直接调 agentos_native_sdk::PipelinePlugin::execute
    /// - Sidecar: rmcp tools/call("execute", {state, config})
    ///
    /// ctx.state 为借用（生命周期在方法泛型位，trait object 兼容）：管道步骤
    /// 串行 await，invoke 期间引擎不写 state。
    async fn invoke_pipeline_plugin<'a>(
        &self,
        plugin_id: &str,
        ctx: &PluginContext<'a>,
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

    /// 解析插件所在宿主的键（生命周期通知的投递单位，BUG-7 宿主去重）。
    ///
    /// 合宿（co-host）进程的生命周期通知是宿主级广播：聚合服务端把每条
    /// `notifications/<hook>` 扇出给全部声明成员的 handler（P27 ADR 广播
    /// 语义）。内核广播侧若按订阅插件逐发，同宿主 N 个声明成员会收到 N 次
    /// 通知、每次扇出全成员——handler 执行 N² 次（子任务完成通知 ×4 根因）。
    /// 广播侧据本键去重：同宿主订阅插件只发一次。
    /// 默认返回 plugin_id 本身 = 独占宿主语义，无宿主分组知识的实现逐发即
    /// 恰好一次，行为不变。
    fn host_key_of(&self, plugin_id: &str) -> String {
        plugin_id.to_string()
    }

    /// 强制卸载插件（热重载/崩溃恢复用）。
    ///
    /// 对 sidecar：kill 子进程 + 从客户端缓存移除，下次调用自动 respawn 加载最新代码。
    /// 对 cdylib：返回不支持错误（Windows dlclose 限制）。
    /// 默认实现返回 Ok（），供无此能力的实现（如 MockInvoker）不破坏编译。
    async fn force_unload(&self, _plugin_id: &str) -> Result<(), PluginError> {
        Ok(())
    }

    /// 成员粒度热重载（合宿成员在共享宿主进程内重载单插件代码）。
    ///
    /// 仅对「已装箱到合宿组宿主且有存活进程」的成员生效：向宿主发
    /// `agentos/reload_member` 请求（带应答）重载该成员并刷新宿主指纹记账，
    /// 其余成员进程不动（驱逐爆炸半径从整组收缩到单成员）。无存活宿主
    /// （未装箱/未 spawn）= 下次 spawn 必用新码，返回 Ok(no-op)；独占插件、
    /// 实现不支持、请求失败（宿主协议错误/超时）返回 Err——调用方（watcher）
    /// 必须回退 [`Self::force_unload`] 整组驱逐。
    /// 默认实现返回不支持错误（无此能力的实现 / MockInvoker 编译兼容）。
    async fn reload_member(&self, _plugin_id: &str) -> Result<(), PluginError> {
        Err(PluginError {
            message: "reload_member not supported by this invoker".into(),
            code: None,
            source: None,
        })
    }

    /// 重新扫描插件目录，发现新增插件（运行时懒加载入口）。
    ///
    /// 重扫 plugin roots（幂等：loader 内部 cache.clear + 重插，不杀已 spawn 的进程），
    /// 返回本次新发现的 manifest 列表（供调用方注册 tools 到 capability_registry）。
    /// 默认实现返回空（无 discover 能力），供 MockInvoker 不破坏编译。
    async fn discover_new_plugins(&self) -> Result<Vec<PluginManifest>, PluginError> {
        Ok(Vec::new())
    }

    /// 拉取插件实际上报的工具清单（G2 双写一致性校验的"实际"侧）。
    ///
    /// sidecar：spawn/复用 MCP 连接 → `tools/list`，返回**原始 JSON**
    /// （`{tools: [{name, description, inputSchema}]}`）——core 不依赖 invoker
    /// 的具体类型，解析/对照由调用方用 `agentos_invoker::verify` 完成。
    /// 若本次是新 spawn 的连接，校验后回收（kill）——安装期校验不破坏懒加载。
    /// 默认实现返回不支持错误（无此能力 / 未实现 host），供 MockInvoker 不破坏编译。
    async fn list_plugin_tools(&self, _plugin_id: &str) -> Result<serde_json::Value, PluginError> {
        Err(PluginError {
            message: "list_plugin_tools not supported by this invoker".into(),
            code: None,
            source: None,
        })
    }

    /// 内核停机：best-effort 杀掉全部已缓存 sidecar（drain 客户端缓存 + 逐个
    /// kill，防孤儿进程）。
    ///
    /// 供 graceful shutdown（Ctrl-C / SIGTERM）与 exit 75 排空路径在进程退出前
    /// 调用（0.2 收尾 §3.3a）。best-effort 语义：单个 kill 失败只记日志不阻断
    /// 其余。默认实现 no-op（无 sidecar 缓存的实现 / MockInvoker 不破坏编译）。
    async fn shutdown_all(&self) {}

    /// 禁用插件的窄口 kill：若该插件有已缓存 sidecar 则 kill 并移除，无则 no-op。
    ///
    /// 语义刻意**轻于** [`Self::force_unload`]：不做 OnUnload 事件广播、不做
    /// loader.unload / 指纹清理（"仅禁用"下插件仍在 loader 内、热发现不失效）；
    /// sidecar 按调用懒 spawn，reenable 后下次调用自然重生（0.2 收尾 §3.3b）。
    /// 默认实现 no-op。
    async fn kill_sidecar_if_any(&self, _plugin_id: &str) {}
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
    /// 通用域事件通道（一次性枚举扩展点）：具体事件名放 [`HookContext`] 的
    /// `event` 标签（如 "session.created" / "session.deleted" /
    /// "session.active_changed"）。此后新增域事件类型不再改本枚举——发射点
    /// 在内核锚点（或终局的 session_manager 插件内），订阅侧走 manifest
    /// `capabilities.lifecycle_hooks` 既有注册制。
    DomainEvent,
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
/// 管理的能力维度：
/// 1. **Tools**: 工具插件/系统插件提供的工具（供 LLM 选择和调用）
/// 2. **RouteSignals**: 管道插件声明的路由信号
/// 3. **HttpRoutes**: 插件贡献的 HTTP 端点（ADR §3.3）
///
/// （原 Resources 维度已删除：manifest 与注册链全链无消费方。旧 manifest 的
/// `capabilities.resources` 字段因 serde 默认忽略未知字段仍可解析，不受影响。）
///
/// 单条内核注册的 RAII 撤销句柄（M1 PluginScope + RegistrationGuard）。
///
/// 内核每个注册面（工具/路由信号/HTTP 路由/hooks 订阅/widget 绑定…）在注册时
/// 返回一个 guard，guard drop 即精确注销该条注册；guard 也可登记进 per-plugin 的
/// PluginScope，插件禁用/卸载时一次性结构性收回（见 plugin-loader::registry）。
///
/// 撤销动作持弱引用语义（由构造方决定），注册表本身先行 drop 时 revoke 静默 no-op。
pub struct RegistrationGuard {
    revoke: Option<Box<dyn FnOnce() + Send + Sync>>,
}

impl RegistrationGuard {
    /// 用撤销闭包构造 guard。闭包必须是幂等的（Drop 与显式 revoke 可能竞争重复调用）。
    pub fn new(revoke: impl FnOnce() + Send + Sync + 'static) -> Self {
        Self {
            revoke: Some(Box::new(revoke)),
        }
    }

    /// 放弃撤销（注册所有权转交他人、或内核停机整体清算时避免逐条重复注销）。
    pub fn disarm(mut self) {
        self.revoke = None;
    }
}

impl Drop for RegistrationGuard {
    fn drop(&mut self) {
        if let Some(revoke) = self.revoke.take() {
            revoke();
        }
    }
}

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

    /// 注册管道插件的路由信号声明。
    fn register_route_signals(&self, plugin_id: &str, signals: Vec<RouteType>);

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
    /// 透传的 UI 声明（如 `chat_card`），由 manifest `capabilities.tools[].ui` 提供。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ui: Option<serde_json::Value>,
    /// 渲染意图声明（对齐 DSH ToolResultView 词汇表）：`{card: "terminal"|"diff"|
    /// "read"|"web"|"search"|"generic", ...绑定}`。工具结果按此路由到前端渲染
    /// 组件；未声明时回退现有 chat_card/推理级联。由 manifest
    /// `capabilities.tools[].render` 提供（task_dsh_plugin_adapter 任务 1）。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub render: Option<serde_json::Value>,
}

// Resource 描述符与 resources 能力维度已删除（全链无消费方）。
// 旧 manifest 的 `capabilities.resources` 条目由 serde 默认忽略未知字段兜底。

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

    /// 读取已发现（discover）插件的 manifest。
    ///
    /// 供内核同步查询插件声明的运行时属性（如 lifecycle 空闲卸载阈值），
    /// 无需再走 async load。默认返回 None，由持 manifest 缓存的实现覆盖。
    fn get_manifest(&self, _plugin_id: &str) -> Option<PluginManifest> {
        None
    }
}

/// 插件生命周期策略（manifest 可声明，覆盖内核默认）。
///
/// 用于让每个插件自定义空闲软卸载等行为，而非全局一个默认值。
/// 典型场景：`human_interaction` 等工具会长时间阻塞等待用户输入，
/// 声明 `idle_timeout_secs: 0`（永不空闲卸载）避免被 GC 在响应到达前回收。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PluginLifecycle {
    /// 空闲软卸载阈值（秒）。
    /// - `None`：用内核默认（300s）或环境变量覆盖。
    /// - `Some(0)`：永不空闲卸载（适用于会长时间阻塞的交互类插件）。
    /// - `Some(n)`：n 秒空闲后软卸载。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub idle_timeout_secs: Option<u64>,
}

/// 插件 state 声明（P1-4 声明化）。
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct StateDeclaration {
    /// 本插件引入的 per-run 易变 state 键（checkpoint 瘦身与恢复合并跳过）。
    /// 内核收集全部插件声明并集，与内核自有运行域键（engine VOLATILE_RUN_KEYS
    /// 内置部分）一同参与剥离——新增 per-run 键零内核改动，与 persistent_fields
    /// 声明化对称。未声明 = 无插件易变键。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub volatile_keys: Vec<String>,
    /// state 读面声明（切片投喂的声明位）：插件执行需要内核投喂的 state 切片。
    /// 条目三种合法形态（文档约定）：键名（如 `task.status`）/ `messages`（全量）/
    /// `messages_tail:N`（最近 N 条，N 为正整数；装载期格式校验，非法条目
    /// 告警忽略）。当前仅 schema 收录与装载校验——投喂消费由后续任务接入。
    /// 空 = 无读面声明。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub reads: Vec<String>,
}

/// 已解析的插件 Manifest（运行时表示）。
///
/// **ADR ⑦ 新增**：`requires_content` 字段声明插件需要的最近消息条数，
/// 引擎据此从 blobs 表按需加载消息内容。
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PluginManifest {
    pub id: String,
    pub name: String,
    /// 插件人读描述。顶层 `description` 是 manifest 真字段（serde 不静默丢弃）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    pub version: String,
    pub plugin_type: PluginType,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pipeline_role: Option<PipelineRole>,
    pub language: String,
    /// 宿主类型——所有插件均支持 InProcess 和 Sidecar（ADR ⑧）
    pub host_type: HostType,
    /// 宿主分组声明（合宿进程模型 §4.1，多组扩展）。
    ///
    /// 声明合法组名（`[A-Za-z0-9_-]{1,32}`）即准入合宿：多插件共享宿主进程，
    /// 宿主键 `group:{组名}:{n}`，由 invoker 运行时按组名分域动态装箱——
    /// 驱逐连坐半径 = 本组宿主，稳定观察面（如 `light_stable`）与易变工具组
    /// （`light`）互不波及。缺省 = 独占宿主（宿主键 `plugin:{plugin_id}`，
    /// 现状语义，默认保守）。白名单制：声明组名即插件作者担保
    /// "无同步阻塞调用、无 C 扩展、无重依赖"，内核不做推断；非法组名一律独占。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub host_group: Option<String>,
    pub entry: String,
    pub capabilities: ManifestCapabilities,
    /// 服务依赖（插件↔插件唯一耦合轴）。
    /// 条目 `ns`（需要该能力角色任意方法已注册）或 `ns.method`（需要该具体服务端点
    /// 已注册）；注册表把条目映射到提供者插件，消费者**不点名插件 id**。依赖只按
    /// 服务能力角色声明，详见
    /// docs/decisions/2026-08-18-plugin-dependency-package.md。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub requires_services: Vec<String>,
    #[serde(default)]
    pub permissions: ManifestPermissions,
    #[serde(default = "default_priority")]
    pub priority: u32,
    /// 分层持久化：插件声明需持久化的 state 标量字段（累计型，如 track.total_tokens）。
    /// 引擎 merge state_updates 时，对在此集合内的 key 走 upsert_state_field 投影。
    /// messages 是系统字段（引擎固定投影），不在此列。空 = 该插件无累计字段需持久化。
    #[serde(default)]
    pub persistent_fields: Vec<String>,
    /// state 声明（P1-4）：本插件的 per-run 易变键（`state.volatile_keys`）。
    /// 内核收集并集参与 checkpoint 瘦身与恢复合并跳过，与 persistent_fields
    /// 声明化对称。None = 无 state 声明。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub state: Option<StateDeclaration>,
    /// 工具面强制注入声明（P1-5）：本插件要求无视 agent `tool_ids` 白名单、
    /// 只要注册即注入 LLM 工具面的工具 id 集合（如 spill_guard 声明
    /// `["spill_retrieve"]`——大输出取回兜底链路）。内核收集全部插件声明
    /// 并集参与 tool-surface 过滤；空 = 无强制注入要求。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub force_include_tools: Vec<String>,
    /// state 出口声明：插件声明允许经 state 摘要出口（GET /pipelines/state 与
    /// pipeline-state.list）的 state 字段；支持 `前缀.*` 通配（如 `task.owned.*`）。
    /// 内核结构基线键（pipeline_id/session_id 等引擎/出生契约自有键）不在此列，
    /// 由内核基线出口。未声明且不在基线 = 不出口（默认拒绝）。空 = 无出口字段。
    #[serde(default)]
    pub export_fields: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mcp: Option<McpConfig>,
    /// 生命周期策略（空闲卸载阈值等）。`None` = 内核默认。插件可声明
    /// `lifecycle.idle_timeout_secs` 覆盖默认（如交互类插件设 0 = 永不卸载）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub lifecycle: Option<PluginLifecycle>,
    /// 原生插件产物（task_11：HostType::InProcess 必填）。
    ///
    /// 指向 cdylib 编译产物，loader 用 libloading 加载并取 `invoke_entry`
    /// 指定的 C-ABI 符号（默认 `plugin_execute`）。仅 host_type==InProcess 时有意义。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub native: Option<NativeArtifact>,
    /// 能力信封（G6/G3：全轨统一授权面）。
    ///
    /// 插件经反向 capability 调用内核时可调用的能力白名单
    /// （如 `["config-reader", "tool-executor"]`）。空 = 未声明，
    /// 按向后兼容默认全授予（存量插件零迁移）；一旦声明非空即白名单制，
    /// 越权调用在 CapabilityRouter 分发前单点拒绝（G6）。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub granted_capabilities: Vec<String>,
    /// 内容懒加载声明（ADR ⑦）。
    ///
    /// 声明插件需要多少条最近消息的完整内容。
    /// 引擎据此从 blobs 表预加载，插件也可通过 ContentLoader 运行时按需加载。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub requires_content: Option<u32>,
    /// sidecar 的 MCP 入口方法名（ADR 附录 D②，P6 命名治理）。
    ///
    /// 仅 pipeline/system 等**非 tool 插件**需要——这是管道引擎/内核 RPC
    /// 调用 sidecar 的入口（如 `llm_core.execute`），与"给 LLM 的真工具"
    /// （tool 类型插件的 `capabilities.tools[]`）分离。
    /// tool 类型插件不用此字段。
    ///
    /// pipeline 类型插件必填：discover 启动期聚合校验，缺失则启动失败
    /// （一次列出所有缺失项，不逐个 panic，见 ADR D.5/E.11）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub invoke_entry: Option<String>,
    /// 配置文件显式映射（ADR §4.2/§4.3 config_files）。
    ///
    /// 每项把一个配置文件（id/path/label）显式映射到现有 `config/` 子树下的文件。
    /// 内核 loader 据此构造注入给插件的配置（按 id 命名空间合并，B3）。
    /// 未声明 config_files 的插件收空配置（13 处 plugins 直读 config_center
    /// 有自己的兜底，见 P1-7 DEBT）。
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
    /// 启用开关（L1 Enabled，安装触发模型 §一）。
    ///
    /// `false` = 已安装但不启用：不进注册表出口（tools/http_routes/contributes 不暴露）。
    /// 缺省时由 `config/kernel/default_profile.yaml` 决定（未列出走 defaults）。
    /// 这是「运行时是否允许参与系统」的开关，与 PluginStatus（运行态）正交。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub enabled: Option<bool>,
    /// 激活策略（L2，安装触发模型 §5.2）。
    ///
    /// `eager` = 内核启动后即 load → Active；`lazy` = 首次 invoke 再 load（默认）；
    /// `manual` = 仅用户显式启动。缺省时由 default_profile 决定。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub activation: Option<ActivationPolicy>,
    /// 插件向内核反向调用通道贡献的 capability（M4 插件自注册能力）。
    ///
    /// 声明本插件提供一个或多个 capability namespace，其他插件（或本插件自身）
    /// 可通过 sidecar 反向调用消费。loader 扫描时把每条记录注册进
    /// `CapabilityHandlerRegistry`，使该 namespace 自动出现在：
    /// - 反向调用白名单（`parse_capability_method_with` 的动态 namespace）；
    /// - initialize 握手声明（sidecar SDK 据此创建 CapabilityHandle）；
    /// - 路由表（`CapabilityHandlerRegistry::route`）。
    ///
    /// 典型用例：交互插件声明 provides 交互 namespace，
    /// 另一个 tool 插件通过 `get_capability(<交互 namespace>)` 反向调用，
    /// 状态留在主进程唯一一份 service 上，链路闭合。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provides: Option<ProvidesCapabilities>,
}

/// 插件贡献的反向调用 capability 声明（M4）。
///
/// 一个插件可贡献多个 namespace，每个 namespace 由 `host` 决定内核如何路由：
/// - `InProcess`：插件代码跑在主进程，handler 持有插件对象引用直接调用；
/// - `Sidecar`：插件是独立进程，handler 把请求转发到该插件的 MCP 连接。
///
/// 注意：纯数据结构，不引用 mcp crate 的 trait（避免 core→mcp 循环依赖）。
/// handler 的桥接逻辑在 plugin-loader（它能同时依赖 core 和 mcp）。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ProvidesCapabilities {
    /// 该插件贡献的全部 capability namespace 声明。
    pub capabilities: Vec<ProvidedCapability>,
}

/// 内核协议面服务角色（provides 声明驱动路由锚点，P1-3）。
///
/// 内核协议端点（如 /api/v1/interaction/response 与 WS interaction_response）
/// 按**角色**查找提供者，不点名 namespace——任意插件声明同角色即接管端点后端
/// （热替换语义与 namespace 一致）。method 须同时出现在该 capability 的
/// methods 清单内（ProvidedCapabilityHandler 白名单校验）。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ProtocolRole {
    /// 角色名（kebab-case）。现有角色：`interaction-respond`（交互应答端点后端）。
    pub role: String,
    /// 该角色绑定的 method（如 `respond`）。
    pub method: String,
}

/// 单个 capability namespace 贡献声明。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ProvidedCapability {
    /// capability namespace（如交互插件的 `interaction-respond` 承载 namespace），全内核唯一。
    /// 多个插件声明同一 namespace 时，loader 按插件 priority 决定胜出者
    ///（与 http_endpoints 的冲突检测不同——capability 允许热替换）。
    pub namespace: String,
    /// 该 namespace 支持的 method 清单（如 `["create_choice", "wait_for_choice"]`）。
    /// 用于自描述/校验，loader 注册 handler 时可据此拒绝未声明的 method。
    pub methods: Vec<String>,
    /// 路由方式——决定内核怎么找到真正的 handler 实现。
    #[serde(default)]
    pub host: ProvidedCapabilityHost,
    /// sidecar 工具名前缀（host=sidecar 时生效）。
    ///
    /// McpBridge 把 `<namespace>.<method>` 映射成 `<tool_prefix>.<method>`
    /// 调 invoker.invoke_tool。例如 namespace 连字符转下划线缺省派生工具前缀，
    /// `<ns>.create_choice` → `<ns_derived>.create_choice`。
    ///
    /// 缺省时从 namespace 派生（连字符转下划线）。工具名前缀与 namespace 不一致时必须显式声明，
    /// 否则 McpBridge 路由会拼出错误的工具名。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_prefix: Option<String>,
    /// 本 namespace 承载的内核协议面角色（P1-3 声明化）。
    /// 空 = 不承载内核协议端点（纯插件间能力）。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub protocol_roles: Vec<ProtocolRole>,
}

/// capability 贡献的路由方式。
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "kebab-case")]
pub enum ProvidedCapabilityHost {
    /// 插件代码跑在主进程（如 Python in-process 插件、内置 system service），
    /// handler 持有插件对象引用直接调用。
    #[default]
    InProcess,
    /// 插件是独立 sidecar 进程，handler 把请求转发到该插件的 MCP 连接。
    Sidecar,
}

/// 插件激活策略（安装触发模型 §5.2）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum ActivationPolicy {
    /// 内核启动后即 load → Active（Tier S 骨架、已启用的 webhook 通道）
    Eager,
    /// 首次 invoke / 首次命中路由再 load（默认，多数 tool/评估/监控）
    #[default]
    Lazy,
    /// 仅用户点「启动」或 API 显式 load（重型/调试插件）
    Manual,
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
    /// 鉴权模式（D2 第一刀起由内核 dispatcher 执行，语义见上模块注释与
    /// `deserialize_http_auth`）：
    /// - `Some("user")`：与 /api/v1 同一 token 校验（resolve_request_user）；
    /// - `Some("admin")`：同上 + admin 角色；
    /// - `Some("none")`：显式匿名白名单（webhook 验签自管等），全方法放行；
    /// - `None`（manifest 缺省）：无声明——写面（非 GET）默认拒绝 401
    ///   （fail-closed），读面（GET）放行 + warn 计数（第二刀收紧的数据源）。
    #[serde(default, deserialize_with = "deserialize_http_auth")]
    pub auth: Option<String>,
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

/// 校验 HttpEndpoint.auth 仅接受 none/user/admin（ADR §3.3 auth 枚举）；
/// 字段缺省反序列化为 `None`（无声明，dispatcher 按 fail-closed 语义处理）。
fn deserialize_http_auth<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let raw = match Option::<String>::deserialize(deserializer)? {
        None => return Ok(None),
        Some(raw) => raw,
    };
    match raw.as_str() {
        "none" | "user" | "admin" => Ok(Some(raw)),
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
    /// 查询参数（key → value，单值 last-wins 形态；兼容旧插件的 `query.get(k)` 消费）。
    pub query: HashMap<String, String>,
    /// 查询参数多值形态（key → 全量 value 列表，保持出现顺序）。
    ///
    /// 多值语义：重复 key（如 `filter=a&filter=b`）的全量值经本字段透传给插件；
    /// 单值 `query` 为 last-wins 投影（`query[k] == query_multi[k].last()`）。
    ///
    /// 向后兼容：`#[serde(default)]`——旧序列化负载（无此字段）反序列化得空 map；
    /// SDK 侧按 handler 签名过滤 kwargs，不声明本字段的插件不受影响。
    #[serde(default)]
    pub query_multi: HashMap<String, Vec<String>>,
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
/// **ADR ⑧**：所有插件（含工具插件、系统插件）均支持多种执行路径，
/// 由开发者根据性能需求自行选择，不因插件类型限制可选路径：
/// - `InProcess`：Rust 原生 cdylib 进程内调用（libloading + C-ABI），零 IPC 开销，适合高频热路径
/// - `Sidecar`：独立进程通过 MCP 协议通信，进程隔离，适合低频或第三方（Python）插件
///
/// （`wasm` 轨已关闭——见
/// `docs/decisions/2026-08-15-plugin-two-track-and-cordis-mechanisms.md` §八.1；
/// 决策标注可逆，wasm 是 S1 受阻时的回退位。）
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum HostType {
    /// Rust 原生 cdylib 进程内调用（libloading，零 IPC 开销）
    InProcess,
    /// 独立进程通过 MCP 协议通信
    #[default]
    Sidecar,
}

/// 原生插件产物描述（task_11：HostType::InProcess 的 manifest 字段）。
///
/// `artifact` 指向编译产物（cdylib：`.dll`/`.so`/`.dylib`），
/// loader 用 `libloading::Library::new` 加载并取 `invoke_entry` 符号。
///
/// 路径相对插件目录（与 manifest 同级目录）。
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct NativeArtifact {
    /// cdylib 文件名（相对插件目录），如 `my_plugin.dll` / `libmy_plugin.so`。
    pub artifact: String,
}

/// Manifest 能力声明（运行时表示）。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ManifestCapabilities {
    /// 给 LLM 的工具声明——**声明即注册**（D.6 槽位拆分）：
    /// 不按 plugin_type 门控，任何类型插件声明 tools 即进
    /// CapabilityRegistry 暴露给 LLM 面。多职能插件
    /// （system 适配器等）自然成立：声明 tools + services 两块即可。
    #[serde(default)]
    pub tools: Vec<ToolCapability>,
    /// 内核/插件间服务方法声明（D.6 槽位拆分）：不进 LLM 面；
    /// 调用走既有通道——invoke_entry（管道）、
    /// http_endpoints（面板）、tool-executor 显式 plugin_id（跨插件）、
    /// provides 命名空间。wire 协议不变（仍是 MCP tools/call，ADR D.3）。
    #[serde(default)]
    pub services: Vec<ServiceCapability>,
    /// 管道步骤服务声明（管道步骤服务化提案 2026-08-27 §3.1）：
    /// 管道步骤成为显式注册的服务（capabilities.steps），引用与实现解耦
    /// （G10 第③级命中 → 已注册步骤服务）。与 tools/services 并存合法
    /// （多能力合一插件的正规形态）。step 名全局唯一（编译期查重，冲突
    /// 启动报错）；不含 phase/orchestration 类字段（被否方案 #1：编排位置
    /// 属管道配置，manifest 只声明存在性与签名）。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub steps: Vec<StepCapability>,
    /// `resources` 能力声明已删除（全链无消费方）；serde 默认忽略未知字段，
    /// 旧 manifest 里的 `capabilities.resources` 条目不影响解析。
    #[serde(default)]
    pub route_signals: Vec<RouteType>,
    #[serde(default)]
    pub lifecycle_hooks: Vec<LifecycleHook>,
    /// 流式事件能力声明（ADR 2026-08-22 流式协议）：插件按
    /// `config/kernel/kernel_capabilities/streaming.json` 发射流式事件（message_id 强制
    /// p_ 命名空间，事件/part_types 声明供 G2 校验器对照实现）。未声明 = 网关
    /// 拒绝其流式事件（fail-closed），与当年 resources 的「声明即接入」同构。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub streaming: Option<StreamingCapability>,
}

/// 流式能力声明（capabilities.streaming，文档见 docs/guides/streaming-protocol.md）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StreamingCapability {
    /// 引擎管道器官声明（P1-2 声明化）：本插件是内核 LLM 路径的执行器官，
    /// 内核经 engine state 下发签发的 a_ message_id（回环消费）。
    /// 声明 true 豁免"capabilities.streaming 须声明 events 清单"的发射闸，
    /// 且 message_id 命名空间执法按 kernel owner（a_）而非 plugin owner（p_）。
    /// 缺省 false = 普通流式插件（fail-closed：未声明此位的插件不豁免）。
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub conduit: bool,
    /// 插件实际发射的事件类型清单（缺省 = streaming.json 契约的全部事件均可发射）。
    /// G2 校验器对照插件实现实际发射的事件做一致性检查。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub events: Option<Vec<String>>,
    /// 插件自定义渲染的 part 类型（前端按注册表分发，见协议 §8）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub part_types: Option<Vec<String>>,
    /// 插件事件的默认持久化语义（缺省 false = 纯展示，只进 store 刷新弃）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub persist: Option<bool>,
}

/// 服务方法声明（D.6 槽位拆分）。结构是 ToolCapability 的子集（迁移期
/// manifest 条目只搬 name/description），语义是"内部服务入口"——不注册
/// 进 LLM 面。2026-08-18 契约定型：`input_schema`（必填，auto-backfill 自
/// 提供方实际 MCP 工具 schema）+ `output_schema`（提供方声明了才填）——
/// 服务由谁提供就在谁的 plugin.json 补，G2 据此比对"插件↔服务"调用形状。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServiceCapability {
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    /// 服务入参形状（JSON Schema）。由提供方 auto-backfill（spawn 其 sidecar 拉
    /// `tools/list` 的真实 schema 写回）；`None` = 未声明（存量未补前容忍，G2
    /// 只对"声明了"的比对，与工具通道一致）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub input_schema: Option<serde_json::Value>,
    /// 服务出参形状（提供方在代码里声明了才填，不伪造）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output_schema: Option<serde_json::Value>,
}

/// 管道步骤服务声明（capabilities.steps，管道步骤服务化提案 2026-08-27 §3.1）。
///
/// 结构是 ServiceCapability 的前三字段（name/description/input_schema）——
/// 语义为"管道步骤服务"：G10 第③级按 name 命中后调用
/// `{plugin_id, method}` 对应实现（sidecar 经 ctx 约定字段分发）。
/// 与 execute 返回契约相同（state/config 入参出参）。不含 phase/orchestration
/// 字段（被否方案 #1）——编排位置属管道配置。
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct StepCapability {
    /// 全局唯一步骤名（建议 `<域>.<动作>` 命名空间化；无点号裸名预留兼容层）。
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    /// per-step inputs 契约（可选；None = 未声明）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub input_schema: Option<serde_json::Value>,
}

/// 配置文件映射项（ADR §4.2/§4.3 `config_files[]`）。
///
/// 把一个现有 `config/` 子树下的文件显式映射给插件。每项三要素：
/// - `id`：该配置子项的标识（插件内唯一，作为注入命名空间 key 与 API file_id）；
/// - `path`：相对 `config/` 根的路径（如 `config/models/llm.yaml`）；
/// - `label`：前端展示用的名称。
///
/// 单一真值形态（2026-09-02 用户裁定，G2 校验执法）：配置真值全系统只有一份——
/// - **引用形态**（`path` 非空）：真值在 `path` 指向的文件，`fields` 仅表单
///   schema，**禁止声明 `default`**（manifest 默认值 + 文件值并存 = 双真值错误）；
/// - **内联形态**（`path` 省略）：真值即 `fields.default`（存在 manifest），
///   PUT 保存直接写回 manifest，不落独立配置文件。
///
/// path 安全校验见 loader 的 B1 实现（归一化 + 落 config/ 子树 + denylist）。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ConfigFileMapping {
    /// 配置子项标识（插件内唯一）。
    pub id: String,
    /// 是否在设置中枢展示为可编辑配置面板（缺省 true）。
    ///
    /// `false` = 注入专用：条目仍参与 sidecar 配置注入（B3）与
    /// `/api/v1/plugins/{id}/config/{file_id}` 读写，但不出口到
    /// `/api/v1/schema` 的 plugin_configs 与插件列表的 config_files
    /// （该文件的 UI 由插件声明的 settings 页/widget 承载，避免双入口）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub settings: Option<bool>,
    /// 相对 config/ 根的文件路径（含 config/ 前缀或相对形式均可，loader 归一化）。
    ///
    /// 省略（空串）= 内联形态：配置真值即 `fields.default`（存在 manifest，
    /// 保存经 PUT 直接写回 manifest）；非空 = 引用形态：真值在文件，`fields`
    /// 仅表单 schema（禁声明 `default`，G2 校验拦截双真值）。
    ///
    /// `target: "env"` 的条目例外：path 不指向 config/ 子树，而表示项目根
    /// `.env`（GAP-4 外部 MCP 源 key 的声明驱动配置入口）；env 条目必须
    /// 声明 path（写入目标语义不可省）。
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub path: String,
    /// 前端展示名称。
    pub label: String,
    /// 写入目标（GAP-4）：`"env"` = 字段写进项目 `.env`（key/加密字段），
    /// 缺省 = 写进 path 指向的插件配置文件（既有 YAML 语义）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target: Option<String>,
    /// 字段级声明：`target: "env"` 时为 env 密钥表单（`name` 对应
    /// `mcp.endpoint.auth.value` / `env` 里的 `${VAR}` 引用；未手写声明的
    /// 引用由内核在装载期自动生成条目，见 loader 的
    /// auto_generate_env_declarations，引用本身即声明）；YAML target 时为
    /// 类型化表单声明（UI 词汇表经 `EnvConfigField::extra` 透传，前端 RJSF
    /// 表单消费，内核不解释）。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub fields: Vec<EnvConfigField>,
}

/// config_files 条目的字段级声明（env 密钥表单 GAP-4 + YAML 类型化表单）。
///
/// env target：字段名 = .env 键。YAML target：字段 name 支持点号路径（如
/// `defaults.chat`），`type`/UI 词汇（options/min/max/step/default…）由前端
/// 词汇表解释——内核只保底解析本结构体的显式字段，其余经 `extra` 原样透传
/// （谁的数据谁出表单，内核不建模 UI 词汇表）。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct EnvConfigField {
    /// 字段名（env = .env 键；YAML = 点号路径）。
    pub name: String,
    /// 前端展示名称。
    pub label: String,
    /// 字段类型：env target 下 `secret`（密码框/掩码展示）| `string`，缺省按
    /// secret 处理（保守默认——宁可掩码不可泄漏）；YAML target 下为前端表单
    /// 词汇（select/toggle/number/textarea…），内核不校验值域。
    #[serde(rename = "type", default = "default_env_field_type")]
    pub field_type: String,
    /// 是否必填（缺失时插件 connect 硬失败 vs 可选降级）。
    #[serde(default)]
    pub required: bool,
    /// 字段说明（前端提示文案，可选）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    /// UI 词汇表透传（YAML target 的表单声明：options/min/max/step/default/
    /// datasourceUri 等）——序列化时平铺进字段对象，不产生 `extra` 键。
    #[serde(flatten, default)]
    pub extra: Option<serde_json::Map<String, serde_json::Value>>,
}

fn default_env_field_type() -> String {
    "secret".to_string()
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
    /// 透传的 UI 声明（如 `chat_card`），原样出口到 ToolDescriptor。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ui: Option<serde_json::Value>,
    /// 渲染意图声明（DSH ToolResultView 词汇表：card=terminal/diff/read/web/
    /// search/generic + 字段绑定），原样出口到 ToolDescriptor，供前端按意图
    /// 路由渲染（task_dsh_plugin_adapter 任务 1）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub render: Option<serde_json::Value>,
    /// 注册闸冒烟开关（2026-08-18）：显式 `true` 的工具，注册时用样例输入真调用
    /// 一次验证"基本能力能跑"（fail-closed，调用失败拒绝该工具）。缺省不冒烟——
    /// 副作用敏感/需要真实参数的能力由插件显式声明后才会被冒烟，避免注册期误伤。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub smoke: Option<bool>,
    /// 非流式调用的总时长超时（毫秒，ADR 2026-09-07 P12）。取值优先级：
    /// 工具声明 > 插件级 `mcp.request_timeout_secs` > 内核默认
    /// [`default_capability_timeout_ms`]（300_000ms）。
    /// `Some(0)` = 显式豁免总时长超时（长等待/流式类工具；对齐
    /// `lifecycle.idle_timeout_secs: Some(0)` 的 0 哨兵惯例）。
    /// `None` = 未声明，按优先级兜底。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timeout_ms: Option<u64>,
}

/// 非流式 capability 调用的内核默认总时长超时（毫秒）：300s。
///
/// 插件未做任何时长声明（工具级 `timeout_ms` / 插件级
/// `mcp.request_timeout_secs` 均缺省）时的兜底界——悬挂调用 bounded，
/// 不再无限等待（ADR 2026-09-07 P12）。
pub fn default_capability_timeout_ms() -> u64 {
    300_000
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
    /// 外部 MCP HTTP 端点（transport=StreamableHttp 时必填）。
    ///
    /// 声明后 invoker 不 spawn 子进程，改用 HTTP 客户端连 `endpoint.url` 指定的
    /// 远程第三方 MCP server。`None` = 本地 stdio sidecar（默认）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub endpoint: Option<McpEndpoint>,
    #[serde(default = "default_idle_timeout")]
    pub idle_timeout_secs: u64,
    #[serde(default = "default_protocol_version")]
    pub protocol_version: String,
    /// 单次工具调用的响应等待超时（秒）。`None` = 内核默认 86400s（mcp 客户端
    /// `DEFAULT_REQUEST_TIMEOUT_SECS`，审批/人机交互族统一值，BUG-60）。
    ///
    /// 长等待业务（交互插件 wait_for_choice 等用户响应，业务超时 24h）与内核
    /// 默认同值；显式声明仍推荐——声明即契约，不受内核默认将来调整影响。
    /// security_check 的 SDK 侧 timeout 参数仅作提示，内核不读。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub request_timeout_secs: Option<u64>,
}

pub fn default_idle_timeout() -> u64 {
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

/// 外部 MCP 端点声明（external_mcp 插件连第三方 MCP server）。
///
/// 嵌套在 [`McpConfig::endpoint`]，由 [`McpConfig::transport`] 决定用哪组字段：
/// - `StreamableHttp`：用 `url`/`headers`/`auth`，invoker 走 HTTP 客户端，不 spawn。
/// - `Stdio`：用 `command`/`args`/`env`，invoker spawn 第三方本地命令（如 npx）。
///
/// `auth.value` 与 `env` 的值支持 `${ENV_VAR}` 占位，在 invoker 构造客户端/子进程时解析。
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct McpEndpoint {
    /// 远程 MCP server 的 HTTP(S) URL（StreamableHttp 用）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    /// 额外请求头（如 `X-Also-Search`，StreamableHttp 用）。
    #[serde(default, skip_serializing_if = "HashMap::is_empty")]
    pub headers: HashMap<String, String>,
    /// 鉴权配置（StreamableHttp 用，可选）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub auth: Option<EndpointAuth>,
    /// 本地第三方 MCP 命令（Stdio 用，如 `npx` / `python3`）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub command: Option<String>,
    /// 命令参数（Stdio 用）。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub args: Vec<String>,
    /// 子进程环境变量（Stdio 用，值支持 `${ENV_VAR}` 占位）。
    #[serde(default, skip_serializing_if = "HashMap::is_empty")]
    pub env: HashMap<String, String>,
}

/// 外部 MCP 端点的鉴权配置。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EndpointAuth {
    /// 鉴权方式。`api_key` 把 value 原样写入 header_name 指定的头；
    /// `bearer` 写入 `Authorization: Bearer <value>`。
    #[serde(rename = "type", default = "default_auth_type")]
    pub auth_type: AuthType,
    /// 承载鉴权值的请求头名（api_key 模式生效），默认 `Authorization`。
    #[serde(default = "default_auth_header_name")]
    pub header_name: String,
    /// 鉴权值，支持 `${ENV_VAR}` 占位（构造 HTTP 客户端时解析）。
    pub value: String,
    /// 鉴权凭据是否必需（GAP-4b）。缺省 `None` 按必需处理（保持既有硬失败
    /// 行为）；显式 `false` 时占位变量缺失 → 跳过该鉴权头照常连接，由
    /// 服务端 401 说话（适用于凭据可选的远端 MCP 源）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub required: Option<bool>,
}

/// 鉴权方式。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum AuthType {
    #[default]
    ApiKey,
    Bearer,
    None,
}

fn default_auth_type() -> AuthType {
    AuthType::ApiKey
}

fn default_auth_header_name() -> String {
    "Authorization".to_string()
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

/// 按 pipeline_id 查询消息的选项（游标分页）。
///
/// 对齐前端 getMessages 的查询参数（session.ts: before_sequence/after_sequence/limit）。
/// sequence 按 pipeline_id 维度连续递增，故游标在管道内全局有效。
#[derive(Debug, Clone, Default)]
pub struct MessageQueryOpts {
    /// 返回 seq_in_branch < before_sequence 的消息（向前翻页）
    pub before_sequence: Option<u32>,
    /// 返回 seq_in_branch > after_sequence 的消息（断线补漏）
    pub after_sequence: Option<u32>,
    /// 最多返回条数
    pub limit: Option<usize>,
}

/// 存储后端抽象（ADR ③④）。
///
/// SQLite 四表模型的 trait 抽象，供 ContentLoader 和 AdrEngine 使用。
/// 具体实现为 SQLite，但 trait 层不绑定具体数据库——便于测试时 mock。
///
/// **存储模型**（ADR 2026-09-18：runs/branches 退役，state 唯一真值）：
/// - `pipeline_state`：state 标量字段的唯一真值（含运行簿记键 run_status/
///   run_started_at/run_ended_at/run_config_hash/run_id/suspend_request_id）
/// - `message_slots` + `blobs`：消息 op 流（槽位 + 内容寻址）
/// - `traces`：pipeline 级 op 日志（定位 = (pipeline_id, seq)，审计/DR 回放）
/// - `pipeline_checkpoints`：纯备份（仅 state 丢失/损坏时作 DR 基线）
///
/// [来源: docs/decisions/（ADR 2026-09-18 runs 退役与 state 唯一真值）]
#[async_trait]
pub trait StorageBackend: Send + Sync {
    /// 获取运行记录（由 state 运行簿记键合成；按 run_id 反查所属管道）。
    async fn get_run(&self, run_id: &str) -> Result<RunRecord, StorageError>;

    /// 运行开始簿记：写 state 运行键（run_id/run_status='running'/run_started_at/
    /// run_config_hash）。默认 no-op（mock/null store），SqliteStore 覆盖。
    async fn record_run_start(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _run_id: &str,
        _config_hash: &str,
    ) -> Result<(), StorageError> {
        Ok(())
    }

    /// 列出某管道的运行投影（ADR 2026-09-18：每管道至多一条——当前运行）。
    /// 默认空（mock/null store），SqliteStore 覆盖。
    async fn list_runs_by_pipeline(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<RunRecord>, StorageError> {
        Ok(Vec::new())
    }

    /// 管道运行快照列表（runs × message_slots × pipeline_sessions × pipeline_run_summaries
    /// 联结，按 started_at 倒序）。调试中心「会话/执行记录」的会话维度数据源，
    /// 与 `GET /api/v1/pipelines/runs` 同查询。默认空（mock/null store），SqliteStore 覆盖。
    async fn list_pipelines(
        &self,
        _tenant_id: &str,
        _status: Option<&str>,
        _limit: u32,
    ) -> Result<Vec<PipelineRunInfo>, StorageError> {
        Ok(Vec::new())
    }

    /// 按 pipeline_id 查询历史消息（消息层自治查询主键）。
    ///
    /// 两域解耦：消息层只按 pipeline_id 查询，不关心会话（thread）归属。
    /// 对齐 0.1 `ExecutionRecordStorage.list_by_pipeline(pipeline_run_id)`。
    /// opts 支持游标分页（before_sequence/after_sequence）与 limit。
    async fn get_messages_by_pipeline(
        &self,
        pipeline_id: &str,
        opts: MessageQueryOpts,
    ) -> Result<Vec<MessageRecord>, StorageError>;

    /// 获取指定 blob_id 的原始数据。
    async fn get_blob(&self, blob_id: &str) -> Result<Vec<u8>, StorageError>;

    /// 追加一条状态变更日志到 traces 表（Append-Only，pipeline 级 op 流）。
    /// seq 由存储层写入时分配（MAX(seq)+1），entry.seq 字段值被忽略。
    async fn append_trace(&self, entry: TraceEntry) -> Result<(), StorageError>;

    /// 存储不可变原始数据到 blobs 表（内容寻址去重，blob_id = SHA256）。
    /// 返回 blob_id 供 message_slots 行引用。
    async fn store_blob(&self, data: &[u8], mime_type: &str) -> Result<String, StorageError>;

    // ── 域10：分层持久化投影（messages 增量对齐 + 标量快照 + checkpoint）────

    /// 应用身份/seq 感知的 messages ops 到 message_slots 表（op-based 新模型单写入器）。
    /// 引擎把插件 emit 的 `set/insert` op 落表（与 `apply_slot_ops_to_array` 落内存是同一组 op）。
    /// 默认 no-op（mock/null store 用），SqliteStore 覆盖为真实实现。详见 docs/message_persistence_design.md。
    async fn apply_messages_ops_to_table(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _ops: &[serde_json::Value],
    ) -> Result<(), StorageError> {
        Ok(())
    }

    /// upsert 一个 state 标量字段到 pipeline_state 表（覆盖最新值）。
    /// 仅对插件 manifest 声明的 persistent_fields 调用；累计语义由插件保证。
    async fn upsert_state_field(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _key: &str,
        _value: &serde_json::Value,
    ) -> Result<(), StorageError> {
        Ok(())
    }

    /// upsert 一批 state 标量字段到 pipeline_state 表（B6 批量接口）。
    ///
    /// 默认实现退化为逐键调用 [`Self::upsert_state_field`]（只覆盖单键的
    /// 实现者零改动）；SqliteStore 覆盖为单事务全量 upsert——任一失败整体
    /// 回滚（全成才 commit），保证调用方「DB 批量成功 → 再写内存」写序下
    /// 无「DB 半套」部分写入面。既有单键接口保持不动（其他调用点不改）。
    async fn upsert_state_fields(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        fields: &serde_json::Map<String, serde_json::Value>,
    ) -> Result<(), StorageError> {
        for (key, value) in fields {
            self.upsert_state_field(pipeline_id, tenant_id, key, value)
                .await?;
        }
        Ok(())
    }

    /// 读出某 pipeline 全部持久化标量字段（冷启动重建喂回 state 用）。
    async fn load_pipeline_state(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
    ) -> Result<std::collections::HashMap<String, serde_json::Value>, StorageError> {
        Ok(std::collections::HashMap::new())
    }

    /// 注入插件声明的 per-run 易变键并集（P1-4 声明化）：checkpoint 落档剥离集 =
    /// 内核自有键（store 内置）∪ 本集。生产侧由 api 从全部插件 manifest
    /// `state.volatile_keys` 声明收集并集后注入，与恢复合并侧消费同一并集。
    /// 缺省空集（不剥任何声明键）；后写覆盖（manifest 热重载后随下一次派发刷新）。
    fn set_declared_volatile_keys(&self, _keys: &[String]) {}

    /// 保存全量 state checkpoint（每 N 步留档，O(1) 重建基线）。
    async fn save_checkpoint(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _step_no: i64,
        _state: &serde_json::Value,
    ) -> Result<(), StorageError> {
        Ok(())
    }

    /// 取最近 checkpoint（step_no 最大），返回 (step_no, state)。
    async fn load_latest_checkpoint(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
    ) -> Result<Option<(i64, serde_json::Value)>, StorageError> {
        Ok(None)
    }

    /// 枚举租户内带持久化 state（pipeline_state 标量 / checkpoint 二者有其一）的管道
    /// `(pipeline_id, thread_id)`。冷读路径（pipeline-state.list / /pipelines/state 的
    /// DB 兜底）据此列 registry 未覆盖的管道——thread_id 以 pipeline_sessions 为准，
    /// 缺省回退 pipeline_id（任务管道 thread_id 恒等于自身 pipeline_id）。
    async fn list_state_pipeline_ids(
        &self,
        _tenant_id: &str,
    ) -> Result<Vec<(String, String)>, StorageError> {
        Ok(vec![])
    }

    /// 冷启动历史读路径：从 message_slots（消息队列持久真值）join blobs 重建完整
    /// 消息对象数组（元素自带稳定 seq），零回放。
    async fn load_message_history(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<serde_json::Value>, StorageError> {
        Ok(vec![])
    }

    // ── 域12：消息段（替换事件的冻结内容，多代切换/压缩原文存档）─────────

    /// 列出某管道的消息段（不含成员、含 preview，按 base_seq, created_at 升序）。
    /// 默认空（mock/null store），SqliteStore 覆盖。
    async fn list_message_segments(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<SegmentRecord>, StorageError> {
        Ok(Vec::new())
    }

    /// 取单个消息段（含成员全文解析：members_blob 引用列表 → 逐成员 blob 重建）。
    /// 不存在 → None。默认 None（mock/null store），SqliteStore 覆盖。
    async fn get_message_segment(
        &self,
        _segment_id: &str,
        _tenant_id: &str,
    ) -> Result<Option<SegmentRecord>, StorageError> {
        Ok(None)
    }

    // ── 域11：pending 输入队列（ADR-2026-08-26）─────────────────────
    // 消息在"入队→激活"之间停留在表中，等待窗口内可修改/删除/清空；
    // 消费任务从表取参数执行（内容不被闭包捕获）。默认 no-op（mock/null store），
    // SqliteStore 覆盖为真实实现。

    /// 入队一条 pending 输入（created_at 即 FIFO 序）。幂等：同 id 重复入队忽略。
    async fn enqueue_pending_input(
        &self,
        _tenant_id: &str,
        _pipeline_id: &str,
        _input: &PendingInputRecord,
    ) -> Result<(), StorageError> {
        Ok(())
    }

    /// 取队首（FIFO：created_at, id 升序）第一条 pending 输入。None = 队列空。
    async fn pop_pending_input(
        &self,
        _tenant_id: &str,
        _pipeline_id: &str,
    ) -> Result<Option<PendingInputRecord>, StorageError> {
        Ok(None)
    }

    /// 列出某管道全部 pending 条目（按 FIFO 序）。
    async fn list_pending_inputs(
        &self,
        _tenant_id: &str,
        _pipeline_id: &str,
    ) -> Result<Vec<PendingInputRecord>, StorageError> {
        Ok(Vec::new())
    }

    /// 修改 pending 条目 content（不存在 → Ok(false)）。
    async fn update_pending_input_content(
        &self,
        _tenant_id: &str,
        _pipeline_id: &str,
        _input_id: &str,
        _new_content: &str,
    ) -> Result<bool, StorageError> {
        Ok(false)
    }

    /// 删除单条 pending 输入（不存在 → Ok(false)）。
    async fn delete_pending_input(
        &self,
        _tenant_id: &str,
        _pipeline_id: &str,
        _input_id: &str,
    ) -> Result<bool, StorageError> {
        Ok(false)
    }

    /// 清空管道全部 pending 输入，返回删除条数。
    async fn clear_pending_inputs(
        &self,
        _tenant_id: &str,
        _pipeline_id: &str,
    ) -> Result<usize, StorageError> {
        Ok(0)
    }

    // 域2：session 标签夹（对齐 0.1 SessionModel）─────────────────────
    // 解耦：session 只持 pipeline_ids 引用列表，不反向 join messages。

    /// 创建会话（对齐 0.1 SessionModel + MemoryStore.set_session）。
    async fn create_session(&self, session: &SessionRecord) -> Result<(), StorageError>;

    /// 按 thread_id 取单个会话（含 pipeline_ids）。
    async fn get_session(&self, thread_id: &str) -> Result<Option<SessionRecord>, StorageError>;

    /// 列会话（按 filter 过滤，对齐 0.1 list_threads）。
    async fn list_sessions(
        &self,
        filter: SessionListFilter,
    ) -> Result<Vec<SessionRecord>, StorageError>;

    /// 更新会话（upsert：存在则更新，不存在则插入）。
    /// 用于 create_thread 后追加 pipeline_id、更新 title/agent 等。
    async fn update_session(&self, session: &SessionRecord) -> Result<(), StorageError>;

    /// 删除会话并级联清理其全部关联数据（主管道 + 子任务管道的 messages/traces/runs/state）。
    /// 通过 pipeline_sessions 映射表按 thread_id 定位全部 pipeline_id，再沿血缘
    /// lineage.parent_pipeline_id 收集后代任务管道（子任务出生落自环绑定
    /// thread=自身 id，不在会话清单里），单次事务级联删除。
    /// 返回实际删除的 pipeline_id 清单（根 + 血缘后代）——调用方据此逐出内存
    /// state 注册表（DB 已净而注册表残留会让 /pipelines/state 继续出口幽灵行）。
    /// 无记录时返回空清单（幂等，对齐 REST 删除语义）。
    async fn delete_session(&self, thread_id: &str) -> Result<Vec<String>, StorageError>;

    /// 按 pipeline_id 删除单条管道的全部执行数据（任务删除语义，2026-08-24）。
    /// 0.2 任务 = 管道：删除任务即删除其管道数据（runs/traces/branches/
    /// message_slots/pipeline_state/pipeline_checkpoints/pipeline_sessions），
    /// 并沿血缘 lineage.parent_pipeline_id 级联其派生的子任务链。
    /// 返回实际删除的 pipeline_id 清单（含血缘后代，供调用方逐出内存注册表）。
    /// 无记录时返回空清单（幂等）。
    async fn delete_pipeline(&self, _pipeline_id: &str) -> Result<Vec<String>, StorageError> {
        Ok(Vec::new())
    }

    /// 写入 pipeline↔session 映射（幂等）。每次管道开跑（persist_run_start）时记录，
    /// 含子任务管道。删除会话时据此按 thread_id 找到全部 pipeline_id 级联清理。
    async fn link_pipeline_session(
        &self,
        pipeline_id: &str,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<(), StorageError>;

    /// 查询某会话下的全部 pipeline_id（主管道 + 子任务管道）。
    async fn list_pipeline_ids_by_thread(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<String>, StorageError>;

    /// 按管道唯一坐标反查所属会话 thread_id（注入分支坐标解析）。
    /// pipeline_id 全局唯一（uuid），单查无歧义，不按租户过滤。
    /// 只查 pipeline_sessions 映射表（唯一真值源），未命中即 None——调用方
    /// （chat.send_message 注入分支）据此报协议错误，不做 sessions 回退。
    /// 默认 `Ok(None)`（mock/null store 不破），SqliteStore 覆盖为真实查询。
    async fn get_thread_id_by_pipeline(
        &self,
        _pipeline_id: &str,
    ) -> Result<Option<String>, StorageError> {
        Ok(None)
    }

    /// 查询某会话下的 step 级轨迹（冷启动统一回放用）。
    /// 经 pipeline_sessions 映射 → run_id → traces，只取 step 级（plugin_id 为配置
    /// step id），按 created_at 升序以便按序 merge 回放重建完整 state（含 messages）。
    ///
    /// 会话是组织集合：本方法只供会话面聚合读（执行记录页/复盘骨架），
    /// 管道执行态恢复禁止用它（会串其它管道的控制态）——用
    /// [`Self::get_step_traces_by_pipeline`]。
    async fn get_step_traces_by_thread(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<TraceEntry>, StorageError>;

    /// 查询单个管道自己的 step 级轨迹（管道执行态冷恢复专用）。
    /// pipeline_id 是执行态唯一坐标：state 回放只允许读本管道产生的轨迹，
    /// 同会话其它管道（父/兄弟子任务）的轨迹一律不可见。
    /// 默认 `Ok(vec![])`（mock/null store 不破），SqliteStore 覆盖为真实查询。
    async fn get_step_traces_by_pipeline(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<TraceEntry>, StorageError> {
        Ok(vec![])
    }

    // ── 域6：users（0.5.0 完整用户系统的最小持久化地基）───────────────
    // register 真实建用户、login/me/refresh/WS 查 DB。RBAC/JWT/bcrypt 留给 0.5.0。
    // 一用户一租户：tenant_id = user_id（admin 种子 = "default"）。

    /// 创建用户（username 全局唯一约束，重复返回 StorageError）。
    async fn create_user(&self, user: &UserRecord) -> Result<(), StorageError>;

    /// 按 user_id 取用户（WS 握手 / token 解析用，按 tenant 隔离）。
    async fn get_user_by_id(&self, user_id: &str) -> Result<Option<UserRecord>, StorageError>;

    /// 按用户名取用户（登录用，跨租户全局查询，不加 tenant 过滤）。
    async fn get_user_by_username(
        &self,
        username: &str,
    ) -> Result<Option<UserRecord>, StorageError>;

    /// 列全部用户（管理用，跨租户）。
    async fn list_users(&self) -> Result<Vec<UserRecord>, StorageError>;

    /// 更新最近登录时间（登录成功后调）。
    async fn update_last_login(&self, user_id: &str) -> Result<(), StorageError>;

    /// 更新口令哈希与首登改密标记（D1：改密端点/启动明文迁移共用）。
    /// `password_hash` 必须已是 argon2id 哈希（调用方负责）；不存在返回 false。
    async fn update_user_password(
        &self,
        user_id: &str,
        password_hash: &str,
        must_change_password: bool,
    ) -> Result<bool, StorageError>;

    /// 删除用户。不存在返回 false。
    async fn delete_user(&self, user_id: &str) -> Result<bool, StorageError>;
}

/// `list_sessions` 的过滤条件。
///
/// tenant_id 走 task_local（与消息查询一致）。limit 控制返回条数。
/// session_type 用于按 metadata.session_type 过滤（对齐 0.1 list_threads 的 query）。
#[derive(Debug, Clone, Default)]
pub struct SessionListFilter {
    /// 按 metadata.session_type 过滤（如 "main_pipeline"）
    pub session_type: Option<String>,
    /// 最多返回条数
    pub limit: Option<usize>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mcp_config_request_timeout_secs_parse_roundtrip() {
        // 显式声明 → 覆盖内核默认（86400s，BUG-60 审批族统一值）
        let cfg: McpConfig = serde_json::from_value(serde_json::json!({
            "transport": "stdio",
            "request_timeout_secs": 90000,
        }))
        .expect("合法配置应可解析");
        assert_eq!(cfg.request_timeout_secs, Some(90000));

        // 缺省 → None（内核 mcp 客户端默认兜底，现为 86400s 统一值）
        let cfg2: McpConfig =
            serde_json::from_value(serde_json::json!({"transport": "stdio"})).expect("缺省可解析");
        assert_eq!(cfg2.request_timeout_secs, None);

        // 序列化往返：None 字段不输出（兼容旧清单）
        let s = serde_json::to_string(&cfg2).unwrap();
        assert!(
            !s.contains("request_timeout_secs"),
            "None 字段不应序列化: {s}"
        );
    }

    #[test]
    fn plugin_manifest_host_group_serde_roundtrip() {
        // 合宿进程模型 §4.1：host_group 缺省 None（旧清单零迁移）；
        // 显式 "light" → Some；None 不序列化输出（兼容旧清单）。
        let base = serde_json::json!({
            "id": "p", "name": "p", "version": "1", "plugin_type": "tool",
            "language": "python", "host_type": "sidecar", "entry": "python s.py",
            "capabilities": {}
        });
        let m: PluginManifest = serde_json::from_value(base).expect("缺省 host_group 应可解析");
        assert_eq!(m.host_group, None, "缺省 = None（独占宿主，默认保守）");

        let light = serde_json::json!({
            "id": "p", "name": "p", "version": "1", "plugin_type": "tool",
            "language": "python", "host_type": "sidecar", "entry": "python s.py",
            "capabilities": {}, "host_group": "light"
        });
        let m2: PluginManifest = serde_json::from_value(light).expect("host_group=light 应可解析");
        assert_eq!(m2.host_group.as_deref(), Some("light"));

        // 序列化往返：None 字段不输出
        let s = serde_json::to_string(&m).unwrap();
        assert!(!s.contains("host_group"), "None 字段不应序列化: {s}");
        let s2 = serde_json::to_string(&m2).unwrap();
        assert!(s2.contains("host_group"), "Some 字段应保留: {s2}");
    }

    /// capabilities.steps 契约（管道步骤服务化提案 §3.1）：带 steps 的
    /// manifest 反序列化 → 序列化 → 反序列化一致（roundtrip 幂等）。
    #[test]
    fn manifest_steps_serde_roundtrip() {
        let json = serde_json::json!({
            "id": "task_service",
            "name": "Task Service",
            "version": "1.0.0",
            "plugin_type": "pipeline",
            "language": "python",
            "host_type": "sidecar",
            "entry": "python server.py",
            "capabilities": {
                "steps": [
                    {
                        "name": "task.inject_params",
                        "description": "注入任务参数",
                        "input_schema": {"type": "object", "properties": {"params": {"type": "string"}}}
                    },
                    { "name": "task.remind", "description": "评估闸门推进" }
                ]
            }
        });
        let m: PluginManifest =
            serde_json::from_value(json).expect("带 steps 的 manifest 应可解析");
        assert_eq!(m.capabilities.steps.len(), 2);
        assert_eq!(m.capabilities.steps[0].name, "task.inject_params");
        assert_eq!(
            m.capabilities.steps[0].description.as_deref(),
            Some("注入任务参数")
        );
        assert!(m.capabilities.steps[0].input_schema.is_some());
        assert_eq!(m.capabilities.steps[1].name, "task.remind");
        assert_eq!(m.capabilities.steps[1].input_schema, None);

        // roundtrip：序列化后重新反序列化，再序列化产物逐字节一致
        // （None 字段不输出、空 input_schema 不输出，但语义等价）
        let s = serde_json::to_string(&m).unwrap();
        assert!(s.contains("task.inject_params"), "序列化应保留步骤名: {s}");
        let m2: PluginManifest = serde_json::from_str(&s).expect("序列化产物应可重新解析");
        let s2 = serde_json::to_string(&m2).unwrap();
        assert_eq!(s, s2, "roundtrip 后序列化产物应逐字节一致");
    }

    /// state.reads 读面声明契约：三种合法形态（键名 / `messages` 全量 /
    /// `messages_tail:N`）解析并保留；roundtrip 幂等；空 reads 不序列化输出。
    #[test]
    fn state_declaration_reads_serde_roundtrip() {
        let json = serde_json::json!({
            "id": "p", "name": "p", "version": "1", "plugin_type": "tool",
            "language": "python", "host_type": "sidecar", "entry": "python s.py",
            "capabilities": {},
            "state": {
                "volatile_keys": ["mood.notes"],
                "reads": ["task.status", "messages", "messages_tail:50"]
            }
        });
        let m: PluginManifest =
            serde_json::from_value(json).expect("含 reads 的 manifest 应可解析");
        let state = m.state.as_ref().expect("state 段应存在");
        assert_eq!(
            state.reads,
            vec![
                "task.status".to_string(),
                "messages".to_string(),
                "messages_tail:50".to_string()
            ],
            "三种合法形态原样收录"
        );

        // roundtrip：序列化产物可重新解析且逐字节一致（reads 保留）
        let s = serde_json::to_string(&m).unwrap();
        assert!(
            s.contains("messages_tail:50"),
            "序列化应保留 reads 条目: {s}"
        );
        let m2: PluginManifest = serde_json::from_str(&s).expect("序列化产物应可重新解析");
        assert_eq!(m2.state.as_ref().unwrap().reads, state.reads);
        let s2 = serde_json::to_string(&m2).unwrap();
        assert_eq!(s, s2, "roundtrip 后序列化产物应逐字节一致");

        // 空 reads 不序列化输出（与既有字段风格一致）；旧 state 段
        //（只有 volatile_keys）→ reads 缺省为空
        let legacy = serde_json::json!({
            "id": "p", "name": "p", "version": "1", "plugin_type": "tool",
            "language": "python", "host_type": "sidecar", "entry": "python s.py",
            "capabilities": {},
            "state": {"volatile_keys": ["k"]}
        });
        let m3: PluginManifest =
            serde_json::from_value(legacy).expect("旧 state 段（无 reads）应可解析");
        assert!(
            m3.state.as_ref().unwrap().reads.is_empty(),
            "缺省 reads = 空（向后兼容）"
        );
        let s3 = serde_json::to_string(&m3.state.as_ref().unwrap()).unwrap();
        assert!(!s3.contains("reads"), "空 reads 不应序列化输出: {s3}");
    }

    /// 向后兼容：旧 manifest 无 capabilities.steps 键 → 反序列化成功且 steps 为空
    /// （serde(default) 兜底，存量 25+ 管道插件零迁移）。
    #[test]
    fn manifest_without_steps_backward_compat() {
        let json = serde_json::json!({
            "id": "legacy_pipeline",
            "name": "Legacy",
            "version": "1.0.0",
            "plugin_type": "pipeline",
            "language": "python",
            "host_type": "sidecar",
            "entry": "python server.py",
            "capabilities": {"services": [{"name": "svc.health"}]}
        });
        let m: PluginManifest =
            serde_json::from_value(json).expect("无 steps 键的旧 manifest 应可解析");
        assert!(
            m.capabilities.steps.is_empty(),
            "旧 manifest 缺省 steps 应为空（隐式默认注册兜底）"
        );
        // 无 steps 时不输出 steps 键（旧清单兼容输出）
        let s = serde_json::to_string(&m).unwrap();
        assert!(!s.contains("\"steps\""), "空 steps 不应序列化输出: {s}");
    }

    // ── trait 默认实现契约（具体实现覆盖后无人单调默认版，此处锁默认语义）──

    use crate::types::{PendingInputRecord, PendingInputSource};

    struct BareInvoker;

    #[async_trait::async_trait]
    impl PluginInvoker for BareInvoker {
        async fn invoke_pipeline_plugin<'a>(
            &self,
            _plugin_id: &str,
            _ctx: &PluginContext<'a>,
        ) -> Result<PluginResult, PluginError> {
            unreachable!("默认面测试不调用")
        }
        async fn invoke_tool(
            &self,
            _plugin_id: &str,
            _tool_name: &str,
            _inputs: &serde_json::Value,
        ) -> Result<ToolExecutionResult, PluginError> {
            unreachable!("默认面测试不调用")
        }
        async fn send_lifecycle_hook(
            &self,
            _plugin_id: &str,
            _hook: LifecycleHook,
            _context: &HookContext,
        ) -> Result<(), PluginError> {
            unreachable!("默认面测试不调用")
        }
    }

    #[tokio::test]
    async fn invoker_default_methods_contract() {
        let inv = BareInvoker;
        // force_unload 默认 Ok；reload_member 默认不支持（调用方须回退 force_unload）；
        // discover 默认空；list_plugin_tools 默认不支持；
        // shutdown_all / kill_sidecar_if_any 默认 no-op（调用即覆盖，无 panic 即契约）。
        assert!(inv.force_unload("p").await.is_ok());
        assert!(inv.reload_member("p").await.is_err());
        assert!(inv.discover_new_plugins().await.unwrap().is_empty());
        assert!(inv.list_plugin_tools("p").await.is_err());
        inv.shutdown_all().await;
        inv.kill_sidecar_if_any("p").await;
    }

    struct BareStore;

    #[async_trait::async_trait]
    impl StorageBackend for BareStore {
        async fn get_run(&self, _run_id: &str) -> Result<RunRecord, StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn get_messages_by_pipeline(
            &self,
            _pipeline_id: &str,
            _opts: MessageQueryOpts,
        ) -> Result<Vec<MessageRecord>, StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn get_blob(&self, _blob_id: &str) -> Result<Vec<u8>, StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn append_trace(&self, _entry: TraceEntry) -> Result<(), StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn store_blob(&self, _data: &[u8], _mime_type: &str) -> Result<String, StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn create_session(&self, _session: &SessionRecord) -> Result<(), StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn get_session(
            &self,
            _thread_id: &str,
        ) -> Result<Option<SessionRecord>, StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn list_sessions(
            &self,
            _filter: SessionListFilter,
        ) -> Result<Vec<SessionRecord>, StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn update_session(&self, _session: &SessionRecord) -> Result<(), StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn delete_session(&self, _thread_id: &str) -> Result<Vec<String>, StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn link_pipeline_session(
            &self,
            _pipeline_id: &str,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<(), StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn list_pipeline_ids_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<String>, StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn get_step_traces_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<TraceEntry>, StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn create_user(&self, _user: &UserRecord) -> Result<(), StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn get_user_by_id(&self, _user_id: &str) -> Result<Option<UserRecord>, StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn get_user_by_username(
            &self,
            _username: &str,
        ) -> Result<Option<UserRecord>, StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn list_users(&self) -> Result<Vec<UserRecord>, StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn update_last_login(&self, _user_id: &str) -> Result<(), StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn update_user_password(
            &self,
            _user_id: &str,
            _password_hash: &str,
            _must_change_password: bool,
        ) -> Result<bool, StorageError> {
            unreachable!("默认面测试不调用")
        }
        async fn delete_user(&self, _user_id: &str) -> Result<bool, StorageError> {
            unreachable!("默认面测试不调用")
        }
    }

    fn bare_pending_input() -> PendingInputRecord {
        PendingInputRecord {
            id: "i".into(),
            pipeline_id: "p".into(),
            tenant_id: "t".into(),
            user_id: "u".into(),
            content: "c".into(),
            thread: "th".into(),
            source: PendingInputSource::System,
            agent_id: "a".into(),
            route_id: "r".into(),
            thinking_strength: "off".into(),
            client_message_id: String::new(),
            execution_context: None,
            state_overlay: None,
            created_at: "2026-01-01T00:00:00Z".into(),
        }
    }

    #[tokio::test]
    async fn storage_default_methods_contract() {
        let s = BareStore;
        // 运行投影族默认：record_run_start no-op = Ok；list_runs_by_pipeline 空集
        assert!(s.record_run_start("p", "t", "r", "hash").await.is_ok());
        assert!(s.list_runs_by_pipeline("p", "t").await.unwrap().is_empty());
        assert!(s.list_pipelines("t", None, 10).await.unwrap().is_empty());
        assert!(s.delete_pipeline("p").await.is_ok());
        // state 投影族默认：no-op / 空 map / None
        assert!(s.apply_messages_ops_to_table("p", "t", &[]).await.is_ok());
        assert!(s
            .upsert_state_field("p", "t", "k", &serde_json::json!(1))
            .await
            .is_ok());
        assert!(s.load_pipeline_state("p", "t").await.unwrap().is_empty());
        assert!(s
            .save_checkpoint("p", "t", 1, &serde_json::json!({}))
            .await
            .is_ok());
        assert!(s.load_latest_checkpoint("p", "t").await.unwrap().is_none());
        assert!(s.list_state_pipeline_ids("t").await.unwrap().is_empty());
        assert!(s.load_message_history("p", "t").await.unwrap().is_empty());
        // pending 队列族默认：no-op / None / 空 / false / 0
        let rec = bare_pending_input();
        assert!(s.enqueue_pending_input("t", "p", &rec).await.is_ok());
        assert!(s.pop_pending_input("t", "p").await.unwrap().is_none());
        assert!(s.list_pending_inputs("t", "p").await.unwrap().is_empty());
        assert!(!s
            .update_pending_input_content("t", "p", "i", "x")
            .await
            .unwrap());
        assert!(!s.delete_pending_input("t", "p", "i").await.unwrap());
        assert_eq!(s.clear_pending_inputs("t", "p").await.unwrap(), 0);
    }

    #[tokio::test]
    async fn storage_remaining_default_methods_contract() {
        let s = BareStore;
        // 声明易变键注入：no-op 契约（调用即覆盖，不 panic 即契约）
        s.set_declared_volatile_keys(&["mood.notes".to_string(), "scratch".to_string()]);
        // B6 批量 upsert 默认退化为逐键单键 upsert：全成才 Ok
        let mut fields = serde_json::Map::new();
        fields.insert("track.total_tokens".into(), serde_json::json!(42));
        fields.insert("mood".into(), serde_json::json!("ok"));
        assert!(s.upsert_state_fields("p", "t", &fields).await.is_ok());
        // 会话坐标反查默认 None；管道 step 轨迹默认空
        assert!(s.get_thread_id_by_pipeline("p").await.unwrap().is_none());
        assert!(s
            .get_step_traces_by_pipeline("p", "t")
            .await
            .unwrap()
            .is_empty());
    }

    // ── serde 往返 / 声明面 / 默认值契约 ─────────────────────────────

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

    fn minimal_manifest() -> PluginManifest {
        serde_json::from_value(serde_json::json!({
            "id": "p", "name": "p", "version": "1", "plugin_type": "tool",
            "language": "python", "host_type": "sidecar", "entry": "python s.py",
            "capabilities": {}
        }))
        .expect("最小 manifest 应可解析")
    }

    #[test]
    fn registration_guard_drop_revokes_exactly_once() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        let counter = std::sync::Arc::new(AtomicUsize::new(0));
        let c2 = counter.clone();
        let guard = RegistrationGuard::new(move || {
            c2.fetch_add(1, Ordering::SeqCst);
        });
        assert_eq!(counter.load(Ordering::SeqCst), 0, "构造不触发注销");
        drop(guard);
        assert_eq!(
            counter.load(Ordering::SeqCst),
            1,
            "drop 必须精确注销该条注册"
        );
    }

    #[test]
    fn registration_guard_disarm_skips_revoke() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        let counter = std::sync::Arc::new(AtomicUsize::new(0));
        let c2 = counter.clone();
        let guard = RegistrationGuard::new(move || {
            c2.fetch_add(1, Ordering::SeqCst);
        });
        guard.disarm(); // 所有权转交 / 停机整体清算：不再逐条注销
        assert_eq!(
            counter.load(Ordering::SeqCst),
            0,
            "disarm 后 drop 不应再注销"
        );
    }

    #[test]
    fn http_route_descriptor_defaults_and_accessors() {
        let endpoint = |timeout_ms: Option<u64>, max_concurrency: Option<u32>| HttpEndpoint {
            route_id: "r1".into(),
            method: "POST".into(),
            path: "/ext/p/callback".into(),
            auth: Some("none".into()),
            handler_capability: "http.handle".into(),
            timeout_ms,
            max_concurrency,
            description: None,
        };
        // 缺省：30000ms / 16 并发（附录 E.1.3）
        let d = HttpRouteDescriptor::new("p".into(), endpoint(None, None));
        assert_eq!(d.plugin_id, "p");
        assert_eq!(d.timeout_ms(), HTTP_ENDPOINT_DEFAULT_TIMEOUT_MS);
        assert_eq!(d.max_concurrency(), HTTP_ENDPOINT_DEFAULT_MAX_CONCURRENCY);
        // 显式声明优先生效
        let e = HttpRouteDescriptor::new("p".into(), endpoint(Some(1234), Some(3)));
        assert_eq!(e.timeout_ms(), 1234);
        assert_eq!(e.max_concurrency(), 3);
        // 运行时表示可经 JSON 传输且解析值保真
        let text = serde_json::to_string(&e).unwrap();
        let back: HttpRouteDescriptor = serde_json::from_str(&text).unwrap();
        assert_eq!(back.timeout_ms(), 1234);
        assert_eq!(back.max_concurrency(), 3);
        assert_eq!(back.endpoint, e.endpoint);
        assert_eq!(back.plugin_id, "p");
    }

    #[test]
    fn http_endpoint_auth_contract() {
        let parse =
            |json: &str| -> Result<HttpEndpoint, serde_json::Error> { serde_json::from_str(json) };
        // 合法枚举：none / user / admin
        for v in ["none", "user", "admin"] {
            let ep = parse(&format!(
                r#"{{"route_id":"r","method":"GET","path":"/ext/p/x","auth":"{v}","handler_capability":"http.handle"}}"#
            ))
            .unwrap();
            assert_eq!(ep.auth.as_deref(), Some(v));
        }
        // 字段缺省 → None（无声明，dispatcher fail-closed）
        let missing = parse(
            r#"{"route_id":"r","method":"GET","path":"/ext/p/x","handler_capability":"http.handle"}"#,
        )
        .unwrap();
        assert_eq!(missing.auth, None);
        // 显式 null → None
        let null_ep = parse(
            r#"{"route_id":"r","method":"GET","path":"/ext/p/x","auth":null,"handler_capability":"http.handle"}"#,
        )
        .unwrap();
        assert_eq!(null_ep.auth, None);
        // 非法值 → 反序列化期拒绝（fail-closed）
        let err = parse(
            r#"{"route_id":"r","method":"GET","path":"/ext/p/x","auth":"root","handler_capability":"http.handle"}"#,
        )
        .unwrap_err();
        assert!(
            err.to_string().contains("must be one of none/user/admin"),
            "非法 auth 报错应指明合法值域: {err}"
        );
        // roundtrip（None 序列化为 null 后仍可解析回 None）
        assert_serde_roundtrip(&missing);
    }

    struct EchoHttpHandler;

    #[async_trait::async_trait]
    impl HttpHandleCapability for EchoHttpHandler {
        async fn handle(&self, req: HttpHandleRequest) -> Result<HttpHandleResponse, String> {
            if req.path.ends_with("/boom") {
                return Err("handler exploded".into());
            }
            Ok(HttpHandleResponse {
                status: 200,
                headers: HashMap::from([("x-echo-plugin".to_string(), req.plugin_id)]),
                body: req.raw_body,
                body_encoding: "base64".into(),
            })
        }
    }

    #[tokio::test]
    async fn http_handle_capability_passthrough_contract() {
        let handler = EchoHttpHandler;
        // 铁律：raw_body 原文透传（base64），headers 全量透传
        let resp = handler
            .handle(HttpHandleRequest {
                method: "POST".into(),
                path: "/ext/wecom/callback".into(),
                plugin_id: "wecom".into(),
                raw_body: "aGVsbG8=".into(),
                headers: HashMap::from([("x-signature".to_string(), "sig".to_string())]),
                query: HashMap::from([("msg_signature".to_string(), "s".to_string())]),
                query_multi: HashMap::from([(
                    "filter".to_string(),
                    vec!["a".to_string(), "b".to_string()],
                )]),
            })
            .await
            .expect("透传请求应成功");
        assert_eq!(resp.status, 200);
        assert_eq!(resp.body, "aGVsbG8=", "raw_body 原文透传");
        assert_eq!(resp.body_encoding, "base64");
        assert_eq!(
            resp.headers.get("x-echo-plugin").map(String::as_str),
            Some("wecom"),
            "插件完全控制响应头"
        );
        // 错误是值：Err 字符串由 dispatcher 记 502
        let err = handler
            .handle(HttpHandleRequest {
                method: "POST".into(),
                path: "/ext/wecom/boom".into(),
                plugin_id: "wecom".into(),
                raw_body: String::new(),
                headers: HashMap::new(),
                query: HashMap::new(),
                query_multi: HashMap::new(),
            })
            .await
            .unwrap_err();
        assert_eq!(err, "handler exploded");
    }

    #[test]
    fn http_handle_request_query_multi_backward_compat() {
        // 旧负载无 query_multi → 空 map（向后兼容，SDK 侧按 handler 签名过滤）
        let legacy: HttpHandleRequest = serde_json::from_str(
            r#"{"method":"GET","path":"/p","plugin_id":"x","raw_body":"","headers":{},"query":{"a":"1"}}"#,
        )
        .unwrap();
        assert!(legacy.query_multi.is_empty());
        assert_eq!(legacy.query.get("a").map(String::as_str), Some("1"));
        // 多值形态 roundtrip：query[k] == query_multi[k].last()
        let full = HttpHandleRequest {
            method: "GET".into(),
            path: "/p".into(),
            plugin_id: "x".into(),
            raw_body: String::new(),
            headers: HashMap::new(),
            query: HashMap::from([("filter".to_string(), "b".to_string())]),
            query_multi: HashMap::from([(
                "filter".to_string(),
                vec!["a".to_string(), "b".to_string()],
            )]),
        };
        assert_serde_roundtrip(&full);
        // 响应结构 roundtrip
        let resp = HttpHandleResponse {
            status: 502,
            headers: HashMap::from([("content-type".to_string(), "text/xml".to_string())]),
            body: "PGZvby8+".into(),
            body_encoding: "base64".into(),
        };
        let text = serde_json::to_string(&resp).unwrap();
        let back: HttpHandleResponse = serde_json::from_str(&text).unwrap();
        assert_eq!(back.status, 502);
        assert_eq!(back.body, "PGZvby8+");
    }

    #[test]
    fn hook_context_tagged_context_contract() {
        let mut ctx = HookContext::new();
        assert!(ctx.get("session_id").is_none(), "空上下文查无");
        // 链式 set（Builder 模式）
        ctx.set("session_id", serde_json::json!("s-1"))
            .set("iteration", serde_json::json!(3));
        assert_eq!(ctx.get("session_id"), Some(&serde_json::json!("s-1")));
        // get_as 类型化读取：成功 + 类型不符静默 None（消费方按需处理）
        assert_eq!(ctx.get_as::<i64>("iteration"), Some(3));
        assert_eq!(ctx.get_as::<String>("iteration"), None, "类型不符静默降级");
        assert_eq!(ctx.get_as::<String>("missing"), None);
        // 同 key 后写覆盖
        ctx.set("iteration", serde_json::json!(4));
        assert_eq!(ctx.get_as::<i64>("iteration"), Some(4));
        assert_eq!(ctx.tags().len(), 2);
        // 上下文随钩子事件跨进程传输：serde 往返保真
        let text = serde_json::to_string(&ctx).unwrap();
        let back: HookContext = serde_json::from_str(&text).unwrap();
        assert_eq!(back.get_as::<String>("session_id"), Some("s-1".to_string()));
        assert_eq!(back.get_as::<i64>("iteration"), Some(4));
    }

    /// HookContext::default() 等价 new()（空上下文）——派生/默认构造路径
    /// 与显式构造同语义，消费方可互换使用。
    #[test]
    fn hook_context_default_equals_new() {
        let d = HookContext::default();
        assert!(d.tags().is_empty(), "默认构造应为空标签表");
        assert!(d.get("any").is_none());
        assert_eq!(d.get_as::<String>("any"), None);
        let n = HookContext::new();
        assert_eq!(d.tags(), n.tags());
    }

    /// ActivationPolicy 的 Default 是 Lazy（多数 tool/评估/监控插件的默认
    /// 激活策略）：manifest 未声明时不得默认 Eager。
    #[test]
    fn activation_policy_default_is_lazy() {
        assert_eq!(ActivationPolicy::default(), ActivationPolicy::Lazy);
        assert_eq!(
            serde_json::from_str::<ActivationPolicy>("\"lazy\"").unwrap(),
            ActivationPolicy::Lazy
        );
    }

    #[test]
    fn lifecycle_hook_serde_roundtrip_all_variants() {
        for (value, wire) in [
            (LifecycleHook::OnLoad, "on_load"),
            (LifecycleHook::OnUnload, "on_unload"),
            (LifecycleHook::OnPipelineStart, "on_pipeline_start"),
            (LifecycleHook::OnPipelineEnd, "on_pipeline_end"),
            (LifecycleHook::OnError, "on_error"),
            (LifecycleHook::DomainEvent, "domain_event"),
        ] {
            let text = serde_json::to_string(&value).unwrap();
            assert_eq!(text, format!("\"{wire}\""));
            let back: LifecycleHook = serde_json::from_str(&text).unwrap();
            assert_eq!(back, value);
        }
        assert!(serde_json::from_str::<LifecycleHook>("\"on_boot\"").is_err());
    }

    #[test]
    fn plugin_and_pipeline_role_serde_roundtrip() {
        for (value, wire) in [
            (PluginType::Pipeline, "pipeline"),
            (PluginType::Tool, "tool"),
            (PluginType::System, "system"),
            (PluginType::Composite, "composite"),
        ] {
            let text = serde_json::to_string(&value).unwrap();
            assert_eq!(text, format!("\"{wire}\""));
            let back: PluginType = serde_json::from_str(&text).unwrap();
            assert_eq!(back, value);
        }
        for (value, wire) in [
            (PipelineRole::Input, "input"),
            (PipelineRole::Core, "core"),
            (PipelineRole::Output, "output"),
        ] {
            let text = serde_json::to_string(&value).unwrap();
            assert_eq!(text, format!("\"{wire}\""));
            let back: PipelineRole = serde_json::from_str(&text).unwrap();
            assert_eq!(back, value);
        }
        // 畸形输入拒收
        assert!(serde_json::from_str::<PluginType>("\"hook\"").is_err());
        assert!(serde_json::from_str::<PipelineRole>("\"middle\"").is_err());
    }

    #[test]
    fn host_activation_status_defaults_and_serde() {
        // Default：sidecar / lazy / discovered
        assert_eq!(HostType::default(), HostType::Sidecar);
        assert_eq!(ActivationPolicy::default(), ActivationPolicy::Lazy);
        assert_eq!(PluginStatus::default(), PluginStatus::Discovered);

        for (text, expected) in [
            ("\"in_process\"", HostType::InProcess),
            ("\"sidecar\"", HostType::Sidecar),
        ] {
            assert_eq!(serde_json::from_str::<HostType>(text).unwrap(), expected);
        }
        for (text, expected) in [
            ("\"eager\"", ActivationPolicy::Eager),
            ("\"lazy\"", ActivationPolicy::Lazy),
            ("\"manual\"", ActivationPolicy::Manual),
        ] {
            assert_eq!(
                serde_json::from_str::<ActivationPolicy>(text).unwrap(),
                expected
            );
        }
        for (text, expected) in [
            ("\"discovered\"", PluginStatus::Discovered),
            ("\"loading\"", PluginStatus::Loading),
            ("\"active\"", PluginStatus::Active),
            ("\"idle\"", PluginStatus::Idle),
            ("\"draining\"", PluginStatus::Draining),
            ("\"unloaded\"", PluginStatus::Unloaded),
            ("\"crashed\"", PluginStatus::Crashed),
            ("\"failed\"", PluginStatus::Failed),
        ] {
            assert_eq!(
                serde_json::from_str::<PluginStatus>(text).unwrap(),
                expected
            );
        }
        // 畸形输入拒收（wasm 轨已关闭；无 auto 激活；无 ready 态）
        assert!(serde_json::from_str::<HostType>("\"wasm\"").is_err());
        assert!(serde_json::from_str::<ActivationPolicy>("\"auto\"").is_err());
        assert!(serde_json::from_str::<PluginStatus>("\"ready\"").is_err());
    }

    #[test]
    fn provided_capability_host_and_declaration_serde() {
        // Default InProcess；kebab-case 线格式
        assert_eq!(
            ProvidedCapabilityHost::default(),
            ProvidedCapabilityHost::InProcess
        );
        assert_eq!(
            serde_json::to_string(&ProvidedCapabilityHost::InProcess).unwrap(),
            "\"in-process\""
        );
        assert_eq!(
            serde_json::to_string(&ProvidedCapabilityHost::Sidecar).unwrap(),
            "\"sidecar\""
        );
        // 缺省 host = InProcess；tool_prefix 缺省 None（从 namespace 派生）
        let cap: ProvidedCapability =
            serde_json::from_str(r#"{"namespace":"interaction-respond","methods":["respond"]}"#)
                .unwrap();
        assert_eq!(cap.host, ProvidedCapabilityHost::InProcess);
        assert_eq!(cap.tool_prefix, None);
        assert!(cap.protocol_roles.is_empty());
        // 完整形态：sidecar host + 显式前缀 + 协议角色（P1-3）
        let full = ProvidedCapability {
            namespace: "interaction-respond".into(),
            methods: vec!["respond".into()],
            host: ProvidedCapabilityHost::Sidecar,
            tool_prefix: Some("ix".into()),
            protocol_roles: vec![ProtocolRole {
                role: "interaction-respond".into(),
                method: "respond".into(),
            }],
        };
        assert_serde_roundtrip(&full);
        assert_eq!(
            full.protocol_roles[0].role, "interaction-respond",
            "角色绑定声明保真"
        );
    }

    #[test]
    fn endpoint_auth_serde_defaults() {
        // type 缺省 api_key；header_name 缺省 Authorization；required 缺省 None（按必需处理）
        let a: EndpointAuth = serde_json::from_str(r#"{"value":"${K}"}"#).unwrap();
        assert_eq!(a.auth_type, AuthType::ApiKey);
        assert_eq!(a.header_name, "Authorization");
        assert_eq!(a.required, None);
        // 显式 bearer + required=false（可选凭据语义）
        let b: EndpointAuth = serde_json::from_str(
            r#"{"type":"bearer","header_name":"Authorization","value":"x","required":false}"#,
        )
        .unwrap();
        assert_eq!(b.auth_type, AuthType::Bearer);
        assert_eq!(b.required, Some(false));
        // AuthType 三态线格式（snake_case）
        assert_eq!(
            serde_json::to_string(&AuthType::ApiKey).unwrap(),
            "\"api_key\""
        );
        assert_eq!(
            serde_json::to_string(&AuthType::Bearer).unwrap(),
            "\"bearer\""
        );
        assert_eq!(serde_json::to_string(&AuthType::None).unwrap(), "\"none\"");
        // 畸形输入拒收
        assert!(serde_json::from_str::<EndpointAuth>(r#"{"value":"x","type":"oauth"}"#).is_err());
    }

    #[test]
    fn mcp_endpoint_serde_defaults() {
        let e: McpEndpoint = serde_json::from_str("{}").unwrap();
        assert_eq!(e.url, None);
        assert!(e.headers.is_empty());
        assert!(e.auth.is_none());
        assert!(e.command.is_none());
        assert!(e.args.is_empty());
        assert!(e.env.is_empty());
        // stdio 形态（本地第三方命令）：字段保真 + ${ENV_VAR} 占位原样保留
        let stdio: McpEndpoint = serde_json::from_str(
            r#"{"command":"npx","args":["-y","mcp"],"env":{"K":"${V}"},"url":null}"#,
        )
        .unwrap();
        assert_eq!(stdio.command.as_deref(), Some("npx"));
        assert_eq!(stdio.args, vec!["-y".to_string(), "mcp".to_string()]);
        assert_eq!(stdio.env.get("K").map(String::as_str), Some("${V}"));
        assert_eq!(stdio.url, None);
        // 序列化再解析保真（invoker 构造客户端的输入面）
        assert_serde_roundtrip(&stdio);
    }

    #[test]
    fn mcp_config_serde_defaults() {
        let c: McpConfig = serde_json::from_str(r#"{"transport":"stdio"}"#).unwrap();
        assert_eq!(c.transport, McpTransport::Stdio);
        assert_eq!(c.idle_timeout_secs, 300, "缺省空闲卸载 300s");
        assert_eq!(c.protocol_version, "2025-06-18");
        assert!(
            c.request_timeout_secs.is_none(),
            "缺省 = None（内核 mcp 客户端默认 86400s 兜底，BUG-60 审批族统一值）"
        );
        assert!(c.endpoint.is_none(), "stdio 无外部端点");
        // HTTP 形态：长等待显式声明保留
        let h: McpConfig = serde_json::from_str(
            r#"{"transport":"streamable_http","endpoint":{"url":"https://x"},"request_timeout_secs":86400}"#,
        )
        .unwrap();
        assert_eq!(h.transport, McpTransport::StreamableHttp);
        assert_eq!(
            h.endpoint.as_ref().unwrap().url.as_deref(),
            Some("https://x")
        );
        assert_eq!(h.request_timeout_secs, Some(86400));
        // 序列化 → 反序列化保真
        let back: McpConfig = serde_json::from_str(&serde_json::to_string(&h).unwrap()).unwrap();
        assert_eq!(back.request_timeout_secs, Some(86400));
        assert_eq!(back.idle_timeout_secs, 300);
        // 畸形 transport 拒收
        assert!(serde_json::from_str::<McpConfig>(r#"{"transport":"grpc"}"#).is_err());
    }

    #[test]
    fn default_value_functions_contract() {
        assert_eq!(default_priority(), 100);
        assert_eq!(default_idle_timeout(), 300);
        assert_eq!(default_capability_timeout_ms(), 300_000);
        assert_eq!(HTTP_ENDPOINT_DEFAULT_TIMEOUT_MS, 30_000);
        assert_eq!(HTTP_ENDPOINT_DEFAULT_MAX_CONCURRENCY, 16);
    }

    #[test]
    fn env_config_field_defaults_and_extra_flatten() {
        // 缺省：secret（保守默认，宁可掩码不可泄漏）+ 非必填
        let f: EnvConfigField = serde_json::from_str(r#"{"name":"K","label":"K"}"#).unwrap();
        assert_eq!(f.field_type, "secret");
        assert!(!f.required);
        assert_eq!(f.description, None);
        assert!(
            f.extra.as_ref().is_none_or(|m| m.is_empty()),
            "无 UI 词汇 → extra 为空"
        );
        // UI 词汇表平铺透传：序列化无 "extra" 键（谁的数据谁出表单）
        let typed: EnvConfigField = serde_json::from_str(
            r#"{"name":"model","label":"Model","type":"select","required":true,"options":["a","b"],"default":"a"}"#,
        )
        .unwrap();
        assert_eq!(typed.field_type, "select");
        assert!(typed.required);
        let extra = typed.extra.as_ref().expect("UI 词汇应进 extra");
        assert_eq!(extra.get("options"), Some(&serde_json::json!(["a", "b"])));
        assert_eq!(extra.get("default"), Some(&serde_json::json!("a")));
        let text = serde_json::to_string(&typed).unwrap();
        assert!(text.contains(r#""options":["a","b"]"#), "平铺输出: {text}");
        assert!(!text.contains(r#""extra""#), "不应产生 extra 键: {text}");
        // roundtrip 保真
        let back: EnvConfigField = serde_json::from_str(&text).unwrap();
        assert_eq!(back, typed);
    }

    #[test]
    fn config_file_mapping_serde_roundtrip() {
        // env target：密钥表单（required 语义驱动 connect 硬失败/降级）
        let full = ConfigFileMapping {
            id: "api_key".into(),
            settings: Some(false),
            path: "config/models/llm.yaml".into(),
            label: "LLM Key".into(),
            target: Some("env".into()),
            fields: vec![EnvConfigField {
                name: "OPENAI_API_KEY".into(),
                label: "OpenAI Key".into(),
                field_type: "secret".into(),
                required: true,
                description: None,
                extra: None,
            }],
        };
        assert_serde_roundtrip(&full);
        // 内联形态：path 省略 → 空串（真值即 fields.default）
        let inline: ConfigFileMapping = serde_json::from_str(r#"{"id":"k","label":"K"}"#).unwrap();
        assert_eq!(inline.path, "");
        assert_eq!(inline.settings, None);
        assert_eq!(inline.target, None);
        assert!(inline.fields.is_empty());
    }

    #[test]
    fn tool_capability_serde_roundtrip() {
        let t = ToolCapability {
            name: "search".into(),
            description: Some("web search".into()),
            input_schema: Some(serde_json::json!({"type": "object"})),
            output_schema: Some(serde_json::json!({"type": "object"})),
            category: Some(ToolCategory::Search),
            ui: Some(serde_json::json!({"card": "web"})),
            render: Some(serde_json::json!({"card": "search"})),
            smoke: Some(true),
            timeout_ms: Some(0), // 0 哨兵 = 显式豁免总时长超时
        };
        assert_serde_roundtrip(&t);
        // None 字段不序列化（清单兼容输出）；反解析缺省 None
        let bare = ToolCapability {
            name: "t".into(),
            description: None,
            input_schema: None,
            output_schema: None,
            category: None,
            ui: None,
            render: None,
            smoke: None,
            timeout_ms: None,
        };
        let s = serde_json::to_string(&bare).unwrap();
        assert!(!s.contains("timeout_ms") && !s.contains("render"), "{s}");
        let back: ToolCapability = serde_json::from_str(&s).unwrap();
        assert_eq!(back.timeout_ms, None);
        assert_eq!(back.smoke, None);
    }

    #[test]
    fn service_and_step_capability_serde() {
        // 服务声明：input/output schema 可选（auto-backfill 前 None 容忍）
        let svc = ServiceCapability {
            name: "memory.search".into(),
            description: Some("语义检索".into()),
            input_schema: Some(serde_json::json!({"type": "object"})),
            output_schema: None,
        };
        let text = serde_json::to_string(&svc).unwrap();
        assert!(!text.contains("output_schema"), "None 不序列化: {text}");
        let back: ServiceCapability = serde_json::from_str(&text).unwrap();
        assert_eq!(back.output_schema, None);
        assert_eq!(back.name, "memory.search");
        // 步骤声明：三字段结构，roundtrip 保真
        let step = StepCapability {
            name: "task.inject_params".into(),
            description: None,
            input_schema: None,
        };
        let stext = serde_json::to_string(&step).unwrap();
        let sback: StepCapability = serde_json::from_str(&stext).unwrap();
        assert_eq!(sback, step);
    }

    #[test]
    fn streaming_capability_serde_defaults() {
        let s: StreamingCapability = serde_json::from_str("{}").unwrap();
        assert!(
            !s.conduit,
            "缺省 false：非内核 LLM 器官，发射闸 fail-closed"
        );
        assert_eq!(s.events, None, "缺省 = 契约全部事件可发射");
        assert_eq!(s.part_types, None);
        assert_eq!(s.persist, None);
        let full = StreamingCapability {
            conduit: true,
            events: Some(vec!["message.delta".into()]),
            part_types: Some(vec!["custom_chart".into()]),
            persist: Some(true),
        };
        assert_serde_roundtrip(&full);
        // conduit=false 不输出（skip_serializing_if Not::not）
        let plain = StreamingCapability {
            conduit: false,
            events: None,
            part_types: None,
            persist: None,
        };
        assert!(!serde_json::to_string(&plain).unwrap().contains("conduit"));
    }

    #[test]
    fn manifest_permissions_defaults_and_roundtrip() {
        let p = ManifestPermissions::default();
        assert!(p.env_vars.is_empty() && p.system_calls.is_empty());
        assert!(p.filesystem.read_paths.is_empty() && p.filesystem.write_paths.is_empty());
        assert!(p.network.allowed_hosts.is_empty());
        let full = ManifestPermissions {
            filesystem: FilesystemPermission {
                read_paths: vec!["config/**".into()],
                write_paths: vec!["workspace/**".into()],
            },
            network: NetworkPermission {
                allowed_hosts: vec!["api.openai.com".into()],
            },
            env_vars: vec!["OPENAI_API_KEY".into()],
            system_calls: vec!["exec".into()],
        };
        assert_serde_roundtrip(&full);
    }

    #[test]
    fn manifest_capabilities_rejects_unknown_fields() {
        // deny_unknown_fields：未知能力键解析期拒绝（fail-closed）
        let err = serde_json::from_str::<ManifestCapabilities>(r#"{"resources":[]}"#)
            .expect_err("未知键应拒绝");
        assert!(err.to_string().contains("unknown"), "{err}");
        // 全部能力槽位解析与 roundtrip
        let caps = ManifestCapabilities {
            tools: vec![ToolCapability {
                name: "t".into(),
                description: None,
                input_schema: None,
                output_schema: None,
                category: None,
                ui: None,
                render: None,
                smoke: None,
                timeout_ms: None,
            }],
            services: vec![ServiceCapability {
                name: "s".into(),
                description: None,
                input_schema: None,
                output_schema: None,
            }],
            steps: vec![StepCapability {
                name: "st".into(),
                description: None,
                input_schema: None,
            }],
            route_signals: vec![RouteType::NextLlm],
            lifecycle_hooks: vec![LifecycleHook::OnLoad],
            streaming: Some(StreamingCapability {
                conduit: false,
                events: None,
                part_types: None,
                persist: None,
            }),
        };
        assert_serde_roundtrip(&caps);
        // 空能力段 → 全缺省
        let empty: ManifestCapabilities = serde_json::from_str("{}").unwrap();
        assert!(empty.tools.is_empty() && empty.steps.is_empty() && empty.streaming.is_none());
    }

    #[test]
    fn plugin_manifest_full_serde_roundtrip() {
        // 顶层标量/简单字段先建基座，复合声明逐段并入（避免单个 json! 递归超限）
        let mut json = serde_json::json!({
            "id": "llm_core", "name": "LLM Core", "description": "LLM 执行器官",
            "version": "1.0.0", "plugin_type": "pipeline", "pipeline_role": "core",
            "language": "python", "host_type": "sidecar", "host_group": "light",
            "entry": "python server.py",
            "requires_services": ["service-registry.search"],
            "priority": 200,
            "persistent_fields": ["track.total_tokens"],
            "state": {"volatile_keys": ["scratch"], "reads": ["messages_tail:20"]},
            "force_include_tools": ["spill_retrieve"],
            "export_fields": ["task.owned.*"],
            "granted_capabilities": ["config-reader"],
            "requires_content": 20,
            "invoke_entry": "llm_core.execute",
            "enabled": true,
            "activation": "eager"
        });
        let obj = json.as_object_mut().unwrap();
        obj.insert(
            "capabilities".into(),
            serde_json::json!({
                "tools": [{"name": "t", "smoke": true}],
                "services": [{"name": "llm.execute"}],
                "route_signals": ["next_llm"],
                "lifecycle_hooks": ["on_load", "domain_event"],
                "streaming": {"conduit": true, "events": ["message.delta"], "persist": false}
            }),
        );
        obj.insert(
            "permissions".into(),
            serde_json::json!({
                "filesystem": {"read_paths": ["config/**"]},
                "network": {"allowed_hosts": ["api.x.com"]},
                "env_vars": ["K"],
                "system_calls": []
            }),
        );
        obj.insert(
            "mcp".into(),
            serde_json::json!({"transport": "stdio", "request_timeout_secs": 86400}),
        );
        obj.insert(
            "lifecycle".into(),
            serde_json::json!({"idle_timeout_secs": 0}),
        );
        obj.insert(
            "native".into(),
            serde_json::json!({"artifact": "libdemo.so"}),
        );
        obj.insert(
            "config_files".into(),
            serde_json::json!([{"id": "k", "path": "config/models/llm.yaml", "label": "K"}]),
        );
        obj.insert("ui_schema".into(), serde_json::json!({"type": "object"}));
        obj.insert("contributes".into(), serde_json::json!({"chatActions": []}));
        obj.insert(
            "http_endpoints".into(),
            serde_json::json!([{
                "route_id": "cb", "method": "POST", "path": "/ext/llm_core/cb",
                "auth": "none", "handler_capability": "http.handle",
                "timeout_ms": 5000, "max_concurrency": 4
            }]),
        );
        obj.insert(
            "provides".into(),
            serde_json::json!({"capabilities": [{
                "namespace": "interaction-respond", "methods": ["respond"],
                "host": "sidecar", "tool_prefix": "ix",
                "protocol_roles": [{"role": "interaction-respond", "method": "respond"}]
            }]}),
        );
        let m: PluginManifest = serde_json::from_value(json).expect("全量 manifest 应可解析");
        // 关键声明面字段逐项核验
        assert_eq!(m.priority, 200);
        assert_eq!(m.requires_content, Some(20));
        assert_eq!(m.activation, Some(ActivationPolicy::Eager));
        assert_eq!(m.enabled, Some(true));
        assert_eq!(m.pipeline_role, Some(PipelineRole::Core));
        assert_eq!(m.host_group.as_deref(), Some("light"));
        assert_eq!(
            m.lifecycle.as_ref().unwrap().idle_timeout_secs,
            Some(0),
            "0 哨兵 = 永不空闲卸载"
        );
        assert_eq!(m.native.as_ref().unwrap().artifact, "libdemo.so");
        assert_eq!(
            m.provides.as_ref().unwrap().capabilities[0].namespace,
            "interaction-respond"
        );
        assert_eq!(m.http_endpoints[0].auth.as_deref(), Some("none"));
        // roundtrip 幂等：序列化 → 反序列化 → 序列化逐字节一致
        let s = serde_json::to_string(&m).unwrap();
        let m2: PluginManifest = serde_json::from_str(&s).expect("序列化产物应可重解析");
        assert_eq!(
            serde_json::to_string(&m2).unwrap(),
            s,
            "roundtrip 逐字节一致"
        );
        // deny_unknown_fields：未知顶层字段拒绝
        let mut bogus = serde_json::to_value(&m).unwrap();
        bogus
            .as_object_mut()
            .unwrap()
            .insert("bogus_field".into(), serde_json::json!(1));
        assert!(serde_json::from_value::<PluginManifest>(bogus).is_err());
    }

    #[test]
    fn plugin_manifest_minimal_parse_and_defaults() {
        let m = minimal_manifest();
        assert_eq!(m.priority, 100, "缺省 priority 100");
        assert_eq!(m.description, None);
        assert!(m.requires_services.is_empty());
        assert!(m.persistent_fields.is_empty());
        assert!(m.force_include_tools.is_empty());
        assert!(m.export_fields.is_empty());
        assert!(m.config_files.is_empty());
        assert!(m.http_endpoints.is_empty());
        assert!(
            m.granted_capabilities.is_empty(),
            "未声明 → 向后兼容默认全授予"
        );
        assert!(m.state.is_none());
        assert!(m.lifecycle.is_none());
        assert!(m.mcp.is_none());
        assert!(m.provides.is_none());
        assert_eq!(m.requires_content, None);
        assert_eq!(m.activation, None);
        assert_eq!(m.enabled, None);
    }

    #[test]
    fn plugin_lifecycle_serde() {
        let l: PluginLifecycle = serde_json::from_str("{}").unwrap();
        assert_eq!(l.idle_timeout_secs, None, "缺省 = 内核默认");
        let zero: PluginLifecycle = serde_json::from_str(r#"{"idle_timeout_secs":0}"#).unwrap();
        assert_eq!(zero.idle_timeout_secs, Some(0), "0 哨兵 = 永不空闲卸载");
        assert!(!serde_json::to_string(&l)
            .unwrap()
            .contains("idle_timeout_secs"));
    }

    #[test]
    fn native_artifact_serde() {
        let n: NativeArtifact = serde_json::from_str(r#"{"artifact":"my.dll"}"#).unwrap();
        assert_eq!(n.artifact, "my.dll");
        assert_eq!(NativeArtifact::default().artifact, "");
        let text = serde_json::to_string(&n).unwrap();
        let back: NativeArtifact = serde_json::from_str(&text).unwrap();
        assert_eq!(back.artifact, "my.dll");
    }

    #[test]
    fn loaded_plugin_serde_roundtrip() {
        let lp = LoadedPlugin {
            manifest: minimal_manifest(),
            status: PluginStatus::Active,
            loaded_at: Some(
                chrono::DateTime::parse_from_rfc3339("2026-09-13T00:00:00Z")
                    .unwrap()
                    .with_timezone(&chrono::Utc),
            ),
        };
        let text = serde_json::to_string(&lp).unwrap();
        assert!(text.contains("2026-09-13"), "{text}");
        let back: LoadedPlugin = serde_json::from_str(&text).unwrap();
        assert_eq!(back.status, PluginStatus::Active);
        assert_eq!(back.manifest.id, "p");
        assert_eq!(
            back.loaded_at.map(|t| t.to_rfc3339()),
            lp.loaded_at.map(|t| t.to_rfc3339())
        );
    }

    struct BareLoader;

    #[async_trait::async_trait]
    impl PluginLoader for BareLoader {
        async fn discover(&self, _root_paths: &[&str]) -> Result<Vec<PluginManifest>, PluginError> {
            Ok(Vec::new())
        }
        fn validate_manifest(&self, _manifest: &PluginManifest) -> Result<(), PluginError> {
            Ok(())
        }
        async fn load(&self, _plugin_id: &str) -> Result<LoadedPlugin, PluginError> {
            unreachable!("默认面测试不调用")
        }
        async fn unload(&self, _plugin_id: &str) -> Result<(), PluginError> {
            Ok(())
        }
        fn get_status(&self, _plugin_id: &str) -> PluginStatus {
            PluginStatus::Discovered
        }
    }

    #[tokio::test]
    async fn loader_default_methods_contract() {
        let loader = BareLoader;
        // load_config 默认空配置；get_plugin_dir 默认 None（内核 CWD）；get_manifest 默认 None
        assert_eq!(loader.load_config().await.unwrap(), serde_json::json!({}));
        assert_eq!(loader.get_plugin_dir("p"), None);
        assert!(loader.get_manifest("p").is_none());
        assert_eq!(loader.get_status("p"), PluginStatus::Discovered);
    }

    #[test]
    fn message_query_opts_and_session_filter_defaults() {
        let q = MessageQueryOpts::default();
        assert_eq!(q.before_sequence, None);
        assert_eq!(q.after_sequence, None);
        assert_eq!(q.limit, None);
        let f = SessionListFilter::default();
        assert_eq!(f.session_type, None);
        assert_eq!(f.limit, None);
    }
}

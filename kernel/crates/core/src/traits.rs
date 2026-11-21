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

/// trait 默认实现的"不支持"错误（code/source 均空的协议面裸错误）。
fn unsupported_error(message: &str) -> PluginError {
    PluginError {
        message: message.to_string(),
        code: None,
        source: None,
    }
}

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

    /// 插件声明读面（并行 for-each 写集克隆的启用判据）。
    ///
    /// `Some(reads)` = manifest 已发现（reads 可为空）；`None` = 实现不支持
    /// 或插件未发现——调用方保守回退全量克隆（行为与现状一致）。
    /// 默认 `None`：mock/轻实现零负担。
    fn plugin_declared_reads(&self, _plugin_id: &str) -> Option<Vec<String>> {
        None
    }

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
        Err(unsupported_error(
            "reload_member not supported by this invoker",
        ))
    }

    /// 保证订阅者在场（域事件投递前的唤醒原语，ADR 2026-10-28 D5）。
    ///
    /// 背景：域事件广播按宿主去重（[`Self::host_key_of`]，防 N² handler 执行），
    /// 但唤醒动作挂在 `send_lifecycle_hook` 里的 `loader.load` 上——被去重跳过
    /// 的同宿主订阅者永远拿不到唤醒，宿主侧扇出只覆盖**当前在场**成员，事件
    /// 静默丢失（装机 2026-10-28 实证：task_service 空闲卸载期间终态事件全丢，
    /// 上级收不到子任务完成通知）。广播侧据此在去重**之前**对每个声明订阅者
    /// 调用本方法：宿主在跑但成员被摘 → 成员级加回；宿主不在 → 交给正常投递
    /// 路径拉起。
    ///
    /// 语义：幂等（已在场即 no-op）。无成员粒度能力的实现（独占宿主 / Native /
    /// Mock）默认返回 Ok——独占进程的成员恒在场，没有可唤醒的对象。
    async fn ensure_member_present(&self, _plugin_id: &str) -> Result<(), PluginError> {
        Ok(())
    }

    /// 成员粒度卸载（合宿成员从共享宿主进程内摘除，其余成员不动）。
    ///
    /// 仅对「已装箱到合宿组宿主、有存活进程且非最后成员」的成员生效：向宿主发
    /// `agentos/unload_member` 请求（带应答），宿主定向 on_unload 并从服务面
    /// 摘除该成员（ADR 2026-09-24-member-granularity-unload-mode-panel-warmup）。
    /// 无存活宿主 = 无进程内服务面，返回 Ok(no-op)；独占插件、组内最后成员
    /// （须走整组回收）、实现不支持、请求失败（宿主协议错误/超时）返回 Err
    /// ——调用方必须回退 [`Self::force_unload`] 整组路径。
    /// 默认实现返回不支持错误（无此能力的实现 / MockInvoker 编译兼容）。
    async fn unload_member(&self, _plugin_id: &str) -> Result<(), PluginError> {
        Err(unsupported_error(
            "unload_member not supported by this invoker",
        ))
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
        Err(unsupported_error(
            "list_plugin_tools not supported by this invoker",
        ))
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

    /// 目标插件所在宿主是否正处于 spawn/respawn 窗口（连接 + initialize 握手
    /// 未完成，调用方只能排队等它就绪）。
    ///
    /// 供 HTTP dispatcher 区分「插件处理慢」与「基础设施冷启动等待」：starting
    /// 期间端点超时预算暂停消耗（等待就绪而非 504，宿主重载风暴 2026-09-25）。
    /// 默认返回 false——无 spawn 概念的实现（InProcess / 测试桩）语义即「不在
    /// 启动窗口」，dispatcher 行为与无探针时完全一致。
    fn is_host_starting(&self, _plugin_id: &str) -> bool {
        false
    }

    /// 请求路径整组并行 respawn：管道链撞死宿主后一次并行拉起全链（Phase 3 #1）。
    ///
    /// 语义：对 `plugin_ids` 中 sidecar 宿主已死/未 spawn 的，有界并行触发
    /// spawn→连接→initialize→on_load 全程并等其完成（=就绪等待，调用方拿到
    /// 返回即全链可服务）；存活宿主命中缓存 no-op。失败隔离：单宿主拉起失败
    /// 只记日志不拖死整组——失败插件的调用路径懒 spawn 兜底（既有语义）。
    /// 不动 keep-warm 常驻集与 idle GC 治理（拉起的宿主照常受空闲回收）。
    /// 非 sidecar / 无 manifest 的 id 跳过。
    /// 默认 no-op（无 spawn 概念的实现 / MockInvoker 不破坏编译）。
    async fn ensure_hosts_ready(&self, _plugin_ids: &[String]) {}

    /// 宿主盒子快照（只读枚举面，`GET /api/v1/plugins/hosts` 数据源）。
    ///
    /// 按宿主进程枚举盒子（进程态/成员集/在飞调用/启动窗口/待装载差集），
    /// 全部由实现方既有记账状态同步组装——只读语义：不 spawn、不驱逐、
    /// 不 touch 记账。响应 serde 字段名冻结（前端宿主盒子视图逐字消费）。
    /// 默认返回空快照——无宿主概念的实现（MockInvoker/测试桩）语义即「无盒子」。
    async fn host_boxes_snapshot(&self) -> HostBoxesSnapshot {
        HostBoxesSnapshot::default()
    }
}

#[async_trait]
pub trait CapabilityRegistry: Send + Sync {
    /// 注册插件提供的工具。
    fn register_tool(&self, plugin_id: &str, tool: ToolDescriptor);

    /// 注销插件提供的工具。
    fn unregister_tools(&self, plugin_id: &str);

    /// 登记宿主领地（F-min 属地路由）：plugin_id → allowed_roots。
    /// 缺省空实现 = 该注册表不参与属地路由（测试桩零负担）。
    fn register_host(&self, _plugin_id: &str, _allowed_roots: Vec<String>) {}

    /// 按工作空间属地解析工具实例：同名多实例时，workspace 前缀命中某宿主
    /// 领地 → 返回该宿主实例；否则返回非宿主主实例（既有语义）。单实例 =
    /// 原样返回。缺省实现 = `get_tool`（主实例直查，行为与属地路由上线前
    /// 逐字节一致）。
    fn resolve_tool_instance(
        &self,
        name: &str,
        _workspace: Option<&str>,
    ) -> Option<ToolDescriptor> {
        self.get_tool(name)
    }

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
    /// 隔离执行能力声明（manifest `capabilities.tools[].isolation_capable`），
    /// 经 tool-surface.schemas 契约表下发 guard；缺省 = 未声明。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub isolation_capable: Option<bool>,
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

    /// 服务条目（`ns.method` 点式）→ 声明提供方插件 id 列表（R7 F3 声明面
    /// 依赖闭包）。附加式默认空（契约冻结惯例：mock/轻实现零负担）。
    ///
    /// 语义边界：只认点式精确声明（调用方点了名它运行时真实调用的服务）；
    /// bare 命名空间条目不在本方法语义内——那是注册闸的存在性保证（全量
    /// `packaging` 样板声明），按它展开会把无关提供方拖进每次 run。
    fn service_provider_ids(&self, _service: &str) -> Vec<String> {
        Vec::new()
    }
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

mod hostbox;
mod manifest;
mod storage;

pub use hostbox::*;
pub use manifest::*;
pub use storage::*;

#[cfg(test)]
mod tests;

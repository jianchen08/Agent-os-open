//! PluginManifest 及其能力声明域（插件协议的数据面）。

use super::*;
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
    /// state 持久写面声明（ADR 2026-10-02）：本插件允许经
    /// `pipeline-state.update` 写入的 state 键。条目支持精确键
    /// （`task.status`）与 `前缀.*` 通配（`task.owned.*`），语义同
    /// `export_fields`。内核按调用方 `_plugin_id`（invoker 注入的信任锚点）
    /// 查本声明，未声明即拒绝——内核只执行"声明才可写"机制，不解释键所属域。
    /// 空 = 无持久写权限（该插件不得经此通道写 state）。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub writes: Vec<String>,
}

/// 插件可见性（manifest `visibility` 字段，private 插件机制）。
///
/// 语义（ADR 2026-11-18-llm-two-tier-plugin-convergence 决策 5）：private 是
/// **分发与可见性属性，不是能力属性**——private 插件随仓内置分发，内核同链路
/// 装载/注册/执行（工具/服务/http_endpoints/contributes 全同），仅不进用户
/// 插件安装/管理面（前端插件管理页按本字段过滤）。
///
/// 兼容约定：字段只增不删。缺省（旧 manifest 无该字段）与未知取值一律按
/// [`PluginVisibility::Public`] 处理——未知取值告警留痕而非拒载（插件清单
/// 先行于内核更新的发布窗口内不得破坏装载）；字段名写错仍由
/// `deny_unknown_fields` 拒绝。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum PluginVisibility {
    /// 公开（缺省）：进用户插件管理面。
    #[default]
    Public,
    /// 私有：随仓内置分发，不进用户插件管理面。
    Private,
}

impl PluginVisibility {
    /// 是否缺省可见——`skip_serializing_if` 谓词：public 不落序列化字段
    /// （既有插件 serde 面字节不变）。
    pub fn is_public(&self) -> bool {
        *self == Self::Public
    }
}

impl<'de> Deserialize<'de> for PluginVisibility {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let raw = Option::<String>::deserialize(deserializer)?;
        Ok(match raw.as_deref() {
            Some("private") => Self::Private,
            Some("public") | None => Self::Public,
            Some(other) => {
                // 字段只增不删：未来新增取值回落 public，插件装载不中断。
                tracing::warn!(
                    value = other,
                    "manifest visibility 取值未知，按 public 处理（字段只增不删的宽容读口径）"
                );
                Self::Public
            }
        })
    }
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
    /// 只要注册即注入 LLM 工具面的工具 id 集合（基础设施工具面通用机制，
    /// 当前出厂插件零使用）。内核收集全部插件声明并集参与 tool-surface
    /// 过滤；空 = 无强制注入要求。
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
    /// 辅助 venv 声明（供给统一面，ADR 2026-09-28-aux-venv-autoprovision）。
    ///
    /// mcp 1.x/2.0 生态互斥等依赖冲突插件（hindsight 双 venv）声明插件目录
    /// 内的第二个解释器环境；内核 boot autoprovision 与主 `.venv` 同窗口
    /// 装配（`uv venv` + `uv pip install -r requirements`，含用户空间重定向
    /// ——插件侧不再自建二道供给，与其他插件装机首启行为统一）。
    /// 缺省 = 无辅助 venv。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub aux_venvs: Vec<AuxVenvDecl>,
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
    /// 危险前端能力被准入系统剥除的记录（2026-09-25 准入分级）。
    ///
    /// 非空 = 本插件清单声明了宿主级危险能力（skin hooks=host_js /
    /// client_styles=host_css）但未获 allowlist `grants` 授予，loader 已在注册面
    /// 剥除对应声明（磁盘清单原样，重扫重算）；经 /api/v1/plugins 出口供设置页
    /// 可见（禁静默降级）。空 = 无剥权（内置根 / dev 姿态 / 已授予 / 未声明）。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub restricted_capabilities: Vec<String>,
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
    /// 可见性（private 插件机制，ADR 2026-11-18-llm-two-tier-plugin-convergence）。
    ///
    /// `private` = 随仓内置分发、内核同链路装载、契约与普通插件全同，仅不进
    /// 用户插件安装/管理面；缺省 = 可见（旧 manifest 无该字段即 public）。
    /// 序列化面 public 不落字段（既有插件 serde 字节不变）。
    #[serde(default, skip_serializing_if = "PluginVisibility::is_public")]
    pub visibility: PluginVisibility,
}

impl PluginManifest {
    /// 本插件作为被调用者的声明需求上界（秒）——M2 单声明派生单点（ADR
    /// 2026-11-05-timeout-taxonomy-and-merge）：`capabilities.call_timeout_seconds`
    /// ∪ `mcp.request_timeout_secs` 取最大（两键任一在场即生效，0 值哨兵按
    /// 未声明）。消费面：A3 传输界（invoker host_acquire）、A4 工具界回退
    /// （invoker `tool_timeout_ms`）、L2 嵌套校验的内层需求（api
    /// timeout_nesting）。两键全缺 = `None`（各消费面落各自缺省档）。
    ///
    /// 派生语义：传输单次往返的响应在整次调用结束才返回（含单请求内逐跳
    /// 兜底），故传输界须 ≥ 声明需求——单声明（call_timeout_seconds）在场
    /// 即同时派生传输界与工具界，插件级 `mcp.request_timeout_secs` 仅作为
    /// 未声明 call_timeout_seconds 的既有声明面继续生效（llm_core 900<1800
    /// 倒挂的解析面修复点）。
    pub fn declared_call_need_secs(&self) -> Option<u64> {
        let call_need = self.capabilities.call_timeout_seconds.filter(|s| *s > 0);
        let transport = self
            .mcp
            .as_ref()
            .and_then(|m| m.request_timeout_secs)
            .filter(|s| *s > 0);
        match (call_need, transport) {
            (Some(a), Some(b)) => Some(a.max(b)),
            (a, b) => a.or(b),
        }
    }
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

/// 仅 traits 子树可见（sibling tests.rs 经 glob 再导出消费）；外置 pub(crate)
/// 会被私有 mod 的可见性封顶，属冗余（clippy::redundant_pub_crate 执法面）。
pub(super) fn default_priority() -> u32 {
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
    /// 异步预派发声明（native 同步 ABI 的跨进程等待卸载）。
    ///
    /// 声明后，内核在进入 cdylib 的同步 `execute` 之前，先以 async 方式调用
    /// 指定 capability/method（如 `tool-executor.invoke`），把结果作为
    /// `_pre_decided_results` 注入插件 config；插件只做整形/校验，不再在
    /// 同步桥里 `block_on` 等待跨进程返回。缺省 = 插件自行同步调用（既有
    /// 行为）；声明即内核契约：结果未预派发时插件必须 fail-closed。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pre_dispatch: Option<PreDispatchSpec>,
}

/// native 插件的异步预派发目标（capability/method 对）。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PreDispatchSpec {
    pub capability: String,
    pub method: String,
}

/// 单次插件调用缺省上界（秒）——跨 crate 单一真值：invoker 基础策略档
/// （manifest 未声明时落此值）与引擎测试锚同源。语义见
/// [`ManifestCapabilities::call_timeout_seconds`]。
pub const DEFAULT_PLUGIN_CALL_TIMEOUT_SECS: u64 = 600;

/// 单次调用超时错误码（`_plugin_errors[].code`）：与 run 级 timeout 署名
/// （`router.stop_reason="timeout"`，FAILED_STOP_REASONS 既有词）分面可辨
/// ——前者步级错误信封、run 继续推进；后者 run 终态签名。invoker 基础策略
/// 层与引擎预算封顶层共用同码（错误面单点可辨）。
pub const STEP_CALL_TIMEOUT_CODE: &str = "STEP_CALL_TIMEOUT";

/// Manifest 能力声明（运行时表示）。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ManifestCapabilities {
    /// 给 LLM 的工具声明——**声明即注册**（D.6 槽位拆分）：不按 plugin_type
    /// 门控，声明即进 CapabilityRegistry 暴露给 LLM 面；多职能插件
    /// （tools + services 并存）自然成立。
    #[serde(default)]
    pub tools: Vec<ToolCapability>,
    /// 内核/插件间服务方法声明（D.6 槽位拆分）：不进 LLM 面；调用走既有通道——
    /// invoke_entry（管道）/ http_endpoints（面板）/ tool-executor 显式 plugin_id
    /// （跨插件）/ provides 命名空间；wire 协议不变（MCP tools/call，ADR D.3）。
    #[serde(default)]
    pub services: Vec<ServiceCapability>,
    /// 管道步骤服务声明（2026-08-27 §3.1）：步骤为显式注册服务
    /// （capabilities.steps），引用与实现解耦（G10 第③级命中）；与
    /// tools/services 并存合法。step 名全局唯一（编译期查重，冲突启动报错）；
    /// 不含 phase/orchestration 字段（被否方案 #1：编排位置属管道配置）。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub steps: Vec<StepCapability>,
    /// `resources` 槽已退役；能力键 fail-closed（deny_unknown_fields 解析期
    /// 拒绝未知键）。
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
    /// 指标分类声明（K2 声明化）：引擎调度层监控的归组类别；合法值单一
    /// 真值源 = `agentos_engine::pipeline_loop` 的 `METRICS_CLASS_*` 常量组
    /// （"llm"/"tool"）。未声明/非法值 = Other 兜底（不计入 LLM/tool 指标，
    /// 仅可观测债务非行为故障）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub metrics_class: Option<String>,
    /// 插件署名停机词声明（停机词声明化 ADR 2026-10-15）：本插件写入
    /// `router.stop_reason` 的失败署名词，经 watcher sync 全量重建进 core
    /// 声明注册表参与 Failed 折算；Completed 收束署名无须声明（缺省兜底）。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub stop_reasons: Vec<String>,
    /// 单次插件调用上界声明（秒；ADR 2026-10-15-plugin-call-timeout 的插件级
    /// 延伸刀）：声明值替换引擎缺省档（600s）作为该插件单次调用的 base 上界。
    /// 大模型调用类插件（上下文压缩多批摘要、长流式）合法时长超缺省档，经此
    /// 自报；state 配置档 `plugin_call_timeout_seconds` 与 run 预算档仍在场取
    /// min——配置只能收紧、预算恒封顶，「所有路径恒有界」不变量不破。
    /// 未声明 = 缺省档；0 值非法（无界单次调用即冻结根因本身），装配点
    /// warn 后按未声明处理落缺省档。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub call_timeout_seconds: Option<u64>,
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
/// 辅助 venv 声明条目（`PluginManifest::aux_venvs`，供给统一面
/// ADR 2026-09-28-aux-venv-autoprovision）。
///
/// 依赖互斥插件（如 hindsight：API 栈 mcp 1.x 与宿主 SDK mcp 2.x 不共容）
/// 的第二解释器环境；内核 boot autoprovision 按 `uv venv` +
/// `uv pip install -r requirements` 装配（插件目录只读时重定向用户空间
/// 登记处 `plugin-venvs/<id>--<dir 净化名>`）。目录与清单名只允许
/// `[A-Za-z0-9._-]` 且不得含路径分隔（防穿越，provision 侧校验）。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AuxVenvDecl {
    /// venv 目录名（插件目录内，如 `.venv-hindsight`）。
    pub dir: String,
    /// 依赖清单文件名（插件目录内，随包分发，如 `requirements.txt`）。
    pub requirements: String,
    /// Python 版本（`uv venv --python` 取值，如 `3.12`）。
    pub python: String,
}

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
    /// 隔离执行能力声明：该工具知道如何消费 `_container_id`/`_exec_backend`
    /// （isolation_guard 服务端权威注入）在隔离环境内执行。声明与否不影响
    /// guard 的隔离裁决（只降不升），仅决定是否注入 `_exec_backend`。
    /// 缺省 = 未声明（不注入）。经 tool-surface.schemas 契约表下发，消费方
    /// 见 isolation_guard（ADR 2026-10-21-tool-isolation-capability-declaration）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub isolation_capable: Option<bool>,
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
///
/// `deny_unknown_fields`：权限名拼错（`allow_hosts` / `networks` / `read_path`
/// 等）必须解析期报错。缺此约束时 serde 静默丢弃未知键 → 插件"看起来声明了
/// 权限、实际声明为空"，靠日志留痕的声明面直接失真（且空声明与"没声明"在
/// 日志上不可辨）。权限声明当前只做留痕、不做强制，声明失真即审计证据失真
/// ——见 ADR 2026-11-16-plugin-permission-surface。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
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

/// 文件系统权限。`deny_unknown_fields`：同上，键拼错即报错不静默丢。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FilesystemPermission {
    #[serde(default)]
    pub read_paths: Vec<String>,
    #[serde(default)]
    pub write_paths: Vec<String>,
}

/// 网络权限。`deny_unknown_fields`：同上，键拼错即报错不静默丢。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
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
    /// 宿主领地声明（F-min 属地路由，设计文档 §53）：该插件承载的宿主机暴露
    /// 给云内核的工作空间根目录清单（绝对路径）。非空 = 该插件是宿主实例
    /// 载体，注册表据此前缀匹配工作空间属地、裁决同名工具的实例选择（同名
    /// 同工具：异名即异工具，命名空间化已被否）。空/缺省 = 非宿主插件，
    /// 不参与属地路由。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub allowed_roots: Vec<String>,
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

//! 宿主盒子（HostBox）快照与插件生命周期钩子契约（观测面 serde 字段名冻结）。

use super::*;
// ── 3b. 宿主盒子快照（观测面契约，serde 字段名冻结） ────────────

/// 宿主盒子类别（`kind` 字段）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum HostBoxKind {
    /// 组宿主（`group:{组名}:{槽位}`，合宿进程承载多成员）
    Group,
    /// 独占宿主（`plugin:{plugin_id}`，单插件单进程）
    Solo,
}

/// 宿主盒子成员行（`members` 数组元素）。
#[derive(Debug, Clone, Serialize)]
pub struct HostBoxMember {
    pub plugin_id: String,
    /// 成员在分配表但不在 spawned_members 快照（成员粒度软卸载后的待重加入
    /// 标记；组宿主 spawn 快照缺失时按不在处理，与漂移检测 fail-closed 同口径）
    pub pending_rejoin: bool,
}

/// 宿主盒子行（一个宿主进程一枚）。
#[derive(Debug, Clone, Serialize)]
pub struct HostBox {
    pub host_key: String,
    pub kind: HostBoxKind,
    /// stdio 宿主的 OS 进程号；HTTP transport / 未连接 = null
    pub pid: Option<u32>,
    /// 未判死即 true（判死门控与进程态轮询同款）
    pub alive: bool,
    /// 宿主进程私有提交内存（MB；「提交大小」口径，含已换出私有页）。由 api 侧按
    /// pid 富化（跳板下探 + 私有提交采集面）；无 pid（HTTP transport）恒 null。
    pub commit_mb: Option<f64>,
    /// 宿主进程运行秒数（spawn 时刻起算；无记账 = null）
    pub uptime_secs: Option<u64>,
    /// spawn 时刻成员集快照（进程实际服务面；独占宿主 = 键内嵌插件自身）
    pub spawned_members: Vec<String>,
    /// 当前成员集（分配表按宿主分组；独占 = 键内嵌插件）
    pub members: Vec<HostBoxMember>,
    /// 当前挂载成员数（分配表口径，与装箱判定同源；独占宿主 = 1）
    pub slot_used: u64,
    /// 挂载成员上限（组宿主 = `AGENTOS_LIGHT_HOST_MAX_MEMBERS` 快照时刻值；
    /// 独占宿主恒 1，结构性单成员不可扩）——slot_used/slot_cap 同现，装箱
    /// 余量观测面（合宿扩容观测用）。
    pub slot_cap: u64,
    /// 宿主级在飞 capability 调用数
    pub in_flight: u64,
    /// 有在飞调用的成员 → 调用数（零调用成员不列）
    pub member_in_flight: HashMap<String, u64>,
    /// 成员最近调用时刻的最大值（epoch 秒；null = 从未被调用）
    pub last_call_at: Option<f64>,
    /// 成员正处于 spawn/respawn 窗口
    pub starting: bool,
    /// 处于 spawn/respawn 窗口的成员
    pub starting_members: Vec<String>,
}

/// 待装载插件行（`pending_spawn` 数组元素）。
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct PendingSpawnEntry {
    pub plugin_id: String,
    /// loader 已知（manifest 仍可解析）；记账残留（manifest 已不可得）= false
    pub enabled: bool,
    /// 最近调用时刻（epoch 秒；null = 从未被调用）
    pub last_call_at: Option<f64>,
}

/// 宿主盒子快照（`GET /api/v1/plugins/hosts` 响应契约，字段名冻结）。
#[derive(Debug, Clone, Default, Serialize)]
pub struct HostBoxesSnapshot {
    /// 在册宿主盒子（按 host_key 排序）
    pub hosts: Vec<HostBox>,
    /// 已知插件中「既无分配表条目也无存活宿主」的差集（按 plugin_id 排序）
    pub pending_spawn: Vec<PendingSpawnEntry>,
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
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct HookContext {
    /// 标签集合：key → value
    tags: HashMap<String, serde_json::Value>,
}

impl HookContext {
    /// 创建空的标签化上下文（与 `Default::default` 同一语义）。
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

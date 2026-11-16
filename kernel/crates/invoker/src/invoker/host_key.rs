//! 宿主键与合宿装箱纯函数族：组/宿主键构造与解析、成员判定、装箱状态、
//! 插件指纹、反向调用身份命名空间与 per-plugin 能力路由器。
//!
//! 自 invoker.rs 按关注点纯搬运拆出（2026-10-12 文件长度机械闸首个应用案例，
//! ADR docs/decisions/2026-10-12-file-length-mechanical-gate.md）；
//! 方法簇的 impl 块分布见同目录各子模块。

use std::path::Path;
use std::time::SystemTime;

use agentos_mcp::McpError;
use serde_json::json;

use super::*;

/// Pull 热加载的检测 TTL：缓存进程在 1 秒内不重复 stat 指纹，热路径零额外开销。
/// TTL 过期后才 stat 插件目录关键文件 mtime，发现变化才 kill 旧进程 respawn。
/// （未用到也不检测——纯按需 pull。）
pub(super) const PLUGIN_FINGERPRINT_TTL: Duration = Duration::from_secs(1);

/// 轻量合宿：manifest.host_group 声明即准入（合宿进程模型 §4.1 的多组扩展）。
/// 组名任意（`light` = 易变/通用组，`light_stable` 等独立组名承载稳定观察面），
/// 装箱槽位按组名分域（`group:{组名}:{n}`），驱逐连坐半径 = 本组宿主。
/// 白名单制担保语义不变：声明即插件作者担保「无同步阻塞调用、无 C 扩展、
/// 无重依赖」，内核不做推断。
pub(super) const DEFAULT_HOST_GROUP: &str = "light";

/// 合宿宿主目录名（plugins/shared/_host/，host.py 由宿主侧任务承载）。
///
/// pub：该名同时是共享宿主 venv 在用户空间登记处的键（`<USER_ROOT>/
/// plugin-venvs/_host`，ADR 2026-09-28-venv-user-space-provisioning）——
/// api::venv_provision 重定向共享宿主重建目标时复用同一常量，键形单源。
pub const GROUP_HOST_DIR: &str = "_host";
/// 宿主启动窗（spawn 握手窗）超时：`connect()` + `initialize()` 全程的独立
/// 时间界（ADR 2026-09-28），与 `request_timeout`（默认 86400s，审批长等待族）
/// 彻底解耦——启动窗是基础设施冷启动等待，不是业务调用等待；若同源复用
/// 24h 界，sidecar 启动失败会钉死 per-host spawn 锁至 24h。
/// 默认 120s 盖过最坏冷启动（WSL bash 冷启 9~15s、合宿 respawn 2-6s、重 import
/// 插件数秒级）约一个数量级；环境变量 `AGENTOS_MCP_START_TIMEOUT_SECS` 覆盖
/// （解析非法——缺失/非数字/≤0——一律回退默认，与 `light_host_max_members`
/// 同款惯例）。超时即显式 kill 半开客户端并返回 `HOST_START_TIMEOUT`
/// （fail-fast 可恢复，下次调用照常重试 respawn，不标记熔断——熔断归批次 B）。
const DEFAULT_HOST_START_TIMEOUT_SECS: u64 = 120;

/// 读取宿主启动窗超时（环境变量优先，全部宿主共用同一窗口）。
pub(super) fn host_start_timeout() -> Duration {
    std::env::var("AGENTOS_MCP_START_TIMEOUT_SECS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .filter(|n| *n > 0)
        .map(Duration::from_secs)
        .unwrap_or(Duration::from_secs(DEFAULT_HOST_START_TIMEOUT_SECS))
}

/// 合法组名字符集（进宿主键/日志/spawn 参数）：ASCII 字母数字下划线连字符，
/// ≤32 字符。非法值一律独占（保守，与缺省同）。
pub(super) fn is_valid_group_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 32
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

/// 手机档单宿主挂载成员上限（原则 P2 统一最小宿主：手机 1-2 宿主，全体成员
/// 装入）。1024 ≥ 现役声明面全集（light 组声明 67），装箱槽位结构性
/// `ceil(n/1024) ∈ [1,2]`——上限兜底而非常规取值，槽容量随档在
/// [`light_host_max_members`] 派生，不再依赖部署面手设环境变量。
pub(super) const PHONE_LIGHT_HOST_MAX_MEMBERS: usize = 1024;

/// 桌面/云档单宿主挂载成员上限：桌面合宿收敛现实（槽容量 20 + pipeline 组
/// 归并 light → 2×light + light_stable + longwait = 4 宿主；桌面合宿收敛台账
/// §7.3 dev 四判据验证过、§7.4 装机已 rollout）。环境变量
/// `AGENTOS_LIGHT_HOST_MAX_MEMBERS` 仍可覆盖（运维逃生口，值不变行为不变）。
pub(super) const DESKTOP_LIGHT_HOST_MAX_MEMBERS: usize = 20;

/// 档位 → 槽容量（纯函数；环境变量覆盖外的默认派生）。
pub(super) fn slot_capacity_for_tier(tier: agentos_core::device_profile::DeviceTier) -> usize {
    match tier {
        agentos_core::device_profile::DeviceTier::Phone => PHONE_LIGHT_HOST_MAX_MEMBERS,
        agentos_core::device_profile::DeviceTier::Desktop
        | agentos_core::device_profile::DeviceTier::Cloud => DESKTOP_LIGHT_HOST_MAX_MEMBERS,
    }
}

/// 读取合宿宿主挂载成员上限：环境变量优先（全组名共用同一上限）；未设置时
/// 按设备档位派生（P2：手机全成员装入 / 桌面收敛值 20）。
pub(super) fn light_host_max_members() -> usize {
    std::env::var("AGENTOS_LIGHT_HOST_MAX_MEMBERS")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .filter(|n| *n > 0)
        .unwrap_or_else(|| {
            slot_capacity_for_tier(agentos_core::device_profile::detect_device_tier())
        })
}

/// 声明组名 → 装箱域：手机档把全部组名（light_stable/longwait 等桌面隔离域）
/// 折入 [`DEFAULT_HOST_GROUP`] 单域（P2 手机 1-2 宿主的达成前提——分组域不折
/// 叠则组数即宿主数下界，1-2 不可达）；桌面/云档按声明组分域不变（隔离语义
/// ADR 2026-09-29 / BUG-3 实证诉求在桌面档保留）。装箱机制本身零改动。
pub(super) fn cohost_packing_domain(
    group: &str,
    tier: agentos_core::device_profile::DeviceTier,
) -> &str {
    match tier {
        agentos_core::device_profile::DeviceTier::Phone => DEFAULT_HOST_GROUP,
        _ => group,
    }
}

/// 组宿主键：`group:{group}:{slot}`（slot 从 1 起，按组内装箱顺序分配；
/// 回收后槽位可复用）。
pub(super) fn group_host_key(group: &str, slot: u64) -> String {
    format!("group:{group}:{slot}")
}

/// 从宿主键解析组名与槽位号；非组宿主键返回 None。
/// 组名合法字符集不含冒号，`rsplit_once` 足以切分。
pub(super) fn parse_group_slot(host_key: &str) -> Option<(&str, u64)> {
    let rest = host_key.strip_prefix("group:")?;
    let (group, slot) = rest.rsplit_once(':')?;
    Some((group, slot.parse().ok()?))
}

/// 独占宿主键：`plugin:{plugin_id}`（每插件独占宿主，现状语义统一走宿主键路径）。
pub(super) fn solo_host_key(plugin_id: &str) -> String {
    format!("plugin:{plugin_id}")
}

/// 合宿成员装箱状态（合宿进程模型 §4.5，多组扩展）。
///
/// - `assignments`：分配表 {plugin_id → host_key}。粘性：成员一旦分配，宿主
///   存活期间归属不变（respawn 按表重建成员集）；宿主被 idle GC 回收时其全部成员
///   条目随之清除（槽位释放复用）；manifest 声明组名变更后旧条目跨组过期，
///   装箱判定按当前声明组重装箱（成员搬组，旧宿主成员集收缩）。
///   容器选型裁定（2026-11-01 热修回退）：保持 HashMap——装箱判定对本表迭代序
///   结构性免疫（槽位计数=数值聚合、派生面全走排序拷贝，见
///   `light_packing_decisions_are_insertion_order_stable` 测试锁定）；键序确定性
///   属打包期公理，待设备侧 boot 基线复验后再评估换 BTreeMap。
/// - `next_slot`：各组名已开过的最大槽位号（只增，按组记账）。装箱时从低槽位
///   找「当前挂载数 < 上限」的宿主塞入——已回收宿主（成员条目已清、计数归 0）
///   自然优先复用，而不是无限开新槽位；全满才开新槽位。
#[derive(Default)]
pub(super) struct LightPacking {
    pub(super) assignments: HashMap<String, String>,
    pub(super) next_slot: HashMap<String, u64>,
}

/// 合宿成员判定：host_group 声明合法组名 且 sidecar 且非外部 MCP（多组扩展）。
///
/// 外部 MCP（StreamableHttp 远端 / stdio 第三方命令）的进程归外部所有，
/// 内核只是客户端（方案 §〇 三类宿主形态表），不进合宿组；host_type=InProcess
/// 天生单进程无独立内存底座，同样不适用。缺省或非法组名一律独占（保守）。
pub fn is_cohost_member(manifest: &PluginManifest) -> bool {
    let Some(group) = manifest.host_group.as_deref() else {
        return false;
    };
    if !is_valid_group_name(group) || manifest.host_type != HostType::Sidecar {
        return false;
    }
    let is_external = match manifest.mcp.as_ref() {
        Some(cfg) => match cfg.transport {
            agentos_core::traits::McpTransport::StreamableHttp => {
                cfg.endpoint.as_ref().and_then(|e| e.url.as_ref()).is_some()
            }
            agentos_core::traits::McpTransport::Stdio => cfg
                .endpoint
                .as_ref()
                .and_then(|e| e.command.as_ref())
                .is_some(),
        },
        None => false,
    };
    !is_external
}

/// 反向调用连接身份（metrics 命名空间）：合宿 = 宿主键；独占 = manifest.id。
/// 身份取进程而非成员，保证不随组重生换人。
pub(super) fn scoped_router_identity(manifest: &PluginManifest, host_key: &str) -> String {
    if is_cohost_member(manifest) {
        host_key.to_string()
    } else {
        manifest.id.clone()
    }
}

/// 合宿成员的 MCP 工具名命名空间（§4.2 第 3 条）：宿主把成员插件的每个工具
/// 注册为 `{plugin_id}.{tool_name}`，调用侧拼前缀分发；独占宿主无前缀（现状不变）。
pub(super) fn namespaced_tool_name(manifest: &PluginManifest, tool_name: &str) -> String {
    if is_cohost_member(manifest) {
        format!("{}.{}", manifest.id, tool_name)
    } else {
        tool_name.to_string()
    }
}

/// 从成员插件目录向上找合宿宿主目录（含 `_host` 子目录的最近祖先）。
///
/// 插件目录层级不固定（plugins/shared/<type>/<phase>/<name>），不能按固定
/// 深度回溯；逐级向上探测 `_host` 子目录，到文件系统根仍无则 None。
/// pub 供 venv 自愈（venv_provision）复用同一探测序，避免两处定位漂移。
pub fn find_group_host_dir(member_dir: &Path) -> Option<std::path::PathBuf> {
    let mut cur = Some(member_dir);
    while let Some(dir) = cur {
        let candidate = dir.join(GROUP_HOST_DIR);
        if candidate.is_dir() {
            return Some(candidate);
        }
        cur = dir.parent();
    }
    None
}

/// 内置根（插件装载根 `AGENTOS_PLUGINS_DIR`，获取方式与 collect_plugin_roots
/// 同源）下的合宿宿主目录：`<内置根>/_host/`（host.py 与共享 venv 的仓库原件）。
///
/// 仅作 find_group_host_dir 探测全空时的回退定位（见 resolve_group_host_command），
/// 不参与常规探测序。pub 供 venv 自愈（venv_provision）复用同一定位序。
pub fn builtin_group_host_dir() -> Option<std::path::PathBuf> {
    let root = std::env::var("AGENTOS_PLUGINS_DIR")
        .ok()
        .filter(|d| !d.trim().is_empty())?;
    let candidate = Path::new(&root).join(GROUP_HOST_DIR);
    candidate.is_dir().then_some(candidate)
}

/// 计算插件指纹：对该插件目录下的**源码文件** + plugin.json 声明的 config_files 路径
/// 取 mtime（秒级精度），拼接为字符串后做简单 hash。
///
/// 设计权衡：
/// 插件源码/配置指纹（mtime 基）。invoker 的 respawn 判定与 watcher 的 G2
/// 复验触发共用同一把指纹——两边对"代码变更"的判定必须同源，否则会出现
/// respawn 了但复验不触发（或反之）的判据分叉。
///
/// - 用 mtime 而非内容 hash：stat 是微秒级，内容 hash 要读全部文件（毫秒级），
///   热路径上 stat 性能可接受，mtime 精度足够捕获代码/配置修改。
/// - **只扫源码文件**（.py/.rs/.js/.ts/.wasm/.json/.yaml/.yml/.toml），跳过运行时
///   产生的杂物（.log/.pyc/__pycache__/临时文件）。否则 sidecar 运行时若在插件目录
///   写入临时文件（如诊断日志），会误触发 respawn。
/// - config_files 指向的配置文件可能在插件目录外（如 config/models/llm.yaml），
///   单独 stat 纳入指纹，配置变更也能触发 respawn。
pub fn compute_plugin_fingerprint(plugin_dir: &Path, manifest: &PluginManifest) -> u64 {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::Hasher;
    let mut hasher = DefaultHasher::new();

    let mtime_str = |p: &Path| -> String {
        std::fs::metadata(p)
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.duration_since(SystemTime::UNIX_EPOCH).ok())
            .map(|d| format!("{}:{}", d.as_secs(), d.subsec_nanos()))
            .unwrap_or_else(|| "0:0".to_string())
    };

    // 只纳入源码/配置扩展名，跳过 .log/.pyc 等运行时产物。
    const SOURCE_EXTS: &[&str] = &[
        "py", "rs", "js", "ts", "jsx", "tsx", "wasm", "json", "yaml", "yml", "toml", "txt", "md",
    ];
    let is_source = |name: &str| -> bool {
        name.rsplit_once('.')
            .map(|(_, ext)| SOURCE_EXTS.contains(&ext.to_lowercase().as_str()))
            .unwrap_or(false)
    };

    // 扫插件目录下的源码文件（非递归，跳过子目录如 __pycache__）
    if let Ok(entries) = std::fs::read_dir(plugin_dir) {
        let mut paths: Vec<_> = entries
            .filter_map(|e| e.ok())
            .filter_map(|e| {
                let p = e.path();
                if !p.is_file() {
                    return None;
                }
                let name = p.file_name()?.to_string_lossy().to_string();
                // 跳过隐藏文件（.开头，如运行时产生的临时日志）和非源码扩展名
                if name.starts_with('.') || !is_source(&name) {
                    return None;
                }
                Some((name, mtime_str(&p)))
            })
            .collect();
        paths.sort_by(|a, b| a.0.cmp(&b.0));
        for (name, mtime) in paths {
            hasher.write(name.as_bytes());
            hasher.write(b"|");
            hasher.write(mtime.as_bytes());
            hasher.write(b";");
        }
    }

    // 声明的 config_files 路径（可能在插件目录外，如 config/models/llm.yaml）
    // path 是相对 config/ 的，需结合 config_root 解析——但 invoker 无 config_root，
    // 这里对存在的绝对/相对路径直接 stat，不存在则跳过（降级：配置变更靠目录内文件指纹）。
    for mapping in &manifest.config_files {
        let p = Path::new(&mapping.path);
        let mtime = mtime_str(p);
        hasher.write(mapping.id.as_bytes());
        hasher.write(b"|");
        hasher.write(mtime.as_bytes());
        hasher.write(b";");
    }

    // 项目根 .env 指纹：API Key 更新走 .env（写 .env 不一定改变 llm.yaml
    // 内容——占位符已是 ${VAR} 时 yaml 不变），必须靠 .env mtime 触发
    // sidecar 热重启，respawn 时经 env_delta_overlay 拿到新 key。
    if let Some(env_path) = agentos_mcp::env_file::project_env_path() {
        hasher.write(b".env|");
        hasher.write(mtime_str(&env_path).as_bytes());
        hasher.write(b";");
    }

    hasher.finish()
}

/// 成员级指纹 diff 判定（ADR 2026-10-19 D2：pull 路径成员级热换的定位核心，
/// 纯函数——spawn 每成员指纹表 vs 当前磁盘每成员指纹表）。
#[derive(Debug, PartialEq, Eq)]
pub(super) enum MemberFingerprintDiff {
    /// 无变化（并集差但逐成员全同：漂移检测先行触发的 stale 等——无热换对象，
    /// 调用方回退整组重建）。
    NoChange,
    /// 成员集变化（表键集不一致：新成员装箱 / 成员移除/搬组）——进程成员集在
    /// spawn 时定格，无法进程内补齐，必须整组重建。
    SetChanged,
    /// 成员集不变，这些成员的代码/配置面指纹变化（代码文件与 config_files 同
    /// 混于单一指纹，两类变更在此同形）→ 调用方对列表逐个走 `reload_member`
    /// 进程内热换。
    Changed(Vec<String>),
}

/// 比对 spawn 每成员指纹表与当前每成员指纹表（纯函数，四象限：无变 / 仅代码
/// 变 / 仅配置变 / 集合变——代码与配置变同为 `Changed`，集合变为 `SetChanged`）。
pub(super) fn diff_member_fingerprints(
    spawned: &std::collections::BTreeMap<String, u64>,
    current: &std::collections::BTreeMap<String, u64>,
) -> MemberFingerprintDiff {
    // BTreeMap 键序确定，逐元素比较即键集比较。
    if spawned.keys().ne(current.keys()) {
        return MemberFingerprintDiff::SetChanged;
    }
    let changed: Vec<String> = spawned
        .iter()
        .filter(|&(pid, old_fp)| current.get(pid) != Some(old_fp))
        .map(|(pid, _)| pid.clone())
        .collect();
    if changed.is_empty() {
        MemberFingerprintDiff::NoChange
    } else {
        MemberFingerprintDiff::Changed(changed)
    }
}

/// 由每成员指纹表合成宿主并集指纹（哈希构造 = [`host_union_fingerprint`] 原式：
/// 宿主键 + 键序逐个 `pid|fp`；BTreeMap 键序与 host_members 排序序一致，合成值
/// 与历史逐成员遍历版逐字节相同）。
pub(super) fn union_fingerprint_of(
    host_key: &str,
    member_fps: &std::collections::BTreeMap<String, u64>,
) -> u64 {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::Hasher;
    let mut hasher = DefaultHasher::new();
    hasher.write(host_key.as_bytes());
    for (pid, fp) in member_fps {
        hasher.write(pid.as_bytes());
        hasher.write(b"|");
        hasher.write(&fp.to_le_bytes());
    }
    hasher.finish()
}

/// 把共享内核路由器包成 per-plugin 路由器——在 params 里注入 `_plugin_id`，
/// 供内核 metrics.record 等需要知道调用方插件的能力使用（监控设计 §三 通道2 + §十
/// 安全：内核强制 plugin_id，插件无法伪报他人指标）。
///
/// 反向调用租户透传（刀1，ADR docs/working/2026-10-15-reverse-call-tenant-
/// passthrough-adr-draft.md）+ 用户维（刀2，ADR
/// docs/decisions/2026-10-17-reverse-call-user-identity-injection.md）：
/// params 携带 `_tenant_id`（+可选 `_session_id`/`_user_id`）时，本桥消费
/// 这些键并以 `agentos_tenant::scope` 包裹 inner.handle——内核读侧
/// （pipeline-state/tenant-context 等 `current_or_default` 家族）按调用管道的
/// 租户解析、派发面（chat.send_message）按注入 user 派发，不再恒落 default
/// 或依赖插件手写 user。身份值 origin = 内核调用插件时经 invocation
/// `_identity` 下发的执行上下文（SDK 回显），非 sidecar 可指派的自由参数。
/// 无 `_tenant_id`（旧插件/工具调用路径）→ 现状语义：不建 scope，内核读侧
/// 回退 default（显式锁定，见 tests）。
///
/// pub + [`PluginScopedRouter::new`]：api 侧组合测试/装配面可直构
/// （host_acquire 生产装配仍走同构造）。
pub struct PluginScopedRouter {
    pub(super) plugin_id: String,
    pub(super) inner: Arc<dyn CapabilityRouter>,
}

impl PluginScopedRouter {
    /// 以连接身份（`scoped_router_identity`）与共享内核路由器构造。
    pub fn new(plugin_id: impl Into<String>, inner: Arc<dyn CapabilityRouter>) -> Self {
        Self {
            plugin_id: plugin_id.into(),
            inner,
        }
    }
}

/// 消费反调 params 里的身份保留键：`(tenant_id, session_id, user_id, 剩余params)`。
///
/// 合法 `_tenant_id` = 非空字符串（内核注入执行上下文的回显形态）；缺省/畸形
/// （非字符串/空串）一律按缺席处理（保守：不按畸形值建 scope），保留键仍然
/// 消费不外漏——`_` 前缀键在本仓是内核传输层保留字面（_plugin_id/_authorization
/// 同族），不得泄漏为业务参数。`_user_id` 同规（刀2，ADR
/// docs/decisions/2026-10-17-reverse-call-user-identity-injection.md）。
pub(super) fn pop_reverse_call_identity(
    params: &mut Value,
) -> (Option<String>, Option<String>, Option<String>) {
    let Some(obj) = params.as_object_mut() else {
        return (None, None, None);
    };
    let tenant = match obj.remove("_tenant_id") {
        Some(Value::String(s)) if !s.is_empty() => Some(s),
        _ => None,
    };
    let session = match obj.remove("_session_id") {
        Some(Value::String(s)) if !s.is_empty() => Some(s),
        _ => None,
    };
    let user = match obj.remove("_user_id") {
        Some(Value::String(s)) if !s.is_empty() => Some(s),
        _ => None,
    };
    (tenant, session, user)
}

#[async_trait]
impl CapabilityRouter for PluginScopedRouter {
    async fn handle(
        &self,
        capability: &str,
        method: &str,
        mut params: Value,
    ) -> Result<Value, McpError> {
        // 反调身份透传（刀1/刀2）：先消费保留键（后注入 _plugin_id 不受影响）。
        let (tenant_id, session_id, user_id) = pop_reverse_call_identity(&mut params);
        // 在 params 注入 _plugin_id（内核侧 metrics.record 读取它做命名空间）。
        // 这是信任锚点：plugin_id 来自 invoker 的 manifest，不是 sidecar 上报。
        if let Some(obj) = params.as_object_mut() {
            obj.insert(
                "_plugin_id".to_string(),
                Value::String(self.plugin_id.clone()),
            );
        } else {
            params = json!({ "_plugin_id": self.plugin_id, "value": params });
        }
        match tenant_id {
            Some(tid) => {
                let mut tenant =
                    agentos_core::types::TenantContext::new(tid, session_id.unwrap_or_default());
                tenant.user_id = user_id;
                agentos_tenant::scope(tenant, self.inner.handle(capability, method, params)).await
            }
            None => self.inner.handle(capability, method, params).await,
        }
    }

    /// 委托给 inner——让 inner（KernelCapabilityRouter）的动态 namespace
    /// （含 handler_registry 注册的交互等 namespace）透传到 initialize 声明。
    /// 不覆盖的话走 trait 默认实现，只返回静态 STANDARD_CAPABILITIES，
    /// sidecar 拿不到插件自注册 namespace 的 CapabilityHandle。
    fn known_namespaces(&self) -> Vec<String> {
        self.inner.known_namespaces()
    }
}

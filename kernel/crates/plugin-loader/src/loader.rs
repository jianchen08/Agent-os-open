//! 插件加载器实现
//!
//! 实现双根扫描、manifest 解析校验、按需加载。
//!

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use agentos_core::traits::{
    ConfigFileMapping, EnvConfigField, LoadedPlugin, PluginLoader, PluginManifest, PluginStatus,
    PluginType,
};
use async_trait::async_trait;
use parking_lot::RwLock;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tracing::{info, warn};

use crate::error::LoaderError;
use crate::native_loader::NativePluginLoader;

mod cache;
use cache::DiscoverCandidate;
pub use cache::{DiscoveryCacheOutcome, DISCOVERY_CACHE_ENV};
use config_overlay::{
    apply_user_config_overlay, auto_generate_env_declarations, collect_user_config_stamps,
};
use managed_mcp::{
    build_external_mcp_manifest, prune_managed_plugin_dirs, write_managed_plugin_dir, PruneCause,
};

/// 插件准入白名单模式。
///
/// - `Permissive`（默认）：白名单为空或插件未列入时放行，开发友好；
///   但若插件被列入白名单且配置了 `sha256`，仍会校验哈希。
/// - `Strict`：未列入白名单的插件加载失败。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum AllowlistMode {
    /// 白名单为空或插件未列入时放行（默认）。
    #[default]
    Permissive,
    /// 未列入白名单的插件加载失败。
    Strict,
}

/// 危险前端能力词表（2026-09-25 准入分级）：插件清单可声明、但须 allowlist
/// 显式授予（`grants`）才在注册面生效的宿主级能力。能力按 manifest 内容静态
/// 判定，插件不得自报等级。
/// - `host_js`：contributes.themes[].skin → 皮肤 hooks.mjs 在宿主渲染进程执行；
/// - `host_css`：contributes.client_styles → 任意 CSS 注入宿主 document.head。
pub const CAP_HOST_JS: &str = "host_js";
pub const CAP_HOST_CSS: &str = "host_css";
pub const DANGEROUS_FRONTEND_CAPS: [&str; 2] = [CAP_HOST_JS, CAP_HOST_CSS];

/// 白名单中的单个插件条目。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct AllowlistEntry {
    pub id: String,
    /// 可选 SHA256（插件目录树规范化哈希，小写 hex，见
    /// [`PluginLoaderImpl::compute_plugin_sha256`]）。留空则只校验 id，不校验哈希。
    #[serde(default)]
    pub sha256: String,
    /// 危险前端能力授予（[`DANGEROUS_FRONTEND_CAPS`] 子集）。缺省 = 不授予：
    /// 该插件清单中的 skin / client_styles 声明在注册面剥除（剥权记录在
    /// manifest.restricted_capabilities，经 /api/v1/plugins 供设置页可见）。
    /// 内置根插件不经本表恒授予（随包分发可信面）。
    #[serde(default)]
    pub grants: Vec<String>,
}

/// 插件准入白名单配置。
///
/// 对应 `config/kernel/plugin_allowlist.yaml`。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct AllowlistConfig {
    /// 白名单模式（默认 permissive）。
    #[serde(default)]
    pub mode: AllowlistMode,
    /// 白名单条目列表。
    #[serde(default)]
    pub plugins: Vec<AllowlistEntry>,
    /// 准入是否已部署（allowlist 文件存在，含解析失败的 fail-closed 退化态）。
    /// false（文件缺失，引导默认）= 危险前端能力全授予——未部署准入的实例
    /// 不引入行为变化；true = 用户根插件按条目 `grants` 授予。
    #[serde(skip)]
    pub deployed: bool,
}

/// 加载插件准入白名单配置（缺失与损坏分化，K2）：
/// - 文件缺失 → 默认 permissive 空白名单（文档化引导默认：未部署 allowlist 的
///   实例按放行 + 可选 sha256 校验运行，保留）；
/// - 文件存在但解析失败 → **fail-closed**：退化为 strict 空名单——strict 语义
///   （白名单外拒载）下零条目 = 所有插件在发现期被拒（scan_root 逐插件 warn
///   "not in the allowlist"），内核以零插件面启动而非静默全放行。安全配置
///   损坏不得伪装成最宽松默认态。
///
/// 生产接线（build_plugin_loader）把 `config/kernel/plugin_allowlist.yaml` 从"空挂"
/// 变成真·准入——permissive=放行 + 条目 sha256 校验（真实语料校准：106 插件
/// 当前零 sha256 声明、零误伤）；strict=白名单外插件加载失败（fail-closed，与
/// deny_unknown_fields 一致），由部署方显式启用。
pub fn load_allowlist_file(path: &Path) -> AllowlistConfig {
    match std::fs::read_to_string(path) {
        Ok(text) => match serde_yaml::from_str::<AllowlistConfig>(&text) {
            Ok(cfg) => AllowlistConfig {
                deployed: true,
                ..cfg
            },
            Err(e) => {
                warn!(
                    path = %path.display(),
                    error = %e,
                    "plugin_allowlist.yaml 解析失败 → fail-closed：退化为 strict 空名单（所有插件拒载），修复配置后重启"
                );
                AllowlistConfig {
                    mode: AllowlistMode::Strict,
                    plugins: Vec::new(),
                    deployed: true,
                }
            }
        },
        Err(_) => {
            warn!(
                path = %path.display(),
                "plugin_allowlist.yaml 缺失 → 默认 permissive 空白名单（文档化默认，非损坏）"
            );
            AllowlistConfig::default()
        }
    }
}

/// 插件加载器实现。
///
/// 支持双根扫描（分级单副本模型，ADR 2026-11-07-plugin-tiered-single-copy-model）：
/// - Tier1 根（`builtin_root`，应用目录只读，安全级插件）
/// - Tier2 根（`user_root`，解包用户目录，产品功能级插件）
///
/// 同 id 多候选（分级模型下只可能来自同根畸形布局或守卫未及的热发现窗口）
/// 取 manifest version 最高者，持平保留在位者——跨根双源由内核启动守卫
/// 拒绝（零仲裁，Disjoint 由清单 + 守卫保证），本层不再有裁决策略。
///
/// 遵循按需加载原则：manifest 声明但不立即实例化，首次被路由到时才加载。
pub struct PluginLoaderImpl {
    /// 内置插件根目录（只读）
    builtin_root: PathBuf,
    /// 用户插件根目录（可写）
    user_root: Option<PathBuf>,
    /// 配置文件根目录（如 `config/`）
    config_root: Option<PathBuf>,
    /// 插件准入白名单（P2-2）
    allowlist: AllowlistConfig,
    /// 已发现的 manifest 来源缓存 {plugin_id: manifest 文件路径}。
    ///
    /// 内存归一（manifest 唯一真源 = AppState.manifests）：本表只持轻量路径
    /// 摘要，**不**持久持有解析后的完整 PluginManifest——完整内容按需
    /// 读盘 + 重新解析（[`Self::read_manifest`]）。路径本身兼作内容指纹的
    /// 粗判（同 id 换路径 = manifest 换源），watcher 热发现经 discover
    /// 重扫刷新。
    manifests: RwLock<HashMap<String, PathBuf>>,
    /// 服务声明缓存 {完整服务名（含 ns.method 点式形态）: 声明提供方 id 列表}。
    ///
    /// discover 全量重扫时随路径缓存同刀重建（watcher 热发现自动刷新）。
    /// 供 [`PluginLoader::service_provider_ids`] 消费——ensure 声明面依赖
    /// 闭包展开（R7 F3）按点式条目解析提供方；bare 命名空间不入键。
    service_providers: RwLock<HashMap<String, Vec<String>>>,
    /// 已加载的插件状态 {plugin_id: LoadedPlugin}
    loaded: RwLock<HashMap<String, LoadedPlugin>>,
    /// 全量配置快照缓存（mtime 指纹）：`load_config` 的解析结果 + 扫描面
    /// 指纹（文件集 × (路径, mtime, size)，factory 根 + 用户覆盖根并集）。
    ///
    /// 动因：load_config 在每次插件 spawn、每次管道步骤、每次跨插件工具
    /// 调用时都会被走到（invoker 七处调用面），装机实测 76 分钟 2415 次全量
    /// 扫描（105 文件读+YAML 解析/次）构成分配 churn 洪峰。指纹未变 → 直接
    /// 返回快照，文件真变更（mtime 必 bump）→ 现场失效重载——变更检测在
    /// 每次 load 时做，无 TTL 无延迟，watcher 语义不变。
    ///
    /// 已知不可检测窗口（如实声明）：同 mtime 同 size 的原位改写（正常
    /// 编辑器/写入路径不会出现）。竞态窗口：指纹采集先于内容读取，二者
    /// 之间发生写入最坏造成一次多余重载，绝不缓存"指纹新于内容"的快照。
    config_cache: RwLock<Option<ConfigSnapshot>>,
}

/// [`PluginLoaderImpl::config_cache`] 的缓存条目。
struct ConfigSnapshot {
    /// 缓存键：排序后的文件指纹集。空集 = "不可指纹化"（用户覆盖层读取
    /// 失败的降级装载），不与任何现场指纹匹配，迫使每次重载直到装载干净。
    stamps: Vec<agentos_core::config_scan::YamlFileStamp>,
    config: serde_json::Value,
}

/// 同 id 多候选记录：id → 全部候选 manifest version（按发现序，胜者除外）。
/// 仅发生同 id 多候选的插件入册（单源零开销），discover 尾部逐 id 打对账
/// 日志——分级单副本模型下该形态 = 同根畸形布局或守卫未及的热发现窗口，
/// 留痕供排障（ADR 2026-11-07-plugin-tiered-single-copy-model）。
type SameIdConflicts = HashMap<String, Vec<String>>;

/// 宽容 semver 解析：trim + strip 单个前导 `v`/`V` 前缀。sync 工作流/
/// 人工编辑写出的 `v1.2.3` 形态不再静默判旧；strip 后仍非 semver → None
/// （version-max 归约保守判「挑战者不新」，在位者保留）。
fn parse_version(raw: &str) -> Option<semver::Version> {
    let s = raw.trim();
    let stripped = s
        .strip_prefix('v')
        .or_else(|| s.strip_prefix('V'))
        .unwrap_or(s);
    semver::Version::parse(stripped).ok()
}

impl PluginLoaderImpl {
    /// 创建插件加载器。
    ///
    /// # Arguments
    /// * `builtin_root` - Tier1 根（应用目录只读，安全级插件）
    /// * `user_root` - Tier2 根（解包用户目录，可选；与 Tier1 同指的 dev
    ///   退化配置传 `None`）
    pub fn new(builtin_root: impl Into<PathBuf>, user_root: Option<PathBuf>) -> Self {
        Self {
            builtin_root: builtin_root.into(),
            user_root,
            config_root: None,
            allowlist: AllowlistConfig::default(),
            manifests: RwLock::new(HashMap::new()),
            service_providers: RwLock::new(HashMap::new()),
            loaded: RwLock::new(HashMap::new()),
            config_cache: RwLock::new(None),
        }
    }

    /// 设置配置文件根目录，返回 self 供链式调用。
    ///
    /// 设置后，`load_config()` 将扫描该目录下的 YAML 文件并解析为 JSON。
    ///
    /// # Security
    ///
    /// 此方法应由应用启动代码调用，传入可信的配置目录路径（如 `./config/`）。
    /// 不应接受来自用户输入的路径，以防止路径遍历攻击。
    pub fn with_config_root(mut self, config_root: impl Into<PathBuf>) -> Self {
        let path = config_root.into();
        // 尝试 canonicalize 以规范化路径（消除 ../、symlink 等）
        // 如果路径尚不存在（如测试中的临时目录可能已清理），保留原始路径
        match std::fs::canonicalize(&path) {
            Ok(canonical) => {
                self.config_root = Some(canonical);
            }
            Err(_) => {
                warn!(
                    "Config root canonicalize failed (path may not exist yet): {}",
                    path.display()
                );
                self.config_root = Some(path);
            }
        }
        self
    }

    /// 设置插件准入白名单，返回 self 供链式调用。
    ///
    /// 设置后，`validate_manifest_internal` 将按 `mode`（strict/permissive）
    /// 校验插件 id 是否在白名单中，并对白名单条目声明了 `sha256` 的插件
    /// 校验 `sha256(manifest_bytes || entry_file_bytes)` 是否匹配。
    ///
    /// # Security
    ///
    /// 白名单应由应用启动代码加载可信配置（如 `config/kernel/plugin_allowlist.yaml`）
    /// 后传入，不应接受来自插件自身的输入。
    pub fn with_allowlist(mut self, allowlist: AllowlistConfig) -> Self {
        self.allowlist = allowlist;
        self
    }

    /// 标准 mcpServers 配置（config/mcp.json）→ external MCP 插件目录同步。
    ///
    /// 对标 Claude Desktop 的安装体验：往 config/mcp.json 贴一段标准
    /// {"mcpServers": {"<name>": {"command","args","env"} | {"url","headers"}}}
    /// 即装上一个 MCP——每个条目生成一个 mcp:external 空声明插件目录
    /// （观测即声明，dynamic MCP），命名 mcp_<name>。discover 每轮先同步：
    /// 新增/变更重写、删除条目清目录（只动带 .mcp-managed 标记的目录）。
    /// 内容未变不重写（mtime 空转会空转 watcher）。
    /// 用户根 config/mcp.json 同名条目覆盖内置（与插件双根同语义）：被覆盖的
    /// 内置副本按单副本收敛清掉（生效面=用户副本，避免同 id 平版歧义）。
    /// 配置读取态三分（2026-11-20 mcp_windows_mcp 启动复活链治理）：
    /// - Readable：条目集生效，缺条目的受管目录清（真·配置删除）；
    /// - FileDeleted（NotFound）：配置文件本身已删 = 删除信号，该根受管目录
    ///   全清——否则陈旧副本常驻发现面，启动期 spawn + 现场装依赖复活；
    /// - Broken（解析失败/形状坏/读失败非 NotFound）：配置抖动，跳过同步且
    ///   不清目录（半写文件、手编打错不得销毁已生成目录）。
    fn sync_external_mcp_plugins(&self) {
        let warn_err = |stage: &str, msg: String| {
            warn!(target: "plugin-loader", stage, error = %msg, "external MCP 配置同步失败（该条跳过）");
        };

        // 配置源：config_root 优先，回退插件根旁的 config/（仓库布局）。
        let mut builtin_servers: std::collections::HashMap<String, serde_json::Value> =
            std::collections::HashMap::new();
        let mut user_servers: std::collections::HashMap<String, serde_json::Value> =
            std::collections::HashMap::new();
        let read_cfg = |dir: Option<&Path>,
                        out: &mut std::collections::HashMap<String, serde_json::Value>|
         -> McpConfigState {
            let Some(dir) = dir else {
                return McpConfigState::Broken;
            };
            let path = dir.join("mcp.json");
            let text = match std::fs::read_to_string(&path) {
                Ok(text) => text,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    return McpConfigState::FileDeleted;
                }
                Err(e) => {
                    // 非 NotFound 读失败（占用/权限）= 抖动：不清目录
                    warn_err("read", format!("{}: {}", path.display(), e));
                    return McpConfigState::Broken;
                }
            };
            match serde_json::from_str::<serde_json::Value>(&text) {
                Ok(v) => match v.get("mcpServers").and_then(|m| m.as_object()) {
                    Some(map) => {
                        for (name, spec) in map {
                            out.insert(name.clone(), spec.clone());
                        }
                        McpConfigState::Readable
                    }
                    None => {
                        // 缺 mcpServers 键同解析失败：该根不可用，不得据此清目录
                        warn_err("parse", format!("{} 缺 mcpServers 对象", path.display()));
                        McpConfigState::Broken
                    }
                },
                Err(e) => {
                    warn_err("parse", format!("{}: {}", path.display(), e));
                    McpConfigState::Broken
                }
            }
        };

        let builtin_state = read_cfg(
            self.config_root
                .as_deref()
                .map(PathBuf::from)
                .or_else(|| self.builtin_root.parent().map(|p| p.join("config")))
                .as_deref(),
            &mut builtin_servers,
        );
        let user_state = self
            .user_root
            .as_ref()
            .map(|user_root| {
                read_cfg(
                    user_root.parent().map(|p| p.join("config")).as_deref(),
                    &mut user_servers,
                )
            })
            .unwrap_or(McpConfigState::Broken);
        // 双侧皆 Broken（无配置/全抖动）= 无删除信号可依据，跳过整轮同步
        if builtin_state == McpConfigState::Broken && user_state == McpConfigState::Broken {
            return;
        }

        // 生成：用户条目落用户根（覆盖内置同名），内置条目落内置根
        let mut keep: std::collections::HashMap<PathBuf, std::collections::HashSet<String>> =
            std::collections::HashMap::new();
        let mut superseded_builtin: std::collections::HashSet<String> =
            std::collections::HashSet::new();
        for (name, spec) in &builtin_servers {
            if user_servers.contains_key(name) {
                // 被用户根同名条目取代：不生成内置副本，记名单副本收敛清理
                if let Some((dir_name, _)) = build_external_mcp_manifest(name, spec) {
                    superseded_builtin.insert(dir_name);
                }
                continue;
            }
            if let Some((dir_name, manifest)) = build_external_mcp_manifest(name, spec) {
                let target = self.builtin_root.join(&dir_name);
                write_managed_plugin_dir(&target, &manifest);
                keep.entry(self.builtin_root.clone())
                    .or_default()
                    .insert(dir_name);
            }
        }
        if let Some(user_root) = &self.user_root {
            for (name, spec) in &user_servers {
                if let Some((dir_name, manifest)) = build_external_mcp_manifest(name, spec) {
                    let target = user_root.join(&dir_name);
                    write_managed_plugin_dir(&target, &manifest);
                    keep.entry(user_root.clone()).or_default().insert(dir_name);
                }
            }
        }

        // 清理：按各根配置态分派（只动带 .mcp-managed 标记的目录）
        let no_superseded = std::collections::HashSet::new();
        match builtin_state {
            McpConfigState::Readable => prune_managed_plugin_dirs(
                &self.builtin_root,
                keep.get(&self.builtin_root),
                &superseded_builtin,
                PruneCause::EntryRemoved,
            ),
            McpConfigState::FileDeleted => prune_managed_plugin_dirs(
                &self.builtin_root,
                None,
                &no_superseded,
                PruneCause::ConfigFileDeleted,
            ),
            McpConfigState::Broken => {}
        }
        if let Some(user_root) = &self.user_root {
            match user_state {
                McpConfigState::Readable => prune_managed_plugin_dirs(
                    user_root,
                    keep.get(user_root),
                    &no_superseded,
                    PruneCause::EntryRemoved,
                ),
                McpConfigState::FileDeleted => prune_managed_plugin_dirs(
                    user_root,
                    None,
                    &no_superseded,
                    PruneCause::ConfigFileDeleted,
                ),
                McpConfigState::Broken => {}
            }
        }
    }

    /// 路径是否落在 Tier2 根子树内（剥权信任边界：应用目录可信，用户目录
    /// 危险前端能力须 allowlist 授予——分级模型 ADR 2026-11-07 §2.1）。
    fn is_user_rooted(&self, p: &Path) -> bool {
        self.user_root
            .as_ref()
            .is_some_and(|user_root| p.starts_with(user_root))
    }

    /// `a_ver` 是否按 semver 严格大于 `b_ver`。任一侧不可解析 →
    /// false（不可判定时保守判「挑战者不新」，在位者保留）。
    fn strictly_newer(a_ver: &str, b_ver: &str) -> bool {
        match (parse_version(a_ver), parse_version(b_ver)) {
            (Some(a), Some(b)) => a > b,
            _ => false,
        }
    }

    /// 同 id 多候选归约：challenger 是否应取代 incumbent——**版本最高者赢，
    /// 持平保留在位者**（ADR 2026-11-07 单副本模型：裁决策略层已退役；本
    /// 归约只兜同根畸形布局/守卫未及的热发现窗口）。
    ///
    /// 判定只依赖两侧 version 且持平单调（挑战者不更优必不换）——多候选
    /// （>2 份）逐对归约等价于取全候选中版本最高者，最终胜者与扫描序解耦
    /// （2026-09-18 翻转根修的性质在纯版本归约下继续成立）。跨根双源不由
    /// 本归约处置：内核启动守卫直接拒启（零仲裁，Disjoint 由清单 + 守卫
    /// 保证）。平版归约 = 包内副本优先（见 [`Self::tie_prefers_incumbent`]）。
    /// 平版（版本相等）裁决：**包内副本胜出**（tier1/builtin 根优先于用户根）。
    ///
    /// 2026-11-08 装机实证（安装器把 tier2 用户副本刷成与包同版后）：平版胜者
    /// 若随扫描序漂移到用户副本，spawn 层即拿无 venv 的用户目录路径（包内副本
    /// 带 venv 交付，用户副本按 provision 契约排除 .venv）→ sidecar 全线
    /// VENV_INTERPRETER_MISSING。与扫描序解耦的优先级裁决 = 双保险（主修在
    /// collect_plugin_roots 根序确定化）；语义与升级覆盖规则同线——用户未改
    /// （版本未抬）→ 包内副本运行；用户改过（版本严格大于）→ 用户副本运行。
    fn tie_prefers_incumbent(
        &self,
        incumbent_path: &std::path::Path,
        challenger_path: &std::path::Path,
    ) -> bool {
        let in_user_root =
            |p: &std::path::Path| self.user_root.as_ref().is_some_and(|u| p.starts_with(u));
        // 在位者非用户根（包内）且挑战者来自用户根 → 平版保留在位者；
        // 其余平版形态（双方同根/在位者用户根挑战者包内）不因平版换位。
        !in_user_root(incumbent_path) && in_user_root(challenger_path)
    }

    /// 把一个扫描候选并入发现集：无在位者直接入；有在位者经严格更高版本 /
    /// 平版包内优先归约（见 [`Self::tie_prefers_incumbent`]）。多候选 id 的
    /// 全部落选版本记入 `conflicts`，由调用方在扫描完后统一输出对账日志。
    fn admit(
        &self,
        all: &mut HashMap<String, (PluginManifest, PathBuf)>,
        conflicts: &mut SameIdConflicts,
        manifest: PluginManifest,
        path: PathBuf,
    ) {
        let replace = match all.get(&manifest.id) {
            None => true,
            Some((incumbent, incumbent_path)) => {
                let should = if Self::strictly_newer(&manifest.version, &incumbent.version) {
                    true
                } else if incumbent.version == manifest.version {
                    // 平版：包内副本优先（扫描序无关，见 tie_prefers_incumbent）
                    !self.tie_prefers_incumbent(incumbent_path, &path)
                } else {
                    false
                };
                conflicts
                    .entry(manifest.id.clone())
                    .or_default()
                    .push(manifest.version.clone());
                should
            }
        };
        if replace {
            all.insert(manifest.id.clone(), (manifest, path));
        }
    }

    /// 扫描单个根目录，发现所有 plugin.json/plugin.yaml manifest。
    fn scan_root(&self, root: &Path) -> Result<Vec<(PluginManifest, PathBuf)>, LoaderError> {
        let mut results = Vec::new();

        if !root.exists() {
            return Ok(results);
        }

        let entries = std::fs::read_dir(root).map_err(|e| LoaderError::Io {
            message: format!("Failed to read dir {}: {}", root.display(), e),
        })?;

        for entry in entries.flatten() {
            let dir_path = entry.path();
            if !dir_path.is_dir() {
                continue;
            }

            // 查找 plugin.json 或 plugin.yaml
            let json_path = dir_path.join("plugin.json");
            let yaml_path = dir_path.join("plugin.yaml");

            let (manifest_path, content) = if json_path.exists() {
                let content = std::fs::read_to_string(&json_path).map_err(|e| LoaderError::Io {
                    message: format!("Failed to read {}: {}", json_path.display(), e),
                })?;
                (json_path, content)
            } else if yaml_path.exists() {
                let content = std::fs::read_to_string(&yaml_path).map_err(|e| LoaderError::Io {
                    message: format!("Failed to read {}: {}", yaml_path.display(), e),
                })?;
                (yaml_path, content)
            } else {
                continue;
            };

            // 解析 manifest（跳过解析失败的插件，不影响同 root 的其他插件）
            let mut manifest: PluginManifest =
                match serde_json::from_str::<PluginManifest>(&content) {
                    Ok(m) => m,
                    Err(json_err) => match serde_yaml::from_str::<PluginManifest>(&content) {
                        Ok(m) => m,
                        Err(yaml_err) => {
                            warn!(
                                "Skipping plugin at {}: json error: {}, yaml error: {}",
                                manifest_path.display(),
                                json_err,
                                yaml_err
                            );
                            continue;
                        }
                    },
                };

            // 校验 manifest（跳过校验失败的插件，不阻断同 root 的其他插件）
            if let Err(e) = self.validate_manifest_internal(&mut manifest, &manifest_path) {
                warn!(
                    "Skipping plugin at {}: validation error: {}",
                    manifest_path.display(),
                    e
                );
                continue;
            }

            results.push((manifest, manifest_path));
        }

        Ok(results)
    }

    /// 内部 manifest 校验逻辑。
    fn validate_manifest_internal(
        &self,
        manifest: &mut PluginManifest,
        source_path: &Path,
    ) -> Result<(), LoaderError> {
        // 必填字段校验
        if manifest.id.is_empty() {
            return Err(LoaderError::ManifestValidation {
                plugin_id: "(unknown)".to_string(),
                reason: "id is required".to_string(),
            });
        }
        if manifest.name.is_empty() {
            return Err(LoaderError::ManifestValidation {
                plugin_id: manifest.id.clone(),
                reason: "name is required".to_string(),
            });
        }
        if manifest.version.is_empty() {
            return Err(LoaderError::ManifestValidation {
                plugin_id: manifest.id.clone(),
                reason: "version is required".to_string(),
            });
        }
        if manifest.language.is_empty() {
            return Err(LoaderError::ManifestValidation {
                plugin_id: manifest.id.clone(),
                reason: "language is required".to_string(),
            });
        }

        // ── capabilities.steps 校验（管道步骤服务化提案 2026-08-27 §3.1）：
        // step 名非空 + 同一 manifest 内唯一（全局唯一由 G10 编译期查重，
        // 启动报错）。空名/重名是声明错误，加载期显式报出，不拖到命中期 ──
        let mut seen_steps: HashSet<&str> = HashSet::new();
        for step in &manifest.capabilities.steps {
            if step.name.trim().is_empty() {
                return Err(LoaderError::ManifestValidation {
                    plugin_id: manifest.id.clone(),
                    reason: format!(
                        "capabilities.steps 存在空 name（description={:?}）——步骤名必填且非空白",
                        step.description
                    ),
                });
            }
            if !seen_steps.insert(step.name.as_str()) {
                return Err(LoaderError::ManifestValidation {
                    plugin_id: manifest.id.clone(),
                    reason: format!(
                        "capabilities.steps 存在重复 name: '{}'——step 名同一 manifest 内必须唯一（全局唯一由 G10 编译期查重）",
                        step.name
                    ),
                });
            }
        }

        // 组合插件 entry 可为空（ADR ⑥）
        if manifest.plugin_type != PluginType::Composite && manifest.entry.is_empty() {
            return Err(LoaderError::ManifestValidation {
                plugin_id: manifest.id.clone(),
                reason: "entry is required for non-composite plugins".to_string(),
            });
        }

        // host_type 校验（ADR ⑧: 所有插件支持双路径，但必须声明一个）
        // host_type 已是必填字段，serde 会校验

        // ── native 产物预检（契约闸门体系 2.5）：InProcess 插件声明的 cdylib
        // 产物必须存在，缺失在加载期明确报错，不再拖到 load/调用期才炸 ──
        if let Some(native) = &manifest.native {
            let Some(dir) = source_path.parent() else {
                return Err(LoaderError::ManifestValidation {
                    plugin_id: manifest.id.clone(),
                    reason: format!(
                        "native artifact 无法解析相对路径（manifest {source_path:?} 无父目录）"
                    ),
                });
            };
            // 与真实加载路径同规则：裸名按平台补 cdylib 后缀；声明带异平台后缀
            // （如 `.dll` 声明在 Linux）时回退到本平台重映射名（resolve_artifact）。
            let artifact_path = NativePluginLoader::resolve_artifact(dir, &native.artifact);
            if artifact_path.is_none() {
                return Err(LoaderError::ManifestValidation {
                    plugin_id: manifest.id.clone(),
                    reason: format!(
                        "native artifact 缺失: {}（cdylib 产物未构建或路径声明有误；已按声明名与本平台名 {} 双查）",
                        dir.join(&native.artifact).display(),
                        NativePluginLoader::platform_artifact_name(
                            native.artifact
                                .trim_end_matches(".dll")
                                .trim_end_matches(".so")
                                .trim_end_matches(".dylib"),
                        )
                    ),
                });
            }
        }

        // ── P2-2 插件准入校验（白名单 + SHA256）──
        self.validate_allowlist(manifest, source_path)?;

        // 准入分级剥权不在本校验期执行（2026-10-23 修正）：校验期逐副本运行、
        // 早于同 id 多候选归约，未胜出的播种副本也被剥权并打 "stripped from
        // registration face" 警告（mode_roleplay 48 次/日实证）——而被剥的
        // 副本对象随即被归约丢弃，注册面（胜者）从未受影响，警告纯属噪声。
        // 剥权已挪至 discover 归约完成后，仅对胜者执行。

        // ── 配置单一真值校验（2026-09-02 用户裁定）：真值要么在 manifest
        // （内联形态，path 省略，fields.default 即真值），要么在引用文件
        // （引用形态，path 非空，fields 仅 schema 禁声明 default）——两处
        // 同时存值即双真值错误，加载期显式报出，不拖到读值漂移才暴露 ──
        for cf in &manifest.config_files {
            if cf.path.is_empty() {
                if cf.target.as_deref() == Some("env") {
                    return Err(LoaderError::ManifestValidation {
                        plugin_id: manifest.id.clone(),
                        reason: format!(
                            "config_files[{}] target=env 但未声明 path——env 条目的写入目标（.env）不可省略",
                            cf.id
                        ),
                    });
                }
                continue;
            }
            let offender = cf.fields.iter().find(|f| {
                f.extra
                    .as_ref()
                    .and_then(|e| e.get("default"))
                    .is_some_and(|d| !d.is_null())
            });
            if let Some(f) = offender {
                return Err(LoaderError::ManifestValidation {
                    plugin_id: manifest.id.clone(),
                    reason: format!(
                        "config_files[{}].fields[{}] 声明了 default——引用形态（path={:?}）真值在文件，manifest 与文件两处存值即双真值错误；要内联请删除 path",
                        cf.id, f.name, cf.path
                    ),
                });
            }
        }

        // ── state.reads 读面声明格式校验（schema 收录阶段）：`messages_tail:N`
        // 条目要求 N 为正整数；非法条目 warn + 忽略该条目（不拒载——读面
        // 声明单条错误只影响该插件的投喂范围，warn 不拒载与既有校验风格
        // 一致）。键名 / `messages` 形态由文档约定，本阶段不校验；合法条目
        // 原样透传到运行时 manifest 视图（投喂消费由后续任务接入）。──
        if let Some(state) = manifest.state.as_mut() {
            state.reads.retain(|entry| {
                let valid = match entry.strip_prefix("messages_tail:") {
                    Some(n) => n.parse::<u64>().is_ok_and(|n| n > 0),
                    None => true,
                };
                if !valid {
                    warn!(
                        "Ignoring invalid state.reads entry {:?} in plugin {}: messages_tail:N requires N to be a positive integer",
                        entry, manifest.id
                    );
                }
                valid
            });
        }

        // ── GAP-4：env 声明自动生成——mcp 端点引用的 ${VAR}（无默认值语法）
        // 即视为 env 配置面，内核自动生成 config_files[target=env] 声明
        // （合并进既有 env 条目或新建），设置页据此提供 key 入口；插件只写
        // endpoint 引用即可被发现使用，无需手写声明（2026-09-03 用户裁定）──
        auto_generate_env_declarations(manifest);

        info!(
            "Manifest validated: id={} type={:?} host={:?} path={}",
            manifest.id,
            manifest.plugin_type,
            manifest.host_type,
            source_path.display()
        );

        Ok(())
    }

    /// 插件准入校验：白名单 + SHA256 哈希。
    ///
    /// 规则：
    /// - `Strict` 模式：`plugin.id` 不在白名单 → `Err`。
    /// - `Permissive` 模式：跳过白名单门槛，所有插件放行；
    ///   但若插件在白名单条目中且配置了 `sha256`，仍执行哈希校验。
    /// - 哈希校验：当匹配的白名单条目 `sha256` 非空时，
    ///   计算 `sha256(manifest_bytes || entry_file_bytes)`，不匹配 → `Err`。
    ///   `entry_file_bytes` 为 entry 字段在插件目录中引用到的实际文件字节
    ///   （找不到入口文件则按空字节处理，等价于只哈希 manifest）。
    fn validate_allowlist(
        &self,
        manifest: &PluginManifest,
        source_path: &Path,
    ) -> Result<(), LoaderError> {
        // 查找白名单条目
        let entry = self.allowlist.plugins.iter().find(|e| e.id == manifest.id);

        // strict 模式门槛校验
        if self.allowlist.mode == AllowlistMode::Strict && entry.is_none() {
            return Err(LoaderError::ManifestValidation {
                plugin_id: manifest.id.clone(),
                reason: format!(
                    "plugin '{}' is not in the allowlist (strict mode)",
                    manifest.id
                ),
            });
        }

        // 哈希校验（仅当白名单条目声明了 sha256 时执行）：插件目录树规范化
        // 哈希，热重扫每轮重验——篡改源文件 → 下轮准入失败 → 从发现集消失 →
        // 注册表注销（fail-closed 即时吊销；文件复原后自动恢复）。
        if let Some(entry) = entry {
            if !entry.sha256.is_empty() {
                let computed = self.compute_plugin_sha256(source_path)?;
                if !secure_eq(&computed, &entry.sha256) {
                    return Err(LoaderError::ManifestValidation {
                        plugin_id: manifest.id.clone(),
                        reason: format!(
                            "SHA256 mismatch for plugin '{}': expected {}, computed {}",
                            manifest.id, entry.sha256, computed
                        ),
                    });
                }
                info!(
                    "Plugin SHA256 verified: id={} sha256={}",
                    manifest.id, computed
                );
            }
        }

        Ok(())
    }

    /// 计算插件目录树规范化哈希，返回小写 hex。
    ///
    /// sha256 输入流 = 排序后的 `(相对路径 \0 文件字节 \0)` 顺序拼接——路径
    /// 排序保证确定性（文件系统枚举序不进摘要），路径入摘要防同字节文件换位
    /// 伪装。排除非源完整性面（环境噪声，不属"插件源"）：`.venv*`/`__pycache__`/
    /// `.git`/`node_modules`/`.pytest_cache`/`logs` 目录与 `*.pyc` 文件；符号
    /// 链接一律跳过（逃逸面，与 /ext 静态服务同口径）。
    fn compute_plugin_sha256(&self, source_path: &Path) -> Result<String, LoaderError> {
        let dir = source_path.parent().ok_or_else(|| LoaderError::Io {
            message: format!("manifest path has no parent: {}", source_path.display()),
        })?;
        let mut files = Vec::new();
        collect_tree_files(dir, dir, &mut files)?;
        files.sort_by(|a, b| a.0.cmp(&b.0));
        let mut hasher = Sha256::new();
        for (rel, bytes) in &files {
            hasher.update(rel.as_bytes());
            hasher.update([0u8]);
            hasher.update(bytes);
            hasher.update([0u8]);
        }
        Ok(format!("{:x}", hasher.finalize()))
    }
}

/// 常数时间字符串比较，避免哈希比较的计时侧信道。
///
/// 先比较长度（长度不同必然不等），再逐字节 AND 累积差异。
fn secure_eq(a: &str, b: &str) -> bool {
    let a = a.as_bytes();
    let b = b.as_bytes();
    if a.len() != b.len() {
        return false;
    }
    let mut diff: u8 = 0;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

/// 从 manifest 内容静态检测危险前端能力（插件不得自报等级，见
/// [`DANGEROUS_FRONTEND_CAPS`]）：
/// - `host_js`：contributes.themes[] 任一条目带非空 `skin` 字符串；
/// - `host_css`：contributes.client_styles 为非空数组。
fn detect_frontend_dangerous_caps(contributes: Option<&serde_json::Value>) -> Vec<&'static str> {
    let Some(contributes) = contributes else {
        return Vec::new();
    };
    let mut caps = Vec::new();
    let has_skin = contributes
        .get("themes")
        .and_then(|t| t.as_array())
        .is_some_and(|themes| {
            themes.iter().any(|t| {
                t.get("skin")
                    .and_then(|s| s.as_str())
                    .is_some_and(|s| !s.trim().is_empty())
            })
        });
    if has_skin {
        caps.push(CAP_HOST_JS);
    }
    if contributes
        .get("client_styles")
        .and_then(|v| v.as_array())
        .is_some_and(|styles| !styles.is_empty())
    {
        caps.push(CAP_HOST_CSS);
    }
    caps
}

/// 把未授予的危险能力从 manifest 注册面剥除（磁盘清单原样，重扫重算）：
/// - `host_js`：contributes.themes[] 逐条移除 `skin` 键（数组变空则整键移除）；
/// - `host_css`：移除 `client_styles` 键。
///
/// contributes 非 object / 目标键形态不符时原样跳过（无可剥面）。
fn strip_frontend_capabilities(manifest: &mut PluginManifest, not_granted: &[&str]) {
    let Some(contributes) = manifest
        .contributes
        .as_mut()
        .and_then(|v| v.as_object_mut())
    else {
        return;
    };
    if not_granted.contains(&CAP_HOST_JS) {
        if let Some(serde_json::Value::Array(themes)) = contributes.get_mut("themes") {
            for theme in themes.iter_mut() {
                if let Some(obj) = theme.as_object_mut() {
                    obj.remove("skin");
                }
            }
            if themes.is_empty() {
                contributes.remove("themes");
            }
        }
    }
    if not_granted.contains(&CAP_HOST_CSS) {
        contributes.remove("client_styles");
    }
}

/// 递归收集插件源树文件（相对路径 + 字节）。排除面与符号链接见
/// [`PluginLoaderImpl::compute_plugin_sha256`]。
fn collect_tree_files(
    root: &Path,
    dir: &Path,
    out: &mut Vec<(String, Vec<u8>)>,
) -> Result<(), LoaderError> {
    const EXCLUDED_DIRS: [&str; 5] = [
        "__pycache__",
        ".git",
        "node_modules",
        ".pytest_cache",
        "logs",
    ];
    for entry in std::fs::read_dir(dir).map_err(|e| LoaderError::Io {
        message: format!("Failed to read dir {}: {}", dir.display(), e),
    })? {
        let entry = entry.map_err(|e| LoaderError::Io {
            message: format!("Failed to read entry in {}: {}", dir.display(), e),
        })?;
        let path = entry.path();
        let file_type = entry.file_type().map_err(|e| LoaderError::Io {
            message: format!("Failed to stat {}: {}", path.display(), e),
        })?;
        let name = entry.file_name().to_string_lossy().to_string();
        if file_type.is_symlink() {
            continue;
        }
        if file_type.is_dir() {
            if EXCLUDED_DIRS.contains(&name.as_str()) || name.starts_with(".venv") {
                continue;
            }
            collect_tree_files(root, &path, out)?;
        } else if name.ends_with(".pyc") {
            continue;
        } else {
            let rel = path
                .strip_prefix(root)
                .map(|p| p.to_string_lossy().to_string())
                .unwrap_or_else(|_| path.to_string_lossy().to_string());
            let bytes = std::fs::read(&path).map_err(|e| LoaderError::Io {
                message: format!("Failed to read {}: {}", path.display(), e),
            })?;
            out.push((rel, bytes));
        }
    }
    Ok(())
}

/// mcp.json 单根配置读取态（external MCP 同步的删除信号判定基面）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum McpConfigState {
    /// 文件在且解析成功：条目集生效。
    Readable,
    /// 文件不存在（NotFound）：配置已删除信号，该根受管目录全清。
    FileDeleted,
    /// 解析失败/形状坏/读失败（非 NotFound）：配置抖动，不清目录。
    Broken,
}

#[async_trait]
impl PluginLoader for PluginLoaderImpl {
    /// 扫描指定根目录，发现所有 plugin.json manifest。
    ///
    /// 双根扫描：先扫内置根，再扫用户根。用户根的插件优先级高于内置根（同 ID 覆盖）。
    async fn discover(
        &self,
        root_paths: &[&str],
    ) -> Result<Vec<PluginManifest>, agentos_core::types::PluginError> {
        self.sync_external_mcp_plugins();
        let candidates = self.gather_candidates_live(root_paths);
        let (manifests, _) = self.finalize_discovery(candidates)?;
        Ok(manifests)
    }
    /// 验证 manifest 是否符合 Schema。
    fn validate_manifest(
        &self,
        manifest: &PluginManifest,
    ) -> Result<(), agentos_core::types::PluginError> {
        // 内部校验会原地补自动生成的 env 声明（auto_generate_env_declarations），
        // 只读包装在副本上跑——生成的条目对调用方不可见（要看生成结果请走
        // validate_manifest_internal 或扫描流程的 manifest 本体）。
        let mut owned = manifest.clone();
        self.validate_manifest_internal(&mut owned, Path::new("(runtime)"))
            .map_err(|e| agentos_core::types::PluginError {
                message: e.to_string(),
                code: Some("MANIFEST_VALIDATION".to_string()),
                source: Some("plugin-loader".to_string()),
            })
    }

    /// 按需加载（实例化）指定插件。
    ///
    /// 如果插件已加载则直接返回；如果未加载则首次实例化。
    /// 按需加载原则：首次被调用时才启动 MCP 边车进程（非预启动）。
    async fn load(
        &self,
        plugin_id: &str,
    ) -> Result<LoadedPlugin, agentos_core::types::PluginError> {
        {
            let loaded = self.loaded.read();
            if let Some(plugin) = loaded.get(plugin_id) {
                return Ok(plugin.clone());
            }
        }

        // 查找 manifest（缓存只持路径，完整 manifest 按需读盘解析）
        let manifest = {
            let manifests = self.manifests.read();
            let path = manifests.get(plugin_id).cloned().ok_or_else(|| {
                agentos_core::types::PluginError {
                    message: format!("plugin not found: {plugin_id}"),
                    code: Some("PLUGIN_NOT_FOUND".to_string()),
                    source: Some("plugin-loader".to_string()),
                }
            })?;
            self.read_manifest(&path)
                .map_err(|e| agentos_core::types::PluginError {
                    message: format!("plugin manifest unreadable: {}: {e}", path.display()),
                    code: Some("MANIFEST_READ".to_string()),
                    source: Some("plugin-loader".to_string()),
                })?
        };

        // 组合插件不实例化（ADR ⑥），只标记为 Active
        let loaded_plugin = LoadedPlugin {
            manifest: manifest.clone(),
            status: PluginStatus::Active,
            loaded_at: Some(chrono::Utc::now()),
        };

        {
            let mut loaded = self.loaded.write();
            loaded.insert(plugin_id.to_string(), loaded_plugin.clone());
        }

        info!(
            "Plugin loaded: id={} type={:?}",
            plugin_id, manifest.plugin_type
        );

        Ok(loaded_plugin)
    }

    /// 卸载插件（释放进程/资源）。
    async fn unload(&self, plugin_id: &str) -> Result<(), agentos_core::types::PluginError> {
        let mut loaded = self.loaded.write();
        if let Some(mut plugin) = loaded.remove(plugin_id) {
            plugin.status = PluginStatus::Unloaded;
            info!("Plugin unloaded: id={}", plugin_id);
            Ok(())
        } else {
            Err(agentos_core::types::PluginError {
                message: format!("plugin not loaded: {plugin_id}"),
                code: Some("NOT_LOADED".to_string()),
                source: Some("plugin-loader".to_string()),
            })
        }
    }

    /// 查询插件当前加载状态。
    fn get_status(&self, plugin_id: &str) -> PluginStatus {
        let loaded = self.loaded.read();
        loaded
            .get(plugin_id)
            .map(|p| p.status.clone())
            .unwrap_or(PluginStatus::Discovered)
    }

    /// 加载配置文件，返回合并后的配置 JSON。
    ///
    /// 扫描 `config_root` 目录下的所有 `.yaml` 文件（递归子目录），
    /// 每个文件以文件名（不含扩展名）为 key，解析后的 JSON 为 value。
    /// 多个文件合并为一个 flat JSON 对象。
    ///
    /// DEBT: 当前返回全量合并配置，所有插件共享同一份 config。ceiling: 所有插件
    /// 收到全系统配置，存在跨插件信息泄漏风险。upgrade: 当插件数量超过 20 个或
    /// 出现需要配置隔离的安全需求时，改为 load_config(manifest_id) 按插件过滤。
    async fn load_config(&self) -> Result<serde_json::Value, agentos_core::types::PluginError> {
        let config_root = match &self.config_root {
            Some(root) => root,
            None => return Ok(serde_json::json!({})),
        };

        if !config_root.exists() {
            warn!("Config root does not exist: {}", config_root.display());
            return Ok(serde_json::json!({}));
        }

        // 指纹先行（免读内容）：factory 根失败传播（与下方扫描的 IO 语义
        // 一致）；用户覆盖根失败降级为空段（与 apply_user_config_overlay
        // 的 warn+跳过同语义，factory-only 结果不被用户层读失败放大为整体
        // 失败）。TOCTOU 安全序：指纹早于内容读取，竞态窗口最坏一次多余
        // 重载，绝不缓存"指纹新于内容"的快照。
        let mut stamps = Vec::new();
        agentos_core::config_scan::collect_yaml_stamps(config_root, &mut stamps).map_err(|e| {
            agentos_core::types::PluginError {
                message: format!("Failed to stat config dir {}: {}", config_root.display(), e),
                code: Some("CONFIG_IO_ERROR".to_string()),
                source: Some("plugin-loader".to_string()),
            }
        })?;
        let user_stamp_count = collect_user_config_stamps(&mut stamps);
        stamps.sort_by(|a, b| a.path.cmp(&b.path));

        if let Some(snapshot) = self.config_cache.read().as_ref() {
            if snapshot.stamps == stamps {
                return Ok(snapshot.config.clone());
            }
        }

        let mut config_map = serde_json::Map::new();

        // 递归扫描经 agentos-core::config_scan 共用骨架；单文件**读失败与
        // 解析失败同语义**（告警跳过）——full_config 只是中间字典，真正注入靠
        // config_files[].path 精确定位，一个无关文件（如模板文档）不可读或
        // 解析失败不该连累全部插件收不到配置（2026-11-13 装机事故：单个非
        // UTF-8 文件使整个 config 目录装载失败，所有插件被注入空配置）。
        // 目录遍历本身失败仍经 read_dir_error 传播。
        let mut load_file = |path: &Path| -> Result<Option<serde_json::Value>, LoaderError> {
            let content = match std::fs::read_to_string(path) {
                Ok(content) => content,
                Err(e) => {
                    warn!("Skipping unreadable config file {}: {}", path.display(), e);
                    return Ok(None);
                }
            };
            // YAML → JSON Value（serde_yaml 直接反序列化到 serde_json::Value）。
            match serde_yaml::from_str(&content) {
                Ok(v) => Ok(Some(v)),
                Err(e) => {
                    warn!("Skipping unparseable config file {}: {}", path.display(), e);
                    Ok(None)
                }
            }
        };
        let mut read_dir_error = |dir: &Path, e: std::io::Error| -> LoaderError {
            LoaderError::Io {
                message: format!("Failed to read config dir {}: {}", dir.display(), e),
            }
        };
        agentos_core::config_scan::collect_yaml_dir(
            config_root,
            &mut config_map,
            &mut load_file,
            &mut read_dir_error,
        )
        .map_err(|e| {
            let code = match &e {
                LoaderError::Io { .. } => "CONFIG_IO_ERROR",
                LoaderError::ManifestParse { .. } => "CONFIG_PARSE_ERROR",
                _ => "CONFIG_LOAD_FAILED",
            };
            agentos_core::types::PluginError {
                message: format!(
                    "Failed to load config from {}: {}",
                    config_root.display(),
                    e
                ),
                code: Some(code.to_string()),
                source: Some("plugin-loader".to_string()),
            }
        })?;

        // 用户配置层叠加（ADR 2026-09-13-unified-user-root）：**文件级整体替换**——
        // 用户层存在的每个文件整体取代 factory 同路径文件，未被接管的文件仍读
        // factory。叠加点选在此（full_config 构建处）一处生效：所有走 config_files
        // 引用形态的插件经 build_injected_config 免费获得用户覆盖。
        // 叠加硬失败（用户根不可读）降级照旧（factory-only 值返回），但本次
        // 快照标记不可指纹化——指纹面缺失用户段时缓存会掩盖后续恢复，宁可靠
        // 重载兜底。
        let mut fingerprint_clean = user_stamp_count.is_ok();
        let user_replaced = match apply_user_config_overlay(&mut config_map) {
            Ok(replaced) => replaced,
            Err(_) => {
                fingerprint_clean = false;
                0
            }
        };

        let value = serde_json::Value::Object(config_map);
        *self.config_cache.write() = Some(ConfigSnapshot {
            stamps: if fingerprint_clean {
                stamps
            } else {
                Vec::new()
            },
            config: value.clone(),
        });

        let config_keys: Vec<String> = value
            .as_object()
            .map(|o| o.keys().cloned().collect())
            .unwrap_or_default();
        info!(
            "Loaded {} config entries from {}: [{}]{}",
            config_keys.len(),
            config_root.display(),
            config_keys.join(", "),
            if user_replaced > 0 {
                format!(" (+{user_replaced} from user space)")
            } else {
                String::new()
            }
        );

        Ok(value)
    }

    /// 获取插件的目录路径（包含 plugin.json/server.py 的目录）。
    ///
    /// 从已缓存的 manifest 发现路径中提取插件目录。
    fn get_plugin_dir(&self, plugin_id: &str) -> Option<String> {
        let manifests = self.manifests.read();
        manifests
            .get(plugin_id)
            .and_then(|path| path.parent().map(|p| p.to_string_lossy().to_string()))
    }

    /// 获取指定插件的 manifest（按需读盘 + 重新解析）。
    ///
    /// 供内核同步查询插件声明的运行时属性（如 lifecycle 空闲卸载阈值）。
    /// 读盘失败按未发现处理（返回 None）——manifest 在源路径上已丢失/损坏，
    /// 与"从未发现"同语义（调用方降级路径兜底）。
    fn get_manifest(&self, plugin_id: &str) -> Option<PluginManifest> {
        let path = {
            let manifests = self.manifests.read();
            manifests.get(plugin_id).cloned()?
        };
        self.read_manifest(&path)
            .map_err(|e| {
                warn!(
                    "get_manifest 读盘失败（按未发现处理）: {}: {}",
                    path.display(),
                    e
                );
                e
            })
            .ok()
    }

    /// 服务条目 → 声明提供方 id 列表（discover 期缓存直读，零读盘）。
    fn service_provider_ids(&self, service: &str) -> Vec<String> {
        self.service_providers
            .read()
            .get(service)
            .cloned()
            .unwrap_or_default()
    }
}

/// 发现候选收集与收口（live 与缓存两路径的共用底座；缓存消费入口
/// [`Self::discover_boot_cached`] 在 `loader/cache.rs`）。
impl PluginLoaderImpl {
    /// 全量候选收集（现行路径）：逐根扫描产出候选流。根序 = root_paths →
    /// 内置根 → 用户根（缓存路径 [`Self::gather_candidates_via_cache`] 同序，
    /// admit 裁决语义依赖此序一致）。单根扫描失败 warn 不阻断其余根（与原
    /// discover 内联循环逐字同款）。
    fn gather_candidates_live(&self, root_paths: &[&str]) -> Vec<DiscoverCandidate> {
        let mut candidates = Vec::new();
        for root_str in root_paths {
            let root = Path::new(root_str);
            match self.scan_root(root) {
                Ok(found) => {
                    for (manifest, path) in found {
                        candidates.push((manifest, path, false));
                    }
                }
                Err(e) => {
                    warn!("Failed to scan root {}: {}", root.display(), e);
                }
            }
        }
        match self.scan_root(&self.builtin_root) {
            Ok(found) => {
                for (manifest, path) in found {
                    candidates.push((manifest, path, false));
                }
            }
            Err(e) => {
                warn!(
                    root = %self.builtin_root.display(),
                    error = %e,
                    "Failed to scan builtin plugin root"
                );
            }
        }
        if let Some(user_root) = &self.user_root {
            match self.scan_root(user_root) {
                Ok(found) => {
                    for (manifest, path) in found {
                        candidates.push((manifest, path, false));
                    }
                }
                Err(e) => {
                    warn!(
                        root = %user_root.display(),
                        error = %e,
                        "Failed to scan user plugin root"
                    );
                }
            }
        }
        candidates
    }

    /// 发现收口（live 与缓存路径共用）：同 id admit 裁决 → 冲突对账日志 →
    /// 装机盲区告警 → 前端危险能力剥权 → invoke_entry 聚合校验 → 路径/服务
    /// 声明缓存重建。返回 (manifests, 缓存已验证胜者 id 集)——后者由候选流
    /// 的 from_cache 标记按胜者归属推导，live 路径恒空集。
    ///
    /// candidates 元组第三位 = 候选是否出自预烤发现缓存（digest 对账已验证）。
    fn finalize_discovery(
        &self,
        candidates: Vec<DiscoverCandidate>,
    ) -> Result<(Vec<PluginManifest>, HashSet<String>), agentos_core::types::PluginError> {
        let mut all_manifests = HashMap::new();
        let mut winner_verified: HashMap<PathBuf, bool> = HashMap::new();
        for (_manifest, path, from_cache) in &candidates {
            winner_verified.entry(path.clone()).or_insert(*from_cache);
        }

        let mut conflicts: SameIdConflicts = HashMap::new();
        for (manifest, path, _from_cache) in candidates {
            self.admit(&mut all_manifests, &mut conflicts, manifest, path);
        }
        let cache_verified_winner_ids: HashSet<String> = all_manifests
            .iter()
            .filter_map(|(id, (_, path))| {
                winner_verified
                    .get(path)
                    .filter(|verified| **verified)
                    .map(|_| id.clone())
            })
            .collect();

        // 同 id 多候选对账日志（排障留痕）：分级单副本模型下该形态 = 同根
        // 畸形布局或守卫未及的热发现窗口，逐 id 如实打出落选版本与胜者版本
        // （version-max 归约）。id 排序保证日志行序确定。
        let mut conflict_ids: Vec<String> = conflicts.keys().cloned().collect();
        conflict_ids.sort();
        for id in &conflict_ids {
            let candidates = &conflicts[id];
            let Some((winner, _)) = all_manifests.get(id) else {
                continue;
            };
            info!(
                "Same-id duplicate candidates resolved by max version: id={} losers=[{}] winner={}",
                id,
                candidates.join(", "),
                winner.version
            );
        }

        // ── 准入分级（2026-09-25；2026-10-23 剥权时机修正）──
        // 危险前端能力按授予剥权：检测在前、剥权在后；剥权写入胜者 manifest
        // 的 restricted_capabilities 供 /api/v1/plugins 与设置页可见（禁静默
        // 降级）。每轮 discover 全量重建 all_manifests 后重算——授予变化或
        // 清单篡改即时生效。仅对胜者执行：生效面恒为胜者，未胜出副本（播种
        // 产物）不剥权不打警告——剥权曾在扫描校验期逐副本运行，未胜出副本
        // 也被剥并打 "registration face" 警告，纯噪声（2026-10-23 实证）。
        for (winner, winner_path) in all_manifests.values_mut() {
            let declared = detect_frontend_dangerous_caps(winner.contributes.as_ref());
            let not_granted = self.frontend_caps_not_granted(&winner.id, winner_path, &declared);
            if !not_granted.is_empty() {
                strip_frontend_capabilities(winner, &not_granted);
                winner.restricted_capabilities =
                    not_granted.iter().map(|cap| (*cap).to_string()).collect();
                warn!(
                    "Plugin '{}' dangerous frontend capabilities not granted by allowlist, stripped from registration face: {:?}",
                    winner.id, not_granted
                );
            }
        }

        // ADR 附录 D③/D.5（P6 命名治理）：启动期聚合校验 invoke_entry。
        // pipeline 类型插件的 MCP 入口名必须显式声明（不再隐式回退 capabilities.tools）。
        // 收集所有缺失项一次性报错——不逐个 panic，避免"修一个崩一个"的迁移体验。
        // D.6 槽位拆分：capabilities.tools = LLM 工具（声明即
        // 注册，不分类型）；capabilities.services = 内部服务方法。两者都不要求
        // invoke_entry（那是管道入口的声明）。
        let mut missing_invoke_entry: Vec<String> = all_manifests
            .values()
            .map(|(m, _)| m)
            .filter(|m| m.plugin_type == PluginType::Pipeline && m.invoke_entry.is_none())
            .map(|m| m.id.clone())
            .collect();
        if !missing_invoke_entry.is_empty() {
            missing_invoke_entry.sort();
            return Err(agentos_core::types::PluginError {
                message: format!(
                    "pipeline plugins missing manifest.invoke_entry (ADR 附录 D②): [{}]",
                    missing_invoke_entry.join(", ")
                ),
                code: Some("MISSING_INVOKE_ENTRY".to_string()),
                source: Some("plugin-loader".to_string()),
            });
        }

        // 更新缓存（只存轻量路径摘要；完整 manifest 由调用方按需读盘解析）
        let mut cache = self.manifests.write();
        cache.clear();
        for (id, (_, path)) in &all_manifests {
            cache.insert(id.clone(), path.clone());
        }
        drop(cache);
        // 服务声明缓存同刀重建（winners 全集）：manifest 体在手，零额外读盘。
        {
            let mut services = self.service_providers.write();
            services.clear();
            for (id, (m, _)) in &all_manifests {
                for svc in &m.capabilities.services {
                    services
                        .entry(svc.name.clone())
                        .or_default()
                        .push(id.clone());
                }
            }
        }

        Ok((
            all_manifests.into_values().map(|(m, _)| m).collect(),
            cache_verified_winner_ids,
        ))
    }
}

impl PluginLoaderImpl {
    /// 从缓存路径读盘 + 解析完整 manifest（JSON 优先，YAML 兜底——与
    /// discover 扫描同一解析顺序）。解析失败返回 Err（调用方决定语义）。
    ///
    /// 供 [`Self::get_manifest`] 与 [`PluginLoader::load`] 按需读盘用——
    /// 运行时完整 manifest 不常驻内存（唯一真源在 AppState.manifests）。
    /// 危险前端能力授予判定：返回清单声明了但**未获授予**的能力子集。
    ///
    /// 授予规则（2026-09-25 准入分级；信任边界改根归属，ADR 2026-11-07
    /// §2.1——形态策略门控随双源仲裁退役）：
    /// - Tier1 根插件（应用目录只读可信面）→ 全授予；
    /// - Tier2 根插件：allowlist 未部署（文件缺失，引导默认）→ 全授予；
    ///   已部署 → 按 allowlist 条目 `grants`（无条目 = 全不授予）。
    fn frontend_caps_not_granted(
        &self,
        manifest_id: &str,
        source_path: &Path,
        caps: &[&'static str],
    ) -> Vec<&'static str> {
        // 信任边界 = 根归属（分级单副本模型 ADR 2026-11-07 §2.1）：Tier2
        // 根（用户目录，可写）插件的危险前端能力须 allowlist 授予，Tier1 根
        // （应用目录，ACL 只读）可信不剥。白名单未部署（allowlist.deployed
        // = false）= 剥权面未启用，全放行。
        if caps.is_empty() || !self.is_user_rooted(source_path) || !self.allowlist.deployed {
            return Vec::new();
        }
        let granted = self
            .allowlist
            .plugins
            .iter()
            .find(|e| e.id == manifest_id)
            .map(|e| &e.grants);
        caps.iter()
            .filter(|cap| !granted.is_some_and(|g| g.iter().any(|g| g.as_str() == **cap)))
            .copied()
            .collect()
    }

    fn read_manifest(&self, path: &Path) -> Result<PluginManifest, LoaderError> {
        let content = std::fs::read_to_string(path).map_err(|e| LoaderError::Io {
            message: format!("Failed to read {}: {}", path.display(), e),
        })?;
        match serde_json::from_str::<PluginManifest>(&content) {
            Ok(m) => Ok(m),
            Err(json_err) => serde_yaml::from_str::<PluginManifest>(&content).map_err(|yaml_err| {
                LoaderError::ManifestParse {
                    path: path.display().to_string(),
                    message: format!("json error: {json_err}, yaml error: {yaml_err}"),
                }
            }),
        }
    }

    /// 获取所有已发现插件的根目录映射（plugin_id → 插件目录绝对路径）。
    ///
    /// HTTP dispatcher 据此把 `/ext/{plugin_id}/assets/{*path}`
    /// 解析到 `<plugin_dir>/web/<path>` 直读文件返回，免去为每个子资源单独声明
    /// http_endpoints。AppState 经 `with_plugin_loader` 持本活对象句柄，
    /// 消费点现取（本表每轮 discover 全量重建，watcher 热发现即时可见——
    /// B1：boot 快照 `AppState.plugin_dirs` 已退役）。
    pub fn get_plugin_dirs(&self) -> std::collections::HashMap<String, std::path::PathBuf> {
        let manifests = self.manifests.read();
        manifests
            .iter()
            .filter_map(|(id, path)| path.parent().map(|p| (id.clone(), p.to_path_buf())))
            .collect()
    }

    /// 单查已发现插件的根目录（活路径表现取，零全表物化）。
    pub fn plugin_dir(&self, plugin_id: &str) -> Option<std::path::PathBuf> {
        let manifests = self.manifests.read();
        manifests
            .get(plugin_id)
            .and_then(|path| path.parent().map(|p| p.to_path_buf()))
    }

    /// 已发现插件数（诊断面计数用，读锁 len 零物化）。
    pub fn plugin_dir_count(&self) -> usize {
        self.manifests.read().len()
    }
}

/// 发现面收集原语（ADR 2026-10-13 拍平配套）：递归找直接含 plugin.json /
/// plugin.yaml 的插件目录。目录布局无关（逐层下钻，不认目录名），是启动
/// 扫描根推导（`discover_plugin_roots`）、模式包约定资源视图聚合
/// （api/src/mode_view.rs，ADR 2026-10-15-mode-registry-retirement）共用的
/// 同一份发现面真相；此前 kernel bin / invoker / watcher 三处各持一份拷贝，
/// 收敛于此（复制必漂移）。
pub fn collect_plugin_dirs(dir: &std::path::Path) -> Vec<std::path::PathBuf> {
    let mut dirs = Vec::new();
    collect_plugin_dirs_into(dir, &mut dirs);
    dirs
}

fn collect_plugin_dirs_into(dir: &std::path::Path, dirs: &mut Vec<std::path::PathBuf>) {
    let entries = match std::fs::read_dir(dir) {
        Ok(e) => e,
        Err(_) => return,
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            // 直接含 manifest 的目录即插件目录（不再下钻）；否则继续递归
            if path.join("plugin.json").exists() || path.join("plugin.yaml").exists() {
                dirs.push(path);
            } else {
                collect_plugin_dirs_into(&path, dirs);
            }
        }
    }
}

mod config_overlay;
mod managed_mcp;

#[cfg(test)]
mod tests;

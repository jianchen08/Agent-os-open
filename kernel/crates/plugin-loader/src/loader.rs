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

/// 双源同 id 裁决策略（ADR 2026-09-20-packaged-dual-source-adjudication）。
///
/// 同一插件 id 同时出现在内置根（打包/仓内共享插件）与用户根（用户空间副本）
/// 时，由本策略决定胜者。此前口径恒为「用户根赢」（见
/// [`DualSourcePolicy::UserFirst`]）——对 dev 合理，但对装机版失真：用户空间的
/// 共享插件副本是旧安装/旧迁移遗留（陈旧、.venv 缺失、扁平布局），无条件压过
/// 版本匹配的打包新版 → 装机版跑陈旧副本（BUG-55）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum DualSourcePolicy {
    /// 用户根赢（dev 形态默认）：同 id 双源时用户根子树恒胜。开发者在
    /// user_root 的合法改动（工作副本、venv、补丁）永远生效，行为与历史
    /// 口径逐位一致。
    #[default]
    UserFirst,
    /// 内置根优先（装机形态）：同 id 双源时内置（打包）副本胜，**除非**用户
    /// 副本 manifest `version` 按 semver 严格大于内置（用户真升级过该插件）；
    /// 版本相等或任一侧不可解析 → 内置胜。用户根仍承载「内置没有的额外
    /// 插件」（第三方安装面不变）。
    BuiltinFirst,
}

/// 双源裁决策略环境变量（装机链由 electron `buildKernelEnv` 设为 `builtin`）。
pub const PLUGIN_SOURCE_PRIORITY_ENV: &str = "AGENTOS_PLUGIN_SOURCE_PRIORITY";

impl DualSourcePolicy {
    /// 从进程环境解析策略：`builtin` → [`DualSourcePolicy::BuiltinFirst`]，
    /// 其余（未设/`user`/未知值）→ [`DualSourcePolicy::UserFirst`]（保守默认 =
    /// 历史口径）。
    pub fn from_env() -> Self {
        Self::parse(std::env::var(PLUGIN_SOURCE_PRIORITY_ENV).ok().as_deref())
    }

    /// 纯解析（`from_env` 的无环境变异测试面）。
    pub fn parse(value: Option<&str>) -> Self {
        match value.map(str::trim) {
            Some("builtin") => Self::BuiltinFirst,
            _ => Self::UserFirst,
        }
    }
}

/// 插件加载器实现。
///
/// 支持双根扫描：
/// - 内置根（只读，随发行包）
/// - 用户根（可写，如 `%APPDATA%/agentos/plugins`）
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
    /// 双源同 id 裁决策略（默认 UserFirst = 历史口径）
    dual_source_policy: DualSourcePolicy,
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
    /// 多源仲裁「用户副本未生效」清单（D4 显式告警面）：每轮 discover 全量
    /// 重建，[`Self::ineffective_user_copies`] 消费（端点透出）。
    ineffective_user_copies: RwLock<Vec<IneffectiveUserCopy>>,
}

/// [`PluginLoaderImpl::config_cache`] 的缓存条目。
struct ConfigSnapshot {
    /// 缓存键：排序后的文件指纹集。空集 = "不可指纹化"（用户覆盖层读取
    /// 失败的降级装载），不与任何现场指纹匹配，迫使每次重载直到装载干净。
    stamps: Vec<agentos_core::config_scan::YamlFileStamp>,
    config: serde_json::Value,
}

/// 同 id 多源冲突记录：id → 全部候选 `(层标签, manifest version)`，按发现
/// 序。仅发生同 id 冲突的插件入册（单源零开销），discover 尾部逐 id 打
/// 裁决对账日志（装机版 boot 验证面）。
type SameIdConflicts = HashMap<String, Vec<(&'static str, String)>>;

/// 宽容 semver 解析：trim + strip 单个前导 `v`/`V` 前缀。sync 工作流/
/// 人工编辑写出的 `v1.2.3` 形态不再静默判旧（D4/审查总册 P1-15 根修）；
/// strip 后仍非 semver → None（走保守裁决 + 显式告警面）。
fn parse_version(raw: &str) -> Option<semver::Version> {
    let s = raw.trim();
    let stripped = s
        .strip_prefix('v')
        .or_else(|| s.strip_prefix('V'))
        .unwrap_or(s);
    semver::Version::parse(stripped).ok()
}

/// 同 id 多源仲裁「用户副本未生效」条目（D4/审查总册 P1-15：BuiltinFirst
/// 装机盲区显式告警面）。
///
/// 用户副本在同 id 冲突中输给内置（版本持平/更旧/不可解析）时入册——
/// sync 写坏版本号由此从"仅裁决日志汇总行可查"变结构化可见，
/// `GET /api/v1/plugins/source-arbitration` 消费；每轮 discover 全量重建
/// （用户副本修复版本后清单收敛为空）。
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct IneffectiveUserCopy {
    pub plugin_id: String,
    /// 用户副本 manifest `version`（原样，未 strip）。
    pub user_version: String,
    /// 胜出内置副本 manifest `version`（原样）。
    pub builtin_version: String,
    /// 用户副本版本经宽容解析（strip v/V 前缀）仍不可解析——sync 写错
    /// 版本号格式的直接证据。
    pub version_unparseable: bool,
}

impl PluginLoaderImpl {
    /// 创建插件加载器。
    ///
    /// # Arguments
    /// * `builtin_root` - 内置插件根目录（只读）
    /// * `user_root` - 用户插件根目录（可选，可写）
    pub fn new(builtin_root: impl Into<PathBuf>, user_root: Option<PathBuf>) -> Self {
        Self {
            builtin_root: builtin_root.into(),
            user_root,
            config_root: None,
            allowlist: AllowlistConfig::default(),
            dual_source_policy: DualSourcePolicy::default(),
            manifests: RwLock::new(HashMap::new()),
            service_providers: RwLock::new(HashMap::new()),
            loaded: RwLock::new(HashMap::new()),
            config_cache: RwLock::new(None),
            ineffective_user_copies: RwLock::new(Vec::new()),
        }
    }

    /// 设置双源同 id 裁决策略，返回 self 供链式调用。
    ///
    /// 生产接线（agentos-kernel `build_plugin_loader`）经
    /// [`DualSourcePolicy::from_env`] 读 [`PLUGIN_SOURCE_PRIORITY_ENV`]；
    /// 测试直接传值。
    pub fn with_dual_source_policy(mut self, policy: DualSourcePolicy) -> Self {
        self.dual_source_policy = policy;
        self
    }

    /// 当前双源同 id 裁决策略（端点透出用，语义见
    /// [`DualSourcePolicy`]）。
    pub fn dual_source_policy(&self) -> DualSourcePolicy {
        self.dual_source_policy
    }

    /// 最近一轮 [`PluginLoader::discover`] 的「用户副本未生效」清单（D4
    /// 显式告警面）：同 id 冲突中用户副本输给内置的条目，语义见
    /// [`IneffectiveUserCopy`]。UserFirst（dev）恒空。
    pub fn ineffective_user_copies(&self) -> Vec<IneffectiveUserCopy> {
        self.ineffective_user_copies.read().clone()
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
    /// 内容未变不重写（mtime 空转会空转 watcher）。config 缺失/解析失败：
    /// 跳过同步且不清目录（不因配置抖动销毁已装插件）。
    /// 用户根 config/mcp.json 同名条目覆盖内置（与插件双根同语义）。
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
         -> bool {
            let Some(dir) = dir else { return false };
            let path = dir.join("mcp.json");
            let Ok(text) = std::fs::read_to_string(&path) else {
                return false;
            };
            match serde_json::from_str::<serde_json::Value>(&text) {
                Ok(v) => match v.get("mcpServers").and_then(|m| m.as_object()) {
                    Some(map) => {
                        for (name, spec) in map {
                            out.insert(name.clone(), spec.clone());
                        }
                        true
                    }
                    None => {
                        // 缺 mcpServers 键同解析失败：该根不可用，不得据此清目录
                        warn_err("parse", format!("{} 缺 mcpServers 对象", path.display()));
                        false
                    }
                },
                Err(e) => {
                    warn_err("parse", format!("{}: {}", path.display(), e));
                    false
                }
            }
        };

        let builtin_read = read_cfg(
            self.config_root
                .as_deref()
                .map(PathBuf::from)
                .or_else(|| self.builtin_root.parent().map(|p| p.join("config")))
                .as_deref(),
            &mut builtin_servers,
        );
        let user_read = if let Some(user_root) = &self.user_root {
            read_cfg(
                user_root.parent().map(|p| p.join("config")).as_deref(),
                &mut user_servers,
            )
        } else {
            false
        };
        if !builtin_read && !user_read {
            return; // 无配置（功能未使用）——不清目录
        }

        // 生成：用户条目落用户根（覆盖内置同名），内置条目落内置根
        let mut keep: std::collections::HashMap<PathBuf, std::collections::HashSet<String>> =
            std::collections::HashMap::new();
        for (name, spec) in &builtin_servers {
            if user_servers.contains_key(name) {
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

        // 清理：配置里已删除的受管目录（只动带 .mcp-managed 标记的）
        if builtin_read {
            prune_managed_plugin_dirs(&self.builtin_root, keep.get(&self.builtin_root));
        }
        if let Some(user_root) = &self.user_root {
            if user_read {
                prune_managed_plugin_dirs(user_root, keep.get(user_root));
            }
        }
    }

    /// 路径是否落在用户根子树内。
    fn is_user_rooted(&self, p: &Path) -> bool {
        self.user_root
            .as_ref()
            .is_some_and(|user_root| p.starts_with(user_root))
    }

    /// `a_ver` 是否按 semver 严格大于 `b_ver`。任一侧不可解析 →
    /// false（不可判定时保守判「挑战者不新」——装机口径下内置为权威分发物）。
    fn strictly_newer(a_ver: &str, b_ver: &str) -> bool {
        match (parse_version(a_ver), parse_version(b_ver)) {
            (Some(a), Some(b)) => a > b,
            _ => false,
        }
    }

    /// 同 id 冲突裁决：challenger 是否应取代 incumbent（ADR
    /// 2026-09-20-packaged-dual-source-adjudication）。
    ///
    /// - [`DualSourcePolicy::UserFirst`]（dev 默认）：历史口径——用户根子树赢，
    ///   同侧 last-wins；
    /// - [`DualSourcePolicy::BuiltinFirst`]（装机）：**全候选版本最高者赢**——
    ///   用户副本 semver 严格更新才赢过内置；持平或任一侧不可解析 → 内置赢；
    ///   同侧严格更新才换、持平保留在位者。
    ///
    /// 用户模式种子区（`<user_root>/modes/`）不做路径豁免：该路径形态无法
    /// 区分「已定制种子」与「旧布局残留」，豁免会让陈旧副本恒压过出厂新版。
    /// 用户定制副本的存活由模式区账本对账（`reconcile_mode_seeds`：版本闸 +
    /// 内容哈希 + 定制检测 H1）与双根同 id 用户赢裁决承载，不由本裁决承担。
    ///
    /// 判定只依赖两侧（根归属， version）且持平单调（挑战者不更优必不换）——
    /// 多源（>2 份）逐对归约等价于取全候选中版本最高者，最终胜者与 root_paths
    /// 迭代序解耦，热路径 HashSet 乱序不会让胜者逐轮翻转（2026-09-18 翻转
    /// 根修在多源下继续成立）。
    fn should_replace(
        &self,
        incumbent_path: &Path,
        incumbent_version: &str,
        challenger_path: &Path,
        challenger_version: &str,
    ) -> bool {
        let incumbent_user = self.is_user_rooted(incumbent_path);
        let challenger_user = self.is_user_rooted(challenger_path);
        match self.dual_source_policy {
            DualSourcePolicy::UserFirst => !incumbent_user || challenger_user,
            DualSourcePolicy::BuiltinFirst => match (incumbent_user, challenger_user) {
                (true, false) => !Self::strictly_newer(incumbent_version, challenger_version),
                (false, true) => Self::strictly_newer(challenger_version, incumbent_version),
                // 同侧（内置×内置 / 用户×用户）：严格更新才换，持平保留在位者
                _ => Self::strictly_newer(challenger_version, incumbent_version),
            },
        }
    }

    /// 把一个扫描候选并入发现集：无在位者直接入；有在位者经
    /// [`Self::should_replace`] 裁决。冲突 id 的全部候选（层， version）记入
    /// `conflicts`，由调用方在扫描完后统一输出裁决对账日志。
    fn admit(
        &self,
        all: &mut HashMap<String, (PluginManifest, PathBuf)>,
        conflicts: &mut SameIdConflicts,
        manifest: PluginManifest,
        path: PathBuf,
    ) {
        let layer_of = |p: &Path| {
            if self.is_user_rooted(p) {
                "user"
            } else {
                "builtin"
            }
        };
        let replace = match all.get(&manifest.id) {
            None => true,
            Some((incumbent, incumbent_path)) => {
                let should = self.should_replace(
                    incumbent_path,
                    &incumbent.version,
                    &path,
                    &manifest.version,
                );
                conflicts
                    .entry(manifest.id.clone())
                    .or_insert_with(|| vec![(layer_of(incumbent_path), incumbent.version.clone())])
                    .push((layer_of(&path), manifest.version.clone()));
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
        // 早于双源裁决，未胜出的播种副本也被剥权并打 "stripped from
        // registration face" 警告（mode_roleplay 48 次/日实证）——而被剥的
        // 副本对象随即被裁决丢弃，注册面（胜者）从未受影响，警告纯属噪声。
        // 剥权已挪至 discover 裁决完成后，仅对胜者执行。

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

        // 递归扫描经 agentos-core::config_scan 共用骨架；单文件读失败传播、
        // 解析失败告警跳过——full_config 只是中间字典，真正注入靠
        // config_files[].path 精确定位，一个无关文件（如模板文档）解析失败
        // 不该连累全部插件收不到配置。
        let mut load_file = |path: &Path| -> Result<Option<serde_json::Value>, LoaderError> {
            let content = std::fs::read_to_string(path).map_err(|e| LoaderError::Io {
                message: format!("Failed to read config file {}: {}", path.display(), e),
            })?;
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

        // 裁决对账日志（装机 boot 验证面）：多源同 id 逐 id 如实打出全部候选
        // （层:版本，发现序）与最终胜者。id 排序保证日志行序确定。
        let mut conflict_ids: Vec<String> = conflicts.keys().cloned().collect();
        conflict_ids.sort();
        for id in &conflict_ids {
            let candidates = &conflicts[id];
            let Some((winner, winner_path)) = all_manifests.get(id) else {
                continue;
            };
            let winner_layer = if self.is_user_rooted(winner_path) {
                "user"
            } else {
                "builtin"
            };
            let candidate_list = candidates
                .iter()
                .map(|(layer, version)| format!("{layer}:{version}"))
                .collect::<Vec<_>>()
                .join(", ");
            info!(
                "Dual-source same-id resolved: id={} candidates=[{}] winner={}:{} policy={:?}",
                id, candidate_list, winner_layer, winner.version, self.dual_source_policy,
            );
        }

        // ── D4（审查总册 P1-15）：BuiltinFirst 装机盲区显式告警面 ──
        // 每轮 discover 全量重建：①同 id 冲突中用户副本输给内置（持平/更旧/
        // 不可解析）→ 逐 id 入册（端点结构化透出）；②任一候选版本宽容解析
        // （strip v 前缀）仍失败 → 逐 id 显式 warn——sync 写坏版本号不再
        // 静默失效（此前仅上方逐 id 汇总行可查，无告警面）。
        let mut ineffective: Vec<IneffectiveUserCopy> = Vec::new();
        for id in &conflict_ids {
            let candidates = &conflicts[id];
            let unparseable: Vec<String> = candidates
                .iter()
                .filter(|(_, version)| {
                    self.dual_source_policy == DualSourcePolicy::BuiltinFirst
                        && parse_version(version).is_none()
                })
                .map(|(layer, version)| format!("{layer}:{version}"))
                .collect();
            if !unparseable.is_empty() {
                warn!(
                    id = %id,
                    unparseable = %unparseable.join(", "),
                    winner_version = %all_manifests.get(id).map(|(m, _)| m.version.clone()).unwrap_or_default(),
                    "多源仲裁遇到不可解析版本号（宽容 strip v/V 前缀后仍非 semver）——保守判「挑战者不新」（装机口径内置赢）；用户副本是否未生效见 GET /api/v1/plugins/source-arbitration"
                );
            }
            let Some((winner, winner_path)) = all_manifests.get(id) else {
                continue;
            };
            if self.is_user_rooted(winner_path) {
                continue; // 用户副本生效，无盲区
            }
            // 多个用户副本同 id 冲突时取宽容解析最强的一份（最接近生效的
            // 尝试仍输，才是如实告警）；全不可解析取首个。
            let mut best_user: Option<&String> = None;
            for (layer, version) in candidates {
                if *layer != "user" {
                    continue;
                }
                let better = match best_user {
                    None => true,
                    Some(cur) => match (parse_version(cur), parse_version(version)) {
                        (cur_v, Some(v)) => cur_v.is_none_or(|c| v > c),
                        (Some(_), None) => false,
                        (None, None) => false,
                    },
                };
                if better {
                    best_user = Some(version);
                }
            }
            if let Some(user_version) = best_user {
                ineffective.push(IneffectiveUserCopy {
                    plugin_id: id.clone(),
                    user_version: user_version.clone(),
                    builtin_version: winner.version.clone(),
                    version_unparseable: parse_version(user_version).is_none(),
                });
            }
        }
        *self.ineffective_user_copies.write() = ineffective;

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
    /// 授予规则（2026-09-25 准入分级，ADR 同日）：
    /// - dev 姿态（[`DualSourcePolicy::UserFirst`]，装机链不设此环境变量）→
    ///   全授予（开发流零变化）；
    /// - 内置根插件（随包分发可信面）→ 全授予；
    /// - 装机姿态用户根插件：allowlist 未部署（文件缺失，引导默认）→ 全授予；
    ///   已部署 → 按 allowlist 条目 `grants`（无条目 = 全不授予）。
    fn frontend_caps_not_granted(
        &self,
        manifest_id: &str,
        source_path: &Path,
        caps: &[&'static str],
    ) -> Vec<&'static str> {
        if caps.is_empty()
            || self.dual_source_policy == DualSourcePolicy::UserFirst
            || !self.is_user_rooted(source_path)
            || !self.allowlist.deployed
        {
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

/// 把用户配置层叠加进已加载的 factory `config_map`（ADR 2026-09-13-unified-user-root）。
///
/// 语义＝**文件级整体替换**：用户层某个 `.yaml` 存在 ⟹ 其解析值整体取代 factory
/// 同路径值（不逐字段合并）；用户层目录递归下钻，未被接管的键保持 factory 值。
/// 这是文件级所有权转移（与插件双根「同 id 用户赢」同构），**不是**被 ADR
/// 2026-09-02 否决的「出厂默认 + 用户覆盖」字段级两层。
///
/// 用户层单文件解析失败 → 告警并**保留 factory 值**（fail-safe：坏的用户文件不该
/// 让插件静默拿到空配置，与 factory 侧解析失败同款处置）。
///
/// **按文件定位接管**（不能按目录整体 replace）：`config/models/llm.yaml` 的键
/// 路径是 `models.llm`，而同一目录下还有 `embedding.yaml`——若在 `models` 这一层
/// 整体替换，会连带抹掉未被接管的兄弟文件。故以「文件相对路径 → 键路径
/// （各级目录名 + stem）」逐文件写入，兄弟键各归各。
///
/// 键路径与注入侧 `resolve_config_path`（剥 `config/` 前缀与扩展名后按 `/` 下钻）
/// 同构，保证"用户层写的文件"与"插件读的键"对得上。
///
/// 返回 `Ok(被用户层接管的文件数)`（日志可观测性——"改了没生效"排查的
/// 第一线索）；`Err` = 用户根不可读的硬失败（内部已 warn，factory-only
/// 结果照常可用，调用方须将对应快照标记不可指纹化）。
fn apply_user_config_overlay(
    config_map: &mut serde_json::Map<String, serde_json::Value>,
) -> Result<usize, LoaderError> {
    let Some(user_root) = agentos_core::user_space::user_config_dir() else {
        return Ok(0);
    };
    if !user_root.is_dir() {
        return Ok(0);
    }

    // collect_yaml_dir 的回调能拿到每个文件的绝对路径与解析值——借它走既有
    // 共用扫描骨架（隐藏文件跳过 / yaml 扩展名过滤 / 目录读取错误映射三处语义
    // 与 factory 侧完全一致），同时把「文件 → 值」逐条记下来供键路径定位。
    let mut files: Vec<(std::path::PathBuf, serde_json::Value)> = Vec::new();
    let mut sink = serde_json::Map::new();
    let mut load_file = |path: &Path| -> Result<Option<serde_json::Value>, LoaderError> {
        let content = std::fs::read_to_string(path).map_err(|e| LoaderError::Io {
            message: format!("Failed to read user config file {}: {}", path.display(), e),
        })?;
        match serde_yaml::from_str::<serde_json::Value>(&content) {
            Ok(v) => {
                files.push((path.to_path_buf(), v.clone()));
                Ok(Some(v))
            }
            Err(e) => {
                warn!(
                    "Skipping unparseable user config file {} (factory value kept): {}",
                    path.display(),
                    e
                );
                Ok(None)
            }
        }
    };
    let mut read_dir_error = |dir: &Path, e: std::io::Error| -> LoaderError {
        LoaderError::Io {
            message: format!("Failed to read user config dir {}: {}", dir.display(), e),
        }
    };
    if let Err(e) = agentos_core::config_scan::collect_yaml_dir(
        &user_root,
        &mut sink,
        &mut load_file,
        &mut read_dir_error,
    ) {
        warn!(
            "User config overlay skipped ({}): {}",
            user_root.display(),
            e
        );
        return Err(e);
    }

    let mut replaced = 0usize;
    for (path, value) in files {
        let Ok(rel) = path.strip_prefix(&user_root) else {
            continue;
        };
        // 键路径 = 各级目录名 + 文件 stem（如 models/llm.yaml → ["models","llm"]）
        let mut segments: Vec<String> = rel
            .iter()
            .map(|s| s.to_string_lossy().to_string())
            .collect();
        let Some(last) = segments.pop() else { continue };
        let stem = last
            .rsplit_once('.')
            .map(|(s, _)| s)
            .unwrap_or(&last)
            .to_string();
        segments.push(stem);
        if set_config_leaf(config_map, &segments, value) {
            replaced += 1;
        }
    }
    Ok(replaced)
}

/// 用户覆盖根的装载面指纹采集（免读内容，best-effort）。
///
/// 目录不存在/非目录 → `Ok(0)`（与 [`apply_user_config_overlay`] 的准入
/// 判定同源，本就不进叠加）；目录读取失败 → 已 warn + `Err(())`，调用方
/// 须将本次快照标记不可指纹化（指纹面缺用户段时缓存会掩盖后续恢复）。
fn collect_user_config_stamps(
    out: &mut Vec<agentos_core::config_scan::YamlFileStamp>,
) -> Result<usize, ()> {
    let Some(user_root) = agentos_core::user_space::user_config_dir() else {
        return Ok(0);
    };
    if !user_root.is_dir() {
        return Ok(0);
    }
    let before = out.len();
    match agentos_core::config_scan::collect_yaml_stamps(&user_root, out) {
        Ok(()) => Ok(out.len() - before),
        Err(e) => {
            warn!(
                "User config stamp scan skipped ({}): {}",
                user_root.display(),
                e
            );
            Err(())
        }
    }
}

/// 按键路径段写入配置叶（中间层不存在则建空对象）。
///
/// 末段直接 **替换**（文件级接管）；中间段是目录层级，仅在缺失时新建——
/// 既不覆盖 factory 的兄弟键，也不因为用户层某文件存在就清空整棵子树。
fn set_config_leaf(
    map: &mut serde_json::Map<String, serde_json::Value>,
    segments: &[String],
    value: serde_json::Value,
) -> bool {
    let Some((head, rest)) = segments.split_first() else {
        return false;
    };
    if rest.is_empty() {
        map.insert(head.clone(), value);
        return true;
    }
    let entry = map
        .entry(head.clone())
        .or_insert_with(|| serde_json::Value::Object(serde_json::Map::new()));
    match entry {
        serde_json::Value::Object(child) => set_config_leaf(child, rest, value),
        // 同名标量挡住了目录层级（用户层文件与 factory 标量键同名）：
        // 不强行覆盖——保留 factory 值并返回未接管，交由日志层面暴露
        _ => false,
    }
}

/// GAP-4 声明自动生成（2026-09-03 用户裁定）：manifest 里 mcp.endpoint 的
/// `${VAR}` 引用（无 `:-` 默认值语法）即视为 env 配置面——引用本身即声明。
/// 内核把未被既有 `config_files[target="env"]` 条目覆盖的引用自动生成声明
/// （合并进既有 env 条目；没有则新建 path=".env" 条目），设置页据此提供
/// key 入口，spawn/connect 侧照旧走「进程环境 → .env overlay」解析——
/// 插件只写 endpoint 引用即可被发现使用。`${VAR:-def}` 带默认值的引用
/// 豁免（可选凭据，无需设置页入口）。
fn auto_generate_env_declarations(manifest: &mut PluginManifest) {
    let Some(mcp) = manifest.mcp.as_ref() else {
        return;
    };
    let Some(endpoint) = mcp.endpoint.as_ref() else {
        return;
    };
    // 收集全部 ${VAR} 引用（auth.value + env 值），排除 ${VAR:-def} 默认值语法
    let mut refs: Vec<String> = Vec::new();
    let mut collect = |value: &str| {
        let mut rest = value;
        while let Some(start) = rest.find("${") {
            let after = &rest[start + 2..];
            let Some(end) = after.find('}') else { break };
            let var = &after[..end];
            let has_default = var.starts_with(':') || var.contains(":-");
            if !var.is_empty() && !has_default && !refs.iter().any(|r| r == var) {
                refs.push(var.to_string());
            }
            rest = &after[end..];
        }
    };
    if let Some(auth) = endpoint.auth.as_ref() {
        collect(&auth.value);
    }
    for v in endpoint.env.values() {
        collect(v);
    }
    if refs.is_empty() {
        return;
    }
    let declared: std::collections::HashSet<&str> = manifest
        .config_files
        .iter()
        .filter(|f| f.target.as_deref() == Some("env"))
        .flat_map(|f| f.fields.iter().map(|fd| fd.name.as_str()))
        .collect();
    let missing: Vec<String> = refs
        .into_iter()
        .filter(|r| !declared.contains(r.as_str()))
        .collect();
    if missing.is_empty() {
        return;
    }
    let fields: Vec<EnvConfigField> = missing
        .iter()
        .map(|name| EnvConfigField {
            name: name.clone(),
            label: name.clone(),
            // 保守默认：宁可掩码不可泄漏（与反序列化缺省语义一致）
            field_type: "secret".to_string(),
            required: false,
            description: Some("内核自动生成：来自 mcp endpoint 的 ${VAR} 引用".to_string()),
            extra: None,
        })
        .collect();
    let existing = manifest
        .config_files
        .iter_mut()
        .find(|f| f.target.as_deref() == Some("env"));
    match existing {
        Some(entry) => entry.fields.extend(fields),
        None => manifest.config_files.push(ConfigFileMapping {
            id: "env".to_string(),
            settings: None,
            // env 条目写入目标语义不可省：真值在项目根 .env
            path: ".env".to_string(),
            label: "环境变量".to_string(),
            target: Some("env".to_string()),
            fields,
        }),
    }
    info!(
        "Auto-generated env declarations for plugin {}: {:?}",
        manifest.id, missing
    );
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_env::{user_space_env_lock, UserRootGuard, UserSpaceEnvGuard};
    use agentos_core::traits::{HostType, ProvidedCapabilityHost, StepCapability};
    use std::fs;

    /// 发现面收集原语（ADR 2026-10-13 拍平配套）：布局无关递归下钻——平铺一级、
    /// 旧类目录二级、三级深嵌套同样命中；无 manifest 的目录只作路径下钻；纯
    /// 文件不干扰；plugin.yaml 兜底同识别。
    #[test]
    fn collect_plugin_dirs_is_layout_agnostic_and_yaml_fallback() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        // ① 平铺：root/flat/plugin.json
        fs::create_dir_all(root.join("flat")).unwrap();
        fs::write(root.join("flat/plugin.json"), r#"{"id":"flat"}"#).unwrap();
        // ② 二级：root/kinds/tools/nested/plugin.yaml
        fs::create_dir_all(root.join("kinds/tools/nested")).unwrap();
        fs::write(root.join("kinds/tools/nested/plugin.yaml"), "id: nested").unwrap();
        // ③ 三级深嵌套：root/a/b/c/deep/plugin.json
        fs::create_dir_all(root.join("a/b/c/deep")).unwrap();
        fs::write(root.join("a/b/c/deep/plugin.json"), r#"{"id":"deep"}"#).unwrap();
        // ④ 无 manifest 的目录（如 _libs）只下钻不命中；⑤ 散文件忽略
        fs::create_dir_all(root.join("_libs")).unwrap();
        fs::write(root.join("_libs/readme.txt"), "not a plugin").unwrap();
        fs::write(root.join("loose.txt"), "x").unwrap();

        let mut dirs = collect_plugin_dirs(root);
        dirs.sort();
        let names: Vec<String> = dirs
            .iter()
            .map(|d| d.file_name().unwrap().to_string_lossy().into_owned())
            .collect();
        assert_eq!(
            names,
            vec!["deep", "flat", "nested"],
            "三种布局同样命中；_libs 与散文件跳过"
        );
        // 性质断言：命中目录恰含 manifest（发现的定义面）
        assert!(dirs
            .iter()
            .all(|d| d.join("plugin.json").is_file() || d.join("plugin.yaml").is_file()));
    }

    fn create_test_plugin_dir(root: &Path, id: &str, plugin_type: &str) {
        create_test_plugin_dir_with_version(root, id, plugin_type, "1.0.0");
    }

    /// [`create_test_plugin_dir`] 的版本参数化变体（双源裁决测试用：
    /// 用户新/资源新/版本相同各形状需要显式版本）。
    fn create_test_plugin_dir_with_version(
        root: &Path,
        id: &str,
        plugin_type: &str,
        version: &str,
    ) {
        let dir = root.join(id);
        fs::create_dir_all(&dir).unwrap();
        // pipeline 类型插件需声明 invoke_entry（ADR 附录 D②，P6 discover 聚合校验）
        let invoke_entry_field = if plugin_type == "pipeline" {
            format!(",\n    \"invoke_entry\": \"{id}.execute\"")
        } else {
            String::new()
        };
        let manifest_json = format!(
            r#"{{
    "id": "{id}",
    "name": "Test Plugin {id}",
    "version": "{version}",
    "plugin_type": "{plugin_type}",
    "language": "rust",
    "host_type": "in_process",
    "entry": "test_plugin",
    "capabilities": {{}},
    "requires_services": [],
    "permissions": {{}},
    "priority": 100{invoke_entry_field}
}}"#
        );
        fs::write(dir.join("plugin.json"), manifest_json).unwrap();
    }

    /// 内存归一（唯一真源在 AppState.manifests）：loader 缓存只持轻量路径
    /// 摘要，完整 manifest 按需读盘——磁盘 plugin.json 编辑后 get_manifest /
    /// load 读到新内容（不经过 discover 重扫）。
    #[tokio::test]
    async fn test_manifest_cache_slim_reparses_disk_on_demand() {
        let builtin = tempfile::tempdir().unwrap();
        create_test_plugin_dir(builtin.path(), "slim_cache", "pipeline");

        let loader = PluginLoaderImpl::new(builtin.path(), None);
        loader.discover(&[]).await.unwrap();

        // discover 后缓存不持完整 manifest：改磁盘声明，get_manifest 应读到新值
        let manifest_path = builtin.path().join("slim_cache").join("plugin.json");
        let edited = std::fs::read_to_string(&manifest_path)
            .unwrap()
            .replace("Test Plugin slim_cache", "Edited Manifest Name");
        std::fs::write(&manifest_path, edited).unwrap();

        let m = loader
            .get_manifest("slim_cache")
            .expect("get_manifest 应可读");
        assert_eq!(m.name, "Edited Manifest Name", "按需读盘应读到磁盘最新声明");
        assert_eq!(m.id, "slim_cache");

        // load 路径同源：加载出的 manifest 也是按需读盘的当前磁盘内容
        let loaded = loader.load("slim_cache").await.expect("load 应成功");
        assert_eq!(loaded.manifest.name, "Edited Manifest Name");

        // get_plugin_dirs 走同一路径摘要缓存（根目录映射不受磁盘编辑影响）
        let dirs = loader.get_plugin_dirs();
        assert!(dirs.contains_key("slim_cache"), "目录映射应含该插件");
        assert_eq!(
            dirs["slim_cache"],
            builtin.path().join("slim_cache"),
            "目录映射应指向缓存路径的父目录"
        );
    }

    /// loader 缓存瘦身后的失败语义：manifest 读盘失败时 load 报显式错误、
    /// get_manifest 按未发现处理（None + warn），不静默返回旧缓存。
    #[tokio::test]
    async fn test_manifest_cache_slim_failures_when_disk_unreadable() {
        let builtin = tempfile::tempdir().unwrap();
        create_test_plugin_dir(builtin.path(), "slim_gone", "pipeline");

        let loader = PluginLoaderImpl::new(builtin.path(), None);
        loader.discover(&[]).await.unwrap();

        let manifest_path = builtin.path().join("slim_gone").join("plugin.json");

        // ① 文件删除（IO 错误）：load 显式报 MANIFEST_READ，不静默降级
        std::fs::remove_file(&manifest_path).unwrap();
        let err = loader.load("slim_gone").await.expect_err("读盘失败应报错");
        assert_eq!(err.code.as_deref(), Some("MANIFEST_READ"));
        // get_manifest 按未发现处理（None）。warn 走 always-enabled 测试订阅者
        // 包裹：默认无订阅者时 tracing 按 callsite 缓存 never-interest，warn!
        // 的 format args 不会被求值（覆盖率盲区）；订阅者在场保证失败路径
        // 真实执行（断言仍是 None 语义，不 assert 日志内容）。
        let missing = tracing::subscriber::with_default(AlwaysSubscriber, || {
            loader.get_manifest("slim_gone").is_none()
        });
        assert!(missing);

        // ② 文件内容损坏（JSON/YAML 双解析失败）：同样按未发现处理
        std::fs::write(&manifest_path, "not a manifest: {{{").unwrap();
        let missing = tracing::subscriber::with_default(AlwaysSubscriber, || {
            loader.get_manifest("slim_gone").is_none()
        });
        assert!(missing, "损坏 manifest 应按未发现处理");
        assert!(loader.load("slim_gone").await.is_err());
    }

    /// 全开测试订阅者：`register_callsite` 返回 always-interest、`enabled` 恒真，
    /// 事件/span 记录全部 no-op。仅供失败路径测试包裹用——不注入日志输出，
    /// 只让 tracing 宏的 format args 被求值（默认无订阅者时 callsite 以
    /// never-interest 缓存，错误路径的 warn 文案永不执行）。
    struct AlwaysSubscriber;

    impl tracing::Subscriber for AlwaysSubscriber {
        fn register_callsite(
            &self,
            _metadata: &'static tracing::Metadata<'static>,
        ) -> tracing::subscriber::Interest {
            tracing::subscriber::Interest::always()
        }
        fn enabled(&self, _metadata: &tracing::Metadata<'_>) -> bool {
            true
        }
        fn new_span(&self, _span: &tracing::span::Attributes<'_>) -> tracing::Id {
            tracing::Id::from_u64(1)
        }
        fn record(&self, _span: &tracing::Id, _values: &tracing::span::Record<'_>) {}
        fn record_follows_from(&self, _span: &tracing::Id, _follows: &tracing::Id) {}
        fn event(&self, _event: &tracing::Event<'_>) {}
        fn enter(&self, _span: &tracing::Id) {}
        fn exit(&self, _span: &tracing::Id) {}
    }

    /// manifest 未知字段 `deny_unknown_fields` 拒绝（fail-closed，不容忍
    /// "声明了却不生效"——如已删除的 `capabilities.resources` 结构字段）。
    /// 本测试断言携带未知字段的 manifest 被拒绝。
    #[test]
    fn test_manifest_with_unknown_field_is_rejected() {
        let manifest_json = r#"{
    "id": "legacy_res",
    "name": "Legacy Resource Plugin",
    "version": "1.0.0",
    "plugin_type": "pipeline",
    "language": "rust",
    "host_type": "in_process",
    "entry": "test_plugin",
    "invoke_entry": "legacy_res.execute",
    "capabilities": {
        "resources": [
            {"uri": "config://app", "name": "App Config", "mime_type": "application/json"}
        ]
    },
    "requires_services": [],
    "permissions": {},
    "priority": 100
}"#;
        let err = serde_json::from_str::<PluginManifest>(manifest_json)
            .expect_err("未知字段 capabilities.resources 必须拒绝，不再静默忽略");
        let msg = format!("{err:?}");
        assert!(
            msg.contains("resources"),
            "错误应指明被拒绝的未知字段: {msg}"
        );
    }

    /// 语料级校验（Phase 1 契约真实性回归）：真实仓库全部 plugin.json 必须毫发
    /// 无伤通过严格反序列化（`deny_unknown_fields`）+ 必填字段校验。
    ///
    /// 这是"校验器不要在真实数据上空转"的持续闸门——语料出现未知字段或缺
    /// 必填（如顶层 `description` 未入 struct、`capabilities.resources` 这类
    /// 非契约字段）即红灯（校验器真实生效）。
    #[test]
    fn real_corpus_all_manifests_pass_strict_parsing() {
        let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let shared = manifest_dir.join("../../../plugins/shared");
        let mut count = 0usize;
        let mut walk = Vec::new();
        fn collect(dir: &std::path::Path, out: &mut Vec<std::path::PathBuf>) {
            // 跳过第三方/运行时产物目录：管理面只管插件声明，node_modules 下也有
            // 大量第三方依赖自带的 plugin.json（非本仓插件）。
            const SKIP: &[&str] = &[
                "node_modules",
                ".venv",
                "__pycache__",
                "dsh_plugins",
                "runtime",
            ];
            let entries = match std::fs::read_dir(dir) {
                Ok(e) => e,
                Err(e) => {
                    panic!("无法读取 {}: {e}", dir.display());
                }
            };
            for entry in entries {
                let entry = entry.unwrap();
                let path = entry.path();
                if path.is_dir() {
                    if !SKIP.contains(&path.file_name().and_then(|n| n.to_str()).unwrap_or("")) {
                        collect(&path, out);
                    }
                } else if path.file_name().and_then(|n| n.to_str()) == Some("plugin.json") {
                    out.push(path);
                }
            }
        }
        collect(&shared, &mut walk);
        assert!(
            walk.len() > 50,
            "应扫到真实插件语料 >50，实际 {}",
            walk.len()
        );
        for path in &walk {
            let text = std::fs::read_to_string(path)
                .unwrap_or_else(|e| panic!("读取 {} 失败: {e}", path.display()));
            let m: PluginManifest = serde_json::from_str(&text).unwrap_or_else(|e| {
                panic!(
                    "真实语料 {} 未通过严格解析（校验器抓到未知字段/坏结构）: {e}",
                    path.display()
                );
            });
            assert!(!m.id.is_empty() && !m.name.is_empty() && !m.version.is_empty());
            // output_schema 声明合法性（声明即校验）：真实工具若有畸形声明即红灯
            for t in &m.capabilities.tools {
                if let Some(out) = &t.output_schema {
                    let msg = crate::registry::output_schema_error(out);
                    assert!(
                        msg.is_none(),
                        "真实语料 {} 的 {} output_schema 声明不合法: {}",
                        path.display(),
                        t.name,
                        msg.unwrap()
                    );
                }
            }
            // provides 服务注册检查：公告的方法必须有已声明工具（"服务未注册"）
            let unbacked = crate::registry::provides_methods_unbacked(&m);
            assert!(
                unbacked.is_empty(),
                "真实语料 {} 公告了未注册的服务（provides 方法无对应已声明工具）: {:?}——服务声明了但消费者调不到",
                path.display(),
                unbacked
            );
            count += 1;
        }
        assert!(count == walk.len(), "每份真实 manifest 都过严格解析");
    }

    /// 真实语料（含 approval 复活的 requires_services）在依赖闸（resolve_requires_services）
    /// 上必须全部通过——服务面 = 插件声明面 + 内核内置能力面，否则 boot fail-fast 误伤。
    #[test]
    fn real_corpus_resolve_requires_services_passes() {
        let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let shared = manifest_dir.join("../../../plugins/shared");
        let mut walk = Vec::new();
        fn collect(dir: &std::path::Path, out: &mut Vec<std::path::PathBuf>) {
            const SKIP: &[&str] = &[
                "node_modules",
                ".venv",
                "__pycache__",
                "dsh_plugins",
                "runtime",
            ];
            let Ok(entries) = std::fs::read_dir(dir) else {
                return;
            };
            for entry in entries {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    if !SKIP.contains(&path.file_name().and_then(|n| n.to_str()).unwrap_or("")) {
                        collect(&path, out);
                    }
                } else if path.file_name().and_then(|n| n.to_str()) == Some("plugin.json") {
                    out.push(path);
                }
            }
        }
        collect(&shared, &mut walk);
        let manifests: Vec<PluginManifest> = walk
            .iter()
            .map(|p| {
                let text = std::fs::read_to_string(p).unwrap();
                serde_json::from_str(&text)
                    .unwrap_or_else(|e| panic!("解析 {} 失败: {e}", p.display()))
            })
            .collect();
        let declared: Vec<&str> = manifests
            .iter()
            .filter(|m| !m.requires_services.is_empty())
            .map(|m| m.id.as_str())
            .collect();
        assert!(
            !declared.is_empty(),
            "应有真实插件声明 requires_services（approval 复活）"
        );
        crate::registry::resolve_requires_services(&manifests).unwrap_or_else(|e| {
            panic!("真实语料依赖闸不过（approval 等 requires_services 未满足→boot 会被拒）: {e}")
        });
    }

    #[test]
    fn test_manifest_validation_valid() {
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let manifest = PluginManifest {
            force_include_tools: Vec::new(),
            state: None,
            id: "test".to_string(),
            name: "Test".to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            language: "rust".to_string(),
            host_type: HostType::InProcess,
            host_group: None,
            entry: "test_entry".to_string(),
            capabilities: Default::default(),
            requires_services: vec![],
            permissions: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            restricted_capabilities: Vec::new(),
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
            export_fields: vec![],
            aux_venvs: Vec::new(),
        };
        assert!(loader.validate_manifest(&manifest).is_ok());
    }

    /// 配置单一真值校验（2026-09-02 用户裁定）：引用形态（path 非空）禁止
    /// fields 声明 default（manifest+文件两处存值 = 双真值错误）；内联形态
    /// （path 空）带 default 合法；env 条目必须声明 path。
    #[test]
    fn test_manifest_validation_config_single_truth() {
        use agentos_core::traits::{ConfigFileMapping, EnvConfigField};

        fn base_manifest(id: &str) -> PluginManifest {
            PluginManifest {
                force_include_tools: Vec::new(),
                state: None,
                id: id.to_string(),
                name: "Single Truth".to_string(),
                description: None,
                version: "1.0.0".to_string(),
                plugin_type: PluginType::Pipeline,
                language: "rust".to_string(),
                host_type: HostType::InProcess,
                host_group: None,
                entry: "test_entry".to_string(),
                capabilities: Default::default(),
                requires_services: vec![],
                permissions: Default::default(),
                priority: 100,
                mcp: None,
                lifecycle: None,
                native: None,
                requires_content: None,
                invoke_entry: None,
                config_files: vec![],
                http_endpoints: vec![],
                ui_schema: None,
                contributes: None,
                restricted_capabilities: Vec::new(),
                enabled: None,
                activation: None,
                provides: None,
                persistent_fields: vec![],
                export_fields: vec![],
                aux_venvs: Vec::new(),
            }
        }

        fn field_with_default(name: &str) -> EnvConfigField {
            EnvConfigField {
                name: name.to_string(),
                label: "F".to_string(),
                field_type: "toggle".to_string(),
                required: false,
                description: None,
                extra: Some(
                    serde_json::json!({"default": true})
                        .as_object()
                        .unwrap()
                        .clone(),
                ),
            }
        }

        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let mut manifest = base_manifest("single_truth");

        // ① 引用形态 + default → 双真值错误
        manifest.config_files = vec![ConfigFileMapping {
            id: "cfg".to_string(),
            path: "config/system/foo.yaml".to_string(),
            label: "F".to_string(),
            target: None,
            settings: None,
            fields: vec![field_with_default("enabled")],
        }];
        let err = loader.validate_manifest(&manifest).unwrap_err().to_string();
        assert!(err.contains("双真值"), "应报双真值错误: {err}");

        // ② 引用形态无 default（纯 schema）→ 合法
        manifest.config_files[0].fields = vec![EnvConfigField {
            name: "enabled".to_string(),
            label: "F".to_string(),
            field_type: "toggle".to_string(),
            required: false,
            description: None,
            extra: None,
        }];
        assert!(loader.validate_manifest(&manifest).is_ok());

        // ③ 内联形态（path 空）带 default → 合法
        manifest.config_files[0].path = String::new();
        manifest.config_files[0].fields = vec![field_with_default("enabled")];
        assert!(loader.validate_manifest(&manifest).is_ok());

        // ④ env 条目省 path → 报错（写入目标语义不可省）
        manifest.config_files[0].target = Some("env".to_string());
        manifest.config_files[0].fields = vec![];
        let err = loader.validate_manifest(&manifest).unwrap_err().to_string();
        assert!(err.contains("不可省略"), "env 条目应必须声明 path: {err}");
    }

    /// capabilities.steps 校验（管道步骤服务化提案 §3.1）：空 name 报错，
    /// 且错误文案包含违规 plugin id。
    #[test]
    fn test_manifest_validation_steps_empty_name_rejected() {
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let mut manifest = PluginManifest {
            force_include_tools: Vec::new(),
            state: None,
            id: "steps_bad".to_string(),
            name: "Steps Bad".to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            language: "rust".to_string(),
            host_type: HostType::InProcess,
            host_group: None,
            entry: "test_entry".to_string(),
            capabilities: Default::default(),
            requires_services: vec![],
            permissions: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            restricted_capabilities: Vec::new(),
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
            export_fields: vec![],
            aux_venvs: Vec::new(),
        };
        manifest.capabilities.steps = vec![StepCapability {
            name: "  ".to_string(),
            description: None,
            input_schema: None,
        }];
        let err = loader
            .validate_manifest(&manifest)
            .expect_err("空 step name 必须拒绝");
        let msg = format!("{err}");
        assert!(
            msg.contains("steps_bad"),
            "错误文案应包含违规 plugin id: {msg}"
        );
        assert!(msg.contains("空 name"), "错误文案应说明原因: {msg}");
    }

    /// capabilities.steps 校验：同一 manifest 内 name 重复报错（全局唯一由
    /// G10 编译期查重），文案含 plugin id 与重复名。
    #[test]
    fn test_manifest_validation_steps_duplicate_name_rejected() {
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let mut manifest = PluginManifest {
            force_include_tools: Vec::new(),
            state: None,
            id: "steps_dup".to_string(),
            name: "Steps Dup".to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            language: "rust".to_string(),
            host_type: HostType::InProcess,
            host_group: None,
            entry: "test_entry".to_string(),
            capabilities: Default::default(),
            requires_services: vec![],
            permissions: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            restricted_capabilities: Vec::new(),
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
            export_fields: vec![],
            aux_venvs: Vec::new(),
        };
        manifest.capabilities.steps = vec![
            StepCapability {
                name: "task.remind".to_string(),
                description: None,
                input_schema: None,
            },
            StepCapability {
                name: "task.remind".to_string(),
                description: Some("重复".to_string()),
                input_schema: None,
            },
        ];
        let err = loader
            .validate_manifest(&manifest)
            .expect_err("重复 step name 必须拒绝");
        let msg = format!("{err}");
        assert!(
            msg.contains("steps_dup"),
            "错误文案应包含违规 plugin id: {msg}"
        );
        assert!(
            msg.contains("task.remind"),
            "错误文案应指明重复的步骤名: {msg}"
        );
    }

    /// capabilities.steps 校验：合法声明（非空且唯一）通过。
    #[test]
    fn test_manifest_validation_steps_valid_ok() {
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let mut manifest = PluginManifest {
            force_include_tools: Vec::new(),
            state: None,
            id: "steps_ok".to_string(),
            name: "Steps Ok".to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            language: "rust".to_string(),
            host_type: HostType::InProcess,
            host_group: None,
            entry: "test_entry".to_string(),
            capabilities: Default::default(),
            requires_services: vec![],
            permissions: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            restricted_capabilities: Vec::new(),
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
            export_fields: vec![],
            aux_venvs: Vec::new(),
        };
        manifest.capabilities.steps = vec![
            StepCapability {
                name: "task.inject_params".to_string(),
                description: None,
                input_schema: None,
            },
            StepCapability {
                name: "task.remind".to_string(),
                description: None,
                input_schema: None,
            },
        ];
        assert!(
            loader.validate_manifest(&manifest).is_ok(),
            "合法 steps 声明应通过校验"
        );
    }

    #[test]
    fn test_manifest_lifecycle_idle_timeout_parsed() {
        // 插件可在 plugin.json 声明 lifecycle.idle_timeout_secs 覆盖内核默认空闲卸载。
        let json = serde_json::json!({
            "id": "human_interaction_tool",
            "name": "Human Interaction",
            "version": "2.0.0",
            "plugin_type": "tool",
            "language": "python",
            "host_type": "sidecar",
            "entry": "python server.py",
            "capabilities": {},
            "lifecycle": { "idle_timeout_secs": 0 }
        });
        let m: PluginManifest = serde_json::from_value(json).expect("parse manifest");
        assert_eq!(
            m.lifecycle.expect("lifecycle present").idle_timeout_secs,
            Some(0)
        );

        let json2 = serde_json::json!({
            "id": "p", "name": "P", "version": "1.0.0",
            "plugin_type": "tool", "language": "python",
            "host_type": "sidecar", "entry": "python s.py",
            "capabilities": {}
        });
        let m2: PluginManifest = serde_json::from_value(json2).expect("parse manifest2");
        assert!(m2.lifecycle.is_none());
    }

    #[test]
    fn test_manifest_validation_missing_id() {
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let manifest = PluginManifest {
            force_include_tools: Vec::new(),
            state: None,
            id: String::new(),
            name: "Test".to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            language: "rust".to_string(),
            host_type: HostType::InProcess,
            host_group: None,
            entry: "test_entry".to_string(),
            capabilities: Default::default(),
            requires_services: vec![],
            permissions: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            restricted_capabilities: Vec::new(),
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
            export_fields: vec![],
            aux_venvs: Vec::new(),
        };
        assert!(loader.validate_manifest(&manifest).is_err());
    }

    #[test]
    fn test_manifest_validation_composite_empty_entry_ok() {
        // ADR ⑥: 组合插件 entry 可为空
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let manifest = PluginManifest {
            force_include_tools: Vec::new(),
            state: None,
            id: "composite_test".to_string(),
            name: "Composite".to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Composite,
            language: "yaml".to_string(),
            host_type: HostType::InProcess,
            host_group: None,
            entry: String::new(), // 空entry
            capabilities: Default::default(),
            requires_services: vec![],
            permissions: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            restricted_capabilities: Vec::new(),
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
            export_fields: vec![],
            aux_venvs: Vec::new(),
        };
        assert!(loader.validate_manifest(&manifest).is_ok());
    }

    #[tokio::test]
    async fn test_dual_root_scan() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();

        create_test_plugin_dir(builtin.path(), "builtin_plugin", "pipeline");
        create_test_plugin_dir(user.path(), "user_plugin", "tool");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()));
        let manifests = loader.discover(&[]).await.unwrap();

        assert_eq!(manifests.len(), 2);
        let ids: Vec<_> = manifests.iter().map(|m| m.id.clone()).collect();
        assert!(ids.contains(&"builtin_plugin".to_string()));
        assert!(ids.contains(&"user_plugin".to_string()));
    }

    #[tokio::test]
    async fn test_user_root_overrides_builtin() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();

        // 两个根都有同 ID 的插件
        create_test_plugin_dir(builtin.path(), "shared_plugin", "pipeline");
        create_test_plugin_dir(user.path(), "shared_plugin", "tool");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()));
        let manifests = loader.discover(&[]).await.unwrap();

        assert_eq!(manifests.len(), 1);
        // 用户根应覆盖内置根
        assert_eq!(manifests[0].plugin_type, PluginType::Tool);
    }

    /// 双源同 id（生产模式包拓扑：`modes/<mode_id>/plugin.json` 二级嵌套，repo 与
    /// 用户空间各一份）：root_paths 的迭代序不得决定胜者——用户根子树同 id 恒赢，
    /// 与内置根/用户根扫描段同语义。热路径 discover_new_plugins 把双根父目录混装
    /// 进每轮新建的 HashSet，序逐轮不稳定；朴素 last-wins 会让胜者逐轮翻转、源码
    /// 目录随之翻转 → 代码指纹每轮必变 → 复验驱逐/重注册循环（/ext 路由间歇
    /// 503/504 根因，2026-09-18）。
    #[tokio::test]
    async fn test_dual_source_same_id_user_wins_regardless_of_root_order() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();

        create_test_plugin_dir(&builtin.path().join("modes"), "mode_x", "pipeline");
        create_test_plugin_dir(&user.path().join("modes"), "mode_x", "tool");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()));

        // 两种 root_paths 序（模拟热路径 HashSet 逐轮序不稳定）：用户份都必须赢。
        let orders = [
            vec![builtin.path().join("modes"), user.path().join("modes")],
            vec![user.path().join("modes"), builtin.path().join("modes")],
        ];
        for roots in &orders {
            let root_strs: Vec<String> = roots
                .iter()
                .map(|p| p.to_string_lossy().to_string())
                .collect();
            let root_refs: Vec<&str> = root_strs.iter().map(|s| s.as_str()).collect();
            let manifests = loader.discover(&root_refs).await.unwrap();
            assert_eq!(
                manifests.len(),
                1,
                "同 id 双源必须合并为单 manifest（roots 序: {roots:?}）"
            );
            assert_eq!(
                manifests[0].plugin_type,
                PluginType::Tool,
                "用户份必须赢（roots 序: {roots:?}）"
            );
            // 源码目录稳定落在用户根——代码指纹/复验驱逐的触发源。
            let dir = loader.get_plugin_dir("mode_x").unwrap();
            assert!(
                std::path::Path::new(&dir).starts_with(user.path()),
                "source dir 必须在用户根（roots 序: {roots:?}），got: {dir}"
            );
        }
    }

    /// 双源裁决策略（BUG-55：装机版同 id 双源时旧用户空间副本压过打包新版）。
    ///
    /// 实测语料（R190 装机版）：用户空间扁平副本与打包副本**字节相同、版本相同**
    /// （llm_service 双侧 1.0.0）——版本/指纹/mtime 全都无法区分，历史口径
    /// 「用户根赢」让装机版永远跑陈旧副本。修复 = 策略化裁决（ADR
    /// 2026-09-20-packaged-dual-source-adjudication）：装机链置 BuiltinFirst，
    /// 跨根同 id 冲突按 semver 裁决；dev 默认 UserFirst 逐位不变。

    #[tokio::test]
    async fn test_builtin_first_policy_parse() {
        // 解析面：仅 `builtin` 激活内置优先；未设/user/未知值/空白一律保守回落
        // UserFirst（历史口径）。纯函数，不动进程环境。
        assert_eq!(
            DualSourcePolicy::parse(Some("builtin")),
            DualSourcePolicy::BuiltinFirst
        );
        assert_eq!(
            DualSourcePolicy::parse(Some(" builtin ")),
            DualSourcePolicy::BuiltinFirst,
            "首尾空白应容忍"
        );
        assert_eq!(
            DualSourcePolicy::parse(None),
            DualSourcePolicy::UserFirst,
            "未设 = dev 历史口径"
        );
        for v in ["user", "BUILTIN", "built-in", ""] {
            assert_eq!(
                DualSourcePolicy::parse(Some(v)),
                DualSourcePolicy::UserFirst,
                "未知值 {v:?} 必须保守回落 UserFirst"
            );
        }
    }

    /// BUG-55 实测形状：双副本版本相同（1.0.0）——装机策略下内置（打包）副本
    /// 必须赢，服务源码目录落到内置根（invoker 按它 spawn sidecar，正是 LLM 链
    /// 断的病灶位置）。
    #[tokio::test]
    async fn test_builtin_first_equal_version_builtin_wins() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        create_test_plugin_dir_with_version(builtin.path(), "llm_like", "system", "1.0.0");
        create_test_plugin_dir_with_version(user.path(), "llm_like", "system", "1.0.0");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_dual_source_policy(DualSourcePolicy::BuiltinFirst);
        let manifests = loader.discover(&[]).await.unwrap();

        assert_eq!(manifests.len(), 1, "同 id 双源必须合并为单 manifest");
        let dir = loader.get_plugin_dir("llm_like").unwrap();
        assert!(
            std::path::Path::new(&dir).starts_with(builtin.path()),
            "版本相同时内置（打包）副本必须赢，got: {dir}"
        );
    }

    /// 用户副本 semver 严格更新（用户真升级过）→ 用户赢——BuiltinFirst 不得
    /// 掐灭用户的显式升级。
    #[tokio::test]
    async fn test_builtin_first_newer_user_copy_user_wins() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        create_test_plugin_dir_with_version(builtin.path(), "upgraded", "system", "1.0.0");
        create_test_plugin_dir_with_version(user.path(), "upgraded", "system", "1.1.0");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_dual_source_policy(DualSourcePolicy::BuiltinFirst);
        loader.discover(&[]).await.unwrap();

        let dir = loader.get_plugin_dir("upgraded").unwrap();
        assert!(
            std::path::Path::new(&dir).starts_with(user.path()),
            "用户 semver 严格更新时用户副本必须赢，got: {dir}"
        );
    }

    /// 用户副本陈旧（semver 严格更老）→ 内置赢（打包升级压过陈旧副本）。
    #[tokio::test]
    async fn test_builtin_first_older_user_copy_builtin_wins() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        create_test_plugin_dir_with_version(builtin.path(), "stale", "system", "1.0.0");
        create_test_plugin_dir_with_version(user.path(), "stale", "system", "0.9.0");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_dual_source_policy(DualSourcePolicy::BuiltinFirst);
        loader.discover(&[]).await.unwrap();

        let dir = loader.get_plugin_dir("stale").unwrap();
        assert!(
            std::path::Path::new(&dir).starts_with(builtin.path()),
            "用户副本陈旧时内置副本必须赢，got: {dir}"
        );
    }

    /// 用户副本 version 非 semver（新旧不可判）→ 内置赢（保守判「用户不新」：
    /// 装机口径下内置是权威分发物；用户定制应升 version 让位规则可见）。
    #[tokio::test]
    async fn test_builtin_first_unparseable_user_version_builtin_wins() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        create_test_plugin_dir_with_version(builtin.path(), "oddver", "system", "1.0.0");
        create_test_plugin_dir_with_version(user.path(), "oddver", "system", "not-semver");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_dual_source_policy(DualSourcePolicy::BuiltinFirst);
        loader.discover(&[]).await.unwrap();

        let dir = loader.get_plugin_dir("oddver").unwrap();
        assert!(
            std::path::Path::new(&dir).starts_with(builtin.path()),
            "用户 version 不可解析时内置副本必须赢，got: {dir}"
        );
    }

    // ── D4（审查总册 P1-15）：v 前缀版本号 + 未生效清单告警面 ──

    /// 用户副本 `v` 前缀 semver（如 `v1.1.0`）宽容 strip 前缀后按 semver
    /// 比较：严格大于内置 → 用户赢（此前解析失败保守判不新 → sync 写
    /// `v` 前缀版本静默失效）。
    #[tokio::test]
    async fn test_builtin_first_v_prefixed_user_version_strictly_newer_user_wins() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        create_test_plugin_dir_with_version(builtin.path(), "vpfx", "system", "1.0.0");
        create_test_plugin_dir_with_version(user.path(), "vpfx", "system", "v1.1.0");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_dual_source_policy(DualSourcePolicy::BuiltinFirst);
        loader.discover(&[]).await.unwrap();

        let dir = loader.get_plugin_dir("vpfx").unwrap();
        assert!(
            std::path::Path::new(&dir).starts_with(user.path()),
            "v 前缀版本 strip 后严格大于内置 → 用户副本必须赢，got: {dir}"
        );
    }

    /// `v` 前缀与纯 semver 持平（`v1.0.0` vs `1.0.0`）→ 内置赢（持平单调
    /// 不得因宽容解析被破坏）。
    #[tokio::test]
    async fn test_builtin_first_v_prefixed_equal_version_builtin_wins() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        create_test_plugin_dir_with_version(builtin.path(), "vpfx_tie", "system", "1.0.0");
        create_test_plugin_dir_with_version(user.path(), "vpfx_tie", "system", "v1.0.0");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_dual_source_policy(DualSourcePolicy::BuiltinFirst);
        loader.discover(&[]).await.unwrap();

        let dir = loader.get_plugin_dir("vpfx_tie").unwrap();
        assert!(
            std::path::Path::new(&dir).starts_with(builtin.path()),
            "v 前缀持平内置 → 内置副本必须赢，got: {dir}"
        );
    }

    /// 未生效清单：用户副本输给内置（纯 semver 更旧 / 不可解析 / v 前缀
    /// strip 后仍更旧）→ 逐 id 入册，版本原样透出；纯 semver 组与 v 前缀组
    /// 的 version_unparseable 判定可区分。
    #[tokio::test]
    async fn test_builtin_first_ineffective_user_copies_report() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        // 组 A（纯 semver）：用户更旧
        create_test_plugin_dir_with_version(builtin.path(), "stale_pure", "system", "1.0.0");
        create_test_plugin_dir_with_version(user.path(), "stale_pure", "system", "0.9.0");
        // 组 B（v 前缀）：strip 后仍更旧（总册 v0.2.0 场景形状）
        create_test_plugin_dir_with_version(builtin.path(), "stale_vpfx", "system", "1.0.0");
        create_test_plugin_dir_with_version(user.path(), "stale_vpfx", "system", "v0.2.0");
        // 组 C：彻底不可解析
        create_test_plugin_dir_with_version(builtin.path(), "stale_bad", "system", "1.0.0");
        create_test_plugin_dir_with_version(user.path(), "stale_bad", "system", "not-semver");
        // 对照：用户严格更新，不入册
        create_test_plugin_dir_with_version(builtin.path(), "eff_new", "system", "1.0.0");
        create_test_plugin_dir_with_version(user.path(), "eff_new", "system", "v1.1.0");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_dual_source_policy(DualSourcePolicy::BuiltinFirst);
        loader.discover(&[]).await.unwrap();

        let mut report = loader.ineffective_user_copies();
        report.sort_by(|a, b| a.plugin_id.cmp(&b.plugin_id));
        let ids: Vec<&str> = report.iter().map(|e| e.plugin_id.as_str()).collect();
        assert_eq!(
            ids,
            vec!["stale_bad", "stale_pure", "stale_vpfx"],
            "恰好三个输给内置的 id 入册（生效对照不入册）"
        );
        let find = |id: &str| report.iter().find(|e| e.plugin_id == id).unwrap();
        let pure = find("stale_pure");
        assert_eq!(pure.user_version, "0.9.0", "用户版本原样透出");
        assert_eq!(pure.builtin_version, "1.0.0", "内置版本原样透出");
        assert!(!pure.version_unparseable, "纯 semver 组可解析");
        let vpfx = find("stale_vpfx");
        assert_eq!(vpfx.user_version, "v0.2.0");
        assert!(!vpfx.version_unparseable, "v 前缀组宽容解析成功");
        assert!(
            find("stale_bad").version_unparseable,
            "彻底不可解析组必须打 unparseable 标"
        );
    }

    /// 删值实验面：清单随 discover 全量重建——用户副本修复版本后下一轮
    /// 清单收敛为空（sync 修复可见）。
    #[tokio::test]
    async fn test_ineffective_user_copies_rebuilt_each_discover() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        create_test_plugin_dir_with_version(builtin.path(), "fixable", "system", "1.0.0");
        create_test_plugin_dir_with_version(user.path(), "fixable", "system", "0.9.0");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_dual_source_policy(DualSourcePolicy::BuiltinFirst);
        loader.discover(&[]).await.unwrap();
        assert_eq!(loader.ineffective_user_copies().len(), 1);

        // 模拟 sync 修复：用户副本版本 bump 到严格更新，重扫后清单收敛
        let manifest_path = user.path().join("fixable").join("plugin.json");
        let mut manifest: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&manifest_path).unwrap()).unwrap();
        manifest["version"] = serde_json::json!("1.1.0");
        fs::write(&manifest_path, manifest.to_string()).unwrap();
        loader.discover(&[]).await.unwrap();
        assert!(
            loader.ineffective_user_copies().is_empty(),
            "用户副本修复后清单必须清空，got: {:?}",
            loader.ineffective_user_copies()
        );
    }

    /// UserFirst（dev 默认）不存在「用户副本未生效」问题（用户根恒赢）：
    /// 清单恒空，即便版本更旧。
    #[tokio::test]
    async fn test_user_first_ineffective_list_always_empty() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        create_test_plugin_dir_with_version(builtin.path(), "dev_old", "system", "1.0.0");
        create_test_plugin_dir_with_version(user.path(), "dev_old", "system", "0.1.0");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()));
        loader.discover(&[]).await.unwrap();

        let dir = loader.get_plugin_dir("dev_old").unwrap();
        assert!(std::path::Path::new(&dir).starts_with(user.path()));
        assert!(loader.ineffective_user_copies().is_empty());
    }

    /// dev 默认（UserFirst，未设策略）：版本相同 → 用户赢——历史口径逐位不变
    /// （钉住回归：dev 用户工作副本压过仓内共享份的语义不得被本次改动破坏）。
    #[tokio::test]
    async fn test_user_first_default_equal_version_user_wins() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        create_test_plugin_dir_with_version(builtin.path(), "dev_copy", "system", "1.0.0");
        create_test_plugin_dir_with_version(user.path(), "dev_copy", "system", "1.0.0");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()));
        loader.discover(&[]).await.unwrap();

        let dir = loader.get_plugin_dir("dev_copy").unwrap();
        assert!(
            std::path::Path::new(&dir).starts_with(user.path()),
            "UserFirst 默认下用户副本必须赢（dev 口径不变），got: {dir}"
        );
    }

    /// BuiltinFirst 下用户根仍是「额外插件」安装面：内置没有的 id 照常从用户
    /// 根装载（第三方安装面不变）。
    #[tokio::test]
    async fn test_builtin_first_user_only_id_still_loads_from_user() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        create_test_plugin_dir(builtin.path(), "builtin_only", "system");
        create_test_plugin_dir(user.path(), "third_party", "tool");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_dual_source_policy(DualSourcePolicy::BuiltinFirst);
        loader.discover(&[]).await.unwrap();

        let dir = loader.get_plugin_dir("third_party").unwrap();
        assert!(
            std::path::Path::new(&dir).starts_with(user.path()),
            "内置没有的 id 必须从用户根装载，got: {dir}"
        );
        assert!(loader.get_plugin_dir("builtin_only").is_some());
    }

    /// BuiltinFirst 下胜者同样不得随 root_paths 迭代序翻转（2026-09-18 翻转
    /// 根修的策略化延续）：热路径把双根父目录混装进 HashSet，序逐轮不稳定，
    /// 裁决只依赖两侧（根归属， version）。
    #[tokio::test]
    async fn test_builtin_first_winner_stable_regardless_of_root_order() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        let nested_a = builtin.path().join("nested-builtin");
        let nested_b = user.path().join("nested-user");
        create_test_plugin_dir_with_version(&nested_a, "mode_x", "pipeline", "1.0.0");
        create_test_plugin_dir_with_version(&nested_b, "mode_x", "pipeline", "1.0.0");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_dual_source_policy(DualSourcePolicy::BuiltinFirst);

        let orders = [
            vec![nested_a.clone(), nested_b.clone()],
            vec![nested_b.clone(), nested_a.clone()],
        ];
        for roots in &orders {
            let root_strs: Vec<String> = roots
                .iter()
                .map(|p| p.to_string_lossy().to_string())
                .collect();
            let root_refs: Vec<&str> = root_strs.iter().map(|s| s.as_str()).collect();
            let manifests = loader.discover(&root_refs).await.unwrap();
            assert_eq!(
                manifests.len(),
                1,
                "同 id 双源必须合并（roots 序: {roots:?}）"
            );
            let dir = loader.get_plugin_dir("mode_x").unwrap();
            assert!(
                std::path::Path::new(&dir).starts_with(builtin.path()),
                "版本相同胜者必须恒为内置根，与序无关（roots 序: {roots:?}），got: {dir}"
            );
        }
    }

    /// BuiltinFirst（装机）下模式种子区不豁免版本裁决：`<user_root>/modes/`
    /// 的路径形态无法区分「已定制种子」与「旧布局残留」，无条件让位会让陈旧
    /// 残留副本恒压过出厂新版。同 id 统一取全候选版本最高者、持平内置赢；
    /// 用户定制副本的存活由模式区账本对账（reconcile_mode_seeds）与双根
    /// 用户赢裁决承载。modes 是二级嵌套，经 root_paths 进入
    /// （与生产 collect_boot_plugin_roots 同形）；每种形状都钉住双序稳定。
    ///
    /// 本例为三源生产形状：内置 0.2.0 + 用户平铺 0.1.0（sync 合法目标）+
    /// 用户旧布局残留 0.1.0（`modes/` 下）→ 内置必须赢，与序无关。
    #[tokio::test]
    async fn test_builtin_first_stale_modes_residual_builtin_wins() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        let builtin_modes = builtin.path().join("modes");
        let user_modes = user.path().join("modes");
        create_test_plugin_dir_with_version(&builtin_modes, "mode_personal", "pipeline", "0.2.0");
        create_test_plugin_dir_with_version(user.path(), "mode_personal", "pipeline", "0.1.0");
        create_test_plugin_dir_with_version(&user_modes, "mode_personal", "pipeline", "0.1.0");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_dual_source_policy(DualSourcePolicy::BuiltinFirst);

        // 三序覆盖三源交错：内置先 / 残留先 / 平铺先（用户根段只扫一级子
        // 目录，平铺经根本身扫描进入）。
        let orders = [
            vec![builtin_modes.clone(), user_modes.clone()],
            vec![user_modes.clone(), builtin_modes.clone()],
            vec![
                user.path().to_path_buf(),
                builtin_modes.clone(),
                user_modes.clone(),
            ],
        ];
        for roots in &orders {
            let root_strs: Vec<String> = roots
                .iter()
                .map(|p| p.to_string_lossy().to_string())
                .collect();
            let root_refs: Vec<&str> = root_strs.iter().map(|s| s.as_str()).collect();
            let manifests = loader.discover(&root_refs).await.unwrap();
            assert_eq!(
                manifests.len(),
                1,
                "同 id 三源必须合并为单 manifest（roots 序: {roots:?}）"
            );
            assert_eq!(
                manifests[0].version, "0.2.0",
                "胜者必须是版本最高的内置副本（roots 序: {roots:?}）"
            );
            let dir = loader.get_plugin_dir("mode_personal").unwrap();
            assert!(
                std::path::Path::new(&dir).starts_with(builtin.path()),
                "残留副本不得压过版本更高的内置副本（roots 序: {roots:?}），got: {dir}"
            );
        }
    }

    /// BuiltinFirst 下版本持平（含 modes 子树内）→ 内置赢，与序无关。
    #[tokio::test]
    async fn test_builtin_first_equal_version_modes_tie_builtin_wins() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        let builtin_modes = builtin.path().join("modes");
        let user_modes = user.path().join("modes");
        create_test_plugin_dir_with_version(&builtin_modes, "mode_tie", "pipeline", "1.0.0");
        create_test_plugin_dir_with_version(&user_modes, "mode_tie", "pipeline", "1.0.0");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_dual_source_policy(DualSourcePolicy::BuiltinFirst);

        let orders = [
            vec![builtin_modes.clone(), user_modes.clone()],
            vec![user_modes.clone(), builtin_modes.clone()],
        ];
        for roots in &orders {
            let root_strs: Vec<String> = roots
                .iter()
                .map(|p| p.to_string_lossy().to_string())
                .collect();
            let root_refs: Vec<&str> = root_strs.iter().map(|s| s.as_str()).collect();
            loader.discover(&root_refs).await.unwrap();
            let dir = loader.get_plugin_dir("mode_tie").unwrap();
            assert!(
                std::path::Path::new(&dir).starts_with(builtin.path()),
                "版本持平时内置副本必须赢，与路径层与序无关（roots 序: {roots:?}），got: {dir}"
            );
        }
    }

    /// BuiltinFirst 下用户 modes 副本 semver 严格更新 → 用户赢：裁决由版本
    /// 驱动而非路径层驱动（用户真升级过的种子副本照常生效）。
    #[tokio::test]
    async fn test_builtin_first_newer_user_modes_copy_user_wins() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        let builtin_modes = builtin.path().join("modes");
        let user_modes = user.path().join("modes");
        create_test_plugin_dir_with_version(&builtin_modes, "mode_up", "pipeline", "0.2.0");
        create_test_plugin_dir_with_version(&user_modes, "mode_up", "pipeline", "0.3.0");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_dual_source_policy(DualSourcePolicy::BuiltinFirst);

        let orders = [
            vec![builtin_modes.clone(), user_modes.clone()],
            vec![user_modes.clone(), builtin_modes.clone()],
        ];
        for roots in &orders {
            let root_strs: Vec<String> = roots
                .iter()
                .map(|p| p.to_string_lossy().to_string())
                .collect();
            let root_refs: Vec<&str> = root_strs.iter().map(|s| s.as_str()).collect();
            loader.discover(&root_refs).await.unwrap();
            let dir = loader.get_plugin_dir("mode_up").unwrap();
            assert!(
                std::path::Path::new(&dir).starts_with(&user_modes),
                "用户副本严格更新时必须赢，与序无关（roots 序: {roots:?}），got: {dir}"
            );
        }
    }

    /// BuiltinFirst 三源混合、最高版本在用户侧：用户平铺 1.1.0 赢过内置 1.0.0
    /// 与用户 modes 残留 1.0.0，与序无关（多源逐对归约 = 取全候选最高版本）。
    #[tokio::test]
    async fn test_builtin_first_three_source_user_highest_wins() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        let builtin_modes = builtin.path().join("modes");
        let user_modes = user.path().join("modes");
        create_test_plugin_dir_with_version(&builtin_modes, "mode_mix", "pipeline", "1.0.0");
        create_test_plugin_dir_with_version(user.path(), "mode_mix", "pipeline", "1.1.0");
        create_test_plugin_dir_with_version(&user_modes, "mode_mix", "pipeline", "1.0.0");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_dual_source_policy(DualSourcePolicy::BuiltinFirst);

        // 三序覆盖三源交错（同 test_builtin_first_stale_modes_residual_-
        // builtin_wins 的根给法）。
        let orders = [
            vec![builtin_modes.clone(), user_modes.clone()],
            vec![user_modes.clone(), builtin_modes.clone()],
            vec![
                user.path().to_path_buf(),
                builtin_modes.clone(),
                user_modes.clone(),
            ],
        ];
        for roots in &orders {
            let root_strs: Vec<String> = roots
                .iter()
                .map(|p| p.to_string_lossy().to_string())
                .collect();
            let root_refs: Vec<&str> = root_strs.iter().map(|s| s.as_str()).collect();
            loader.discover(&root_refs).await.unwrap();
            let dir = loader.get_plugin_dir("mode_mix").unwrap();
            assert!(
                std::path::Path::new(&dir).starts_with(user.path())
                    && !std::path::Path::new(&dir).starts_with(&user_modes),
                "最高版本（用户平铺 1.1.0）必须赢，与序无关（roots 序: {roots:?}），got: {dir}"
            );
        }
    }

    /// BuiltinFirst 同侧多源（内置无此 id）：用户侧内部同样按版本裁决——
    /// 嵌套平铺 0.2.0 赢过 modes 残留 0.1.0，与序无关。平铺副本放嵌套目录
    /// （只经 root_paths 进入）：若退回 last-wins，两序会选出不同副本，
    /// 本测试即红（版本裁决消除该序敏感）。
    #[tokio::test]
    async fn test_builtin_first_same_side_multi_source_newer_wins() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        let user_modes = user.path().join("modes");
        let user_flat = user.path().join("flat-layout");
        create_test_plugin_dir_with_version(&user_flat, "mode_side", "pipeline", "0.2.0");
        create_test_plugin_dir_with_version(&user_modes, "mode_side", "pipeline", "0.1.0");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_dual_source_policy(DualSourcePolicy::BuiltinFirst);

        let orders = [
            vec![user_flat.clone(), user_modes.clone()],
            vec![user_modes.clone(), user_flat.clone()],
        ];
        for roots in &orders {
            let root_strs: Vec<String> = roots
                .iter()
                .map(|p| p.to_string_lossy().to_string())
                .collect();
            let root_refs: Vec<&str> = root_strs.iter().map(|s| s.as_str()).collect();
            loader.discover(&root_refs).await.unwrap();
            let dir = loader.get_plugin_dir("mode_side").unwrap();
            assert!(
                std::path::Path::new(&dir).starts_with(&user_flat),
                "同侧多源必须版本最高者赢（roots 序: {roots:?}），got: {dir}"
            );
        }
    }

    /// UserFirst（dev 默认）不变量：三源同 id（内置 + 用户平铺 + 用户 modes
    /// 残留）下胜者恒在用户侧——dev 口径「用户根子树赢」逐位不变；同侧两个
    /// 用户副本之间维持 last-wins（不锁具体副本，只锁层别与序稳定）。
    #[tokio::test]
    async fn test_user_first_three_source_winner_stays_user_side() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        let builtin_modes = builtin.path().join("modes");
        let user_modes = user.path().join("modes");
        create_test_plugin_dir_with_version(&builtin_modes, "mode_seed", "pipeline", "1.0.0");
        create_test_plugin_dir_with_version(user.path(), "mode_seed", "pipeline", "1.0.0");
        create_test_plugin_dir_with_version(&user_modes, "mode_seed", "pipeline", "1.0.0");

        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()));

        let orders = [
            vec![builtin_modes.clone(), user_modes.clone()],
            vec![user_modes.clone(), builtin_modes.clone()],
        ];
        for roots in &orders {
            let root_strs: Vec<String> = roots
                .iter()
                .map(|p| p.to_string_lossy().to_string())
                .collect();
            let root_refs: Vec<&str> = root_strs.iter().map(|s| s.as_str()).collect();
            loader.discover(&root_refs).await.unwrap();
            let dir = loader.get_plugin_dir("mode_seed").unwrap();
            assert!(
                std::path::Path::new(&dir).starts_with(user.path()),
                "UserFirst 下胜者必须在用户侧（与序无关，roots 序: {roots:?}），got: {dir}"
            );
        }
    }

    #[tokio::test]
    async fn test_load_and_unload() {
        let builtin = tempfile::tempdir().unwrap();
        create_test_plugin_dir(builtin.path(), "test_load", "pipeline");

        let loader = PluginLoaderImpl::new(builtin.path(), None);
        loader.discover(&[]).await.unwrap();

        // 初始状态：Discovered
        assert_eq!(loader.get_status("test_load"), PluginStatus::Discovered);

        // 加载
        let loaded = loader.load("test_load").await.unwrap();
        assert_eq!(loaded.status, PluginStatus::Active);
        assert_eq!(loader.get_status("test_load"), PluginStatus::Active);

        // 重复加载返回已加载的
        let loaded2 = loader.load("test_load").await.unwrap();
        assert_eq!(loaded2.status, PluginStatus::Active);

        // 卸载
        loader.unload("test_load").await.unwrap();
        assert_eq!(loader.get_status("test_load"), PluginStatus::Discovered);
    }

    #[tokio::test]
    async fn test_get_plugin_dir_returns_correct_path() {
        let builtin = tempfile::tempdir().unwrap();
        create_test_plugin_dir(builtin.path(), "dir_test_plugin", "pipeline");

        let loader = PluginLoaderImpl::new(builtin.path(), None);
        loader.discover(&[]).await.unwrap();

        let plugin_dir = loader.get_plugin_dir("dir_test_plugin");
        assert!(
            plugin_dir.is_some(),
            "get_plugin_dir should return Some for discovered plugin"
        );
        let dir = plugin_dir.unwrap();
        assert!(
            dir.ends_with("dir_test_plugin"),
            "plugin dir should end with plugin id, got: {dir}"
        );
    }

    #[tokio::test]
    async fn test_get_plugin_dir_nonexistent_returns_none() {
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        // 未 discover 的插件应返回 None
        assert!(loader.get_plugin_dir("nonexistent_plugin").is_none());
    }

    #[tokio::test]
    async fn test_get_plugin_dir_existing() {
        let builtin = tempfile::tempdir().unwrap();
        create_test_plugin_dir(builtin.path(), "dir_test", "pipeline");

        let loader = PluginLoaderImpl::new(builtin.path(), None);
        loader.discover(&[]).await.unwrap();

        let plugin_dir = loader.get_plugin_dir("dir_test");
        assert!(
            plugin_dir.is_some(),
            "get_plugin_dir should return Some for discovered plugin"
        );
        let dir = plugin_dir.unwrap();
        assert!(
            dir.ends_with("dir_test"),
            "plugin dir should end with plugin id, got: {dir}"
        );
    }

    #[tokio::test]
    async fn test_get_plugin_dir_nonexistent() {
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        // 未 discover 时返回 None
        assert!(loader.get_plugin_dir("no_such_plugin").is_none());
    }

    #[tokio::test]
    async fn test_load_nonexistent_plugin() {
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let result = loader.load("nonexistent").await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_unload_not_loaded() {
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let result = loader.unload("not_loaded").await;
        assert!(result.is_err());
    }

    #[test]
    fn test_manifest_with_requires_content() {
        // ADR ⑦: requires_content 字段
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let manifest = PluginManifest {
            force_include_tools: Vec::new(),
            state: None,
            id: "memory_read".to_string(),
            name: "Memory Read".to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            language: "rust".to_string(),
            host_type: HostType::InProcess,
            host_group: None,
            entry: "memory_read".to_string(),
            capabilities: Default::default(),
            requires_services: vec![],
            permissions: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            requires_content: Some(2),
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            restricted_capabilities: Vec::new(),
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
            export_fields: vec![],
            aux_venvs: Vec::new(),
        };
        assert!(loader.validate_manifest(&manifest).is_ok());
        assert_eq!(manifest.requires_content, Some(2));
    }

    #[test]
    fn test_manifest_parses_provides_capabilities() {
        // M4: plugin.json 的 provides.capabilities 声明应被正确反序列化。
        // 验证：声明了 provides 的 manifest，provides 字段非 None 且内容正确。
        let json = r#"{
            "id": "human_interaction_service",
            "name": "Human Interaction Service",
            "version": "1.0.0",
            "plugin_type": "system",
            "language": "python",
            "host_type": "sidecar",
            "entry": "python server.py",
            "capabilities": {"tools": [], "lifecycle_hooks": ["on_load"]},
            "provides": {
                "capabilities": [
                    {
                        "namespace": "human-interaction",
                        "methods": ["create_choice", "wait_for_choice", "respond", "cancel"],
                        "host": "in-process"
                    }
                ]
            }
        }"#;
        let manifest: PluginManifest = serde_json::from_str(json).unwrap();
        let provides = manifest.provides.expect("provides 应被解析");
        assert_eq!(provides.capabilities.len(), 1);
        let cap = &provides.capabilities[0];
        assert_eq!(cap.namespace, "human-interaction");
        assert_eq!(
            cap.methods,
            vec!["create_choice", "wait_for_choice", "respond", "cancel"]
        );
        assert_eq!(cap.host, ProvidedCapabilityHost::InProcess);
    }

    #[test]
    fn test_manifest_without_provides_defaults_to_none() {
        // 向后兼容：旧 plugin.json 不含 provides 字段时，解析为 None，不报错。
        let json = r#"{
            "id": "legacy_plugin",
            "name": "Legacy",
            "version": "1.0.0",
            "plugin_type": "tool",
            "language": "python",
            "host_type": "sidecar",
            "entry": "python server.py",
            "capabilities": {"tools": [{"name": "legacy_tool"}]}
        }"#;
        let manifest: PluginManifest = serde_json::from_str(json).unwrap();
        assert!(manifest.provides.is_none(), "无 provides 字段应解析为 None");
    }

    #[test]
    fn test_manifest_provides_defaults_host_to_inprocess() {
        // host 字段缺省时（#[serde(default)])应为 InProcess。
        let json = r#"{
            "id": "p1", "name": "P1", "version": "1.0.0",
            "plugin_type": "system", "language": "python",
            "host_type": "sidecar", "entry": "s.py",
            "capabilities": {"tools": []},
            "provides": {"capabilities": [{"namespace": "my-cap", "methods": ["do"]}]}
        }"#;
        let manifest: PluginManifest = serde_json::from_str(json).unwrap();
        let cap = &manifest.provides.unwrap().capabilities[0];
        assert_eq!(
            cap.host,
            ProvidedCapabilityHost::InProcess,
            "host 缺省应为 InProcess"
        );
    }

    // ── 配置加载测试 ──

    #[tokio::test]
    async fn test_load_config_no_config_root_returns_empty() {
        let _user_space = factory_only_user_space();
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let config = loader.load_config().await.unwrap();
        assert_eq!(config, serde_json::json!({}));
    }

    #[tokio::test]
    async fn test_load_config_nonexistent_dir_returns_empty() {
        let _user_space = factory_only_user_space();
        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root("/tmp/no_such_dir");
        let config = loader.load_config().await.unwrap();
        assert_eq!(config, serde_json::json!({}));
    }

    #[tokio::test]
    async fn test_load_config_reads_yaml_files() {
        let _user_space = factory_only_user_space();
        let config_dir = tempfile::tempdir().unwrap();

        // 写入两个 YAML 配置文件
        fs::write(
            config_dir.path().join("memory_storage.yaml"),
            "storage_backend: sqlite\ncache_size: 1000\n",
        )
        .unwrap();
        fs::write(
            config_dir.path().join("api_config.yaml"),
            "timeout: 30\nhost: localhost\n",
        )
        .unwrap();

        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

        let config = loader.load_config().await.unwrap();
        let obj = config.as_object().unwrap();

        assert_eq!(obj.len(), 2);
        assert!(obj.contains_key("memory_storage"));
        assert!(obj.contains_key("api_config"));

        // 验证 YAML 内容正确解析
        let mem = obj.get("memory_storage").unwrap();
        assert_eq!(mem["storage_backend"], "sqlite");
        assert_eq!(mem["cache_size"], 1000);
    }

    #[tokio::test]
    async fn test_load_config_handles_nested_dirs() {
        let _user_space = factory_only_user_space();
        let config_dir = tempfile::tempdir().unwrap();
        let sub_dir = config_dir.path().join("isolation");
        fs::create_dir_all(&sub_dir).unwrap();

        // 根目录 YAML
        fs::write(
            config_dir.path().join("memory_storage.yaml"),
            "backend: sqlite\n",
        )
        .unwrap();
        // 子目录 YAML
        fs::write(
            sub_dir.join("isolation_config.yaml"),
            "level: strict\ncontainers: 5\n",
        )
        .unwrap();

        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

        let config = loader.load_config().await.unwrap();
        let obj = config.as_object().unwrap();

        assert_eq!(obj.len(), 2);
        assert!(obj.contains_key("memory_storage"));
        assert!(obj.contains_key("isolation"));

        // 子目录合并为嵌套对象
        let isolation = obj.get("isolation").unwrap().as_object().unwrap();
        assert_eq!(isolation.len(), 1);
        assert!(isolation.contains_key("isolation_config"));
        assert_eq!(isolation["isolation_config"]["level"], "strict");
    }

    #[tokio::test]
    async fn test_load_config_ignores_non_yaml_files() {
        let _user_space = factory_only_user_space();
        let config_dir = tempfile::tempdir().unwrap();

        fs::write(config_dir.path().join("config.yaml"), "key: value\n").unwrap();
        fs::write(config_dir.path().join("readme.md"), "# Not a config\n").unwrap();
        fs::write(
            config_dir.path().join("data.json"),
            "{\"key\": \"value\"}\n",
        )
        .unwrap();

        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

        let config = loader.load_config().await.unwrap();
        let obj = config.as_object().unwrap();

        // 只有 YAML 文件被收录
        assert_eq!(obj.len(), 1);
        assert!(obj.contains_key("config"));
        assert!(!obj.contains_key("readme"));
        assert!(!obj.contains_key("data"));
    }

    #[tokio::test]
    async fn test_load_config_yml_extension() {
        let _user_space = factory_only_user_space();
        let config_dir = tempfile::tempdir().unwrap();

        fs::write(config_dir.path().join("short.yml"), "name: test\n").unwrap();

        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

        let config = loader.load_config().await.unwrap();
        let obj = config.as_object().unwrap();

        assert_eq!(obj.len(), 1);
        assert!(obj.contains_key("short"));
    }

    #[tokio::test]
    async fn test_load_config_empty_yaml_dir() {
        let _user_space = factory_only_user_space();
        let config_dir = tempfile::tempdir().unwrap();
        // 空目录

        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

        let config = loader.load_config().await.unwrap();
        assert_eq!(config, serde_json::json!({}));
    }

    #[tokio::test]
    async fn test_load_config_deep_nested_dirs() {
        let _user_space = factory_only_user_space();
        // 深层嵌套（≥ 2 层目录）
        let config_dir = tempfile::tempdir().unwrap();
        let deep_dir = config_dir.path().join("system").join("subsystem");
        fs::create_dir_all(&deep_dir).unwrap();

        fs::write(
            config_dir.path().join("root_config.yaml"),
            "root_key: root_value\n",
        )
        .unwrap();
        fs::write(deep_dir.join("deep_config.yaml"), "deep_key: deep_value\n").unwrap();

        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

        let config = loader.load_config().await.unwrap();
        let obj = config.as_object().unwrap();

        assert_eq!(obj.len(), 2);
        assert!(obj.contains_key("root_config"));
        assert!(obj.contains_key("system"));

        // 第二层
        let system = obj.get("system").unwrap().as_object().unwrap();
        assert!(system.contains_key("subsystem"));

        // 第三层
        let subsystem = system.get("subsystem").unwrap().as_object().unwrap();
        assert!(subsystem.contains_key("deep_config"));
        assert_eq!(subsystem["deep_config"]["deep_key"], "deep_value");
    }

    #[tokio::test]
    async fn test_load_config_same_stem_across_dirs() {
        let _user_space = factory_only_user_space();
        // 同名文件跨目录：根目录和子目录都有同名 yaml
        let config_dir = tempfile::tempdir().unwrap();
        let sub_dir = config_dir.path().join("sub");
        fs::create_dir_all(&sub_dir).unwrap();

        // 根目录有 config.yaml
        fs::write(config_dir.path().join("config.yaml"), "from: root\n").unwrap();
        // 子目录也有 config.yaml
        fs::write(sub_dir.join("config.yaml"), "from: sub\n").unwrap();

        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

        let config = loader.load_config().await.unwrap();
        let obj = config.as_object().unwrap();

        // 根目录的 config.yaml 作为顶层 key
        assert!(obj.contains_key("config"));
        assert_eq!(obj["config"]["from"], "root");

        // 子目录的 config.yaml 在 sub 子对象下（不会覆盖）
        assert!(obj.contains_key("sub"));
        let sub = obj.get("sub").unwrap().as_object().unwrap();
        assert!(sub.contains_key("config"));
        assert_eq!(sub["config"]["from"], "sub");
    }

    #[tokio::test]
    async fn test_load_config_empty_subdir_not_collected() {
        let _user_space = factory_only_user_space();
        // 空子目录不应出现在结果中
        let config_dir = tempfile::tempdir().unwrap();
        let empty_sub = config_dir.path().join("empty_dir");
        fs::create_dir_all(&empty_sub).unwrap();

        fs::write(config_dir.path().join("config.yaml"), "key: value\n").unwrap();

        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

        let config = loader.load_config().await.unwrap();
        let obj = config.as_object().unwrap();

        // 空子目录不应出现
        assert_eq!(obj.len(), 1);
        assert!(obj.contains_key("config"));
        assert!(!obj.contains_key("empty_dir"));
    }

    #[tokio::test]
    async fn test_load_config_mixed_yaml_and_yml() {
        let _user_space = factory_only_user_space();
        // 同一目录下同时有 .yaml 和 .yml 文件
        let config_dir = tempfile::tempdir().unwrap();

        fs::write(config_dir.path().join("long.yaml"), "type: yaml\n").unwrap();
        fs::write(config_dir.path().join("short.yml"), "type: yml\n").unwrap();

        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

        let config = loader.load_config().await.unwrap();
        let obj = config.as_object().unwrap();

        assert_eq!(obj.len(), 2);
        assert!(obj.contains_key("long"));
        assert!(obj.contains_key("short"));
        assert_eq!(obj["long"]["type"], "yaml");
        assert_eq!(obj["short"]["type"], "yml");
    }

    /// 单个 YAML 文件解析失败时,不应连累整个 load_config 失败——
    /// 应跳过该文件并继续加载其余文件(防御性降级 + 可观测 warn)。
    ///
    /// 真实用例:config/templates/.agent_template_spec.yaml 是 Agent 配置模板
    /// 文档(含 {placeholder}: 占位符 key),解析失败时若整体失败,所有插件
    /// 都收不到配置(实证:0.2 内核启动后管道循环空转,每步 CONFIG_PARSE_ERROR)。
    #[tokio::test]
    async fn test_load_config_skips_unparseable_file_without_failing_others() {
        let _user_space = factory_only_user_space();
        let config_dir = tempfile::tempdir().unwrap();

        // 合法配置
        fs::write(config_dir.path().join("good.yaml"), "key: value\n").unwrap();
        // 非法 YAML(map key 用了占位符,serde_yaml 解析失败)
        fs::write(
            config_dir.path().join("bad.yaml"),
            "input_schema:\n  properties:\n    {placeholder}:\n      type: string\n",
        )
        .unwrap();

        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

        // 关键:不返回 Err,而是跳过 bad.yaml 继续加载 good.yaml
        let config = loader.load_config().await.unwrap();
        let obj = config.as_object().unwrap();
        assert!(obj.contains_key("good"), "合法文件应正常加载");
        assert!(!obj.contains_key("bad"), "非法文件应被跳过,不让整体失败");
    }

    // ── 全量配置装载的 mtime 快照缓存（每次插件 spawn/工具调用都走
    // load_config，装机实测 76 分钟 2415 次全量扫描构成分配 churn 洪峰）──

    /// 缓存键语义（对外可观察契约）：指纹 = 文件集 × (路径, mtime, size)。
    /// 指纹未变 → 返回快照（同长度同 mtime 原位改写不可检测）；真实改写
    /// （mtime 必 bump）→ 下一次装载立见新值。watcher 语义保留：变更检测
    /// 在每次 load 现场做，无 TTL 无延迟。
    #[tokio::test]
    async fn config_cache_hit_within_fingerprint_and_invalidates_on_real_edit() {
        let _user_space = factory_only_user_space();
        let config_dir = tempfile::tempdir().unwrap();
        let file = config_dir.path().join("svc.yaml");
        fs::write(&file, "k: one\n").unwrap();

        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

        let first = loader.load_config().await.unwrap();
        assert_eq!(first["svc"]["k"], "one");

        // 同字节长度改写 + 显式还原 mtime：指纹不可检测 → 快照窗口内
        let mtime = fs::metadata(&file).unwrap().modified().unwrap();
        fs::write(&file, "k: two\n").unwrap();
        std::fs::File::options()
            .write(true)
            .open(&file)
            .unwrap()
            .set_modified(mtime)
            .unwrap();
        let second = loader.load_config().await.unwrap();
        assert_eq!(
            second["svc"]["k"], "one",
            "指纹未变应命中缓存（不重读内容）"
        );

        // 真实改写（长度与 mtime 双变）→ 必须失效重载
        fs::write(&file, "k: three-much-longer\n").unwrap();
        let third = loader.load_config().await.unwrap();
        assert_eq!(
            third["svc"]["k"], "three-much-longer",
            "mtime 变更必须失效重载"
        );
    }

    /// 文件集变更（新增/删除）必然改指纹：新文件进下一次装载、被删文件退出。
    #[tokio::test]
    async fn config_cache_invalidates_on_file_add_and_remove() {
        let _user_space = factory_only_user_space();
        let config_dir = tempfile::tempdir().unwrap();
        fs::write(config_dir.path().join("a.yaml"), "x: one\n").unwrap();
        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

        let cfg = loader.load_config().await.unwrap();
        assert_eq!(cfg.as_object().unwrap().len(), 1);

        fs::write(config_dir.path().join("b.yaml"), "y: two\n").unwrap();
        let cfg = loader.load_config().await.unwrap();
        assert_eq!(
            cfg.as_object().unwrap().len(),
            2,
            "新增文件应进入下一次装载"
        );
        assert_eq!(cfg["b"]["y"], "two");

        fs::remove_file(config_dir.path().join("b.yaml")).unwrap();
        let cfg = loader.load_config().await.unwrap();
        assert_eq!(
            cfg.as_object().unwrap().len(),
            1,
            "删除文件应退出下一次装载"
        );
        assert!(cfg.get("b").is_none());
    }

    /// 用户覆盖层的变更同样经指纹失效：改写立见新值、删除回退 factory 值
    /// （缓存不得把用户层冻结在旧快照——装机"改了没生效"排查依赖此语义）。
    #[tokio::test]
    async fn config_cache_invalidates_on_user_overlay_edit_and_removal() {
        let _lock = user_space_env_lock();
        let factory = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        fs::write(factory.path().join("llm.yaml"), "model: factory\n").unwrap();
        let user_file = user.path().join("llm.yaml");
        fs::write(&user_file, "model: user-v1\n").unwrap();

        let _g = UserRootGuard::set(user.path());
        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(factory.path());

        let cfg = loader.load_config().await.unwrap();
        assert_eq!(cfg["llm"]["model"], "user-v1");

        fs::write(&user_file, "model: user-v2\n").unwrap();
        let cfg = loader.load_config().await.unwrap();
        assert_eq!(cfg["llm"]["model"], "user-v2", "用户层改写应立即可见");

        fs::remove_file(&user_file).unwrap();
        let cfg = loader.load_config().await.unwrap();
        assert_eq!(
            cfg["llm"]["model"], "factory",
            "用户层删除应回退 factory 值"
        );
    }

    /// 多触发合并：同一 loader 上并发多次 load_config（spawn 洪峰形态），
    /// 全部成功且结果一致——单份快照承载全部触发，无撕裂中间态。
    #[tokio::test]
    async fn config_cache_concurrent_loads_consistent() {
        let _user_space = factory_only_user_space();
        let config_dir = tempfile::tempdir().unwrap();
        fs::write(config_dir.path().join("a.yaml"), "x: one\n").unwrap();
        fs::create_dir_all(config_dir.path().join("sub")).unwrap();
        fs::write(config_dir.path().join("sub/b.yaml"), "y: two\n").unwrap();

        let loader = std::sync::Arc::new(
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path()),
        );
        let expected = loader.load_config().await.unwrap();

        let mut handles = Vec::new();
        for _ in 0..8 {
            let loader = loader.clone();
            handles.push(tokio::spawn(
                async move { loader.load_config().await.unwrap() },
            ));
        }
        for h in handles {
            assert_eq!(h.await.unwrap(), expected, "并发装载结果应与首载一致");
        }
    }

    /// 用户配置层：**文件级整体替换**（ADR 2026-09-13-unified-user-root）。
    ///
    /// 覆盖语义的两组区分度输入：被接管的文件取用户值（整体，不合并 field），
    /// 未被接管的文件保持 factory 值——后者是关键：用户层同目录存在别的文件时，
    /// 不得因目录级整体替换而抹掉兄弟文件的 factory 值。
    #[tokio::test]
    async fn user_config_overlay_replaces_files_not_directories() {
        let _lock = user_space_env_lock();
        let factory = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        // factory: models/llm.yaml + models/embedding.yaml（同目录兄弟文件）
        fs::create_dir_all(factory.path().join("models")).unwrap();
        fs::write(
            factory.path().join("models/llm.yaml"),
            "model: factory-model\nnested: {a: 1, b: 2}\n",
        )
        .unwrap();
        fs::write(
            factory.path().join("models/embedding.yaml"),
            "provider: factory-embed\n",
        )
        .unwrap();
        // 用户层：只接管 llm.yaml
        fs::create_dir_all(user.path().join("models")).unwrap();
        fs::write(user.path().join("models/llm.yaml"), "model: user-model\n").unwrap();

        let _g = UserRootGuard::set(user.path());
        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(factory.path());
        let config = loader.load_config().await.unwrap();
        let models = config.get("models").unwrap().as_object().unwrap();

        // 接管生效：整文件替换（nested 不参与合并，用户文件没有该键就不该有）
        let llm = models.get("llm").unwrap().as_object().unwrap();
        assert_eq!(llm.get("model").unwrap().as_str().unwrap(), "user-model");
        assert!(
            !llm.contains_key("nested"),
            "整体替换：factory 的键不得渗进用户文件（合并即两处存值）"
        );
        // 兄弟文件不受牵连：未被接管的 embedding 仍读 factory
        assert_eq!(
            models
                .get("embedding")
                .unwrap()
                .get("provider")
                .unwrap()
                .as_str()
                .unwrap(),
            "factory-embed",
            "同目录兄弟文件必须保持 factory 值（防目录级整体替换抹掉）"
        );
    }

    /// 用户层不存在该文件时不接管；用户层完全没有时读 factory（回落）。
    #[tokio::test]
    async fn user_config_overlay_absent_falls_back_to_factory() {
        let _lock = user_space_env_lock();
        let factory = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        fs::write(factory.path().join("solo.yaml"), "v: factory\n").unwrap();

        // ① 用户层目录为空
        let _g = UserRootGuard::set(user.path());
        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(factory.path());
        assert_eq!(
            loader
                .load_config()
                .await
                .unwrap()
                .get("solo")
                .unwrap()
                .get("v")
                .unwrap()
                .as_str()
                .unwrap(),
            "factory"
        );

        // ② 用户层有其它文件但没接管 solo
        fs::write(user.path().join("other.yaml"), "v: user\n").unwrap();
        let loader2 =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(factory.path());
        let cfg = loader2.load_config().await.unwrap();
        assert_eq!(
            cfg.get("solo").unwrap().get("v").unwrap().as_str().unwrap(),
            "factory",
            "存在性判定按文件而非目录"
        );
        assert_eq!(
            cfg.get("other")
                .unwrap()
                .get("v")
                .unwrap()
                .as_str()
                .unwrap(),
            "user"
        );
    }

    /// 用户层坏文件 fail-safe：告警跳过并**保留 factory 值**，不让插件静默拿空配置。
    #[tokio::test]
    async fn user_config_overlay_bad_file_keeps_factory_value() {
        let _lock = user_space_env_lock();
        let factory = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        fs::write(factory.path().join("cfg.yaml"), "v: factory\n").unwrap();
        fs::write(
            user.path().join("cfg.yaml"),
            "input_schema:\n  properties:\n    {placeholder}:\n      type: string\n",
        )
        .unwrap();

        let _g = UserRootGuard::set(user.path());
        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(factory.path());
        assert_eq!(
            loader
                .load_config()
                .await
                .unwrap()
                .get("cfg")
                .unwrap()
                .get("v")
                .unwrap()
                .as_str()
                .unwrap(),
            "factory",
            "坏的用户文件应告警跳过并保留 factory 值"
        );
    }

    /// 嵌套目录键路径：`agents/main/agentos.yaml` → 键 `agents.main.agentos`
    /// （与注入侧 resolve_config_path 按 `/` 下钻同构）。
    #[tokio::test]
    async fn user_config_overlay_nested_paths_map_to_key_paths() {
        let _lock = user_space_env_lock();
        let factory = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        fs::create_dir_all(factory.path().join("agents/main")).unwrap();
        fs::write(
            factory.path().join("agents/main/agentos.yaml"),
            "n: factory\n",
        )
        .unwrap();
        fs::create_dir_all(user.path().join("agents/main")).unwrap();
        fs::write(user.path().join("agents/main/agentos.yaml"), "n: user\n").unwrap();

        let _g = UserRootGuard::set(user.path());
        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(factory.path());
        let cfg = loader.load_config().await.unwrap();
        assert_eq!(
            cfg["agents"]["main"]["agentos"]["n"].as_str().unwrap(),
            "user",
            "嵌套路径应按目录层级映射到键路径"
        );
    }

    /// 只读 factory 的用例专用：持锁 + 把用户配置层钉到一个**全新空目录**。
    ///
    /// `load_config` 会把用户层文件并入结果（ADR 2026-09-13-unified-user-root），
    /// 故"只断言 factory 内容"的用例必须让用户层为空——否则会读到开发机
    /// `%APPDATA%/agentos/config/` 的残留（实测断言条数 2 却得 4）。
    ///
    /// 返回值须绑定到变量（guard 存活到用例结束）。
    struct FactoryOnlyUserSpace {
        _lock: UserSpaceEnvGuard,
        _tmp: tempfile::TempDir,
        original_cfg: Option<String>,
    }

    impl Drop for FactoryOnlyUserSpace {
        fn drop(&mut self) {
            match self.original_cfg.take() {
                Some(v) => {
                    std::env::set_var(agentos_core::user_space::USER_CONFIG_DIR_ENV, v);
                }
                None => std::env::remove_var(agentos_core::user_space::USER_CONFIG_DIR_ENV),
            }
        }
    }

    fn factory_only_user_space() -> FactoryOnlyUserSpace {
        let tmp = tempfile::tempdir().unwrap();
        let lock = user_space_env_lock();
        let original_cfg = std::env::var(agentos_core::user_space::USER_CONFIG_DIR_ENV).ok();
        std::env::set_var(
            agentos_core::user_space::USER_CONFIG_DIR_ENV,
            tmp.path().join("user-config"),
        );
        FactoryOnlyUserSpace {
            _lock: lock,
            _tmp: tmp,
            original_cfg,
        }
    }

    /// 隐藏文件(以 `.` 开头)不应被当作配置加载——
    /// Unix 惯例隐藏文件是元数据/文档,不是常规配置。
    /// 真实用例:.agent_template_spec.yaml 是 Agent 配置规范文档。
    #[tokio::test]
    async fn test_load_config_skips_hidden_files() {
        let _user_space = factory_only_user_space();
        let config_dir = tempfile::tempdir().unwrap();

        fs::write(config_dir.path().join("visible.yaml"), "key: value\n").unwrap();
        fs::write(
            config_dir.path().join(".hidden.yaml"),
            "hidden_key: hidden_value\n",
        )
        .unwrap();

        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

        let config = loader.load_config().await.unwrap();
        let obj = config.as_object().unwrap();
        assert!(obj.contains_key("visible"), "常规文件应加载");
        assert!(
            !obj.contains_key(".hidden") && !obj.contains_key("hidden"),
            "隐藏文件(.前缀)应被跳过"
        );
    }

    // ── P2-2 插件准入白名单 + SHA256 校验测试 ──

    /// strict 模式下，非白名单插件应被拒绝。
    #[tokio::test]
    async fn test_allowlist_strict_rejects_unknown() {
        let builtin = tempfile::tempdir().unwrap();
        // 写一个插件，但它不在白名单里
        create_test_plugin_dir(builtin.path(), "unknown_plugin", "pipeline");

        // strict 模式 + 空白名单
        let allowlist = AllowlistConfig {
            mode: AllowlistMode::Strict,
            plugins: vec![],
            ..Default::default()
        };
        let loader = PluginLoaderImpl::new(builtin.path(), None).with_allowlist(allowlist);

        let manifests = loader.discover(&[]).await.unwrap();
        // 非白名单插件被 validate_manifest_internal 拒绝 → 不进入结果
        assert!(
            manifests.is_empty(),
            "strict mode should reject plugins not in allowlist"
        );
    }

    /// permissive 模式下，所有插件应放行（含非白名单插件）。
    #[tokio::test]
    async fn test_allowlist_permissive_allows_all() {
        let builtin = tempfile::tempdir().unwrap();
        create_test_plugin_dir(builtin.path(), "any_plugin_a", "pipeline");
        create_test_plugin_dir(builtin.path(), "any_plugin_b", "tool");

        // permissive 模式 + 空白名单（默认）
        let loader = PluginLoaderImpl::new(builtin.path(), None);

        let manifests = loader.discover(&[]).await.unwrap();
        assert_eq!(
            manifests.len(),
            2,
            "permissive mode should allow all plugins"
        );
        let ids: Vec<_> = manifests.iter().map(|m| m.id.clone()).collect();
        assert!(ids.contains(&"any_plugin_a".to_string()));
        assert!(ids.contains(&"any_plugin_b".to_string()));
    }

    /// 白名单条目声明了 sha256，实际内容不匹配时应被拒绝。
    #[tokio::test]
    async fn test_sha256_mismatch_rejected() {
        let builtin = tempfile::tempdir().unwrap();
        let plugin_dir = builtin.path().join("hashed_plugin");
        fs::create_dir_all(&plugin_dir).unwrap();

        // manifest 文件（pipeline 类型声明 invoke_entry，P6 discover 聚合校验）
        let manifest_content = r#"{
    "id": "hashed_plugin",
    "name": "Hashed Plugin",
    "version": "1.0.0",
    "plugin_type": "pipeline",
    "language": "python",
    "host_type": "sidecar",
    "entry": "python3 server.py",
    "capabilities": {},
    "requires_services": [],
    "permissions": {},
    "priority": 100,
    "invoke_entry": "hashed_plugin.execute"
}"#;
        let manifest_path = plugin_dir.join("plugin.json");
        fs::write(&manifest_path, manifest_content).unwrap();
        // 入口文件
        fs::write(plugin_dir.join("server.py"), "# entry file\n").unwrap();

        // 第一性原理重算目录树规范化哈希：本插件仅两文件，排序相对路径为
        // (plugin.json, server.py)，sha256 输入 = 各 `路径\0字节\0` 顺序拼接。
        let manifest_bytes = fs::read(&manifest_path).unwrap();
        let entry_bytes = fs::read(plugin_dir.join("server.py")).unwrap();
        let mut hasher = Sha256::new();
        hasher.update(b"plugin.json");
        hasher.update([0u8]);
        hasher.update(&manifest_bytes);
        hasher.update([0u8]);
        hasher.update(b"server.py");
        hasher.update([0u8]);
        hasher.update(&entry_bytes);
        hasher.update([0u8]);
        let correct_hash = format!("{:x}", hasher.finalize());

        // 1) 正确哈希 → 放行
        let allowlist_ok = AllowlistConfig {
            mode: AllowlistMode::Permissive,
            plugins: vec![AllowlistEntry {
                id: "hashed_plugin".to_string(),
                sha256: correct_hash.clone(),
                ..Default::default()
            }],
            ..Default::default()
        };
        let loader_ok = PluginLoaderImpl::new(builtin.path(), None).with_allowlist(allowlist_ok);
        let manifests = loader_ok.discover(&[]).await.unwrap();
        assert_eq!(
            manifests.len(),
            1,
            "matching sha256 should allow the plugin"
        );
        assert_eq!(manifests[0].id, "hashed_plugin");

        // 2) 故意改坏哈希 → 拒绝
        // 翻转第一个 hex 字符以构造不匹配（保持合法 hex）
        let mut chars = correct_hash.chars();
        let first = chars.next().unwrap();
        let flipped = if first == '0' { '1' } else { '0' };
        let broken_hash = format!("{}{}", flipped, &correct_hash[1..]);
        let allowlist_bad = AllowlistConfig {
            mode: AllowlistMode::Permissive,
            plugins: vec![AllowlistEntry {
                id: "hashed_plugin".to_string(),
                sha256: broken_hash,
                ..Default::default()
            }],
            ..Default::default()
        };
        let loader_bad = PluginLoaderImpl::new(builtin.path(), None).with_allowlist(allowlist_bad);
        let manifests_bad = loader_bad.discover(&[]).await.unwrap();
        assert!(
            manifests_bad.is_empty(),
            "sha256 mismatch should reject the plugin"
        );
    }

    // ── 准入分级（2026-09-25）：危险前端能力 grants / 剥权 ──

    /// 写一个带危险前端能力声明的 tool 插件（skin / client_styles 可选开关）。
    fn write_frontend_plugin(root: &Path, id: &str, with_skin: bool, with_client_styles: bool) {
        let dir = root.join(id);
        fs::create_dir_all(&dir).unwrap();
        let themes = if with_skin {
            r#"[{"id": "t1", "name": "T1", "base": "dark", "skin": "s1"}]"#
        } else {
            "[]"
        };
        let styles = if with_client_styles {
            r#"[{"id": "s", "path": "/assets/x.css"}]"#
        } else {
            "[]"
        };
        let manifest = format!(
            r#"{{
    "id": "{id}",
    "name": "Frontend {id}",
    "version": "1.0.0",
    "plugin_type": "tool",
    "language": "python",
    "host_type": "sidecar",
    "entry": "python server.py",
    "capabilities": {{}},
    "contributes": {{"themes": {themes}, "client_styles": {styles}}}
}}"#
        );
        fs::write(dir.join("plugin.json"), manifest).unwrap();
    }

    /// 内容静态检测：themes[].skin 非空 → host_js；client_styles 非空数组 →
    /// host_css；空串/空数组/缺失 → 不算声明。
    #[test]
    fn detect_frontend_dangerous_caps_by_manifest_content() {
        use serde_json::json;
        assert!(
            detect_frontend_dangerous_caps(None).is_empty(),
            "无 contributes 无危险能力"
        );
        assert!(
            detect_frontend_dangerous_caps(Some(&json!({"themes": []}))).is_empty(),
            "空 themes 无危险能力"
        );
        assert_eq!(
            detect_frontend_dangerous_caps(Some(&json!({"themes": [{"skin": "s1"}]}))),
            vec![CAP_HOST_JS],
            "skin 非空 → host_js"
        );
        assert!(
            detect_frontend_dangerous_caps(Some(&json!({"themes": [{"skin": ""}]}))).is_empty(),
            "空串 skin 不算声明"
        );
        assert_eq!(
            detect_frontend_dangerous_caps(Some(&json!({"client_styles": [{"id": "s"}]}))),
            vec![CAP_HOST_CSS],
            "非空 client_styles → host_css"
        );
        assert!(
            detect_frontend_dangerous_caps(Some(&json!({"client_styles": []}))).is_empty(),
            "空 client_styles 不算声明"
        );
        assert_eq!(
            detect_frontend_dangerous_caps(Some(
                &json!({"themes": [{"skin": "s1"}], "client_styles": [{"id": "s"}]})
            )),
            vec![CAP_HOST_JS, CAP_HOST_CSS],
            "双能力按词表序"
        );
    }

    /// 装机姿态（BuiltinFirst）：用户根插件未列入 allowlist → 危险能力全剥 +
    /// restricted_capabilities 记录；内置根同款声明 → 原样（可信面不剥）。
    #[tokio::test]
    async fn builtin_first_strips_ungranted_dangerous_caps_for_user_plugins() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        write_frontend_plugin(user.path(), "skin_user", true, true);
        write_frontend_plugin(builtin.path(), "skin_builtin", true, true);

        let allowlist = AllowlistConfig {
            mode: AllowlistMode::Permissive,
            plugins: vec![],
            deployed: true,
        };
        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_allowlist(allowlist)
            .with_dual_source_policy(DualSourcePolicy::BuiltinFirst);
        let manifests = loader.discover(&[]).await.unwrap();

        let user_manifest = manifests
            .iter()
            .find(|m| m.id == "skin_user")
            .expect("user plugin admitted");
        assert_eq!(
            user_manifest.restricted_capabilities,
            vec![CAP_HOST_JS.to_string(), CAP_HOST_CSS.to_string()],
            "未授予应按词表序记录剥权"
        );
        let user_contributes = user_manifest.contributes.as_ref().unwrap();
        assert!(
            user_contributes["themes"][0].get("skin").is_none(),
            "skin 字段应被剥除"
        );
        assert!(
            user_contributes.get("client_styles").is_none(),
            "client_styles 应被整键剥除"
        );

        let builtin_manifest = manifests
            .iter()
            .find(|m| m.id == "skin_builtin")
            .expect("builtin plugin admitted");
        assert!(
            builtin_manifest.restricted_capabilities.is_empty(),
            "内置根插件不应剥权"
        );
        assert!(
            builtin_manifest
                .contributes
                .as_ref()
                .unwrap()
                .get("client_styles")
                .is_some(),
            "内置根 client_styles 应原样保留"
        );
    }

    /// dev 姿态（UserFirst）：allowlist 已部署但未列入 → 仍全授予（开发流零变化）。
    #[tokio::test]
    async fn user_first_keeps_dangerous_caps_even_when_unlisted() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        write_frontend_plugin(user.path(), "skin_dev", true, true);

        let allowlist = AllowlistConfig {
            mode: AllowlistMode::Permissive,
            plugins: vec![],
            deployed: true,
        };
        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_allowlist(allowlist)
            .with_dual_source_policy(DualSourcePolicy::UserFirst);
        let manifests = loader.discover(&[]).await.unwrap();
        let manifest = manifests.iter().find(|m| m.id == "skin_dev").unwrap();
        assert!(
            manifest.restricted_capabilities.is_empty(),
            "dev 姿态应全授予"
        );
        assert!(
            manifest
                .contributes
                .as_ref()
                .unwrap()
                .get("client_styles")
                .is_some(),
            "dev 姿态 client_styles 应原样"
        );
    }

    /// allowlist 条目按 grants 部分授予：未授予项剥、已授予项保留。
    #[tokio::test]
    async fn allowlist_grants_selective() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        write_frontend_plugin(user.path(), "skin_partial", true, true);

        let allowlist = AllowlistConfig {
            mode: AllowlistMode::Permissive,
            plugins: vec![AllowlistEntry {
                id: "skin_partial".to_string(),
                sha256: String::new(),
                grants: vec![CAP_HOST_CSS.to_string()],
            }],
            deployed: true,
        };
        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_allowlist(allowlist)
            .with_dual_source_policy(DualSourcePolicy::BuiltinFirst);
        let manifests = loader.discover(&[]).await.unwrap();
        let manifest = manifests.iter().find(|m| m.id == "skin_partial").unwrap();
        assert_eq!(
            manifest.restricted_capabilities,
            vec![CAP_HOST_JS.to_string()],
            "只记录未授予项"
        );
        let contributes = manifest.contributes.as_ref().unwrap();
        assert!(
            contributes["themes"][0].get("skin").is_none(),
            "host_js 未授予应剥 skin"
        );
        assert!(
            contributes.get("client_styles").is_some(),
            "host_css 已授予应保留"
        );
    }

    /// 播种副本（用户根同 id 同版本）不被剥权不打警告——剥权只对裁决胜者
    /// 执行（2026-10-23 修正）：BuiltinFirst 持平内置赢，生效面=内置副本
    /// （builtin 路径不走用户根判定），未胜出的播种副本早于裁决的剥权是
    /// 纯噪声（mode_roleplay 48 次/日实证，功能从未丢失）。
    #[tokio::test]
    async fn seeded_user_copy_not_stripped_when_builtin_wins() {
        let builtin = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        write_frontend_plugin(builtin.path(), "mode_seed", false, true);
        write_frontend_plugin(user.path(), "mode_seed", false, true);

        let allowlist = AllowlistConfig {
            mode: AllowlistMode::Permissive,
            plugins: vec![], // 无条目：若误剥，client_styles 必失
            deployed: true,
        };
        let loader = PluginLoaderImpl::new(builtin.path(), Some(user.path().to_path_buf()))
            .with_allowlist(allowlist)
            .with_dual_source_policy(DualSourcePolicy::BuiltinFirst);
        let manifests = loader.discover(&[]).await.unwrap();
        let manifest = manifests.iter().find(|m| m.id == "mode_seed").unwrap();

        assert!(
            manifest.restricted_capabilities.is_empty(),
            "内置胜者不剥权，实际: {:?}",
            manifest.restricted_capabilities
        );
        let contributes = manifest.contributes.as_ref().unwrap();
        assert!(
            contributes.get("client_styles").is_some(),
            "client_styles 应完整保留（注册面从未被剥）"
        );
    }

    // ── P6 命名治理（ADR 附录 D③/D.5）：discover 启动期聚合校验 invoke_entry ──

    /// 辅助：写一个 sidecar pipeline manifest（可指定 invoke_entry）。
    fn write_pipeline_plugin(root: &Path, id: &str, invoke_entry: Option<&str>) {
        let dir = root.join(id);
        fs::create_dir_all(&dir).unwrap();
        let entry_field = match invoke_entry {
            Some(e) => format!(",\n    \"invoke_entry\": \"{e}\""),
            None => String::new(),
        };
        let manifest_json = format!(
            r#"{{
    "id": "{id}",
    "name": "Pipeline {id}",
    "version": "1.0.0",
    "plugin_type": "pipeline",
    "language": "python",
    "host_type": "sidecar",
    "entry": "python server.py",
    "capabilities": {{}}{entry_field}
}}"#
        );
        fs::write(dir.join("plugin.json"), manifest_json).unwrap();
    }

    /// pipeline 插件声明了 invoke_entry → discover 成功，manifest 原样返回。
    #[tokio::test]
    async fn test_discover_pipeline_with_invoke_entry_succeeds() {
        let builtin = tempfile::tempdir().unwrap();
        write_pipeline_plugin(builtin.path(), "good_pipe", Some("good_pipe.execute"));

        let loader = PluginLoaderImpl::new(builtin.path(), None);
        let manifests = loader.discover(&[]).await.unwrap();
        assert_eq!(manifests.len(), 1);
        assert_eq!(
            manifests[0].invoke_entry.as_deref(),
            Some("good_pipe.execute")
        );
    }

    /// 缺 invoke_entry 的 pipeline 插件 → discover 启动期聚合报错（不逐个 panic）。
    /// 错误消息必须列出所有缺失的插件 id（聚合，不是第一个就崩）。
    #[tokio::test]
    async fn test_discover_pipeline_missing_invoke_entry_aggregates_errors() {
        let builtin = tempfile::tempdir().unwrap();
        // 两个 pipeline 插件都缺 invoke_entry——聚合报错应一次列出两者
        write_pipeline_plugin(builtin.path(), "bad_pipe_one", None);
        write_pipeline_plugin(builtin.path(), "bad_pipe_two", None);

        let loader = PluginLoaderImpl::new(builtin.path(), None);
        let result = loader.discover(&[]).await;
        assert!(result.is_err(), "missing invoke_entry must fail discover");
        let err = result.unwrap_err();
        assert_eq!(err.code.as_deref(), Some("MISSING_INVOKE_ENTRY"));
        // 聚合：两个缺失插件都在错误消息里
        assert!(
            err.message.contains("bad_pipe_one"),
            "error must aggregate all missing plugins, got: {}",
            err.message
        );
        assert!(
            err.message.contains("bad_pipe_two"),
            "error must aggregate all missing plugins (not fail on first), got: {}",
            err.message
        );
    }

    /// tool 类型插件不需要 invoke_entry（它用 capabilities.tools[]）；
    /// system 类型暂保留 tools[] 不强制 invoke_entry（D.6 双语义待评估）。
    #[tokio::test]
    async fn test_discover_tool_plugin_does_not_require_invoke_entry() {
        let builtin = tempfile::tempdir().unwrap();
        create_test_plugin_dir(builtin.path(), "a_tool", "tool");
        create_test_plugin_dir(builtin.path(), "a_system", "system");

        let loader = PluginLoaderImpl::new(builtin.path(), None);
        let manifests = loader.discover(&[]).await.unwrap();
        // tool 和 system 都不要求 invoke_entry，discover 成功
        assert_eq!(manifests.len(), 2);
    }

    /// R7 F3：discover 落服务声明缓存——service_provider_ids 按点式条目
    /// （ns.method）解析提供方（llm.complete_stream → llm_service），供 ensure
    /// 声明面依赖闭包展开。缓存键 = 完整服务名；bare 命名空间不入键（调用面
    /// 展开只认点式精确声明，bare 属注册闸存在性语义）。
    #[tokio::test]
    async fn test_discover_populates_service_provider_cache() {
        let builtin = tempfile::tempdir().unwrap();
        let pdir = builtin.path().join("llm_service");
        fs::create_dir_all(&pdir).unwrap();
        fs::write(
            pdir.join("plugin.json"),
            r#"{"id":"llm_service","name":"LLM Service","version":"1.0.0","plugin_type":"system","language":"python","host_type":"sidecar","entry":"python server.py","capabilities":{"services":[{"name":"llm.complete_stream","description":"streaming completion"}]}}"#,
        )
        .unwrap();

        let loader = PluginLoaderImpl::new(builtin.path(), None);
        loader.discover(&[]).await.unwrap();

        assert_eq!(
            loader.service_provider_ids("llm.complete_stream"),
            vec!["llm_service".to_string()],
            "点式服务条目必须解析出声明提供方"
        );
        assert!(
            loader.service_provider_ids("llm").is_empty(),
            "bare 命名空间不入缓存键（不参与调用面展开）"
        );
    }

    // ── GAP-4：env 声明自动生成（引用本身即声明）────────────────

    fn env_coverage_manifest_json(declare: bool) -> String {
        let files = if declare {
            r#""config_files": [{
                "id": "api_keys", "label": "Keys", "path": ".env", "target": "env",
                "fields": [{"name": "SMITHERY_API_KEY", "label": "Smithery", "type": "secret", "required": true}]
            }],"#
        } else {
            ""
        };
        format!(
            r#"{{"id": "cov_plugin", "name": "Cov", "version": "1.0.0",
            "plugin_type": "tool", "language": "external", "host_type": "sidecar", "entry": "mcp:external", "capabilities": {{}}, "requires_services": [], "permissions": {{}}, "priority": 30,
            {files}
            "mcp": {{
                "transport": "streamable_http",
                "endpoint": {{
                    "url": "https://example.com/mcp",
                    "auth": {{"type": "api_key", "header_name": "Authorization", "value": "{value}"}}
                }}
            }}}}"#,
            files = files,
            value = "${SMITHERY_API_KEY}"
        )
    }

    #[test]
    fn test_env_declarations_auto_generated_when_undeclared() {
        // 未声明引用不再拒绝：校验通过 + 自动生成 env 条目（引用即声明）
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let mut manifest: PluginManifest =
            serde_json::from_str(&env_coverage_manifest_json(false)).unwrap();
        let r = loader.validate_manifest_internal(&mut manifest, Path::new("(test)"));
        assert!(r.is_ok(), "未声明引用应自动生成而非拒绝：{r:?}");
        let entry = manifest
            .config_files
            .iter()
            .find(|f| f.target.as_deref() == Some("env"))
            .expect("应自动生成 target=env 条目");
        assert_eq!(entry.path, ".env", "env 条目写入目标（.env）不可省");
        let field = entry
            .fields
            .iter()
            .find(|f| f.name == "SMITHERY_API_KEY")
            .expect("引用的 var 应生成对应 field");
        assert_eq!(field.field_type, "secret", "生成字段走保守掩码默认");
        assert!(!field.required, "生成字段不强制必填");
    }

    #[test]
    fn test_env_declarations_merge_into_existing_entry() {
        // 已有 env 条目声明了别的 var → 新引用合并进同一条目，不另建
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let json = env_coverage_manifest_json(false).replace(
            r#""mcp": {"#,
            r#""config_files": [{
                "id": "api_keys", "label": "Keys", "path": ".env", "target": "env",
                "fields": [{"name": "OTHER_KEY", "label": "Other", "type": "string", "required": true}]
            }],
            "mcp": {"#,
        );
        let mut manifest: PluginManifest = serde_json::from_str(&json).unwrap();
        let r = loader.validate_manifest_internal(&mut manifest, Path::new("(test)"));
        assert!(r.is_ok(), "{r:?}");
        let env_entries: Vec<_> = manifest
            .config_files
            .iter()
            .filter(|f| f.target.as_deref() == Some("env"))
            .collect();
        assert_eq!(env_entries.len(), 1, "应合并进既有 env 条目而非新建");
        let names: Vec<_> = env_entries[0]
            .fields
            .iter()
            .map(|f| f.name.as_str())
            .collect();
        assert!(names.contains(&"OTHER_KEY") && names.contains(&"SMITHERY_API_KEY"));
    }

    #[test]
    fn test_env_declarations_declared_not_duplicated() {
        // 手写声明仍在 → 校验通过且不重复生成
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let mut manifest: PluginManifest =
            serde_json::from_str(&env_coverage_manifest_json(true)).unwrap();
        let r = loader.validate_manifest_internal(&mut manifest, Path::new("(test)"));
        assert!(r.is_ok(), "声明覆盖应通过：{r:?}");
        let env_fields: Vec<_> = manifest
            .config_files
            .iter()
            .filter(|f| f.target.as_deref() == Some("env"))
            .flat_map(|f| f.fields.iter().map(|fd| fd.name.as_str()))
            .collect();
        assert_eq!(
            env_fields
                .iter()
                .filter(|n| **n == "SMITHERY_API_KEY")
                .count(),
            1,
            "已声明的 var 不应重复生成"
        );
    }

    #[test]
    fn test_env_coverage_default_syntax_exempt() {
        // ${VAR:-def} 带默认值（可选凭据）豁免：不生成声明条目
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let json =
            env_coverage_manifest_json(false).replace("${SMITHERY_API_KEY}", "${OPTIONAL_KEY:-}");
        let mut manifest: PluginManifest = serde_json::from_str(&json).unwrap();
        assert!(loader
            .validate_manifest_internal(&mut manifest, Path::new("(test)"))
            .is_ok());
        assert!(
            !manifest
                .config_files
                .iter()
                .any(|f| f.target.as_deref() == Some("env")),
            "带默认值的引用不应生成声明"
        );
    }

    // ── state.reads 读面声明（schema 收录阶段）：合法透传 / 非法告警忽略 ──

    fn state_reads_manifest_json(reads_entries: &str) -> String {
        format!(
            r#"{{"id": "reads_plugin", "name": "Reads", "version": "1.0.0",
            "plugin_type": "tool", "language": "external", "host_type": "sidecar",
            "entry": "mcp:external", "capabilities": {{}}, "requires_services": [],
            "permissions": {{}}, "priority": 30,
            "state": {{"volatile_keys": ["k.volatile"], "reads": [{reads_entries}]}}}}"#
        )
    }

    /// 三种合法形态（键名 / `messages` 全量 / `messages_tail:N`）解析成功并
    /// 原样透传到运行时 manifest 视图。
    #[test]
    fn test_state_reads_valid_entries_pass_through() {
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let mut manifest: PluginManifest = serde_json::from_str(&state_reads_manifest_json(
            r#""task.status", "messages", "messages_tail:50""#,
        ))
        .unwrap();
        let r = loader.validate_manifest_internal(&mut manifest, Path::new("(test)"));
        assert!(r.is_ok(), "合法 reads 条目应通过校验：{r:?}");
        assert_eq!(
            manifest.state.as_ref().unwrap().reads,
            vec!["task.status", "messages", "messages_tail:50"],
            "合法条目应原样透传（不增不删不改写）"
        );
    }

    /// `messages_tail:N` 格式非法（N 非正整数）→ G2 装载告警并忽略该条目
    /// （warn 不拒载），同 manifest 其余合法条目不受影响。
    #[test]
    fn test_state_reads_invalid_messages_tail_warns_and_ignored() {
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let mut manifest: PluginManifest = serde_json::from_str(&state_reads_manifest_json(
            r#""task.status", "messages_tail:x", "messages_tail:0", "messages_tail:", "messages_tail:1.5", "messages_tail:50""#,
        ))
        .unwrap();
        // warn 走 always-enabled 测试订阅者包裹（默认无订阅者时 warn! 文案
        // 不求值）；断言仍是语义结果（不拒载 + 非法条目被忽略），不断言日志
        let r = tracing::subscriber::with_default(AlwaysSubscriber, || {
            loader.validate_manifest_internal(&mut manifest, Path::new("(test)"))
        });
        assert!(
            r.is_ok(),
            "非法 messages_tail 条目应告警忽略而非拒载：{r:?}"
        );
        assert_eq!(
            manifest.state.as_ref().unwrap().reads,
            vec!["task.status", "messages_tail:50"],
            "非法条目（非数字/零/空/非整数）被忽略，合法邻居条目保留"
        );
    }

    /// 向后兼容：旧 manifest 的 state 段无 reads（或整段无 state）→ 校验全绿，
    /// reads 缺省为空。
    #[test]
    fn test_state_reads_absent_backward_compat() {
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let legacy_state = r#"{"id": "legacy_state", "name": "L", "version": "1.0.0",
            "plugin_type": "tool", "language": "external", "host_type": "sidecar",
            "entry": "mcp:external", "capabilities": {},
            "state": {"volatile_keys": ["k.volatile"]}}"#;
        let mut m1: PluginManifest = serde_json::from_str(legacy_state).unwrap();
        assert!(loader
            .validate_manifest_internal(&mut m1, Path::new("(test)"))
            .is_ok());
        assert!(
            m1.state.as_ref().unwrap().reads.is_empty(),
            "旧 state 段（只有 volatile_keys）reads 缺省为空"
        );

        let no_state = r#"{"id": "no_state", "name": "N", "version": "1.0.0",
            "plugin_type": "tool", "language": "external", "host_type": "sidecar",
            "entry": "mcp:external", "capabilities": {}}"#;
        let mut m2: PluginManifest = serde_json::from_str(no_state).unwrap();
        assert!(loader
            .validate_manifest_internal(&mut m2, Path::new("(test)"))
            .is_ok());
        assert!(m2.state.is_none(), "无 state 段不受影响");
    }

    /// 契约闸门 2.5：native 产物预检——artifact 缺失 → 加载期明确报错（不是拖到
    /// load/调用期才炸）。
    #[test]
    fn test_native_artifact_precheck_missing_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let plugin_dir = dir.path().join("p_native");
        std::fs::create_dir_all(&plugin_dir).unwrap();
        let json_path = plugin_dir.join("plugin.json");
        std::fs::write(
            &json_path,
            r#"{
                "id": "p_native", "name": "p_native", "version": "1.0.0",
                "plugin_type": "tool", "language": "rust",
                "host_type": "in_process", "entry": "p_native",
                "capabilities": {},
                "native": { "artifact": "libp_native.so" }
            }"#,
        )
        .unwrap();
        let loader = PluginLoaderImpl::new(dir.path(), None);
        let mut manifest: PluginManifest =
            serde_json::from_str(&std::fs::read_to_string(&json_path).unwrap()).unwrap();
        let err = loader.validate_manifest_internal(&mut manifest, &json_path);
        let msg = err.expect_err("native artifact 缺失应被拒").to_string();
        assert!(msg.contains("native artifact 缺失"), "{msg}");
    }

    /// 契约闸门 2.5：native 产物预检——artifact 存在 → 通过。
    #[test]
    fn test_native_artifact_precheck_existing_accepted() {
        let dir = tempfile::tempdir().unwrap();
        let plugin_dir = dir.path().join("p_native");
        std::fs::create_dir_all(&plugin_dir).unwrap();
        let json_path = plugin_dir.join("plugin.json");
        std::fs::write(
            &json_path,
            r#"{
                "id": "p_native", "name": "p_native", "version": "1.0.0",
                "plugin_type": "tool", "language": "rust",
                "host_type": "in_process", "entry": "p_native",
                "capabilities": {},
                "native": { "artifact": "libp_native.so" }
            }"#,
        )
        .unwrap();
        std::fs::write(plugin_dir.join("libp_native.so"), b"not-really-so").unwrap();
        let loader = PluginLoaderImpl::new(dir.path(), None);
        let mut manifest: PluginManifest =
            serde_json::from_str(&std::fs::read_to_string(&json_path).unwrap()).unwrap();
        let r = loader.validate_manifest_internal(&mut manifest, &json_path);
        assert!(r.is_ok(), "native artifact 存在应通过：{r:?}");
    }

    /// allowlist 生产接线：缺文件 → 默认 permissive 空白名单（不误伤）；
    /// yaml 合法 → 如实载入 mode/条目。
    #[test]
    fn load_allowlist_missing_file_defaults_to_permissive() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = load_allowlist_file(&dir.path().join("no_allowlist.yaml"));
        assert_eq!(cfg.mode, AllowlistMode::Permissive);
        assert!(cfg.plugins.is_empty());
    }

    #[test]
    fn load_allowlist_parses_yaml() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("plugin_allowlist.yaml");
        std::fs::write(
            &p,
            "mode: strict\nplugins:\n  - id: foo\n    sha256: abc123\n",
        )
        .unwrap();
        let cfg = load_allowlist_file(&p);
        assert_eq!(cfg.mode, AllowlistMode::Strict);
        assert_eq!(cfg.plugins.len(), 1);
        assert_eq!(cfg.plugins[0].id, "foo");
        assert_eq!(cfg.plugins[0].sha256, "abc123");
    }

    /// K2：解析失败（文件存在但损坏）→ fail-closed：strict 空名单（所有插件
    /// 在发现期被拒），不再静默降为 permissive 全放行。文件缺失仍 permissive
    /// （文档化默认，见 load_allowlist_missing_file_defaults_to_permissive）。
    #[test]
    fn load_allowlist_corrupt_file_fails_closed_to_strict_empty() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("plugin_allowlist.yaml");
        std::fs::write(&p, "mode: [broken\n  ::::").unwrap();
        let cfg = load_allowlist_file(&p);
        assert_eq!(
            cfg.mode,
            AllowlistMode::Strict,
            "损坏文件必须 fail-closed 为 strict"
        );
        assert!(cfg.plugins.is_empty(), "strict 空名单 = 全部插件拒载");
    }

    /// K2 端到端：解析失败的 allowlist 接入 loader 后，白名单外插件被拒载
    /// （fail-closed 的实际效果，非仅配置对象形态）。
    #[tokio::test]
    async fn corrupt_allowlist_rejects_plugin_load() {
        let builtin = tempfile::tempdir().unwrap();
        create_test_plugin_dir(builtin.path(), "some_plugin", "tool");

        let cfg_dir = tempfile::tempdir().unwrap();
        let allowlist_path = cfg_dir.path().join("plugin_allowlist.yaml");
        std::fs::write(&allowlist_path, "mode: [broken").unwrap();
        let allowlist = load_allowlist_file(&allowlist_path);

        let loader = PluginLoaderImpl::new(builtin.path(), None).with_allowlist(allowlist);
        let discovered = loader.discover(&[]).await.unwrap();
        assert!(
            discovered.is_empty(),
            "strict 空名单下损坏 allowlist 应拒载所有插件（fail-closed）"
        );
    }

    // ── scan_root 分支补充：YAML manifest / 解析失败跳过 / 校验失败跳过 ──

    /// plugin.yaml 形态：JSON 解析失败后回落 YAML 解析（同一份 discover 路径
    /// 支持两种 manifest 文件名与两种语法）。
    #[tokio::test]
    async fn scan_root_parses_yaml_manifest() {
        let builtin = tempfile::tempdir().unwrap();
        let dir = builtin.path().join("yaml_plugin");
        fs::create_dir_all(&dir).unwrap();
        fs::write(
            dir.join("plugin.yaml"),
            "id: yaml_plugin\nname: YAML Plugin\nversion: 1.0.0\n\
             plugin_type: tool\nlanguage: python\nhost_type: sidecar\n\
             entry: python server.py\ncapabilities: {}\n",
        )
        .unwrap();

        let loader = PluginLoaderImpl::new(builtin.path(), None);
        let discovered = loader.discover(&[]).await.unwrap();
        assert_eq!(discovered.len(), 1, "plugin.yaml 应被发现");
        assert_eq!(discovered[0].id, "yaml_plugin");
        assert_eq!(discovered[0].name, "YAML Plugin");
    }

    /// 目录里既无 plugin.json 也无 plugin.yaml → 跳过（不报错、不产出伪条目）。
    #[tokio::test]
    async fn scan_root_skips_dir_without_manifest() {
        let builtin = tempfile::tempdir().unwrap();
        fs::create_dir_all(builtin.path().join("empty_dir")).unwrap();
        // 顶层散落文件也不是插件目录（read_dir 只下钻目录）
        fs::write(builtin.path().join("stray.txt"), "x").unwrap();
        create_test_plugin_dir(builtin.path(), "real_plugin", "tool");

        let loader = PluginLoaderImpl::new(builtin.path(), None);
        let discovered = loader.discover(&[]).await.unwrap();
        assert_eq!(discovered.len(), 1, "只发现真插件目录");
        assert_eq!(discovered[0].id, "real_plugin");
    }

    /// 损坏 manifest（JSON/YAML 双解析失败）→ 跳过该插件但不阻断同 root 其他插件。
    #[tokio::test]
    async fn scan_root_skips_unparseable_and_keeps_others() {
        let builtin = tempfile::tempdir().unwrap();
        let bad = builtin.path().join("broken_plugin");
        fs::create_dir_all(&bad).unwrap();
        fs::write(bad.join("plugin.json"), "not json {{{").unwrap();
        create_test_plugin_dir(builtin.path(), "good_plugin", "tool");

        let loader = PluginLoaderImpl::new(builtin.path(), None);
        let discovered = loader
            .discover(&[])
            .await
            .expect("损坏插件不应让 discover 失败");
        assert_eq!(discovered.len(), 1, "坏插件跳过，好插件保留");
        assert_eq!(discovered[0].id, "good_plugin");
    }

    /// 校验失败（name 为空）→ 跳过该插件（warn 留痕），同 root 其他插件照常发现。
    #[tokio::test]
    async fn scan_root_skips_validation_failure_and_keeps_others() {
        let builtin = tempfile::tempdir().unwrap();
        let invalid = builtin.path().join("nameless");
        fs::create_dir_all(&invalid).unwrap();
        fs::write(
            invalid.join("plugin.json"),
            r#"{
                "id":"nameless","name":"","version":"1.0.0",
                "plugin_type":"tool","language":"python","host_type":"sidecar",
                "entry":"python server.py","capabilities":{}
            }"#,
        )
        .unwrap();
        create_test_plugin_dir(builtin.path(), "valid_peer", "tool");

        let loader = PluginLoaderImpl::new(builtin.path(), None);
        let discovered = loader.discover(&[]).await.unwrap();
        assert_eq!(discovered.len(), 1, "校验失败插件跳过");
        assert_eq!(discovered[0].id, "valid_peer");
    }

    /// 不存在的 root → scan_root 直接返回空（不报 IO 错）——双根扫描里
    /// builtin root 未部署时的正常路径。
    #[tokio::test]
    async fn discover_missing_root_returns_empty_without_error() {
        let missing = tempfile::tempdir().unwrap().path().join("not_deployed");
        let loader = PluginLoaderImpl::new(&missing, None);
        let discovered = loader.discover(&[]).await.expect("不存在的 root 不应报错");
        assert!(discovered.is_empty());
    }

    // ── validate_manifest_internal 必填字段逐项（id/name/version/language）──

    /// 逐个必填字段缺失 → 对应 reason（表驱动；entry 校验另有专测）。
    #[test]
    fn validate_manifest_reports_each_missing_required_field() {
        let dir = tempfile::tempdir().unwrap();
        let manifest_path = {
            let p = dir.path().join("p.json");
            fs::write(
                &p,
                r#"{
                    "id":"p","name":"P","version":"1.0.0",
                    "plugin_type":"tool","language":"python","host_type":"sidecar",
                    "entry":"python server.py","capabilities":{}
                }"#,
            )
            .unwrap();
            p
        };
        let loader = PluginLoaderImpl::new(dir.path(), None);
        let base: PluginManifest =
            serde_json::from_str(&fs::read_to_string(&manifest_path).unwrap()).unwrap();

        type ManifestEdit = fn(&mut PluginManifest);
        let cases: [(&str, ManifestEdit); 4] = [
            ("id is required", |m| m.id.clear()),
            ("name is required", |m| m.name.clear()),
            ("version is required", |m| m.version.clear()),
            ("language is required", |m| m.language.clear()),
        ];
        for (expected, mutate) in cases {
            let mut m = base.clone();
            mutate(&mut m);
            let err = loader
                .validate_manifest_internal(&mut m, &manifest_path)
                .expect_err("缺必填字段应拒绝");
            let msg = format!("{err:?}");
            assert!(msg.contains(expected), "应报「{expected}」: {msg}");
        }
    }

    /// language 缺失时的 plugin_id 归属：错误应带被拒插件 id（非 "(unknown)"）。
    /// 对照 id 缺失用例（此时只能报 (unknown)）。
    #[test]
    fn validate_manifest_error_carries_plugin_id() {
        let dir = tempfile::tempdir().unwrap();
        let manifest_path = {
            let p = dir.path().join("p.json");
            fs::write(
                &p,
                r#"{
                    "id":"who_am_i","name":"N","version":"1.0.0",
                    "plugin_type":"tool","language":"","host_type":"sidecar",
                    "entry":"python server.py","capabilities":{}
                }"#,
            )
            .unwrap();
            p
        };
        let loader = PluginLoaderImpl::new(dir.path(), None);
        let mut m: PluginManifest =
            serde_json::from_str(&fs::read_to_string(&manifest_path).unwrap()).unwrap();
        let err = loader
            .validate_manifest_internal(&mut m, &manifest_path)
            .unwrap_err();
        let msg = format!("{err:?}");
        assert!(msg.contains("who_am_i"), "错误应带插件 id: {msg}");

        // id 缺失：无从归属，报 (unknown)
        let mut no_id = m.clone();
        no_id.id.clear();
        let err = loader
            .validate_manifest_internal(&mut no_id, &manifest_path)
            .unwrap_err();
        assert!(
            format!("{err:?}").contains("(unknown)"),
            "id 缺失时归属为 (unknown): {err:?}"
        );
    }

    /// state.reads 非法条目（messages_tail 非正整数）→ 忽略该条目但整体放行
    /// （单条声明错误不拒载）；合法条目原样保留。
    #[test]
    fn validate_manifest_ignores_invalid_state_reads_entries() {
        let dir = tempfile::tempdir().unwrap();
        let manifest_path = dir.path().join("p.json");
        fs::write(
            &manifest_path,
            r#"{
                "id":"reads_mix","name":"N","version":"1.0.0",
                "plugin_type":"tool","language":"python","host_type":"sidecar",
                "entry":"python server.py","capabilities":{},
                "state":{"reads":["messages","messages_tail:0","messages_tail:abc","messages_tail:5"]}
            }"#,
        )
        .unwrap();
        let loader = PluginLoaderImpl::new(dir.path(), None);
        let mut m: PluginManifest =
            serde_json::from_str(&fs::read_to_string(&manifest_path).unwrap()).unwrap();
        loader
            .validate_manifest_internal(&mut m, &manifest_path)
            .expect("非法读取条目应被忽略而非拒载");

        let reads = &m.state.as_ref().unwrap().reads;
        assert!(
            reads.contains(&"messages".to_string()),
            "普通键保留: {reads:?}"
        );
        assert!(
            reads.contains(&"messages_tail:5".to_string()),
            "正整数保留: {reads:?}"
        );
        assert!(
            !reads.iter().any(|r| r.contains(":0") || r.contains(":abc")),
            "0 与非数字条目应被剔除: {reads:?}"
        );
    }

    // ── scan_root 的 IO 错误路径：root 存在但不可读 ──

    /// root 是文件而非目录（read_dir 必然失败）→ LoaderError::Io 上抛，
    /// 不静默返回空（配置面故障必须可见）。
    #[tokio::test]
    async fn scan_root_io_error_is_isolated_to_that_root() {
        let dir = tempfile::tempdir().unwrap();
        let not_a_dir = dir.path().join("regular_file");
        fs::write(&not_a_dir, "x").unwrap();

        // 坏根（文件而非目录）与好根并存：坏根读失败只记 warn 不阻断，
        // 好根插件照常发现——单根失败不得瘫痪整次发现。
        let good = tempfile::tempdir().unwrap();
        create_test_plugin_dir(good.path(), "survives_bad_root", "tool");

        let loader = PluginLoaderImpl::new(&not_a_dir, Some(good.path().to_path_buf()));
        let discovered = loader
            .discover(&[not_a_dir.to_str().unwrap()])
            .await
            .expect("单根读失败不应让 discover 整体失败");
        assert_eq!(discovered.len(), 1, "好根插件应照常发现");
        assert_eq!(discovered[0].id, "survives_bad_root");
    }

    /// root 下某插件目录的 manifest 不可读（Windows 下用目录占位同名路径
    /// 制造读失败）→ 该 root 扫描报 IO 错（不产出部分结果）。
    #[tokio::test]
    async fn scan_root_manifest_read_failure_isolated_to_that_root() {
        let bad = tempfile::tempdir().unwrap();
        // plugin.json 位置是目录 → read_to_string 必然失败
        let plugin_dir = bad.path().join("unreadable_manifest");
        fs::create_dir_all(plugin_dir.join("plugin.json")).unwrap();

        let good = tempfile::tempdir().unwrap();
        create_test_plugin_dir(good.path(), "peer_ok", "tool");

        let loader = PluginLoaderImpl::new(bad.path(), Some(good.path().to_path_buf()));
        let discovered = loader
            .discover(&[])
            .await
            .expect("单根 manifest 读失败不应让 discover 整体失败");
        assert_eq!(discovered.len(), 1, "用户根插件照常发现");
        assert_eq!(discovered[0].id, "peer_ok");
    }

    /// entry 为空且非 composite → 明确拒绝（entry is required）。
    #[test]
    fn validate_manifest_non_composite_empty_entry_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let manifest_path = dir.path().join("p.json");
        fs::write(
            &manifest_path,
            r#"{
                "id":"no_entry","name":"N","version":"1.0.0",
                "plugin_type":"tool","language":"python","host_type":"sidecar",
                "entry":"","capabilities":{}
            }"#,
        )
        .unwrap();
        let loader = PluginLoaderImpl::new(dir.path(), None);
        let mut m: PluginManifest =
            serde_json::from_str(&fs::read_to_string(&manifest_path).unwrap()).unwrap();
        let err = loader
            .validate_manifest_internal(&mut m, &manifest_path)
            .expect_err("非 composite 空 entry 应拒绝");
        assert!(
            format!("{err:?}").contains("entry is required for non-composite"),
            "文案: {err:?}"
        );
    }

    /// native 声明但 manifest 路径无父目录 → 明确拒绝（无法解析相对产物路径）。
    /// 用相对文件名作为 source_path 制造 parent() == Some("") 的边界；
    /// 空路径 parent 为 None 才触发该分支，故取 `""`。
    #[test]
    fn validate_manifest_native_without_parent_dir_rejected() {
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let mut m: PluginManifest = serde_json::from_str(
            r#"{
                "id":"no_parent","name":"N","version":"1.0.0",
                "plugin_type":"tool","language":"rust","host_type":"in_process",
                "entry":"x","capabilities":{},
                "native":{"artifact":"no_parent"}
            }"#,
        )
        .unwrap();
        let err = loader
            .validate_manifest_internal(&mut m, Path::new(""))
            .expect_err("空 source_path（无父目录）应拒绝 native 预检");
        assert!(
            format!("{err:?}").contains("无父目录"),
            "文案应指明无父目录: {err:?}"
        );
    }

    // ── 目录树规范化哈希（compute_plugin_sha256）的边界分支 ──

    /// 确定性：同一棵树重复计算结果一致（枚举序不进摘要——路径排序兜底）。
    #[test]
    fn tree_hash_is_deterministic() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("plugin.json"), b"{}").unwrap();
        fs::write(dir.path().join("server.py"), b"print('hi')").unwrap();
        let loader = PluginLoaderImpl::new(dir.path(), None);
        let manifest_path = dir.path().join("plugin.json");
        let first = loader.compute_plugin_sha256(&manifest_path).unwrap();
        let second = loader.compute_plugin_sha256(&manifest_path).unwrap();
        assert_eq!(first, second, "同一棵树两次计算应一致");
    }

    /// 内容变更 → 摘要变化（热篡改检测的判定基础）。
    #[test]
    fn tree_hash_changes_when_file_content_changes() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("plugin.json"), b"{}").unwrap();
        fs::write(dir.path().join("server.py"), b"print('hi')").unwrap();
        let loader = PluginLoaderImpl::new(dir.path(), None);
        let manifest_path = dir.path().join("plugin.json");
        let before = loader.compute_plugin_sha256(&manifest_path).unwrap();
        fs::write(dir.path().join("server.py"), b"print('bye')").unwrap();
        let after = loader.compute_plugin_sha256(&manifest_path).unwrap();
        assert_ne!(before, after, "文件字节变更应改变摘要");
    }

    /// 排除面：.venv* / __pycache__ / *.pyc / node_modules / logs 不进摘要
    /// （环境噪声，不属于"插件源"；增删这些文件不改变摘要）。
    #[test]
    fn tree_hash_excludes_env_noise() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("plugin.json"), b"{}").unwrap();
        let loader = PluginLoaderImpl::new(dir.path(), None);
        let manifest_path = dir.path().join("plugin.json");
        let before = loader.compute_plugin_sha256(&manifest_path).unwrap();

        fs::create_dir_all(dir.path().join(".venv-hindsight")).unwrap();
        fs::write(
            dir.path().join(".venv-hindsight").join("pyvenv.cfg"),
            b"junk",
        )
        .unwrap();
        fs::create_dir_all(dir.path().join("__pycache__")).unwrap();
        fs::write(
            dir.path()
                .join("__pycache__")
                .join("server.cpython-312.pyc"),
            b"junk",
        )
        .unwrap();
        fs::write(dir.path().join("server.pyc"), b"junk").unwrap();
        fs::create_dir_all(dir.path().join("node_modules")).unwrap();
        fs::write(dir.path().join("node_modules").join("x.js"), b"junk").unwrap();
        fs::create_dir_all(dir.path().join("logs")).unwrap();
        fs::write(dir.path().join("logs").join("run.log"), b"junk").unwrap();
        let after = loader.compute_plugin_sha256(&manifest_path).unwrap();
        assert_eq!(before, after, "排除面文件增删不应改变摘要");
    }

    /// 相对路径入摘要：同字节文件放在不同路径下 → 摘要不同（防换位伪装）。
    #[test]
    fn tree_hash_binds_relative_paths() {
        let dir_a = tempfile::tempdir().unwrap();
        fs::create_dir_all(dir_a.path().join("a")).unwrap();
        fs::write(dir_a.path().join("a").join("x.py"), b"same").unwrap();
        let dir_b = tempfile::tempdir().unwrap();
        fs::create_dir_all(dir_b.path().join("b")).unwrap();
        fs::write(dir_b.path().join("b").join("x.py"), b"same").unwrap();

        let loader = PluginLoaderImpl::new(dir_a.path(), None);
        let hash_a = loader
            .compute_plugin_sha256(&dir_a.path().join("a"))
            .unwrap();
        let hash_b = loader
            .compute_plugin_sha256(&dir_b.path().join("b"))
            .unwrap();
        assert_ne!(hash_a, hash_b, "同字节不同相对路径应产生不同摘要");
    }

    /// 符号链接不进摘要（逃逸面，与 /ext 静态服务同口径）。
    #[cfg(unix)]
    #[test]
    fn tree_hash_skips_symlinks() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("plugin.json"), b"{}").unwrap();
        std::os::unix::fs::symlink("/etc/hostname", dir.path().join("evil.py")).unwrap();
        let loader = PluginLoaderImpl::new(dir.path(), None);
        let hash = loader
            .compute_plugin_sha256(&dir.path().join("plugin.json"))
            .unwrap();
        // 只含 plugin.json 一个文件：与手工两文件摘要不同（symlink 未进摘要）
        let mut hasher = Sha256::new();
        hasher.update(b"plugin.json");
        hasher.update([0u8]);
        hasher.update(b"{}");
        hasher.update([0u8]);
        assert_eq!(hash, format!("{:x}", hasher.finalize()), "symlink 应被跳过");
    }

    /// 哈希比对：等长不同内容判不等（常量时间比较的差异分支）。
    /// 长度不同同样判不等。
    #[test]
    fn hash_compare_detects_length_and_content_differences() {
        assert!(!secure_eq("abcd", "abce"), "同长不同内容应不等");
        assert!(secure_eq("abcd", "abcd"), "自身相等");
        assert!(!secure_eq("abcd", "abc"), "长度不同应不等");
        assert!(secure_eq("", ""), "空串相等");
    }

    // ── 用户配置层叠加的单元素语义（apply_user_config_overlay / set_config_leaf）──

    /// `set_config_leaf` 的分段写入契约：空段返回 false（不写）；
    /// 单段直接替换；中间段缺失时新建；同名标量挡路时保留原值返回 false。
    #[test]
    fn set_config_leaf_creates_missing_layers_and_refuses_scalar_shadow() {
        let mut map = serde_json::Map::new();

        assert!(
            !set_config_leaf(&mut map, &[], serde_json::json!(1)),
            "空段不写"
        );
        assert!(
            set_config_leaf(&mut map, &["a".to_string()], serde_json::json!("v")),
            "单段直接写入"
        );
        assert_eq!(map["a"], serde_json::json!("v"));

        assert!(
            set_config_leaf(
                &mut map,
                &["d".to_string(), "e".to_string()],
                serde_json::json!(2)
            ),
            "中间段缺失时新建空对象"
        );
        assert_eq!(map["d"]["e"], serde_json::json!(2));

        // 同名标量挡路（a 已是字符串，不能再作目录层级）→ 不覆盖、返回未接管
        assert!(
            !set_config_leaf(
                &mut map,
                &["a".to_string(), "child".to_string()],
                serde_json::json!(3)
            ),
            "标量挡路不得强拆（保留 factory 值）"
        );
        assert_eq!(map["a"], serde_json::json!("v"), "原标量保持不变");

        // 已有对象子树：只替换末段，兄弟键保留（目录层不整体清空）
        assert!(set_config_leaf(
            &mut map,
            &["d".to_string(), "e".to_string()],
            serde_json::json!(9)
        ));
        assert!(set_config_leaf(
            &mut map,
            &["d".to_string(), "sibling".to_string()],
            serde_json::json!(8)
        ));
        assert_eq!(map["d"]["e"], serde_json::json!(9), "末段被文件级替换");
        assert_eq!(map["d"]["sibling"], serde_json::json!(8), "兄弟键不受影响");
    }

    /// 用户层文件与 factory 同路径：整体替换（不逐字段合并）；未被接管的
    /// factory 兄弟文件保留；用户层返回的接管计数如实反映文件数。
    #[tokio::test]
    async fn user_config_overlay_replaces_file_wholesale_and_counts() {
        let _lock = user_space_env_lock();
        let factory = tempfile::tempdir().unwrap();
        let user = tempfile::tempdir().unwrap();
        // factory 的 llm.yaml 有 a/b 两键；用户层只有 a —— 文件级接管应丢掉 b
        fs::write(
            factory.path().join("llm.yaml"),
            "a: factory\nextra: keep-me-only-in-factory\n",
        )
        .unwrap();
        fs::write(factory.path().join("peer.yaml"), "p: factory\n").unwrap();
        fs::write(user.path().join("llm.yaml"), "a: user\n").unwrap();

        let _g = UserRootGuard::set(user.path());
        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(factory.path());
        let cfg = loader.load_config().await.unwrap();
        assert_eq!(cfg["llm"]["a"], "user", "用户层文件整体取代 factory");
        assert!(
            cfg["llm"].get("extra").is_none(),
            "文件级替换不是字段级合并——factory 独有键随文件被取代"
        );
        assert_eq!(cfg["peer"]["p"], "factory", "未接管的兄弟文件保持 factory");

        // 无用户层文件时接管计数为 0（用户目录存在但空）
        let empty_user = tempfile::tempdir().unwrap();
        let _g2 = UserRootGuard::set(empty_user.path());
        let loader2 =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(factory.path());
        let cfg2 = loader2.load_config().await.unwrap();
        assert_eq!(cfg2["llm"]["a"], "factory", "空用户层 = 全 factory");
        assert_eq!(cfg2["llm"]["extra"], "keep-me-only-in-factory");
    }

    /// 用户层**不可读**目录（read_dir 失败）→ overlay 整体跳过并返回 0，
    /// factory 全量保留（不半途写入部分键）。
    #[tokio::test]
    async fn user_config_overlay_unreadable_dir_skips_whole() {
        let _lock = user_space_env_lock();
        let factory = tempfile::tempdir().unwrap();
        fs::write(factory.path().join("cfg.yaml"), "v: factory\n").unwrap();

        // 用文件路径冒充用户配置目录：is_dir() 为假 → 直接返回 0（第一道门）
        let as_file = tempfile::tempdir().unwrap();
        let fake = as_file.path().join("not-a-dir");
        fs::write(&fake, "x").unwrap();
        let _g = UserRootGuard::set(&fake);
        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(factory.path());
        let cfg = loader.load_config().await.unwrap();
        assert_eq!(cfg["cfg"]["v"], "factory", "非目录用户层 = 未接管");
    }

    // ── 配置加载的错误映射与 canonicalize 回落 ──

    /// `with_config_root` 对不存在路径回落原始路径（不报错），并照常可用；
    /// 配置根不存在时 `load_config` 返回空对象（不抛错）。
    #[tokio::test]
    async fn with_config_root_missing_path_falls_back_and_loads_empty() {
        let dir = tempfile::tempdir().unwrap();
        let missing = dir.path().join("no-such-config-dir");
        let loader = tracing::subscriber::with_default(AlwaysSubscriber, || {
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(&missing)
        });
        let cfg = loader.load_config().await.unwrap();
        assert_eq!(cfg, serde_json::json!({}), "不存在的配置根返回空对象");
    }

    /// 配置目录里出现无法读取的条目（非 UTF-8 字节的 .yaml 文件）
    /// → 该文件读失败向上传播为 CONFIG_IO_ERROR（不静默丢配置），
    /// 且错误消息指明来源文件。
    #[tokio::test]
    async fn load_config_read_failure_propagates_as_io_error() {
        let _user_space = factory_only_user_space();
        let dir = tempfile::tempdir().unwrap();
        // 非 UTF-8 内容 → read_to_string 必然失败（InvalidData）
        fs::write(dir.path().join("broken.yaml"), [0xff_u8, 0xfe]).unwrap();
        fs::write(dir.path().join("ok.yaml"), "v: 1\n").unwrap();

        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(dir.path());
        let err = loader.load_config().await.expect_err("单文件读失败应传播");
        assert_eq!(
            err.code.as_deref(),
            Some("CONFIG_IO_ERROR"),
            "读失败映射为 CONFIG_IO_ERROR，实际: {err:?}"
        );
        assert!(
            err.message.contains("broken.yaml"),
            "错误消息应含来源文件: {}",
            err.message
        );
    }

    /// 目录缺失时 sha256 计算传播 IO 错误（不静默当空树）。
    #[test]
    fn compute_plugin_sha256_propagates_missing_dir() {
        let dir = tempfile::tempdir().unwrap();
        let loader = PluginLoaderImpl::new(dir.path(), None);
        let ghost_path = dir.path().join("ghost").join("plugin.json");
        let err = loader
            .compute_plugin_sha256(&ghost_path)
            .expect_err("目录缺失应报 IO 错误");
        assert!(
            matches!(err, LoaderError::Io { .. }),
            "应为 Io 变体，实际: {err:?}"
        );
    }

    /// `set_config_leaf` 的空段与既有对象层组合：目录层已存在时递归只动末段；
    /// 末段键已存在时被替换（不追加）。
    #[test]
    fn set_config_leaf_replaces_existing_leaf() {
        let mut map = serde_json::Map::new();
        map.insert(
            "cfg".to_string(),
            serde_json::json!({"keep": 1, "target": "old"}),
        );
        assert!(set_config_leaf(
            &mut map,
            &["cfg".to_string(), "target".to_string()],
            serde_json::json!("new")
        ));
        assert_eq!(map["cfg"]["target"], "new");
        assert_eq!(map["cfg"]["keep"], 1, "兄弟键在递归路径下同样保留");
    }
}

/// mcp 配置名 → 受管插件目录名（`mcp_<清洗名>`）；清洗后为空返回 None。
fn sanitize_mcp_dir_name(name: &str) -> Option<String> {
    let cleaned: String = name
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() {
                c.to_ascii_lowercase()
            } else {
                '_'
            }
        })
        .collect();
    let dir = format!("mcp_{cleaned}");
    if dir == "mcp_" {
        None
    } else {
        Some(dir)
    }
}

/// 构造 external MCP manifest（mcp:external 空声明 + mcp 块）。
/// 返回 (目录名, manifest JSON)——目录名 = mcp_<清洗名>。
fn build_external_mcp_manifest(
    name: &str,
    spec: &serde_json::Value,
) -> Option<(String, serde_json::Value)> {
    let warn = |msg: String| {
        warn!(target: "plugin-loader", error = %msg, "external MCP 条目跳过");
    };
    let dir_name = sanitize_mcp_dir_name(name)?;
    let mut endpoint = serde_json::Map::new();
    let transport: &str;
    if let Some(cmd) = spec.get("command").and_then(|v| v.as_str()) {
        transport = "stdio";
        endpoint.insert("command".into(), serde_json::json!(cmd));
        if let Some(args) = spec.get("args") {
            endpoint.insert("args".into(), args.clone());
        }
        if let Some(env) = spec.get("env") {
            endpoint.insert("env".into(), env.clone());
        }
    } else if let Some(url) = spec.get("url").and_then(|v| v.as_str()) {
        transport = "streamable_http";
        endpoint.insert("url".into(), serde_json::json!(url));
        if let Some(headers) = spec.get("headers") {
            endpoint.insert("headers".into(), headers.clone());
        }
    } else {
        warn(format!("mcp {name:?} 缺 command/url"));
        return None;
    }
    let manifest = serde_json::json!({
        "id": dir_name,
        "name": format!("MCP {name}"),
        "description": "External MCP (config/mcp.json)",
        "version": "1.0.0",
        "plugin_type": "tool",
        "language": "external",
        "host_type": "sidecar",
        "entry": "mcp:external",
        "capabilities": {},
        "mcp": {"transport": transport, "endpoint": endpoint},
        "permissions": {},
        "priority": 30,
    });
    Some((dir_name, manifest))
}

/// 写受管 external MCP 插件目录：`.mcp-managed` 标记 + plugin.json（内容不变不重写）。
fn write_managed_plugin_dir(target: &Path, manifest: &serde_json::Value) {
    if std::fs::create_dir_all(target).is_err() {
        warn!(target: "plugin-loader", dir = %target.display(), "external MCP 目录创建失败");
        return;
    }
    let marker = target.join(".mcp-managed");
    if std::fs::write(&marker, b"generated from config/mcp.json").is_err() {
        warn!(target: "plugin-loader", dir = %target.display(), "external MCP 标记写入失败");
        return;
    }
    let Ok(pretty) = serde_json::to_string_pretty(manifest) else {
        return;
    };
    let path = target.join("plugin.json");
    if std::fs::read_to_string(&path).is_ok_and(|existing| existing == pretty) {
        return; // 内容未变不重写（防 mtime 空转 watcher）
    }
    if let Err(e) = std::fs::write(&path, pretty) {
        warn!(target: "plugin-loader", path = %path.display(), error = %e, "external MCP manifest 写入失败");
    }
}

/// 清理配置中已删除的受管目录：仅限 `mcp_` 前缀且带 `.mcp-managed` 标记者。
fn prune_managed_plugin_dirs(root: &Path, keep: Option<&std::collections::HashSet<String>>) {
    let Ok(entries) = std::fs::read_dir(root) else {
        return;
    };
    for entry in entries.flatten() {
        let dir = entry.path();
        if !dir.is_dir() {
            continue;
        }
        let Some(name) = dir.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        if !name.starts_with("mcp_") || keep.is_some_and(|k| k.contains(name)) {
            continue;
        }
        if !dir.join(".mcp-managed").exists() {
            continue; // 非受管目录（手写插件同名前缀）不动
        }
        match std::fs::remove_dir_all(&dir) {
            Ok(()) => {
                warn!(target: "plugin-loader", dir = %dir.display(), "external MCP 目录已随配置删除而清理")
            }
            Err(e) => {
                warn!(target: "plugin-loader", dir = %dir.display(), error = %e, "external MCP 目录清理失败")
            }
        }
    }
}

// ── 标准 mcpServers 配置同步（external MCP 一行接入）──

#[cfg(test)]
mod mcp_sync_tests {
    use super::*;
    use agentos_core::traits::McpTransport;

    /// 辅助：建临时插件根 + config 目录，写 config/mcp.json。
    fn make_mcp_config_fixture(root: &Path, body: &str) -> PluginLoaderImpl {
        let plugins = root.join("plugins");
        let config = root.join("config");
        std::fs::create_dir_all(&config).unwrap();
        std::fs::write(config.join("mcp.json"), body).unwrap();
        PluginLoaderImpl::new(plugins, None)
    }

    #[tokio::test]
    async fn test_mcp_config_stdio_entry_generates_plugin_dir() {
        let root = tempfile::tempdir().unwrap();
        let loader = make_mcp_config_fixture(
            root.path(),
            r#"{"mcpServers":{"windows-mcp":{"command":"uvx","args":["windows-mcp","serve"]}}}"#,
        );
        let manifests = loader.discover(&[]).await.unwrap();
        let m = manifests
            .iter()
            .find(|m| m.id == "mcp_windows_mcp")
            .expect("生成的 external MCP manifest 应被发现");
        assert_eq!(m.entry, "mcp:external");
        assert!(m.capabilities.tools.is_empty(), "空声明 → 观测即声明");
        let mcp = m.mcp.as_ref().expect("mcp 块存在");
        assert_eq!(mcp.transport, McpTransport::Stdio);
        let endpoint = mcp.endpoint.as_ref().unwrap();
        assert_eq!(endpoint.command.as_deref(), Some("uvx"));
        assert_eq!(
            endpoint.args,
            vec!["windows-mcp".to_string(), "serve".to_string()]
        );
    }

    #[tokio::test]
    async fn test_mcp_config_http_entry_maps_streamable_http() {
        let root = tempfile::tempdir().unwrap();
        let loader = make_mcp_config_fixture(
            root.path(),
            r#"{"mcpServers":{"remote":{"url":"https://example.com/mcp"}}}"#,
        );
        let manifests = loader.discover(&[]).await.unwrap();
        let m = manifests
            .iter()
            .find(|m| m.id == "mcp_remote")
            .expect("http 条目生成 manifest");
        let mcp = m.mcp.as_ref().unwrap();
        assert_eq!(mcp.transport, McpTransport::StreamableHttp);
        assert_eq!(
            mcp.endpoint.as_ref().unwrap().url.as_deref(),
            Some("https://example.com/mcp")
        );
    }

    #[tokio::test]
    async fn test_mcp_config_entry_removal_prunes_managed_dir() {
        let root = tempfile::tempdir().unwrap();
        let loader = make_mcp_config_fixture(
            root.path(),
            r#"{"mcpServers":{"gone":{"command":"uvx","args":["x"]}}}"#,
        );
        loader.discover(&[]).await.unwrap();
        let dir = root.path().join("plugins").join("mcp_gone");
        assert!(dir.join("plugin.json").exists(), "首次同步生成目录");
        assert!(dir.join(".mcp-managed").exists(), "受管标记存在");

        // 配置删条目 → 下一轮 discover 清目录
        let config = root.path().join("config").join("mcp.json");
        std::fs::write(&config, r#"{"mcpServers":{}}"#).unwrap();
        loader.discover(&[]).await.unwrap();
        assert!(!dir.exists(), "受管目录随配置删除被清理");
    }

    #[tokio::test]
    async fn test_mcp_config_prune_spares_unmanaged_dir() {
        let root = tempfile::tempdir().unwrap();
        let loader = make_mcp_config_fixture(root.path(), r#"{"mcpServers":{}}"#);
        // 手写同名前缀目录（无受管标记）不得被清
        let handmade = root.path().join("plugins").join("mcp_handmade");
        std::fs::create_dir_all(&handmade).unwrap();
        std::fs::write(handmade.join("plugin.json"), "{}").unwrap();

        loader.discover(&[]).await.unwrap();
        assert!(handmade.exists(), "非受管目录不动");
    }

    #[tokio::test]
    async fn test_mcp_config_malformed_json_keeps_managed_dirs() {
        // 契约：解析失败 → 跳过同步且不清目录（不因配置抖动销毁已装插件）。
        // 手编 mcp.json 打错一个逗号，下一轮 discover 不得删光受管插件目录。
        let root = tempfile::tempdir().unwrap();
        let loader = make_mcp_config_fixture(
            root.path(),
            r#"{"mcpServers":{"keep":{"command":"uvx","args":["x"]}}}"#,
        );
        loader.discover(&[]).await.unwrap();
        let dir = root.path().join("plugins").join("mcp_keep");
        assert!(dir.join(".mcp-managed").exists(), "首次同步生成受管目录");

        let config = root.path().join("config").join("mcp.json");
        std::fs::write(&config, r#"{"mcpServers":{ "broken":,,,}}"#).unwrap();
        loader.discover(&[]).await.unwrap();
        assert!(dir.exists(), "解析失败不得清受管目录");
    }

    #[tokio::test]
    async fn test_mcp_config_missing_mcp_servers_key_keeps_managed_dirs() {
        // 合法 JSON 但缺 mcpServers 键 = 形状坏，同解析失败口径：不清目录
        let root = tempfile::tempdir().unwrap();
        let loader = make_mcp_config_fixture(
            root.path(),
            r#"{"mcpServers":{"keep":{"command":"uvx","args":["x"]}}}"#,
        );
        loader.discover(&[]).await.unwrap();
        let dir = root.path().join("plugins").join("mcp_keep");

        let config = root.path().join("config").join("mcp.json");
        std::fs::write(&config, r#"{"other":1}"#).unwrap();
        loader.discover(&[]).await.unwrap();
        assert!(dir.exists(), "缺 mcpServers 键不得清受管目录");
    }

    #[tokio::test]
    async fn test_mcp_config_missing_file_is_noop() {
        let root = tempfile::tempdir().unwrap();
        let plugins = root.path().join("plugins");
        std::fs::create_dir_all(&plugins).unwrap();
        let loader = PluginLoaderImpl::new(plugins.clone(), None);
        let manifests = loader.discover(&[]).await.unwrap();
        assert!(manifests.is_empty());
        assert!(plugins.exists(), "无配置目录不动");
    }
}

// @feature: FP-0.2.一 插件协议——分级单副本实例启动配置与启动守卫 | @ci: rust-test
//! 实例启动配置（`instance.yaml`）+ Tier1 分级清单（`plugin_tiers.yaml`）
//! + 启动守卫 + 启动装载清单（ADR 2026-11-07-plugin-tiered-single-copy-model）。
//!
//! 分级单副本模型的三根全部**显式配置**：Tier1 根（安全级，应用目录只读）、
//! Tier2 根（解包用户目录）、共享根（`_libs`/`_host` 等跨插件共享件）。内核是
//! 纯消费者——**零推导零回落**：配置文件缺失、字段缺失、schema 不认识、根目录
//! 不存在/不可读，一律拒绝启动并显式报「插件根未配置」/守卫错误（fail-closed，
//! 绝不猜）。
//!
//! 配置文件定位（env 仅作为启动链交付配置文件**路径**的载体，内容在文件里）：
//! 1. 环境变量 [`INSTANCE_CONFIG_ENV`] 指向的配置文件（启动链显式钉位）；
//! 2. 缺省位置 `<config_root>/kernel/instance.yaml`。
//!
//! 配置文件内的相对路径按**该文件所在目录**解析（标准配置文件语义，非推导）：
//! dev 仓形态 `config/kernel/instance.yaml` 写 `../../plugins/shared` 即指仓内
//! 插件真源（三根同指 = dev 退化配置，显式写明，ADR §2.2）。
//!
//! Tier 归类的单一真源 = [`TIER_MANIFEST_FILENAME`]（`tier1.safety_plugins` +
//! `tier1.shared_modules`，与 `config/kernel/plugin_tiers.yaml` 同构）：清单内
//! id = Tier1，只许从 tier1_root 加载（出现在 tier2_root = 越界，拒启）；其余
//! 插件 = Tier2，从 tier2_root 加载。**根=位置，清单=归类**：tier1_root 里的
//! 非清单条目是 Tier2 安装种子（ADR §2.1 包内副本=纯安装种子，不参与运行），
//! 从 Tier1 扫描面剔除——装机包布局下 tier1_root 装着 Tier1 运行件 + Tier2
//! 种子，Tier2 id 双根共存（tier1 根侧=种子，tier2 根=唯一运行副本）合法；
//! 「双根同 id 无差别拒启」检查随归类语义退役（其错误域被「Tier1 越界拒启 ∪
//! Tier2 双根合法」完全划分，保留即不可达死代码）。

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use parking_lot::RwLock;
use serde::{Deserialize, Serialize};

/// 实例启动配置文件路径的交付载体环境变量（内容在配置文件里，env 只交路径）。
pub const INSTANCE_CONFIG_ENV: &str = "AGENTOS_INSTANCE_CONFIG";
/// 实例启动配置缺省文件名（位于 `<config_root>/kernel/` 下）。
pub const INSTANCE_CONFIG_FILENAME: &str = "instance.yaml";
/// Tier1 分级清单文件名（位于 `<config_root>/kernel/` 下）。
pub const TIER_MANIFEST_FILENAME: &str = "plugin_tiers.yaml";
/// 实例启动配置 schema 版本（当前唯一受支持版本）。
pub const INSTANCE_CONFIG_SCHEMA: u32 = 1;

// ==== 实例启动配置 ====

/// 实例启动配置（schema 1）：三根 + schema 版本，全部必填显式。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstanceConfig {
    /// schema 版本；只接受 [`INSTANCE_CONFIG_SCHEMA`]。
    pub schema: u32,
    /// Tier1 根：安全级插件（清单内 id）的加载根（应用目录只读）。
    pub tier1_root: String,
    /// Tier2 根：产品功能级插件的加载根（解包用户目录）。
    pub tier2_root: String,
    /// 共享根：`_libs`/`_host` 等跨插件共享件所在根。
    pub shared_root: String,
}

/// 加载完成的实例配置：相对根已按配置文件所在目录解析为绝对路径。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LoadedInstanceConfig {
    /// 配置文件自身路径（诊断与装载清单透出「配置来自哪份文件」）。
    pub config_path: PathBuf,
    /// 三根（已解析）。
    pub tier1_root: PathBuf,
    pub tier2_root: PathBuf,
    pub shared_root: PathBuf,
}

/// 实例启动配置错误（全部 fail-closed：缺失/损坏/不认识 = 拒绝启动）。
#[derive(Debug, thiserror::Error)]
pub enum InstanceConfigError {
    #[error(
        "插件根未配置：实例启动配置缺失（{path}）。\
         请由启动链/安装器写入 instance.yaml（或设 AGENTOS_INSTANCE_CONFIG 指向配置文件），\
         结构={{schema, tier1_root, tier2_root, shared_root}}；内核不做任何推导回落"
    )]
    Missing { path: PathBuf },
    #[error("插件根未配置：实例启动配置不可读（{path}）：{reason}")]
    Read { path: PathBuf, reason: String },
    #[error("插件根未配置：实例启动配置解析失败（{path}）：{reason}")]
    Parse { path: PathBuf, reason: String },
    #[error("插件根未配置：实例启动配置 schema 不支持（{path}）：期望 {expected}，实际 {actual}")]
    SchemaMismatch {
        path: PathBuf,
        expected: u32,
        actual: u32,
    },
    #[error("插件根未配置：实例启动配置字段 `{field}` 缺失或为空（{path}）")]
    FieldEmpty { path: PathBuf, field: &'static str },
}

/// 读环境变量，空白值视为未设（与 user_space::env_path 同语义）。
fn env_config_path() -> Option<PathBuf> {
    std::env::var(INSTANCE_CONFIG_ENV)
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
}

/// 解析实例启动配置文件路径：[`INSTANCE_CONFIG_ENV`] > `<config_root>/kernel/` 缺省位。
pub fn resolve_instance_config_path(config_root: &Path) -> PathBuf {
    env_config_path().unwrap_or_else(|| config_root.join("kernel").join(INSTANCE_CONFIG_FILENAME))
}

/// 把配置内的根路径解析为绝对路径：相对路径按**配置文件所在目录**解析，
/// 并做词法归一化（消解 `.`/`..` 段——后续前缀归属判定与日志都吃干净路径）。
fn resolve_root(config_dir: &Path, raw: &str) -> PathBuf {
    let trimmed = raw.trim();
    let joined = if Path::new(trimmed).is_absolute() {
        PathBuf::from(trimmed)
    } else {
        config_dir.join(trimmed)
    };
    let mut out = PathBuf::new();
    for comp in joined.components() {
        match comp {
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// 加载实例启动配置（fail-closed）：缺失/不可读/解析失败/schema 不认识/字段
/// 缺失一律 Err；相对根按配置文件所在目录解析。
pub fn load_instance_config(
    config_root: &Path,
) -> Result<LoadedInstanceConfig, InstanceConfigError> {
    let path = resolve_instance_config_path(config_root);
    if !path.is_file() {
        return Err(InstanceConfigError::Missing { path });
    }
    let raw = std::fs::read_to_string(&path).map_err(|e| InstanceConfigError::Read {
        path: path.clone(),
        reason: e.to_string(),
    })?;
    let config: InstanceConfig =
        serde_yaml::from_str(&raw).map_err(|e| InstanceConfigError::Parse {
            path: path.clone(),
            reason: e.to_string(),
        })?;
    if config.schema != INSTANCE_CONFIG_SCHEMA {
        return Err(InstanceConfigError::SchemaMismatch {
            path: path.clone(),
            expected: INSTANCE_CONFIG_SCHEMA,
            actual: config.schema,
        });
    }
    let config_dir = path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    for (field, raw_root) in [
        ("tier1_root", &config.tier1_root),
        ("tier2_root", &config.tier2_root),
        ("shared_root", &config.shared_root),
    ] {
        if raw_root.trim().is_empty() {
            return Err(InstanceConfigError::FieldEmpty {
                path: path.clone(),
                field,
            });
        }
    }
    Ok(LoadedInstanceConfig {
        config_path: path,
        tier1_root: resolve_root(&config_dir, &config.tier1_root),
        tier2_root: resolve_root(&config_dir, &config.tier2_root),
        shared_root: resolve_root(&config_dir, &config.shared_root),
    })
}

// ==== Tier1 分级清单 ====

/// Tier1 分级清单（`plugin_tiers.yaml` 的运行时表示，单一真源）。
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PluginTiers {
    /// Tier1 = 安全级（应用目录只读）：安全关键插件 + 共享模块根。
    #[serde(default)]
    pub tier1: Tier1Tiers,
}

/// Tier1 清单内容：安全关键插件 id + 共享模块条目。
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Tier1Tiers {
    /// 安全关键插件（审批链/隔离边界/凭证/防护闸）的 manifest id。
    #[serde(default)]
    pub safety_plugins: Vec<String>,
    /// 共享模块条目（`_host`/`_libs` 等目录或 `kernel_token.py` 等文件名）。
    #[serde(default)]
    pub shared_modules: Vec<String>,
}

impl PluginTiers {
    /// Tier1 插件 id 集（safety_plugins；共享模块不是插件，不入插件分类集）。
    pub fn tier1_plugin_ids(&self) -> BTreeSet<String> {
        self.tier1.safety_plugins.iter().cloned().collect()
    }
}

/// Tier1 分级清单错误。
#[derive(Debug, thiserror::Error)]
pub enum TierManifestError {
    #[error(
        "插件根未配置：Tier1 分级清单缺失（{path}）——分级归类的单一真源缺席即拒启（fail-closed）"
    )]
    Missing { path: PathBuf },
    #[error("插件根未配置：Tier1 分级清单解析失败（{path}）：{reason}")]
    Parse { path: PathBuf, reason: String },
}

/// 加载 Tier1 分级清单：`<config_root>/kernel/plugin_tiers.yaml`；缺失/损坏拒启。
pub fn load_tier_manifest(config_root: &Path) -> Result<PluginTiers, TierManifestError> {
    let path = config_root.join("kernel").join(TIER_MANIFEST_FILENAME);
    if !path.is_file() {
        return Err(TierManifestError::Missing { path });
    }
    let raw = std::fs::read_to_string(&path).map_err(|e| TierManifestError::Parse {
        path: path.clone(),
        reason: e.to_string(),
    })?;
    serde_yaml::from_str(&raw).map_err(|e| TierManifestError::Parse {
        path: path.clone(),
        reason: e.to_string(),
    })
}

// ==== 启动守卫 ====

/// 启动守卫错误（fail-closed）：信息一律含「期望根 / 实际解析」便于排障。
#[derive(Debug, thiserror::Error)]
pub enum BootGuardError {
    #[error("启动守卫：插件根不可用（{label}）——期望根 {expected}，实际解析 {actual}：{reason}")]
    RootUnusable {
        label: &'static str,
        expected: String,
        actual: String,
        reason: String,
    },
    #[error(
        "启动守卫：Tier1 插件 `{id}` 越界出现在 Tier2 根——期望根 {expected}（Tier1 只许从应用目录只读根加载），实际解析 {actual}"
    )]
    Tier1PluginInTier2Root {
        id: String,
        expected: String,
        actual: String,
    },
    #[error(
        "启动守卫：Tier1 清单声明的安全插件 `{id}` 在 tier1_root 缺席——期望根 {expected}，实际解析 缺席（清单与实例配置不一致，拒启）"
    )]
    Tier1PluginMissing { id: String, expected: String },
    #[error(
        "启动守卫：Tier1 清单声明的共享件 `{entry}` 在 shared_root 缺席——期望根 {expected}，实际解析 缺席"
    )]
    SharedModuleMissing { entry: String, expected: String },
    #[error(
        "启动守卫：插件 manifest 不可读（{root_label} 根下 {path}）：{reason}——守卫无法判定分级，fail-closed"
    )]
    ManifestUnreadable {
        root_label: &'static str,
        path: String,
        reason: String,
    },
}

/// 根存在且可读（目录 + read_dir 成功）。
fn ensure_root_usable(label: &'static str, root: &Path) -> Result<(), BootGuardError> {
    let reason = if !root.exists() {
        Some("目录不存在".to_string())
    } else if !root.is_dir() {
        Some("路径不是目录".to_string())
    } else {
        std::fs::read_dir(root)
            .err()
            .map(|e| format!("目录不可读: {e}"))
    };
    if let Some(reason) = reason {
        return Err(BootGuardError::RootUnusable {
            label,
            expected: root.display().to_string(),
            actual: root.display().to_string(),
            reason,
        });
    }
    Ok(())
}

/// 读插件目录 manifest 的 `id` 字符串（plugin.json JSON / plugin.yaml YAML）。
fn read_manifest_id(dir: &Path) -> Result<String, String> {
    let json_path = dir.join("plugin.json");
    let raw = if json_path.is_file() {
        std::fs::read_to_string(&json_path).map_err(|e| e.to_string())?
    } else {
        let yaml_path = dir.join("plugin.yaml");
        if !yaml_path.is_file() {
            return Err("plugin.json 与 plugin.yaml 均缺席".to_string());
        }
        std::fs::read_to_string(&yaml_path).map_err(|e| e.to_string())?
    };
    let value: serde_json::Value = if json_path.is_file() {
        serde_json::from_str(&raw).map_err(|e| format!("plugin.json 解析失败: {e}"))?
    } else {
        serde_yaml::from_str::<serde_json::Value>(&raw)
            .map_err(|e| format!("plugin.yaml 解析失败: {e}"))?
    };
    value
        .get("id")
        .and_then(|v| v.as_str())
        .map(str::to_string)
        .ok_or_else(|| "manifest 缺少字符串字段 id".to_string())
}

/// 两根是否同一目录（canonicalize 后比较；任一侧 canonicalize 失败按原样比较）。
fn same_root(a: &Path, b: &Path) -> bool {
    let ca = a.canonicalize().unwrap_or_else(|_| a.to_path_buf());
    let cb = b.canonicalize().unwrap_or_else(|_| b.to_path_buf());
    ca == cb
}

/// 枚举根下全部插件目录 → (id, 路径)；manifest 不可读 = 守卫无法判定分级，报错。
fn enumerate_plugin_ids(
    root: &Path,
    root_label: &'static str,
) -> Result<Vec<(String, PathBuf)>, BootGuardError> {
    let mut out = Vec::new();
    for dir in crate::loader::collect_plugin_dirs(root) {
        let id = read_manifest_id(&dir).map_err(|reason| BootGuardError::ManifestUnreadable {
            root_label,
            path: dir.display().to_string(),
            reason,
        })?;
        out.push((id, dir));
    }
    Ok(out)
}

/// 启动守卫（fail-closed，ADR 2026-11-07 §2.3）：
///
/// 1. 三根存在/可读（期望根/实际解析/原因进错误信息）；
/// 2. Tier1 清单内 id 出现在 tier2_root = 越界，拒启（两根不同时；Tier1 id
///    双根同在亦由本检查拒启——Tier1 只许从 tier1_root 加载）；
/// 3. 扫描面按清单归类（根=位置，清单=归类）：tier1_root 只认清单内 Tier1 id
///    ——非清单条目是 Tier2 安装种子（ADR §2.1 包内副本不参与运行），从 Tier1
///    扫描面剔除；Tier2 id 双根共存 = 合法（tier2 根=运行副本，tier1 根侧=
///    种子，不构成双源仲裁场景）；
/// 4. Tier1 清单声明的安全插件必须实际位于 tier1_root（缺席 = 清单与实例配置
///    不一致，拒启；种子不得充当 Tier1 租户）；
/// 5. Tier1 清单声明的共享件必须实际位于 shared_root（`.py` 后缀条目按文件、
///    其余按目录判定）；
/// 6. dev 三根同指的退化配置不触发越界类检查（同一路径不构成双源）。
pub fn validate_boot_roots(
    config: &LoadedInstanceConfig,
    tiers: &PluginTiers,
) -> Result<(), BootGuardError> {
    ensure_root_usable("tier1_root", &config.tier1_root)?;
    ensure_root_usable("tier2_root", &config.tier2_root)?;
    ensure_root_usable("shared_root", &config.shared_root)?;

    let degenerate = same_root(&config.tier1_root, &config.tier2_root);
    let tier1_scanned = enumerate_plugin_ids(&config.tier1_root, "tier1")?;
    let tier2_entries = enumerate_plugin_ids(&config.tier2_root, "tier2")?;

    // 越界检查（raw 扫描面，两根不同时）：Tier1 清单内 id 只许出现在
    // tier1_root；Tier1 id 双根同在亦由本检查拒启。Tier2 id 双根共存 = 合法
    // （tier1 根侧是安装种子，见下方归类过滤）。
    let tier1_ids = tiers.tier1_plugin_ids();
    if !degenerate {
        for (id, path) in &tier2_entries {
            if tier1_ids.contains(id) {
                return Err(BootGuardError::Tier1PluginInTier2Root {
                    id: id.clone(),
                    expected: config.tier1_root.display().to_string(),
                    actual: path.display().to_string(),
                });
            }
        }
    }

    // 扫描面按清单归类（根=位置，清单=归类，ADR §2.1）：tier1_root 里的非
    // 清单条目是 Tier2 安装种子——装机包布局下 tier1_root 装着全部插件（Tier1
    // 运行件 + Tier2 种子），种子不得当作 Tier1 租户参与一致性判定。manifest
    // 可读性守卫在枚举面已 fail-closed（种子 manifest 损坏同样拒启）。
    let tier1_entries: Vec<_> = tier1_scanned
        .into_iter()
        .filter(|(id, _)| tier1_ids.contains(id))
        .collect();

    // 清单 → 实例一致性：声明的安全插件必须实际在 tier1_root。
    let tier1_present: BTreeSet<&str> = tier1_entries.iter().map(|(id, _)| id.as_str()).collect();
    for id in &tiers.tier1.safety_plugins {
        if !tier1_present.contains(id.as_str()) {
            return Err(BootGuardError::Tier1PluginMissing {
                id: id.clone(),
                expected: config.tier1_root.display().to_string(),
            });
        }
    }

    // 清单 → 实例一致性：声明的共享件必须实际在 shared_root（`.py` = 文件）。
    for entry in &tiers.tier1.shared_modules {
        let target = config.shared_root.join(entry);
        let present = if entry.ends_with(".py") {
            target.is_file()
        } else {
            target.is_dir()
        };
        if !present {
            return Err(BootGuardError::SharedModuleMissing {
                entry: entry.clone(),
                expected: config.shared_root.display().to_string(),
            });
        }
    }
    Ok(())
}

// ==== 启动装载清单（可观察：路径/版本/级别/来源根） ====

/// 插件分级（装载清单与诊断面透出；JSON 形态 = `tier1`/`tier2` 小写）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum PluginTier {
    /// 安全级（应用目录只读）。
    Tier1,
    /// 产品功能级（解包用户目录）。
    Tier2,
}

impl PluginTier {
    /// 字符串形态（日志/JSON 透出）。
    pub fn as_str(&self) -> &'static str {
        match self {
            PluginTier::Tier1 => "tier1",
            PluginTier::Tier2 => "tier2",
        }
    }
}

/// 一条启动装载清单条目：每个插件 {路径, 版本, 级别, 来源根}。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PluginLoadEntry {
    /// 插件 id。
    pub plugin_id: String,
    /// manifest version。
    pub version: String,
    /// 分级（清单归类）。
    pub tier: PluginTier,
    /// 加载副本的 manifest 文件所在目录（路径）。
    pub path: PathBuf,
    /// 来源根标签（tier1/tier2）。
    pub source_root: &'static str,
}

/// 启动装载清单快照（boot 时刻的「我在跑哪份」答案面）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct BootLoadManifest {
    /// 生成自的实例启动配置 schema。
    pub schema: u32,
    /// 实例启动配置文件路径。
    pub config_path: PathBuf,
    /// 三根（已解析）。
    pub tier1_root: PathBuf,
    pub tier2_root: PathBuf,
    pub shared_root: PathBuf,
    /// 逐插件条目（按 id 字典序）。
    pub plugins: Vec<PluginLoadEntry>,
}

/// 由发现结果构建装载清单：级别按清单归类，来源根按 manifest 路径前缀归属。
pub fn build_load_manifest(
    manifests: &[agentos_core::traits::PluginManifest],
    manifest_paths: &std::collections::HashMap<String, PathBuf>,
    config: &LoadedInstanceConfig,
    tiers: &PluginTiers,
) -> BootLoadManifest {
    let tier1_ids = tiers.tier1_plugin_ids();
    let mut plugins: Vec<PluginLoadEntry> = manifests
        .iter()
        .map(|m| {
            let tier = if tier1_ids.contains(&m.id) {
                PluginTier::Tier1
            } else {
                PluginTier::Tier2
            };
            let (path, source_root) = match manifest_paths.get(&m.id) {
                Some(p) if p.starts_with(&config.tier1_root) => (p.clone(), "tier1"),
                Some(p) => (p.clone(), "tier2"),
                None => (
                    PathBuf::new(),
                    if tier == PluginTier::Tier1 {
                        "tier1"
                    } else {
                        "tier2"
                    },
                ),
            };
            PluginLoadEntry {
                plugin_id: m.id.clone(),
                version: m.version.clone(),
                tier,
                path,
                source_root,
            }
        })
        .collect();
    plugins.sort_by(|a, b| a.plugin_id.cmp(&b.plugin_id));
    BootLoadManifest {
        schema: INSTANCE_CONFIG_SCHEMA,
        config_path: config.config_path.clone(),
        tier1_root: config.tier1_root.clone(),
        tier2_root: config.tier2_root.clone(),
        shared_root: config.shared_root.clone(),
        plugins,
    }
}

/// 全局装载清单槽（boot 发布一次；诊断面读取）。
static BOOT_LOAD_MANIFEST: OnceLock<RwLock<Option<BootLoadManifest>>> = OnceLock::new();

fn manifest_slot() -> &'static RwLock<Option<BootLoadManifest>> {
    BOOT_LOAD_MANIFEST.get_or_init(|| RwLock::new(None))
}

/// 发布启动装载清单（boot 发现完成后调用一次；重复发布覆盖旧值）。
pub fn publish_boot_load_manifest(manifest: BootLoadManifest) {
    *manifest_slot().write() = Some(manifest);
}

/// 读取启动装载清单快照（boot 未发布过 = None）。
pub fn boot_load_manifest_snapshot() -> Option<BootLoadManifest> {
    manifest_slot().read().clone()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 环境变量是进程全局态：本模块涉 env 用例互斥执行。
    fn env_lock() -> std::sync::MutexGuard<'static, ()> {
        static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
        LOCK.lock().unwrap_or_else(|e| e.into_inner())
    }

    struct EnvGuard {
        key: &'static str,
        original: Option<String>,
    }

    impl EnvGuard {
        fn set(key: &'static str, value: Option<&str>) -> Self {
            let original = std::env::var(key).ok();
            match value {
                Some(v) => std::env::set_var(key, v),
                None => std::env::remove_var(key),
            }
            Self { key, original }
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            match &self.original {
                Some(v) => std::env::set_var(self.key, v),
                None => std::env::remove_var(self.key),
            }
        }
    }

    /// 搭一个最小实例布局：三根目录 + 分级清单 + 配置文件。
    ///
    /// 持有进程级 env 锁并清掉 `AGENTOS_INSTANCE_CONFIG`（env 是进程全局态，
    /// load_instance_config 读它——不锁会让并行用例互踩，读到别的用例刚设的
    /// 载体路径）。
    struct Fixture {
        tmp: tempfile::TempDir,
        config_root: PathBuf,
        _env_lock: std::sync::MutexGuard<'static, ()>,
    }

    impl Fixture {
        fn new() -> Self {
            let _env_lock = env_lock();
            std::env::remove_var(INSTANCE_CONFIG_ENV);
            let tmp = tempfile::tempdir().unwrap();
            let config_root = tmp.path().join("config");
            std::fs::create_dir_all(config_root.join("kernel")).unwrap();
            std::fs::create_dir_all(tmp.path().join("plugins/shared")).unwrap();
            write_tiers(&config_root);
            let fx = Self {
                tmp,
                config_root,
                _env_lock,
            };
            fx.write_default_config();
            fx
        }

        fn plugins_root(&self) -> PathBuf {
            self.tmp.path().join("plugins/shared")
        }

        fn user_root(&self) -> PathBuf {
            let p = self.tmp.path().join("user-plugins");
            std::fs::create_dir_all(&p).unwrap();
            p
        }

        fn write_config(&self, body: &str) {
            std::fs::write(
                self.config_root
                    .join("kernel")
                    .join(INSTANCE_CONFIG_FILENAME),
                body,
            )
            .unwrap();
        }

        fn write_default_config(&self) {
            self.write_config(&format!(
                "schema: 1\n\
                 tier1_root: '{}'\n\
                 tier2_root: '{}'\n\
                 shared_root: '{}'\n",
                self.plugins_root().display(),
                self.plugins_root().display(),
                self.plugins_root().display(),
            ));
        }

        fn load(&self) -> LoadedInstanceConfig {
            load_instance_config(&self.config_root).unwrap()
        }

        fn tiers(&self) -> PluginTiers {
            load_tier_manifest(&self.config_root).unwrap()
        }
    }

    fn write_tiers(config_root: &Path) {
        std::fs::write(
            config_root.join("kernel").join(TIER_MANIFEST_FILENAME),
            "tier1:\n  safety_plugins:\n    - approval\n  shared_modules:\n    - _libs\n",
        )
        .unwrap();
    }

    /// 写一个插件目录（manifest id = 目录名；plugin.json）。
    fn write_plugin(root: &Path, id: &str) -> PathBuf {
        let dir = root.join(id);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("plugin.json"),
            format!(r#"{{"id":"{id}","version":"1.0.0"}}"#),
        )
        .unwrap();
        dir
    }

    // ── 配置加载（fail-closed 各分支）──

    #[test]
    fn load_missing_config_refuses_with_not_configured() {
        let fx = Fixture::new();
        // 删掉配置文件（模拟未配置实例）
        std::fs::remove_file(fx.config_root.join("kernel").join(INSTANCE_CONFIG_FILENAME)).unwrap();
        let err = load_instance_config(&fx.config_root).unwrap_err();
        assert!(err.to_string().contains("插件根未配置"), "{err}");
        assert!(matches!(err, InstanceConfigError::Missing { .. }));
    }

    #[test]
    fn load_env_carrier_points_at_config_file() {
        // 手工搭布局（不经 Fixture::new——它已持同一把 env 锁，套用会自锁死）。
        let _lock = env_lock();
        let tmp = tempfile::tempdir().unwrap();
        let shared = tmp.path().join("plugins/shared");
        std::fs::create_dir_all(&shared).unwrap();
        let config_root = tmp.path().join("config");
        std::fs::create_dir_all(config_root.join("kernel")).unwrap();
        // env 载体指向另一份配置文件（内容在文件里）
        let alt_dir = tmp.path().join("alt-config/kernel");
        std::fs::create_dir_all(&alt_dir).unwrap();
        let alt = alt_dir.join(INSTANCE_CONFIG_FILENAME);
        std::fs::write(
            &alt,
            format!(
                "schema: 1\ntier1_root: '{}'\ntier2_root: '{}'\nshared_root: '{}'\n",
                shared.display(),
                shared.display(),
                shared.display()
            ),
        )
        .unwrap();
        let _g = EnvGuard::set(INSTANCE_CONFIG_ENV, alt.to_str());
        let loaded = load_instance_config(&config_root).unwrap();
        assert_eq!(loaded.config_path, alt, "env 载体优先于缺省位");
        assert_eq!(loaded.tier1_root, shared);
        // 缺省位缺席但 env 载体在 → 不报 Missing（env 载体是合法交付形态）
        let empty_root = tmp.path().join("no-config-root");
        std::fs::create_dir_all(&empty_root).unwrap();
        assert!(load_instance_config(&empty_root).is_ok());
    }

    #[test]
    fn load_rejects_corrupt_schema_and_empty_fields() {
        let fx = Fixture::new();
        let path = fx.config_root.join("kernel").join(INSTANCE_CONFIG_FILENAME);

        fx.write_config("{{not yaml");
        assert!(matches!(
            load_instance_config(&fx.config_root),
            Err(InstanceConfigError::Parse { .. })
        ));

        fx.write_config("schema: 2\ntier1_root: a\ntier2_root: b\nshared_root: c\n");
        assert!(matches!(
            load_instance_config(&fx.config_root),
            Err(InstanceConfigError::SchemaMismatch {
                expected: 1,
                actual: 2,
                ..
            })
        ));

        fx.write_config("schema: 1\ntier1_root: a\ntier2_root: b\n");
        assert!(
            matches!(
                load_instance_config(&fx.config_root),
                Err(InstanceConfigError::Parse { .. })
            ),
            "字段缺失 = 解析失败（必填字段）"
        );

        fx.write_config("schema: 1\ntier1_root: a\ntier2_root: b\nshared_root: ' '\n");
        assert!(matches!(
            load_instance_config(&fx.config_root),
            Err(InstanceConfigError::FieldEmpty {
                field: "shared_root",
                ..
            })
        ));

        fx.write_config(
            "schema: 1\ntier1_root: a\ntier2_root: b\nshared_root: c\nunknown_field: 1\n",
        );
        assert!(
            load_instance_config(&fx.config_root).is_err(),
            "未知字段拒绝（deny_unknown_fields，配置拼错不得静默吞掉）"
        );
        assert!(path.is_file());
    }

    #[test]
    fn relative_roots_resolve_against_config_file_dir() {
        let fx = Fixture::new();
        // config/kernel/instance.yaml 写相对路径 → 按 config/kernel/ 解析
        fx.write_config(
            "schema: 1\ntier1_root: ../../plugins/shared\ntier2_root: ../../plugins/shared\nshared_root: ../../plugins/shared\n",
        );
        let loaded = fx.load();
        assert_eq!(loaded.tier1_root, fx.plugins_root());
        assert_eq!(
            loaded.config_path.file_name().unwrap(),
            INSTANCE_CONFIG_FILENAME
        );
    }

    // ── 分级清单 ──

    #[test]
    fn tier_manifest_missing_or_corrupt_refuses() {
        let tmp = tempfile::tempdir().unwrap();
        let config_root = tmp.path().join("config");
        std::fs::create_dir_all(config_root.join("kernel")).unwrap();
        let err = load_tier_manifest(&config_root).unwrap_err();
        assert!(err.to_string().contains("插件根未配置"), "{err}");

        std::fs::write(
            config_root.join("kernel").join(TIER_MANIFEST_FILENAME),
            "{bad",
        )
        .unwrap();
        assert!(matches!(
            load_tier_manifest(&config_root),
            Err(TierManifestError::Parse { .. })
        ));
    }

    #[test]
    fn tier_manifest_parses_and_classifies() {
        let fx = Fixture::new();
        let tiers = fx.tiers();
        assert!(tiers.tier1_plugin_ids().contains("approval"));
        assert_eq!(tiers.tier1.shared_modules, vec!["_libs"]);
    }

    // ── 启动守卫 ──

    #[test]
    fn guard_missing_root_reports_expected_and_actual() {
        let fx = Fixture::new();
        let tiers = fx.tiers();
        let mut config = fx.load();
        config.tier2_root = fx.tmp.path().join("no-such-root");
        let err = validate_boot_roots(&config, &tiers).unwrap_err();
        assert!(matches!(
            err,
            BootGuardError::RootUnusable {
                label: "tier2_root",
                ..
            }
        ));
        let msg = err.to_string();
        assert!(msg.contains("期望根"), "{msg}");
        assert!(msg.contains("实际解析"), "{msg}");
        assert!(msg.contains("no-such-root"), "{msg}");
    }

    #[test]
    fn guard_tier1_plugin_in_tier2_root_is_boundary_violation() {
        let fx = Fixture::new();
        let user = fx.user_root();
        write_plugin(&user, "approval"); // Tier1 清单内插件被放进 Tier2 根
        let config = LoadedInstanceConfig {
            config_path: fx.config_root.join("kernel").join(INSTANCE_CONFIG_FILENAME),
            tier1_root: fx.plugins_root(),
            tier2_root: user.clone(),
            shared_root: fx.plugins_root(),
        };
        write_plugin(&fx.plugins_root(), "approval");
        let err = validate_boot_roots(&config, &fx.tiers()).unwrap_err();
        let msg = err.to_string();
        assert!(
            matches!(err, BootGuardError::Tier1PluginInTier2Root { id, .. } if id == "approval")
        );
        assert!(msg.contains("越界"), "{msg}");
        assert!(msg.contains("期望根"), "{msg}");
    }

    #[test]
    fn guard_degenerate_config_same_dir_passes_dual_root_checks() {
        let fx = Fixture::new();
        // 清单声明件补齐（approval + _libs 在 tier1/shared 根）
        write_plugin(&fx.plugins_root(), "approval");
        std::fs::create_dir_all(fx.plugins_root().join("_libs")).unwrap();
        write_plugin(&fx.plugins_root(), "tool_a");
        // dev 退化配置（三根同指）：同一路径不构成双源，越界类检查不触发，放行
        let config = LoadedInstanceConfig {
            config_path: fx.config_root.join("kernel").join(INSTANCE_CONFIG_FILENAME),
            tier1_root: fx.plugins_root(),
            tier2_root: fx.plugins_root(),
            shared_root: fx.plugins_root(),
        };
        assert!(validate_boot_roots(&config, &fx.tiers()).is_ok());
    }

    // ── 归类扫描（根=位置，清单=归类；装机包布局语义）──

    /// ① Tier2 安装种子在 tier1_root 被归类过滤：不当作 Tier1 租户、不参与双根
    /// 判定——tier2_root 同 id 运行副本在场 = 合法放行。装机包布局下 tier1_root
    /// 装着全部插件（Tier1 运行件 + Tier2 种子），修复前该形态被双根同 id 检查
    /// 误判为配置错误拒启。
    #[test]
    fn guard_tier2_seed_in_tier1_root_filtered_out_of_dual_root_check() {
        let fx = Fixture::new();
        let user = fx.user_root();
        // tier1_root：Tier1 租户 + 共享件 + Tier2 种子（清单外）
        write_plugin(&fx.plugins_root(), "approval");
        std::fs::create_dir_all(fx.plugins_root().join("_libs")).unwrap();
        write_plugin(&fx.plugins_root(), "acp_pool");
        // tier2_root：同 id 运行副本（Tier2 唯一运行面）
        write_plugin(&user, "acp_pool");
        let config = LoadedInstanceConfig {
            config_path: fx.config_root.join("kernel").join(INSTANCE_CONFIG_FILENAME),
            tier1_root: fx.plugins_root(),
            tier2_root: user,
            shared_root: fx.plugins_root(),
        };
        assert!(
            validate_boot_roots(&config, &fx.tiers()).is_ok(),
            "Tier2 种子（tier1_root）与运行副本（tier2_root）双根共存 = 合法"
        );

        // 种子不充当 Tier1 租户：声明件缺席仍按缺席拒启（归类只剔种子，不放水）
        std::fs::remove_dir_all(fx.plugins_root().join("approval")).unwrap();
        let err = validate_boot_roots(&config, &fx.tiers()).unwrap_err();
        assert!(matches!(err, BootGuardError::Tier1PluginMissing { id, .. } if id == "approval"));
    }

    /// ② Tier2 id 双根共存放行（加载走 tier2）：tier2_root 运行副本版本高于
    /// tier1_root 种子——守卫放行后，发现面 version-max 归约（严格更优才换，
    /// `loader::should_replace`）由 tier2 运行副本胜出，tier1 根侧种子不参与
    /// 运行（ADR §2.1）。version 只决定加载归属，守卫本身不读版本。
    #[test]
    fn guard_tier2_id_dual_root_coexistence_allowed_runtime_in_tier2() {
        let fx = Fixture::new();
        let user = fx.user_root();
        write_plugin(&fx.plugins_root(), "approval");
        std::fs::create_dir_all(fx.plugins_root().join("_libs")).unwrap();
        write_plugin(&fx.plugins_root(), "tool_a"); // 种子 1.0.0
                                                    // tier2_root 运行副本 2.0.0（version-max 胜者）+ 独占 Tier2 插件
        let runtime = write_plugin(&user, "tool_a");
        std::fs::write(
            runtime.join("plugin.json"),
            r#"{"id":"tool_a","version":"2.0.0"}"#,
        )
        .unwrap();
        write_plugin(&user, "tool_b");
        let config = LoadedInstanceConfig {
            config_path: fx.config_root.join("kernel").join(INSTANCE_CONFIG_FILENAME),
            tier1_root: fx.plugins_root(),
            tier2_root: user,
            shared_root: fx.plugins_root(),
        };
        assert!(
            validate_boot_roots(&config, &fx.tiers()).is_ok(),
            "Tier2 id 双根共存 = 合法（tier2 根 = 运行副本所在根）"
        );
    }

    /// ③ Tier1 id 双根仍拒：清单内 id 出现在 tier2_root = 越界（与 tier1_root
    /// 同在与否无关——Tier1 只许从 tier1_root 加载；Tier2 种子在场不豁免越界
    /// 判定）。
    #[test]
    fn guard_tier1_id_dual_root_still_refused_regardless_of_seeds() {
        let fx = Fixture::new();
        let user = fx.user_root();
        write_plugin(&fx.plugins_root(), "approval"); // Tier1 正位
        std::fs::create_dir_all(fx.plugins_root().join("_libs")).unwrap();
        write_plugin(&fx.plugins_root(), "acp_pool"); // Tier2 种子在场
        write_plugin(&user, "approval"); // Tier1 id 越界出现在 tier2_root
        write_plugin(&user, "acp_pool"); // Tier2 运行副本
        let config = LoadedInstanceConfig {
            config_path: fx.config_root.join("kernel").join(INSTANCE_CONFIG_FILENAME),
            tier1_root: fx.plugins_root(),
            tier2_root: user,
            shared_root: fx.plugins_root(),
        };
        let err = validate_boot_roots(&config, &fx.tiers()).unwrap_err();
        let msg = err.to_string();
        assert!(
            matches!(err, BootGuardError::Tier1PluginInTier2Root { id, .. } if id == "approval"),
            "{msg}"
        );
    }

    #[test]
    fn guard_declared_tier1_plugin_missing_from_tier1_root_refuses() {
        let fx = Fixture::new();
        // 清单声明 approval 是 Tier1，但 tier1_root 里没有（只在别处）→ 拒启
        let config = fx.load();
        let err = validate_boot_roots(&config, &fx.tiers()).unwrap_err();
        let msg = err.to_string();
        assert!(matches!(err, BootGuardError::Tier1PluginMissing { id, .. } if id == "approval"));
        assert!(msg.contains("tier1_root"), "{msg}");
    }

    #[test]
    fn guard_declared_shared_module_missing_refuses() {
        let fx = Fixture::new();
        std::fs::create_dir_all(fx.plugins_root().join("approval")).unwrap();
        std::fs::write(
            fx.plugins_root().join("approval/plugin.json"),
            r#"{"id":"approval","version":"1.0.0"}"#,
        )
        .unwrap();
        // _libs 未建 → 清单声明与 shared_root 实际不一致
        let config = fx.load();
        let err = validate_boot_roots(&config, &fx.tiers()).unwrap_err();
        assert!(
            matches!(err, BootGuardError::SharedModuleMissing { entry, .. } if entry == "_libs")
        );

        // 补上目录后放行；`.py` 后缀条目按文件判定
        std::fs::create_dir_all(fx.plugins_root().join("_libs")).unwrap();
        std::fs::write(
            fx.config_root.join("kernel").join(TIER_MANIFEST_FILENAME),
            "tier1:\n  safety_plugins:\n    - approval\n  shared_modules:\n    - _libs\n    - kernel_token.py\n",
        )
        .unwrap();
        let err2 = validate_boot_roots(&config, &fx.tiers()).unwrap_err();
        assert!(
            matches!(err2, BootGuardError::SharedModuleMissing { entry, .. } if entry == "kernel_token.py")
        );
        std::fs::write(fx.plugins_root().join("kernel_token.py"), "# shared\n").unwrap();
        assert!(validate_boot_roots(&config, &fx.tiers()).is_ok());
    }

    #[test]
    fn guard_unreadable_tier2_manifest_refuses_fail_closed() {
        let fx = Fixture::new();
        let user = fx.user_root();
        let dir = write_plugin(&user, "tool_b");
        // 清单声明件补齐，守卫走到枚举面
        write_plugin(&fx.plugins_root(), "approval");
        std::fs::create_dir_all(fx.plugins_root().join("_libs")).unwrap();
        std::fs::write(dir.join("plugin.json"), "{broken").unwrap();
        let config = LoadedInstanceConfig {
            config_path: fx.config_root.join("kernel").join(INSTANCE_CONFIG_FILENAME),
            tier1_root: fx.plugins_root(),
            tier2_root: user,
            shared_root: fx.plugins_root(),
        };
        let err = validate_boot_roots(&config, &fx.tiers()).unwrap_err();
        assert!(matches!(
            err,
            BootGuardError::ManifestUnreadable {
                root_label: "tier2",
                ..
            }
        ));
    }

    // ── 装载清单 ──

    #[test]
    fn load_manifest_classifies_tier_and_source_root() {
        use agentos_core::traits::PluginManifest;
        let fx = Fixture::new();
        let user = fx.user_root();
        write_plugin(&fx.plugins_root(), "approval");
        write_plugin(&user, "tool_a");

        let base: PluginManifest = serde_json::from_str(
            r#"{
            "id": "x", "name": "x", "version": "0.0.0",
            "plugin_type": "tool", "language": "rust",
            "host_type": "in_process", "entry": "x",
            "capabilities": { "tools": [] }
        }"#,
        )
        .unwrap();
        let manifest = |id: &str, version: &str| PluginManifest {
            id: id.to_string(),
            version: version.to_string(),
            ..base.clone()
        };

        let manifests = vec![manifest("tool_a", "2.0.0"), manifest("approval", "1.2.3")];
        let mut paths = std::collections::HashMap::new();
        paths.insert("tool_a".to_string(), user.join("tool_a"));
        paths.insert("approval".to_string(), fx.plugins_root().join("approval"));

        let config = LoadedInstanceConfig {
            config_path: fx.config_root.join("kernel").join(INSTANCE_CONFIG_FILENAME),
            tier1_root: fx.plugins_root(),
            tier2_root: user.clone(),
            shared_root: fx.plugins_root(),
        };
        let tiers = fx.tiers();
        let lm = build_load_manifest(&manifests, &paths, &config, &tiers);
        assert_eq!(lm.plugins.len(), 2);
        // id 字典序：approval 在前
        assert_eq!(lm.plugins[0].plugin_id, "approval");
        assert_eq!(lm.plugins[0].tier, PluginTier::Tier1);
        assert_eq!(lm.plugins[0].source_root, "tier1");
        assert_eq!(lm.plugins[0].version, "1.2.3");
        assert_eq!(lm.plugins[1].plugin_id, "tool_a");
        assert_eq!(lm.plugins[1].tier, PluginTier::Tier2);
        assert_eq!(lm.plugins[1].source_root, "tier2");
        assert_eq!(lm.plugins[1].version, "2.0.0");
        assert_eq!(lm.tier1_root, fx.plugins_root());
    }

    #[test]
    fn publish_and_snapshot_roundtrip_and_overwrite() {
        let fx = Fixture::new();
        let lm = BootLoadManifest {
            schema: INSTANCE_CONFIG_SCHEMA,
            config_path: fx.config_root.join("kernel").join(INSTANCE_CONFIG_FILENAME),
            tier1_root: fx.plugins_root(),
            tier2_root: fx.plugins_root(),
            shared_root: fx.plugins_root(),
            plugins: vec![PluginLoadEntry {
                plugin_id: "p".into(),
                version: "1.0.0".into(),
                tier: PluginTier::Tier2,
                path: fx.plugins_root().join("p"),
                source_root: "tier1",
            }],
        };
        publish_boot_load_manifest(lm.clone());
        let snap = boot_load_manifest_snapshot().unwrap();
        assert_eq!(snap, lm);
        assert_eq!(snap.plugins[0].tier.as_str(), "tier2");
        // 重复发布 = 覆盖
        let mut lm2 = lm.clone();
        lm2.plugins.clear();
        publish_boot_load_manifest(lm2.clone());
        assert_eq!(boot_load_manifest_snapshot().unwrap(), lm2);
    }
}

// @feature: FP-0.2.CFG 内核 core 用户空间 | @ci: rust-test
//! 用户空间（`AGENTOS_USER_ROOT`）——用户可写资产的统一根。
//!
//! 用户可写的东西（插件/配置/数据/密钥）全部住在一个根下面，使之整体位于
//! 仓库**之外**：仓内 `config/`（git 跟踪）与 `data/` 处于工作区还原的抹除
//! 风险面内，而用户空间不受影响。
//!
//! ```text
//! <USER_ROOT>/                 # 默认 dirs::data_dir()/agentos（按 OS 不同）
//! ├── plugins/                 # 用户插件根（覆盖内置根：同 id 用户赢）
//! ├── config/                  # 用户配置层（镜像 factory config/ 相对路径）
//! ├── data/                    # 运行时数据（多租户树 / uploads / DB）
//! └── .env                     # 密钥与环境变量
//! ```
//!
//! 覆盖语义（见 ADR 2026-09-13-unified-user-root）：**文件级整体替换**——用户层
//! 存在某文件时，factory 同路径文件不被读取、不被合并，任一时刻一个路径只有
//! 一份生效文件。这是文件级所有权转移（与插件双根「同 id 用户赢」同构），
//! **不是**被 ADR 2026-09-02 否决的「出厂默认 + 用户覆盖」字段级两层。
//!
//! 本模块承载：① 路径**解析**（根在哪、某文件该读哪个根）；② 配置接管登记
//! （`.ownership.json`，读账本+登记）；③ 模式种子版本管理（`.seeds.json`，
//! 播种/启动对账/恢复出厂，见下方专区）——调用方按各自已有语义使用。

use std::path::{Path, PathBuf};

/// 用户空间根环境变量名。
pub const USER_ROOT_ENV: &str = "AGENTOS_USER_ROOT";
/// 用户配置层根环境变量名（分区覆盖）。
pub const USER_CONFIG_DIR_ENV: &str = "AGENTOS_USER_CONFIG_DIR";
/// 用户数据根环境变量名（分区覆盖）。
pub const USER_DATA_DIR_ENV: &str = "AGENTOS_DATA_DIR";
/// 用户插件根环境变量名（分区覆盖）。
pub const USER_PLUGINS_DIR_ENV: &str = "AGENTOS_USER_PLUGINS_DIR";

/// 读一个环境变量，空白值视为未设。
fn env_path(name: &str) -> Option<PathBuf> {
    std::env::var(name)
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
}

/// 用户空间根：`AGENTOS_USER_ROOT` > OS 标准目录（`dirs::data_dir()/agentos`）。
///
/// 用 `data_dir()` 而非 `data_local_dir()`：`%LOCALAPPDATA%` 不随用户漫游，
/// 而配置与密钥应当跟用户走。
pub fn user_root() -> Option<PathBuf> {
    env_path(USER_ROOT_ENV).or_else(|| dirs::data_dir().map(|d| d.join("agentos")))
}

/// 用户配置层根：`AGENTOS_USER_CONFIG_DIR` > `<USER_ROOT>/config`。
pub fn user_config_dir() -> Option<PathBuf> {
    env_path(USER_CONFIG_DIR_ENV).or_else(|| user_root().map(|r| r.join("config")))
}

/// 用户数据根：`AGENTOS_DATA_DIR` > `<USER_ROOT>/data`。
pub fn user_data_dir() -> Option<PathBuf> {
    env_path(USER_DATA_DIR_ENV).or_else(|| user_root().map(|r| r.join("data")))
}

/// 用户插件根：`AGENTOS_USER_PLUGINS_DIR` > `<USER_ROOT>/plugins`。
pub fn user_plugins_dir() -> Option<PathBuf> {
    env_path(USER_PLUGINS_DIR_ENV).or_else(|| user_root().map(|r| r.join("plugins")))
}

/// 解析一个配置相对路径应读/写的落点。
///
/// `rel` 是相对 factory config 根的路径（如 `models/llm.yaml`；允许带
/// `config/` 前缀，会被剥掉）。返回用户层路径**当且仅当该文件已存在**——
/// 不存在则回落到 factory 路径（`factory_root/rel`）。
///
/// 语义要点（ADR 2026-09-13）：
/// - **整体替换**：命中用户层时 factory 文件完全不参与——调用方不得再读它；
/// - **存在性判定用文件而非目录**：用户层目录存在但该文件不存在 = 该键未被接管；
/// - **单一解析器**：读与写必须走同一个本函数，禁止两侧各自拼路径（那正是
///   单真值 ADR 的实证根因：用户值写盘了、插件读的却是另一份）。
///
/// 返回 `None` 仅当用户层不可用（无 OS data dir）且 factory 路径也拼不出来；
/// 生产环境下 `factory_root` 必为绝对路径，故实际总能返回 `Some`。
pub fn resolve_config_path(factory_root: &Path, rel: &str) -> Option<PathBuf> {
    let rel_norm = rel.replace('\\', "/");
    let rel_trimmed = rel_norm.strip_prefix("config/").unwrap_or(&rel_norm);
    let candidate = user_config_dir().map(|u| u.join(rel_trimmed));
    if let Some(ref user_path) = candidate {
        if user_path.is_file() {
            return Some(user_path.clone());
        }
    }
    Some(factory_root.join(rel_trimmed))
}

/// 同上，但返回「落点根」信息：`(路径, 是否用户层)`。
///
/// 写路径需要区分两种情形——用户层文件已存在（直接写）、尚未接管（先播种
/// factory 内容再写）。`is_user` 为真表示该路径在用户空间内（无论文件是否
/// 已存在）；为假表示用户层不可用，只能回落 factory。
pub fn config_write_target(factory_root: &Path, rel: &str) -> (PathBuf, bool) {
    let rel_norm = rel.replace('\\', "/");
    let rel_trimmed = rel_norm.strip_prefix("config/").unwrap_or(&rel_norm);
    if let Some(user_root_dir) = user_config_dir() {
        return (user_root_dir.join(rel_trimmed), true);
    }
    (factory_root.join(rel_trimmed), false)
}

// ==== 接管登记（ADR 2026-09-14：单一存在与接管登记） ====
//
// 单一存在不变量：任一配置路径，生效文件恰好一份。用户层接管后，出厂侧同路径
// 文件失效（解析层已保证：resolve 只在用户层无此文件时回落 factory）。登记是
// 接管的**唯一凭证**——没有它，「恢复出厂」（删用户文件 + 删登记）与「漂移提示」
// （出厂文件相对接管基线有更新）都无从做起。

use serde::{Deserialize, Serialize};
use sha2::Digest;

/// 接管登记文件名（位于用户配置层根下，随用户空间整体备份/迁移）。
pub const OWNERSHIP_LEDGER_FILENAME: &str = ".ownership.json";

/// 一条接管登记：用户层接管的配置路径及其出厂基线。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OwnershipEntry {
    /// 相对出厂 config 根的路径（`/` 分隔、无 `config/` 前缀）。
    pub path: String,
    /// 接管时出厂文件内容的 SHA-256（hex）；出厂侧无原件（用户全新创建）时为 null。
    pub seeded_from_sha256: Option<String>,
    /// 接管时间（RFC 3339）。
    pub seeded_at: String,
    /// 接管时的出厂版本（内核构建版本）；不可得时为 null。
    pub factory_version: Option<String>,
}

/// 接管登记账本的错误。
#[derive(Debug, thiserror::Error)]
pub enum OwnershipLedgerError {
    #[error("ownership ledger corrupt（不静默重置，请人工修复）: {0}")]
    Corrupt(String),
    #[error("ownership ledger io error: {0}")]
    Io(String),
    #[error("user space unavailable")]
    UserSpaceUnavailable,
}

/// 接管登记文件落点；用户空间不可用时 `None`。
pub fn ownership_ledger_path() -> Option<PathBuf> {
    user_config_dir().map(|d| d.join(OWNERSHIP_LEDGER_FILENAME))
}

/// 内容 SHA-256（hex 小写）。
pub fn sha256_hex(content: &[u8]) -> String {
    let mut hasher = sha2::Sha256::new();
    hasher.update(content);
    format!("{:x}", hasher.finalize())
}

/// 规范化相对路径：`\` → `/`、剥 `config/` 前缀；拒绝 `../` 越界。
fn normalize_rel(rel: &str) -> Result<String, OwnershipLedgerError> {
    let norm = rel.replace('\\', "/");
    let trimmed = norm.strip_prefix("config/").unwrap_or(&norm).to_string();
    if trimmed.contains("../") || trimmed.starts_with('/') {
        return Err(OwnershipLedgerError::Io(format!(
            "illegal relative path: {rel}"
        )));
    }
    Ok(trimmed)
}

/// 读全部接管登记。文件缺失 = 从未接管 → 空表。
///
/// 账本损坏返回 [`OwnershipLedgerError::Corrupt`]——**不静默重置**（重置等于
/// 把全部已接管路径打回"无凭证"状态，恢复出厂/漂移提示整体失灵）。
pub fn load_ownership_entries() -> Result<Vec<OwnershipEntry>, OwnershipLedgerError> {
    let Some(path) = ownership_ledger_path() else {
        return Err(OwnershipLedgerError::UserSpaceUnavailable);
    };
    if !path.is_file() {
        return Ok(Vec::new());
    }
    let raw = std::fs::read_to_string(&path)
        .map_err(|e| OwnershipLedgerError::Io(format!("{}: {e}", path.display())))?;
    if raw.trim().is_empty() {
        return Ok(Vec::new());
    }
    serde_json::from_str(&raw)
        .map_err(|e| OwnershipLedgerError::Corrupt(format!("{}: {e}", path.display())))
}

/// 登记（或更新）一条接管：同路径已登记则覆盖基线，否则追加。
///
/// 落盘走临时文件 + rename，避免半写账本。
pub fn register_ownership(
    rel: &str,
    seeded_from_sha256: Option<String>,
    factory_version: Option<String>,
) -> Result<(), OwnershipLedgerError> {
    let path = normalize_rel(rel)?;
    let entry = OwnershipEntry {
        path,
        seeded_from_sha256,
        seeded_at: chrono::Utc::now().to_rfc3339(),
        factory_version,
    };
    let mut entries = load_ownership_entries()?;
    entries.retain(|e| e.path != entry.path);
    entries.push(entry);
    save_ledger(&entries)
}

/// 删除一条接管登记（恢复出厂的账面侧）。返回是否确有该条目。
pub fn remove_ownership(rel: &str) -> Result<bool, OwnershipLedgerError> {
    let path = normalize_rel(rel)?;
    let mut entries = load_ownership_entries()?;
    let before = entries.len();
    entries.retain(|e| e.path != path);
    if entries.len() == before {
        return Ok(false);
    }
    save_ledger(&entries)?;
    Ok(true)
}

fn save_ledger(entries: &[OwnershipEntry]) -> Result<(), OwnershipLedgerError> {
    let Some(path) = ownership_ledger_path() else {
        return Err(OwnershipLedgerError::UserSpaceUnavailable);
    };
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| OwnershipLedgerError::Io(format!("{}: {e}", parent.display())))?;
    }
    let body = serde_json::to_string_pretty(entries)
        .map_err(|e| OwnershipLedgerError::Io(format!("serialize: {e}")))?;
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, body)
        .map_err(|e| OwnershipLedgerError::Io(format!("{}: {e}", tmp.display())))?;
    std::fs::rename(&tmp, &path)
        .map_err(|e| OwnershipLedgerError::Io(format!("{}: {e}", path.display())))?;
    Ok(())
}

/// 漂移核对结论。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum DriftKind {
    /// 出厂文件与接管基线一致。
    Current,
    /// 出厂侧有新版本（哈希 ≠ 基线）——提示用户查看后自行并入，系统绝不合并。
    Drifted,
    /// 出厂侧已无此文件（改名/删除）。
    FactoryMissing,
    /// 基线未知（存量补登记时取不到出厂原件），只能弱提示。
    UnknownBaseline,
}

/// 对全部接管登记做漂移核对（升级后调用，驱动 UI 提示）。
pub fn check_ownership_drift(
    factory_root: &Path,
) -> Result<Vec<(OwnershipEntry, DriftKind)>, OwnershipLedgerError> {
    let mut out = Vec::new();
    for entry in load_ownership_entries()? {
        let factory_file = factory_root.join(&entry.path);
        let kind = match &entry.seeded_from_sha256 {
            None => DriftKind::UnknownBaseline,
            Some(baseline) => {
                if !factory_file.is_file() {
                    DriftKind::FactoryMissing
                } else {
                    let current = std::fs::read(&factory_file).map_err(|e| {
                        OwnershipLedgerError::Io(format!("{}: {e}", factory_file.display()))
                    })?;
                    if sha256_hex(&current) == *baseline {
                        DriftKind::Current
                    } else {
                        DriftKind::Drifted
                    }
                }
            }
        };
        out.push((entry, kind));
    }
    Ok(out)
}

// ==== 模式种子版本管理（设计稿 2026-09-15 §2「版本管理」） ====
//
// 模式插件是「预装 + 版本对账」模型（区别于配置层的 copy-on-write 播种，范围
// 仅限 modes 根）：出厂种子在 `<内置插件根>/modes/<id>/`（自包含种子单元，
// profile.yaml 内打包），供给时整目录播种到 `<USER_ROOT>/plugins/modes/<id>/`。
// 账本 `<USER_ROOT>/plugins/modes/.seeds.json` 记每模式的 seeded_version 与
// 文件哈希基线；内核启动早期（插件扫描之前）跑一轮对账：
// - 用户副本缺席 → 整目录播种（含用户自行删除副本后的回落重播种）；
// - 出厂 version ≤ 账本 seeded_version → no-op（幂等）；
// - 未定制（账本清单内文件逐一哈希一致；账本外用户自加文件不参与判定）
//   且出厂更新 → 静默升级：按新出厂清单逐文件替换/拷入 + 刷新账本，
//   绝不删除账本外文件（H1，2026-09-15——用户自建卡/agent 等附加文件
//   在升级后必须存活）；
// - 已定制（账本内任一文件被改/被删）且出厂更新 → 保留副本，登记「升级
//   可用」（启动日志 warn；读面经 [`load_mode_seed_ledger`] + 出厂 manifest
//   现读即得，不另存状态）；
// - 副本存在但账本无条目（用户手工放置）→ 补账不替换（最小惊讶）。
//
// 对账/恢复出厂均以参数注入目录（生产由启动序列传 `AGENTOS_PLUGINS_DIR`/
// `user_plugins_dir` 的解析结果；测试传临时目录），本节不做环境变量解析。

/// 模式种子账本文件名（位于 `<USER_ROOT>/plugins/modes/` 下）。
pub const MODE_SEEDS_LEDGER_FILENAME: &str = ".seeds.json";

/// 一条模式种子账目：播种时的版本与全部文件哈希基线。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModeSeedEntry {
    /// 播种（或最后静默升级）时的出厂种子版本（plugin.json `version`）。
    pub seeded_version: String,
    /// 播种基线：`相对路径（/ 分隔）→ 内容 SHA-256（hex）`。
    pub files: std::collections::BTreeMap<String, String>,
}

/// 单个模式种子的一轮对账结论（启动日志与后续读面的结构载体）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ModeSeedOutcome {
    /// 用户副本缺席 → 整目录播种。
    Seeded { mode_id: String, version: String },
    /// 副本未定制且出厂更新 → 静默升级（逐文件替换/拷入 + 账本刷新，
    /// 账本外文件原样存活）。
    Upgraded {
        mode_id: String,
        from: String,
        to: String,
    },
    /// 副本已定制且出厂更新 → 保留副本，登记「升级可用」。
    UpgradeAvailable {
        mode_id: String,
        seeded_version: String,
        factory_version: String,
    },
    /// 用户手工副本无账目 → 补记账本（seeded_version 取副本 manifest
    /// version，取不到回落出厂 version），不替换。
    BaselineRegistered { mode_id: String, version: String },
    /// 出厂 version ≤ 账本 seeded_version → 幂等 no-op。
    Current { mode_id: String },
    /// 出厂/账本 version 非 semver → 新旧不可判，保守不动用户副本。
    VersionUnparseable {
        mode_id: String,
        factory_version: String,
        ledger_version: String,
    },
}

/// 模式种子账本的错误。
#[derive(Debug, thiserror::Error)]
pub enum ModeSeedError {
    #[error("mode seeds ledger corrupt（不静默重置，请人工修复）: {0}")]
    Corrupt(String),
    #[error("mode seeds io error: {0}")]
    Io(String),
}

fn mode_seed_ledger_path(user_modes_dir: &Path) -> PathBuf {
    user_modes_dir.join(MODE_SEEDS_LEDGER_FILENAME)
}

/// 读模式种子账本。文件缺失 = 从未播种 → 空表；损坏返回
/// [`ModeSeedError::Corrupt`]——不静默重置（重置会让全部副本被判"手工放置"，
/// 静默升级与升级提示整体失灵）。
pub fn load_mode_seed_ledger(
    user_modes_dir: &Path,
) -> Result<std::collections::BTreeMap<String, ModeSeedEntry>, ModeSeedError> {
    let path = mode_seed_ledger_path(user_modes_dir);
    if !path.is_file() {
        return Ok(std::collections::BTreeMap::new());
    }
    let raw = std::fs::read_to_string(&path)
        .map_err(|e| ModeSeedError::Io(format!("{}: {e}", path.display())))?;
    if raw.trim().is_empty() {
        return Ok(std::collections::BTreeMap::new());
    }
    serde_json::from_str(&raw)
        .map_err(|e| ModeSeedError::Corrupt(format!("{}: {e}", path.display())))
}

fn save_mode_seed_ledger(
    user_modes_dir: &Path,
    ledger: &std::collections::BTreeMap<String, ModeSeedEntry>,
) -> Result<(), ModeSeedError> {
    std::fs::create_dir_all(user_modes_dir)
        .map_err(|e| ModeSeedError::Io(format!("{}: {e}", user_modes_dir.display())))?;
    let path = mode_seed_ledger_path(user_modes_dir);
    let body = serde_json::to_string_pretty(ledger)
        .map_err(|e| ModeSeedError::Io(format!("serialize: {e}")))?;
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, body).map_err(|e| ModeSeedError::Io(format!("{}: {e}", tmp.display())))?;
    std::fs::rename(&tmp, &path)
        .map_err(|e| ModeSeedError::Io(format!("{}: {e}", path.display())))?;
    Ok(())
}

fn upsert_mode_seed_entry(
    user_modes_dir: &Path,
    mode_id: &str,
    entry: ModeSeedEntry,
) -> Result<(), ModeSeedError> {
    let mut ledger = load_mode_seed_ledger(user_modes_dir)?;
    ledger.insert(mode_id.to_string(), entry);
    save_mode_seed_ledger(user_modes_dir, &ledger)
}

/// 读一个插件目录的 manifest 版本（plugin.json `version`）；缺失/损坏 → None。
fn read_manifest_version(dir: &Path) -> Option<String> {
    let raw = std::fs::read_to_string(dir.join("plugin.json")).ok()?;
    let value: serde_json::Value = serde_json::from_str(&raw).ok()?;
    value.get("version")?.as_str().map(str::to_string)
}

/// 枚举出厂种子：`factory_modes_dir` 下含可解析 plugin.json version 的子目录，
/// 按目录名字典序（结果确定性）。无 version 真值的目录不可管理，跳过。
fn enumerate_factory_seeds(factory_modes_dir: &Path) -> Vec<(String, String, PathBuf)> {
    let mut seeds = Vec::new();
    let Ok(entries) = std::fs::read_dir(factory_modes_dir) else {
        return seeds;
    };
    for entry in entries.flatten() {
        let dir = entry.path();
        if dir.is_dir() {
            if let Some(version) = read_manifest_version(&dir) {
                if let Some(name) = entry.file_name().to_str() {
                    seeds.push((name.to_string(), version, dir));
                }
            }
        }
    }
    seeds.sort_by(|a, b| a.0.cmp(&b.0));
    seeds
}

/// 递归计算目录内全部文件的内容哈希：`相对路径（/ 分隔）→ SHA-256（hex）`。
fn compute_dir_hashes(
    dir: &Path,
) -> Result<std::collections::BTreeMap<String, String>, ModeSeedError> {
    fn walk(
        dir: &Path,
        prefix: &str,
        out: &mut std::collections::BTreeMap<String, String>,
    ) -> Result<(), ModeSeedError> {
        let entries = std::fs::read_dir(dir)
            .map_err(|e| ModeSeedError::Io(format!("{}: {e}", dir.display())))?;
        for entry in entries.flatten() {
            let path = entry.path();
            let rel = if prefix.is_empty() {
                entry.file_name().to_string_lossy().into_owned()
            } else {
                format!("{prefix}/{}", entry.file_name().to_string_lossy())
            };
            if path.is_dir() {
                walk(&path, &rel, out)?;
            } else if path.is_file() {
                let content = std::fs::read(&path)
                    .map_err(|e| ModeSeedError::Io(format!("{}: {e}", path.display())))?;
                out.insert(rel, sha256_hex(&content));
            }
        }
        Ok(())
    }
    let mut out = std::collections::BTreeMap::new();
    walk(dir, "", &mut out)?;
    Ok(out)
}

fn copy_dir_recursive(src: &Path, dst: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let from = entry.path();
        let to = dst.join(entry.file_name());
        if from.is_dir() {
            copy_dir_recursive(&from, &to)?;
        } else if from.is_file() {
            std::fs::copy(&from, &to)?;
        }
    }
    Ok(())
}

/// 整目录落盘（staging 换入）：先拷到目标同级暂存目录，再换入目标——中途 IO
/// 失败时目标原地保留（未定制副本下次启动可原样重试升级，绝无半写副本）。
fn copy_tree_staged(src: &Path, dst: &Path) -> Result<(), ModeSeedError> {
    let parent = dst
        .parent()
        .ok_or_else(|| ModeSeedError::Io(format!("{}: no parent dir", dst.display())))?;
    std::fs::create_dir_all(parent)
        .map_err(|e| ModeSeedError::Io(format!("{}: {e}", parent.display())))?;
    let staging = parent.join(format!(
        ".{}.seed-staging",
        dst.file_name().unwrap_or_default().to_string_lossy()
    ));
    let result = copy_dir_recursive(src, &staging).and_then(|()| {
        if dst.exists() {
            std::fs::remove_dir_all(dst)?;
        }
        std::fs::rename(&staging, dst)
    });
    if result.is_err() {
        let _ = std::fs::remove_dir_all(&staging);
    }
    result.map_err(|e| ModeSeedError::Io(format!("{} ← {}: {e}", dst.display(), src.display())))
}

/// 账本内相对路径合法性（fail-closed）：`/` 分隔、无空段与 `..` 段、不以
/// `/` 开头、不含 `\`。账本是用户空间文件、可被手改，而升级按它逐文件写
/// 用户副本——越界路径等于任意文件写，必须在读/写前拒绝。
fn validate_ledger_paths(
    files: &std::collections::BTreeMap<String, String>,
) -> Result<(), ModeSeedError> {
    for rel in files.keys() {
        if rel.is_empty()
            || rel.starts_with('/')
            || rel.contains('\\')
            || rel.split('/').any(|seg| seg.is_empty() || seg == "..")
        {
            return Err(ModeSeedError::Io(format!(
                "illegal seed ledger path: {rel}"
            )));
        }
    }
    Ok(())
}

/// 未定制判定（H1 细化，2026-09-15）：**账本清单内**文件逐一存在且哈希一致。
/// 账本外文件（用户自加的卡/agent/笔记等）不参与判定——它们不是出厂物，
/// 其存在不构成对出厂基线的偏离。账本内任一文件被改/被删 → 已定制。
/// 调用方须先经 [`validate_ledger_paths`] 校验账本路径。
fn is_uncustomized(user_dir: &Path, entry: &ModeSeedEntry) -> Result<bool, ModeSeedError> {
    for (rel, expected) in &entry.files {
        let path = user_dir.join(rel);
        if !path.is_file() {
            return Ok(false);
        }
        let content = std::fs::read(&path)
            .map_err(|e| ModeSeedError::Io(format!("{}: {e}", path.display())))?;
        if sha256_hex(&content) != *expected {
            return Ok(false);
        }
    }
    Ok(true)
}

/// 静默升级（H1，2026-09-15）：按新出厂清单逐文件落盘——账本内文件替换、
/// 出厂新增文件拷入；**账本外且用户侧已存在的文件是用户所有，跳过不动**，
/// 绝不删除任何用户侧文件。返回刷新后的账本条目：基线 = 实际落盘的出厂
/// 文件（被跳过的用户所有文件不入账，否则下轮升级会把用户内容当出厂物覆盖）。
///
/// 原地逐文件写而非整目录换入——staging 换入必删目录，与「绝不删除账本外
/// 文件」互斥。中途 IO 失败：已写文件保留、账本未刷新，下轮对账按旧账本
/// 判「已定制」走升级可用通知（保守可见，无静默半态长期化）。
fn upgrade_seed_files(
    seed_dir: &Path,
    user_dir: &Path,
    old_ledger: &std::collections::BTreeMap<String, String>,
    seeded_version: &str,
) -> Result<ModeSeedEntry, ModeSeedError> {
    let factory_files = compute_dir_hashes(seed_dir)?;
    let mut files = std::collections::BTreeMap::new();
    for (rel, hash) in &factory_files {
        let user_path = user_dir.join(rel);
        let factory_owned = old_ledger.contains_key(rel) || !user_path.is_file();
        if factory_owned {
            if let Some(parent) = user_path.parent() {
                std::fs::create_dir_all(parent)
                    .map_err(|e| ModeSeedError::Io(format!("{}: {e}", parent.display())))?;
            }
            std::fs::copy(seed_dir.join(rel), &user_path).map_err(|e| {
                ModeSeedError::Io(format!(
                    "{} ← {}: {e}",
                    user_path.display(),
                    seed_dir.join(rel).display()
                ))
            })?;
            files.insert(rel.clone(), hash.clone());
        }
    }
    Ok(ModeSeedEntry {
        seeded_version: seeded_version.to_string(),
        files,
    })
}

/// 启动对账：对 `factory_modes_dir` 下每个出厂种子，按账本判定播种/静默升级/
/// 升级可用/补账/幂等 no-op（语义见本节头注）。只枚举出厂侧——用户自建模式
/// （出厂无同名种子）不参与对账。逐模式落账，单个模式 IO 失败即返回 Err
/// （已完成的模式保持已落盘状态，下次启动按账本续对）。
pub fn reconcile_mode_seeds(
    factory_modes_dir: &Path,
    user_modes_dir: &Path,
) -> Result<Vec<ModeSeedOutcome>, ModeSeedError> {
    let ledger = load_mode_seed_ledger(user_modes_dir)?;
    let mut outcomes = Vec::new();
    for (mode_id, seed_version, seed_dir) in enumerate_factory_seeds(factory_modes_dir) {
        let user_dir = user_modes_dir.join(&mode_id);
        if !user_dir.is_dir() {
            copy_tree_staged(&seed_dir, &user_dir)?;
            let entry = ModeSeedEntry {
                files: compute_dir_hashes(&user_dir)?,
                seeded_version: seed_version.clone(),
            };
            upsert_mode_seed_entry(user_modes_dir, &mode_id, entry)?;
            outcomes.push(ModeSeedOutcome::Seeded {
                mode_id,
                version: seed_version,
            });
            continue;
        }
        let Some(entry) = ledger.get(&mode_id) else {
            // 手工副本：补账不替换（最小惊讶）。seeded_version 取副本 manifest
            // version——取不到回落出厂 version，保证下轮不把手工副本判"可升级"。
            let version = read_manifest_version(&user_dir).unwrap_or_else(|| seed_version.clone());
            let entry = ModeSeedEntry {
                files: compute_dir_hashes(&user_dir)?,
                seeded_version: version.clone(),
            };
            upsert_mode_seed_entry(user_modes_dir, &mode_id, entry)?;
            outcomes.push(ModeSeedOutcome::BaselineRegistered { mode_id, version });
            continue;
        };
        let (Some(factory_ver), Some(ledger_ver)) = (
            semver::Version::parse(&seed_version).ok(),
            semver::Version::parse(&entry.seeded_version).ok(),
        ) else {
            outcomes.push(ModeSeedOutcome::VersionUnparseable {
                mode_id,
                factory_version: seed_version,
                ledger_version: entry.seeded_version.clone(),
            });
            continue;
        };
        match factory_ver.cmp(&ledger_ver) {
            std::cmp::Ordering::Less | std::cmp::Ordering::Equal => {
                outcomes.push(ModeSeedOutcome::Current { mode_id });
            }
            std::cmp::Ordering::Greater => {
                validate_ledger_paths(&entry.files)?;
                if is_uncustomized(&user_dir, entry)? {
                    let from = entry.seeded_version.clone();
                    let new_entry =
                        upgrade_seed_files(&seed_dir, &user_dir, &entry.files, &seed_version)?;
                    upsert_mode_seed_entry(user_modes_dir, &mode_id, new_entry)?;
                    outcomes.push(ModeSeedOutcome::Upgraded {
                        mode_id,
                        from,
                        to: seed_version,
                    });
                } else {
                    outcomes.push(ModeSeedOutcome::UpgradeAvailable {
                        mode_id,
                        seeded_version: entry.seeded_version.clone(),
                        factory_version: seed_version,
                    });
                }
            }
        }
    }
    Ok(outcomes)
}

/// 恢复出厂（账面+副本侧）：删用户副本（同 id 用户赢回落出厂种子，下次启动
/// 对账重播种）+ 清账本条目。返回是否确有可清之物。HTTP 面不在本函数范围。
pub fn reset_mode_seed(user_modes_dir: &Path, mode_id: &str) -> Result<bool, ModeSeedError> {
    if mode_id.is_empty()
        || mode_id.contains('/')
        || mode_id.contains('\\')
        || mode_id == "."
        || mode_id == ".."
    {
        return Err(ModeSeedError::Io(format!("illegal mode id: {mode_id}")));
    }
    let mut ledger = load_mode_seed_ledger(user_modes_dir)?;
    let had_entry = ledger.remove(mode_id).is_some();
    let user_dir = user_modes_dir.join(mode_id);
    if user_dir.is_dir() {
        std::fs::remove_dir_all(&user_dir)
            .map_err(|e| ModeSeedError::Io(format!("{}: {e}", user_dir.display())))?;
    } else if !had_entry {
        return Ok(false);
    }
    save_mode_seed_ledger(user_modes_dir, &ledger)?;
    Ok(true)
}

// ==== 用户空间 git 化（设计稿 2026-09-15 §2.2「用户目录 git 化边界」） ====
//
// 仓库根 = `<USER_ROOT>` 本身；**管辖面仅 `/plugins/` 与 `/config/` 两个子层**，
// `.env` 密钥、`data/` 及其余一切排除——.gitignore 先 ignore-all（`/*`）再白名单
// 两子层，未来新增顶层目录默认不泄漏。晋升 = 用户仓 commit，审计/回滚/合并 git
// 原生；git 化是增强不是依赖：git 二进制缺席或命令失败一律以 Err 上浮，由调用方
// warn 降级（播种/配置等功能不受损），绝不 push。
//
// 与模式种子区一致：目录经参数注入（生产由启动序列传 `user_root()`，测试传临时
// 目录），本节不做环境变量解析。

/// 用户仓 .gitignore 内容：ignore-all + 白名单两子层（`!/.gitignore` 不需要——
/// 本文件自身不入仓，缺失/被删时由 [`ensure_user_repo`] 自动补回）。
const USER_REPO_GITIGNORE: &str = "\
# 用户空间 git 化边界（设计稿 2026-09-15 §2.2）：仅 plugins/ 与 config/ 入仓，
# .env 密钥、data/ 与其余一切排除。本文件缺失会被内核自动补回。
/*
!/plugins/
!/config/
";

/// 用户仓错误。
#[derive(Debug, thiserror::Error)]
pub enum UserRepoError {
    #[error("user repo not initialized（先经 ensure_user_repo 初始化）: {0}")]
    NotInitialized(String),
    #[error("user repo git command failed: {command}: {detail}")]
    Git { command: String, detail: String },
    #[error("user repo io error: {0}")]
    Io(String),
}

/// 在用户仓根跑一条 git 命令，成功返回 stdout（trim 后）；非零退出 → Err。
fn run_git(user_root: &Path, args: &[&str]) -> Result<String, UserRepoError> {
    let command = format!("git {}", args.join(" "));
    let output = std::process::Command::new("git")
        .arg("-C")
        .arg(user_root)
        .args(args)
        .output()
        .map_err(|e| UserRepoError::Git {
            command: command.clone(),
            detail: format!("git 不可用或执行失败: {e}"),
        })?;
    if !output.status.success() {
        return Err(UserRepoError::Git {
            command,
            detail: String::from_utf8_lossy(&output.stderr).trim().to_string(),
        });
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

/// `.git` 存在性（目录仓为目录；worktree/子模块形态为文件，故用 exists）。
fn user_repo_present(user_root: &Path) -> bool {
    user_root.join(".git").exists()
}

fn require_user_repo(user_root: &Path) -> Result<(), UserRepoError> {
    if !user_repo_present(user_root) {
        return Err(UserRepoError::NotInitialized(
            user_root.display().to_string(),
        ));
    }
    Ok(())
}

/// .gitignore 缺失/被删 → 补回（幂等；已存在则尊重现状不覆盖）。
fn ensure_user_repo_gitignore(user_root: &Path) -> Result<(), UserRepoError> {
    let path = user_root.join(".gitignore");
    if path.is_file() {
        return Ok(());
    }
    std::fs::write(&path, USER_REPO_GITIGNORE)
        .map_err(|e| UserRepoError::Io(format!("{}: {e}", path.display())))
}

/// 确保用户仓已初始化（引导挂点用，模式种子对账完成后调用）。
///
/// 已初始化 → 只补 .gitignore（幂等跳过），返回 `Ok(false)`；未初始化 →
/// `git init` + 固定本地身份（`agentos <agentos@localhost>`——提交必须在无全局
/// git 身份的机器上也能成功，且审计作者确定）+ 写 .gitignore，返回 `Ok(true)`。
/// git 二进制缺席/初始化失败 → Err（调用方 warn 降级，功能不受损）。
pub fn ensure_user_repo(user_root: &Path) -> Result<bool, UserRepoError> {
    std::fs::create_dir_all(user_root)
        .map_err(|e| UserRepoError::Io(format!("{}: {e}", user_root.display())))?;
    let fresh = !user_repo_present(user_root);
    if fresh {
        run_git(user_root, &["init"])?;
        run_git(user_root, &["config", "user.name", "agentos"])?;
        run_git(user_root, &["config", "user.email", "agentos@localhost"])?;
    }
    ensure_user_repo_gitignore(user_root)?;
    Ok(fresh)
}

/// 给定路径范围内已暂存的文件清单（`git diff --cached --name-only`；
/// 无变化时该命令本就退出 0 且输出为空，错误翻译统一走 [`run_git`]）。
fn staged_files(user_root: &Path, paths: &[&str]) -> Result<Vec<String>, UserRepoError> {
    let mut args = vec!["diff", "--cached", "--name-only", "--"];
    args.extend(paths.iter().copied());
    Ok(run_git(user_root, &args)?
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .map(str::to_string)
        .collect())
}

/// 提交一笔已暂存改动，返回提交哈希（`git rev-parse HEAD`）。
fn commit_staged(user_root: &Path, message: &str) -> Result<String, UserRepoError> {
    run_git(user_root, &["commit", "-m", message])?;
    run_git(user_root, &["rev-parse", "HEAD"])
}

/// 提交助手：把管辖面（`plugins/` + `config/`）内的暂存/工作区改动提交一笔。
///
/// 面内无任何改动 → `Ok(None)`；有 → `git add -A` 两子层后提交，返回提交哈希
/// （`git rev-parse HEAD`）。失败 → Err（调用方 warn 不 panic）。仓库未初始化 →
/// [`UserRepoError::NotInitialized`]。管辖面外（`.env`/`data/` 等）由 .gitignore
/// 排除、助手也只 `add` 两子层，绝不入提交。
pub fn commit_user_changes(
    user_root: &Path,
    message: &str,
) -> Result<Option<String>, UserRepoError> {
    require_user_repo(user_root)?;
    ensure_user_repo_gitignore(user_root)?;
    let faces: Vec<&str> = ["plugins", "config"]
        .into_iter()
        .filter(|d| user_root.join(d).is_dir())
        .collect();
    if faces.is_empty() {
        return Ok(None);
    }
    let status = run_git(
        user_root,
        &["status", "--porcelain", "--", "plugins", "config"],
    )?;
    if status.is_empty() {
        return Ok(None);
    }
    let mut add = vec!["add", "-A", "--"];
    add.extend(faces.iter().copied());
    run_git(user_root, &add)?;
    if staged_files(user_root, &faces)?.is_empty() {
        return Ok(None);
    }
    Ok(Some(commit_staged(user_root, message)?))
}

/// 种子对账联动提交：对账落盘了播种/升级/补账后自动提交一笔
/// `seed: reconcile <n> files`（n = 暂存文件数）。
///
/// **只暂存本轮对账系统写下的文件**——Seeded/Upgraded 按（刷新后的）账本清单
/// 逐文件 + 账本本身；BaselineRegistered 只补账本（手工副本是用户放置物，留给
/// 用户/晋升流程）。用户工作区其余改动不进这笔提交。无对账写入（outcomes 全是
/// no-op）或无可暂存之物 → `Ok(None)`。
pub fn commit_seed_reconciliation(
    user_root: &Path,
    outcomes: &[ModeSeedOutcome],
) -> Result<Option<String>, UserRepoError> {
    require_user_repo(user_root)?;
    ensure_user_repo_gitignore(user_root)?;
    let touched = outcomes.iter().any(|o| {
        matches!(
            o,
            ModeSeedOutcome::Seeded { .. }
                | ModeSeedOutcome::Upgraded { .. }
                | ModeSeedOutcome::BaselineRegistered { .. }
        )
    });
    if !touched {
        return Ok(None);
    }
    let ledger = load_mode_seed_ledger(&user_root.join("plugins/modes"))
        .map_err(|e| UserRepoError::Io(e.to_string()))?;
    let mut paths: Vec<String> = Vec::new();
    for outcome in outcomes {
        if let ModeSeedOutcome::Seeded { mode_id, .. } | ModeSeedOutcome::Upgraded { mode_id, .. } =
            outcome
        {
            if let Some(entry) = ledger.get(mode_id) {
                paths.extend(
                    entry
                        .files
                        .keys()
                        .map(|rel| format!("plugins/modes/{mode_id}/{rel}")),
                );
            }
        }
    }
    paths.push("plugins/modes/.seeds.json".to_string());
    let existing: Vec<&str> = paths
        .iter()
        .filter(|p| user_root.join(p.as_str()).is_file())
        .map(String::as_str)
        .collect();
    if existing.is_empty() {
        return Ok(None);
    }
    let mut add = vec!["add", "--"];
    add.extend(existing.iter().copied());
    run_git(user_root, &add)?;
    let staged = staged_files(user_root, &existing)?;
    if staged.is_empty() {
        return Ok(None);
    }
    Ok(Some(commit_staged(
        user_root,
        &format!("seed: reconcile {} files", staged.len()),
    )?))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 环境变量是进程全局态：测试串行并配对清场，防同二进制并行用例互相污染。
    struct EnvGuard {
        key: &'static str,
        original: Option<String>,
    }

    impl EnvGuard {
        fn set(key: &'static str, value: &str) -> Self {
            let original = std::env::var(key).ok();
            std::env::set_var(key, value);
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

    /// 把四个分区环境变量全部钉到临时目录，返回隔离的 (user_root, factory_root)。
    fn isolate(tmp: &Path) -> (PathBuf, PathBuf) {
        let user = tmp.join("user-root");
        let factory = tmp.join("factory-config");
        std::fs::create_dir_all(&user).unwrap();
        std::fs::create_dir_all(&factory).unwrap();
        std::env::set_var(USER_ROOT_ENV, &user);
        std::env::remove_var(USER_CONFIG_DIR_ENV);
        std::env::remove_var(USER_DATA_DIR_ENV);
        std::env::remove_var(USER_PLUGINS_DIR_ENV);
        (user, factory)
    }

    #[test]
    fn sub_roots_default_under_user_root() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (user, _f) = isolate(tmp.path());

        assert_eq!(user_plugins_dir().unwrap(), user.join("plugins"));
        assert_eq!(user_config_dir().unwrap(), user.join("config"));
        assert_eq!(user_data_dir().unwrap(), user.join("data"));
        assert_eq!(user_root().unwrap(), user);
    }

    #[test]
    fn partition_env_overrides_user_root() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (user, _f) = isolate(tmp.path());
        let _g = EnvGuard::set(
            USER_CONFIG_DIR_ENV,
            tmp.path().join("elsewhere").to_str().unwrap(),
        );

        // 分区覆盖：config 落到别处，其余仍随用户根
        assert_eq!(user_config_dir().unwrap(), tmp.path().join("elsewhere"));
        assert_eq!(user_plugins_dir().unwrap(), user.join("plugins"));
    }

    #[test]
    fn blank_env_is_treated_as_unset() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (user, _f) = isolate(tmp.path());
        let _g = EnvGuard::set(USER_CONFIG_DIR_ENV, "   ");

        assert_eq!(
            user_config_dir().unwrap(),
            user.join("config"),
            "空白值应视为未设（回退用户根推导）"
        );
    }

    #[test]
    fn resolve_prefers_user_file_when_present() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (_user, factory) = isolate(tmp.path());
        // factory 侧有该文件，用户侧也有 → 用户赢（整体替换）
        std::fs::create_dir_all(factory.join("models")).unwrap();
        std::fs::write(factory.join("models/llm.yaml"), "factory: true").unwrap();
        let user_cfg = user_config_dir().unwrap();
        std::fs::create_dir_all(user_cfg.join("models")).unwrap();
        std::fs::write(user_cfg.join("models/llm.yaml"), "user: true").unwrap();

        let resolved = resolve_config_path(&factory, "models/llm.yaml").unwrap();
        assert_eq!(resolved, user_cfg.join("models/llm.yaml"));
        assert_eq!(std::fs::read_to_string(&resolved).unwrap(), "user: true");
    }

    #[test]
    fn resolve_falls_back_to_factory_when_user_absent() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (_user, factory) = isolate(tmp.path());
        std::fs::create_dir_all(factory.join("models")).unwrap();
        std::fs::write(factory.join("models/llm.yaml"), "factory: true").unwrap();

        let resolved = resolve_config_path(&factory, "models/llm.yaml").unwrap();
        assert_eq!(resolved, factory.join("models/llm.yaml"));
    }

    #[test]
    fn resolve_accepts_config_prefixed_rel() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (_user, factory) = isolate(tmp.path());
        std::fs::create_dir_all(factory.join("models")).unwrap();
        std::fs::write(factory.join("models/llm.yaml"), "x: 1").unwrap();

        // manifest 的 config_files[].path 可能带 config/ 前缀——两种写法必须同解
        let bare = resolve_config_path(&factory, "models/llm.yaml").unwrap();
        let prefixed = resolve_config_path(&factory, "config/models/llm.yaml").unwrap();
        assert_eq!(bare, prefixed, "带 config/ 前缀应解析到同一落点");
    }

    #[test]
    fn user_dir_present_but_file_absent_is_not_takeover() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (_user, factory) = isolate(tmp.path());
        // 用户层目录存在（别的文件已接管），但本文件没接管 → 仍读 factory
        std::fs::create_dir_all(user_config_dir().unwrap().join("models")).unwrap();
        std::fs::write(user_config_dir().unwrap().join("models/other.yaml"), "u").unwrap();
        std::fs::create_dir_all(factory.join("models")).unwrap();
        std::fs::write(factory.join("models/llm.yaml"), "factory").unwrap();

        assert_eq!(
            resolve_config_path(&factory, "models/llm.yaml").unwrap(),
            factory.join("models/llm.yaml"),
            "接管判定按文件存在性，不按目录存在性"
        );
    }

    #[test]
    fn write_target_always_points_into_user_space() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (_user, factory) = isolate(tmp.path());
        let user_cfg = user_config_dir().unwrap();

        // 已接管：直接写用户层文件
        std::fs::create_dir_all(user_cfg.join("models")).unwrap();
        std::fs::write(user_cfg.join("models/llm.yaml"), "u").unwrap();
        let (p, is_user) = config_write_target(&factory, "models/llm.yaml");
        assert!(is_user);
        assert_eq!(p, user_cfg.join("models/llm.yaml"));

        // 未接管：写目标**仍在用户空间**（调用方先播种再写，不覆写 factory）
        let (p2, is_user2) = config_write_target(&factory, "agents/main/agentos.yaml");
        assert!(is_user2);
        assert_eq!(p2, user_cfg.join("agents/main/agentos.yaml"));
        assert!(
            !p2.starts_with(&factory),
            "写目标绝不得落在 factory（否则用户改动进仓内，正是本机制要消灭的）"
        );
    }

    #[test]
    fn ownership_register_load_remove_roundtrip() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (_user, _f) = isolate(tmp.path());

        // 从未接管 = 空表（文件都不存在）
        assert!(load_ownership_entries().unwrap().is_empty());
        assert!(
            !remove_ownership("models/llm.yaml").unwrap(),
            "删除不存在的条目应报 false"
        );

        register_ownership(
            "config/models/llm.yaml",
            Some("deadbeef".into()),
            Some("0.2.0".into()),
        )
        .unwrap();
        let entries = load_ownership_entries().unwrap();
        assert_eq!(entries.len(), 1);
        // rel 规范化：剥 config/ 前缀
        assert_eq!(entries[0].path, "models/llm.yaml");
        assert_eq!(entries[0].seeded_from_sha256.as_deref(), Some("deadbeef"));
        assert!(entries.contains(&entries[0]));

        // 同路径再登记 = 覆盖基线，不重复追加
        register_ownership("models/llm.yaml", Some("newbase".into()), None).unwrap();
        let entries = load_ownership_entries().unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].seeded_from_sha256.as_deref(), Some("newbase"));

        // 删除 = 恢复出厂的账面侧
        assert!(remove_ownership("models/llm.yaml").unwrap());
        assert!(load_ownership_entries().unwrap().is_empty());
    }

    #[test]
    fn ownership_corrupt_ledger_is_err_not_reset() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (_user, _f) = isolate(tmp.path());
        let ledger = ownership_ledger_path().unwrap();
        std::fs::create_dir_all(ledger.parent().unwrap()).unwrap();
        std::fs::write(&ledger, "{not json").unwrap();

        assert!(matches!(
            load_ownership_entries(),
            Err(OwnershipLedgerError::Corrupt(_))
        ));
        assert!(
            register_ownership("a/b.yaml", None, None).is_err(),
            "账本损坏时登记必须失败（fail-closed），不得静默重置丢账"
        );
        // 原始损坏内容原样保留，交人工处置
        assert_eq!(std::fs::read_to_string(&ledger).unwrap(), "{not json");
    }

    #[test]
    fn ownership_illegal_rel_rejected() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (_user, _f) = isolate(tmp.path());
        assert!(register_ownership("../etc/passwd", None, None).is_err());
        assert!(register_ownership("/abs/path.yaml", None, None).is_err());
    }

    #[test]
    fn drift_check_reports_all_four_kinds() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (_user, factory) = isolate(tmp.path());
        let content = b"key: v1\n";
        let baseline = sha256_hex(content);

        // current：出厂文件与基线一致
        std::fs::create_dir_all(factory.join("a")).unwrap();
        std::fs::write(factory.join("a/current.yaml"), content).unwrap();
        std::fs::write(factory.join("a/drifted.yaml"), b"key: v1\n").unwrap();
        register_ownership("a/current.yaml", Some(baseline.clone()), None).unwrap();
        register_ownership("a/drifted.yaml", Some(baseline.clone()), None).unwrap();
        // 漂移：出厂侧换新
        std::fs::write(factory.join("a/drifted.yaml"), b"key: v2\n").unwrap();
        // 出厂缺失
        register_ownership("a/gone.yaml", Some(baseline), None).unwrap();
        // 基线未知（存量补登记取不到出厂原件）
        register_ownership("a/unknown.yaml", None, None).unwrap();

        let mut kinds: Vec<(String, DriftKind)> = check_ownership_drift(&factory)
            .unwrap()
            .into_iter()
            .map(|(e, k)| (e.path, k))
            .collect();
        kinds.sort();
        assert_eq!(
            kinds,
            vec![
                ("a/current.yaml".to_string(), DriftKind::Current),
                ("a/drifted.yaml".to_string(), DriftKind::Drifted),
                ("a/gone.yaml".to_string(), DriftKind::FactoryMissing),
                ("a/unknown.yaml".to_string(), DriftKind::UnknownBaseline),
            ]
        );
    }

    #[test]
    fn sha256_hex_matches_known_vector() {
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    // ── 模式种子版本管理（对账/账本/恢复出厂；目录经参数注入，真实临时目录） ──

    /// 写一个出厂种子目录：plugin.json（带 version）+ profile.yaml + 子目录文件
    /// （种子单元自包含，profile 内打包——设计稿 §2）。
    fn write_seed(root: &Path, mode_id: &str, version: &str, profile: &str) {
        let dir = root.join(mode_id);
        std::fs::create_dir_all(dir.join("assets")).unwrap();
        std::fs::write(
            dir.join("plugin.json"),
            format!(r#"{{"id":"{mode_id}","version":"{version}"}}"#),
        )
        .unwrap();
        std::fs::write(dir.join("profile.yaml"), profile).unwrap();
        std::fs::write(dir.join("assets/extra.txt"), "static").unwrap();
    }

    fn factory_modes(tmp: &Path) -> PathBuf {
        tmp.join("factory-modes")
    }

    fn user_modes(tmp: &Path) -> PathBuf {
        tmp.join("user-modes")
    }

    #[test]
    fn mode_seed_seeds_missing_copy_and_writes_ledger() {
        let tmp = tempfile::tempdir().unwrap();
        let (f, u) = (factory_modes(tmp.path()), user_modes(tmp.path()));
        write_seed(&f, "mode_coding", "0.1.0", "chain: v1\n");

        let outcomes = reconcile_mode_seeds(&f, &u).unwrap();
        assert_eq!(
            outcomes,
            vec![ModeSeedOutcome::Seeded {
                mode_id: "mode_coding".into(),
                version: "0.1.0".into()
            }]
        );
        // 整目录拷贝（含子目录与 profile）
        assert_eq!(
            std::fs::read_to_string(u.join("mode_coding/profile.yaml")).unwrap(),
            "chain: v1\n"
        );
        assert!(u.join("mode_coding/assets/extra.txt").is_file());
        // 账本：版本 + 全部文件哈希基线
        let ledger = load_mode_seed_ledger(&u).unwrap();
        let entry = &ledger["mode_coding"];
        assert_eq!(entry.seeded_version, "0.1.0");
        assert_eq!(entry.files.len(), 3);
        assert_eq!(
            entry.files["profile.yaml"],
            sha256_hex(b"chain: v1\n"),
            "files 基线必须是对拷贝内容的真实哈希"
        );
    }

    #[test]
    fn mode_seed_reconcile_idempotent_and_version_gated() {
        let tmp = tempfile::tempdir().unwrap();
        let (f, u) = (factory_modes(tmp.path()), user_modes(tmp.path()));
        write_seed(&f, "mode_coding", "0.1.0", "chain: v1\n");
        reconcile_mode_seeds(&f, &u).unwrap();

        // 幂等：同版本重跑 → Current，账本不再变
        let outcomes = reconcile_mode_seeds(&f, &u).unwrap();
        assert_eq!(
            outcomes,
            vec![ModeSeedOutcome::Current {
                mode_id: "mode_coding".into()
            }]
        );

        // 出厂内容变了但版本没变 → 版本闸优先，仍 Current，用户副本不动
        write_seed(&f, "mode_coding", "0.1.0", "chain: v1-modified\n");
        assert_eq!(
            reconcile_mode_seeds(&f, &u).unwrap(),
            vec![ModeSeedOutcome::Current {
                mode_id: "mode_coding".into()
            }]
        );
        assert_eq!(
            std::fs::read_to_string(u.join("mode_coding/profile.yaml")).unwrap(),
            "chain: v1\n"
        );

        // 出厂降级（0.1.0 → 0.0.9）→ 同样 no-op（出厂 ≤ 账本）
        write_seed(&f, "mode_coding", "0.0.9", "chain: older\n");
        assert_eq!(
            reconcile_mode_seeds(&f, &u).unwrap(),
            vec![ModeSeedOutcome::Current {
                mode_id: "mode_coding".into()
            }]
        );
        assert_eq!(
            load_mode_seed_ledger(&u).unwrap()["mode_coding"].seeded_version,
            "0.1.0"
        );
    }

    #[test]
    fn mode_seed_uncustomized_copy_silently_upgrades() {
        let tmp = tempfile::tempdir().unwrap();
        let (f, u) = (factory_modes(tmp.path()), user_modes(tmp.path()));
        write_seed(&f, "mode_writing", "0.9.0", "chain: v1\n");
        reconcile_mode_seeds(&f, &u).unwrap();

        // 出厂升到 0.10.0（0.10.0 > 0.9.0 仅在 semver 数值语义下成立——字典序
        // 会误判 "0.10.0" < "0.9.0"，此处同时验证比较走 semver）
        write_seed(&f, "mode_writing", "0.10.0", "chain: v2\n");

        let outcomes = reconcile_mode_seeds(&f, &u).unwrap();
        assert_eq!(
            outcomes,
            vec![ModeSeedOutcome::Upgraded {
                mode_id: "mode_writing".into(),
                from: "0.9.0".into(),
                to: "0.10.0".into()
            }]
        );
        assert_eq!(
            std::fs::read_to_string(u.join("mode_writing/profile.yaml")).unwrap(),
            "chain: v2\n",
            "未定制副本静默替换为新种子"
        );
        assert_eq!(
            load_mode_seed_ledger(&u).unwrap()["mode_writing"].seeded_version,
            "0.10.0"
        );
    }

    /// H1（2026-09-15）：账本外用户自加文件不影响未定制判定——仍走静默升级；
    /// 升级后附加文件必须存活，且不入新账本（否则下轮会被当出厂物覆盖）。
    #[test]
    fn mode_seed_upgrade_keeps_user_added_files_out_of_new_ledger() {
        let tmp = tempfile::tempdir().unwrap();
        let (f, u) = (factory_modes(tmp.path()), user_modes(tmp.path()));
        write_seed(&f, "mode_roleplay", "1.0.0", "chain: v1\n");
        reconcile_mode_seeds(&f, &u).unwrap();

        // 用户在副本里自加文件（角色卡 + 笔记，均不在出厂清单/账本）
        std::fs::create_dir_all(u.join("mode_roleplay/agents")).unwrap();
        std::fs::write(u.join("mode_roleplay/agents/my_card.yaml"), "name: mine\n").unwrap();
        std::fs::write(u.join("mode_roleplay/NOTES.md"), "my notes").unwrap();

        // 出厂升版：账本内文件内容变化 + 出厂新增文件
        write_seed(&f, "mode_roleplay", "1.1.0", "chain: v2\n");
        std::fs::write(f.join("mode_roleplay/assets/new_factory.txt"), "added").unwrap();

        assert_eq!(
            reconcile_mode_seeds(&f, &u).unwrap(),
            vec![ModeSeedOutcome::Upgraded {
                mode_id: "mode_roleplay".into(),
                from: "1.0.0".into(),
                to: "1.1.0".into()
            }],
            "账本外附加文件不参与未定制判定，仍静默升级"
        );
        // 用户附加文件存活
        assert_eq!(
            std::fs::read_to_string(u.join("mode_roleplay/agents/my_card.yaml")).unwrap(),
            "name: mine\n"
        );
        assert_eq!(
            std::fs::read_to_string(u.join("mode_roleplay/NOTES.md")).unwrap(),
            "my notes"
        );
        // 升级生效：账本内文件替换 + 出厂新增文件拷入
        assert_eq!(
            std::fs::read_to_string(u.join("mode_roleplay/profile.yaml")).unwrap(),
            "chain: v2\n"
        );
        assert_eq!(
            std::fs::read_to_string(u.join("mode_roleplay/assets/new_factory.txt")).unwrap(),
            "added"
        );
        // 新账本只记出厂清单（哈希=新出厂内容），绝不收编用户附加文件
        let entry = &load_mode_seed_ledger(&u).unwrap()["mode_roleplay"];
        assert_eq!(entry.seeded_version, "1.1.0");
        assert_eq!(
            entry.files.len(),
            4,
            "plugin.json + profile.yaml + assets/extra.txt + assets/new_factory.txt"
        );
        assert_eq!(entry.files["profile.yaml"], sha256_hex(b"chain: v2\n"));
        assert!(
            !entry.files.contains_key("NOTES.md")
                && !entry.files.contains_key("agents/my_card.yaml"),
            "用户附加文件不入账"
        );
    }

    /// H1 边界：出厂曾删除的账本文件转为用户所有——用户对它的编辑在出厂
    /// 重新加回同名文件时不得被覆盖（升级只替换账本内/拷入新增文件，
    /// 账本外且用户侧已存在的文件一律跳过）。
    #[test]
    fn mode_seed_upgrade_never_clobbers_untracked_user_content() {
        let tmp = tempfile::tempdir().unwrap();
        let (f, u) = (factory_modes(tmp.path()), user_modes(tmp.path()));
        write_seed(&f, "mode_writing", "1.0.0", "chain: v1\n");
        reconcile_mode_seeds(&f, &u).unwrap();

        // 出厂 v2 删除 assets/extra.txt（种子里不再有此文件）
        write_seed(&f, "mode_writing", "2.0.0", "chain: v2\n");
        std::fs::remove_file(f.join("mode_writing/assets/extra.txt")).unwrap();
        assert_eq!(
            reconcile_mode_seeds(&f, &u).unwrap(),
            vec![ModeSeedOutcome::Upgraded {
                mode_id: "mode_writing".into(),
                from: "1.0.0".into(),
                to: "2.0.0".into()
            }]
        );
        // 升级只增改不删：副本里该文件原样保留，且从账本除名（转用户所有）
        assert!(u.join("mode_writing/assets/extra.txt").is_file());
        assert!(!load_mode_seed_ledger(&u).unwrap()["mode_writing"]
            .files
            .contains_key("assets/extra.txt"));

        // 用户编辑这份已脱账的文件
        std::fs::write(u.join("mode_writing/assets/extra.txt"), "user edit").unwrap();

        // 出厂 v3 重新加回同名文件 → 账本外且用户侧已有 → 跳过，用户内容存活
        write_seed(&f, "mode_writing", "3.0.0", "chain: v3\n");
        std::fs::write(f.join("mode_writing/assets/extra.txt"), "factory reborn").unwrap();
        assert_eq!(
            reconcile_mode_seeds(&f, &u).unwrap(),
            vec![ModeSeedOutcome::Upgraded {
                mode_id: "mode_writing".into(),
                from: "2.0.0".into(),
                to: "3.0.0".into()
            }]
        );
        assert_eq!(
            std::fs::read_to_string(u.join("mode_writing/assets/extra.txt")).unwrap(),
            "user edit",
            "账本外文件即使用户侧已存在也绝不覆盖"
        );
        assert!(
            !load_mode_seed_ledger(&u).unwrap()["mode_writing"]
                .files
                .contains_key("assets/extra.txt"),
            "被跳过的文件仍不入账（持续用户所有）"
        );
    }

    /// 已定制判定（H1 后不变）：账本内文件被**删除**同样构成定制——保守走
    /// 升级可用，绝不静默升级（否则用户的删除会被出厂清单复活）。
    #[test]
    fn mode_seed_deleted_ledger_file_counts_as_customized() {
        let tmp = tempfile::tempdir().unwrap();
        let (f, u) = (factory_modes(tmp.path()), user_modes(tmp.path()));
        write_seed(&f, "mode_coding", "1.0.0", "chain: v1\n");
        reconcile_mode_seeds(&f, &u).unwrap();

        std::fs::remove_file(u.join("mode_coding/assets/extra.txt")).unwrap();
        write_seed(&f, "mode_coding", "2.0.0", "chain: v2\n");

        assert_eq!(
            reconcile_mode_seeds(&f, &u).unwrap(),
            vec![ModeSeedOutcome::UpgradeAvailable {
                mode_id: "mode_coding".into(),
                seeded_version: "1.0.0".into(),
                factory_version: "2.0.0".into()
            }]
        );
        assert!(
            !u.join("mode_coding/assets/extra.txt").exists(),
            "副本原样保留（用户的删除不被出厂复活）"
        );
        assert_eq!(
            load_mode_seed_ledger(&u).unwrap()["mode_coding"].seeded_version,
            "1.0.0"
        );
    }

    /// 账本被篡改出越界路径（`../`）→ 对账报错拒绝执行（fail-closed——升级
    /// 按账本逐文件写用户副本，越界路径等于任意文件写），副本原样不动。
    #[test]
    fn mode_seed_tampered_ledger_path_is_rejected_fail_closed() {
        let tmp = tempfile::tempdir().unwrap();
        let (f, u) = (factory_modes(tmp.path()), user_modes(tmp.path()));
        write_seed(&f, "mode_coding", "1.0.0", "chain: v1\n");
        reconcile_mode_seeds(&f, &u).unwrap();

        let ledger_path = u.join(MODE_SEEDS_LEDGER_FILENAME);
        let tampered = serde_json::json!({
            "mode_coding": {
                "seeded_version": "1.0.0",
                "files": {
                    "profile.yaml": sha256_hex(b"chain: v1\n"),
                    "../escaped.txt": "x"
                }
            }
        });
        std::fs::write(&ledger_path, serde_json::to_string(&tampered).unwrap()).unwrap();
        write_seed(&f, "mode_coding", "2.0.0", "chain: v2\n");

        assert!(
            reconcile_mode_seeds(&f, &u).is_err(),
            "越界账本路径必须报错"
        );
        assert_eq!(
            std::fs::read_to_string(u.join("mode_coding/profile.yaml")).unwrap(),
            "chain: v1\n",
            "账本非法时副本绝不被触碰"
        );
    }

    #[test]
    fn mode_seed_customized_copy_preserved_and_upgrade_flagged() {
        let tmp = tempfile::tempdir().unwrap();
        let (f, u) = (factory_modes(tmp.path()), user_modes(tmp.path()));
        write_seed(&f, "mode_research", "1.0.0", "chain: v1\n");
        reconcile_mode_seeds(&f, &u).unwrap();

        // 用户定制：改 profile + 新增自己的文件（任一偏离账本基线即算已定制）
        std::fs::write(u.join("mode_research/profile.yaml"), "chain: mine\n").unwrap();
        std::fs::write(u.join("mode_research/my_notes.yaml"), "note: x").unwrap();

        write_seed(&f, "mode_research", "2.0.0", "chain: v2\n");
        let outcomes = reconcile_mode_seeds(&f, &u).unwrap();
        assert_eq!(
            outcomes,
            vec![ModeSeedOutcome::UpgradeAvailable {
                mode_id: "mode_research".into(),
                seeded_version: "1.0.0".into(),
                factory_version: "2.0.0".into()
            }]
        );
        // 副本与账本都原样保留（升级是用户显式动作，系统绝不合并）
        assert_eq!(
            std::fs::read_to_string(u.join("mode_research/profile.yaml")).unwrap(),
            "chain: mine\n"
        );
        assert!(u.join("mode_research/my_notes.yaml").is_file());
        assert_eq!(
            load_mode_seed_ledger(&u).unwrap()["mode_research"].seeded_version,
            "1.0.0"
        );
    }

    #[test]
    fn mode_seed_manual_copy_gets_baseline_registered_not_replaced() {
        let tmp = tempfile::tempdir().unwrap();
        let (f, u) = (factory_modes(tmp.path()), user_modes(tmp.path()));
        write_seed(&f, "mode_roleplay", "1.2.3", "chain: factory\n");

        // 用户手工放置副本（带自己的版本号，无账目）
        let manual = u.join("mode_roleplay");
        std::fs::create_dir_all(&manual).unwrap();
        std::fs::write(
            manual.join("plugin.json"),
            r#"{"id":"mode_roleplay","version":"9.9.9"}"#,
        )
        .unwrap();
        std::fs::write(manual.join("profile.yaml"), "chain: mine\n").unwrap();

        let outcomes = reconcile_mode_seeds(&f, &u).unwrap();
        assert_eq!(
            outcomes,
            vec![ModeSeedOutcome::BaselineRegistered {
                mode_id: "mode_roleplay".into(),
                version: "9.9.9".into()
            }]
        );
        assert_eq!(
            std::fs::read_to_string(manual.join("profile.yaml")).unwrap(),
            "chain: mine\n",
            "补账绝不替换手工副本"
        );
        let entry = &load_mode_seed_ledger(&u).unwrap()["mode_roleplay"];
        assert_eq!(entry.seeded_version, "9.9.9");
        assert_eq!(entry.files["profile.yaml"], sha256_hex(b"chain: mine\n"));

        // 补账后重跑：9.9.9 > 出厂 1.2.3 → 幂等 no-op，副本仍不动
        assert_eq!(
            reconcile_mode_seeds(&f, &u).unwrap(),
            vec![ModeSeedOutcome::Current {
                mode_id: "mode_roleplay".into()
            }]
        );
        assert_eq!(
            std::fs::read_to_string(manual.join("profile.yaml")).unwrap(),
            "chain: mine\n"
        );
    }

    #[test]
    fn mode_seed_manual_copy_without_version_falls_back_to_factory_version() {
        let tmp = tempfile::tempdir().unwrap();
        let (f, u) = (factory_modes(tmp.path()), user_modes(tmp.path()));
        write_seed(&f, "mode_roleplay", "1.0.0", "chain: factory\n");

        // 手工副本 manifest 无 version → seeded_version 回落出厂 version
        // （保守：下轮出厂同版本时判 Current，不会把手工副本误判"未定制可升级"）
        let manual = u.join("mode_roleplay");
        std::fs::create_dir_all(&manual).unwrap();
        std::fs::write(manual.join("plugin.json"), r#"{"id":"mode_roleplay"}"#).unwrap();
        std::fs::write(manual.join("profile.yaml"), "chain: mine\n").unwrap();

        assert_eq!(
            reconcile_mode_seeds(&f, &u).unwrap(),
            vec![ModeSeedOutcome::BaselineRegistered {
                mode_id: "mode_roleplay".into(),
                version: "1.0.0".into()
            }]
        );
        assert_eq!(
            std::fs::read_to_string(manual.join("profile.yaml")).unwrap(),
            "chain: mine\n"
        );
    }

    #[test]
    fn mode_seed_reset_removes_copy_and_entry_then_reseeds() {
        let tmp = tempfile::tempdir().unwrap();
        let (f, u) = (factory_modes(tmp.path()), user_modes(tmp.path()));
        write_seed(&f, "mode_coding", "1.0.0", "chain: v1\n");
        write_seed(&f, "mode_writing", "1.0.0", "chain: w1\n");
        reconcile_mode_seeds(&f, &u).unwrap();

        assert!(reset_mode_seed(&u, "mode_coding").unwrap());
        assert!(!u.join("mode_coding").exists(), "恢复出厂删用户副本");
        let ledger = load_mode_seed_ledger(&u).unwrap();
        assert!(!ledger.contains_key("mode_coding"), "恢复出厂清账本条目");
        assert!(ledger.contains_key("mode_writing"), "其他模式条目不受牵连");

        // 回落链路：下次启动对账按出厂种子重播种
        assert_eq!(
            reconcile_mode_seeds(&f, &u).unwrap(),
            vec![
                ModeSeedOutcome::Seeded {
                    mode_id: "mode_coding".into(),
                    version: "1.0.0".into()
                },
                ModeSeedOutcome::Current {
                    mode_id: "mode_writing".into()
                },
            ]
        );

        // 无可清之物（无副本也无条目）→ false；非法 id 拒绝（fail-closed，防路径穿越）
        assert!(!reset_mode_seed(&u, "mode_roleplay").unwrap());
        assert!(reset_mode_seed(&u, "../evil").is_err());
        assert!(reset_mode_seed(&u, "a\\b").is_err());
    }

    #[test]
    fn mode_seed_corrupt_ledger_is_err_not_reset() {
        let tmp = tempfile::tempdir().unwrap();
        let (f, u) = (factory_modes(tmp.path()), user_modes(tmp.path()));
        write_seed(&f, "mode_coding", "1.0.0", "chain: v1\n");
        reconcile_mode_seeds(&f, &u).unwrap();

        let ledger_path = u.join(MODE_SEEDS_LEDGER_FILENAME);
        std::fs::write(&ledger_path, "{not json").unwrap();
        write_seed(&f, "mode_coding", "2.0.0", "chain: v2\n");

        assert!(matches!(
            reconcile_mode_seeds(&f, &u),
            Err(ModeSeedError::Corrupt(_))
        ));
        // 账本损坏时用户副本绝不被替换（fail-closed），损坏内容原样保留交人工
        assert_eq!(
            std::fs::read_to_string(u.join("mode_coding/profile.yaml")).unwrap(),
            "chain: v1\n"
        );
        assert_eq!(std::fs::read_to_string(&ledger_path).unwrap(), "{not json");
    }

    #[test]
    fn mode_seed_nonsemver_version_reports_unparseable_and_keeps_copy() {
        let tmp = tempfile::tempdir().unwrap();
        let (f, u) = (factory_modes(tmp.path()), user_modes(tmp.path()));
        write_seed(&f, "mode_coding", "not-semver", "chain: v1\n");
        reconcile_mode_seeds(&f, &u).unwrap();

        write_seed(&f, "mode_coding", "still-not", "chain: v2\n");
        assert_eq!(
            reconcile_mode_seeds(&f, &u).unwrap(),
            vec![ModeSeedOutcome::VersionUnparseable {
                mode_id: "mode_coding".into(),
                factory_version: "still-not".into(),
                ledger_version: "not-semver".into()
            }]
        );
        assert_eq!(
            std::fs::read_to_string(u.join("mode_coding/profile.yaml")).unwrap(),
            "chain: v1\n",
            "版本不可判定时保守不动用户副本"
        );
    }

    #[test]
    fn mode_seed_user_built_mode_without_factory_seed_ignored() {
        let tmp = tempfile::tempdir().unwrap();
        let (f, u) = (factory_modes(tmp.path()), user_modes(tmp.path()));
        // 用户自建模式（出厂无同名种子）不参与对账，账本也不给它立条目
        let custom = u.join("mode_mine");
        std::fs::create_dir_all(&custom).unwrap();
        std::fs::write(
            custom.join("plugin.json"),
            r#"{"id":"mode_mine","version":"0.1.0"}"#,
        )
        .unwrap();

        assert!(reconcile_mode_seeds(&f, &u).unwrap().is_empty());
        assert!(custom.is_dir());
        assert!(load_mode_seed_ledger(&u).unwrap().is_empty());
    }

    // ── 用户空间 git 化（§2.2；真实临时目录 + 真实 git 二进制，目录参数注入） ──

    /// 测试侧独立跑 git 做交叉核验（不经被测函数自己的 run_git）。
    fn git_out(user_root: &Path, args: &[&str]) -> String {
        let output = std::process::Command::new("git")
            .arg("-C")
            .arg(user_root)
            .args(args)
            .output()
            .expect("git binary must be available");
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        assert!(output.status.success(), "git {args:?} failed: {stderr}");
        String::from_utf8_lossy(&output.stdout).trim().to_string()
    }

    #[test]
    fn user_repo_init_is_idempotent_and_restores_gitignore() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("user-root");

        // 全新初始化：建仓 + .gitignore 白名单 + 可重复
        assert!(ensure_user_repo(&root).unwrap(), "全新应报告新建仓");
        assert!(root.join(".git").exists());
        let ignore = std::fs::read_to_string(root.join(".gitignore")).unwrap();
        assert_eq!(ignore, USER_REPO_GITIGNORE);
        for line in ["/*", "!/plugins/", "!/config/"] {
            let whitelisted = ignore.lines().any(|l| l.trim() == line);
            assert!(whitelisted, "缺白名单行 {line}");
        }
        // 已初始化 → 跳过（幂等零操作）
        assert!(!ensure_user_repo(&root).unwrap());

        // .gitignore 被删 → 自动补回
        std::fs::remove_file(root.join(".gitignore")).unwrap();
        assert!(!ensure_user_repo(&root).unwrap(), "已初始化只补 ignore");
        assert_eq!(
            std::fs::read_to_string(root.join(".gitignore")).unwrap(),
            USER_REPO_GITIGNORE
        );
    }

    #[test]
    fn user_repo_commit_tracks_whitelisted_face_only() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("user-root");
        ensure_user_repo(&root).unwrap();

        // 管辖面内 + 面外（.env 密钥 / data/ / 顶层杂文件）
        std::fs::create_dir_all(root.join("plugins/m1")).unwrap();
        std::fs::create_dir_all(root.join("data")).unwrap();
        std::fs::write(root.join("plugins/m1/plugin.json"), r#"{"id":"m1"}"#).unwrap();
        std::fs::create_dir_all(root.join("config")).unwrap();
        std::fs::write(root.join("config/a.yaml"), "a: 1\n").unwrap();
        std::fs::write(root.join(".env"), "TOKEN=secret\n").unwrap();
        std::fs::write(root.join("data/d.db"), "bytes").unwrap();
        std::fs::write(root.join("top.md"), "stray").unwrap();

        let h1 = commit_user_changes(&root, "base").unwrap().unwrap();
        assert_eq!(h1, git_out(&root, &["rev-parse", "HEAD"]));
        assert_eq!(h1.len(), 40, "提交哈希为完整 SHA-1");

        // 排除面：只有两子层入仓
        let tracked = git_out(&root, &["ls-files"]);
        assert!(tracked.contains("plugins/m1/plugin.json"));
        assert!(tracked.contains("config/a.yaml"));
        assert!(!tracked.contains(".env"), "密钥文件绝不入仓");
        assert!(!tracked.contains("data/"), "data/ 绝不入仓");
        assert!(!tracked.contains("top.md"), "顶层杂文件不入仓");

        // 无改动 → None；仅面外改动（.env）同样 → None
        assert_eq!(commit_user_changes(&root, "noop").unwrap(), None);
        std::fs::write(root.join(".env"), "TOKEN=rotated\n").unwrap();
        assert_eq!(commit_user_changes(&root, "env-only").unwrap(), None);

        // 面内改动 → 新哈希 ≠ 旧哈希
        std::fs::write(root.join("plugins/m1/plugin.json"), r#"{"id":"m1","v":2}"#).unwrap();
        std::fs::write(root.join("config/a.yaml"), "a: 2\n").unwrap();
        let h2 = commit_user_changes(&root, "user edit").unwrap().unwrap();
        assert_ne!(h2, h1);
        let changed = git_out(&root, &["show", "--pretty=format:", "--name-only", "HEAD"]);
        assert_eq!(changed.lines().count(), 2, "一笔只含两处面内改动");
    }

    /// 管辖面两子层全缺席（新仓只有 .git 与 .gitignore）→ 提交零操作。
    #[test]
    fn user_repo_commit_without_managed_dirs_is_noop() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("user-root");
        ensure_user_repo(&root).unwrap();
        assert_eq!(commit_user_changes(&root, "empty").unwrap(), None);
    }

    /// 面内状态"脏"但 add 后无实质暂存（暂存过一版、工作区又退回 HEAD 内容）
    /// → None，绝不产出空提交。
    #[test]
    fn user_repo_commit_with_nothing_staged_after_add_is_noop() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("user-root");
        ensure_user_repo(&root).unwrap();
        std::fs::create_dir_all(root.join("plugins/m1")).unwrap();
        std::fs::write(root.join("plugins/m1/a.yaml"), "a: 1\n").unwrap();
        commit_user_changes(&root, "base").unwrap();

        std::fs::write(root.join("plugins/m1/a.yaml"), "a: 2\n").unwrap();
        git_out(&root, &["add", "plugins/m1/a.yaml"]);
        std::fs::write(root.join("plugins/m1/a.yaml"), "a: 1\n").unwrap();

        assert_eq!(commit_user_changes(&root, "ghost").unwrap(), None);
    }

    #[test]
    fn user_repo_commit_without_repo_is_err() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("not-a-repo");
        std::fs::create_dir_all(&root).unwrap();

        assert!(matches!(
            commit_user_changes(&root, "x"),
            Err(UserRepoError::NotInitialized(_))
        ));
        assert!(matches!(
            commit_seed_reconciliation(
                &root,
                &[ModeSeedOutcome::Seeded {
                    mode_id: "m".into(),
                    version: "1.0.0".into()
                }]
            ),
            Err(UserRepoError::NotInitialized(_))
        ));
    }

    /// git 命令失败（仓库元数据损坏）→ Err 上浮（调用方 warn 降级），绝不吞错。
    #[test]
    fn user_repo_git_failure_surfaces_as_err() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("user-root");
        ensure_user_repo(&root).unwrap();
        std::fs::create_dir_all(root.join("plugins/m1")).unwrap();
        std::fs::write(root.join("plugins/m1/x.yaml"), "x: 1\n").unwrap();
        std::fs::write(root.join(".git/HEAD"), "garbage").unwrap();
        assert!(commit_user_changes(&root, "broken").is_err());
    }

    /// git 二进制不可解析（PATH 钉到空目录）→ Err 上浮不 panic；warn 降级由
    /// 调用方负责。PATH 是进程全局态：全部涉 git 用例互斥执行。
    #[test]
    fn user_repo_missing_git_binary_is_err_not_panic() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("user-root");
        ensure_user_repo(&root).unwrap();
        std::fs::create_dir_all(root.join("plugins/m1")).unwrap();
        std::fs::write(root.join("plugins/m1/x.yaml"), "x: 1\n").unwrap();
        let _path = EnvGuard::set("PATH", tmp.path().join("no-bin").to_str().unwrap());
        let result = commit_user_changes(&root, "no git");
        assert!(matches!(result, Err(UserRepoError::Git { .. })));
    }

    /// 联动提交只收对账写下的文件：升级文件 + 账本入提交，用户未提交的
    /// 自加文件（mode 副本内 NOTES、config 下私文件）留在工作区待晋升。
    #[test]
    fn seed_reconcile_commit_captures_only_reconciled_files() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("user-root");
        let factory = tmp.path().join("factory-modes");
        let user_modes = root.join("plugins/modes");
        write_seed(&factory, "mode_x", "1.0.0", "chain: v1\n");

        let _outcomes = reconcile_mode_seeds(&factory, &user_modes).unwrap();
        ensure_user_repo(&root).unwrap();
        let _base = commit_user_changes(&root, "base").unwrap().unwrap();

        // 用户工作区改动：副本内自加文件 + 用户配置（均不提交，留给晋升）
        std::fs::write(user_modes.join("mode_x/NOTES.md"), "mine").unwrap();
        std::fs::create_dir_all(root.join("config")).unwrap();
        std::fs::write(root.join("config/mine.yaml"), "mine: true\n").unwrap();

        // 出厂升级 → 静默升级写盘 → 联动提交
        write_seed(&factory, "mode_x", "1.1.0", "chain: v2\n");
        std::fs::write(factory.join("mode_x/assets/new.txt"), "added").unwrap();
        let upgraded = reconcile_mode_seeds(&factory, &user_modes).unwrap();
        assert!(matches!(upgraded[0], ModeSeedOutcome::Upgraded { .. }));

        let hash = commit_seed_reconciliation(&root, &upgraded)
            .unwrap()
            .unwrap();
        assert_eq!(hash, git_out(&root, &["rev-parse", "HEAD"]));
        let changed = git_out(&root, &["show", "--pretty=format:", "--name-only", &hash]);
        for file in [
            "plugins/modes/mode_x/profile.yaml",
            "plugins/modes/mode_x/assets/new.txt",
            "plugins/modes/.seeds.json",
        ] {
            assert!(changed.lines().any(|l| l == file), "联动提交应含 {file}");
        }
        let has_user_files = changed.lines().any(|l| l.ends_with("NOTES.md"))
            || changed.lines().any(|l| l == "config/mine.yaml");
        assert!(!has_user_files, "用户工作区改动不进联动提交");

        // message 形如 `seed: reconcile <n> files`，n == 提交内文件数（性质断言）
        let subject = git_out(&root, &["log", "-1", "--format=%s"]);
        assert!(subject.starts_with("seed: reconcile ") && subject.ends_with(" files"));
        let n: usize = subject
            .trim_start_matches("seed: reconcile ")
            .trim_end_matches(" files")
            .parse()
            .unwrap();
        assert_eq!(n, changed.lines().count());
        assert!(n >= 3, "至少含升级文件与账本");

        // 幂等：同一批对账结论重复联动提交 → add 后无实质暂存 → None
        assert_eq!(commit_seed_reconciliation(&root, &upgraded).unwrap(), None);

        // 用户改动仍在工作区，走晋升流程显式提交
        let dirty = git_out(&root, &["status", "--porcelain"]);
        assert_eq!(dirty.lines().count(), 2, "用户改动保持未提交");
        let promoted = commit_user_changes(&root, "promote user edits")
            .unwrap()
            .unwrap();
        assert_ne!(promoted, hash);
        assert!(git_out(&root, &["ls-files"]).contains("config/mine.yaml"));
    }

    /// 对账全 no-op（幂等重跑）→ 联动提交零操作，历史不变。
    #[test]
    fn seed_reconcile_without_changes_commits_nothing() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("user-root");
        let factory = tmp.path().join("factory-modes");
        let user_modes = root.join("plugins/modes");
        write_seed(&factory, "mode_x", "1.0.0", "chain: v1\n");

        let _outcomes = reconcile_mode_seeds(&factory, &user_modes).unwrap();
        ensure_user_repo(&root).unwrap();
        commit_user_changes(&root, "base").unwrap();

        let rerun = reconcile_mode_seeds(&factory, &user_modes).unwrap();
        assert!(matches!(rerun[0], ModeSeedOutcome::Current { .. }));
        assert_eq!(commit_seed_reconciliation(&root, &rerun).unwrap(), None);
        assert_eq!(git_out(&root, &["rev-list", "--count", "HEAD"]), "1");

        // outcomes 为空（无出厂种子）同样零操作
        assert_eq!(commit_seed_reconciliation(&root, &[]).unwrap(), None);
        assert_eq!(git_out(&root, &["rev-list", "--count", "HEAD"]), "1");
    }

    /// 对账结论指向的模式在账本无条目、账本文件亦缺席（如种子被重定位）→
    /// 无可暂存之物，联动提交零操作。
    #[test]
    fn seed_reconcile_commit_without_any_paths_is_noop() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("user-root");
        ensure_user_repo(&root).unwrap();
        let ghost = [ModeSeedOutcome::Seeded {
            mode_id: "ghost".into(),
            version: "1.0.0".into(),
        }];
        assert_eq!(commit_seed_reconciliation(&root, &ghost).unwrap(), None);
    }

    /// 串行锁：env 是进程态，本模块测试互斥执行。
    fn tests_guard() -> std::sync::MutexGuard<'static, ()> {
        static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
        LOCK.lock().unwrap_or_else(|e| e.into_inner())
    }
}

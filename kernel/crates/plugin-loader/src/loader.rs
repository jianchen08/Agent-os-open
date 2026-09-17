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

/// 白名单中的单个插件条目。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct AllowlistEntry {
    pub id: String,
    /// 可选 SHA256（manifest 文件字节 || entry 文件字节 的哈希，小写 hex）。
    /// 留空则只校验 id，不校验哈希。
    #[serde(default)]
    pub sha256: String,
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
            Ok(cfg) => cfg,
            Err(e) => {
                warn!(
                    path = %path.display(),
                    error = %e,
                    "plugin_allowlist.yaml 解析失败 → fail-closed：退化为 strict 空名单（所有插件拒载），修复配置后重启"
                );
                AllowlistConfig {
                    mode: AllowlistMode::Strict,
                    plugins: Vec::new(),
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
    /// 已发现的 manifest 来源缓存 {plugin_id: manifest 文件路径}。
    ///
    /// 内存归一（manifest 唯一真源 = AppState.manifests）：本表只持轻量路径
    /// 摘要，**不**持久持有解析后的完整 PluginManifest——完整内容按需
    /// 读盘 + 重新解析（[`Self::read_manifest`]）。路径本身兼作内容指纹的
    /// 粗判（同 id 换路径 = manifest 换源），watcher 热发现经 discover
    /// 重扫刷新。
    manifests: RwLock<HashMap<String, PathBuf>>,
    /// 已加载的插件状态 {plugin_id: LoadedPlugin}
    loaded: RwLock<HashMap<String, LoadedPlugin>>,
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
            manifests: RwLock::new(HashMap::new()),
            loaded: RwLock::new(HashMap::new()),
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
                        "native artifact 无法解析相对路径（manifest {:?} 无父目录）",
                        source_path
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

        // 哈希校验（仅当白名单条目声明了 sha256 时执行）
        if let Some(entry) = entry {
            if !entry.sha256.is_empty() {
                let computed = self.compute_plugin_sha256(manifest, source_path)?;
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

    /// 计算 `sha256(manifest_bytes || entry_file_bytes)`，返回小写 hex。
    ///
    /// `manifest_bytes` = 读取 `source_path` 的原始字节。
    /// `entry_file_bytes` = entry 字段在插件目录（`source_path.parent()`）
    /// 中引用到的入口文件字节；找不到入口文件则按空字节处理
    /// （如 `python3 -m my_plugin` 这类没有明确入口文件的情况）。
    fn compute_plugin_sha256(
        &self,
        manifest: &PluginManifest,
        source_path: &Path,
    ) -> Result<String, LoaderError> {
        let manifest_bytes = std::fs::read(source_path).map_err(|e| LoaderError::Io {
            message: format!("Failed to read manifest {}: {}", source_path.display(), e),
        })?;

        let entry_bytes = self.read_entry_bytes(manifest, source_path)?;

        let mut hasher = Sha256::new();
        hasher.update(&manifest_bytes);
        hasher.update(&entry_bytes);
        Ok(format!("{:x}", hasher.finalize()))
    }

    /// 读取 entry 字段引用的入口文件字节。
    ///
    /// 解析规则（保守）：
    /// - 取 entry 字符串的最后一个 token 作为候选入口文件名
    ///   （如 `python3 server.py` → `server.py`）。
    /// - 仅当该 token 在插件目录（`source_path.parent()`）下作为文件存在时才读取；
    ///   否则返回空字节（如 `python3 -m my_plugin` 或 entry 为空）。
    fn read_entry_bytes(
        &self,
        manifest: &PluginManifest,
        source_path: &Path,
    ) -> Result<Vec<u8>, LoaderError> {
        if manifest.entry.is_empty() {
            return Ok(Vec::new());
        }
        // 取最后一个 token
        let candidate = manifest.entry.split_whitespace().last();
        let Some(file_name) = candidate else {
            return Ok(Vec::new());
        };
        // 排除明显是 flag（如 `-m`/`--port`）或命令本身的情况
        if file_name.starts_with('-') {
            return Ok(Vec::new());
        }
        let Some(dir) = source_path.parent() else {
            return Ok(Vec::new());
        };
        let entry_path = dir.join(file_name);
        match std::fs::read(&entry_path) {
            Ok(bytes) => Ok(bytes),
            Err(_) => Ok(Vec::new()),
        }
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

#[async_trait]
impl PluginLoader for PluginLoaderImpl {
    /// 扫描指定根目录，发现所有 plugin.json manifest。
    ///
    /// 双根扫描：先扫内置根，再扫用户根。用户根的插件优先级高于内置根（同 ID 覆盖）。
    async fn discover(
        &self,
        root_paths: &[&str],
    ) -> Result<Vec<PluginManifest>, agentos_core::types::PluginError> {
        let mut all_manifests = HashMap::new();

        // 扫描 root_paths（外部传入的路径）。同 id 双源裁决与内置根/用户根扫描段
        // 同语义：**用户根子树赢**。root_paths 的迭代序来自调用方（热路径
        // discover_new_plugins 把内置+用户根的父目录混装进每轮新建的 HashSet，
        // 序不稳定），朴素 last-wins 会让双源同 id 的胜者逐轮翻转——源码目录随之
        // 翻转 → 代码指纹每轮必变 → 复验驱逐/重注册循环（/ext 路由间歇 503/504，
        // 2026-09-18 根修）。故用户根条目一旦胜出即不可被非用户根条目覆盖，
        // 与迭代序解耦；其余路径间维持 last-wins 不变。
        let is_user_rooted = |p: &Path| -> bool {
            self.user_root
                .as_ref()
                .is_some_and(|user_root| p.starts_with(user_root))
        };
        for root_str in root_paths {
            let root = Path::new(root_str);
            match self.scan_root(root) {
                Ok(found) => {
                    for (manifest, path) in found {
                        let dominated = all_manifests.get(&manifest.id).is_some_and(
                            |(_, existing): &(PluginManifest, PathBuf)| {
                                is_user_rooted(existing.as_path()) && !is_user_rooted(&path)
                            },
                        );
                        if !dominated {
                            all_manifests.insert(manifest.id.clone(), (manifest, path));
                        }
                    }
                }
                Err(e) => {
                    warn!("Failed to scan root {}: {}", root.display(), e);
                }
            }
        }

        // 扫描内置根（Err 记 warn 不吞——与 root_paths 分支同款：单根失败不阻断其余根）
        match self.scan_root(&self.builtin_root) {
            Ok(found) => {
                for (manifest, path) in found {
                    all_manifests
                        .entry(manifest.id.clone())
                        .or_insert((manifest, path));
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

        // 扫描用户根（用户根覆盖内置根：同 ID）
        if let Some(user_root) = &self.user_root {
            match self.scan_root(user_root) {
                Ok(found) => {
                    for (manifest, path) in found {
                        all_manifests.insert(manifest.id.clone(), (manifest, path));
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

        Ok(all_manifests.into_values().map(|(m, _)| m).collect())
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
                    message: format!("plugin not found: {}", plugin_id),
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
                message: format!("plugin not loaded: {}", plugin_id),
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
        let user_replaced = apply_user_config_overlay(&mut config_map);

        let config_keys: Vec<String> = config_map.keys().cloned().collect();
        info!(
            "Loaded {} config entries from {}: [{}]{}",
            config_map.len(),
            config_root.display(),
            config_keys.join(", "),
            if user_replaced > 0 {
                format!(" (+{user_replaced} from user space)")
            } else {
                String::new()
            }
        );

        Ok(serde_json::Value::Object(config_map))
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
}

impl PluginLoaderImpl {
    /// 从缓存路径读盘 + 解析完整 manifest（JSON 优先，YAML 兜底——与
    /// discover 扫描同一解析顺序）。解析失败返回 Err（调用方决定语义）。
    ///
    /// 供 [`Self::get_manifest`] 与 [`PluginLoader::load`] 按需读盘用——
    /// 运行时完整 manifest 不常驻内存（唯一真源在 AppState.manifests）。
    fn read_manifest(&self, path: &Path) -> Result<PluginManifest, LoaderError> {
        let content = std::fs::read_to_string(path).map_err(|e| LoaderError::Io {
            message: format!("Failed to read {}: {}", path.display(), e),
        })?;
        match serde_json::from_str::<PluginManifest>(&content) {
            Ok(m) => Ok(m),
            Err(json_err) => serde_yaml::from_str::<PluginManifest>(&content).map_err(|yaml_err| {
                LoaderError::ManifestParse {
                    path: path.display().to_string(),
                    message: format!("json error: {}, yaml error: {}", json_err, yaml_err),
                }
            }),
        }
    }

    /// 获取所有已发现插件的根目录映射（plugin_id → 插件目录绝对路径）。
    ///
    /// HTTP dispatcher 据此把 `/ext/{plugin_id}/assets/{*path}`
    /// 解析到 `<plugin_dir>/web/<path>` 直读文件返回，免去为每个子资源单独声明
    /// http_endpoints。由 agentos-kernel 启动期调用，把结果经
    /// `AppState::with_plugin_dirs` 注入。
    pub fn get_plugin_dirs(&self) -> std::collections::HashMap<String, std::path::PathBuf> {
        let manifests = self.manifests.read();
        manifests
            .iter()
            .filter_map(|(id, path)| path.parent().map(|p| (id.clone(), p.to_path_buf())))
            .collect()
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
/// 返回被用户层接管的文件数（日志可观测性——"改了没生效"排查的第一线索）。
fn apply_user_config_overlay(config_map: &mut serde_json::Map<String, serde_json::Value>) -> usize {
    let Some(user_root) = agentos_core::user_space::user_config_dir() else {
        return 0;
    };
    if !user_root.is_dir() {
        return 0;
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
        return 0;
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
    replaced
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

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::traits::{HostType, ProvidedCapabilityHost, StepCapability};
    use std::fs;

    fn create_test_plugin_dir(root: &Path, id: &str, plugin_type: &str) {
        let dir = root.join(id);
        fs::create_dir_all(&dir).unwrap();
        // pipeline 类型插件需声明 invoke_entry（ADR 附录 D②，P6 discover 聚合校验）
        let invoke_entry_field = if plugin_type == "pipeline" {
            format!(",\n    \"invoke_entry\": \"{}.execute\"", id)
        } else {
            String::new()
        };
        let manifest_json = format!(
            r#"{{
    "id": "{}",
    "name": "Test Plugin {}",
    "version": "1.0.0",
    "plugin_type": "{}",
    "language": "rust",
    "host_type": "in_process",
    "entry": "test_plugin",
    "capabilities": {{}},
    "requires_services": [],
    "permissions": {{}},
    "priority": 100{}
}}"#,
            id, id, plugin_type, invoke_entry_field
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
            pipeline_role: None,
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
            granted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
            export_fields: vec![],
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
                pipeline_role: None,
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
                granted_capabilities: vec![],
                requires_content: None,
                invoke_entry: None,
                config_files: vec![],
                http_endpoints: vec![],
                ui_schema: None,
                contributes: None,
                enabled: None,
                activation: None,
                provides: None,
                persistent_fields: vec![],
                export_fields: vec![],
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
            pipeline_role: None,
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
            granted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
            export_fields: vec![],
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
            pipeline_role: None,
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
            granted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
            export_fields: vec![],
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
            pipeline_role: None,
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
            granted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
            export_fields: vec![],
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
            pipeline_role: None,
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
            granted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
            export_fields: vec![],
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
            pipeline_role: None,
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
            granted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
            export_fields: vec![],
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
            "plugin dir should end with plugin id, got: {}",
            dir
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
            "plugin dir should end with plugin id, got: {}",
            dir
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
            pipeline_role: None,
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
            granted_capabilities: vec![],
            requires_content: Some(2),
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
            export_fields: vec![],
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

    /// 环境变量是进程全局态：用户空间相关用例互斥执行 + 自动还原。
    ///
    /// 可重入（线程内）：用例可能先显式取锁、再由 [`UserRootGuard`] 取一次——
    /// 裸 `Mutex` 会自锁死。首个持有者记原始环境，最后一个释放时恢复。
    fn user_space_env_lock() -> UserSpaceEnvGuard {
        static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
        thread_local! {
            static DEPTH: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
        }
        let depth = DEPTH.with(|d| {
            let v = d.get();
            d.set(v + 1);
            v
        });
        if depth > 0 {
            return UserSpaceEnvGuard { _lock: None };
        }
        let lock = LOCK.lock().unwrap_or_else(|e| e.into_inner());
        UserSpaceEnvGuard {
            _lock: Option::Some(lock),
        }
    }

    /// [`user_space_env_lock`] 的 guard：最外层持有者 drop 时放锁。
    struct UserSpaceEnvGuard {
        _lock: Option<std::sync::MutexGuard<'static, ()>>,
    }

    impl Drop for UserSpaceEnvGuard {
        fn drop(&mut self) {
            thread_local! {
                static DEPTH: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
            }
            DEPTH.with(|d| d.set(d.get().saturating_sub(1)));
        }
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

    /// 把 `AGENTOS_USER_CONFIG_DIR` 钉到指定目录（Drop 还原），隔离真实用户空间。
    ///
    /// 直接钉配置层而非 `USER_ROOT`：测试目录即配置层根，省去 `config/` 中转，
    /// 且顺带覆盖「分区环境变量覆盖用户根」这条解析路径。
    ///
    /// **必须同时持 [`user_space_env_lock`]**：环境变量是进程全局态，本文件用例
    /// 默认并行——只设不锁时，未钉桩的用例（如 load_config 空目录类）会读到别的
    /// 用例刚设的值，或读到开发机真实用户目录里的残留配置（实测断言恒非空）。
    struct UserRootGuard {
        _lock: UserSpaceEnvGuard,
        original_cfg: Option<String>,
    }

    impl UserRootGuard {
        fn set(config_dir: &Path) -> Self {
            let lock = user_space_env_lock();
            let original_cfg = std::env::var(agentos_core::user_space::USER_CONFIG_DIR_ENV).ok();
            std::env::set_var(agentos_core::user_space::USER_CONFIG_DIR_ENV, config_dir);
            Self {
                _lock: lock,
                original_cfg,
            }
        }
    }

    impl Drop for UserRootGuard {
        fn drop(&mut self) {
            match &self.original_cfg {
                Some(v) => std::env::set_var(agentos_core::user_space::USER_CONFIG_DIR_ENV, v),
                None => std::env::remove_var(agentos_core::user_space::USER_CONFIG_DIR_ENV),
            }
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

        // 算出正确的 sha256(manifest_bytes || entry_bytes)
        let manifest_bytes = fs::read(&manifest_path).unwrap();
        let entry_bytes = fs::read(plugin_dir.join("server.py")).unwrap();
        let mut hasher = Sha256::new();
        hasher.update(&manifest_bytes);
        hasher.update(&entry_bytes);
        let correct_hash = format!("{:x}", hasher.finalize());

        // 1) 正确哈希 → 放行
        let allowlist_ok = AllowlistConfig {
            mode: AllowlistMode::Permissive,
            plugins: vec![AllowlistEntry {
                id: "hashed_plugin".to_string(),
                sha256: correct_hash.clone(),
            }],
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
            }],
        };
        let loader_bad = PluginLoaderImpl::new(builtin.path(), None).with_allowlist(allowlist_bad);
        let manifests_bad = loader_bad.discover(&[]).await.unwrap();
        assert!(
            manifests_bad.is_empty(),
            "sha256 mismatch should reject the plugin"
        );
    }

    // ── P6 命名治理（ADR 附录 D③/D.5）：discover 启动期聚合校验 invoke_entry ──

    /// 辅助：写一个 sidecar pipeline manifest（可指定 invoke_entry）。
    fn write_pipeline_plugin(root: &Path, id: &str, invoke_entry: Option<&str>) {
        let dir = root.join(id);
        fs::create_dir_all(&dir).unwrap();
        let entry_field = match invoke_entry {
            Some(e) => format!(",\n    \"invoke_entry\": \"{}\"", e),
            None => String::new(),
        };
        let manifest_json = format!(
            r#"{{
    "id": "{}",
    "name": "Pipeline {}",
    "version": "1.0.0",
    "plugin_type": "pipeline",
    "language": "python",
    "host_type": "sidecar",
    "entry": "python server.py",
    "capabilities": {{}}{}
}}"#,
            id, id, entry_field
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

    // ── 入口文件哈希提取（read_entry_bytes）的边界分支 ──

    /// entry 为空 → 空字节（等价只哈希 manifest）。
    #[test]
    fn read_entry_bytes_empty_entry_is_empty() {
        let dir = tempfile::tempdir().unwrap();
        let manifest_path = dir.path().join("p.json");
        fs::write(&manifest_path, "{}").unwrap();
        let loader = PluginLoaderImpl::new(dir.path(), None);
        let m: PluginManifest = serde_json::from_str(
            r#"{
                "id":"e","name":"N","version":"1.0.0","plugin_type":"tool",
                "language":"python","host_type":"sidecar","entry":"",
                "capabilities":{}
            }"#,
        )
        .unwrap();
        assert!(
            loader
                .read_entry_bytes(&m, &manifest_path)
                .unwrap()
                .is_empty(),
            "空 entry 无入口文件"
        );
    }

    /// entry 末 token 是 flag（`-m`）或文件不存在 → 空字节（不误当文件读）。
    #[test]
    fn read_entry_bytes_rejects_flags_and_missing_files() {
        let dir = tempfile::tempdir().unwrap();
        let manifest_path = dir.path().join("p.json");
        fs::write(&manifest_path, "{}").unwrap();
        let loader = PluginLoaderImpl::new(dir.path(), None);

        for entry in ["python -m", "python -m somemod", "python missing_server.py"] {
            let m: PluginManifest = serde_json::from_str(&format!(
                r#"{{
                    "id":"e","name":"N","version":"1.0.0","plugin_type":"tool",
                    "language":"python","host_type":"sidecar","entry":"{entry}",
                    "capabilities":{{}}
                }}"#
            ))
            .unwrap();
            assert!(
                loader
                    .read_entry_bytes(&m, &manifest_path)
                    .unwrap()
                    .is_empty(),
                "entry={entry:?} 不应取到入口文件字节"
            );
        }
    }

    /// 入口文件存在 → 返回其真实字节（sha256 语料需覆盖插件代码本体）。
    #[test]
    fn read_entry_bytes_reads_existing_entry_file() {
        let dir = tempfile::tempdir().unwrap();
        let manifest_path = dir.path().join("plugin.json");
        fs::write(&manifest_path, "{}").unwrap();
        fs::write(dir.path().join("server.py"), b"print('hi')").unwrap();
        let loader = PluginLoaderImpl::new(dir.path(), None);
        let m: PluginManifest = serde_json::from_str(
            r#"{
                "id":"e","name":"N","version":"1.0.0","plugin_type":"tool",
                "language":"python","host_type":"sidecar","entry":"python server.py",
                "capabilities":{}
            }"#,
        )
        .unwrap();
        assert_eq!(
            loader.read_entry_bytes(&m, &manifest_path).unwrap(),
            b"print('hi')",
            "应读到入口文件字节"
        );
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

    /// manifest 路径不可读时 sha256 计算同样传播 IO 错误（不静默当空 manifest）。
    #[test]
    fn compute_plugin_sha256_propagates_manifest_read_failure() {
        let dir = tempfile::tempdir().unwrap();
        let loader = PluginLoaderImpl::new(dir.path(), None);
        let m: PluginManifest = serde_json::from_str(
            r#"{
                "id":"e","name":"N","version":"1.0.0","plugin_type":"tool",
                "language":"python","host_type":"sidecar","entry":"server.py",
                "capabilities":{}
            }"#,
        )
        .unwrap();
        let missing_path = dir.path().join("no-such-manifest.json");
        let err = loader
            .compute_plugin_sha256(&m, &missing_path)
            .expect_err("manifest 缺失应报 IO 错误");
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

    /// 入口文件为**目录**（存在但不可读）→ 空字节（读取失败按无入口处理）。
    #[test]
    fn read_entry_bytes_directory_instead_of_file_reads_empty() {
        let dir = tempfile::tempdir().unwrap();
        let manifest_path = dir.path().join("p.json");
        fs::write(&manifest_path, "{}").unwrap();
        // 同名目录占位：is_file 为假，std::fs::read 失败
        fs::create_dir_all(dir.path().join("server.py")).unwrap();
        let loader = PluginLoaderImpl::new(dir.path(), None);
        let m: PluginManifest = serde_json::from_str(
            r#"{
                "id":"e","name":"N","version":"1.0.0","plugin_type":"tool",
                "language":"python","host_type":"sidecar","entry":"python server.py",
                "capabilities":{}
            }"#,
        )
        .unwrap();
        assert!(
            loader
                .read_entry_bytes(&m, &manifest_path)
                .unwrap()
                .is_empty(),
            "目录占位入口应读为空字节（不 panic）"
        );
    }
}

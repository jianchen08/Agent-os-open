//! 插件加载器实现
//!
//! 实现双根扫描、manifest 解析校验、按需加载。
//!
//! [来源: docs/tasks/task_05_plugin_system.md AC-04-1/AC-04-2]

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use agentos_core::traits::{LoadedPlugin, PluginLoader, PluginManifest, PluginStatus, PluginType};
use async_trait::async_trait;
use parking_lot::RwLock;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tracing::{info, warn};

use crate::error::LoaderError;

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
/// 对应 `config/system/plugin_allowlist.yaml`。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct AllowlistConfig {
    /// 白名单模式（默认 permissive）。
    #[serde(default)]
    pub mode: AllowlistMode,
    /// 白名单条目列表。
    #[serde(default)]
    pub plugins: Vec<AllowlistEntry>,
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
    /// 已发现的 manifest 缓存 {plugin_id: (manifest, source_path)}
    manifests: RwLock<HashMap<String, (PluginManifest, PathBuf)>>,
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
    /// 白名单应由应用启动代码加载可信配置（如 `config/system/plugin_allowlist.yaml`）
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
            let manifest: PluginManifest = match serde_json::from_str::<PluginManifest>(&content) {
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
            if let Err(e) = self.validate_manifest_internal(&manifest, &manifest_path) {
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
        manifest: &PluginManifest,
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

        // 组合插件 entry 可为空（ADR ⑥）
        if manifest.plugin_type != PluginType::Composite && manifest.entry.is_empty() {
            return Err(LoaderError::ManifestValidation {
                plugin_id: manifest.id.clone(),
                reason: "entry is required for non-composite plugins".to_string(),
            });
        }

        // host_type 校验（ADR ⑧: 所有插件支持双路径，但必须声明一个）
        // host_type 已是必填字段，serde 会校验

        // ── P2-2 插件准入校验（白名单 + SHA256）──
        self.validate_allowlist(manifest, source_path)?;

        // ── GAP-4：env target 声明闭环——mcp 端点的 ${VAR} 引用（无默认值
        // 语法）必须被 config_files[target=env].fields 覆盖，漂移启动期暴露
        // （新插件带 key 接入只改 manifest，内核/前端零改动的对称代价）──
        validate_env_field_coverage(manifest)?;

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

        // 扫描 root_paths（外部传入的路径）
        for root_str in root_paths {
            let root = Path::new(root_str);
            match self.scan_root(root) {
                Ok(found) => {
                    for (manifest, path) in found {
                        all_manifests.insert(manifest.id.clone(), (manifest, path));
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
        // D.6 槽位拆分（2026-08-15 落地）：capabilities.tools = LLM 工具（声明即
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

        // 更新缓存
        let mut cache = self.manifests.write();
        cache.clear();
        for (id, (manifest, path)) in &all_manifests {
            cache.insert(id.clone(), (manifest.clone(), path.clone()));
        }

        Ok(all_manifests.into_values().map(|(m, _)| m).collect())
    }

    /// 验证 manifest 是否符合 Schema。
    fn validate_manifest(
        &self,
        manifest: &PluginManifest,
    ) -> Result<(), agentos_core::types::PluginError> {
        self.validate_manifest_internal(manifest, Path::new("(runtime)"))
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
        // 检查是否已加载
        {
            let loaded = self.loaded.read();
            if let Some(plugin) = loaded.get(plugin_id) {
                return Ok(plugin.clone());
            }
        }

        // 查找 manifest
        let manifest = {
            let manifests = self.manifests.read();
            manifests
                .get(plugin_id)
                .map(|(m, _)| m.clone())
                .ok_or_else(|| agentos_core::types::PluginError {
                    message: format!("plugin not found: {}", plugin_id),
                    code: Some("PLUGIN_NOT_FOUND".to_string()),
                    source: Some("plugin-loader".to_string()),
                })?
        };

        // 组合插件不实例化（ADR ⑥），只标记为 Active
        let loaded_plugin = LoadedPlugin {
            manifest: manifest.clone(),
            status: PluginStatus::Active,
            loaded_at: Some(chrono::Utc::now()),
        };

        // 更新缓存
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

        self.collect_yaml_configs(config_root, &mut config_map)
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

        let config_keys: Vec<String> = config_map.keys().cloned().collect();
        info!(
            "Loaded {} config entries from {}: [{}]",
            config_map.len(),
            config_root.display(),
            config_keys.join(", ")
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
            .and_then(|(_, path)| path.parent().map(|p| p.to_string_lossy().to_string()))
    }

    /// 获取指定插件的 manifest（同步读缓存）。
    ///
    /// 供内核同步查询插件声明的运行时属性（如 lifecycle 空闲卸载阈值）。
    fn get_manifest(&self, plugin_id: &str) -> Option<PluginManifest> {
        let manifests = self.manifests.read();
        manifests.get(plugin_id).map(|(m, _)| m.clone())
    }
}

impl PluginLoaderImpl {
    /// 获取已发现的所有 manifest。
    pub fn get_manifests(&self) -> Vec<PluginManifest> {
        let manifests = self.manifests.read();
        manifests.values().map(|(m, _)| m.clone()).collect()
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
            .filter_map(|(id, (_, path))| path.parent().map(|p| (id.clone(), p.to_path_buf())))
            .collect()
    }

    /// 递归扫描目录下的所有 YAML 文件，解析并合并到 config_map。
    ///
    /// 文件名（不含 `.yaml`/`.yml` 扩展名）作为 key，
    /// 文件内容解析后的 JSON Value 作为 value。
    /// 子目录名也会作为嵌套 key 被收录（子目录下的文件合并到该 key 对应的子对象中）。
    ///
    /// **这是插件配置注入的唯一权威路径**（invoker → sidecar）。
    /// 注意与 config crate 的 `ConfigLoader::load_all`（非递归、仅顶层、
    /// 不在注入路径上）和 0.1 `src/config/loader.py::load_all`（同样非递归、
    /// 服务于 0.1 自身）区分——后两者是镜像移植 / 旧路径，不要用于注入。
    #[allow(clippy::only_used_in_recursion)]
    fn collect_yaml_configs(
        &self,
        dir: &Path,
        config_map: &mut serde_json::Map<String, serde_json::Value>,
    ) -> Result<(), LoaderError> {
        let entries = std::fs::read_dir(dir).map_err(|e| LoaderError::Io {
            message: format!("Failed to read config dir {}: {}", dir.display(), e),
        })?;

        for entry in entries.flatten() {
            let path = entry.path();

            if path.is_dir() {
                // 递归处理子目录
                let dir_name = path
                    .file_name()
                    .map(|n| n.to_string_lossy().to_string())
                    .unwrap_or_default();

                let mut sub_map = serde_json::Map::new();
                self.collect_yaml_configs(&path, &mut sub_map)?;

                if !sub_map.is_empty() {
                    config_map.insert(dir_name, serde_json::Value::Object(sub_map));
                }
            } else if path.is_file() {
                // 跳过隐藏文件(`.` 前缀)——Unix 惯例隐藏文件是元数据/文档,
                // 不是常规配置。真实用例:.agent_template_spec.yaml 是 Agent 配置
                // 规范文档(含 {placeholder}: 占位符 key),不是可执行配置。
                let is_hidden = path
                    .file_name()
                    .map(|n| n.to_string_lossy().starts_with('.'))
                    .unwrap_or(false);
                if is_hidden {
                    continue;
                }

                // 只处理 .yaml 和 .yml 文件
                let ext = path.extension().map(|e| e.to_string_lossy().to_string());
                if ext.as_deref() != Some("yaml") && ext.as_deref() != Some("yml") {
                    continue;
                }

                let stem = path
                    .file_stem()
                    .map(|n| n.to_string_lossy().to_string())
                    .unwrap_or_default();

                let content = std::fs::read_to_string(&path).map_err(|e| LoaderError::Io {
                    message: format!("Failed to read config file {}: {}", path.display(), e),
                })?;

                // YAML → JSON Value（serde_yaml 直接反序列化到 serde_json::Value）。
                // 单个文件解析失败时跳过该文件并 warn,不让整体 load_config 失败——
                // full_config 只是中间字典,真正注入靠 config_files[].path 精确定位;
                // 一个无关文件(如模板文档)解析失败不该连累全部插件收不到配置。
                let yaml_value: serde_json::Value = match serde_yaml::from_str(&content) {
                    Ok(v) => v,
                    Err(e) => {
                        warn!("Skipping unparseable config file {}: {}", path.display(), e);
                        continue;
                    }
                };

                config_map.insert(stem, yaml_value);
            }
        }

        Ok(())
    }
}

/// GAP-4 校验闭环：manifest 里 mcp.endpoint 的 `${VAR}` 引用（无 `:-` 默认值）
/// 必须被某个 `target: "env"` 的 config_files 条目的 fields 覆盖。
///
/// 覆盖了才能在设置页配置——否则 key 无入口，connect 硬失败且用户无处可填
/// （e2e 缺口 GAP-4 的病根）。`${VAR:-def}` 带默认值的引用豁免（可选凭据）。
fn validate_env_field_coverage(manifest: &PluginManifest) -> Result<(), LoaderError> {
    let Some(mcp) = manifest.mcp.as_ref() else {
        return Ok(());
    };
    let Some(endpoint) = mcp.endpoint.as_ref() else {
        return Ok(());
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
        return Ok(());
    }
    let declared: std::collections::HashSet<&str> = manifest
        .config_files
        .iter()
        .filter(|f| f.target.as_deref() == Some("env"))
        .flat_map(|f| f.fields.iter().map(|fd| fd.name.as_str()))
        .collect();
    let missing: Vec<&String> = refs.iter().filter(|r| !declared.contains(r.as_str())).collect();
    if missing.is_empty() {
        return Ok(());
    }
    Err(LoaderError::ManifestValidation {
        plugin_id: manifest.id.clone(),
        reason: format!(
            "mcp endpoint references undeclared env vars {:?}: declare them in a              config_files[target=\"env\"] fields entry so the settings page can manage them",
            missing
        ),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::traits::{HostType, ProvidedCapabilityHost};
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
    "dependencies": [],
    "permissions": {{}},
    "priority": 100{}
}}"#,
            id, id, plugin_type, invoke_entry_field
        );
        fs::write(dir.join("plugin.json"), manifest_json).unwrap();
    }

    /// Phase 1 契约定型（2026-08-18）：manifest 未知字段从"静默忽略"改为
    /// `deny_unknown_fields` 拒绝——历史遗留 `capabilities.resources`（结构已删）
    /// 是 82 个真实插件曾携带的死声明，扫描后已全量清除；本测试断言未知字段
    /// 现在**拒绝**（fail-closed），不再容忍"声明了却不生效"。
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
    "dependencies": [],
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
    /// 这是"校验器不要在真实数据上空转"的持续闸门——它第一次在真实语料上运行时
    /// 抓到的硬伤（已全量迁移）：
    /// - 38 个插件顶层 `description`：struct 无此字段，serde 静默丢弃 → 现已入 struct；
    /// - 82 个插件 `capabilities.resources`：结构已删的遗留字段 → 已清除；
    /// - approval 的 `capabilities_required`：非契约字段 → 已清除。
    /// 若此后熟悉语料出现未知字段/缺必填，本测试即红灯（校验器真实生效）。
    #[test]
    fn real_corpus_all_manifests_pass_strict_parsing() {
        let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let shared = manifest_dir.join("../../../plugins/shared");
        let mut count = 0usize;
        let mut walk = Vec::new();
        fn collect(dir: &std::path::Path, out: &mut Vec<std::path::PathBuf>) {
            // 跳过第三方/运行时产物目录：管理面只管插件声明，node_modules 下也有
            // 大量 widget_demo/dsh 依赖的 plugin.json（非本仓插件）。
            const SKIP: &[&str] = &["node_modules", ".venv", "__pycache__", "dsh_plugins", "runtime"];
            let mut entries = match std::fs::read_dir(dir) {
                Ok(e) => e,
                Err(e) => {
                    panic!("无法读取 {}: {e}", dir.display());
                }
            };
            while let Some(entry) = entries.next() {
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
        assert!(walk.len() > 50, "应扫到真实插件语料 >50，实际 {}", walk.len());
        for path in &walk {
            let text = std::fs::read_to_string(&path)
                .unwrap_or_else(|e| panic!("读取 {} 失败: {e}", path.display()));
            let m: PluginManifest = serde_json::from_str(&text).unwrap_or_else(|e| {
                panic!("真实语料 {} 未通过严格解析（校验器抓到未知字段/坏结构）: {e}", path.display());
            });
            assert!(!m.id.is_empty() && !m.name.is_empty() && !m.version.is_empty());
            count += 1;
        }
        assert!(count == walk.len(), "每份真实 manifest 都过严格解析");
    }

    #[test]
    fn test_manifest_validation_valid() {
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let manifest = PluginManifest {
            id: "test".to_string(),
            name: "Test".to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            pipeline_role: None,
            language: "rust".to_string(),
            host_type: HostType::InProcess,
            entry: "test_entry".to_string(),
            capabilities: Default::default(),
            dependencies: vec![],
            permissions: Default::default(),
            error_policy: Default::default(),
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
        };
        assert!(loader.validate_manifest(&manifest).is_ok());
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
            id: String::new(),
            name: "Test".to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            pipeline_role: None,
            language: "rust".to_string(),
            host_type: HostType::InProcess,
            entry: "test_entry".to_string(),
            capabilities: Default::default(),
            dependencies: vec![],
            permissions: Default::default(),
            error_policy: Default::default(),
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
        };
        assert!(loader.validate_manifest(&manifest).is_err());
    }

    #[test]
    fn test_manifest_validation_composite_empty_entry_ok() {
        // ADR ⑥: 组合插件 entry 可为空
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let manifest = PluginManifest {
            id: "composite_test".to_string(),
            name: "Composite".to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Composite,
            pipeline_role: None,
            language: "yaml".to_string(),
            host_type: HostType::InProcess,
            entry: String::new(), // 空entry
            capabilities: Default::default(),
            dependencies: vec![],
            permissions: Default::default(),
            error_policy: Default::default(),
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
            id: "memory_read".to_string(),
            name: "Memory Read".to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            pipeline_role: None,
            language: "rust".to_string(),
            host_type: HostType::InProcess,
            entry: "memory_read".to_string(),
            capabilities: Default::default(),
            dependencies: vec![],
            permissions: Default::default(),
            error_policy: Default::default(),
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
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let config = loader.load_config().await.unwrap();
        assert_eq!(config, serde_json::json!({}));
    }

    #[tokio::test]
    async fn test_load_config_nonexistent_dir_returns_empty() {
        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root("/tmp/no_such_dir");
        let config = loader.load_config().await.unwrap();
        assert_eq!(config, serde_json::json!({}));
    }

    #[tokio::test]
    async fn test_load_config_reads_yaml_files() {
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
        let config_dir = tempfile::tempdir().unwrap();
        // 空目录

        let loader =
            PluginLoaderImpl::new("/tmp/nonexistent", None).with_config_root(config_dir.path());

        let config = loader.load_config().await.unwrap();
        assert_eq!(config, serde_json::json!({}));
    }

    #[tokio::test]
    async fn test_load_config_deep_nested_dirs() {
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

    /// 隐藏文件(以 `.` 开头)不应被当作配置加载——
    /// Unix 惯例隐藏文件是元数据/文档,不是常规配置。
    /// 真实用例:.agent_template_spec.yaml 是 Agent 配置规范文档。
    #[tokio::test]
    async fn test_load_config_skips_hidden_files() {
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
    "dependencies": [],
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

    // ── GAP-4：env target 声明闭环 ──────────────────────────────

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
            "plugin_type": "tool", "language": "external", "host_type": "sidecar", "entry": "mcp:external", "capabilities": {{}}, "dependencies": [], "permissions": {{}}, "priority": 30,
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
    fn test_env_coverage_rejects_undeclared_var() {
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let manifest: PluginManifest =
            serde_json::from_str(&env_coverage_manifest_json(false)).unwrap();
        let err = loader.validate_manifest(&manifest);
        assert!(err.is_err(), "未声明的 env var 引用应被拒：{err:?}");
    }

    #[test]
    fn test_env_coverage_accepts_declared_var() {
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let manifest: PluginManifest =
            serde_json::from_str(&env_coverage_manifest_json(true)).unwrap();
        let r = loader.validate_manifest(&manifest);
        assert!(r.is_ok(), "声明覆盖应通过：{r:?}");
    }

    #[test]
    fn test_env_coverage_default_syntax_exempt() {
        // ${VAR:-def} 带默认值（可选凭据）豁免声明要求
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let json = env_coverage_manifest_json(false).replace(
            "${SMITHERY_API_KEY}",
            "${OPTIONAL_KEY:-}",
        );
        let manifest: PluginManifest = serde_json::from_str(&json).unwrap();
        assert!(loader.validate_manifest(&manifest).is_ok());
    }

}

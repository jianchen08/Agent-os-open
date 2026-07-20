//! 插件加载器实现
//!
//! 实现双根扫描、manifest 解析校验、按需加载。
//!
//! [来源: docs/tasks/task_05_plugin_system.md AC-04-1/AC-04-2]

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use async_trait::async_trait;
use lingxi_core::traits::{LoadedPlugin, PluginLoader, PluginManifest, PluginStatus, PluginType};
use parking_lot::RwLock;
use tracing::{info, warn};

use crate::error::LoaderError;

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

            // 解析 manifest
            let manifest: PluginManifest = serde_json::from_str(&content).or_else(|_| {
                serde_yaml::from_str(&content).map_err(|e| LoaderError::ManifestParse {
                    path: manifest_path.to_string_lossy().to_string(),
                    message: e.to_string(),
                })
            })?;

            // 校验 manifest
            self.validate_manifest_internal(&manifest, &manifest_path)?;

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

        info!(
            "Manifest validated: id={} type={:?} host={:?} path={}",
            manifest.id,
            manifest.plugin_type,
            manifest.host_type,
            source_path.display()
        );

        Ok(())
    }
}

#[async_trait]
impl PluginLoader for PluginLoaderImpl {
    /// 扫描指定根目录，发现所有 plugin.json manifest。
    ///
    /// 双根扫描：先扫内置根，再扫用户根。用户根的插件优先级高于内置根（同 ID 覆盖）。
    async fn discover(
        &self,
        root_paths: &[&str],
    ) -> Result<Vec<PluginManifest>, lingxi_core::types::PluginError> {
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

        // 扫描内置根
        if let Ok(found) = self.scan_root(&self.builtin_root) {
            for (manifest, path) in found {
                all_manifests
                    .entry(manifest.id.clone())
                    .or_insert((manifest, path));
            }
        }

        // 扫描用户根
        if let Some(user_root) = &self.user_root {
            if let Ok(found) = self.scan_root(user_root) {
                for (manifest, path) in found {
                    // 用户根覆盖内置根（同 ID）
                    all_manifests.insert(manifest.id.clone(), (manifest, path));
                }
            }
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
    ) -> Result<(), lingxi_core::types::PluginError> {
        self.validate_manifest_internal(manifest, Path::new("(runtime)"))
            .map_err(|e| lingxi_core::types::PluginError {
                message: e.to_string(),
                code: Some("MANIFEST_VALIDATION".to_string()),
                source: Some("plugin-loader".to_string()),
            })
    }

    /// 按需加载（实例化）指定插件。
    ///
    /// 如果插件已加载则直接返回；如果未加载则首次实例化。
    /// 按需加载原则：首次被调用时才启动 MCP 边车进程（非预启动）。
    async fn load(&self, plugin_id: &str) -> Result<LoadedPlugin, lingxi_core::types::PluginError> {
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
                .ok_or_else(|| lingxi_core::types::PluginError {
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
    async fn unload(&self, plugin_id: &str) -> Result<(), lingxi_core::types::PluginError> {
        let mut loaded = self.loaded.write();
        if let Some(mut plugin) = loaded.remove(plugin_id) {
            plugin.status = PluginStatus::Unloaded;
            info!("Plugin unloaded: id={}", plugin_id);
            Ok(())
        } else {
            Err(lingxi_core::types::PluginError {
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
    async fn load_config(&self) -> Result<serde_json::Value, lingxi_core::types::PluginError> {
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
                lingxi_core::types::PluginError {
                    message: format!("Failed to load config from {}: {}", config_root.display(), e),
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
}

impl PluginLoaderImpl {
    /// 获取已发现的所有 manifest。
    pub fn get_manifests(&self) -> Vec<PluginManifest> {
        let manifests = self.manifests.read();
        manifests.values().map(|(m, _)| m.clone()).collect()
    }

    /// 获取指定插件的 manifest。
    pub fn get_manifest(&self, plugin_id: &str) -> Option<PluginManifest> {
        let manifests = self.manifests.read();
        manifests.get(plugin_id).map(|(m, _)| m.clone())
    }

    /// 递归扫描目录下的所有 YAML 文件，解析并合并到 config_map。
    ///
    /// 文件名（不含 `.yaml`/`.yml` 扩展名）作为 key，
    /// 文件内容解析后的 JSON Value 作为 value。
    /// 子目录名也会作为嵌套 key 被收录（子目录下的文件合并到该 key 对应的子对象中）。
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

                // YAML → JSON Value（serde_yaml 直接反序列化到 serde_json::Value）
                let yaml_value: serde_json::Value =
                    serde_yaml::from_str(&content).map_err(|e| LoaderError::ManifestParse {
                        path: path.to_string_lossy().to_string(),
                        message: e.to_string(),
                    })?;

                config_map.insert(stem, yaml_value);
            }
        }

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use lingxi_core::traits::HostType;
    use std::fs;

    fn create_test_plugin_dir(root: &Path, id: &str, plugin_type: &str) {
        let dir = root.join(id);
        fs::create_dir_all(&dir).unwrap();
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
    "error_policy": "abort",
    "priority": 100
}}"#,
            id, id, plugin_type
        );
        fs::write(dir.join("plugin.json"), manifest_json).unwrap();
    }

    #[test]
    fn test_manifest_validation_valid() {
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let manifest = PluginManifest {
            id: "test".to_string(),
            name: "Test".to_string(),
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
            requires_content: None,
        };
        assert!(loader.validate_manifest(&manifest).is_ok());
    }

    #[test]
    fn test_manifest_validation_missing_id() {
        let loader = PluginLoaderImpl::new("/tmp/nonexistent", None);
        let manifest = PluginManifest {
            id: String::new(),
            name: "Test".to_string(),
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
            requires_content: None,
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
            requires_content: None,
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
            requires_content: Some(2),
        };
        assert!(loader.validate_manifest(&manifest).is_ok());
        assert_eq!(manifest.requires_content, Some(2));
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

        fs::write(
            config_dir.path().join("config.yaml"),
            "key: value\n",
        )
        .unwrap();
        fs::write(
            config_dir.path().join("readme.md"),
            "# Not a config\n",
        )
        .unwrap();
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

        fs::write(
            config_dir.path().join("short.yml"),
            "name: test\n",
        )
        .unwrap();

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
        let deep_dir = config_dir
            .path()
            .join("system")
            .join("subsystem");
        fs::create_dir_all(&deep_dir).unwrap();

        fs::write(
            config_dir.path().join("root_config.yaml"),
            "root_key: root_value\n",
        )
        .unwrap();
        fs::write(
            deep_dir.join("deep_config.yaml"),
            "deep_key: deep_value\n",
        )
        .unwrap();

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
        fs::write(
            config_dir.path().join("config.yaml"),
            "from: root\n",
        )
        .unwrap();
        // 子目录也有 config.yaml
        fs::write(
            sub_dir.join("config.yaml"),
            "from: sub\n",
        )
        .unwrap();

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

        fs::write(
            config_dir.path().join("config.yaml"),
            "key: value\n",
        )
        .unwrap();

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

        fs::write(
            config_dir.path().join("long.yaml"),
            "type: yaml\n",
        )
        .unwrap();
        fs::write(
            config_dir.path().join("short.yml"),
            "type: yml\n",
        )
        .unwrap();

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
}

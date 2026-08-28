//! 配置中心（热重载）
//!
//! 对应 0.1 的 `src/config/config_center.py ConfigCenter`。
//! 基于 notify crate 实现文件监听，支持 500ms 防抖 + 内容哈希去重。
//!
//! ⚠️ **push 模式未接线**：`start_watching()`/notify 推送未被任何调用方启用；
//! pull 模式（`load` 的 mtime 缓存）已由 engine 在管道循环按迭代调用。
//!
//! [来源: src/config/config_center.py]

use crate::error::ConfigError;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tracing::{info, warn};

/// 防抖窗口（500ms）
const DEBOUNCE_DURATION: Duration = Duration::from_millis(500);

/// 审计日志最大条数
const MAX_AUDIT_HISTORY: usize = 500;

/// 热重载事件类型
#[derive(Debug, Clone, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ConfigEventType {
    Created,
    Modified,
    Deleted,
    ManualReload,
}

/// 热重载审计记录
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct AuditEntry {
    pub file_path: String,
    pub event_type: ConfigEventType,
    pub config_type: String,
    pub success: bool,
    pub rolled_back: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    pub timestamp: String,
    pub content_hash: String,
}

/// 配置变更事件（传给回调函数）
#[derive(Debug, Clone)]
pub struct ConfigChangeEvent {
    pub event_type: ConfigEventType,
    pub file_path: String,
    pub config_type: String,
    pub content_hash: String,
}

/// 配置中心 — 热重载统一入口。
///
/// 职责：
/// 1. 基于 notify 监听 config/ 目录变更
/// 2. 500ms 防抖 + 内容哈希去重
/// 3. 读写锁保证并发安全
/// 4. 加载失败时保留旧配置 + 审计日志
/// 5. 暴露 watch / reload / get 接口
pub struct ConfigCenter {
    config_root: PathBuf,
    debounce: Duration,
    content_hashes: Arc<RwLock<HashMap<String, String>>>,
    config_cache: Arc<RwLock<HashMap<String, serde_json::Value>>>,
    debounce_state: Arc<RwLock<HashMap<String, Instant>>>,
    audit_log: Arc<RwLock<Vec<AuditEntry>>>,
    /// load() 的 mtime 缓存：path → 文件 mtime（pull 模式变更检测依据）
    mtime_cache: Arc<RwLock<HashMap<String, std::time::SystemTime>>>,
}

use parking_lot::RwLock;

impl ConfigCenter {
    /// 创建配置中心。
    pub fn new(config_root: impl Into<PathBuf>) -> Self {
        Self {
            config_root: config_root.into(),
            debounce: DEBOUNCE_DURATION,
            content_hashes: Arc::new(RwLock::new(HashMap::new())),
            config_cache: Arc::new(RwLock::new(HashMap::new())),
            debounce_state: Arc::new(RwLock::new(HashMap::new())),
            audit_log: Arc::new(RwLock::new(Vec::new())),
            mtime_cache: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    /// 创建配置中心（自定义防抖窗口）。
    pub fn with_debounce(config_root: impl Into<PathBuf>, debounce: Duration) -> Self {
        Self {
            config_root: config_root.into(),
            debounce,
            content_hashes: Arc::new(RwLock::new(HashMap::new())),
            config_cache: Arc::new(RwLock::new(HashMap::new())),
            debounce_state: Arc::new(RwLock::new(HashMap::new())),
            audit_log: Arc::new(RwLock::new(Vec::new())),
            mtime_cache: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    /// 计算内容的 SHA256 哈希。
    fn compute_hash(content: &str) -> String {
        let mut hasher = Sha256::new();
        hasher.update(content.as_bytes());
        format!("{:x}", hasher.finalize())
    }

    /// 防抖检查：同一文件在防抖窗口内的重复事件被过滤。
    #[cfg(test)]
    fn check_debounce(&self, path_str: &str) -> bool {
        check_debounce_impl(&self.debounce_state, path_str, self.debounce)
    }

    /// 根据文件路径判断配置类型。
    pub fn determine_config_type(file_path: &str) -> String {
        let path = Path::new(file_path);
        for part in path.components() {
            let lower = part.as_os_str().to_string_lossy().to_lowercase();
            match lower.as_str() {
                "agents" => return "agent".to_string(),
                "pipelines" => return "pipeline".to_string(),
                "tools" => return "tool".to_string(),
                "models" => return "model".to_string(),
                "templates" => return "template".to_string(),
                "triggers" => return "trigger".to_string(),
                "evaluation_metrics" => return "evaluation_metric".to_string(),
                "evaluation" => return "evaluation".to_string(),
                "isolation" => return "isolation".to_string(),
                "modules" => return "module".to_string(),
                "system" => return "system".to_string(),
                "rules" => return "rules".to_string(),
                _ => {}
            }
        }
        "unknown".to_string()
    }

    /// Pull 模式带缓存加载：mtime 变了才重读磁盘，否则返回缓存。
    ///
    /// 这是统一配置加载的**核心入口**（设计文档决策 1+3）。
    /// 调用方决定调用频率（per-run / per-iteration），本方法保证：
    /// - mtime 不变 → 返回缓存（不读磁盘）
    /// - mtime 变了 → 重读 + 解析 + 刷新缓存 + 审计
    /// - 解析失败 → 保留旧缓存（失败回滚），返回 Err
    ///
    /// 内部复用 `reload()` 的缓存写入逻辑，保证缓存/审计/回滚一致。
    pub fn load(&self, path: &str) -> Result<serde_json::Value, ConfigError> {
        let abs_path = if Path::new(path).is_absolute() {
            PathBuf::from(path)
        } else {
            self.config_root.join(path)
        };
        let path_str = abs_path.to_string_lossy().to_string();

        // ① 先查缓存：若缓存存在且 mtime 一致，直接返回（即使文件已被删除）
        //    —— 这是"缓存命中不读磁盘"的核心契约（test_load_returns_cache_when_mtime_unchanged）
        if let Some(cached) = self.config_cache.read().get(&path_str) {
            let cached_mtime = self.last_known_mtime(&path_str);
            let disk_mtime = abs_path.metadata().and_then(|m| m.modified()).ok();
            match disk_mtime {
                Some(m) if m == cached_mtime => {
                    return Ok(cached.clone()); // mtime 一致，缓存命中
                }
                None if cached_mtime != std::time::UNIX_EPOCH => {
                    return Ok(cached.clone()); // 文件已删但缓存还在，返回缓存
                }
                _ => {} // mtime 变了或首次加载，继续走 reload
            }
        }

        if !abs_path.exists() {
            return Err(ConfigError::NotFound { path: path_str });
        }

        let current_mtime = abs_path
            .metadata()
            .and_then(|m| m.modified())
            .map_err(|e| ConfigError::Io {
                message: e.to_string(),
            })?;

        // ② 需要重读：调 reload（复用其缓存写入 + 审计 + 回滚）
        let (ok, _rolled_back, err) = self.reload(&path_str);
        if !ok {
            return Err(ConfigError::Io {
                message: err.unwrap_or_else(|| "reload failed".to_string()),
            });
        }
        self.mtime_cache
            .write()
            .insert(path_str.clone(), current_mtime);

        self.config_cache
            .read()
            .get(&path_str)
            .cloned()
            .ok_or(ConfigError::NotFound { path: path_str })
    }

    /// 取缓存中记录的 mtime（用于 load 的变更检测）。
    fn last_known_mtime(&self, path_str: &str) -> std::time::SystemTime {
        // 无记录视为 epoch（一定不等于真实 mtime，触发重读）
        self.mtime_cache
            .read()
            .get(path_str)
            .copied()
            .unwrap_or(std::time::UNIX_EPOCH)
    }

    /// 原子写入配置文件 + 刷新缓存。
    ///
    /// 写入流程：建父目录 → 写 `.tmp` → `rename` 原子替换 → 更新缓存。
    /// 原子写避免 reader（load）读到半写文件。
    /// 写后 load() 能立即读到新内容（缓存已刷新到新 mtime）。
    pub fn store(&self, path: &str, content: &str) -> Result<(), ConfigError> {
        let abs_path = if Path::new(path).is_absolute() {
            PathBuf::from(path)
        } else {
            self.config_root.join(path)
        };
        let path_str = abs_path.to_string_lossy().to_string();

        // 建父目录（支持 agents/sub/deep.yaml 这种深路径）
        if let Some(parent) = abs_path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| ConfigError::Io {
                message: e.to_string(),
            })?;
        }

        // 原子写：先写隐藏 .tmp，再 rename 替换
        let tmp_path = abs_path.with_extension("yaml.tmp");
        std::fs::write(&tmp_path, content).map_err(|e| ConfigError::Io {
            message: e.to_string(),
        })?;
        std::fs::rename(&tmp_path, &abs_path).map_err(|e| ConfigError::Io {
            message: e.to_string(),
        })?;

        // 刷新缓存：直接用新内容 + 新 mtime，让 load() 立即读到
        let new_value: serde_json::Value =
            serde_yaml::from_str(content).map_err(|e| ConfigError::YamlParse {
                path: path_str.clone(),
                message: e.to_string(),
            })?;
        let new_mtime = abs_path
            .metadata()
            .and_then(|m| m.modified())
            .unwrap_or(std::time::SystemTime::now());

        {
            let mut cache = self.config_cache.write();
            cache.insert(path_str.clone(), new_value);
        }
        self.mtime_cache.write().insert(path_str.clone(), new_mtime);

        // 同步 content_hashes（reload 去重用），保持缓存三件套一致
        {
            let mut hashes = self.content_hashes.write();
            hashes.insert(path_str, Self::compute_hash(content));
        }

        Ok(())
    }

    /// 批量加载目录下所有 .yaml/.yml（递归），返回嵌套 Map。
    ///
    /// 结构对齐 `plugin-loader collect_yaml_configs`：
    ///   - 文件 → `{stem: content}`
    ///   - 子目录 → `{子目录名: {子内容}}`
    ///   - 跳过隐藏文件（`.` 前缀）和非 yaml 文件
    ///   - 单文件解析失败跳过，不连累整体
    ///
    /// 每个文件复用 `load()`（享受 mtime 缓存 + 失败回滚）。
    pub fn load_dir(
        &self,
        rel_dir: &str,
    ) -> Result<serde_json::Map<String, serde_json::Value>, ConfigError> {
        let abs_dir = if Path::new(rel_dir).is_absolute() {
            PathBuf::from(rel_dir)
        } else {
            self.config_root.join(rel_dir)
        };

        if !abs_dir.exists() {
            return Err(ConfigError::NotFound {
                path: abs_dir.to_string_lossy().to_string(),
            });
        }

        let mut result = serde_json::Map::new();
        self.collect_dir_recursive(&abs_dir, &mut result)?;
        Ok(result)
    }

    /// 递归收集目录内容到 config_map（结构对齐 collect_yaml_configs）。
    fn collect_dir_recursive(
        &self,
        dir: &Path,
        config_map: &mut serde_json::Map<String, serde_json::Value>,
    ) -> Result<(), ConfigError> {
        let entries = std::fs::read_dir(dir).map_err(|e| ConfigError::Io {
            message: e.to_string(),
        })?;

        for entry in entries.flatten() {
            let path = entry.path();

            if path.is_dir() {
                let dir_name = path
                    .file_name()
                    .map(|n| n.to_string_lossy().to_string())
                    .unwrap_or_default();
                let mut sub_map = serde_json::Map::new();
                self.collect_dir_recursive(&path, &mut sub_map)?;
                if !sub_map.is_empty() {
                    config_map.insert(dir_name, serde_json::Value::Object(sub_map));
                }
            } else if path.is_file() {
                // 跳过隐藏文件
                let is_hidden = path
                    .file_name()
                    .map(|n| n.to_string_lossy().starts_with('.'))
                    .unwrap_or(false);
                if is_hidden {
                    continue;
                }
                // 只处理 yaml/yml
                let ext = path.extension().map(|e| e.to_string_lossy().to_string());
                if ext.as_deref() != Some("yaml") && ext.as_deref() != Some("yml") {
                    continue;
                }
                let stem = path
                    .file_stem()
                    .map(|n| n.to_string_lossy().to_string())
                    .unwrap_or_default();
                // 复用 load()（mtime 缓存 + 失败回滚）；单文件失败跳过
                match self.load(&path.to_string_lossy()) {
                    Ok(v) => {
                        config_map.insert(stem, v);
                    }
                    Err(e) => {
                        warn!("Skipping unparseable config file {}: {}", path.display(), e);
                    }
                }
            }
        }
        Ok(())
    }

    /// 手动重载指定配置文件。
    ///
    /// 读取文件 → 计算哈希 → 去重检查 → 更新缓存 → 返回结果。
    /// 失败时保留旧配置。
    pub fn reload(&self, path: &str) -> (bool, bool, Option<String>) {
        let abs_path = if Path::new(path).is_absolute() {
            PathBuf::from(path)
        } else {
            self.config_root.join(path)
        };

        if !abs_path.exists() {
            return (
                false,
                false,
                Some(format!("file not found: {}", abs_path.display())),
            );
        }

        let config_type = Self::determine_config_type(&abs_path.to_string_lossy());

        let content = match std::fs::read_to_string(&abs_path) {
            Ok(c) => c,
            Err(e) => {
                return self.handle_load_failure(
                    &abs_path.to_string_lossy(),
                    ConfigEventType::ManualReload,
                    &config_type,
                    &format!("IO error: {}", e),
                );
            }
        };

        let data: serde_json::Value = match serde_yaml::from_str(&content) {
            Ok(v) => v,
            Err(e) => {
                return self.handle_load_failure(
                    &abs_path.to_string_lossy(),
                    ConfigEventType::ManualReload,
                    &config_type,
                    &format!("YAML parse error: {}", e),
                );
            }
        };

        let content_hash = Self::compute_hash(&content);
        let path_str = abs_path.to_string_lossy().to_string();

        {
            let mut hashes = self.content_hashes.write();
            if let Some(old_hash) = hashes.get(&path_str) {
                if *old_hash == content_hash {
                    info!("Config content unchanged, skip reload: {}", path_str);
                    return (true, false, None);
                }
            }

            let mut cache = self.config_cache.write();
            cache.insert(path_str.clone(), data);
            hashes.insert(path_str.clone(), content_hash.clone());
        }

        self.write_audit(AuditEntry {
            file_path: path_str,
            event_type: ConfigEventType::ManualReload,
            config_type,
            success: true,
            rolled_back: false,
            error: None,
            timestamp: chrono::Utc::now().to_rfc3339(),
            content_hash,
        });

        info!("Config reloaded successfully");
        (true, false, None)
    }

    /// 获取已缓存的配置数据。
    ///
    /// 缓存未命中时从磁盘读取并缓存。IO/YAML 错误记录日志后返回 None。
    pub fn get(&self, path: &str) -> Option<serde_json::Value> {
        let abs_path = if Path::new(path).is_absolute() {
            PathBuf::from(path)
        } else {
            self.config_root.join(path)
        };
        let path_str = abs_path.to_string_lossy().to_string();

        {
            let cache = self.config_cache.read();
            if let Some(data) = cache.get(&path_str) {
                return Some(data.clone());
            }
        }

        if !abs_path.exists() {
            return None;
        }

        let content = match std::fs::read_to_string(&abs_path) {
            Ok(c) => c,
            Err(e) => {
                warn!("Failed to read {}: {}", abs_path.display(), e);
                return None;
            }
        };

        let data: serde_json::Value = match serde_yaml::from_str(&content) {
            Ok(v) => v,
            Err(e) => {
                warn!("YAML parse error in {}: {}", abs_path.display(), e);
                return None;
            }
        };

        let content_hash = Self::compute_hash(&content);

        {
            let mut cache = self.config_cache.write();
            cache.insert(path_str.clone(), data.clone());
            let mut hashes = self.content_hashes.write();
            hashes.insert(path_str, content_hash);
        }

        Some(data)
    }

    /// 启动文件监听（基于 notify crate）。
    ///
    /// 监听 config_root 目录，检测到 YAML 文件变更时进行防抖 + 哈希去重。
    /// 返回 notify Watcher，调用方持有它以保持监听。
    pub fn start_watching(&self) -> Result<notify::RecommendedWatcher, ConfigError> {
        use notify::{Config, EventKind, RecommendedWatcher, RecursiveMode, Watcher};

        let hashes = Arc::clone(&self.content_hashes);
        let cache = Arc::clone(&self.config_cache);
        let audit_log = Arc::clone(&self.audit_log);
        let debounce_state = Arc::clone(&self.debounce_state);
        let debounce_dur = self.debounce;

        let mut watcher = RecommendedWatcher::new(
            move |res: Result<notify::Event, notify::Error>| {
                if let Ok(event) = res {
                    for path in &event.paths {
                        let path_str = path.to_string_lossy().to_string();

                        // 过滤非 YAML 文件
                        if path.extension().is_none_or(|e| e != "yaml" && e != "yml") {
                            continue;
                        }

                        // 过滤临时文件
                        let name = path
                            .file_name()
                            .map(|n| n.to_string_lossy().to_string())
                            .unwrap_or_default();
                        if name.starts_with('.') || name.starts_with('~') {
                            continue;
                        }

                        let config_type = Self::determine_config_type(&path_str);

                        // 删除事件
                        if matches!(event.kind, EventKind::Remove(_)) {
                            let mut h = hashes.write();
                            let mut c = cache.write();
                            let mut ds = debounce_state.write();
                            h.remove(&path_str);
                            c.remove(&path_str);
                            ds.remove(&path_str);

                            let mut al = audit_log.write();
                            al.push(AuditEntry {
                                file_path: path_str.clone(),
                                event_type: ConfigEventType::Deleted,
                                config_type,
                                success: true,
                                rolled_back: false,
                                error: None,
                                timestamp: chrono::Utc::now().to_rfc3339(),
                                content_hash: String::new(),
                            });
                            trim_audit(&mut al);
                            continue;
                        }

                        // 防抖检查（AC-03-4: 500ms 防抖窗口）
                        if !check_debounce_impl(&debounce_state, &path_str, debounce_dur) {
                            continue;
                        }

                        // 创建/修改事件
                        if !path.exists() {
                            continue;
                        }

                        let content = match std::fs::read_to_string(path) {
                            Ok(c) => c,
                            Err(e) => {
                                warn!("Failed to read {}: {}", path_str, e);
                                let mut al = audit_log.write();
                                al.push(AuditEntry {
                                    file_path: path_str.clone(),
                                    event_type: ConfigEventType::Modified,
                                    config_type: config_type.clone(),
                                    success: false,
                                    rolled_back: cache.read().contains_key(&path_str),
                                    error: Some(format!("IO error: {}", e)),
                                    timestamp: chrono::Utc::now().to_rfc3339(),
                                    content_hash: String::new(),
                                });
                                trim_audit(&mut al);
                                continue;
                            }
                        };

                        let new_hash = Self::compute_hash(&content);

                        // 去重
                        {
                            let h = hashes.read();
                            if let Some(old) = h.get(&path_str) {
                                if *old == new_hash {
                                    continue;
                                }
                            }
                        }

                        // 解析 — 失败时走 handle_load_failure 逻辑（保留旧配置 + 审计）
                        let data: serde_json::Value = match serde_yaml::from_str(&content) {
                            Ok(v) => v,
                            Err(e) => {
                                let error_msg = format!("YAML parse error: {}", e);
                                warn!("Config load failed: {} | {}", path_str, error_msg);
                                let rolled_back = {
                                    let c = cache.read();
                                    c.contains_key(&path_str)
                                };
                                if rolled_back {
                                    warn!("Keeping old config for: {}", path_str);
                                } else {
                                    warn!("No old config to rollback for: {}", path_str);
                                }
                                let mut al = audit_log.write();
                                al.push(AuditEntry {
                                    file_path: path_str.clone(),
                                    event_type: ConfigEventType::Modified,
                                    config_type: config_type.clone(),
                                    success: false,
                                    rolled_back,
                                    error: Some(error_msg),
                                    timestamp: chrono::Utc::now().to_rfc3339(),
                                    content_hash: new_hash,
                                });
                                trim_audit(&mut al);
                                continue;
                            }
                        };

                        // 更新缓存
                        {
                            let mut h = hashes.write();
                            let mut c = cache.write();
                            c.insert(path_str.clone(), data);
                            h.insert(path_str.clone(), new_hash.clone());
                        }

                        let event_type = if matches!(event.kind, EventKind::Create(_)) {
                            ConfigEventType::Created
                        } else {
                            ConfigEventType::Modified
                        };

                        info!(
                            "Config auto-reloaded: type={} path={}",
                            config_type, path_str
                        );

                        let mut al = audit_log.write();
                        al.push(AuditEntry {
                            file_path: path_str,
                            event_type,
                            config_type,
                            success: true,
                            rolled_back: false,
                            error: None,
                            timestamp: chrono::Utc::now().to_rfc3339(),
                            content_hash: new_hash,
                        });
                        trim_audit(&mut al);
                    }
                }
            },
            Config::default(),
        )
        .map_err(|e| ConfigError::Io {
            message: format!("notify init error: {}", e),
        })?;

        watcher
            .watch(&self.config_root, RecursiveMode::Recursive)
            .map_err(|e| ConfigError::Io {
                message: format!("notify watch error: {}", e),
            })?;

        info!(
            "ConfigCenter started watching: {}",
            self.config_root.display()
        );
        Ok(watcher)
    }

    /// 处理加载失败：保留旧配置 + 写审计日志。
    fn handle_load_failure(
        &self,
        path_str: &str,
        event_type: ConfigEventType,
        config_type: &str,
        error: &str,
    ) -> (bool, bool, Option<String>) {
        let cache = self.config_cache.read();
        let rolled_back = cache.contains_key(path_str);

        if rolled_back {
            warn!(
                "Config load failed, keeping old config: {} | {}",
                path_str, error
            );
        } else {
            warn!(
                "Config load failed (no old config to rollback): {} | {}",
                path_str, error
            );
        }

        self.write_audit(AuditEntry {
            file_path: path_str.to_string(),
            event_type,
            config_type: config_type.to_string(),
            success: false,
            rolled_back,
            error: Some(error.to_string()),
            timestamp: chrono::Utc::now().to_rfc3339(),
            content_hash: String::new(),
        });

        (false, rolled_back, Some(error.to_string()))
    }

    /// 写审计日志。
    fn write_audit(&self, entry: AuditEntry) {
        let mut log = self.audit_log.write();
        log.push(entry);
        trim_audit(&mut log);
    }

    /// 获取审计日志（最新在前）。
    pub fn get_audit_log(&self, limit: usize) -> Vec<AuditEntry> {
        let log = self.audit_log.read();
        log.iter().rev().take(limit).cloned().collect()
    }

    /// 获取配置根目录。
    pub fn config_root(&self) -> &Path {
        &self.config_root
    }
}

/// 裁剪审计日志到最大条数。
fn trim_audit(log: &mut Vec<AuditEntry>) {
    while log.len() > MAX_AUDIT_HISTORY {
        log.remove(0);
    }
}

/// 防抖检查实现（供 ConfigCenter::check_debounce 和 start_watching 回调共用）。
fn check_debounce_impl(
    debounce_state: &Arc<RwLock<HashMap<String, Instant>>>,
    path_str: &str,
    debounce: Duration,
) -> bool {
    let now = Instant::now();
    let mut state = debounce_state.write();
    if let Some(&last) = state.get(path_str) {
        if now.duration_since(last) < debounce {
            return false;
        }
    }
    state.insert(path_str.to_string(), now);
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_determine_config_type() {
        assert_eq!(
            ConfigCenter::determine_config_type("/app/config/agents/test.yaml"),
            "agent"
        );
        assert_eq!(
            ConfigCenter::determine_config_type("/app/config/pipelines/default.yaml"),
            "pipeline"
        );
        assert_eq!(
            ConfigCenter::determine_config_type("/app/config/tools/xxx.yaml"),
            "tool"
        );
        assert_eq!(
            ConfigCenter::determine_config_type("/app/config/models/gpt.yaml"),
            "model"
        );
        assert_eq!(
            ConfigCenter::determine_config_type("/app/random/foo.yaml"),
            "unknown"
        );
    }

    #[test]
    fn test_content_hash_dedup() {
        let temp = tempfile::tempdir().unwrap();
        let file_path = temp.path().join("test.yaml");
        std::fs::write(&file_path, "key: value\n").unwrap();

        let center = ConfigCenter::new(temp.path());
        let path_str = file_path.to_string_lossy().to_string();

        let (ok1, _, _) = center.reload(&path_str);
        assert!(ok1);

        let (ok2, _, _) = center.reload(&path_str);
        assert!(ok2);

        std::fs::write(&file_path, "key: new_value\n").unwrap();
        let (ok3, _, _) = center.reload(&path_str);
        assert!(ok3);
    }

    #[test]
    fn test_reload_failure_rollback() {
        let temp = tempfile::tempdir().unwrap();
        let file_path = temp.path().join("bad.yaml");

        let center = ConfigCenter::new(temp.path());

        std::fs::write(&file_path, "key: value\n").unwrap();
        let _ = center.reload(&file_path.to_string_lossy());

        std::fs::write(&file_path, "key: [invalid\n").unwrap();
        let (ok, rolled_back, _) = center.reload(&file_path.to_string_lossy());
        assert!(!ok);
        assert!(rolled_back);

        let cached = center.get(&file_path.to_string_lossy());
        assert!(cached.is_some());
    }

    #[test]
    fn test_get_from_cache_or_disk() {
        let temp = tempfile::tempdir().unwrap();
        let file_path = temp.path().join("getable.yaml");
        std::fs::write(&file_path, "foo: bar\n").unwrap();

        let center = ConfigCenter::new(temp.path());
        let data = center.get(&file_path.to_string_lossy());
        assert!(data.is_some());
        assert_eq!(data.unwrap()["foo"], "bar");
    }

    #[test]
    fn test_audit_log() {
        let temp = tempfile::tempdir().unwrap();
        let file_path = temp.path().join("audit_test.yaml");
        std::fs::write(&file_path, "key: value\n").unwrap();

        let center = ConfigCenter::new(temp.path());
        let _ = center.reload(&file_path.to_string_lossy());

        let log = center.get_audit_log(10);
        assert_eq!(log.len(), 1);
        assert!(log[0].success);
        assert_eq!(log[0].config_type, "unknown");
    }

    #[test]
    fn test_debounce_check() {
        let center = ConfigCenter::new("/tmp");
        let path = "/tmp/test_debounce.yaml";

        assert!(center.check_debounce(path));
        assert!(!center.check_debounce(path));

        std::thread::sleep(Duration::from_millis(600));
        assert!(center.check_debounce(path));
    }

    // ════════════════════════════════════════════════════════════════
    // TDD-1: load() pull 模式核心方法测试
    // 设计依据：docs/working/重要设计/统一配置加载方案.md 决策 1+3
    // ════════════════════════════════════════════════════════════════

    #[test]
    fn test_load_reads_file_first_time() {
        // 契约：首次调用读文件，返回解析后的 YAML 内容
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("cfg.yaml"), "foo: bar\nnum: 42\n").unwrap();

        let cc = ConfigCenter::new(temp.path());
        let val = cc.load("cfg.yaml").expect("首次加载应成功");

        assert_eq!(val["foo"], "bar");
        assert_eq!(val["num"], 42);
    }

    #[test]
    fn test_load_returns_cache_when_mtime_unchanged() {
        // 契约：mtime 不变时，第二次调用返回缓存（不重读磁盘）
        // 验证方式：第二次调用后删掉文件，load 仍应成功（说明读的是缓存）
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("cfg.yaml"), "key: value\n").unwrap();

        let cc = ConfigCenter::new(temp.path());
        let _ = cc.load("cfg.yaml").unwrap();

        // 删文件，缓存应仍可用
        std::fs::remove_file(temp.path().join("cfg.yaml")).unwrap();
        let val = cc.load("cfg.yaml").expect("mtime 不变应返回缓存");
        assert_eq!(val["key"], "value");
    }

    #[test]
    fn test_load_rereads_when_mtime_changed() {
        // 契约：文件 mtime 变了（内容更新），load 重读返回新内容
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("cfg.yaml"), "v: 1\n").unwrap();

        let cc = ConfigCenter::new(temp.path());
        let v1 = cc.load("cfg.yaml").unwrap();
        assert_eq!(v1["v"], 1);

        // 改内容（确保 mtime 变化——sleep 跨过文件系统 mtime 精度）
        std::thread::sleep(std::time::Duration::from_millis(50));
        std::fs::write(temp.path().join("cfg.yaml"), "v: 2\n").unwrap();

        let v2 = cc.load("cfg.yaml").unwrap();
        assert_eq!(v2["v"], 2);
    }

    #[test]
    fn test_load_returns_err_when_file_missing() {
        // 契约：文件不存在返回 Err（ConfigError::NotFound 或 Io）
        let temp = tempfile::tempdir().unwrap();
        let cc = ConfigCenter::new(temp.path());

        let result = cc.load("nonexistent.yaml");
        assert!(result.is_err(), "文件不存在应返回 Err");
    }

    #[test]
    fn test_load_keeps_old_cache_on_parse_failure() {
        // 契约：YAML 解析失败时保留旧缓存（失败回滚），返回 Err
        // 验证：先加载合法配置 → 写入非法 YAML → load 报错但旧缓存仍在
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("cfg.yaml"), "good: yes\n").unwrap();

        let cc = ConfigCenter::new(temp.path());
        let _ = cc.load("cfg.yaml").unwrap();

        // 写入非法 YAML（确保 mtime 变化触发重读）
        std::thread::sleep(std::time::Duration::from_millis(50));
        std::fs::write(temp.path().join("cfg.yaml"), "bad: [unclosed\n").unwrap();

        let result = cc.load("cfg.yaml");
        assert!(result.is_err(), "非法 YAML 应返回 Err");

        // 旧缓存应该还在（get 能拿到旧值）—— 但 get 读的是 reload() 维护的缓存，
        // load() 应共用同一份缓存，所以解析失败后 get 应返回旧值
        let cached = cc.get("cfg.yaml");
        assert!(cached.is_some(), "解析失败应保留旧缓存");
        assert_eq!(cached.unwrap()["good"], "yes");
    }

    #[test]
    fn test_load_records_audit_on_reload() {
        // 契约：实际重读（非缓存命中）时记录审计日志
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("cfg.yaml"), "x: 1\n").unwrap();

        let cc = ConfigCenter::new(temp.path());
        let _ = cc.load("cfg.yaml").unwrap();

        let log = cc.get_audit_log(10);
        assert_eq!(log.len(), 1, "首次加载应记 1 条审计");
        assert!(log[0].success);
    }

    // ════════════════════════════════════════════════════════════════
    // TDD-2: store() 原子写测试
    // 设计依据：docs/working/重要设计/统一配置加载方案.md 决策 4（阶段 4）
    // ════════════════════════════════════════════════════════════════

    #[test]
    fn test_store_writes_content() {
        // 契约：store 写入后，文件内容正确
        let temp = tempfile::tempdir().unwrap();
        let cc = ConfigCenter::new(temp.path());

        cc.store("cfg.yaml", "foo: bar\n").expect("store 应成功");

        let content = std::fs::read_to_string(temp.path().join("cfg.yaml")).unwrap();
        assert_eq!(content, "foo: bar\n");
    }

    #[test]
    fn test_store_then_load_sees_new_content() {
        // 契约：store 后 load 能读到新内容（缓存自动失效）
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("cfg.yaml"), "v: 1\n").unwrap();
        let cc = ConfigCenter::new(temp.path());

        let _ = cc.load("cfg.yaml").unwrap();
        assert_eq!(cc.load("cfg.yaml").unwrap()["v"], 1);

        cc.store("cfg.yaml", "v: 2\n").unwrap();

        let after = cc.load("cfg.yaml").unwrap();
        assert_eq!(after["v"], 2, "store 后 load 应读到新内容");
    }

    #[test]
    fn test_store_creates_parent_dirs() {
        // 契约：store 到不存在的子目录时自动创建
        let temp = tempfile::tempdir().unwrap();
        let cc = ConfigCenter::new(temp.path());

        cc.store("agents/sub/deep.yaml", "key: val\n")
            .expect("应自动创建子目录");

        assert!(temp.path().join("agents/sub/deep.yaml").exists());
    }

    #[test]
    fn test_store_no_partial_file_on_success() {
        // 契约：原子写——成功后不留 .tmp 残留文件
        let temp = tempfile::tempdir().unwrap();
        let cc = ConfigCenter::new(temp.path());

        cc.store("cfg.yaml", "x: 1\n").unwrap();

        assert!(
            !temp.path().join("cfg.yaml.tmp").exists(),
            "不应残留 .tmp 文件"
        );
        assert!(
            !temp.path().join(".cfg.yaml.tmp").exists(),
            "不应残留隐藏 .tmp 文件"
        );
    }

    // ════════════════════════════════════════════════════════════════
    // TDD-3: load_dir() 批量加载目录测试
    // 设计依据：统一配置加载方案.md 阶段 3（plugin config_files 统一）
    // 返回结构对齐 plugin-loader collect_yaml_configs：嵌套 Map
    // ════════════════════════════════════════════════════════════════

    #[test]
    fn test_load_dir_returns_all_yaml_files() {
        // 契约：加载目录下所有 .yaml/.yml，返回 {stem: content}
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("a.yaml"), "x: 1\n").unwrap();
        std::fs::write(temp.path().join("b.yml"), "y: 2\n").unwrap();

        let cc = ConfigCenter::new(temp.path());
        let map = cc.load_dir(".").expect("load_dir 应成功");

        assert_eq!(map["a"]["x"], 1);
        assert_eq!(map["b"]["y"], 2);
    }

    #[test]
    fn test_load_dir_nested_subdirs() {
        // 契约：子目录嵌套为 {子目录名: {文件stem: content}}
        let temp = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(temp.path().join("models")).unwrap();
        std::fs::write(temp.path().join("models/llm.yaml"), "chat: gpt\n").unwrap();
        std::fs::write(temp.path().join("top.yaml"), "z: 9\n").unwrap();

        let cc = ConfigCenter::new(temp.path());
        let map = cc.load_dir(".").expect("load_dir 应成功");

        assert_eq!(map["models"]["llm"]["chat"], "gpt");
        assert_eq!(map["top"]["z"], 9);
    }

    #[test]
    fn test_load_dir_skips_non_yaml_and_hidden() {
        // 契约：跳过非 yaml 文件和隐藏文件（. 前缀）
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("cfg.yaml"), "ok: true\n").unwrap();
        std::fs::write(temp.path().join("readme.md"), "not yaml\n").unwrap();
        std::fs::write(temp.path().join(".hidden.yaml"), "hidden: true\n").unwrap();

        let cc = ConfigCenter::new(temp.path());
        let map = cc.load_dir(".").unwrap();

        assert!(map.contains_key("cfg"));
        assert!(!map.contains_key("readme"), "非 yaml 应被跳过");
        assert!(!map.contains_key(".hidden"), "隐藏文件应被跳过");
    }

    #[test]
    fn test_load_dir_missing_returns_err() {
        // 契约：目录不存在返回 Err
        let temp = tempfile::tempdir().unwrap();
        let cc = ConfigCenter::new(temp.path());

        let result = cc.load_dir("nonexistent_dir");
        assert!(result.is_err(), "目录不存在应返回 Err");
    }

    #[test]
    fn test_load_dir_skips_unparseable_file() {
        // 契约：单个文件解析失败不连累整体（跳过坏文件）
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("good.yaml"), "ok: 1\n").unwrap();
        std::fs::write(temp.path().join("bad.yaml"), "bad: [unclosed\n").unwrap();

        let cc = ConfigCenter::new(temp.path());
        let map = cc.load_dir(".").unwrap();

        assert!(map.contains_key("good"), "合法文件应正常加载");
        // bad 文件被跳过，不出现或值为空（不连累 good）
    }

    // ════════════════════════════════════════════════════════════════
    // 补充：determine_config_type 全分支 / 失败路径 / 审计裁剪 / 监听事件
    // ════════════════════════════════════════════════════════════════

    #[test]
    fn test_determine_config_type_all_branches() {
        for (path, expected) in [
            ("/c/agents/x.yaml", "agent"),
            ("/c/pipelines/x.yaml", "pipeline"),
            ("/c/tools/x.yaml", "tool"),
            ("/c/models/x.yaml", "model"),
            ("/c/templates/x.yaml", "template"),
            ("/c/triggers/x.yaml", "trigger"),
            ("/c/evaluation_metrics/x.yaml", "evaluation_metric"),
            ("/c/evaluation/x.yaml", "evaluation"),
            ("/c/isolation/x.yaml", "isolation"),
            ("/c/modules/x.yaml", "module"),
            ("/c/system/x.yaml", "system"),
            ("/c/rules/x.yaml", "rules"),
            // 大小写不敏感
            ("/c/Agents/x.yaml", "agent"),
            ("/c/PIPELINES/x.yaml", "pipeline"),
            // 无匹配 → unknown
            ("/c/random/x.yaml", "unknown"),
            ("", "unknown"),
        ] {
            assert_eq!(
                ConfigCenter::determine_config_type(path),
                expected,
                "路径 {path} 分类错误"
            );
        }
    }

    #[test]
    fn test_reload_missing_file_and_io_error() {
        let temp = tempfile::tempdir().unwrap();
        let center = ConfigCenter::new(temp.path());

        // 文件不存在 → (false, false, Some)
        let (ok, rolled_back, err) = center.reload("nonexistent.yaml");
        assert!(!ok);
        assert!(!rolled_back);
        assert!(err.is_some());

        // 目录路径 → read_to_string IO 错误 → handle_load_failure（无旧缓存）
        let dir = temp.path().join("subdir");
        std::fs::create_dir_all(&dir).unwrap();
        let (ok2, rolled_back2, err2) = center.reload(&dir.to_string_lossy());
        assert!(!ok2);
        assert!(!rolled_back2);
        assert!(err2.is_some());
        // 失败也记审计
        let log = center.get_audit_log(10);
        assert_eq!(log.len(), 1);
        assert!(!log[0].success);
    }

    #[test]
    fn test_store_invalid_yaml_returns_err_but_file_written() {
        let temp = tempfile::tempdir().unwrap();
        let cc = ConfigCenter::new(temp.path());

        let res = cc.store("bad.yaml", "k: [unclosed\n");
        assert!(res.is_err(), "非法 YAML 应返回 Err");
        // 写入发生在解析之前：文件已落盘（现状契约）
        assert!(temp.path().join("bad.yaml").exists());
    }

    #[test]
    fn test_store_and_load_absolute_path() {
        let temp = tempfile::tempdir().unwrap();
        let cc = ConfigCenter::new(temp.path());

        let abs = temp.path().join("abs.yaml");
        cc.store(&abs.to_string_lossy(), "a: 1\n").unwrap();
        assert!(abs.exists());

        let v = cc.load(&abs.to_string_lossy()).unwrap();
        assert_eq!(v["a"], 1);
    }

    #[test]
    fn test_load_missing_relative_and_absolute() {
        let temp = tempfile::tempdir().unwrap();
        let cc = ConfigCenter::new(temp.path());

        let err = cc.load("nope.yaml").unwrap_err();
        assert!(matches!(err, ConfigError::NotFound { .. }));

        let abs = temp.path().join("nope2.yaml");
        let err2 = cc.load(&abs.to_string_lossy()).unwrap_err();
        assert!(matches!(err2, ConfigError::NotFound { .. }));
    }

    #[test]
    fn test_get_none_on_missing_and_parse_error() {
        let temp = tempfile::tempdir().unwrap();
        let cc = ConfigCenter::new(temp.path());

        assert!(cc.get("missing.yaml").is_none());

        std::fs::write(temp.path().join("bad.yaml"), "k: [unclosed\n").unwrap();
        assert!(cc.get("bad.yaml").is_none(), "非法 YAML 应返回 None");
    }

    #[test]
    fn test_audit_log_trim_and_limit() {
        let temp = tempfile::tempdir().unwrap();
        let cc = ConfigCenter::new(temp.path());

        for i in 0..510 {
            cc.write_audit(AuditEntry {
                file_path: format!("f{i}"),
                event_type: ConfigEventType::ManualReload,
                config_type: "x".to_string(),
                success: true,
                rolled_back: false,
                error: None,
                timestamp: String::new(),
                content_hash: String::new(),
            });
        }
        let log = cc.get_audit_log(1000);
        assert_eq!(log.len(), 500, "审计日志应裁剪到 500 条");
        assert_eq!(log[0].file_path, "f509", "最新在前");
        assert_eq!(log[499].file_path, "f10");

        let limited = cc.get_audit_log(3);
        assert_eq!(limited.len(), 3);
        assert_eq!(limited[0].file_path, "f509");
    }

    #[test]
    fn test_load_dir_absolute_and_empty_subdir() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("a.yaml"), "x: 1\n").unwrap();
        std::fs::create_dir_all(temp.path().join("empty")).unwrap();

        let cc = ConfigCenter::new(temp.path());
        let map = cc.load_dir(&temp.path().to_string_lossy()).unwrap();
        assert_eq!(map["a"]["x"], 1);
        assert!(!map.contains_key("empty"), "空子目录不应出现在结果里");
    }

    /// 轮询等待条件成立（notify 事件异步到达）。
    fn wait_until(cond: impl Fn() -> bool, timeout: Duration) {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if cond() {
                return;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        panic!("wait_until 超时");
    }

    #[test]
    fn test_start_watching_filters_and_events() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(temp.path().join("agents")).unwrap();
        let center = ConfigCenter::new(temp.path());
        let _watcher = center.start_watching().expect("start_watching 应成功");

        // 非 yaml / 隐藏 / 临时 / 无扩展名文件 → 不产生审计
        std::fs::write(temp.path().join("note.txt"), "x").unwrap();
        std::fs::write(temp.path().join(".hidden.yaml"), "a: 1").unwrap();
        std::fs::write(temp.path().join("~tmp.yaml"), "a: 1").unwrap();
        std::fs::write(temp.path().join("noext"), "a: 1").unwrap();
        std::thread::sleep(Duration::from_millis(700));
        assert_eq!(
            center.get_audit_log(10).len(),
            0,
            "非 yaml/隐藏/临时文件不应产生审计"
        );

        // 合法 yaml 写入 → 成功审计（config_type 按目录判定）
        std::fs::write(temp.path().join("agents/hello.yaml"), "k: v\n").unwrap();
        wait_until(
            || center.get_audit_log(10).len() >= 1,
            Duration::from_secs(5),
        );
        let log = center.get_audit_log(10);
        assert!(log[0].success);
        assert_eq!(log[0].config_type, "agent");

        // 内容未变 → 哈希去重，不新增审计
        std::thread::sleep(Duration::from_millis(600));
        std::fs::write(temp.path().join("agents/hello.yaml"), "k: v\n").unwrap();
        std::thread::sleep(Duration::from_millis(700));
        assert_eq!(center.get_audit_log(10).len(), 1, "内容未变应去重");

        // 非法 yaml → 失败审计（无旧缓存 → rolled_back=false）
        std::thread::sleep(Duration::from_millis(600));
        std::fs::write(temp.path().join("agents/bad.yaml"), "k: [unclosed\n").unwrap();
        wait_until(
            || center.get_audit_log(10).iter().any(|e| !e.success),
            Duration::from_secs(5),
        );
        let log = center.get_audit_log(10);
        assert!(!log[0].success);
        assert!(!log[0].rolled_back);

        // 删除 → Deleted 审计
        std::thread::sleep(Duration::from_millis(600));
        std::fs::remove_file(temp.path().join("agents/hello.yaml")).unwrap();
        wait_until(
            || {
                center
                    .get_audit_log(10)
                    .iter()
                    .any(|e| e.event_type == ConfigEventType::Deleted)
            },
            Duration::from_secs(5),
        );
    }

    #[test]
    fn test_start_watching_modified_event_and_rollback() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(temp.path().join("agents")).unwrap();
        let center = ConfigCenter::new(temp.path());
        let _watcher = center.start_watching().expect("start_watching 应成功");

        // 先经 reload 建立缓存（旧配置在册）→ 再写非法 YAML → 事件回调走
        // rolled_back=true 分支（保留旧配置 + 失败审计）
        let file = temp.path().join("agents/roll.yaml");
        std::fs::write(&file, "k: v\n").unwrap();
        let (ok, _, _) = center.reload(&file.to_string_lossy());
        assert!(ok);
        wait_until(
            || center.get_audit_log(10).len() >= 1,
            Duration::from_secs(5),
        );

        std::thread::sleep(Duration::from_millis(600));
        std::fs::write(&file, "k: [unclosed\n").unwrap();
        wait_until(
            || center.get_audit_log(10).iter().any(|e| !e.success),
            Duration::from_secs(5),
        );
        let log = center.get_audit_log(10);
        assert!(!log[0].success);
        assert!(log[0].rolled_back, "有旧缓存时应 rolled_back=true");
        // 旧配置保留
        let cached = center.get(&file.to_string_lossy());
        assert_eq!(cached.unwrap()["k"], "v");

        // 修改既有文件（内容变化）→ Modified 事件（非 Created）
        std::thread::sleep(Duration::from_millis(600));
        std::fs::write(&file, "k: v2\n").unwrap();
        wait_until(
            || {
                center
                    .get_audit_log(10)
                    .iter()
                    .any(|e| e.event_type == ConfigEventType::Modified && e.success)
            },
            Duration::from_secs(5),
        );
    }
}

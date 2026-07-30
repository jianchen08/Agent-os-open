//! 配置中心（热重载）
//!
//! 对应 0.1 的 `src/config/config_center.py ConfigCenter`。
//! 基于 notify crate 实现文件监听，支持 500ms 防抖 + 内容哈希去重。
//!
//! ⚠️ **未接线（截至 0.2）**：本模块实现了热重载能力，但在运行期尚未启用——
//! 没有任何 crate 依赖 `agentos-config`、`start_watching()` 从未被调用。
//! 管道配置（config/pipelines/autonomous.yaml）目前只在启动期由
//! `pipeline_loader::load_pipeline_config` 加载一次到 `Arc<PipelineConfig>`，
//! 运行期不可变。修改管道配置的唯一生效方式是重启内核进程。
//!
//! 接线涉及 main.rs 启动流程 + 将 AppState.pipeline_config 从 `Arc`
//! 改为可替换容器 + 并发安全，留作独立任务，不属本次持久化修复范围。
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
}

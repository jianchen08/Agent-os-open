//! 配置读写安全服务（P1-5，ADR §4.3 B1-B6）。
//!
//! 提供 manifest `config_files` 映射路径校验、secret 掩码、ETag、原子写等
//! 安全原语，供 `/api/v1/plugins/{id}/config/{file_id}` 端点复用。
//!
//! 设计依据：ADR §4.3「配置读写安全」+ 实测现状（routes_config.py:118 截断写、
//! GET 不掩码）。本模块把安全控制收敛为可独立测试的纯函数。

use std::path::{Path, PathBuf};

use serde_json::Value;
use sha2::{Digest, Sha256};

/// 配置读写错误。
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum ConfigError {
    /// 路径越界（不在 config/ 子树内或含 ../ 越界）—— B1。
    #[error("path escapes config root: {path}")]
    PathOutsideConfigRoot { path: String },
    /// 映射了内核保留文件（plugin_allowlist / pipelines 等）—— B1 denylist。
    #[error("kernel-reserved config file: {path}")]
    KernelReservedFile { path: String },
    /// 文件不存在。
    #[error("config file not found: {path}")]
    NotFound { path: String },
    /// YAML 序列化/解析失败 —— B6 round-trip 校验。
    #[error("yaml round-trip failed: {detail}")]
    YamlInvalid { detail: String },
    /// I/O 错误。
    #[error("io error: {message}")]
    Io { message: String },
}

/// 内核保留文件/目录的 denylist（ADR §4.4 硬边界）。
///
/// manifest 的 config_files 不得映射这些路径——它们归内核（准入/调度/鉴权）。
/// 匹配按路径片段前缀（相对 config/ 根）。
const KERNEL_RESERVED_SEGMENTS: &[&str] = &[
    "plugin_allowlist",
    "plugin_roots",
    "auth",
    "pipelines",
    "steps",
];

/// B1：校验 manifest config_files[].path 解析后的绝对路径安全。
///
/// 规则（ADR §4.3 B1）：
/// - 把 `mapping_path`（相对项目根，如 `config/models/llm.yaml`）解析为绝对路径；
/// - canonicalize 后必须落在 `<project_root>/config/` 子树内，禁止 `../` 越界；
/// - 不得映射内核保留文件（plugin_allowlist / plugin_roots / auth / pipelines / steps）。
///
/// 返回校验通过后的绝对路径。
///
/// # Errors
/// - [`ConfigError::PathOutsideConfigRoot`]：路径越界或不在 config/ 下。
/// - [`ConfigError::KernelReservedFile`]：命中 denylist。
pub fn validate_config_path(
    project_root: &Path,
    mapping_path: &str,
) -> Result<PathBuf, ConfigError> {
    let normalized = mapping_path.replace('\\', "/");
    // 拒绝显式 ../ 越界（即便 canonicalize 也会随后兜底，这里快速失败给出明确错误）
    if normalized.contains("../") {
        return Err(ConfigError::PathOutsideConfigRoot {
            path: mapping_path.to_string(),
        });
    }

    let config_root = project_root.join("config");
    let target = if normalized.starts_with("config/") || normalized.starts_with("config") {
        project_root.join(&normalized)
    } else {
        config_root.join(&normalized)
    };

    // 校验落在 config/ 子树内（用 components 比较，避免前缀字符串误判）
    if !is_under_config(&target, &config_root) {
        return Err(ConfigError::PathOutsideConfigRoot {
            path: mapping_path.to_string(),
        });
    }

    // denylist：相对 config/ 根的任一路径段（含文件 stem）不得命中保留名。
    // 如 config/system/plugin_allowlist.yaml → 段含 plugin_allowlist → 拒绝；
    //    config/pipelines/default.yaml → 段含 pipelines → 拒绝。
    if let Ok(rel) = target.strip_prefix(&config_root) {
        for seg in rel.iter() {
            let s = seg.to_string_lossy();
            let stem = s.split('.').next().unwrap_or(&s);
            if KERNEL_RESERVED_SEGMENTS.iter().any(|r| *r == stem) {
                return Err(ConfigError::KernelReservedFile {
                    path: mapping_path.to_string(),
                });
            }
        }
    }

    Ok(target)
}

/// 判断 `target` 是否落在 `config_root` 子树内（components 逐段比较）。
fn is_under_config(target: &Path, config_root: &Path) -> bool {
    let cfg = match config_root.canonicalize() {
        Ok(c) => c,
        Err(_) => config_root.to_path_buf(),
    };
    let tgt = target
        .canonicalize()
        .unwrap_or_else(|_| target.to_path_buf());
    tgt.starts_with(&cfg)
}

/// B2（GET 掩码）：递归掩码真实明文 secret 值，**保留 `${ENV_VAR}` 占位符**。
///
/// 规则（ADR §4.3 B2）：
/// - 仅对"看起来是敏感字段"的字符串值掩码（key 含 api_key/secret/token/password）；
/// - `${ENV_VAR}` 形式的占位符原样返回（占位符本身不是 secret）；
/// - 其余字段不变。
pub fn mask_secrets(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut out = serde_json::Map::new();
            for (k, v) in map {
                if is_secret_key(k) {
                    out.insert(k.clone(), mask_secret_value(v));
                } else {
                    out.insert(k.clone(), mask_secrets(v));
                }
            }
            Value::Object(out)
        }
        Value::Array(arr) => Value::Array(arr.iter().map(mask_secrets).collect()),
        other => other.clone(),
    }
}

/// 字段名是否疑似 secret（api_key / secret / token / password，不区分大小写）。
fn is_secret_key(key: &str) -> bool {
    let lower = key.to_ascii_lowercase();
    ["api_key", "apikey", "secret", "token", "password"]
        .iter()
        .any(|s| lower.contains(s))
}

/// 掩码单个 secret 值：`${...}` 占位符原样，真实明文 → `****`。
fn mask_secret_value(v: &Value) -> Value {
    let Some(s) = v.as_str() else {
        return v.clone();
    };
    if s.starts_with("${") && s.ends_with('}') {
        return Value::String(s.to_string());
    }
    Value::String("****".to_string())
}

/// B2（PUT 保留原值）：合并提交配置，`"***"` 哨兵字段保留磁盘原值。
///
/// 前端整文件 PUT 时，被掩码的 secret 字段值为 `"***"`；服务端必须保留磁盘原值，
/// 否则会把 `${ENV_VAR}` 占位符冲掉。
pub fn apply_put_masked_sentinels(stored: &Value, submitted: &Value) -> Value {
    match (stored, submitted) {
        (Value::Object(stored_map), Value::Object(submitted_map)) => {
            let mut out = serde_json::Map::new();
            for (k, sv) in submitted_map {
                if sv.as_str() == Some("***") {
                    // 哨兵：保留磁盘原值（若磁盘无该 key 则删除该字段）
                    if let Some(orig) = stored_map.get(k) {
                        out.insert(k.clone(), orig.clone());
                    }
                } else if let Some(orig) = stored_map.get(k) {
                    out.insert(k.clone(), apply_put_masked_sentinels(orig, sv));
                } else {
                    out.insert(k.clone(), sv.clone());
                }
            }
            Value::Object(out)
        }
        (_, submitted_other) => submitted_other.clone(),
    }
}

/// B4：计算内容的 ETag（sha256 hex，弱校验语义）。
pub fn compute_etag(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

/// B4 + B6：原子写入 YAML（tmp + os::replace + round-trip 校验）。
///
/// 流程（ADR §4.3 B4/B6）：
/// 1. serde_yaml 序列化 value 为字符串；
/// 2. **round-trip 校验**：对序列化结果再 `yaml.safe_load`（这里反序列化回 Value），
///    不可解析则拒绝写、磁盘保持原值；
/// 3. 写入同目录临时文件；
/// 4. `os::replace` 原子替换目标（避免半写文件被 watcher 读到）。
///
/// # Errors
/// - [`ConfigError::YamlInvalid`]：round-trip 校验失败。
/// - [`ConfigError::Io`]：写盘失败。
pub fn atomic_write_yaml(target: &Path, value: &Value) -> Result<(), ConfigError> {
    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent).map_err(|e| ConfigError::Io {
            message: format!("create_dir_all {}: {}", parent.display(), e),
        })?;
    }

    let serialized = serde_yaml::to_string(value).map_err(|e| ConfigError::YamlInvalid {
        detail: e.to_string(),
    })?;

    // B6 round-trip：序列化结果必须可解析回 YAML（防半结构化数据损坏）
    serde_yaml::from_str::<serde_yaml::Value>(&serialized).map_err(|e| {
        ConfigError::YamlInvalid {
            detail: e.to_string(),
        }
    })?;

    // B4 原子写：tmp + os::replace
    let tmp = target.with_extension("yaml.tmp");
    std::fs::write(&tmp, serialized.as_bytes()).map_err(|e| ConfigError::Io {
        message: format!("write tmp {}: {}", tmp.display(), e),
    })?;
    std::fs::rename(&tmp, target).map_err(|e| ConfigError::Io {
        message: format!("replace {}: {}", target.display(), e),
    })?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_is_secret_key_detection() {
        assert!(is_secret_key("api_key"));
        assert!(is_secret_key("ApiKey"));
        assert!(is_secret_key("secret_key"));
        assert!(is_secret_key("wecom_token"));
        assert!(is_secret_key("user_password"));
        assert!(!is_secret_key("name"));
        assert!(!is_secret_key("endpoint"));
    }

    #[test]
    fn test_mask_secret_value_keeps_env_placeholder() {
        let v = Value::String("${DEEPSEEK_API_KEY}".to_string());
        assert_eq!(
            mask_secret_value(&v),
            Value::String("${DEEPSEEK_API_KEY}".to_string())
        );
    }

    #[test]
    fn test_mask_secret_value_masks_plaintext() {
        let v = Value::String("sk-real-secret-123".to_string());
        assert_eq!(mask_secret_value(&v), Value::String("****".to_string()));
    }

    #[test]
    fn test_validate_rejects_dotdot() {
        let err = validate_config_path(Path::new("/tmp"), "config/../etc/passwd").unwrap_err();
        assert_eq!(
            err,
            ConfigError::PathOutsideConfigRoot {
                path: "config/../etc/passwd".to_string()
            }
        );
    }
}

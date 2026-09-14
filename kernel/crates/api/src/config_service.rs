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
    /// 映射了内核保留文件（kernel/ / pipelines 等）—— B1 denylist。
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
const KERNEL_RESERVED_SEGMENTS: &[&str] = &["kernel", "plugin_roots", "auth", "pipelines", "steps"];

/// B1：校验 manifest config_files[].path 解析后的绝对路径安全，并解析到
/// **用户空间优先**的实际落点。
///
/// 规则（ADR §4.3 B1 + ADR 2026-09-13-unified-user-root）：
/// - 把 `mapping_path`（相对项目根，如 `config/models/llm.yaml`）解析为绝对路径；
/// - **落点解析走单一解析器**（`user_space::config_write_target`）：用户层存在该
///   文件时优先用户层，否则 factory——读与写共用本函数，杜绝"写一处、读另一处"
///   （单真值 ADR 的实证根因）；
/// - 归一化后必须落在**对应根**的子树内，禁止 `../` 越界与跨根穿透；
/// - 不得映射内核保留文件（kernel / plugin_roots / auth / pipelines / steps），
///   该 denylist 对两个根同样生效——插件不得借用户层绕开内核保留文件。
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
    resolve_config_target(project_root, mapping_path, ConfigTargetMode::Read)
}

/// 落点解析模式：读＝用户层文件存在才用它，写＝一律写用户层。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConfigTargetMode {
    /// 读：用户层存在该文件 → 用户层；否则 → factory。
    Read,
    /// 写：用户层可用 → 用户层（调用方负责先播种 factory 内容）；否则 → factory。
    Write,
}

/// 解析 manifest `config_files[].path` 的落点（读/写同一实现，差异仅参数化）。
///
/// 用户空间优先（ADR 2026-09-13-unified-user-root）：
/// - `Read`：**文件级整体替换**——用户层文件存在即用它，factory 同名文件完全不
///   参与（不读取、不合并）。用户层目录存在但该文件不存在 ≠ 被接管。
/// - `Write`：一律写用户层（用户空间不可用时回落 factory）；用户第一次改某配置
///   时由调用方先播种 factory 内容再写，用户拿到完整文件而非 diff 片段。
///
/// 读与写共用本函数是**硬要求**：两侧各自拼路径正是单真值 ADR 的实证根因
/// （用户值已写盘、插件读到的却是另一份）。
///
/// 安全边界（两个根同样生效，插件不得借用户层绕开）：
/// - `../` 显式拒绝；落点必须在其所属根的子树内（跨根穿透同样拒绝）；
/// - 内核保留段 denylist 按**相对路径段**判定，与落在哪个根无关。
///
/// 解析落点（读/写同一实现，差异仅参数化）并施加内核保留段 denylist。
///
/// 插件 manifest `config_files[].path` 专用入口——内核保留文件不得被插件映射。
///
/// # Errors
/// - [`ConfigError::PathOutsideConfigRoot`]：路径越界或跨根穿透。
/// - [`ConfigError::KernelReservedFile`]：命中 denylist。
pub fn resolve_config_target(
    project_root: &Path,
    mapping_path: &str,
    mode: ConfigTargetMode,
) -> Result<PathBuf, ConfigError> {
    resolve_config_target_inner(project_root, mapping_path, mode, true)
}

/// 内核自有写面的落点解析：路径安全校验同上，但**不施加保留段 denylist**。
///
/// denylist 的语义是「插件不得经 manifest config_files 映射内核调度/鉴权配置」，
/// 不是「用户不得编辑内核配置」——`PUT /config/pipelines/{name}`、
/// `PUT /plugins/{id}/enabled` 这类内核自有 UI 写面正是这些文件的**合法**编辑入口
/// （`pipelines` 与 `plugins` 都在保留清单里）。故内核路由走本入口。
///
/// 路径安全（`../` 与跨根穿透）与落点语义与插件入口完全一致，共用同一实现。
pub fn resolve_kernel_config_target(
    project_root: &Path,
    mapping_path: &str,
    mode: ConfigTargetMode,
) -> Result<PathBuf, ConfigError> {
    resolve_config_target_inner(project_root, mapping_path, mode, false)
}

fn resolve_config_target_inner(
    project_root: &Path,
    mapping_path: &str,
    mode: ConfigTargetMode,
    enforce_denylist: bool,
) -> Result<PathBuf, ConfigError> {
    let normalized = mapping_path.replace('\\', "/");
    // 拒绝显式 ../ 越界（即便 canonicalize 也会随后兜底，这里快速失败给出明确错误）
    if normalized.contains("../") {
        return Err(ConfigError::PathOutsideConfigRoot {
            path: mapping_path.to_string(),
        });
    }

    // 统一以 project_root 的 canonical 形态派生 config_root 与 target：
    // 目标文件不存在时 target.canonicalize() 失败会回退原始路径，与单独
    // canonicalize 的 config_root 在 Windows（canonicalize 返回 \\?\ 前缀）/
    // junction 环境下组件前缀不同源，误判"路径越界"（manifest 内联默认
    // 模式文件缺失是常态，ADR 2026-09-02-context-window-config-inline-manifest）。
    let root = project_root
        .canonicalize()
        .unwrap_or_else(|_| project_root.to_path_buf());
    let config_root = root.join("config");
    // 相对 config 根的路径（manifest 的 path 可带或不带 "config/" 前缀）
    let rel = normalized
        .strip_prefix("config/")
        .unwrap_or(&normalized)
        .to_string();

    // 用户层落点（用户空间不可用 = None → 一律回落 factory）
    let user_dir = agentos_core::user_space::user_config_dir();
    let user_target = user_dir.as_ref().map(|d| d.join(&rel));
    let factory_target = config_root.join(&rel);

    let (target, bound_root) = match mode {
        ConfigTargetMode::Read => match user_target {
            Some(u) if u.is_file() => (u, user_dir),
            _ => (factory_target, Some(config_root.clone())),
        },
        ConfigTargetMode::Write => match user_target {
            Some(u) => (u, user_dir),
            None => (factory_target, Some(config_root.clone())),
        },
    };

    // 边界校验：落在**所属根**的子树内（`../` 已在上拒绝）。
    //
    // 两侧同源：目标与边界都由同一个根 `join(rel)` 派生。规范化只用于比较，
    // **不改变返回的 path 形态**（消费方按普通路径使用，Windows 上 `\\?\` 形态
    // 会外溢到调用方）。
    //
    // 关键：目标文件**尚不存在**时 `canonicalize` 必然失败（写路径首次落盘的常态），
    // 此时两侧都退回原始拼法比较——若一边规范化（根存在 → 拿到 `\\?\` 形态）而
    // 另一边回退原始路径，组件前缀不同源，合法落点会被误判"越界"，写入恒 400。
    if let Some(bound) = bound_root {
        match target.canonicalize() {
            Ok(target_canon) => {
                let bound_cmp = bound.canonicalize().unwrap_or(bound);
                if !target_canon.starts_with(&bound_cmp) {
                    return Err(ConfigError::PathOutsideConfigRoot {
                        path: mapping_path.to_string(),
                    });
                }
            }
            // 目标不存在：结构上已由「同根 + 无 ../ 的 rel」保证包含，无需再做
            // 符号链接级判定（不存在的东西无法是链接）。
            Err(_) => {
                if !target.starts_with(&bound) {
                    return Err(ConfigError::PathOutsideConfigRoot {
                        path: mapping_path.to_string(),
                    });
                }
            }
        }
    }

    // denylist：按**相对路径段**（含文件 stem）判定，与落在哪个根无关——
    // 用户层不是绕过内核保留文件的旁路。
    // 如 kernel/plugin_allowlist.yaml → 段含 kernel → 拒绝；
    //    pipelines/default.yaml → 段含 pipelines → 拒绝。
    // （内核自有写面经 resolve_kernel_config_target 走本函数并关闭此闸。）
    if enforce_denylist {
        for seg in Path::new(&rel).iter() {
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

/// 写路径落点的播种：目标不存在而 factory 同名文件存在时，把 factory 内容
/// 复制到用户层目标（copy-on-write 首次接管）。
///
/// 返回 `Ok(true)` = 执行了播种。目标已存在（已接管）或 factory 无此文件
/// （全新配置）时不动作——后者由调用方按既有"PUT 保存将创建文件"语义处理。
///
/// 不自动合并漂移：factory 升级新增键而用户文件是旧快照时**绝不静默合并**
/// （静默合并 = 两处存值），漂移提示属 UI 层职责。
pub fn seed_user_config_from_factory(
    project_root: &Path,
    mapping_path: &str,
) -> Result<bool, ConfigError> {
    let normalized = mapping_path.replace('\\', "/");
    let rel = normalized
        .strip_prefix("config/")
        .unwrap_or(&normalized)
        .to_string();
    let Some(user_dir) = agentos_core::user_space::user_config_dir() else {
        return Ok(false);
    };
    let factory = project_root
        .canonicalize()
        .unwrap_or_else(|_| project_root.to_path_buf())
        .join("config")
        .join(&rel);
    let target = user_dir.join(&rel);
    if target.is_file() || !factory.is_file() {
        return Ok(false);
    }
    // 基线先读：登记要用出厂内容哈希（ADR 2026-09-14 §2.2 接管三件套）
    let baseline = std::fs::read(&factory).map_err(|e| ConfigError::Io {
        message: format!("read factory for ownership baseline failed: {e}"),
    })?;
    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent).map_err(|e| ConfigError::Io {
            message: format!("seed parent dir failed: {e}"),
        })?;
    }
    std::fs::copy(&factory, &target).map_err(|e| ConfigError::Io {
        message: format!("seed from factory failed: {e}"),
    })?;
    // 接管登记：播种成功即登记出厂基线；登记失败回滚副本——登记与文件同生
    // 共死，绝不产生无凭证的接管（否则恢复出厂/漂移提示对该路径永久失灵）。
    if let Err(e) = agentos_core::user_space::register_ownership(
        &rel,
        Some(agentos_core::user_space::sha256_hex(&baseline)),
        Some(env!("CARGO_PKG_VERSION").to_string()),
    ) {
        let _ = std::fs::remove_file(&target);
        return Err(ConfigError::Io {
            message: format!("ownership registration failed (seed rolled back): {e}"),
        });
    }
    Ok(true)
}

/// 恢复出厂（ADR 2026-09-14 §2.2 规则 3）：删除用户层文件 + 删除接管登记，
/// 出厂文件即复活——单一存在始终成立，任意时刻生效的仍是一份。
///
/// 用户层无此文件时不动作（返回 `Ok(false)`）；出厂侧文件**绝不**被本函数触碰。
/// 显式操作入口，供设置页/管理路由接线。
///
/// # Errors
/// - [`ConfigError::PathOutsideConfigRoot`]：路径含 `../` 越界。
/// - [`ConfigError::Io`]：删除失败或登记删除失败（此时用户文件可能已删而账未销，
///   状态仍安全：无文件即出厂生效，残留空条目由漂移核对自然清理）。
pub fn restore_factory_config(mapping_path: &str) -> Result<bool, ConfigError> {
    let normalized = mapping_path.replace('\\', "/");
    if normalized.contains("../") {
        return Err(ConfigError::PathOutsideConfigRoot {
            path: mapping_path.to_string(),
        });
    }
    let rel = normalized
        .strip_prefix("config/")
        .unwrap_or(&normalized)
        .to_string();
    let Some(user_dir) = agentos_core::user_space::user_config_dir() else {
        return Ok(false);
    };
    let target = user_dir.join(&rel);
    if !target.is_file() {
        return Ok(false);
    }
    std::fs::remove_file(&target).map_err(|e| ConfigError::Io {
        message: format!("restore factory: remove user file failed: {e}"),
    })?;
    agentos_core::user_space::remove_ownership(&rel).map_err(|e| ConfigError::Io {
        message: format!("restore factory: drop ownership entry failed: {e}"),
    })?;
    Ok(true)
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
    if let Err(e) = std::fs::rename(&tmp, target) {
        // rename 失败 best-effort 清 tmp（D7：不留 .tmp 残骸）
        if let Err(cleanup_err) = std::fs::remove_file(&tmp) {
            tracing::warn!(
                target = %tmp.display(),
                error = %cleanup_err,
                "清理 .tmp 残骸失败"
            );
        }
        return Err(ConfigError::Io {
            message: format!("replace {}: {}", target.display(), e),
        });
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn atomic_write_yaml_rename_failure_cleans_tmp() {
        // D7：rename 失败（目标被同名目录占位模拟占用）→ Err 且 .tmp 被清理
        let tmp = tempfile::tempdir().unwrap();
        let occupied = tmp.path().join("occupied.yaml");
        std::fs::create_dir_all(&occupied).unwrap();
        let err = atomic_write_yaml(&occupied, &serde_json::json!({"a": 1})).unwrap_err();
        assert!(
            matches!(err, ConfigError::Io { .. }),
            "rename 失败应报 Io 错: {err:?}"
        );
        assert!(
            !tmp.path().join("occupied.yaml.tmp").exists(),
            "rename 失败后 .tmp 残骸必须被清理"
        );
        // 成功路径回归：正常目标原子替换成功且无 .tmp 残留
        let normal = tmp.path().join("normal.yaml");
        atomic_write_yaml(&normal, &serde_json::json!({"a": 1})).unwrap();
        assert_eq!(
            std::fs::read_to_string(&normal).unwrap(),
            "a: 1
"
        );
        assert!(!tmp.path().join("normal.yaml.tmp").exists());
    }

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

    /// manifest 内联默认（ADR 2026-09-02：值在 fields.default，磁盘文件缺失是
    /// 常态）——目标文件不存在时不得误判越界（canonicalize 单边解析的坑）。
    #[test]
    fn test_validate_accepts_missing_file_in_config_subtree() {
        let tmp = tempfile::tempdir().unwrap();
        // config 根存在、目标文件不存在（config/system/ 目录也不存在）
        let validated =
            validate_config_path(tmp.path(), "config/system/context_window_config.yaml")
                .expect("文件缺失（manifest 内联默认）不得误判越界");
        assert!(validated.ends_with("config/system/context_window_config.yaml"));
    }

    /// 越界仍拒绝：显式 ../ 与 config 根之外的绝对路径。
    #[test]
    fn test_validate_rejects_path_outside_config_root() {
        let tmp = tempfile::tempdir().unwrap();
        let err = validate_config_path(tmp.path(), "../outside.yaml").unwrap_err();
        assert_eq!(
            err,
            ConfigError::PathOutsideConfigRoot {
                path: "../outside.yaml".to_string()
            }
        );
    }

    /// 内核自有写面（`PUT /config/pipelines/{name}`）必须能解析 `pipelines/`：
    /// 该段在插件映射的 denylist 里，但内核路由正是它的合法编辑入口——若不区分，
    /// 管道配置页保存恒 422。
    #[test]
    fn kernel_target_bypasses_reserved_denylist_but_plugin_target_does_not() {
        let tmp = tempfile::tempdir().unwrap();
        let factory = tmp.path().join("factory");
        std::fs::create_dir_all(factory.join("config")).unwrap();
        let user = tmp.path().join("user-config");
        std::fs::create_dir_all(&user).unwrap();
        let _guard = crate::test_env::pin_user_config_dir(&user);

        // 插件入口：pipelines 命中保留段 → 拒绝
        assert!(matches!(
            resolve_config_target(
                &factory,
                "config/pipelines/autonomous.yaml",
                ConfigTargetMode::Read
            ),
            Err(ConfigError::KernelReservedFile { .. })
        ));
        // 内核入口：同一路径放行，且写落点落在用户层
        let write_path = resolve_kernel_config_target(
            &factory,
            "pipelines/autonomous.yaml",
            ConfigTargetMode::Write,
        )
        .expect("内核自有写面不得被保留段 denylist 挡住");
        assert_eq!(write_path, user.join("pipelines/autonomous.yaml"));

        // 内核入口仍不放宽路径安全：../ 与跨根穿透继续拒绝
        assert!(matches!(
            resolve_kernel_config_target(
                &factory,
                "pipelines/../../etc/passwd",
                ConfigTargetMode::Write
            ),
            Err(ConfigError::PathOutsideConfigRoot { .. })
        ));
    }

    /// `PUT /plugins/{id}/enabled` 的落点语义：用户层文件已存在（已接管）时读它，
    /// 未接管时读 factory——否则首次开关的基线是"空 profile"，用户只关一个插件
    /// 就把 factory 里其他插件的启停整份写没。
    #[test]
    fn kernel_profile_target_reads_user_layer_once_taken_over() {
        let tmp = tempfile::tempdir().unwrap();
        let factory = tmp.path().join("factory");
        std::fs::create_dir_all(factory.join("config/kernel")).unwrap();
        std::fs::write(
            factory.join("config/kernel/default_profile.yaml"),
            "plugins:\n  a:\n    enabled: true\n  b:\n    enabled: false\n",
        )
        .unwrap();
        let user = tmp.path().join("user-config");
        std::fs::create_dir_all(&user).unwrap();
        let _guard = crate::test_env::pin_user_config_dir(&user);

        let rel = "kernel/default_profile.yaml";
        // 未接管：读回 factory（用户层无此文件）。factory 根经 project_root
        // canonicalize 派生（既有行为，Windows 上带 \\?\ 前缀），故比较规范化形态。
        let factory_hit =
            resolve_kernel_config_target(&factory, rel, ConfigTargetMode::Read).unwrap();
        assert_eq!(
            factory_hit.canonicalize().unwrap(),
            factory.join("config").join(rel).canonicalize().unwrap(),
            "未接管时读 factory"
        );

        // 播种后再读：命中用户层整体替换，factory 完全不参与
        assert!(seed_user_config_from_factory(&factory, rel).unwrap());
        let seeded = resolve_kernel_config_target(&factory, rel, ConfigTargetMode::Read).unwrap();
        assert_eq!(seeded, user.join(rel));
        assert_eq!(
            std::fs::read_to_string(&seeded).unwrap(),
            "plugins:\n  a:\n    enabled: true\n  b:\n    enabled: false\n",
            "播种必须是 factory 的完整内容，用户拿到的不是 diff 片段"
        );
        // 幂等：已接管不再播种（不清空用户改动）
        std::fs::write(&seeded, "plugins:\n  a:\n    enabled: false\n").unwrap();
        assert!(!seed_user_config_from_factory(&factory, rel).unwrap());
        assert_eq!(
            std::fs::read_to_string(&seeded).unwrap(),
            "plugins:\n  a:\n    enabled: false\n"
        );
    }

    /// 接管三件套（ADR 2026-09-14 §2.2）：播种成功必须自动登记出厂基线；
    /// 登记失败（账本损坏）必须回滚副本——绝不产生无凭证的接管。
    #[test]
    fn seed_registers_ownership_and_rolls_back_when_ledger_corrupt() {
        let tmp = tempfile::tempdir().unwrap();
        let factory = tmp.path().join("factory");
        std::fs::create_dir_all(factory.join("config/models")).unwrap();
        let content = "model: x\n";
        std::fs::write(factory.join("config/models/llm.yaml"), content).unwrap();
        let user = tmp.path().join("user-config");
        std::fs::create_dir_all(&user).unwrap();
        let _guard = crate::test_env::pin_user_config_dir(&user);

        // 正常播种：登记自动落账，基线 = 出厂内容哈希
        assert!(seed_user_config_from_factory(&factory, "models/llm.yaml").unwrap());
        let entries = agentos_core::user_space::load_ownership_entries().unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].path, "models/llm.yaml");
        assert_eq!(
            entries[0].seeded_from_sha256.as_deref(),
            Some(agentos_core::user_space::sha256_hex(content.as_bytes()).as_str())
        );
        assert_eq!(
            entries[0].factory_version.as_deref(),
            Some(env!("CARGO_PKG_VERSION"))
        );

        // 账本损坏：播种必须失败且**不留用户副本**（回滚），出厂文件原样
        std::fs::write(factory.join("config/models/other.yaml"), "other: 1\n").unwrap();
        let ledger = user.join(".ownership.json");
        std::fs::write(&ledger, "{corrupt").unwrap();
        assert!(seed_user_config_from_factory(&factory, "models/other.yaml").is_err());
        assert!(
            !user.join("models/other.yaml").exists(),
            "登记失败的播种必须回滚，不得留下无凭证的接管副本"
        );
        assert_eq!(
            std::fs::read_to_string(factory.join("config/models/other.yaml")).unwrap(),
            "other: 1\n",
            "出厂文件不得被触碰"
        );
    }

    /// 恢复出厂（ADR 2026-09-14 §2.2 规则 3）：删用户文件 + 销登记；此后读路径
    /// 回到出厂，且可以重新播种接管。出厂文件全程不被触碰。
    #[test]
    fn restore_factory_resurrects_factory_and_allows_reseed() {
        let tmp = tempfile::tempdir().unwrap();
        let factory = tmp.path().join("factory");
        std::fs::create_dir_all(factory.join("config/models")).unwrap();
        std::fs::write(factory.join("config/models/llm.yaml"), "factory: v1\n").unwrap();
        let user = tmp.path().join("user-config");
        std::fs::create_dir_all(&user).unwrap();
        let _guard = crate::test_env::pin_user_config_dir(&user);

        assert!(seed_user_config_from_factory(&factory, "models/llm.yaml").unwrap());
        std::fs::write(user.join("models/llm.yaml"), "user: custom\n").unwrap();

        // 恢复出厂：用户文件与登记同时消失
        assert!(restore_factory_config("config/models/llm.yaml").unwrap());
        assert!(!user.join("models/llm.yaml").exists());
        assert!(
            agentos_core::user_space::load_ownership_entries()
                .unwrap()
                .is_empty(),
            "销登记后账本应为空"
        );
        assert_eq!(
            std::fs::read_to_string(factory.join("config/models/llm.yaml")).unwrap(),
            "factory: v1\n",
            "出厂文件全程不被触碰"
        );

        // 无用户文件的路径：恢复出厂 = 不动作
        assert!(!restore_factory_config("models/absent.yaml").unwrap());
        // ../ 越界照常拒绝
        assert!(restore_factory_config("models/../../etc/passwd").is_err());
        // 恢复后可重新播种接管（生命周期闭环）
        assert!(seed_user_config_from_factory(&factory, "models/llm.yaml").unwrap());
    }

    // ── 落点解析的写模式 / 用户空间缺席 / 工厂文件缺席 分支面 ──

    /// Write 模式：用户空间可用时**一律写用户层**（不论文件是否已存在）；
    /// 工厂缺该文件（全新配置）同样落用户层。
    #[test]
    fn write_mode_always_targets_user_layer() {
        let tmp = tempfile::tempdir().unwrap();
        let factory = tmp.path().join("factory");
        std::fs::create_dir_all(factory.join("config/models")).unwrap();
        let user = tmp.path().join("user-config");
        std::fs::create_dir_all(&user).unwrap();
        let _guard = crate::test_env::pin_user_config_dir(&user);

        let rel = "models/llm.yaml";
        // 工厂与用户层都没有该文件（首次写入）→ 仍落用户层
        let p = resolve_kernel_config_target(&factory, rel, ConfigTargetMode::Write).unwrap();
        assert_eq!(p, user.join(rel), "Write 模式一律落用户层");

        // 用户层已有该文件（已接管）→ 仍是用户层（不因存在性改判）
        std::fs::create_dir_all(user.join("models")).unwrap();
        std::fs::write(user.join(rel), "user: v1\n").unwrap();
        let p = resolve_kernel_config_target(&factory, rel, ConfigTargetMode::Write).unwrap();
        assert_eq!(p, user.join(rel));
    }

    /// 用户空间不可用（三个 env 全清但仍可能落 OS 标准目录——故直接验证
    /// `user_config_dir()` 为 None 的形态不可构造时的等价面：Write 落 factory）。
    /// 用 `AGENTOS_USER_ROOT` 钉到一个**不可写成目录**的路径来逼出回落。
    #[test]
    fn write_mode_falls_back_to_factory_when_user_root_unusable() {
        // 钉用户根为"空串"→ env_path 过滤空串 → user_root() 回落 dirs::data_dir()；
        // 无法真正构造 None（OS 标准目录恒存在），故本用例锁的是"分区变量为空串
        // 时按未设置处理"这一解析契约，进而走 Read 的 factory 分支。
        let tmp = tempfile::tempdir().unwrap();
        let factory = tmp.path().join("factory");
        std::fs::create_dir_all(factory.join("config/models")).unwrap();
        std::fs::write(factory.join("config/models/llm.yaml"), "f: 1\n").unwrap();
        let _guard =
            crate::test_env::pin_env(agentos_core::user_space::USER_CONFIG_DIR_ENV, Some(""));
        let _guard2 = crate::test_env::pin_env(agentos_core::user_space::USER_ROOT_ENV, Some(""));

        // 空串 = 未设置 → 走 OS 标准目录（存在但无该文件）→ Read 落 factory
        let p = resolve_kernel_config_target(&factory, "models/llm.yaml", ConfigTargetMode::Read)
            .unwrap();
        assert_eq!(
            p.canonicalize().unwrap(),
            factory
                .join("config/models/llm.yaml")
                .canonicalize()
                .unwrap(),
            "空串 env 视为未设置，Read 落 factory"
        );
    }

    /// 目标**不存在**时两侧都退回原始拼法比较（canonicalize 失败分支）——
    /// 「config 根存在、目标文件缺失」的常态必须放行，不得误判越界。
    #[test]
    fn missing_target_compares_raw_paths_without_false_escape() {
        let tmp = tempfile::tempdir().unwrap();
        // config 根存在（可 canonicalize 成 \\?\ 形态），目标文件不存在
        std::fs::create_dir_all(tmp.path().join("config")).unwrap();
        let p =
            resolve_kernel_config_target(tmp.path(), "brand/new/deep.yaml", ConfigTargetMode::Read)
                .expect("目标缺失不得误判越界");
        assert!(p.ends_with("brand/new/deep.yaml") || p.ends_with("brand\\new\\deep.yaml"));
    }

    /// 显式 `../` 越界在归一化后被快速拒绝（Write 与 Read 同规）。
    #[test]
    fn dotdot_rejected_in_both_modes() {
        let tmp = tempfile::tempdir().unwrap();
        for mode in [ConfigTargetMode::Read, ConfigTargetMode::Write] {
            let err =
                resolve_kernel_config_target(tmp.path(), "a/../../escape.yaml", mode).unwrap_err();
            assert!(
                matches!(err, ConfigError::PathOutsideConfigRoot { .. }),
                "mode={mode:?}: {err:?}"
            );
        }
    }

    /// denylist 覆盖全五个保留段，且按**文件 stem** 判定（`pipelines.yaml` 命中
    /// `pipelines` 段）；`config/` 前缀可有可无。
    #[test]
    fn denylist_covers_all_reserved_segments() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(tmp.path().join("config")).unwrap();
        for rel in [
            "config/kernel/plugin_allowlist.yaml",
            "config/plugin_roots/x.yaml",
            "config/auth/users.yaml",
            "config/pipelines/default.yaml",
            "config/steps/common.yaml",
            // 无 config/ 前缀 + stem 判定：pipelines.yaml 的 stem = pipelines
            "pipelines.yaml",
            "auth.yaml",
        ] {
            let err = resolve_config_target(tmp.path(), rel, ConfigTargetMode::Read).unwrap_err();
            assert!(
                matches!(err, ConfigError::KernelReservedFile { .. }),
                "{rel} 应命中保留段: {err:?}"
            );
        }
        // 非保留段 + 内核入口关闭 denylist 时放行
        resolve_config_target(tmp.path(), "models/llm.yaml", ConfigTargetMode::Read)
            .expect("models 段非保留");
        resolve_kernel_config_target(tmp.path(), "pipelines/default.yaml", ConfigTargetMode::Read)
            .expect("内核入口关闭 denylist");
    }

    /// 反斜杠路径归一化：Windows 形态 `config\\models\\x.yaml` 等价正斜杠。
    #[test]
    fn backslash_path_normalized() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(tmp.path().join("config/models")).unwrap();
        let a = resolve_config_target(tmp.path(), "config/models/x.yaml", ConfigTargetMode::Read)
            .unwrap();
        let b = resolve_config_target(tmp.path(), "config\\models\\x.yaml", ConfigTargetMode::Read)
            .unwrap();
        assert_eq!(a, b, "反斜杠形态必须与正斜杠同落点");
        // 反斜杠形态的 ../ 同样被拒
        assert!(resolve_config_target(
            tmp.path(),
            "config\\..\\escape.yaml",
            ConfigTargetMode::Read
        )
        .is_err());
    }

    /// `seed_user_config_from_factory` 的两个"不动作"分支：
    /// ① 目标已存在（已接管，不覆盖用户改动）；
    /// ② 工厂无此文件（全新配置，交给 PUT 隐式创建语义）。
    #[test]
    fn seed_noop_when_target_exists_or_factory_absent() {
        let tmp = tempfile::tempdir().unwrap();
        let factory = tmp.path().join("factory");
        std::fs::create_dir_all(factory.join("config/models")).unwrap();
        std::fs::write(factory.join("config/models/llm.yaml"), "f: 1\n").unwrap();
        let user = tmp.path().join("user-config");
        std::fs::create_dir_all(&user).unwrap();
        let _guard = crate::test_env::pin_user_config_dir(&user);

        // ② 工厂无此文件 → Ok(false)，不创建用户文件
        assert!(!seed_user_config_from_factory(&factory, "models/absent.yaml").unwrap());
        assert!(!user.join("models/absent.yaml").exists());

        // ① 目标已存在 → Ok(false)，内容原样
        std::fs::create_dir_all(user.join("models")).unwrap();
        std::fs::write(user.join("models/llm.yaml"), "user: mine\n").unwrap();
        assert!(!seed_user_config_from_factory(&factory, "models/llm.yaml").unwrap());
        assert_eq!(
            std::fs::read_to_string(user.join("models/llm.yaml")).unwrap(),
            "user: mine\n",
            "已接管不得覆盖用户内容"
        );
    }

    /// `restore_factory_config` 的 `../` 越界拒绝（不触碰任何文件）。
    #[test]
    fn restore_factory_rejects_dotdot() {
        let tmp = tempfile::tempdir().unwrap();
        let user = tmp.path().join("user-config");
        std::fs::create_dir_all(&user).unwrap();
        let _guard = crate::test_env::pin_user_config_dir(&user);
        let err = restore_factory_config("models/../../outside.yaml").unwrap_err();
        assert!(matches!(err, ConfigError::PathOutsideConfigRoot { .. }));
    }

    // ── B2 掩码 / B4 原子写的剩余分支 ──

    /// `mask_secrets` 递归覆盖数组与嵌套对象；非字符串 secret 值原样保留。
    #[test]
    fn mask_secrets_recurses_arrays_and_keeps_non_strings() {
        let value = serde_json::json!({
            "providers": [
                {"api_key": "sk-real", "name": "a"},
                {"api_key": "${ENV_KEY}", "name": "b"}
            ],
            "limits": {"token_budget": 100, "password": 1234},
            "plain": null
        });
        let masked = mask_secrets(&value);
        assert_eq!(masked["providers"][0]["api_key"], "****");
        assert_eq!(
            masked["providers"][1]["api_key"], "${ENV_KEY}",
            "数组内占位符保留"
        );
        assert_eq!(masked["providers"][0]["name"], "a");
        assert_eq!(
            masked["limits"]["password"], 1234,
            "非字符串 secret 值原样（不臆造掩码）"
        );
        assert_eq!(masked["limits"]["token_budget"], 100);
        assert!(masked["plain"].is_null());
    }

    /// `apply_put_masked_sentinels`：磁盘无该 key 的 `***` 哨兵 = 删除字段；
    /// 嵌套对象的哨兵递归保留原值。
    #[test]
    fn apply_put_masked_sentinels_nested_and_missing_key() {
        let stored = serde_json::json!({
            "llm": {"api_key": "${K}", "model": "old"},
            "name": "keep"
        });
        let submitted = serde_json::json!({
            // api_key 磁盘无此 key（新字段带哨兵）→ 该字段被删除
            "llm": {"api_key": "***", "model": "new", "brand_new": "***"},
            "name": "renamed"
        });
        let merged = apply_put_masked_sentinels(&stored, &submitted);
        assert_eq!(merged["llm"]["api_key"], "${K}", "嵌套哨兵保留磁盘原值");
        assert_eq!(merged["llm"]["model"], "new", "非哨兵用提交值");
        assert!(
            merged["llm"].get("brand_new").is_none(),
            "磁盘无该 key 的哨兵 = 删除字段: {merged}"
        );
        assert_eq!(merged["name"], "renamed");

        // 提交值非对象（标量/数组）→ 原样返回（类型替换语义）
        assert_eq!(
            apply_put_masked_sentinels(&serde_json::json!({"a": 1}), &serde_json::json!([1, 2])),
            serde_json::json!([1, 2])
        );
    }

    /// `compute_etag` 内容敏感（不同内容不同 etag）且同内容稳定。
    #[test]
    fn compute_etag_is_content_sensitive_and_stable() {
        let a = compute_etag(b"x: 1\n");
        assert_eq!(a, compute_etag(b"x: 1\n"), "同内容稳定");
        assert_ne!(a, compute_etag(b"x: 2\n"), "不同内容必须不同 etag");
        assert_eq!(a.len(), 64, "sha256 hex 长度");
    }

    /// `atomic_write_yaml`：目标父目录不存在时自动创建（首次写入形态）。
    #[test]
    fn atomic_write_creates_missing_parent_dirs() {
        let tmp = tempfile::tempdir().unwrap();
        let target = tmp.path().join("deep/nested/new.yaml");
        atomic_write_yaml(&target, &serde_json::json!({"k": "v"})).unwrap();
        assert_eq!(std::fs::read_to_string(&target).unwrap(), "k: v\n");
    }
}

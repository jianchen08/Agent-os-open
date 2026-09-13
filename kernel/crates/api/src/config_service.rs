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

/// B1：校验 manifest config_files[].path 解析后的绝对路径安全，并解析到
/// **用户空间优先**的实际落点。
///
/// 规则（ADR §4.3 B1 + ADR 2026-09-13-unified-user-root）：
/// - 把 `mapping_path`（相对项目根，如 `config/models/llm.yaml`）解析为绝对路径；
/// - **落点解析走单一解析器**（`user_space::config_write_target`）：用户层存在该
///   文件时优先用户层，否则 factory——读与写共用本函数，杜绝"写一处、读另一处"
///   （单真值 ADR 的实证根因）；
/// - 归一化后必须落在**对应根**的子树内，禁止 `../` 越界与跨根穿透；
/// - 不得映射内核保留文件（plugin_allowlist / plugin_roots / auth / pipelines / steps），
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
    // 如 system/plugin_allowlist.yaml → 段含 plugin_allowlist → 拒绝；
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
    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent).map_err(|e| ConfigError::Io {
            message: format!("seed parent dir failed: {e}"),
        })?;
    }
    std::fs::copy(&factory, &target).map_err(|e| ConfigError::Io {
        message: format!("seed from factory failed: {e}"),
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
        std::fs::create_dir_all(factory.join("config/plugins")).unwrap();
        std::fs::write(
            factory.join("config/plugins/default_profile.yaml"),
            "plugins:\n  a:\n    enabled: true\n  b:\n    enabled: false\n",
        )
        .unwrap();
        let user = tmp.path().join("user-config");
        std::fs::create_dir_all(&user).unwrap();
        let _guard = crate::test_env::pin_user_config_dir(&user);

        let rel = "plugins/default_profile.yaml";
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
}

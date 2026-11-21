//! 用户配置层叠加（user_root 接管 factory 配置，按文件逐份写入）。

use super::*;
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
/// 返回 `Ok(被用户层接管的文件数)`（日志可观测性——"改了没生效"排查的
/// 第一线索）；`Err` = 用户根不可读的硬失败（内部已 warn，factory-only
/// 结果照常可用，调用方须将对应快照标记不可指纹化）。
pub(super) fn apply_user_config_overlay(
    config_map: &mut serde_json::Map<String, serde_json::Value>,
) -> Result<usize, LoaderError> {
    let Some(user_root) = agentos_core::user_space::user_config_dir() else {
        return Ok(0);
    };
    if !user_root.is_dir() {
        return Ok(0);
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
        return Err(e);
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
    Ok(replaced)
}

/// 用户覆盖根的装载面指纹采集（免读内容，best-effort）。
///
/// 目录不存在/非目录 → `Ok(0)`（与 [`apply_user_config_overlay`] 的准入
/// 判定同源，本就不进叠加）；目录读取失败 → 已 warn + `Err(())`，调用方
/// 须将本次快照标记不可指纹化（指纹面缺用户段时缓存会掩盖后续恢复）。
pub(super) fn collect_user_config_stamps(
    out: &mut Vec<agentos_core::config_scan::YamlFileStamp>,
) -> Result<usize, ()> {
    let Some(user_root) = agentos_core::user_space::user_config_dir() else {
        return Ok(0);
    };
    if !user_root.is_dir() {
        return Ok(0);
    }
    let before = out.len();
    match agentos_core::config_scan::collect_yaml_stamps(&user_root, out) {
        Ok(()) => Ok(out.len() - before),
        Err(e) => {
            warn!(
                "User config stamp scan skipped ({}): {}",
                user_root.display(),
                e
            );
            Err(())
        }
    }
}

/// 按键路径段写入配置叶（中间层不存在则建空对象）。
///
/// 末段直接 **替换**（文件级接管）；中间段是目录层级，仅在缺失时新建——
/// 既不覆盖 factory 的兄弟键，也不因为用户层某文件存在就清空整棵子树。
pub(super) fn set_config_leaf(
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
pub(super) fn auto_generate_env_declarations(manifest: &mut PluginManifest) {
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

//! 插件配置读写与用户环境变量面（routes 子模块）：manifest/配置映射定位、
//! env 脱敏视图、inline 配置更新、ETag 读面。处理函数经父模块 glob 再导出，
//! `crate::routes::get_plugin_config_handler` 等既有引用路径不变。
use agentos_core::traits::{ConfigFileMapping, PluginManifest};
use axum::response::IntoResponse;
use serde::{Deserialize, Serialize};
use serde_json::json;

use super::{config_err_to_api, AppState};
use crate::config_service::{
    apply_put_masked_sentinels, atomic_write_yaml, compute_etag, mask_secrets,
    resolve_config_target, seed_user_config_from_factory, validate_config_path, ConfigTargetMode,
};
use agentos_http::error::ApiError;

/// GET 配置响应体：返回掩码后的文件内容 + 元数据。
#[derive(Debug, Serialize)]
pub struct PluginConfigResponse {
    pub plugin_id: String,
    pub file_id: String,
    pub label: String,
    pub path: String,
    pub data: serde_json::Value,
    pub etag: String,
}

/// PUT 配置请求体：data 为完整文件内容，if_match 为 GET 返回的 ETag（B4 乐观锁）。
#[derive(Debug, Deserialize)]
pub struct PluginConfigUpdateRequest {
    pub data: serde_json::Value,
    pub if_match: Option<String>,
}

/// 在 manifests 中按 id 查找插件。
fn find_manifest<'a>(
    manifests: &'a [PluginManifest],
    plugin_id: &str,
) -> Option<&'a PluginManifest> {
    manifests.iter().find(|m| m.id == plugin_id)
}

/// 在插件 manifest 的 config_files 中按 file_id 查找映射项。
fn find_config_mapping<'a>(
    manifest: &'a PluginManifest,
    file_id: &str,
) -> Option<&'a ConfigFileMapping> {
    manifest.config_files.iter().find(|f| f.id == file_id)
}

/// env target 条目的掩码视图：{字段名: "***"(已设置) | ""(未设置)}（GAP-4）。
///
/// 读取语义与内核侧解析一致：进程环境优先，缺失回退 .env（用户可能经系统
/// 环境变量注入而非设置页——掩码视图如实反映"内核能否解析到"）。
fn masked_env_fields(mapping: &ConfigFileMapping, env_path: &std::path::Path) -> serde_json::Value {
    let text = std::fs::read_to_string(env_path).unwrap_or_default();
    let from_file = agentos_mcp::env_file::parse_env_text_for_read(&text);
    let mut out = serde_json::Map::new();
    for f in &mapping.fields {
        let set = std::env::var(&f.name).ok().is_some() || from_file.contains_key(&f.name);
        out.insert(
            f.name.clone(),
            serde_json::Value::String(if set {
                "***".to_string()
            } else {
                String::new()
            }),
        );
    }
    serde_json::Value::Object(out)
}

/// GET /api/v1/plugins/{id}/config/{file_id}（ADR §4.3）。
///
/// 流程：查 manifest → 查 config_files[file_id] → B1 path 校验 → 读文件 →
/// B2 掩码 → 返回带 ETag（B4）的内容。
pub async fn get_plugin_config_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path((plugin_id, file_id)): axum::extract::Path<(String, String)>,
) -> Result<axum::Json<PluginConfigResponse>, ApiError> {
    let project_root = state.project_root.ok_or_else(|| ApiError::Internal {
        message: "project_root not configured".to_string(),
    })?;

    let manifests = state.manifests.read().await;
    let manifest = find_manifest(&manifests, &plugin_id).ok_or_else(|| ApiError::NotFound {
        message: format!("plugin not found: {plugin_id}"),
    })?;

    let mapping = find_config_mapping(manifest, &file_id).ok_or_else(|| ApiError::NotFound {
        message: format!("config file_id not found: {file_id}"),
    })?;

    // ── env target 分支（GAP-4：key/加密字段写 .env）──
    // GET 语义：data = {字段名: "***"(已设置) | ""(未设置)}——*** 哨兵即
    // has_key 语义，前端按掩码渲染密码框；ETag 由该掩码视图派生（B4 乐观锁
    // 对同一视图生效）。
    if mapping.target.as_deref() == Some("env") {
        let env_path = agentos_mcp::env_file::env_path_for_root(&project_root);
        let data = masked_env_fields(mapping, &env_path);
        let etag = compute_etag(serde_json::to_string(&data).unwrap_or_default().as_bytes());
        return Ok(axum::Json(PluginConfigResponse {
            plugin_id,
            file_id,
            label: mapping.label.clone(),
            path: ".env".to_string(),
            data,
            etag,
        }));
    }

    // ── 内联形态（path 空）：真值 = fields.default（单一真值裁定
    // 2026-09-02），无文件语义，PUT 直接写回 manifest ──
    if mapping.path.is_empty() {
        let data = agentos_invoker::shared::config_defaults_from_fields(&mapping.fields);
        let etag = compute_etag(serde_json::to_string(&data).unwrap_or_default().as_bytes());
        return Ok(axum::Json(PluginConfigResponse {
            plugin_id,
            file_id,
            label: mapping.label.clone(),
            path: mapping.path.clone(),
            data,
            etag,
        }));
    }

    let resolved = validate_config_path(&project_root, &mapping.path).map_err(config_err_to_api)?;
    let raw = match std::fs::read_to_string(&resolved) {
        Ok(raw) => raw,
        Err(_) => {
            // 引用形态文件缺失：fields 不声明 default（G2 拦截双真值），无值
            // 可读即空配置视图（PUT 保存将创建文件）
            let etag = compute_etag(
                serde_json::to_string(&serde_json::Value::Object(Default::default()))
                    .unwrap_or_default()
                    .as_bytes(),
            );
            return Ok(axum::Json(PluginConfigResponse {
                plugin_id,
                file_id,
                label: mapping.label.clone(),
                path: mapping.path.clone(),
                data: serde_json::Value::Object(Default::default()),
                etag,
            }));
        }
    };

    let etag = compute_etag(raw.as_bytes());
    let parsed: serde_json::Value =
        serde_yaml::from_str(&raw).map_err(ApiError::internal("config file yaml parse error"))?;
    let masked = mask_secrets(&parsed);

    Ok(axum::Json(PluginConfigResponse {
        plugin_id,
        file_id,
        label: mapping.label.clone(),
        path: mapping.path.clone(),
        data: masked,
        etag,
    }))
}

/// PUT /api/v1/plugins/{id}/config/{file_id}（ADR §4.3）。
///
/// 流程：查 manifest → 查 config_files[file_id] → B1 path 校验 → 读磁盘原值 →
/// B4 校验 If-Match（缺失/不匹配 → 409）→ B2 *** 哨兵合并 → B4/B6 原子写 →
/// 返回新 ETag。
pub async fn put_plugin_config_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path((plugin_id, file_id)): axum::extract::Path<(String, String)>,
    axum::Json(req): axum::Json<PluginConfigUpdateRequest>,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    let project_root = state
        .project_root
        .clone()
        .ok_or_else(|| ApiError::Internal {
            message: "project_root not configured".to_string(),
        })?;

    // 克隆后即刻释放读锁：内联形态保存末尾要拿写锁同步内存 manifest
    let mapping = {
        let manifests = state.manifests.read().await;
        let manifest = find_manifest(&manifests, &plugin_id).ok_or_else(|| ApiError::NotFound {
            message: format!("plugin not found: {plugin_id}"),
        })?;
        find_config_mapping(manifest, &file_id)
            .ok_or_else(|| ApiError::NotFound {
                message: format!("config file_id not found: {file_id}"),
            })?
            .clone()
    };

    // ── env target 分支（GAP-4）：*** 哨兵跳过、空值清除、新值写入 .env ──
    // 生效语义：写入即生效（stdio spawn overlay + HTTP resolve_env_placeholders
    // 均回读 .env，配合 invoker 的 .env mtime 指纹触发客户端重建）。
    if mapping.target.as_deref() == Some("env") {
        let env_path = agentos_mcp::env_file::env_path_for_root(&project_root);
        let current = masked_env_fields(&mapping, &env_path);
        let current_etag = compute_etag(
            serde_json::to_string(&current)
                .unwrap_or_default()
                .as_bytes(),
        );
        match req.if_match.as_deref() {
            Some(given) if given == current_etag => {}
            _ => {
                return Err(ApiError::Conflict {
                    message: format!(
                        "ETag mismatch: current={current_etag}, given={:?}",
                        req.if_match
                    ),
                });
            }
        }
        let declared: std::collections::HashSet<&str> =
            mapping.fields.iter().map(|f| f.name.as_str()).collect();
        let mut updates: Vec<(String, String)> = Vec::new();
        if let Some(obj) = req.data.as_object() {
            for (name, value) in obj {
                if !declared.contains(name.as_str()) {
                    return Err(ApiError::BadRequest {
                        message: format!("undeclared env field: {name}"),
                    });
                }
                let v = value.as_str().unwrap_or("");
                if v == "***" {
                    continue; // 哨兵：保留现值
                }
                updates.push((name.clone(), v.to_string()));
            }
        }
        agentos_mcp::env_file::write_env_updates(&env_path, &updates).map_err(|e| {
            ApiError::Internal {
                message: format!("write .env: {e}"),
            }
        })?;
        let data = masked_env_fields(&mapping, &env_path);
        let new_etag = compute_etag(serde_json::to_string(&data).unwrap_or_default().as_bytes());
        return Ok(axum::Json(json!({
            "ok": true,
            "plugin_id": plugin_id,
            "file_id": file_id,
            "etag": new_etag,
        })));
    }

    // ── 内联形态（path 空）：真值 = fields.default，保存直接写回 manifest
    // （plugin.json），不再生成独立覆盖文件（单一真值裁定 2026-09-02）──
    if mapping.path.is_empty() {
        return put_inline_manifest_config(state, &mapping, plugin_id, file_id, req).await;
    }

    // 读落点＝用户空间优先（用户层文件存在即用它，否则 factory）——ETag 与 GET 同源。
    let read_path = resolve_config_target(&project_root, &mapping.path, ConfigTargetMode::Read)
        .map_err(config_err_to_api)?;
    let raw = match std::fs::read_to_string(&read_path) {
        Ok(raw) => raw,
        Err(_) => serde_json::to_string(&serde_json::Value::Object(Default::default()))
            .unwrap_or_default(),
    };
    let current_etag = compute_etag(raw.as_bytes());

    // B4 乐观锁：If-Match 必须匹配当前 ETag，否则 409（在播种/写入之前判定——
    // ETag 语义以客户端所见视图为准）
    match req.if_match.as_deref() {
        Some(given) if given == current_etag => {}
        _ => {
            return Err(ApiError::Conflict {
                message: format!(
                    "ETag mismatch: current={current_etag}, given={:?}",
                    req.if_match
                ),
            });
        }
    }

    // 写落点＝用户空间（ADR 2026-09-13-unified-user-root）：用户改动出仓，不再
    // 覆写 git 跟踪的 factory 文件（否则用户配置处于工作区还原抹除风险面内）。
    // 首次接管（用户层尚无此文件）时先按 factory 内容播种，用户拿到完整文件
    // 而非 diff 片段；不自动合并 factory 后续新增键（静默合并 = 两处存值）。
    let write_path = resolve_config_target(&project_root, &mapping.path, ConfigTargetMode::Write)
        .map_err(config_err_to_api)?;
    seed_user_config_from_factory(&project_root, &mapping.path).map_err(config_err_to_api)?;

    let stored: serde_json::Value =
        serde_yaml::from_str(&raw).map_err(ApiError::internal("stored config yaml parse error"))?;
    // B2：*** 哨兵字段保留磁盘原值
    let merged = apply_put_masked_sentinels(&stored, &req.data);

    // B4/B6：原子写 + round-trip 校验
    atomic_write_yaml(&write_path, &merged).map_err(config_err_to_api)?;

    let new_etag = compute_etag(
        std::fs::read_to_string(&write_path)
            .map_err(ApiError::internal("re-read after write failed"))?
            .as_bytes(),
    );

    Ok(axum::Json(json!({
        "plugin_id": plugin_id,
        "file_id": file_id,
        "etag": new_etag,
    })))
}

/// 内联 PUT data 的声明闭环收集器：递归下钻嵌套对象，收集 (字段名, 新值)。
///
/// data 接受两种形态：与 GET 视图同形的嵌套对象（`{"budgets": {"l1": 0.1}}`，
/// 往返对称）与扁平点分键（`{"budgets.l1": 0.1}`）。判定按**叶路径**进行——
/// 路径命中 `fields[].name` 即为叶（无论值类型，保持声明字段可写对象值的旧
/// 契约），否则对象值继续下钻；未声明的叶路径 fail-closed 返回 Err(路径)。
pub(super) fn collect_inline_field_updates<'a>(
    path: &str,
    value: &'a serde_json::Value,
    declared: &std::collections::HashSet<&str>,
    out: &mut Vec<(String, &'a serde_json::Value)>,
) -> Result<(), String> {
    if declared.contains(path) {
        out.push((path.to_string(), value));
        return Ok(());
    }
    match value {
        serde_json::Value::Object(map) => {
            for (k, v) in map {
                collect_inline_field_updates(&format!("{path}.{k}"), v, declared, out)?;
            }
            Ok(())
        }
        _ => Err(path.to_string()),
    }
}

/// 内联形态 PUT：把字段值直接写回插件 manifest（fields.default）。
///
/// 单一真值裁定（2026-09-02）：内联条目不引用磁盘配置文件，保存 = 原地编辑
/// plugin.json 的 `config_files[file_id].fields[name].default`（raw JSON 编辑，
/// 不经结构体序列化回写以免丢未建模键）+ 内存 manifest 同步（GET/ETag 即时
/// 一致；watcher 热重载随后 respawn 让注入层生效）。data 为字段名 → 新值的
/// 部分更新；值 null = 清除该字段 default（组装时跳过，语义同未声明值）。
async fn put_inline_manifest_config(
    state: AppState,
    mapping: &ConfigFileMapping,
    plugin_id: String,
    file_id: String,
    req: PluginConfigUpdateRequest,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    // B4 乐观锁：当前值视图 = fields.default 组装（与 GET 同源，ETag 一致）
    let current = agentos_invoker::shared::config_defaults_from_fields(&mapping.fields);
    let current_etag = compute_etag(
        serde_json::to_string(&current)
            .unwrap_or_default()
            .as_bytes(),
    );
    match req.if_match.as_deref() {
        Some(given) if given == current_etag => {}
        _ => {
            return Err(ApiError::Conflict {
                message: format!(
                    "ETag mismatch: current={current_etag}, given={:?}",
                    req.if_match
                ),
            });
        }
    }

    // 字段声明闭环：按下钻叶路径判定（嵌套视图与扁平点分键两形态见收集器）
    let Some(data) = req.data.as_object() else {
        return Err(ApiError::BadRequest {
            message: "inline config data must be an object".to_string(),
        });
    };
    let declared: std::collections::HashSet<&str> =
        mapping.fields.iter().map(|f| f.name.as_str()).collect();
    let mut updates: Vec<(String, &serde_json::Value)> = Vec::new();
    for (name, value) in data {
        collect_inline_field_updates(name, value, &declared, &mut updates).map_err(|p| {
            ApiError::BadRequest {
                message: format!("undeclared inline config field: {p}"),
            }
        })?;
    }

    // 先在 fields 副本上落值，磁盘与内存两处共用同一份结果
    let mut new_fields = mapping.fields.clone();
    for f in &mut new_fields {
        let Some((_, v)) = updates.iter().find(|(name, _)| *name == f.name) else {
            continue;
        };
        let extra = f.extra.get_or_insert_with(serde_json::Map::new);
        if v.is_null() {
            extra.remove("default");
        } else {
            extra.insert("default".to_string(), (*v).clone());
        }
    }

    // 定位 plugin.json（loader 活路径表现取）
    let Some(dir) = state.plugin_dir(&plugin_id) else {
        return Err(ApiError::Internal {
            message: format!("plugin dir not found: {plugin_id}"),
        });
    };
    let manifest_path = dir.join("plugin.json");
    let raw = std::fs::read_to_string(&manifest_path).map_err(|e| ApiError::Internal {
        message: format!("read manifest {}: {e}", manifest_path.display()),
    })?;
    let mut root: serde_json::Value =
        serde_json::from_str(&raw).map_err(ApiError::internal("manifest json parse error"))?;
    let Some(fields_json) = root
        .get_mut("config_files")
        .and_then(|v| v.as_array_mut())
        .and_then(|arr| {
            arr.iter_mut()
                .find(|e| e.get("id").and_then(|v| v.as_str()) == Some(mapping.id.as_str()))
        })
        .and_then(|entry| entry.get_mut("fields"))
        .and_then(|v| v.as_array_mut())
    else {
        return Err(ApiError::Internal {
            message: format!("config_files[{}].fields not found in manifest", mapping.id),
        });
    };
    for (name, value) in &updates {
        let Some(field) = fields_json
            .iter_mut()
            .find(|f| f.get("name").and_then(|v| v.as_str()) == Some(name.as_str()))
        else {
            return Err(ApiError::Internal {
                message: format!("field {name} not found in manifest"),
            });
        };
        let Some(obj) = field.as_object_mut() else {
            continue;
        };
        if value.is_null() {
            obj.remove("default");
        } else {
            obj.insert("default".to_string(), (*value).clone());
        }
    }

    // 原子写 + 末尾换行（对齐仓库 end-of-file 约定）
    let mut out = serde_json::to_string_pretty(&root)
        .map_err(ApiError::internal("manifest serialize error"))?;
    out.push('\n');
    let tmp = manifest_path.with_extension("json.tmp");
    std::fs::write(&tmp, out.as_bytes()).map_err(ApiError::internal("write manifest tmp"))?;
    if let Err(e) = std::fs::rename(&tmp, &manifest_path) {
        // rename 失败 best-effort 清 tmp（D7：不留 .tmp 残骸）
        if let Err(cleanup_err) = std::fs::remove_file(&tmp) {
            tracing::warn!(target = %tmp.display(), error = %cleanup_err, "清理 .tmp 残骸失败");
        }
        return Err(ApiError::Internal {
            message: format!("rename manifest: {e}"),
        });
    }

    // 新 ETag 从写入后的值视图派生（先算后移交，new_fields 随后移入内存 manifest）
    let updated = agentos_invoker::shared::config_defaults_from_fields(&new_fields);
    let new_etag = compute_etag(
        serde_json::to_string(&updated)
            .unwrap_or_default()
            .as_bytes(),
    );

    // 内存 manifest 同步：GET/ETag 即时反映新值（watcher 热重载随后接管）
    {
        let mut manifests = state.manifests.write().await;
        if let Some(slot) = manifests.iter_mut().find(|m| m.id == plugin_id) {
            if let Some(cf) = slot.config_files.iter_mut().find(|c| c.id == mapping.id) {
                cf.fields = new_fields;
            }
        }
    }

    Ok(axum::Json(json!({
        "plugin_id": plugin_id,
        "file_id": file_id,
        "etag": new_etag,
    })))
}

// ── 通用用户空间 .env 读写（2026-09-28 配置读写单源化批次 A2）─────────────
// 动态命名的凭证变量（provider key = {PROVIDER_ID}_API_KEY，自定义 provider
// 任意命名）无法走 manifest env-target 静态声明制——本端点是其唯一写面。
// 落点恒用户空间 .env（env_file::env_path_for_root），写后经 invoker 的
// .env mtime 指纹触发 sidecar 热重启生效。

/// 内核级保留变量：AGENTOS_* 是内核行为开关（ADMIN_PASSWORD/DB_PATH/USER_ROOT/
/// CONFIG_ROOT/STORAGE_DRIVER…），系统关键变量不得经配置面改写——422 拒绝。
fn is_reserved_env_name(name: &str) -> bool {
    let upper = name.to_uppercase();
    if upper.starts_with("AGENTOS_") {
        return true;
    }
    matches!(
        upper.as_str(),
        "PATH"
            | "PATHEXT"
            | "SYSTEMROOT"
            | "SYSTEMDRIVE"
            | "WINDIR"
            | "COMSPEC"
            | "HOMEDRIVE"
            | "HOMEPATH"
            | "USERPROFILE"
            | "TEMP"
            | "TMP"
            | "PROGRAMFILES"
            | "PROGRAMDATA"
            | "APPDATA"
            | "LOCALAPPDATA"
            | "USERDOMAIN"
    )
}

fn user_env_masked_view(env_path: &std::path::Path) -> serde_json::Value {
    let text = std::fs::read_to_string(env_path).unwrap_or_default();
    let vars = agentos_mcp::env_file::parse_env_text_for_read(&text);
    let mut masked = serde_json::Map::new();
    for name in vars.keys() {
        masked.insert(name.clone(), serde_json::Value::String("***".to_string()));
    }
    serde_json::Value::Object(masked)
}

/// GET /api/v1/config/env——用户 .env 全量掩码视图（值恒 "***"，仅可见
/// "是否已设置"）+ ETag。消费方：设置页派生 provider key 配置状态。
pub async fn get_user_env_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    let project_root = state.project_root.ok_or_else(|| ApiError::Internal {
        message: "project_root not configured".to_string(),
    })?;
    let env_path = agentos_mcp::env_file::env_path_for_root(&project_root);
    let vars = user_env_masked_view(&env_path);
    let etag = compute_etag(serde_json::to_string(&vars).unwrap_or_default().as_bytes());
    Ok(axum::Json(json!({
        "path": ".env",
        "vars": vars,
        "etag": etag,
    })))
}

/// PUT /api/v1/config/env 请求体：set 写值、unset 清除、哨兵与 ETag 同 env-target 语义。
#[derive(Debug, serde::Deserialize)]
pub struct UserEnvUpdateRequest {
    #[serde(default)]
    pub set: std::collections::HashMap<String, String>,
    #[serde(default)]
    pub unset: Vec<String>,
    pub if_match: Option<String>,
}

/// PUT /api/v1/config/env——保留名拒绝（422）、If-Match 乐观锁（409）、
/// `***` 哨兵跳过、空值移除；落点恒用户空间 .env，原子行级合并。
pub async fn put_user_env_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::Json(req): axum::Json<UserEnvUpdateRequest>,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    let project_root = state.project_root.ok_or_else(|| ApiError::Internal {
        message: "project_root not configured".to_string(),
    })?;
    let env_path = agentos_mcp::env_file::env_path_for_root(&project_root);

    let vars = user_env_masked_view(&env_path);
    let current_etag = compute_etag(serde_json::to_string(&vars).unwrap_or_default().as_bytes());
    if let Some(given) = req.if_match.as_deref() {
        if given != current_etag {
            return Err(ApiError::Conflict {
                message: format!("ETag mismatch: current={current_etag}, given={given:?}"),
            });
        }
    }

    for name in req.set.keys().chain(req.unset.iter()) {
        if is_reserved_env_name(name) {
            return Err(ApiError::UnprocessableEntity {
                message: format!("reserved env var not writable: {name}"),
            });
        }
    }

    let mut updates: Vec<(String, String)> = Vec::new();
    for (name, value) in req.set {
        if value == "***" {
            continue; // 哨兵：保留现值
        }
        updates.push((name, value));
    }
    for name in req.unset {
        updates.push((name, String::new())); // 空值 = 移除该行
    }
    agentos_mcp::env_file::write_env_updates(&env_path, &updates).map_err(|e| {
        ApiError::Internal {
            message: format!("write .env: {e}"),
        }
    })?;

    let new_vars = user_env_masked_view(&env_path);
    let new_etag = compute_etag(
        serde_json::to_string(&new_vars)
            .unwrap_or_default()
            .as_bytes(),
    );
    Ok(axum::Json(json!({
        "ok": true,
        "vars": new_vars,
        "etag": new_etag,
    })))
}

/// 返回带 ETag 头的 GET 响应（覆盖默认 JSON，附加 header）。
pub async fn get_plugin_config_with_etag(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path((plugin_id, file_id)): axum::extract::Path<(String, String)>,
) -> Result<axum::response::Response, ApiError> {
    let axum::Json(resp) = get_plugin_config_handler(
        axum::extract::State(state),
        axum::extract::Path((plugin_id, file_id)),
    )
    .await?;
    let etag = resp.etag.clone();
    Ok(([(axum::http::header::ETAG, etag)], axum::Json(resp)).into_response())
}

// ── 插件管理端点（/api/v1/plugins）——loader 监管能力（内核职责）──
//
// 现存端点：GET /api/v1/plugins（状态清单）与
// PUT /api/v1/plugins/{id}/enabled（启停，写 default_profile.yaml + 热更新）。

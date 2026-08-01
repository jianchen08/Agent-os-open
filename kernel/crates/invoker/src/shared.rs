//! 三种 host_type 共用的逻辑：config 注入、PluginInput 构造。
//!
//! 这些逻辑 sidecar / InProcess / Wasm 都要用，独立成模块避免在 invoker.rs 里
//! 堆叠，也确保三种插件拿到一致的配置和输入。

use agentos_core::traits::{PluginLoader, PluginManifest};
use agentos_core::types::{PluginContext, PluginError};
use serde_json::Value;
use tracing::warn;

/// 加载 manifest 声明的 `config_files` 并按命名空间合并（与 sidecar spawn 同逻辑）。
///
/// `load_config()` → `build_injected_config(full, manifest)`。
/// 解析错误（YAML 语法）上抛；IO/缺文件降级为空对象（同 sidecar）。
///
/// 原生/WASM 插件由此拿到与 sidecar 一致的配置，不再硬编码空 `{}`。
pub async fn injected_config(
    loader: &dyn PluginLoader,
    manifest: &PluginManifest,
) -> Result<Value, PluginError> {
    let full_config = match loader.load_config().await {
        Ok(c) => c,
        Err(e) => {
            if e.code.as_deref().map(|c| c.contains("PARSE")).unwrap_or(false) {
                return Err(PluginError {
                    message: format!("Plugin config parse error: {}", e),
                    code: Some("CONFIG_PARSE_ERROR".to_string()),
                    source: Some("plugin-invoker".to_string()),
                });
            }
            warn!("Failed to load plugin config, using empty: {}", e);
            serde_json::json!({})
        }
    };
    Ok(build_injected_config(&full_config, manifest))
}

/// 构造带 config_files 注入的 PluginInput JSON（native/wasm/sidecar 统一入口）。
///
/// config 来自 [`injected_config`]（manifest config_files 命名空间合并）。
pub async fn build_plugin_input(
    loader: &dyn PluginLoader,
    ctx: &PluginContext,
    manifest: &PluginManifest,
) -> Result<Value, PluginError> {
    let config = injected_config(loader, manifest).await?;
    Ok(serde_json::json!({
        "state": ctx.state,
        "config": config,
        "tenant_id": ctx.tenant.tenant_id,
        "session_id": ctx.tenant.session_id,
    }))
}

/// 按 manifest 的 `config_files` 命名空间合并配置（ADR §4.3，P6：只走 config_files）。
///
/// - 声明了 `config_files`：按 `config_files[].id` 命名空间合并（B3）。
/// - 未声明：收空 Object（P6 删除 config_refs 后不再回退全量）。
pub fn build_injected_config(full_config: &Value, manifest: &PluginManifest) -> Value {
    if manifest.config_files.is_empty() {
        return Value::Object(serde_json::Map::new());
    }
    let mut merged = serde_json::Map::new();
    for mapping in &manifest.config_files {
        let value = resolve_config_path(full_config, &mapping.path)
            .cloned()
            .unwrap_or_else(|| serde_json::json!({}));
        merged.insert(mapping.id.clone(), value);
    }
    Value::Object(merged)
}

/// 按 `config_files[].path` 在递归扫描的 full_config 中定位文件内容。
///
/// 路径归一化：去掉 `config/` 前缀、`.yaml`/`.yml` 后缀，按 `/` 分层下钻。
/// 任一层缺失 → 返回 None（调用方降级为空 dict）。
pub fn resolve_config_path<'a>(full_config: &'a Value, path: &str) -> Option<&'a Value> {
    let normalized = path
        .trim_start_matches("config/")
        .trim_start_matches("config\\");
    let no_ext = normalized
        .strip_suffix(".yaml")
        .or_else(|| normalized.strip_suffix(".yml"))
        .unwrap_or(normalized);
    let mut current = full_config;
    for seg in no_ext.replace('\\', "/").split('/') {
        if seg.is_empty() {
            continue;
        }
        current = current.get(seg)?;
    }
    Some(current)
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::traits::{
        ConfigFileMapping, HostType, ManifestCapabilities, ManifestPermissions, PluginType,
    };
    use serde_json::json;

    fn manifest_with_id(id: &str, config_files: Vec<ConfigFileMapping>) -> PluginManifest {
        PluginManifest {
            id: id.to_string(),
            name: format!("Test {id}"),
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            pipeline_role: None,
            language: "rust".to_string(),
            host_type: HostType::InProcess,
            entry: String::new(),
            capabilities: ManifestCapabilities::default(),
            dependencies: vec![],
            permissions: ManifestPermissions::default(),
            error_policy: Default::default(),
            priority: 50,
            mcp: None,
            native: None,
            wasm: None,
            requires_content: None,
            invoke_entry: None,
            config_files,
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
        }
    }

    /// 有 config_files 时，按 config_files[].id 命名空间合并（B3）。
    #[test]
    fn build_injected_config_uses_config_files_namespaced() {
        let full = json!({
            "models": {
                "llm": {"default_model": "glm"},
                "embedding": {"dim": 1024}
            }
        });
        let manifest = manifest_with_id(
            "llm_service",
            vec![
                ConfigFileMapping {
                    id: "llm".to_string(),
                    path: "config/models/llm.yaml".to_string(),
                    label: "LLM".to_string(),
                },
                ConfigFileMapping {
                    id: "embedding".to_string(),
                    path: "config/models/embedding.yaml".to_string(),
                    label: "Embedding".to_string(),
                },
            ],
        );
        let injected = build_injected_config(&full, &manifest);
        assert_eq!(injected.as_object().unwrap().len(), 2);
        assert_eq!(injected["llm"]["default_model"], "glm");
        assert_eq!(injected["embedding"]["dim"], 1024);
    }

    /// config_files 声明的文件在 full_config 不存在时，该 id 对应空 dict（不崩）。
    #[test]
    fn build_injected_config_missing_file_yields_empty_dict() {
        let full = json!({"models": {"llm": {"name": "glm"}}});
        let manifest = manifest_with_id(
            "p",
            vec![ConfigFileMapping {
                id: "nope".to_string(),
                path: "config/models/nope.yaml".to_string(),
                label: "Nope".to_string(),
            }],
        );
        let injected = build_injected_config(&full, &manifest);
        assert_eq!(injected["nope"], json!({}));
    }

    /// P6：未声明 config_files 的插件收空配置，全量配置里的 secrets 不泄漏。
    #[test]
    fn build_injected_config_no_config_files_yields_empty() {
        let full = json!({
            "models": {"llm": {"name": "glm"}},
            "secrets": {"api_key": "leak"}
        });
        let manifest = manifest_with_id("no_config_plugin", vec![]);
        let injected = build_injected_config(&full, &manifest);
        assert!(
            injected.as_object().map(|o| o.is_empty()).unwrap_or(true),
            "no config_files → empty, got: {injected}"
        );
        assert!(
            injected.get("secrets").is_none(),
            "undeclared secrets must NOT leak"
        );
    }

    #[test]
    fn resolve_config_path_handles_backslash_and_prefix() {
        let full = json!({"x": {"y": 7}});
        assert_eq!(resolve_config_path(&full, "config/x/y.yaml"), Some(&json!(7)));
        assert_eq!(resolve_config_path(&full, "x\\y"), Some(&json!(7)));
        assert_eq!(resolve_config_path(&full, "missing"), None);
    }
}

//! 三种 host_type 共用的逻辑：config 注入、PluginInput 构造。
//!
//! 这些逻辑 sidecar / InProcess 都要用，独立成模块避免在 invoker.rs 里
//! 堆叠，也确保三种插件拿到一致的配置和输入。

use agentos_core::traits::EnvConfigField;
use agentos_core::traits::{PluginLoader, PluginManifest};
use agentos_core::types::{PluginContext, PluginError};
use serde_json::{json, Value};
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
            if e.code
                .as_deref()
                .map(|c| c.contains("PARSE"))
                .unwrap_or(false)
            {
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

/// 构造带 config_files 注入的 PluginInput JSON（native/sidecar 统一入口）。
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

/// 把 manifest 注入命名空间与引擎步骤 config 合成 sidecar 管道调用的 config。
///
/// 注入键 = config_files[].id 命名空间（如 `context_window`）；步骤 config
/// 键 = `inputs`/`_step_method`，置于后者、同名覆盖（当前两键集合不相交，
/// 覆盖序只为语义确定）。
pub fn merge_injected_with_step_config(injected: Value, step_config: &Value) -> Value {
    let mut merged = injected.as_object().cloned().unwrap_or_default();
    if let Some(obj) = step_config.as_object() {
        for (k, v) in obj {
            merged.insert(k.clone(), v.clone());
        }
    }
    Value::Object(merged)
}

/// 按 manifest 的 `config_files` 命名空间合并配置（ADR §4.3，P6：只走 config_files）。
///
/// - 声明了 `config_files`：按 `config_files[].id` 命名空间合并（B3）。
/// - 未声明：收空 Object（P6 删除 config_refs 后不再回退全量）。
///
/// 单一真值（2026-09-02 用户裁定）：内联形态（path 空）真值即
/// [`config_defaults_from_fields`]（fields.default 存在 manifest）；引用形态
/// 真值在文件，缺失时同样以 fields.default 组装兜底（G2 已禁止引用形态声明
/// default，该兜底对引用形态恒为空 dict，仅为直构 manifest 的容错）。
pub fn build_injected_config(full_config: &Value, manifest: &PluginManifest) -> Value {
    if manifest.config_files.is_empty() {
        return Value::Object(serde_json::Map::new());
    }
    let mut merged = serde_json::Map::new();
    for mapping in &manifest.config_files {
        let value = if mapping.path.is_empty() {
            // 内联形态：真值在 manifest，无文件语义
            config_defaults_from_fields(&mapping.fields)
        } else {
            resolve_config_path(full_config, &mapping.path)
                .cloned()
                .unwrap_or_else(|| {
                    // env target 条目（.env 密钥）不进 full_config——密钥无默认语义，保持空 dict
                    if mapping.target.as_deref() == Some("env") {
                        serde_json::json!({})
                    } else {
                        config_defaults_from_fields(&mapping.fields)
                    }
                })
        };
        merged.insert(mapping.id.clone(), value);
    }
    Value::Object(merged)
}

/// 从 `config_files[].fields` 的 `default`（extra 透传）组装命名空间 dict。
///
/// 字段 name 支持点号路径（如 `budgets.l1`、`compression.enabled`），按路径
/// 展开为嵌套 Object；无 `default` 或值为 null 的字段跳过。manifest 因此成为
/// 配置值的单一声明源，磁盘文件不必预置模板（文件缺失即用本函数兜底）。
pub fn config_defaults_from_fields(fields: &[EnvConfigField]) -> Value {
    let mut out = serde_json::Map::new();
    for field in fields {
        let Some(extra) = field.extra.as_ref() else {
            continue;
        };
        let Some(default) = extra.get("default") else {
            continue;
        };
        if default.is_null() {
            continue;
        }
        let segs: Vec<&str> = field.name.split('.').collect();
        set_nested_default(&mut out, &segs, default.clone());
    }
    Value::Object(out)
}

fn set_nested_default(map: &mut serde_json::Map<String, Value>, segs: &[&str], value: Value) {
    debug_assert!(!segs.is_empty());
    if segs.len() == 1 {
        map.insert(segs[0].to_string(), value);
        return;
    }
    let entry = map
        .entry(segs[0].to_string())
        .or_insert_with(|| Value::Object(serde_json::Map::new()));
    if let Value::Object(child) = entry {
        set_nested_default(child, &segs[1..], value);
    }
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

/// 步骤服务调用的约定字段键（服务化提案 §3.4 传输通道）。
///
/// 管道步骤具名调用时，ctx.config 对象携带本键（值为步骤 name），SDK 侧据此
/// 分发到对应注册函数；未携带/值为 execute 时走现行 execute 路径。对齐既有
/// 先例：`tool_call_json`（invoker.rs send_hook_via_execute 注释明示的
/// 「约定字段表达特殊调用」模式）——本设计是该模式的第二次应用。
pub const STEP_METHOD_KEY: &str = "_step_method";

/// 构造步骤服务调用的额外 config：在既有 config 对象上设置约定字段
/// `_step_method`（值为步骤 name），幂等（重复设置覆盖为最新值）。
///
/// 非对象 config（如标量/数组）不合法——步骤服务调用方（引擎接线）恒传
/// 对象形态；防御性处理：非对象时包成 `{"_step_method": <name>}` 新对象
/// （与 `_plugin_id` 注入先例同构，见 PluginScopedRouter）。
pub fn with_step_method(mut config: Value, step_method: &str) -> Value {
    if let Some(obj) = config.as_object_mut() {
        obj.insert(
            STEP_METHOD_KEY.to_string(),
            Value::String(step_method.to_string()),
        );
        config
    } else {
        json!({ STEP_METHOD_KEY: step_method })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::traits::{
        ConfigFileMapping, EnvConfigField, HostType, ManifestCapabilities, ManifestPermissions,
        PluginType,
    };
    use serde_json::json;

    fn manifest_with_id(id: &str, config_files: Vec<ConfigFileMapping>) -> PluginManifest {
        PluginManifest {
            id: id.to_string(),
            name: format!("Test {id}"),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            pipeline_role: None,
            language: "rust".to_string(),
            host_type: HostType::InProcess,
            host_group: None,
            entry: String::new(),
            capabilities: ManifestCapabilities::default(),
            requires_services: vec![],
            permissions: ManifestPermissions::default(),
            priority: 50,
            mcp: None,
            lifecycle: None,
            native: None,
            granted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files,
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
            export_fields: vec![],
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
                    target: None,
                    settings: None,
                    fields: vec![],
                },
                ConfigFileMapping {
                    id: "embedding".to_string(),
                    path: "config/models/embedding.yaml".to_string(),
                    label: "Embedding".to_string(),
                    target: None,
                    settings: None,
                    fields: vec![],
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
                target: None,
                settings: None,
                fields: vec![],
            }],
        );
        let injected = build_injected_config(&full, &manifest);
        assert_eq!(injected["nope"], json!({}));
    }

    /// manifest 内联默认（ADR 2026-09-02）：文件缺失时按 fields.default 组装，
    /// 点号路径展开为嵌套 Object；null 默认跳过。
    #[test]
    fn build_injected_config_missing_file_falls_back_to_field_defaults() {
        let full = json!({});
        let manifest = manifest_with_id(
            "pipeline_context_window_guard",
            vec![ConfigFileMapping {
                id: "context_window".to_string(),
                path: "config/system/context_window_config.yaml".to_string(),
                label: "上下文窗口配置".to_string(),
                target: None,
                settings: None,
                fields: vec![
                    EnvConfigField {
                        name: "compress_trigger_ratio".to_string(),
                        label: "压缩触发比例".to_string(),
                        field_type: "slider".to_string(),
                        required: false,
                        description: None,
                        extra: Some(
                            json!({"default": 0.55, "min": 0, "max": 1})
                                .as_object()
                                .unwrap()
                                .clone(),
                        ),
                    },
                    EnvConfigField {
                        name: "budgets.l1".to_string(),
                        label: "预算·L1 记忆".to_string(),
                        field_type: "slider".to_string(),
                        required: false,
                        description: None,
                        extra: Some(json!({"default": 0.1}).as_object().unwrap().clone()),
                    },
                    // null 默认（如压缩模型留空）不进入组装
                    EnvConfigField {
                        name: "compression.model".to_string(),
                        label: "压缩模型".to_string(),
                        field_type: "string".to_string(),
                        required: false,
                        description: None,
                        extra: Some(json!({"default": null}).as_object().unwrap().clone()),
                    },
                ],
            }],
        );
        let injected = build_injected_config(&full, &manifest);
        assert_eq!(injected["context_window"]["compress_trigger_ratio"], 0.55);
        assert_eq!(injected["context_window"]["budgets"]["l1"], 0.1);
        assert!(
            injected["context_window"]["compression"].is_null()
                || injected["context_window"]["compression"]["model"].is_null()
                || injected["context_window"]["compression"]
                    .get("model")
                    .is_none()
        );
    }

    /// 内联形态（path 空，单一真值裁定 2026-09-02）：真值 = fields.default，
    /// 不做文件查找（full_config 为空也不影响）。
    #[test]
    fn build_injected_config_inline_entry_uses_field_defaults() {
        let full = json!({});
        let manifest = manifest_with_id(
            "pipeline_context_window_guard",
            vec![ConfigFileMapping {
                id: "context_window".to_string(),
                path: String::new(),
                label: "上下文窗口配置".to_string(),
                target: None,
                settings: None,
                fields: vec![EnvConfigField {
                    name: "compress_trigger_ratio".to_string(),
                    label: "压缩触发比例".to_string(),
                    field_type: "slider".to_string(),
                    required: false,
                    description: None,
                    extra: Some(json!({"default": 0.06}).as_object().unwrap().clone()),
                }],
            }],
        );
        let injected = build_injected_config(&full, &manifest);
        assert_eq!(injected["context_window"]["compress_trigger_ratio"], 0.06);
    }

    /// merge_injected_with_step_config：注入命名空间与步骤 config 合并，
    /// 步骤键（inputs/_step_method）齐全。
    #[test]
    fn merge_injected_with_step_config_overlays_step_keys() {
        let injected = json!({"context_window": {"compress_trigger_ratio": 0.06}});
        let step = json!({"inputs": {"a": 1}, "_step_method": "execute"});
        let merged = merge_injected_with_step_config(injected, &step);
        assert_eq!(merged["context_window"]["compress_trigger_ratio"], 0.06);
        assert_eq!(merged["inputs"]["a"], 1);
        assert_eq!(merged["_step_method"], "execute");
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
        assert_eq!(
            resolve_config_path(&full, "config/x/y.yaml"),
            Some(&json!(7))
        );
        assert_eq!(resolve_config_path(&full, "x\\y"), Some(&json!(7)));
        assert_eq!(resolve_config_path(&full, "missing"), None);
    }

    // ── 步骤服务约定字段透传（服务化提案 §3.4）──

    /// 幂等设置：既有 config 对象保留原键，_step_method 覆盖为最新值。
    #[test]
    fn with_step_method_sets_idempotently_and_preserves_keys() {
        let config = json!({"inputs": {"k": "v"}, "other": 1});
        let once = with_step_method(config.clone(), "task.remind");
        assert_eq!(once.get(STEP_METHOD_KEY), Some(&json!("task.remind")));
        assert_eq!(once.get("other"), Some(&json!(1)), "原键保留");
        assert_eq!(once.get("inputs"), Some(&json!({"k": "v"})), "原键保留");
        // 幂等：重复设置覆盖为最新值，不产生重复键
        let twice = with_step_method(once, "task.inject_params");
        assert_eq!(
            twice.get(STEP_METHOD_KEY),
            Some(&json!("task.inject_params"))
        );
        assert_eq!(twice.get("other"), Some(&json!(1)));
        assert_eq!(twice.as_object().map(|o| o.len()), Some(3), "键数不变");
    }

    /// 非对象 config（防御分支）：包成新对象，不 panic 不吞。
    #[test]
    fn with_step_method_wraps_non_object_config() {
        let wrapped = with_step_method(json!(42), "task.remind");
        assert_eq!(wrapped.get(STEP_METHOD_KEY), Some(&json!("task.remind")));
        let wrapped_arr = with_step_method(json!([1, 2]), "task.remind");
        assert_eq!(
            wrapped_arr.get(STEP_METHOD_KEY),
            Some(&json!("task.remind"))
        );
    }
}

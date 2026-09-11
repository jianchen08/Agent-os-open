//! 三种 host_type 共用的逻辑：config 注入、PluginInput 构造。
//!
//! 这些逻辑 sidecar / InProcess 都要用，独立成模块避免在 invoker.rs 里
//! 堆叠，也确保三种插件拿到一致的配置和输入。

use std::borrow::Cow;

use agentos_core::traits::EnvConfigField;
use agentos_core::traits::{PluginLoader, PluginManifest};
use agentos_core::types::PluginError;
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
            warn!(
                "Failed to load plugin config for '{}', using empty: {}",
                manifest.id, e
            );
            serde_json::json!({})
        }
    };
    Ok(build_injected_config(&full_config, manifest))
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

/// state 切片投喂的控制键白名单（`state.reads` 非空时恒带的小键）。
///
/// 管道控制面小键（身份/路由/生命周期信号），体积可忽略；插件按声明切片
/// 投喂时恒带这些键，免去每个插件逐一声明控制键，插件侧对控制键的读取
/// 预期也不随读面声明漂移。state 中不存在的白名单键省略（缺省略）。
pub const STATE_SLICE_CONTROL_KEYS: &[&str] = &[
    "pipeline_id",
    "session_id",
    "thread_id",
    "message_id",
    "run_id",
    "task_id",
    "workspace",
    "conversation_mode",
    "core_type",
    "core_plugin",
    "suspended",
    "ended",
    "next_phase",
    "agent.id",
    "model_tier",
    "agent_level",
];

/// manifest `state.reads` 的非空声明视图：`Some(reads)` = 按声明切片投喂；
/// `None`（无 state 段 / reads 为空）= 全量投喂（迁移期默认，行为零变化）。
pub fn declared_reads(manifest: &PluginManifest) -> Option<&[String]> {
    manifest
        .state
        .as_ref()
        .map(|s| s.reads.as_slice())
        .filter(|r| !r.is_empty())
}

/// 投喂 state 载荷（sidecar/native 两路共用的单点）：声明非空 → 切片对象
/// （Owned）；未声明 → 全量 state 借用（Borrowed，零拷贝——迁移期默认，
/// 行为与开销零变化）。
pub fn state_feed_payload<'a>(manifest: &PluginManifest, ctx_state: &'a Value) -> Cow<'a, Value> {
    match declared_reads(manifest) {
        Some(reads) => Cow::Owned(assemble_state_slice(ctx_state, reads)),
        None => Cow::Borrowed(ctx_state),
    }
}

/// native 路径的投喂 state JSON 串（PluginCtx.state_json）。
pub fn pipeline_state_json(manifest: &PluginManifest, ctx_state: &Value) -> String {
    match state_feed_payload(manifest, ctx_state) {
        Cow::Owned(v) => serde_json::to_string(&v).unwrap_or_else(|_| "{}".into()),
        Cow::Borrowed(v) => serde_json::to_string(v).unwrap_or_else(|_| "{}".into()),
    }
}

/// 平键优先的 state 取值（同引擎 Path 求值惯例，engine::condition::eval_value）：
/// 先按整体平键查（本仓 state 惯例——插件 state_updates 平插 "task.status" 等
/// 点键，不拆点），未命中再按点链嵌套解析（嵌套写入方兼容不变）。未命中 None。
fn resolve_state_key<'a>(state: &'a Value, key: &str) -> Option<&'a Value> {
    if let Some(v) = state.get(key) {
        return Some(v);
    }
    let mut cur = state;
    for seg in key.split('.') {
        cur = cur.get(seg)?;
    }
    Some(cur)
}

/// 按目标插件 `state.reads` 声明组装投喂 state 切片（sidecar/native 两路共用）。
///
/// 投喂 state = 控制键白名单（恒带，[`STATE_SLICE_CONTROL_KEYS`]）∪ 声明键切片。
/// 声明条目三形态：
/// - 键名（含点键）：按 [`resolve_state_key`] 平键优先取值；state 缺该键 →
///   省略不报错（缺省略契约）。
/// - `messages`：state["messages"] 数组全量。
/// - `messages_tail:N`：该数组末尾 N 条（投喂键仍为 `messages`；N ≥ 数组长度 =
///   全数组；loader 装载期已过滤非法 N，此处解析失败按缺键省略）。
///
/// 同键重复声明后者覆盖前者（声明序确定，无歧义）。
pub fn assemble_state_slice(full_state: &Value, reads: &[String]) -> Value {
    let mut out = serde_json::Map::new();
    for key in STATE_SLICE_CONTROL_KEYS {
        if let Some(v) = resolve_state_key(full_state, key) {
            out.insert((*key).to_string(), v.clone());
        }
    }
    for entry in reads {
        if let Some((key, value)) = resolve_read_entry(full_state, entry) {
            out.insert(key, value);
        }
    }
    Value::Object(out)
}

/// 解析单条 reads 声明为 (投喂键, 值)；非法/缺键返回 None（调用方省略）。
fn resolve_read_entry(state: &Value, entry: &str) -> Option<(String, Value)> {
    if let Some(n) = entry.strip_prefix("messages_tail:") {
        let n: usize = n.parse().ok()?;
        let arr = state.get("messages")?.as_array()?;
        let start = arr.len().saturating_sub(n);
        return Some(("messages".to_string(), Value::Array(arr[start..].to_vec())));
    }
    // 键名形态（含 "messages" 全量——键名直取本身就是数组全量语义）。
    resolve_state_key(state, entry).map(|v| (entry.to_string(), v.clone()))
}

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
        PluginType, StateDeclaration,
    };
    use serde_json::json;

    fn manifest_with_id(id: &str, config_files: Vec<ConfigFileMapping>) -> PluginManifest {
        PluginManifest {
            force_include_tools: Vec::new(),
            state: None,
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

    // ── state.reads 切片组装（W2b）──────────────────────────────────

    fn manifest_with_reads(reads: Vec<&str>) -> PluginManifest {
        let mut m = manifest_with_id("slice_plugin", vec![]);
        m.state = Some(StateDeclaration {
            reads: reads.into_iter().map(str::to_string).collect(),
            ..Default::default()
        });
        m
    }

    /// 声明门控：无 state 段 / 空 reads → None（全量投喂）；非空声明 → Some(条目)。
    /// 未声明分支的字节等价契约在 [`pipeline_state_json_undeclared_is_full_state_bytes`]。
    #[test]
    fn declared_reads_gate_absent_or_empty_is_none() {
        let no_state = manifest_with_id("p1", vec![]);
        let empty_state = manifest_with_reads(vec![]);
        assert!(declared_reads(&no_state).is_none(), "无 state 段 → 全量");
        assert!(declared_reads(&empty_state).is_none(), "空 reads → 全量");
        let declared = manifest_with_reads(vec!["messages", "task.status"]);
        let reads = declared_reads(&declared).expect("非空声明应 Some");
        assert_eq!(
            reads,
            ["messages".to_string(), "task.status".to_string()],
            "声明条目原样透出"
        );
    }

    /// 未声明（无 state 段 / 空 reads）→ 投喂串与全量 state 序列化**字节等价**
    /// （迁移期行为零变化的契约钉死）；声明非空 → 切片串化。
    #[test]
    fn pipeline_state_json_undeclared_is_full_state_bytes() {
        let full = json!({
            "messages": [1, 2, 3],
            "task.status": "running",
            "unrelated_secret": "must-be-present-when-undeclared"
        });
        let no_state = manifest_with_id("p1", vec![]);
        assert_eq!(
            pipeline_state_json(&no_state, &full),
            serde_json::to_string(&full).unwrap(),
            "无 state 段 → 全量字节等价"
        );
        let empty_state = manifest_with_reads(vec![]);
        assert_eq!(
            pipeline_state_json(&empty_state, &full),
            serde_json::to_string(&full).unwrap(),
            "空 reads → 全量字节等价"
        );

        // 区分度输入（符号相反）：声明非空 → 未声明键不在投喂串里。
        let declared = manifest_with_reads(vec!["task.status", "messages"]);
        let out = pipeline_state_json(&declared, &full);
        let parsed: Value = serde_json::from_str(&out).expect("投喂串必为合法 JSON");
        assert_eq!(parsed["task.status"], "running");
        assert_eq!(parsed["messages"], json!([1, 2, 3]));
        assert!(
            !out.contains("unrelated_secret"),
            "未声明键不得出现在投喂串：{out}"
        );
    }

    /// 载荷借用契约：未声明 → Borrowed（零拷贝，指针同一）；声明 → Owned 切片。
    #[test]
    fn state_feed_payload_borrowed_when_undeclared_owned_when_declared() {
        let full = json!({"a": 1, "messages": [1]});
        let undeclared = manifest_with_id("p", vec![]);
        match state_feed_payload(&undeclared, &full) {
            Cow::Borrowed(v) => assert!(std::ptr::eq(v, &full), "未声明必须借用全量"),
            Cow::Owned(_) => panic!("未声明不得拷贝"),
        }
        let declared = manifest_with_reads(vec!["a"]);
        match state_feed_payload(&declared, &full) {
            Cow::Owned(v) => assert_eq!(v["a"], 1, "声明 → 切片对象"),
            Cow::Borrowed(_) => panic!("声明必须组装切片"),
        }
    }

    /// 切片命中（两组区分度输入）：声明键 + 白名单控制键进切片、未声明且
    /// 非白名单的键被排除；messages 全量形态原数组整体进切片。
    #[test]
    fn assemble_state_slice_hits_declared_and_whitelist_excludes_rest() {
        // 组 1：管道控制面小键 + 点键声明 + 大键排除。
        let state1 = json!({
            "pipeline_id": "pipe-1",
            "session_id": "sess-1",
            "run_id": "run-1",
            "agent.id": "main",
            "task.status": "completed",
            "raw_tool_calls": [{"name": "bash_execute"}],
            "huge_undeclared": {"blob": "x".repeat(100)},
        });
        let fed1 = assemble_state_slice(
            &state1,
            &["task.status".to_string(), "raw_tool_calls".to_string()],
        );
        assert_eq!(fed1["pipeline_id"], "pipe-1", "白名单键恒带");
        assert_eq!(fed1["session_id"], "sess-1");
        assert_eq!(fed1["run_id"], "run-1");
        assert_eq!(fed1["agent.id"], "main", "白名单点键恒带");
        assert_eq!(fed1["task.status"], "completed", "声明键命中");
        assert_eq!(
            fed1["raw_tool_calls"],
            json!([{"name": "bash_execute"}]),
            "声明键命中"
        );
        assert!(
            fed1.get("huge_undeclared").is_none(),
            "未声明且非白名单的键必须排除：{fed1}"
        );

        // 性质断言：切片键集 ⊆ 白名单 ∪ 声明键集（信息面收缩不变量）。
        let declared: std::collections::HashSet<&str> =
            ["task.status", "raw_tool_calls"].into_iter().collect();
        let allowed: std::collections::HashSet<&str> = STATE_SLICE_CONTROL_KEYS
            .iter()
            .copied()
            .chain(declared.iter().copied())
            .collect();
        for key in fed1.as_object().unwrap().keys() {
            assert!(allowed.contains(key.as_str()), "切片出现越界键 {key}");
        }

        // 组 2：符号量级相反的区分度输入——声明面不同（messages 全量 + 另一
        // 点键），白名单键大面积缺席（缺省略，不造键）。
        let state2 = json!({
            "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "tool"}],
            "model_tier": "heavy",
            "task.owned.depth": 2,
        });
        let fed2 = assemble_state_slice(
            &state2,
            &["messages".to_string(), "task.owned.depth".to_string()],
        );
        assert_eq!(
            fed2["messages"],
            json!([{"role": "user"}, {"role": "assistant"}, {"role": "tool"}]),
            "messages 全量形态 = 原数组整体"
        );
        assert_eq!(fed2["model_tier"], "heavy");
        assert_eq!(fed2["task.owned.depth"], 2);
        assert!(
            fed2.get("pipeline_id").is_none(),
            "state 缺席的白名单键省略（缺省略，不造键）"
        );
    }

    /// tail:N 语义：投喂键仍为 messages、取末尾 N 条；N ≥ 数组长度 = 全数组；
    /// 单调性质——tail(k) 是 tail(k+1) 的后缀、len(tail(N)) == min(N, len)。
    #[test]
    fn assemble_state_slice_messages_tail_semantics_and_monotonicity() {
        let state = json!({"messages": [0, 1, 2, 3, 4]});

        // 命中：末尾 2 条，键名仍为 messages。
        let fed_tail2 = assemble_state_slice(&state, &["messages_tail:2".to_string()]);
        assert_eq!(fed_tail2["messages"], json!([3, 4]));

        // N 大于数组长度 = 全尾（不报错、不缺省略）。
        let fed_tail99 = assemble_state_slice(&state, &["messages_tail:99".to_string()]);
        assert_eq!(fed_tail99["messages"], json!([0, 1, 2, 3, 4]));

        // 性质断言（tail 单调性 + 长度律）：k 从 0 到 len，
        // len(tail(k)) == min(k, len) 且 tail(k) 是 tail(k+1) 的后缀、
        // 也是全数组的后缀。
        let full_arr = state["messages"].as_array().unwrap().clone();
        let len = full_arr.len();
        let mut prev: Option<Vec<Value>> = None;
        for k in 1..=(len + 2) {
            let fed = assemble_state_slice(&state, &[format!("messages_tail:{k}")]);
            let got = fed["messages"].as_array().unwrap().clone();
            assert_eq!(got.len(), k.min(len), "len(tail:{k}) == min(k, len)");
            assert_eq!(
                got,
                full_arr[len - got.len()..].to_vec(),
                "tail:{k} 必须是全数组的后缀"
            );
            if let Some(p) = prev {
                assert_eq!(
                    got[got.len() - p.len()..].to_vec(),
                    p,
                    "tail(k) 是 tail(k+1) 的后缀（单调性）"
                );
            }
            prev = Some(got);
        }
    }

    /// 缺省略契约：声明了 state 中不存在的键（含 tail 声明而 messages 缺席/
    /// 非数组）→ 该键省略不报错；白名单缺席键同样省略。
    #[test]
    fn assemble_state_slice_missing_keys_omitted_silently() {
        let state = json!({
            "pipeline_id": "pipe-9",
            "messages": "not-an-array",
        });
        let fed = assemble_state_slice(
            &state,
            &[
                "ghost.key".to_string(),
                "messages_tail:5".to_string(),
                "another_missing".to_string(),
            ],
        );
        assert_eq!(fed["pipeline_id"], "pipe-9", "存在键照常投喂");
        assert!(fed.get("ghost.key").is_none(), "不存在键省略");
        assert!(
            fed.get("messages").is_none(),
            "messages 非数组时 tail 声明省略不报错"
        );
        assert!(fed.get("session_id").is_none(), "白名单缺席键省略");

        // 区分度输入 2：messages 整键缺席时 messages 全量声明同样省略。
        let state2 = json!({"pipeline_id": "pipe-10"});
        let fed2 = assemble_state_slice(&state2, &["messages".to_string()]);
        assert!(fed2.get("messages").is_none());
        assert_eq!(fed2["pipeline_id"], "pipe-10");
    }

    /// 平键优先（同引擎 Path 求值惯例）：点键先按整体平键查，命中即取；
    /// 平键缺失再按点链嵌套解析（嵌套写入方兼容）。
    #[test]
    fn assemble_state_slice_flat_key_first_then_nested_fallback() {
        // 平键命中：嵌套同路径存在时以平键为准。
        let state = json!({
            "task.status": "flat-wins",
            "task": {"status": "nested-loses", "extra": "only-nested"},
        });
        let fed = assemble_state_slice(&state, &["task.status".to_string()]);
        assert_eq!(fed["task.status"], "flat-wins", "平键优先");

        // 平键缺失 → 点链嵌套解析兜底（平键形态投喂，插件侧平键可读）。
        let state2 = json!({"agent": {"id": "nested-agent"}});
        let fed2 = assemble_state_slice(&state2, &["agent.id".to_string()]);
        assert_eq!(fed2["agent.id"], "nested-agent", "嵌套兜底解析");

        // 两处都无 → 省略。
        let state3 = json!({"task": {"other": 1}});
        let fed3 = assemble_state_slice(&state3, &["task.status".to_string()]);
        assert!(fed3.get("task.status").is_none());
    }
}

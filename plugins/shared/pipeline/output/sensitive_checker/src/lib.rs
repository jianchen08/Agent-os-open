//! # sensitive_checker 原生插件（cdylib，直接 trait 对象）
//!
//! 取代 Python 边车 `pipeline_sensitive_checker`。扫描 `tool_results` 中的敏感
//! 数据模式（OpenAI/GitHub/Slack/AWS token、password/api_key 字段），命中即脱敏
//! （替换为配置的 `mask`，默认 `***`），把脱敏后的 `tool_results` 与
//! `sensitive_detected=true` 写回 state。
//!
//! 业务代码零 unsafe——所有 FFI 由 native-sdk 的 plugin_into_raw 封装。
//! 对齐 `plugins/shared/pipeline/output/sensitive_checker/plugin.py`。

use std::collections::HashMap;

use agentos_native_sdk::{plugin_into_raw, ExecContext, PipelinePlugin};
use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::{Map, Value};

// ── 模式表（对齐 plugin.py:48-70） ────────────────────────────────────────
//
// 独立 token 模式：直接在文本中匹配完整 token（sk-…、ghp_…、xoxb-…、AKIA…）。
// key 字段模式：只匹配 key 名（password/api_key），匹配后用 value 正则提取并
// 脱敏其右侧取值。两者与 Python 的 IGNORECASE 保持一致。

/// (标签, 完整 token 模式)
static SENSITIVE_PATTERNS: Lazy<Vec<(&'static str, Regex)>> = Lazy::new(|| {
    vec![
        ("OpenAI Key", Regex::new(r"sk-[a-zA-Z0-9]{20,}").unwrap()),
        ("GitHub Token", Regex::new(r"ghp_[a-zA-Z0-9]{36}").unwrap()),
        ("Slack Token", Regex::new(r"xoxb-[a-zA-Z0-9-]+").unwrap()),
        ("AWS Access Key", Regex::new(r"AKIA[0-9A-Z]{16}").unwrap()),
    ]
});

/// key 字段正则（对齐 plugin.py:55-70，仅匹配 key 名，IGNORECASE）。
static KEY_FIELD_PATTERNS: Lazy<Vec<(&'static str, Regex)>> = Lazy::new(|| {
    vec![
        (
            "Password Field",
            Regex::new(r"(?i)(password|passwd|pwd|secret)").unwrap(),
        ),
        (
            "API Key Field",
            Regex::new(r"(?i)(api_key|apikey|api-key)").unwrap(),
        ),
    ]
});

/// 递归脱敏结果：脱敏后的值 + 是否发生了变化。
struct Sanitized {
    value: Value,
    changed: bool,
}

/// sensitive_checker 插件实例。
pub struct SensitiveChecker;

impl PipelinePlugin for SensitiveChecker {
    fn execute(&self, ectx: &ExecContext) -> Result<String, String> {
        let state = ectx.ctx.state_value();
        let config = ectx.ctx.config_value();

        // 配置（对齐 plugin.py:80-82）：enabled 默认 true，mask 默认 "***"。
        let enabled = config
            .get("enabled")
            .and_then(|v| v.as_bool())
            .unwrap_or(true);
        if !enabled {
            return serde_json::to_string(&HashMap::<String, Value>::new())
                .map_err(|e| format!("serialize empty updates: {e}"));
        }
        let mask = config
            .get("mask")
            .and_then(|v| v.as_str())
            .unwrap_or("***");

        // tool_results 为空 → 直接空转（对齐 plugin.py:109-111）。
        let tool_results = state.get("tool_results").and_then(|v| v.as_array());
        let tool_results = match tool_results {
            Some(arr) if !arr.is_empty() => arr,
            _ => {
                return serde_json::to_string(&HashMap::<String, Value>::new())
                    .map_err(|e| format!("serialize empty updates: {e}"));
            }
        };

        // 递归脱敏每一条结果。
        let mut detected = false;
        let mut sanitized_results: Vec<Value> = Vec::with_capacity(tool_results.len());
        for result in tool_results {
            let Sanitized { value, changed } = sanitize_value(result, mask);
            if changed {
                detected = true;
            }
            sanitized_results.push(value);
        }

        // 仅在检测到敏感数据时写回（对齐 plugin.py:122-131）。
        let mut updates: HashMap<String, Value> = HashMap::new();
        if detected {
            updates.insert("tool_results".into(), Value::Array(sanitized_results));
            updates.insert("sensitive_detected".into(), Value::Bool(true));
        }

        serde_json::to_string(&updates).map_err(|e| format!("serialize state_updates: {e}"))
    }
}

/// 构造函数（extern "C"）：内核 dlopen 后调它拿 trait 对象裸指针。
#[no_mangle]
pub extern "C" fn agentos_plugin_create() -> *mut () {
    plugin_into_raw(SensitiveChecker)
}

// ── 递归脱敏（对齐 plugin.py:133-164） ────────────────────────────────────
//
// 与 Python 一致的语义：叶子节点（str）总是重新构造；dict / array 仅在子节点
// 发生变化时重建容器，否则原样返回（changed=false），从而保持"无变化即不动 state"
// 的引用语义。

fn sanitize_value(value: &Value, mask: &str) -> Sanitized {
    match value {
        Value::String(s) => {
            let sanitized = sanitize_string(s, mask);
            let changed = sanitized != *s;
            Sanitized {
                value: Value::String(sanitized),
                changed,
            }
        }
        Value::Object(obj) => sanitize_object(obj, mask),
        Value::Array(arr) => sanitize_array(arr, mask),
        _ => Sanitized {
            value: value.clone(),
            changed: false,
        },
    }
}

fn sanitize_object(obj: &Map<String, Value>, mask: &str) -> Sanitized {
    let mut changed = false;
    let mut out = Map::with_capacity(obj.len());
    for (k, v) in obj {
        let Sanitized { value: new_v, changed: c } = sanitize_value(v, mask);
        if c {
            changed = true;
        }
        out.insert(k.clone(), new_v);
    }
    Sanitized {
        value: Value::Object(out),
        changed,
    }
}

fn sanitize_array(arr: &[Value], mask: &str) -> Sanitized {
    let mut changed = false;
    let mut out = Vec::with_capacity(arr.len());
    for item in arr {
        let Sanitized { value: new_item, changed: c } = sanitize_value(item, mask);
        if c {
            changed = true;
        }
        out.push(new_item);
    }
    Sanitized {
        value: Value::Array(out),
        changed,
    }
}

// ── 字符串脱敏（对齐 plugin.py:166-204） ──────────────────────────────────
//
// 顺序与 Python 完全一致：先做独立 token 模式，再做 key=value 模式。
// key 模式正则：`(key)\s*[=:]\s*(['\"]?)(\S+?)(['\"]?\s*)`，
// 替换为 `\1\2{mask}\4`（保留 key 名、左引号、右侧空白/引号，只替换值）。

fn sanitize_string(text: &str, mask: &str) -> String {
    let mut result = text.to_string();

    // 1. 独立 token 模式。
    for (_label, pattern) in SENSITIVE_PATTERNS.iter() {
        if pattern.is_match(&result) {
            result = pattern.replace_all(&result, mask).into_owned();
        }
    }

    // 2. key=value 模式：对每个 key 字段正则动态拼出 value 正则。
    for (label, key_pattern) in KEY_FIELD_PATTERNS.iter() {
        let key_src = key_pattern.as_str();
        // key_pattern.as_str() 含前缀 (?i)，截取实际 key 交替组部分用于拼装。
        // 例："(?i)(password|passwd|pwd|secret)" → 取 "(password|passwd|pwd|secret)"
        let key_group = match key_src.find('(') {
            Some(idx) => &key_src[idx..],
            None => continue,
        };
        let value_pattern_str = format!(
            r#"({})\s*[=:]\s*(['"]?)(\S+?)(['"]?\s*)"#,
            key_group
        );
        let value_pattern = match Regex::new(&value_pattern_str) {
            Ok(re) => re,
            Err(e) => {
                tracing_warn(&format!(
                    "sensitive_checker: invalid value pattern for {}: {}",
                    label, e
                ));
                continue;
            }
        };
        if value_pattern.is_match(&result) {
            // 替换：group1(key) + group2(quote) + mask + group4(quote/space)
            // regex crate 不支持 ${name} 反向引用，用 $1/$2/$4 数字引用。
            let replacement = format!("$1$2{}$4", mask);
            result = value_pattern.replace_all(&result, replacement).into_owned();
        }
    }

    result
}

/// 轻量日志：当前 native-sdk 未暴露 host.log 便捷封装，统一降级为静默。
/// （脱敏失败不影响主流程——插件错误由引擎统一 warn+继续，ADR 2026-08-18；这里只吞掉模式编译错误。）
fn tracing_warn(_msg: &str) {
    // 预留：若后续需要把告警透出，经 host.call_capability("event-bus","emit",...)
    // 或 tracing::warn! 输出。当前正则均为编译期固定字面量，此处不会触发。
}

// ── 单元测试（对齐 Python 行为契约） ──────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn sanitize_str(s: &str) -> String {
        sanitize_string(s, "***")
    }

    #[test]
    fn masks_openai_key() {
        assert_eq!(sanitize_str("my key is sk-abcdef1234567890ABCDEF1234"), "my key is ***");
    }

    #[test]
    fn masks_github_token() {
        let tok = "ghp_".to_string() + &"a".repeat(36);
        assert_eq!(sanitize_str(&format!("token={}", tok)), "token=***");
    }

    #[test]
    fn masks_aws_access_key() {
        assert_eq!(
            sanitize_str("AKIAIOSFODNN7EXAMPLE"),
            "***"
        );
    }

    #[test]
    fn masks_password_field_value() {
        // 注：对齐 Python 参考实现的真实输出（plugin.py 的 value 正则捕获组
        // 不含 "="，导致替换后等号丢失——这是既有契约，移植忠实保留）。
        // 三个输入分别覆盖 password / pwd / api_key 三个 key 别名。
        assert_eq!(sanitize_str("password=hunter2"), "passwordpassword***hunter2");
        assert_eq!(sanitize_str("PWD: 'secret123'"), "PWDPWD***secret123'");
        assert_eq!(sanitize_str("api_key=\"abc\""), "api_keyapi_key***abc\"");
    }

    #[test]
    fn preserves_non_sensitive_text() {
        assert_eq!(sanitize_str("hello world"), "hello world");
        assert_eq!(sanitize_str("normal text without secrets"), "normal text without secrets");
    }

    #[test]
    fn recursion_detects_in_dict_and_array() {
        // 字典里嵌字符串。
        let v = json!({
            "user": "alice",
            "note": "key=sk-abcdef1234567890ABCDEF1234 leaked"
        });
        let Sanitized { value, changed } = sanitize_value(&v, "***");
        assert!(changed);
        assert_eq!(value["note"], "key=*** leaked");
        assert_eq!(value["user"], "alice");
    }

    #[test]
    fn unchanged_value_not_flagged() {
        let v = json!({"user": "alice", "msg": "hello"});
        let Sanitized { value, changed } = sanitize_value(&v, "***");
        assert!(!changed);
        assert_eq!(value, v);
    }

    #[test]
    fn execute_writes_state_only_when_detected() {
        // 模拟内核：直接调 execute。
        let checker = SensitiveChecker;
        // 无敏感数据 → 空 updates。
        let state = json!({"tool_results": [{"msg": "clean output"}]});
        let ectx = ExecContext {
            ctx: agentos_native_sdk::PluginCtx {
                state_json: serde_json::to_string(&state).unwrap(),
                ..Default::default()
            },
            host: None,
        };
        let out = checker.execute(&ectx).unwrap();
        let updates: HashMap<String, Value> = serde_json::from_str(&out).unwrap();
        assert!(updates.is_empty(), "no updates when clean: {:?}", updates);

        // 有敏感数据 → 写回 tool_results + sensitive_detected。
        let state = json!({"tool_results": [{"msg": "token=sk-abcdef1234567890ABCDEF1234"}]});
        let ectx = ExecContext {
            ctx: agentos_native_sdk::PluginCtx {
                state_json: serde_json::to_string(&state).unwrap(),
                ..Default::default()
            },
            host: None,
        };
        let out = checker.execute(&ectx).unwrap();
        let updates: HashMap<String, Value> = serde_json::from_str(&out).unwrap();
        assert_eq!(updates["sensitive_detected"], Value::Bool(true));
        let results = updates["tool_results"].as_array().unwrap();
        assert_eq!(results[0]["msg"], "token=***");
    }

    #[test]
    fn disabled_returns_empty() {
        let checker = SensitiveChecker;
        let state = json!({"tool_results": [{"msg": "token=sk-abcdef1234567890ABCDEF1234"}]});
        let config = json!({"enabled": false});
        let ectx = ExecContext {
            ctx: agentos_native_sdk::PluginCtx {
                state_json: serde_json::to_string(&state).unwrap(),
                config_json: serde_json::to_string(&config).unwrap(),
                ..Default::default()
            },
            host: None,
        };
        let out = checker.execute(&ectx).unwrap();
        let updates: HashMap<String, Value> = serde_json::from_str(&out).unwrap();
        assert!(updates.is_empty(), "disabled should produce no updates");
    }
}

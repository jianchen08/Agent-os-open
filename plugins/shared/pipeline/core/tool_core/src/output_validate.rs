//! 工具输出 JSON Schema 校验（task_dsh_plugin_adapter 任务 1）。
//!
//! 内核在 pipeline 启动时把 `tool_output_contracts`（tool_name → {schema, render}）
//! 注入 state（server.rs inject_tool_schemas）。tool_core 执行工具成功后按
//! `schema` 校验返回值——这是 output_schema 的第一个消费端：此前该字段全链路
//! 零消费（41 工具仅 3 个填写，声明无意义）。
//!
//! 实现为**常用子集**校验器（不引 jsonschema 全量 crate——输出契约当前只用到
//! 这些关键字，DSH defineTool 的 output.schema 同样限于此子集）：
//! `type`（含数组多型）/ `properties` / `required` / `items` / `enum`。
//! `additionalProperties`/`$ref`/组合关键字**忽略**（宽松）——契约目的是兜底
//! 拦截结构性漂移（字段缺失/类型错位），不是严格模式校验。
//!
//! 校验失败 → fail-closed：结果转 `ToolResult::failed`（错误信息带违规点），
//! LLM 收到错误并自我修正；插件错误由引擎统一 warn+继续管道（ADR 2026-08-18，
//! 对齐 DSH `tools/post-execute` 兜底语义，见 docs/dsh_hook_translation.md）。
//!
//! 可配置：state["tool_output_validation"] = "off" 时整体跳过（默认开启）。

use serde_json::Value;

/// 校验 data 是否符合 output_schema（常用子集）。
///
/// 返回 None 表示通过；Some(err) 为首个违规点的人类可读描述（JSON Pointer 风格路径）。
pub fn validate(schema: &Value, data: &Value) -> Option<String> {
    validate_at(schema, data, "")
}

fn validate_at(schema: &Value, data: &Value, path: &str) -> Option<String> {
    let Some(obj) = schema.as_object() else {
        return None; // 非 object 的 schema（true/缺失）不做约束
    };

    // type：单字符串或字符串数组（["string","null"] 等）。
    if let Some(ty) = obj.get("type") {
        if let Some(err) = check_type(ty, data, path) {
            return Some(err);
        }
    }

    // enum：值必须命中其一（比较用 JSON 等值）。
    if let Some(serde_json::Value::Array(variants)) = obj.get("enum") {
        if !variants.iter().any(|v| json_eq(v, data)) {
            return Some(format!(
                "{path}: value {} not in enum [{}]",
                truncate(data),
                variants.iter().map(truncate).collect::<Vec<_>>().join(", ")
            ));
        }
    }

    // object：required + properties 递归。
    if let (Some(serde_json::Value::Array(required)), Some(props)) = (obj.get("required"), data.as_object()) {
        for key in required {
            if let Some(k) = key.as_str() {
                if !props.contains_key(k) {
                    return Some(format!("{path}: missing required field `{k}`"));
                }
            }
        }
    }
    if let (Some(serde_json::Value::Object(prop_schemas)), Some(props)) =
        (obj.get("properties"), data.as_object())
    {
        for (key, prop_schema) in prop_schemas {
            if let Some(value) = props.get(key) {
                let child = format!("{path}.{key}");
                if let Some(err) = validate_at(prop_schema, value, &child) {
                    return Some(err);
                }
            }
        }
    }

    // array：items 逐元素递归。
    if let (Some(item_schema), Some(items)) = (obj.get("items"), data.as_array()) {
        for (i, item) in items.iter().enumerate() {
            let child = format!("{path}[{i}]");
            if let Some(err) = validate_at(item_schema, item, &child) {
                return Some(err);
            }
        }
    }

    None
}

/// type 关键字检查（integer 与 number：整数收窄检查——1.0 视作 integer 兼容）。
fn check_type(ty: &Value, data: &Value, path: &str) -> Option<String> {
    let matches = |t: &str| -> bool {
        match t {
            "object" => data.is_object(),
            "array" => data.is_array(),
            "string" => data.is_string(),
            "boolean" => data.is_boolean(),
            "null" => data.is_null(),
            "number" => data.is_number(),
            "integer" => data.is_i64() || data.is_u64() || {
                // f64-backed 整值（json!(1.0)）兼容——serde_json 的 as_i64 对
                // 浮点载体返回 None，按数值判断（宽松，与文档声明一致）。
                data.as_f64().map(|f| f.fract() == 0.0).unwrap_or(false)
            },
            _ => true, // 未知类型名不约束（宽松）
        }
    };
    let ok = match ty {
        Value::String(t) => matches(t),
        Value::Array(ts) => ts.iter().any(|t| t.as_str().map(matches).unwrap_or(true)),
        _ => true,
    };
    if ok {
        None
    } else {
        Some(format!(
            "{path}: expected type {}, got {} ({})",
            truncate(ty),
            json_type_name(data),
            truncate(data)
        ))
    }
}

fn json_type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "boolean",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

/// JSON 等值比较（serde_json Number 的 1 与 1.0 不等——这里数值统一转 f64 宽松比较）。
fn json_eq(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => {
            x.as_f64().zip(y.as_f64()).map(|(x, y)| x == y).unwrap_or(false)
        }
        _ => a == b,
    }
}

fn truncate(v: &Value) -> String {
    let s = v.to_string();
    s.chars().take(80).collect()
}

/// 从 state 取工具输出校验开关（"off" 关闭，默认开启）。
pub fn validation_enabled(state: &Value) -> bool {
    state.get("tool_output_validation").and_then(|v| v.as_str()) != Some("off")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn bash_output_schema() -> Value {
        // 真实样本：plugins/shared/tools/bash/plugin.json 的 output_schema。
        json!({
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["completed", "running", "terminated"]},
                "pid": {"type": "integer"},
                "exit_code": {"type": "integer"},
                "output": {"type": "string"},
                "summary": {"type": "array", "items": {"type": "string"}},
                "elapsed": {"type": "number"},
                "message": {"type": "string"},
                "error": {"type": "string"}
            },
            "required": ["status"]
        })
    }

    #[test]
    fn valid_result_passes() {
        let data = json!({"status": "completed", "exit_code": 0, "output": "hi", "elapsed": 1.5});
        assert_eq!(validate(&bash_output_schema(), &data), None);
    }

    #[test]
    fn missing_required_fails_with_pointer() {
        let data = json!({"exit_code": 0});
        let err = validate(&bash_output_schema(), &data).unwrap();
        assert!(err.contains("missing required field `status`"), "{err}");
    }

    #[test]
    fn wrong_type_fails() {
        let data = json!({"status": "completed", "exit_code": "0"});
        let err = validate(&bash_output_schema(), &data).unwrap();
        assert!(err.contains(".exit_code"), "{err}");
        assert!(err.contains("expected type"), "{err}");
    }

    #[test]
    fn enum_mismatch_fails() {
        let data = json!({"status": "zombie"});
        let err = validate(&bash_output_schema(), &data).unwrap();
        assert!(err.contains("not in enum"), "{err}");
    }

    #[test]
    fn nested_items_checked() {
        let schema = json!({
            "type": "object",
            "properties": {
                "lines": {"type": "array", "items": {"type": "object", "properties": {
                    "number": {"type": "integer"}, "text": {"type": "string"}
                }, "required": ["number", "text"]}}
            },
            "required": ["lines"]
        });
        let bad = json!({"lines": [{"number": 1, "text": "ok"}, {"number": "2", "text": "x"}]});
        let err = validate(&schema, &bad).unwrap();
        assert!(err.contains(".lines[1].number"), "{err}");
    }

    #[test]
    fn multi_type_null_union() {
        let schema = json!({"type": ["string", "null"]});
        assert_eq!(validate(&schema, &json!("x")), None);
        assert_eq!(validate(&schema, &json!(null)), None);
        assert!(validate(&schema, &json!(5)).is_some());
    }

    #[test]
    fn integer_accepts_integral_float() {
        let schema = json!({"type": "integer"});
        assert_eq!(validate(&schema, &json!(1.0)), None);
        assert!(validate(&schema, &json!(1.5)).is_some());
    }

    #[test]
    fn non_object_schema_lenient() {
        assert_eq!(validate(&json!(true), &json!("anything")), None);
        assert_eq!(validate(&Value::Null, &json!(42)), None);
    }

    #[test]
    fn switch_off() {
        let state = json!({"tool_output_validation": "off"});
        assert!(!validation_enabled(&state));
        assert!(validation_enabled(&json!({})));
    }
}

//! # 模板插值器
//!
//! 解析配置里的 `{{...}}` 模板表达式，支持从 `state` 取字段以及读取相对路径文件内容。
//!
//! ## 支持的语法
//!
//! - `{{state.xxx}}` —— 从 `state`（`serde_json::Value`）取字段，支持点链，
//!   例如 `{{state.agent_id}}`、`{{state.user.name}}`。
//! - `{{path:相对路径}}` —— 读取文件内容（相对于项目根），例如
//!   `{{path:config/agents/main/persona/agentos_persona.md}}`。
//! - 字段/文件不存在时静默降级为空字符串（不报错）。
//! - 其它无法识别的表达式当作字面量原样保留。
//!
//! [来源: docs/tasks 0.2 引擎模板插值器]

use std::path::Path;

use serde_json::Value;

/// 解析模板字符串，返回替换后的字符串。
///
/// - `template`: 含 `{{...}}` 的字符串
/// - `state`: 当前状态（`serde_json::Value`）
/// - `project_root`: 项目根，用于 `path:` 解析
///
/// 输出为替换后的字符串。无法识别的表达式原样保留。
pub fn render_template(template: &str, state: &Value, project_root: &Path) -> String {
    // 手写扫描：找出所有 `{{ ... }}` 区段并替换。
    // 不使用 regex 是为了避免给 engine 引入额外依赖。
    let bytes = template.as_bytes();
    let mut out = String::with_capacity(template.len());
    let mut i = 0;

    while i < bytes.len() {
        // 查找下一个 `{{`
        if bytes[i] == b'{' && i + 1 < bytes.len() && bytes[i + 1] == b'{' {
            // 寻找对应的 `}}`
            if let Some(rel_end) = find_close(&bytes[i..]) {
                // rel_end 指向 `}}` 的第一个 `{` 的下标（相对于 i）
                let expr_start = i + 2;
                let expr_end = i + rel_end;
                let expr = &template[expr_start..expr_end];
                let replaced = render_expr(expr.trim(), state, project_root);
                out.push_str(&replaced);
                // 跳过 `{{ ... }}`
                i = i + rel_end + 2;
                continue;
            } else {
                // 没有匹配的 `}}`：剩余部分作为字面量输出
                out.push_str(&template[i..]);
                break;
            }
        }
        // 普通字符直接追加
        // 这里用 char_indices 风格更稳妥，但为了简单逐字节也安全：
        // 找到下一个 UTF-8 字符边界
        let next_ch = template[i..].chars().next().expect("non-empty slice");
        out.push(next_ch);
        i += next_ch.len_utf8();
    }

    out
}

/// 在切片中查找 `}}` 的起始下标（即第一个 `}` 的相对位置）。
/// 切片以 `{{` 开头。返回值是 `}` 第一个字符相对位置；找不到返回 `None`。
fn find_close(slice: &[u8]) -> Option<usize> {
    let mut j = 2; // 跳过开头的 `{{`
    while j + 1 < slice.len() {
        if slice[j] == b'}' && slice[j + 1] == b'}' {
            return Some(j);
        }
        j += 1;
    }
    None
}

/// 解析单个表达式（已 trim），返回替换后的字符串。
fn render_expr(expr: &str, state: &Value, project_root: &Path) -> String {
    if let Some(path) = expr.strip_prefix("path:") {
        // 文件读取：失败静默降级为空串
        let trimmed = path.trim();
        let full = project_root.join(trimmed);
        std::fs::read_to_string(&full).unwrap_or_default()
    } else if let Some(rest) = expr.strip_prefix("state.") {
        // state 点链取值：不存在返回空串
        state_lookup(state, rest).unwrap_or_default()
    } else {
        // 无法识别 → 当字面量（恢复原始 {{...}} 形式）
        format!("{{{{{}}}}}", expr)
    }
}

/// 从 `state` 按 `a.b.c` 点链逐层取值，返回字符串化结果。
/// 取不到（字段缺失或中间节点非对象）返回 `None`。
fn state_lookup(state: &Value, path: &str) -> Option<String> {
    let mut current = state;
    for key in path.split('.') {
        let key = key.trim();
        if key.is_empty() {
            return None;
        }
        let obj = current.as_object()?;
        current = obj.get(key)?;
    }
    value_to_string(current)
}

/// 把叶子节点 `Value` 转为字符串：String 原样，Number/Bool 取原始字面量。
/// Null/Array/Object 不视为标量叶子，返回 `None`（进而降级为空串）。
fn value_to_string(v: &Value) -> Option<String> {
    match v {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        Value::Bool(b) => Some(b.to_string()),
        _ => None,
    }
}

/// 递归渲染一个 `serde_json::Value`：对 String 调用 `render_template`，
/// Object/Array 深度遍历渲染每个字符串值，其它类型原样返回。
pub fn render_value(value: &Value, state: &Value, project_root: &Path) -> Value {
    match value {
        Value::String(s) => Value::String(render_template(s, state, project_root)),
        Value::Array(arr) => {
            let mapped: Vec<Value> = arr
                .iter()
                .map(|v| render_value(v, state, project_root))
                .collect();
            Value::Array(mapped)
        }
        Value::Object(obj) => {
            let mapped: serde_json::Map<String, Value> = obj
                .iter()
                .map(|(k, v)| (k.clone(), render_value(v, state, project_root)))
                .collect();
            Value::Object(mapped)
        }
        other => other.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::fs;
    use std::io::Write;

    #[test]
    fn test_render_state_field() {
        let state = json!({ "agent_id": "agent-007" });
        let root = Path::new(".");
        let out = render_template("id={{state.agent_id}}", &state, root);
        assert_eq!(out, "id=agent-007");
    }

    #[test]
    fn test_render_state_dot_chain() {
        let state = json!({
            "user": { "name": "alice", "profile": { "age": 30 } }
        });
        let root = Path::new(".");
        assert_eq!(
            render_template("{{state.user.name}}", &state, root),
            "alice"
        );
        // 数字也应当能字符串化
        assert_eq!(
            render_template("{{state.user.profile.age}}", &state, root),
            "30"
        );
    }

    #[test]
    fn test_render_state_missing() {
        let state = json!({ "agent_id": "x" });
        let root = Path::new(".");
        // 字段不存在 → 空串
        assert_eq!(
            render_template("[{{state.nonexistent}}]", &state, root),
            "[]"
        );
        // 中间节点缺失也应当返回空串
        assert_eq!(render_template("[{{state.user.name}}]", &state, root), "[]");
    }

    #[test]
    fn test_render_path() {
        // 用 tempfile 建一个临时目录 + 文件，project_root 指向临时目录
        let dir = tempfile::tempdir().expect("create tempdir");
        let file_path = dir.path().join("persona.md");
        let mut f = fs::File::create(&file_path).expect("create file");
        f.write_all(b"hello persona").expect("write");
        drop(f);

        let out = render_template("{{path:persona.md}}", &json!({}), dir.path());
        assert_eq!(out, "hello persona");
    }

    #[test]
    fn test_render_path_missing() {
        let dir = tempfile::tempdir().expect("create tempdir");
        let out = render_template("[{{path:does/not/exist.md}}]", &json!({}), dir.path());
        assert_eq!(out, "[]");
    }

    #[test]
    fn test_render_no_template() {
        let state = json!({});
        let root = Path::new(".");
        let src = "just plain text, nothing to replace";
        assert_eq!(render_template(src, &state, root), src);
    }

    #[test]
    fn test_render_value_recursive() {
        let state = json!({ "agent_id": "A1", "count": 5 });
        let root = Path::new(".");
        let input = json!({
            "name": "{{state.agent_id}}",
            "nested": {
                "deep": "n={{state.agent_id}}",
                "missing": "[{{state.nope}}]",
                "num": 42,
                "bool": true,
                "null": null
            },
            "list": [
                "{{state.agent_id}}",
                { "inner": "{{state.count}}" },
                "plain"
            ]
        });
        let out = render_value(&input, &state, root);
        assert_eq!(out["name"], json!("A1"));
        assert_eq!(out["nested"]["deep"], json!("n=A1"));
        assert_eq!(out["nested"]["missing"], json!("[]"));
        // 非字符串标量保持原样
        assert_eq!(out["nested"]["num"], json!(42));
        assert_eq!(out["nested"]["bool"], json!(true));
        assert_eq!(out["nested"]["null"], Value::Null);
        // 数组递归
        assert_eq!(out["list"][0], json!("A1"));
        assert_eq!(out["list"][1]["inner"], json!("5"));
        assert_eq!(out["list"][2], json!("plain"));
    }

    #[test]
    fn test_render_mixed() {
        let dir = tempfile::tempdir().expect("create tempdir");
        let file_path = dir.path().join("x.txt");
        fs::write(&file_path, "FILE").expect("write");

        let state = json!({ "agent_id": "ag-1" });
        let src = "agent={{state.agent_id}}, file={{path:x.txt}}, miss=[{{state.none}}]";
        let out = render_template(src, &state, dir.path());
        assert_eq!(out, "agent=ag-1, file=FILE, miss=[]");
    }

    #[test]
    fn test_render_unknown_expr_literal() {
        // 未识别前缀 → 原样保留
        let state = json!({});
        let root = Path::new(".");
        assert_eq!(
            render_template("v={{foobar.baz}}", &state, root),
            "v={{foobar.baz}}"
        );
    }

    #[test]
    fn test_render_multiple_templates() {
        let state = json!({ "a": "1", "b": "2" });
        let root = Path::new(".");
        let out = render_template("{{state.a}}-{{state.b}}-{{state.a}}", &state, root);
        assert_eq!(out, "1-2-1");
    }

    #[test]
    fn test_render_utf8_safe() {
        // 确保多字节字符不被破坏
        let state = json!({ "name": "你好" });
        let root = Path::new(".");
        let out = render_template("世界, {{state.name}}!", &state, root);
        assert_eq!(out, "世界, 你好!");
    }
}

//! JSON 容错修复（对齐 Python `_message_normalizer.py:18-194` 的 `repair_json_string`）。
//!
//! LLM 返回的工具参数 JSON 经常不规范（markdown 包裹、尾逗号、单引号、
//! 截断、注释等）。本模块按 7 步状态机尝试修复，返回合法 JSON 字符串。
//!
//! 纯算法，零依赖外部状态。

/// 尝试修复常见的 JSON 格式问题，返回修复后的 JSON 字符串。失败返回 None。
///
/// 对齐 Python `repair_json_string` 的 7 步：
/// 0. 去 markdown code block 包裹
/// 1. 直接解析（已是合法则原样返回）
/// 2. 提取首个完整 `{...}` 对象（括号匹配 + 字符串/转义状态机）
/// 3. 去尾逗号 `,\s*([}\]])` → `\1`
/// 4. 单引号 → 双引号（仅当单引号多于双引号）
/// 5. 截断修复（状态机闭合未结束字符串 + 补全括号）
/// 6. 去 `//` 和 `/* */` 注释
pub fn repair_json_string(input: &str) -> Option<String> {
    if input.is_empty() {
        return None;
    }
    let mut s = input.trim().to_string();
    if s.is_empty() {
        return None;
    }

    // 尝试 0：去 markdown code block 包裹。
    if s.starts_with("```") {
        let lines: Vec<&str> = s.split('\n').collect();
        let start = if lines.is_empty() { 0 } else { 1 };
        let mut end = lines.len();
        if !lines.is_empty() && lines.last().map(|l| l.trim() == "```").unwrap_or(false) {
            end -= 1;
        }
        s = lines[start.min(end)..end.min(lines.len())].join("\n").trim().to_string();
    }

    // 尝试 1：直接解析。
    if serde_json::from_str::<serde_json::Value>(&s).is_ok() {
        return Some(s);
    }

    // 尝试 2：提取首个完整 {...}。
    if let Some(found) = extract_first_object(&s) {
        if serde_json::from_str::<serde_json::Value>(&found).is_ok() {
            return Some(found);
        }
    }

    // 尝试 3：去尾逗号。
    let fixed = strip_trailing_commas(&s);
    if fixed != s && serde_json::from_str::<serde_json::Value>(&fixed).is_ok() {
        return Some(fixed);
    }

    // 尝试 4：单引号 → 双引号（仅当单引号明显多于双引号）。
    if s.chars().filter(|&c| c == '\'').count() > s.chars().filter(|&c| c == '"').count() {
        let fixed = s.replace('\'', "\"");
        if serde_json::from_str::<serde_json::Value>(&fixed).is_ok() {
            return Some(fixed);
        }
    }

    // 尝试 5：截断修复。
    if let Some(repaired) = repair_truncation(&s) {
        return Some(repaired);
    }

    // 尝试 6：去注释。
    let fixed = strip_comments(&s);
    if fixed != s && serde_json::from_str::<serde_json::Value>(&fixed).is_ok() {
        return Some(fixed);
    }

    None
}

/// 提取第一个完整的 JSON 对象 `{...}`（括号匹配 + 字符串/转义状态机）。
fn extract_first_object(s: &str) -> Option<String> {
    let bytes = s.as_bytes();
    let first_brace = s.find('{')?;
    let mut depth = 0i32;
    let mut in_string = false;
    let mut escape_next = false;

    for i in first_brace..s.len() {
        let c = bytes[i] as char;
        if escape_next {
            escape_next = false;
            continue;
        }
        if c == '\\' {
            escape_next = true;
            continue;
        }
        if c == '"' {
            in_string = !in_string;
            continue;
        }
        if in_string {
            continue;
        }
        if c == '{' {
            depth += 1;
        } else if c == '}' {
            depth -= 1;
            if depth == 0 {
                return Some(s[first_brace..=i].to_string());
            }
        }
    }
    None
}

/// 去掉对象/数组中尾逗号：`...,\s*}` 或 `...,\s*]` → 去逗号。
fn strip_trailing_commas(s: &str) -> String {
    // 用状态机扫描，避免误伤字符串内的 `,}`。
    let bytes = s.as_bytes();
    let mut out = String::with_capacity(s.len());
    let mut in_string = false;
    let mut escape_next = false;
    let mut i = 0;
    while i < bytes.len() {
        let c = bytes[i] as char;
        if escape_next {
            escape_next = false;
            out.push(c);
            i += 1;
            continue;
        }
        if c == '\\' {
            escape_next = true;
            out.push(c);
            i += 1;
            continue;
        }
        if c == '"' {
            in_string = !in_string;
            out.push(c);
            i += 1;
            continue;
        }
        // 字符串外遇到 `,` 后跟（可选空白）`}` 或 `]`：跳过逗号。
        if !in_string && c == ',' {
            let mut j = i + 1;
            while j < bytes.len() && (bytes[j] == b' ' || bytes[j] == b'\t' || bytes[j] == b'\n' || bytes[j] == b'\r') {
                j += 1;
            }
            if j < bytes.len() && (bytes[j] == b'}' || bytes[j] == b']') {
                i += 1; // 跳过逗号，保留后续空白与括号
                continue;
            }
        }
        out.push(c);
        i += 1;
    }
    out
}

/// 修复被截断的 JSON：闭合未结束的字符串并补全括号。
///
/// 对齐 Python `_repair_truncation`：状态机扫描记录结构栈与最后一个完整字段边界，
/// 三步尝试（闭合字符串→仅补括号→回退到最后完整字段）。
fn repair_truncation(s: &str) -> Option<String> {
    let bytes = s.as_bytes();
    let mut in_string = false;
    let mut escape_next = false;
    let mut stack: Vec<char> = Vec::new();
    let mut last_complete_idx: isize = -1; // 顶层对象内最后一个逗号 = 完整字段边界

    for i in 0..bytes.len() {
        let c = bytes[i] as char;
        if in_string {
            if escape_next {
                escape_next = false;
            } else if c == '\\' {
                escape_next = true;
            } else if c == '"' {
                in_string = false;
            }
            continue;
        }
        if c == '"' {
            in_string = true;
        } else if c == '{' {
            stack.push('{');
        } else if c == '[' {
            stack.push('[');
        } else if c == '}' {
            if stack.last() == Some(&'{') {
                stack.pop();
            }
        } else if c == ']' {
            if stack.last() == Some(&'[') {
                stack.pop();
            }
        } else if c == ',' && stack.len() <= 1 {
            last_complete_idx = i as isize;
        }
    }

    if stack.is_empty() {
        return None; // 无未闭合结构，不是截断
    }

    // 步骤 1：截断在字符串内部 → 闭合引号（先处理结尾悬空反斜杠）。
    if in_string {
        let base = if escape_next { &s[..s.len().saturating_sub(1)] } else { s };
        let candidate = close_braces(&format!("{base}\""));
        if serde_json::from_str::<serde_json::Value>(&candidate).is_ok() {
            return Some(candidate);
        }
    }

    // 步骤 2：仅补全括号（字符串都已闭合、只缺右括号）。
    let candidate = close_braces(s);
    if serde_json::from_str::<serde_json::Value>(&candidate).is_ok() {
        return Some(candidate);
    }

    // 步骤 3：回退到最后一个完整字段边界，丢弃不完整尾部。
    if last_complete_idx > 0 {
        let prefix = &s[..last_complete_idx as usize];
        let candidate = close_braces(prefix);
        if serde_json::from_str::<serde_json::Value>(&candidate).is_ok() {
            return Some(candidate);
        }
    }

    None
}

/// 为 prefix 补全当前未闭合的括号（按结构栈逆序）。
///
/// 对齐 Python `_close_braces`：重新扫描 prefix 重建结构栈，末尾补对应右括号。
fn close_braces(prefix: &str) -> String {
    let bytes = prefix.as_bytes();
    let mut st: Vec<char> = Vec::new();
    let mut in_s = false;
    let mut esc = false;
    for &b in bytes {
        let ch = b as char;
        if in_s {
            if esc {
                esc = false;
            } else if ch == '\\' {
                esc = true;
            } else if ch == '"' {
                in_s = false;
            }
            continue;
        }
        if ch == '"' {
            in_s = true;
        } else if ch == '{' || ch == '[' {
            st.push(ch);
        } else if ch == '}' && st.last() == Some(&'{') {
            st.pop();
        } else if ch == ']' && st.last() == Some(&'[') {
            st.pop();
        }
    }
    let suffix: String = st.iter().rev().map(|&c| if c == '{' { '}' } else { ']' }).collect();
    format!("{prefix}{suffix}")
}

/// 去掉 `//` 行注释和 `/* */` 块注释（字符串外）。
fn strip_comments(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = String::with_capacity(s.len());
    let mut in_string = false;
    let mut escape_next = false;
    let mut i = 0;
    while i < bytes.len() {
        let c = bytes[i] as char;
        if escape_next {
            escape_next = false;
            out.push(c);
            i += 1;
            continue;
        }
        if c == '\\' {
            escape_next = true;
            out.push(c);
            i += 1;
            continue;
        }
        if c == '"' {
            in_string = !in_string;
            out.push(c);
            i += 1;
            continue;
        }
        if !in_string && c == '/' && i + 1 < bytes.len() {
            // 行注释 //
            if bytes[i + 1] == b'/' {
                i += 2;
                while i < bytes.len() && bytes[i] != b'\n' {
                    i += 1;
                }
                continue;
            }
            // 块注释 /* */
            if bytes[i + 1] == b'*' {
                i += 2;
                while i + 1 < bytes.len() && !(bytes[i] == b'*' && bytes[i + 1] == b'/') {
                    i += 1;
                }
                i = (i + 2).min(bytes.len());
                continue;
            }
        }
        out.push(c);
        i += 1;
    }
    out.trim().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn valid_json_unchanged() {
        assert_eq!(repair_json_string(r#"{"a":1}"#), Some(r#"{"a":1}"#.into()));
    }

    #[test]
    fn markdown_unwrap() {
        let input = "```json\n{\"a\":1}\n```";
        assert_eq!(repair_json_string(input), Some(r#"{"a":1}"#.into()));
    }

    #[test]
    fn trailing_comma() {
        assert_eq!(repair_json_string(r#"{"a":1,}"#), Some(r#"{"a":1}"#.into()));
    }

    #[test]
    fn single_quotes() {
        assert_eq!(repair_json_string("{'a':1}"), Some(r#"{"a":1}"#.into()));
    }

    #[test]
    fn truncated_string() {
        // 截断在字符串内部 → 闭合引号 + 补括号
        let fixed = repair_json_string(r#"{"text":"hello wo"#).unwrap();
        assert!(serde_json::from_str::<serde_json::Value>(&fixed).is_ok());
    }

    #[test]
    fn extract_object_from_noise() {
        let s = "prefix text {\"a\":1} trailing";
        assert_eq!(repair_json_string(s), Some(r#"{"a":1}"#.into()));
    }

    #[test]
    fn unfixable_returns_none() {
        assert_eq!(repair_json_string("not json at all !!!"), None);
    }

    #[test]
    fn line_comment() {
        // 删注释后保留结构（Python 版亦留空白），能解析即成功。
        let fixed = repair_json_string("{\n  // comment\n  \"a\":1\n}").unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&fixed).unwrap();
        assert_eq!(parsed["a"], 1);
    }
}

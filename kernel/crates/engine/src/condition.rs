//! 安全条件表达式解析器（0.1 `src/pipeline/condition_parser.py` 的 Rust 移植）。
//!
//! 替换 eval() 用于路由条件的求值。支持比较操作、布尔逻辑和 state 字段访问，
//! 不使用任何动态求值，杜绝代码注入风险。
//!
//! 支持的条件语法（default.yaml 实际用到的最小集）：
//!     True / False                                    — 布尔字面量
//!     core_type == 'llm_call'                         — 变量 == 字符串
//!     pause_guard.checked.paused == True              — 点链访问 + == 布尔
//!     raw_tool_calls != []                            — 变量 != 空列表
//!     text_only == true and no_new_input == true      — and 逻辑
//!
//! 保留 0.1 已有的扩展语法：
//!     or / not
//!     == / != / > / < / >= / <=
//!     点链访问 a.b.c（嵌套 dict.get）
//!     字面量：字符串（单/双引号）、数字、True/False/None、列表 [...]
//!
//! 优先级（低到高）：or < and < not < comparison < primary

use serde_json::Value;

/// 安全求值条件表达式。
///
/// 输入: `condition`（条件字符串，如 `"core_type == 'llm_call'"`），`state`（当前状态 serde_json::Value）
/// 输出: 求值结果 bool；解析/求值异常返回 `false`（与 0.1 行为一致的安全兜底）。
pub fn eval_condition(condition: &str, state: &Value) -> bool {
    let expr = condition.trim();
    if expr.is_empty() {
        // 与 0.1 一致：空表达式视为 true（无条件）
        return true;
    }

    let tokens = match tokenize(expr) {
        Ok(t) => t,
        Err(_) => return false,
    };
    if tokens.is_empty() {
        return true;
    }

    let mut parser = Parser::new(tokens, state);
    match parser.parse() {
        Some(v) => value_truthy(&v),
        None => false,
    }
}

// ===========================================================================
// Token
// ===========================================================================

#[derive(Debug, Clone, PartialEq)]
enum TokKind {
    String,    // "..." 或 '...'
    Number,    // 123 / 1.5
    Bool,      // True / False / None
    Keyword,   // and / or / not
    Op,        // == / != / > / < / >= / <=
    Dot,       // .
    Ident,     // 标识符
    LBracket,  // [
    RBracket,  // ]
    LParen,    // (
    RParen,    // )
    Comma,     // ,
}

#[derive(Debug, Clone)]
struct Token {
    kind: TokKind,
    value: String,
}

/// 手写扫描器：从表达式字符串产生 token 序列。无法识别的字符返回 Err。
fn tokenize(expr: &str) -> Result<Vec<Token>, String> {
    let chars: Vec<char> = expr.chars().collect();
    let mut tokens = Vec::new();
    let mut i = 0;
    let n = chars.len();

    while i < n {
        let c = chars[i];

        // 跳过空白
        if c.is_whitespace() {
            i += 1;
            continue;
        }

        // 字符串字面量（单/双引号）
        if c == '\'' || c == '"' {
            let quote = c;
            let start = i + 1;
            i += 1;
            while i < n && chars[i] != quote {
                i += 1;
            }
            if i >= n {
                return Err(format!("Unterminated string literal at position {}", start));
            }
            // chars[start..i] 即引号内内容
            let body: String = chars[start..i].iter().collect();
            i += 1; // 消耗结束引号
            tokens.push(Token { kind: TokKind::String, value: body });
            continue;
        }

        // 数字字面量（含可选小数）；负号这里不特殊处理，交给上下文（与 0.1 一致：-?\d+\.?\d*）
        // 0.1 用正则把 -? 算进数字，但那会吞掉 "a - 1" 中的 "-1"。这里只解析无符号数字，保持简单与安全。
        if c.is_ascii_digit() {
            let start = i;
            while i < n && chars[i].is_ascii_digit() {
                i += 1;
            }
            if i < n && chars[i] == '.' {
                i += 1;
                while i < n && chars[i].is_ascii_digit() {
                    i += 1;
                }
            }
            let body: String = chars[start..i].iter().collect();
            tokens.push(Token { kind: TokKind::Number, value: body });
            continue;
        }

        // 标识符 / 关键字 / 布尔字面量
        if c.is_ascii_alphabetic() || c == '_' {
            let start = i;
            while i < n && (chars[i].is_ascii_alphanumeric() || chars[i] == '_') {
                i += 1;
            }
            let word: String = chars[start..i].iter().collect();
            // 大小写不敏感地识别布尔/None 字面量与逻辑关键字（对齐 JSON 的 true/false 与
            // 0.1 的 True/False/None；default.yaml 实际出现小写 true）。
            match word.to_lowercase().as_str() {
                "true" | "false" | "none" => {
                    tokens.push(Token { kind: TokKind::Bool, value: word.to_lowercase() })
                }
                "and" | "or" | "not" => {
                    tokens.push(Token { kind: TokKind::Keyword, value: word.to_lowercase() })
                }
                _ => tokens.push(Token { kind: TokKind::Ident, value: word }),
            }
            continue;
        }

        // 比较运算符（多字符优先）
        if c == '=' || c == '!' || c == '<' || c == '>' {
            if i + 1 < n && chars[i + 1] == '=' {
                let op: String = format!("{}=", c);
                i += 2;
                tokens.push(Token { kind: TokKind::Op, value: op });
                continue;
            }
            if c == '=' {
                return Err(format!("Unexpected '=' at position {}", i));
            }
            // <, > 单字符
            tokens.push(Token { kind: TokKind::Op, value: c.to_string() });
            i += 1;
            continue;
        }

        // 单字符 token
        match c {
            '.' => {
                tokens.push(Token { kind: TokKind::Dot, value: c.to_string() });
                i += 1;
            }
            '[' => {
                tokens.push(Token { kind: TokKind::LBracket, value: c.to_string() });
                i += 1;
            }
            ']' => {
                tokens.push(Token { kind: TokKind::RBracket, value: c.to_string() });
                i += 1;
            }
            '(' => {
                tokens.push(Token { kind: TokKind::LParen, value: c.to_string() });
                i += 1;
            }
            ')' => {
                tokens.push(Token { kind: TokKind::RParen, value: c.to_string() });
                i += 1;
            }
            ',' => {
                tokens.push(Token { kind: TokKind::Comma, value: c.to_string() });
                i += 1;
            }
            _ => return Err(format!("Unexpected character '{}' at position {}", c, i)),
        }
    }

    Ok(tokens)
}

// ===========================================================================
// Parser — 递归下降，求值同步进行
// ===========================================================================

struct Parser<'a> {
    tokens: Vec<Token>,
    pos: usize,
    state: &'a Value,
}

impl<'a> Parser<'a> {
    fn new(tokens: Vec<Token>, state: &'a Value) -> Self {
        Self { tokens, pos: 0, state }
    }

    fn peek(&self) -> Option<&Token> {
        self.tokens.get(self.pos)
    }

    fn advance(&mut self) -> Option<Token> {
        let tok = self.tokens.get(self.pos).cloned();
        if tok.is_some() {
            self.pos += 1;
        }
        tok
    }

    /// 入口：parse = parse_or
    fn parse(&mut self) -> Option<Value> {
        let v = self.parse_or()?;
        // 解析完应到达 token 末尾；否则视为异常表达式
        if self.pos != self.tokens.len() {
            return None;
        }
        Some(v)
    }

    /// or 表达式：left or right or ...
    fn parse_or(&mut self) -> Option<Value> {
        let mut left = self.parse_and()?;
        while let Some(tok) = self.peek() {
            if tok.kind == TokKind::Keyword && tok.value == "or" {
                self.advance();
                let right = self.parse_and()?;
                let b = value_truthy(&left) || value_truthy(&right);
                left = Value::Bool(b);
            } else {
                break;
            }
        }
        Some(left)
    }

    /// and 表达式：left and right and ...
    fn parse_and(&mut self) -> Option<Value> {
        let mut left = self.parse_not()?;
        while let Some(tok) = self.peek() {
            if tok.kind == TokKind::Keyword && tok.value == "and" {
                self.advance();
                let right = self.parse_not()?;
                let b = value_truthy(&left) && value_truthy(&right);
                left = Value::Bool(b);
            } else {
                break;
            }
        }
        Some(left)
    }

    /// not 表达式：not <operand>
    fn parse_not(&mut self) -> Option<Value> {
        if let Some(tok) = self.peek() {
            if tok.kind == TokKind::Keyword && tok.value == "not" {
                self.advance();
                let operand = self.parse_not()?;
                return Some(Value::Bool(!value_truthy(&operand)));
            }
        }
        self.parse_comparison()
    }

    /// 比较表达式：primary OP primary。
    /// 注意：当 primary 后没有比较运算符时返回左侧值（与 0.1 一致），
    /// 不能因为 peek() 返回 None（到达 token 末尾）就视为解析失败。
    fn parse_comparison(&mut self) -> Option<Value> {
        let left = self.parse_primary()?;
        let tok = match self.peek() {
            Some(t) => t.clone(),
            None => return Some(left),
        };

        if tok.kind == TokKind::Op {
            let op = self.advance()?.value;
            let right = self.parse_primary()?;
            return Some(Value::Bool(compare(&left, &op, &right)));
        }

        // 非比较运算符：返回左侧值（用于上层做布尔判定）
        Some(left)
    }

    /// primary：字面量 / 列表 / 标识符（含点链访问）
    fn parse_primary(&mut self) -> Option<Value> {
        let tok = self.peek()?.clone();

        // 布尔字面量 true / false / none（tokenizer 已小写归一化）
        if tok.kind == TokKind::Bool {
            self.advance();
            return Some(match tok.value.as_str() {
                "true" => Value::Bool(true),
                "false" => Value::Bool(false),
                _ => Value::Null, // none
            });
        }

        // 数字字面量
        if tok.kind == TokKind::Number {
            self.advance();
            return Some(parse_number_literal(&tok.value));
        }

        // 字符串字面量
        if tok.kind == TokKind::String {
            self.advance();
            return Some(Value::String(tok.value));
        }

        // 列表字面量 [...]
        if tok.kind == TokKind::LBracket {
            return self.parse_list();
        }

        // 括号分组 ( expr )
        if tok.kind == TokKind::LParen {
            self.advance(); // 消耗 (
            let inner = self.parse_or()?;
            let close = self.advance()?;
            if close.kind != TokKind::RParen {
                return None;
            }
            return Some(inner);
        }

        // 标识符：从 state 取值，随后处理点链 / 下标
        if tok.kind == TokKind::Ident {
            self.advance();
            let mut value = resolve_name(self.state, &tok.value);

            while let Some(next) = self.peek() {
                match next.kind {
                    // 点号访问：value.property
                    TokKind::Dot => {
                        self.advance(); // 消耗 DOT
                        let dot_tok = self.advance()?;
                        if dot_tok.kind != TokKind::Ident {
                            // 点后必须跟标识符；链断掉，置 None 并停止
                            value = Value::Null;
                            break;
                        }
                        // 仅支持属性访问（不支持方法调用，与最小集一致）
                        value = get_field(&value, &dot_tok.value);
                    }
                    // 下标访问：value[key]
                    TokKind::LBracket => {
                        self.advance(); // 消耗 [
                        let key = self.parse_primary()?;
                        let close = self.advance()?;
                        if close.kind != TokKind::RBracket {
                            return None;
                        }
                        value = get_index(&value, &key);
                    }
                    _ => break,
                }
            }

            return Some(value);
        }

        None
    }

    /// 解析方括号列表字面量，如 [1, 2, 'a']
    fn parse_list(&mut self) -> Option<Value> {
        // 消耗 [
        let open = self.advance()?;
        if open.kind != TokKind::LBracket {
            return None;
        }

        let mut items = Vec::new();
        // 空列表
        if let Some(tok) = self.peek() {
            if tok.kind == TokKind::RBracket {
                self.advance();
                return Some(Value::Array(items));
            }
        }

        loop {
            let item = self.parse_primary()?;
            items.push(item);
            match self.peek()? {
                Token { kind: TokKind::Comma, .. } => {
                    self.advance();
                }
                Token { kind: TokKind::RBracket, .. } => {
                    self.advance();
                    return Some(Value::Array(items));
                }
                _ => return None,
            }
        }
    }
}

// ===========================================================================
// 求值辅助
// ===========================================================================

/// 把 serde_json::Value 折算为布尔（和 Python 的 bool() 对齐）。
/// - Bool → 自身
/// - Null → false
/// - 数字 → 非 0
/// - 字符串 → 非空
/// - 数组 → 非空
/// - 对象 → 非空
fn value_truthy(v: &Value) -> bool {
    match v {
        Value::Bool(b) => *b,
        Value::Null => false,
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                return i != 0;
            }
            n.as_f64().map(|f| f != 0.0).unwrap_or(false)
        }
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// 解析数字字面量字符串为 Value（整数优先，否则浮点）。
fn parse_number_literal(s: &str) -> Value {
    if let Ok(i) = s.parse::<i64>() {
        return Value::from(i);
    }
    if let Ok(f) = s.parse::<f64>() {
        return serde_json::Number::from_f64(f)
            .map(Value::Number)
            .unwrap_or(Value::Null);
    }
    Value::Null
}

/// 顶层标识符取值：从 state（serde_json::Value）读取 key。
/// 与 0.1 _resolve_name 对齐：name 存在则取值，不存在返回 Null。
fn resolve_name(state: &Value, name: &str) -> Value {
    if let Some(obj) = state.as_object() {
        if let Some(v) = obj.get(name) {
            return v.clone();
        }
    }
    // state 不是对象或 key 不存在 → None
    Value::Null
}

/// 点链访问：对 dict 取 key；非 dict 返回 Null。
fn get_field(value: &Value, key: &str) -> Value {
    if let Some(obj) = value.as_object() {
        return obj.get(key).cloned().unwrap_or(Value::Null);
    }
    Value::Null
}

/// 下标访问：value[key]。key 为字符串/数字时分别按对象 key / 数组下标取。
fn get_index(value: &Value, key: &Value) -> Value {
    match (value, key) {
        (Value::Object(obj), Value::String(k)) => obj.get(k).cloned().unwrap_or(Value::Null),
        (Value::Array(arr), Value::Number(idx)) => {
            if let Some(i) = idx.as_i64() {
                if i >= 0 && (i as usize) < arr.len() {
                    return arr[i as usize].clone();
                }
            }
            Value::Null
        }
        _ => Value::Null,
    }
}

/// 比较运算，语义对齐 Python（用 serde_json::Value 的等价比较 + 数值大小比较）。
fn compare(left: &Value, op: &str, right: &Value) -> bool {
    match op {
        "==" => json_eq(left, right),
        "!=" => !json_eq(left, right),
        ">" => json_cmp(left, right).map(|o| o.is_gt()).unwrap_or(false),
        "<" => json_cmp(left, right).map(|o| o.is_lt()).unwrap_or(false),
        ">=" => json_cmp(left, right).map(|o| !o.is_lt()).unwrap_or(false),
        "<=" => json_cmp(left, right).map(|o| !o.is_gt()).unwrap_or(false),
        _ => false,
    }
}

/// serde_json::Value 的相等判定。
/// 注意 JSON 中 true == 1 在 serde_json 里不相等，与 Python 一致（True == 1 为 True），
/// 但实际配置里 bool/number/字符串各司其职，这里直接用 PartialEq。
fn json_eq(a: &Value, b: &Value) -> bool {
    // 数字跨整数/浮点等价：serde_json 的 Number 已处理 1 == 1.0。
    a == b
}

/// 数值/字符串大小比较。返回 Option<Ordering>，类型不匹配返回 None。
fn json_cmp(a: &Value, b: &Value) -> Option<std::cmp::Ordering> {
    use std::cmp::Ordering;
    // 数字 vs 数字
    if let (Some(x), Some(y)) = (a.as_f64(), b.as_f64()) {
        return x.partial_cmp(&y).map(|o| match o {
            Ordering::Less => Ordering::Less,
            Ordering::Equal => Ordering::Equal,
            Ordering::Greater => Ordering::Greater,
        });
    }
    // 字符串 vs 字符串
    if let (Some(x), Some(y)) = (a.as_str(), b.as_str()) {
        return Some(x.cmp(y));
    }
    None
}

// ===========================================================================
// 单元测试
// ===========================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    // ---- 实际配置（default.yaml）用到的最小集 ----

    #[test]
    fn test_true() {
        assert!(eval_condition("True", &json!({})));
    }

    #[test]
    fn test_false() {
        assert!(!eval_condition("False", &json!({})));
    }

    #[test]
    fn test_eq_string() {
        let state = json!({ "core_type": "llm_call" });
        assert!(eval_condition("core_type == 'llm_call'", &state));
    }

    #[test]
    fn test_eq_string_mismatch() {
        let state = json!({ "core_type": "tool_execute" });
        assert!(!eval_condition("core_type == 'llm_call'", &state));
    }

    #[test]
    fn test_eq_string_tool_execute() {
        // default.yaml 另一个实际分支
        let state = json!({ "core_type": "tool_execute" });
        assert!(eval_condition("core_type == 'tool_execute'", &state));
    }

    #[test]
    fn test_ne_empty_list() {
        // raw_tool_calls != []：当为空列表时为 false
        let state = json!({ "raw_tool_calls": [] });
        assert!(!eval_condition("raw_tool_calls != []", &state));
    }

    #[test]
    fn test_ne_nonempty_list() {
        // raw_tool_calls != []：当非空时为 true
        let state = json!({ "raw_tool_calls": ["x"] });
        assert!(eval_condition("raw_tool_calls != []", &state));
    }

    #[test]
    fn test_dot_chain() {
        let state = json!({ "pause_guard": { "checked": { "paused": true } } });
        assert!(eval_condition("pause_guard.checked.paused == True", &state));
    }

    #[test]
    fn test_dot_chain_false() {
        let state = json!({ "pause_guard": { "checked": { "paused": false } } });
        assert!(!eval_condition("pause_guard.checked.paused == True", &state));
    }

    // ---- 扩展语法（0.1 已有，保留扩展性）----

    #[test]
    fn test_and() {
        // 两个都 true 才 true
        let state = json!({ "a": true, "b": true });
        assert!(eval_condition("a == true and b == true", &state));
        let state2 = json!({ "a": true, "b": false });
        assert!(!eval_condition("a == true and b == true", &state2));
    }

    #[test]
    fn test_or() {
        // 任一 true 即 true
        let state = json!({ "a": false, "b": true });
        assert!(eval_condition("a == true or b == true", &state));
        let state2 = json!({ "a": false, "b": false });
        assert!(!eval_condition("a == true or b == true", &state2));
    }

    #[test]
    fn test_not() {
        // not a：a 为 false 时为 true
        let state = json!({ "a": false });
        assert!(eval_condition("not a", &state));
        let state2 = json!({ "a": true });
        assert!(!eval_condition("not a", &state2));
    }

    #[test]
    fn test_missing_var() {
        // 变量不存在 → None == 'x' → false
        let state = json!({});
        assert!(!eval_condition("nonexistent == 'x'", &state));
    }

    #[test]
    fn test_invalid_expr() {
        // 解析异常 → false
        assert!(!eval_condition("!!!invalid", &json!({})));
    }

    // ---- 额外覆盖 ----

    #[test]
    fn test_lowercase_bool() {
        // text_only == true（小写）也要工作
        let state = json!({ "text_only": true, "no_new_input": true });
        assert!(eval_condition("text_only == true and no_new_input == true", &state));
    }

    #[test]
    fn test_empty_condition_is_true() {
        // 与 0.1 一致：空表达式 → true
        assert!(eval_condition("", &json!({})));
        assert!(eval_condition("   ", &json!({})));
    }

    #[test]
    fn test_double_quoted_string() {
        let state = json!({ "core_type": "llm_call" });
        assert!(eval_condition("core_type == \"llm_call\"", &state));
    }

    #[test]
    fn test_numeric_comparison() {
        let state = json!({ "n": 5 });
        assert!(eval_condition("n > 3", &state));
        assert!(!eval_condition("n > 10", &state));
        assert!(eval_condition("n >= 5", &state));
        assert!(eval_condition("n <= 5", &state));
        assert!(eval_condition("n < 6", &state));
    }

    #[test]
    fn test_not_equal_string() {
        let state = json!({ "core_type": "tool_execute" });
        assert!(eval_condition("core_type != 'llm_call'", &state));
    }

    #[test]
    fn test_none_literal() {
        // missing → None；None == None → true
        let state = json!({});
        assert!(eval_condition("missing == None", &state));
    }

    #[test]
    fn test_paren_grouping() {
        // (a or b) and c 的优先级
        let state = json!({ "a": true, "b": false, "c": true });
        assert!(eval_condition("(a == true or b == true) and c == true", &state));
    }

    #[test]
    fn test_partial_dot_chain_missing() {
        // 中间节点缺失 → None == True → false
        let state = json!({ "pause_guard": { } });
        assert!(!eval_condition("pause_guard.checked.paused == True", &state));
    }

    #[test]
    fn test_list_equality() {
        let state = json!({ "xs": [1, 2, 3] });
        assert!(eval_condition("xs == [1, 2, 3]", &state));
        assert!(!eval_condition("xs == [1, 2]", &state));
    }
}

//! 安全条件表达式解析器（插件侧 triggers_ext/triggers/condition_parser.py 为其 Python 回移实现）。
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
//! 扩展语法：
//!     or / not
//!     == / != / > / < / >= / <=
//!     算术 + - * /（数字运算，* / 高于 + -，括号分组；任一操作数非数字或
//!     除零 → Null + warn 留痕，fail-soft 条件判假不炸管道）
//!     内建函数调用 name(args...)——标识符后紧跟 '(' 即函数形态，否则维持
//!     路径访问语义。内建表仅通用语言设施（len），零领域知识（一切皆插件：
//!     领域估算公式写在管道 when 表达式里，引擎只负责求值）；未知函数名
//!     求值 = Null + warn（fail-soft）
//!     点链访问 a.b.c（嵌套 dict.get）
//!     字面量：字符串（单/双引号）、数字、True/False/None、列表 [...]
//!
//! 优先级（低到高）：or < and < not < comparison < + - < * / < primary
//!
//! ## 两段式（G10 加载期编译）
//!
//! - [`parse_condition`]：把表达式字符串 tokenize + parse 成 [`Expr`] AST，
//!   加载期一次性完成（管道编译时）；语法错误在加载期暴露，不静默。
//! - [`eval_expr`]：对已编译 AST 求值，运行时零解析。

use serde_json::Value;

/// 编译后的条件表达式 AST（G10：加载期 parse 一次，运行时只求值）。
///
/// 无副作用、无动态求值：求值仅读取 `state`。
#[derive(Debug, Clone, PartialEq)]
pub enum Expr {
    /// 字面量（bool / number / string / null）。
    Literal(Value),
    /// 列表字面量（元素可以是任意表达式，求值时逐项折算）。
    List(Vec<Expr>),
    /// state 路径访问（root + 点链/下标步）。
    Path { root: String, steps: Vec<PathStep> },
    /// 逻辑非。
    Not(Box<Expr>),
    /// 逻辑与（短路）。
    And(Box<Expr>, Box<Expr>),
    /// 逻辑或（短路）。
    Or(Box<Expr>, Box<Expr>),
    /// 比较（== != > < >= <=）。
    Compare {
        op: &'static str,
        left: Box<Expr>,
        right: Box<Expr>,
    },
    /// 算术（+ - * /，左结合；* / 优先级高于 + -）。数字运算：任一操作数
    /// 求值结果非数字或除零 → Null + warn（fail-soft，见 [`arith`]）。
    Arith {
        op: &'static str,
        left: Box<Expr>,
        right: Box<Expr>,
    },
    /// 内建函数调用（标识符后紧跟 '('）。分派见 [`call_native`]：内建表仅
    /// 通用语言设施（len），未知函数名 → Null + warn（fail-soft）。
    Fn { name: String, args: Vec<Expr> },
}

/// 路径访问的一步：点字段或下标。
#[derive(Debug, Clone, PartialEq)]
pub enum PathStep {
    /// `a.b`
    Field(String),
    /// `a[key]`（key 是表达式，求值后按 [`get_index`] 语义取）。
    Index(Box<Expr>),
}

/// 解析条件表达式为 AST。
///
/// - `Ok(None)`：空串 / 纯空白——恒真（无条件）。
/// - `Ok(Some(expr))`：可求值的表达式。
/// - `Err(msg)`：语法错误（带位置提示）——调用方应在加载期暴露，勿静默吞掉。
pub fn parse_condition(condition: &str) -> Result<Option<Expr>, String> {
    let expr = condition.trim();
    if expr.is_empty() {
        return Ok(None);
    }
    let tokens = tokenize(expr)?;
    if tokens.is_empty() {
        return Ok(None);
    }
    let mut parser = Parser::new(tokens);
    let ast = parser.parse()?;
    Ok(Some(ast))
}

/// 求值已编译表达式：折算为布尔（与 Python bool() 对齐）。
///
/// 运行时零解析——表达式结构在编译期已定型。恒真（`None`）由调用方短路，
/// 本函数只接收 `Some`。
pub fn eval_expr(expr: &Expr, state: &Value) -> bool {
    value_truthy(&eval_value(expr, state))
}

/// 求值表达式为原始值（内部实现，比较/路径/列表求值用）。
fn eval_value(expr: &Expr, state: &Value) -> Value {
    match expr {
        Expr::Literal(v) => v.clone(),
        Expr::List(items) => Value::Array(items.iter().map(|e| eval_value(e, state)).collect()),
        Expr::Path { root, steps } => {
            // 平键优先（含前缀平键）：本仓 state 惯例是平铺点键（插件 state_updates
            // 经 set_key 平插 "task.status"/"router.duplicate_back_llm"，不拆
            // 点）——全 Field 链先按逐段前缀拼平键查：任一前缀命中即以**剩余
            // steps 嵌套下钻**（生产形态：track.llm_usage 是平键持 dict，
            // last_input_tokens 是其内层字段——整链平铺或前段平铺都要命中）。
            // 未命中再走点链嵌套解析（嵌套写入方兼容不变）。含 Index 下标的链
            // 无平键形态，直接走嵌套解析。
            let all_fields = steps.iter().all(|s| matches!(s, PathStep::Field(_)));
            if all_fields && !steps.is_empty() {
                if let Some(obj) = state.as_object() {
                    let mut prefix = root.clone();
                    for (i, s) in steps.iter().enumerate() {
                        let PathStep::Field(f) = s else {
                            unreachable!("all_fields 已排除 Index");
                        };
                        prefix.push('.');
                        prefix.push_str(f);
                        if let Some(v) = obj.get(&prefix) {
                            // 前缀命中（最短优先，平键优先于嵌套）：剩余 steps 下钻
                            let mut v = v.clone();
                            for step in &steps[i + 1..] {
                                v = match step {
                                    PathStep::Field(f) => get_field(&v, f),
                                    PathStep::Index(key) => {
                                        let k = eval_value(key, state);
                                        get_index(&v, &k)
                                    }
                                };
                            }
                            return v;
                        }
                    }
                }
            }
            let mut v = resolve_name(state, root);
            for step in steps {
                v = match step {
                    PathStep::Field(f) => get_field(&v, f),
                    PathStep::Index(key) => {
                        let k = eval_value(key, state);
                        get_index(&v, &k)
                    }
                };
            }
            v
        }
        Expr::Not(inner) => Value::Bool(!value_truthy(&eval_value(inner, state))),
        Expr::And(l, r) => {
            // 短路：左侧为假直接出 false（右侧不求值）
            let lv = eval_value(l, state);
            if !value_truthy(&lv) {
                Value::Bool(false)
            } else {
                Value::Bool(value_truthy(&eval_value(r, state)))
            }
        }
        Expr::Or(l, r) => {
            let lv = eval_value(l, state);
            if value_truthy(&lv) {
                Value::Bool(true)
            } else {
                Value::Bool(value_truthy(&eval_value(r, state)))
            }
        }
        Expr::Compare { op, left, right } => {
            let l = eval_value(left, state);
            let r = eval_value(right, state);
            Value::Bool(compare(&l, op, &r))
        }
        Expr::Arith { op, left, right } => {
            let l = eval_value(left, state);
            let r = eval_value(right, state);
            arith(op, &l, &r)
        }
        Expr::Fn { name, args } => {
            let arg_values: Vec<Value> = args.iter().map(|a| eval_value(a, state)).collect();
            call_native(name, &arg_values)
        }
    }
}

// ===========================================================================
// Token
// ===========================================================================

#[derive(Debug, Clone, PartialEq)]
enum TokKind {
    String,   // "..." 或 '...'
    Number,   // 123 / 1.5
    Bool,     // True / False / None
    Keyword,  // and / or / not
    Op,       // 比较运算符 == / != / > / < / >= / <= 与算术 + - * /
    Dot,      // .
    Ident,    // 标识符
    LBracket, // [
    RBracket, // ]
    LParen,   // (
    RParen,   // )
    Comma,    // ,
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
    while i < chars.len() {
        let c = chars[i];
        // 按 token 类别分派到扫描器（返回 token 与消耗后的下一游标）
        if c.is_whitespace() {
            i += 1;
        } else if c == '\'' || c == '"' {
            let (tok, next) = scan_string(&chars, i)?;
            tokens.push(tok);
            i = next;
        } else if c.is_ascii_digit() {
            let (tok, next) = scan_number(&chars, i);
            tokens.push(tok);
            i = next;
        } else if c.is_ascii_alphabetic() || c == '_' {
            let (tok, next) = scan_word(&chars, i);
            tokens.push(tok);
            i = next;
        } else if matches!(c, '=' | '!' | '<' | '>' | '+' | '-' | '*' | '/') {
            let (tok, next) = scan_operator(&chars, i)?;
            tokens.push(tok);
            i = next;
        } else {
            let (tok, next) = scan_single(&chars, i)?;
            tokens.push(tok);
            i = next;
        }
    }
    Ok(tokens)
}

/// 字符串字面量（单/双引号）。`i` 停在起始引号上，返回 token 与结束引号后的游标。
fn scan_string(chars: &[char], i: usize) -> Result<(Token, usize), String> {
    let n = chars.len();
    let quote = chars[i];
    let start = i + 1;
    let mut j = i + 1;
    while j < n && chars[j] != quote {
        j += 1;
    }
    if j >= n {
        return Err(format!("Unterminated string literal at position {}", start));
    }
    // chars[start..j] 即引号内内容
    let body: String = chars[start..j].iter().collect();
    Ok((
        Token {
            kind: TokKind::String,
            value: body,
        },
        j + 1, // 消耗结束引号
    ))
}

/// 数字字面量（含可选小数）；负号不特殊处理，交给上下文：只解析无符号数字，
/// 避免 "a - 1" 中的 "-1" 被误吞为数字。返回 token 与数字后的游标。
fn scan_number(chars: &[char], i: usize) -> (Token, usize) {
    let n = chars.len();
    let start = i;
    let mut j = i;
    while j < n && chars[j].is_ascii_digit() {
        j += 1;
    }
    if j < n && chars[j] == '.' {
        j += 1;
        while j < n && chars[j].is_ascii_digit() {
            j += 1;
        }
    }
    let body: String = chars[start..j].iter().collect();
    (
        Token {
            kind: TokKind::Number,
            value: body,
        },
        j,
    )
}

/// 标识符 / 关键字 / 布尔字面量。返回 token 与单词后的游标。
fn scan_word(chars: &[char], i: usize) -> (Token, usize) {
    let n = chars.len();
    let start = i;
    let mut j = i;
    while j < n && (chars[j].is_ascii_alphanumeric() || chars[j] == '_') {
        j += 1;
    }
    let word: String = chars[start..j].iter().collect();
    // 大小写不敏感地识别布尔/None 字面量与逻辑关键字（对齐 JSON 的 true/false
    // 与 Python 风格 True/False/None；default.yaml 实际出现小写 true）。
    let tok = match word.to_lowercase().as_str() {
        "true" | "false" | "none" => Token {
            kind: TokKind::Bool,
            value: word.to_lowercase(),
        },
        "and" | "or" | "not" => Token {
            kind: TokKind::Keyword,
            value: word.to_lowercase(),
        },
        _ => Token {
            kind: TokKind::Ident,
            value: word,
        },
    };
    (tok, j)
}

/// 运算符（比较 ==/!=/>/</>=/<= + 算术 + - * /；多字符优先，两字符形式
/// 仅比较运算符有）。`i` 停在运算符首字符，返回 token 与下一游标。
fn scan_operator(chars: &[char], i: usize) -> Result<(Token, usize), String> {
    let n = chars.len();
    let c = chars[i];
    let may_be_two_char = matches!(c, '=' | '!' | '<' | '>');
    if may_be_two_char && i + 1 < n && chars[i + 1] == '=' {
        let op: String = format!("{}=", c);
        return Ok((
            Token {
                kind: TokKind::Op,
                value: op,
            },
            i + 2,
        ));
    }
    if c == '=' {
        return Err(format!("Unexpected '=' at position {}", i));
    }
    // <, >, +, -, *, / 单字符
    Ok((
        Token {
            kind: TokKind::Op,
            value: c.to_string(),
        },
        i + 1,
    ))
}

/// 单字符 token（`.` `[` `]` `(` `)` `,`）。返回 token 与下一游标。
fn scan_single(chars: &[char], i: usize) -> Result<(Token, usize), String> {
    let c = chars[i];
    let kind = match c {
        '.' => TokKind::Dot,
        '[' => TokKind::LBracket,
        ']' => TokKind::RBracket,
        '(' => TokKind::LParen,
        ')' => TokKind::RParen,
        ',' => TokKind::Comma,
        _ => return Err(format!("Unexpected character '{}' at position {}", c, i)),
    };
    Ok((
        Token {
            kind,
            value: c.to_string(),
        },
        i + 1,
    ))
}

// ===========================================================================
// Parser — 递归下降，产出 AST（不碰 state）
// ===========================================================================

struct Parser {
    tokens: Vec<Token>,
    pos: usize,
}

impl Parser {
    fn new(tokens: Vec<Token>) -> Self {
        Self { tokens, pos: 0 }
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

    /// 入口：parse = parse_or；解析完必须到达 token 末尾，否则语法错误。
    fn parse(&mut self) -> Result<Expr, String> {
        let ast = self.parse_or()?;
        if self.pos != self.tokens.len() {
            return Err(format!(
                "position {}: 多余的 token '{}'",
                self.pos, self.tokens[self.pos].value
            ));
        }
        Ok(ast)
    }

    /// or 表达式：left or right or ...
    fn parse_or(&mut self) -> Result<Expr, String> {
        let mut left = self.parse_and()?;
        while let Some(tok) = self.peek() {
            if tok.kind == TokKind::Keyword && tok.value == "or" {
                self.advance();
                let right = self.parse_and()?;
                left = Expr::Or(Box::new(left), Box::new(right));
            } else {
                break;
            }
        }
        Ok(left)
    }

    /// and 表达式：left and right and ...
    fn parse_and(&mut self) -> Result<Expr, String> {
        let mut left = self.parse_not()?;
        while let Some(tok) = self.peek() {
            if tok.kind == TokKind::Keyword && tok.value == "and" {
                self.advance();
                let right = self.parse_not()?;
                left = Expr::And(Box::new(left), Box::new(right));
            } else {
                break;
            }
        }
        Ok(left)
    }

    /// not 表达式：not <operand>
    fn parse_not(&mut self) -> Result<Expr, String> {
        if let Some(tok) = self.peek() {
            if tok.kind == TokKind::Keyword && tok.value == "not" {
                self.advance();
                let operand = self.parse_not()?;
                return Ok(Expr::Not(Box::new(operand)));
            }
        }
        self.parse_comparison()
    }

    /// 比较表达式：additive OP additive（算术绑定比比较紧，
    /// `a + b > c` 即 `(a + b) > c`）。
    /// 注意：当左侧后没有比较运算符时返回左侧表达式（上层用布尔折算判定），
    /// 不能因为 peek() 返回 None 就视为解析失败。
    fn parse_comparison(&mut self) -> Result<Expr, String> {
        let left = self.parse_additive()?;
        let tok = match self.peek() {
            Some(t) => t.clone(),
            None => return Ok(left),
        };

        if tok.kind == TokKind::Op {
            let op = self
                .advance()
                .ok_or_else(|| format!("position {}: 比较运算符后表达式意外结束", self.pos))?
                .value;
            let right = self.parse_additive()?;
            let op: &'static str = match op.as_str() {
                "==" => "==",
                "!=" => "!=",
                ">" => ">",
                "<" => "<",
                ">=" => ">=",
                "<=" => "<=",
                other => return Err(format!("position {}: 未知比较运算符 '{}'", self.pos, other)),
            };
            return Ok(Expr::Compare {
                op,
                left: Box::new(left),
                right: Box::new(right),
            });
        }

        // 非比较运算符：返回左侧表达式（用于上层做布尔判定）
        Ok(left)
    }

    /// 加减表达式（左结合）：a + b - c ...
    fn parse_additive(&mut self) -> Result<Expr, String> {
        self.parse_arith_level(classify_additive, Self::parse_multiplicative)
    }

    /// 乘除表达式（左结合）：a * b / c ...
    fn parse_multiplicative(&mut self) -> Result<Expr, String> {
        self.parse_arith_level(classify_multiplicative, Self::parse_primary)
    }

    /// 算术二元层（+ - 与 * / 两层共用骨架）：classify 把 token 值归入本层
    /// 运算符（None = 不属于本层，交回上层循环），operand 为本层操作数解析。
    fn parse_arith_level(
        &mut self,
        classify: fn(&str) -> Option<&'static str>,
        operand: fn(&mut Self) -> Result<Expr, String>,
    ) -> Result<Expr, String> {
        let mut left = operand(self)?;
        while let Some(Token {
            kind: TokKind::Op,
            value,
        }) = self.peek()
        {
            let op = match classify(value) {
                Some(op) => op,
                None => break,
            };
            self.advance();
            let right = operand(self)?;
            left = Expr::Arith {
                op,
                left: Box::new(left),
                right: Box::new(right),
            };
        }
        Ok(left)
    }

    /// primary：字面量 / 列表 / 标识符（含点链访问）。
    fn parse_primary(&mut self) -> Result<Expr, String> {
        let tok = self
            .peek()
            .cloned()
            .ok_or_else(|| format!("position {}: 表达式意外结束", self.pos))?;

        // 布尔字面量 true / false / none（tokenizer 已小写归一化）
        if tok.kind == TokKind::Bool {
            self.advance();
            return Ok(Expr::Literal(match tok.value.as_str() {
                "true" => Value::Bool(true),
                "false" => Value::Bool(false),
                _ => Value::Null, // none
            }));
        }

        // 数字字面量
        if tok.kind == TokKind::Number {
            self.advance();
            return Ok(Expr::Literal(parse_number_literal(&tok.value)));
        }

        // 字符串字面量
        if tok.kind == TokKind::String {
            self.advance();
            return Ok(Expr::Literal(Value::String(tok.value)));
        }

        // 列表字面量 [...]
        if tok.kind == TokKind::LBracket {
            return self.parse_list();
        }

        // 括号分组 ( expr )
        if tok.kind == TokKind::LParen {
            self.advance(); // 消耗 (
            let inner = self.parse_or()?;
            let close = self
                .advance()
                .ok_or_else(|| format!("position {}: 括号未闭合", self.pos))?;
            if close.kind != TokKind::RParen {
                return Err(format!("position {}: 期望 ')'", self.pos));
            }
            return Ok(inner);
        }

        // 标识符：state 路径（root + 点链 / 下标）或函数调用
        if tok.kind == TokKind::Ident {
            self.advance();
            let root = tok.value;

            // 函数调用形态：标识符后紧跟 '('。tokenizer 产 Ident 与 LParen
            // 两个独立 token（括号分组/路径访问语义不变），在此合并识别。
            if matches!(
                self.peek(),
                Some(Token {
                    kind: TokKind::LParen,
                    ..
                })
            ) {
                let args = self.parse_call_args()?;
                return Ok(Expr::Fn { name: root, args });
            }

            let mut steps = Vec::new();

            while let Some(next) = self.peek() {
                match next.kind {
                    // 点号访问：value.property
                    TokKind::Dot => {
                        self.advance(); // 消耗 DOT
                        let dot_tok = self.advance().ok_or_else(|| {
                            format!("position {}: '.' 后表达式意外结束", self.pos)
                        })?;
                        if dot_tok.kind != TokKind::Ident {
                            return Err(format!(
                                "position {}: '.' 后应为字段名（不支持方法调用）",
                                self.pos
                            ));
                        }
                        // 仅支持属性访问（不支持方法调用，与最小集一致）
                        steps.push(PathStep::Field(dot_tok.value));
                    }
                    // 下标访问：value[key]
                    TokKind::LBracket => {
                        self.advance(); // 消耗 [
                        let key = self.parse_primary()?;
                        let close = self
                            .advance()
                            .ok_or_else(|| format!("position {}: '[' 未闭合", self.pos))?;
                        if close.kind != TokKind::RBracket {
                            return Err(format!("position {}: 期望 ']'", self.pos));
                        }
                        steps.push(PathStep::Index(Box::new(key)));
                    }
                    _ => break,
                }
            }

            return Ok(Expr::Path { root, steps });
        }

        Err(format!(
            "position {}: 无法识别的 token '{}'",
            self.pos, tok.value
        ))
    }

    /// 解析方括号列表字面量，如 [1, 2, 'a']
    fn parse_list(&mut self) -> Result<Expr, String> {
        // 消耗 [
        let open = self
            .advance()
            .ok_or_else(|| format!("position {}: 表达式意外结束", self.pos))?;
        if open.kind != TokKind::LBracket {
            return Err(format!("position {}: 期望 '['", self.pos));
        }

        let mut items = Vec::new();
        // 空列表
        if let Some(tok) = self.peek() {
            if tok.kind == TokKind::RBracket {
                self.advance();
                return Ok(Expr::List(items));
            }
        }

        loop {
            let item = self.parse_primary()?;
            items.push(item);
            match self.peek() {
                Some(Token {
                    kind: TokKind::Comma,
                    ..
                }) => {
                    self.advance();
                }
                Some(Token {
                    kind: TokKind::RBracket,
                    ..
                }) => {
                    self.advance();
                    return Ok(Expr::List(items));
                }
                Some(Token { value, .. }) => {
                    return Err(format!(
                        "position {}: 列表内意外的 token '{value}'（期望 ',' 或 ']'）",
                        self.pos
                    ))
                }
                None => return Err(format!("position {}: 列表未闭合", self.pos)),
            }
        }
    }

    /// 解析函数调用参数表（游标停在 '(' 上）：空参 '()' 或逗号分隔的
    /// 表达式表，以 ')' 闭合。参数走 parse_or（与括号分组同级，任意表达式）。
    fn parse_call_args(&mut self) -> Result<Vec<Expr>, String> {
        self.advance(); // 消耗 (
        let mut args = Vec::new();
        if matches!(
            self.peek(),
            Some(Token {
                kind: TokKind::RParen,
                ..
            })
        ) {
            self.advance();
            return Ok(args);
        }
        loop {
            args.push(self.parse_or()?);
            match self.peek() {
                Some(Token {
                    kind: TokKind::Comma,
                    ..
                }) => {
                    self.advance();
                }
                Some(Token {
                    kind: TokKind::RParen,
                    ..
                }) => {
                    self.advance();
                    return Ok(args);
                }
                Some(Token { value, .. }) => {
                    return Err(format!(
                        "position {}: 参数表内意外的 token '{value}'（期望 ',' 或 ')'）",
                        self.pos
                    ))
                }
                None => return Err(format!("position {}: 参数表未闭合", self.pos)),
            }
        }
    }
}

/// 加减层运算符归类。
fn classify_additive(s: &str) -> Option<&'static str> {
    match s {
        "+" => Some("+"),
        "-" => Some("-"),
        _ => None,
    }
}

/// 乘除层运算符归类。
fn classify_multiplicative(s: &str) -> Option<&'static str> {
    match s {
        "*" => Some("*"),
        "/" => Some("/"),
        _ => None,
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
/// name 存在则取值，不存在返回 Null。
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

/// 算术运算：数字 × 数字 → 数字（整数值落 i64，与整型字面量 == 相等可比）。
/// 任一操作数非数字（Null/bool/字符串等）或除零 → Null + warn 一次
/// （fail-soft：条件判假，不炸管道）。
fn arith(op: &str, l: &Value, r: &Value) -> Value {
    let (x, y) = match (l.as_f64(), r.as_f64()) {
        (Some(x), Some(y)) => (x, y),
        _ => {
            tracing::warn!(op, ?l, ?r, "条件 DSL 算术操作数非数字，fail-soft 返回 Null");
            return Value::Null;
        }
    };
    let result = match op {
        "+" => x + y,
        "-" => x - y,
        "*" => x * y,
        "/" if y == 0.0 => {
            tracing::warn!(op, ?l, ?r, "条件 DSL 除零，fail-soft 返回 Null");
            return Value::Null;
        }
        "/" => x / y,
        // 不可达：parser 只经 classify_* 产四则 op
        _ => return Value::Null,
    };
    number_value(result)
}

/// f64 → JSON 数值：整数值落 i64，其余落浮点；非有限（NaN/∞，四则溢出）
/// → Null（fail-soft）。
fn number_value(f: f64) -> Value {
    if f.is_finite() && f.fract() == 0.0 && f.abs() <= i64::MAX as f64 {
        return Value::from(f as i64);
    }
    serde_json::Number::from_f64(f)
        .map(Value::Number)
        .unwrap_or(Value::Null)
}

// ===========================================================================
// 内建函数（Expr::Fn 分派）——仅通用语言设施，零领域知识（一切皆插件公理）
// ===========================================================================

/// 内建函数分派：len(x) → 数组元素数 / 字符串字符数。参数个数不符或
/// 未知函数名 → Null + warn 一次（fail-soft：条件判假，不炸管道）。
fn call_native(name: &str, args: &[Value]) -> Value {
    match name {
        "len" => match args {
            [Value::Array(a)] => Value::from(a.len()),
            [Value::String(s)] => Value::from(s.chars().count()),
            _ => {
                tracing::warn!(
                    function = name,
                    "条件 DSL len() 参数个数不符或非数组/字符串，fail-soft 返回 Null"
                );
                Value::Null
            }
        },
        other => {
            tracing::warn!(function = other, "条件 DSL 未知函数，fail-soft 返回 Null");
            Value::Null
        }
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

    /// 测试辅助：字符串 → 求值（走生产路径 parse_condition + eval_expr）。
    fn eval_condition(condition: &str, state: &Value) -> bool {
        match parse_condition(condition) {
            Ok(None) => true,
            Ok(Some(expr)) => eval_expr(&expr, state),
            Err(_) => false,
        }
    }

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
    fn test_flat_dotted_key() {
        // 平键点链：本仓 state 惯例是平铺点键（插件 state_updates 经 set_key
        // 平插 "task.status"/"router.duplicate_back_llm"，不拆点）——路由条件
        // 对平键形态必须命中，否则 post 路由表的路由恒假静默落入兜底 end。
        let state = json!({ "task.status": "completed" });
        assert!(eval_condition("task.status == 'completed'", &state));
        assert!(!eval_condition("task.status == 'failed'", &state));
        let flagged = json!({ "router.duplicate_back_llm": true });
        assert!(eval_condition(
            "router.duplicate_back_llm == True",
            &flagged
        ));
        // 平键未命中时回退点链嵌套解析（嵌套写入方兼容不变）
        let nested = json!({ "task": { "status": "completed" } });
        assert!(eval_condition("task.status == 'completed'", &nested));
        // 平键优先于嵌套：两者同场时平键获胜
        let both = json!({
            "task.status": "completed",
            "task": { "status": "failed" }
        });
        assert!(eval_condition("task.status == 'completed'", &both));
    }

    #[test]
    fn test_flat_prefix_key_then_nested_descend() {
        // 前缀平键（生产形态：track.llm_usage 是平键持 dict，last_input_tokens
        // 是其内层字段）——整链平铺查不到时逐段前缀命中并以剩余 steps 下钻。
        // 无此回退时该路径解析为 Null，压缩粗门公式恒假（W2a 实测踩坑）。
        let state = json!({ "track.llm_usage": { "last_input_tokens": 78000 } });
        assert!(eval_condition(
            "track.llm_usage.last_input_tokens == 78000",
            &state
        ));
        assert!(eval_condition(
            "track.llm_usage.last_input_tokens > 77999",
            &state
        ));
        assert!(!eval_condition(
            "track.llm_usage.last_input_tokens == none",
            &state
        ));
        // 内层字段缺失 → Null（== none 为真，冷启动兜底支依赖此语义）
        let no_field = json!({ "track.llm_usage": {} });
        assert!(eval_condition(
            "track.llm_usage.last_input_tokens == none",
            &no_field
        ));
        // 完全没有 track 键 → Null
        assert!(eval_condition(
            "track.llm_usage.last_input_tokens == none",
            &json!({})
        ));
        // 前缀平键优先于嵌套：两形态同场，平键（最短前缀）获胜
        let both = json!({
            "track.llm_usage": { "last_input_tokens": 1 },
            "track": { "llm_usage": { "last_input_tokens": 2 } }
        });
        assert!(eval_condition(
            "track.llm_usage.last_input_tokens == 1",
            &both
        ));
    }

    #[test]
    fn test_dot_chain_false() {
        let state = json!({ "pause_guard": { "checked": { "paused": false } } });
        assert!(!eval_condition(
            "pause_guard.checked.paused == True",
            &state
        ));
    }

    // ---- 扩展语法 ----

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
        assert!(eval_condition(
            "text_only == true and no_new_input == true",
            &state
        ));
    }

    #[test]
    fn test_empty_condition_is_true() {
        // 空表达式 → true
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
        assert!(eval_condition(
            "(a == true or b == true) and c == true",
            &state
        ));
    }

    #[test]
    fn test_partial_dot_chain_missing() {
        // 中间节点缺失 → None == True → false
        let state = json!({ "pause_guard": { } });
        assert!(!eval_condition(
            "pause_guard.checked.paused == True",
            &state
        ));
    }

    #[test]
    fn test_list_equality() {
        let state = json!({ "xs": [1, 2, 3] });
        assert!(eval_condition("xs == [1, 2, 3]", &state));
        assert!(!eval_condition("xs == [1, 2]", &state));
    }

    // ---- 算术（+ - * /）----

    #[test]
    fn test_arith_precedence_and_parens() {
        // * / 高于 + -；括号分组改序；左结合；整数值与整型字面量 == 可比
        assert!(eval_condition("2 + 3 * 4 == 14", &json!({})));
        assert!(eval_condition("(2 + 3) * 4 == 20", &json!({})));
        assert!(eval_condition("10 - 2 - 3 == 5", &json!({})));
        assert!(eval_condition("8 / 4 == 2", &json!({})));
        assert!(eval_condition("7 / 2 == 3.5", &json!({})));
        // 区分度反向输入：优先级若错（(2+3)*4 语义）会得 20；右结合会得 11
        assert!(!eval_condition("2 + 3 * 4 == 20", &json!({})));
        assert!(!eval_condition("10 - 2 - 3 == 11", &json!({})));
    }

    #[test]
    fn test_arith_minus_is_binary_operator() {
        // '-' 是二元运算符不是数字符号：a - 1 不把 -1 吞成字面量
        assert!(eval_condition("a - 1 == 4", &json!({ "a": 5 })));
        assert!(eval_condition("a-1 == 4", &json!({ "a": 5 })));
    }

    #[test]
    fn test_arith_paths_in_formula() {
        // 路径（嵌套/平键两形态）参与运算：track.a + track.b * 2 > x，真假两侧
        let nested = json!({ "track": { "a": 1000, "b": 400 }, "x": 1799 });
        assert!(eval_condition("track.a + track.b * 2 > x", &nested)); // 1800 > 1799
        let flat = json!({ "track.a": 1000, "track.b": 400, "x": 1801 });
        assert!(!eval_condition("track.a + track.b * 2 > x", &flat)); // 1800 > 1801 假
    }

    #[test]
    fn test_arith_context_gate_formula() {
        // 目标用法（压缩粗门）：估算公式整体写在管道 when 里，引擎只负责求值
        let cond = "track.llm_usage.last_input_tokens + (track.messages_chars - track.messages_chars_at_llm) * 2.0 > 150000";
        let below = json!({
            "track.llm_usage.last_input_tokens": 100000,
            "track.messages_chars": 12000,
            "track.messages_chars_at_llm": 8000,
        }); // 100000 + 4000×2 = 108000
        assert!(!eval_condition(cond, &below));
        let above = json!({
            "track.llm_usage.last_input_tokens": 100000,
            "track.messages_chars": 60000,
            "track.messages_chars_at_llm": 8000,
        }); // 100000 + 52000×2 = 204000
        assert!(eval_condition(cond, &above));
    }

    #[test]
    fn test_context_gate_percentage_formula_production() {
        // W2a 生产公式（autonomous.yaml pipeline_context_window_guard 项级 when，
        // 百分比阈值改道后形态）：占用比 =（上次 input_tokens + 字符增量×2.0）
        // ÷ 模型窗口 > 0.6，or 冷启动兜底（无 token 锚且消息 > 40）。
        // state 用**生产形态**（track.llm_usage 平键持 dict——前缀平键解析）。
        let cond = "(track.llm_usage.last_input_tokens + (track.messages_chars - track.messages_chars_at_llm) * 2.0) / track.model_context_window > 0.6 or (track.llm_usage.last_input_tokens == none and len(messages) > 40)";
        let base = json!({
            "track.llm_usage": {"last_input_tokens": 78000},
            "track.messages_chars": 59000,
            "track.messages_chars_at_llm": 8000,
            "track.model_context_window": 200000,
            "messages": [ {"role": "user", "content": "hi"} ],
        }); // (78000+102000)/200000 = 0.9 > 0.6 → 真
        assert!(eval_condition(cond, &base));
        // 单调性质：窗口放大（其余不变）→ 占用比下降，跨过 0.6 翻假
        let mut wide = base.clone();
        wide["track.model_context_window"] = json!(300000);
        assert!(!eval_condition(cond, &wide)); // 180000/300000 = 0.6 不大于 0.6
                                               // 字符增量趋零 → 回落到纯 last_input 占用比
        let fresh = json!({
            "track.llm_usage": {"last_input_tokens": 60000},
            "track.messages_chars": 9000,
            "track.messages_chars_at_llm": 9000,
            "track.model_context_window": 200000,
            "messages": [ {"role": "user", "content": "hi"} ],
        });
        assert!(!eval_condition(cond, &fresh)); // 0.3 ≤ 0.6
                                                // 窗口 0（llm_core 未解析到 context_window 时写 0）→ 除零 fail-soft
                                                // 判假——公式支安全关闭，只剩冷启动兜底支
        let zero_window = json!({
            "track.llm_usage": {"last_input_tokens": 78000},
            "track.messages_chars": 59000,
            "track.messages_chars_at_llm": 8000,
            "track.model_context_window": 0,
            "messages": [ {"role": "user", "content": "hi"} ],
        });
        assert!(!eval_condition(cond, &zero_window));
        // 冷启动兜底：无 token 锚 + 41 条消息 → 真（放行 guard 精确判定）
        let cold_msgs: Vec<serde_json::Value> = (0..41)
            .map(|i| json!({"seq": i, "role": "user", "content": format!("m{i}")}))
            .collect();
        let cold = json!({"messages": cold_msgs});
        assert!(eval_condition(cond, &cold));
        // 区分度反向：无锚但消息少 → 两支均假
        let cold_small = json!({"messages": [ {"role": "user", "content": "hi"} ]});
        assert!(!eval_condition(cond, &cold_small));
    }

    #[test]
    fn test_arith_non_numeric_fail_soft() {
        // 任一操作数非数字（字符串/Null/bool）→ Null：判假，不 panic
        assert!(!eval_condition("'a' + 1", &json!({})));
        assert!(!eval_condition("missing + 1 > 0", &json!({})));
        assert!(!eval_condition("flag + 1 > 0", &json!({ "flag": true })));
        // 性质：Null 结果对全部比较运算符（两侧任一位置）均判假
        for cond in [
            "'a' + 1 > 0",
            "'a' + 1 < 0",
            "'a' + 1 >= 0",
            "'a' + 1 <= 0",
            "'a' + 1 == 0",
            "0 > 'a' + 1",
        ] {
            assert!(!eval_condition(cond, &json!({})), "{cond}");
        }
    }

    #[test]
    fn test_arith_division_by_zero() {
        // 除零（整数 0 / 浮点 0.0 / state 键）→ Null + warn，判假不炸
        assert!(!eval_condition("1 / 0 > 0", &json!({})));
        assert!(!eval_condition("1 / 0.0 > 0", &json!({})));
        assert!(!eval_condition("n / d > 0", &json!({ "n": 10, "d": 0 })));
        // 正常除法不受影响
        assert!(eval_condition("n / d == 2.5", &json!({ "n": 5, "d": 2 })));
    }

    #[test]
    fn test_arith_monotonic() {
        // 性质：a + b 与 a * 2 随 a 严格单调递增（b 固定，a 多档递增）
        let mut prev_sum = f64::NEG_INFINITY;
        let mut prev_double = f64::NEG_INFINITY;
        for a in [0, 1, 5, 100] {
            let state = json!({ "a": a, "b": 7 });
            let sum = eval_value(&parse_condition("a + b").unwrap().unwrap(), &state)
                .as_f64()
                .unwrap();
            let double = eval_value(&parse_condition("a * 2").unwrap().unwrap(), &state)
                .as_f64()
                .unwrap();
            assert!(sum > prev_sum);
            assert!(double > prev_double);
            prev_sum = sum;
            prev_double = double;
        }
    }

    #[test]
    fn test_arith_combined_with_logic_and_compare() {
        // 算术嵌进既有 and/or/not 与比较（not 作用于整个比较，算术先折算）
        let state = json!({ "n": 5, "m": 2 });
        assert!(eval_condition("n * m == 10 and n - m > 2", &state));
        assert!(!eval_condition("n * m == 11 or not n + m > 6", &state));
    }

    // ---- 内建函数（Expr::Fn：通用语言设施）----

    #[test]
    fn test_fn_len_semantics() {
        // len：数组元素数 / 字符串字符数；真假两侧 + 两档长度（性质：等于实际长度）
        let state = json!({ "messages": [ {}, {}, {} ], "name": "abc" });
        assert!(eval_condition("len(messages) == 3", &state));
        assert!(eval_condition("len(name) == 3", &state));
        assert!(!eval_condition("len(messages) == 2", &state));
        assert!(eval_condition("len(xs) == 2", &json!({ "xs": [1, 2] })));
        assert!(eval_condition(
            "len(xs) == 5",
            &json!({ "xs": [1, 2, 3, 4, 5] })
        ));
        // len 结果参与算术与比较
        assert!(eval_condition("len(xs) * 2 == 4", &json!({ "xs": [1, 2] })));
        assert!(eval_condition("len(xs) >= 2", &json!({ "xs": [1, 2] })));
        assert!(!eval_condition("len(xs) >= 2", &json!({ "xs": [1] })));
    }

    #[test]
    fn test_fn_len_fail_soft() {
        // 参数非数组/字符串、个数不符（空参/多参）、键缺失 → Null 判假不炸
        assert!(!eval_condition("len(n) > 0", &json!({ "n": 5 })));
        assert!(!eval_condition("len() > 0", &json!({})));
        assert!(!eval_condition(
            "len(a, b) > 0",
            &json!({ "a": [], "b": [] })
        ));
        assert!(!eval_condition("len(missing) > 0", &json!({})));
    }

    #[test]
    fn test_fn_parse_forms() {
        // 解析形态：空参 / 多参 / 嵌套调用（参数表机制）
        assert_eq!(
            parse_condition("f()").unwrap().unwrap(),
            Expr::Fn {
                name: "f".into(),
                args: vec![]
            }
        );
        assert_eq!(
            parse_condition("f(x, 's')").unwrap().unwrap(),
            Expr::Fn {
                name: "f".into(),
                args: vec![
                    Expr::Path {
                        root: "x".into(),
                        steps: vec![]
                    },
                    Expr::Literal(Value::from("s")),
                ],
            }
        );
        assert_eq!(
            parse_condition("f(g())").unwrap().unwrap(),
            Expr::Fn {
                name: "f".into(),
                args: vec![Expr::Fn {
                    name: "g".into(),
                    args: vec![]
                }],
            }
        );
    }

    #[test]
    fn test_fn_parse_errors() {
        // 参数表未闭合 / 尾随逗号 / 空位：加载期报语法错误，不静默
        for src in ["f(a", "f(a,)", "f(,"] {
            assert!(parse_condition(src).is_err(), "{src}");
        }
    }

    #[test]
    fn test_fn_unknown_fail_soft() {
        // 未知函数名（含嵌套）→ Null（fail-soft 判假）不 panic
        assert!(!eval_condition("no_such_fn()", &json!({})));
        assert!(!eval_condition("no_such_fn() > 0", &json!({})));
        assert!(!eval_condition("f(g()) > 0", &json!({})));
        // 同名裸标识符（无 '('）维持路径访问语义
        assert!(eval_condition(
            "no_such_fn > 3",
            &json!({ "no_such_fn": 5 })
        ));
        // 函数形态不破坏括号分组与列表字面量：len([1, 2]) + 1 = 3，×2 = 6
        assert!(eval_condition("(len([1, 2]) + 1) * 2 == 6", &json!({})));
    }

    // ── 语法错误路径（加载期暴露，不静默）──
    //
    // 表驱动覆盖 tokenizer/parser 的各类错误分支：未闭合引号、孤立 '='、
    // 非法字符、未知比较运算符、括号/下标/列表/参数表各类未闭合与错位。
    // 断言"报错且文案指明位置/期望形态"，不断言内部 token 结构。

    #[test]
    fn test_parse_errors_are_reported_with_context() {
        let cases: [&str; 14] = [
            "'unterminated",
            "a = 1",
            "a @ 1",
            "a <> 1",
            "a == (",
            "a == (1]",
            "a.1",
            "a.",
            "a[",
            "a[1)",
            "[1 2]",
            "[1,",
            "len(1 2)",
            "len(1,",
        ];
        for src in cases {
            let err = parse_condition(src).expect_err(&format!("{src:?} 应报语法错误"));
            assert!(
                err.contains("position"),
                "{src:?} 的错误应带位置信息（可定位到表达式内的出错点），实际: {err}"
            );
        }
    }

    /// 下标访问（`a[key]`）的解析与求值：数字下标、字符串键、表达式键
    /// （与 `a.b` 字段访问等价能力）——列表取下标、字典取键。
    #[test]
    fn test_index_access_parses_and_evaluates() {
        let state = json!({
            "items": [10, 20, 30],
            "map": {"k1": "v1", "k2": "v2"},
            "idx": 1,
        });
        // 数组数字下标
        assert!(eval_condition("items[1] == 20", &state));
        assert!(eval_condition("items[0] == 10", &state));
        // 表达式键（变量下标）
        assert!(eval_condition("items[idx] == 20", &state));
        // 字典字符串键（双引号 / 单引号两种字面量）
        assert!(eval_condition(r#"map["k1"] == 'v1'"#, &state));
        assert!(eval_condition("map['k2'] == 'v2'", &state));
        // 链式：下标后再字段访问
        assert!(eval_condition("map['k1'] == 'v1'", &state));
        // 越界/未知键 → Null（fail-soft 判假，不 panic）
        assert!(!eval_condition("items[9] == 20", &state));
        assert!(!eval_condition("map['nope'] == 'v1'", &state));
    }

    /// 解析失败与求值失败都要落到"条件判假"（fail-soft）：管道不因坏条件炸。
    /// 对照：语法合法且为真 → 判真。
    #[test]
    fn test_bad_condition_is_false_not_panic() {
        assert!(!eval_condition("a = 1", &json!({})));
        assert!(!eval_condition("'unterminated", &json!({})));
        assert!(eval_condition("True", &json!({})));
    }

    /// 空表达式与纯空白 → Ok(None)（调用方按"无条件"处理，恒真）。
    /// parse_condition 返回 None 表示无需条件；eval_condition helper 对 None 判真。
    #[test]
    fn test_empty_condition_is_none() {
        for src in ["", "   ", "\t\n "] {
            assert!(
                parse_condition(src)
                    .expect("空白表达式不是语法错误")
                    .is_none(),
                "{src:?} 应解析为 None（无条件）"
            );
        }
        assert!(eval_condition("", &json!({})), "无条件 → 恒真");
    }

    /// 真值判定（truthy）的数值分支：0 与 0.0 为假、非零（含负数/小数）为真。
    /// 表驱动覆盖整数与浮点两条解析路径。
    #[test]
    fn test_truthiness_of_numbers() {
        let state = json!({
            "zero": 0,
            "neg": -3,
            "pos": 7,
            "fzero": 0.0,
            "fpos": 2.5,
        });
        assert!(!eval_condition("zero", &state), "整数 0 为假");
        assert!(eval_condition("pos", &state), "整数非零为真");
        assert!(eval_condition("neg", &state), "负数为真");
        assert!(!eval_condition("fzero", &state), "浮点 0.0 为假");
        assert!(eval_condition("fpos", &state), "浮点非零为真");
    }

    /// 真值判定的字符串/数组/对象分支（Python bool() 对齐）：空串/空列表/空对象
    /// 为假，非空为真。与数值分支共用同一折算点，这里锁非数值三态。
    #[test]
    fn test_truthiness_of_string_array_object() {
        let state = json!({
            "empty_s": "",
            "s": "x",
            "empty_arr": [],
            "arr": [0],
            "empty_obj": {},
            "obj": {"k": null},
        });
        assert!(!eval_condition("empty_s", &state), "空串为假");
        assert!(eval_condition("s", &state), "非空串为真");
        assert!(!eval_condition("empty_arr", &state), "空列表为假");
        assert!(
            eval_condition("arr", &state),
            "非空列表为真（元素值为假不影响）"
        );
        assert!(!eval_condition("empty_obj", &state), "空对象为假");
        assert!(eval_condition("obj", &state), "非空对象为真");
    }

    /// 下标访问的类型错配与越界边界：数组用字符串键 / 对象用数字下标 /
    /// 负下标 / 小数下标 → Null（fail-soft 判假），不 panic 不误命中。
    #[test]
    fn test_index_access_type_mismatch_fail_soft() {
        let state = json!({
            "items": [10, 20],
            "map": {"0": "zero"},
        });
        assert!(
            !eval_condition("items['0'] == 10", &state),
            "数组不接受字符串键"
        );
        assert!(
            !eval_condition("map[0] == 'zero'", &state),
            "对象不接受数字下标"
        );
        assert!(!eval_condition("items[-1] == 20", &state), "负下标越界");
        assert!(
            !eval_condition("items[1.5] == 20", &state),
            "非整数下标越界"
        );
        assert!(!eval_condition("items[2] == 20", &state), "越界下标");
    }

    /// 字符串大小比较（json_cmp 字符串分支）：字典序，两侧有区分度。
    #[test]
    fn test_string_ordering_comparison() {
        let state = json!({ "a": "apple", "b": "banana" });
        assert!(eval_condition("a < b", &state));
        assert!(eval_condition("b > a", &state));
        assert!(!eval_condition("a > b", &state));
        assert!(eval_condition("a <= a and b >= b", &state));
        // 类型错配（字符串 vs 数字）→ 顺序未定义 → 判假，不误判大小
        assert!(!eval_condition("a > 1", &state));
        assert!(!eval_condition("a < 1", &state));
    }

    /// 平键链上状态非对象（标量/数组根）时不得 panic：整链取空 → 判假。
    #[test]
    fn test_dot_chain_on_non_object_state() {
        assert!(!eval_condition("a.b", &json!(5)));
        assert!(!eval_condition("a.b == 1", &json!(5)));
        assert!(!eval_condition("a.b", &json!([1, 2])));
        assert!(!eval_condition("a.b.c == 1", &json!("scalar")));
        // 对照组：同形态表达式在对象 state 上可命中（区分"分支未走"与"恒假"）
        assert!(eval_condition("a.b == 1", &json!({ "a": { "b": 1 } })));
    }

    /// 列表字面量未闭合成因两类：最后一个元素后直接结束（`[1`）与
    /// 元素后跟非法 token（`[1 2]`）——加载期报错且带位置。
    #[test]
    fn test_list_unterminated_variants() {
        for src in ["[", "[1"] {
            let err = parse_condition(src).expect_err(&format!("{src:?} 应报语法错误"));
            assert!(err.contains("position"), "{src:?} 应带位置，实际: {err}");
        }
    }

    /// 未闭合字符串 / 孤立 '=' / 未知比较运算符的错误文案要指明形态
    /// （加载期可定位，不许静默吞掉）。
    #[test]
    fn test_tokenizer_error_messages_name_the_fault() {
        let err = parse_condition("'abc").expect_err("未闭合引号应报错");
        assert!(
            err.contains("Unterminated string literal"),
            "未闭合字符串文案，实际: {err}"
        );
        let err = parse_condition("a = 1").expect_err("孤立 '=' 应报错");
        assert!(
            err.contains("Unexpected '='") && err.contains("position 2"),
            "孤立 '=' 文案含位置，实际: {err}"
        );
        // 单字符 '!' 非法成为比较运算符（tokenizer 产 Op 但白名单外）
        let err = parse_condition("a ! 1").expect_err("非法比较运算符应报错");
        assert!(
            err.contains("未知比较运算符"),
            "未知比较运算符文案，实际: {err}"
        );
    }

    /// 内部纯函数的防御契约：解析不出的数字字面量 / 白名单外的算术与比较
    /// 运算符一律 fail-soft 返回 Null/false（parser 不会产出，但函数契约独立成立）。
    #[test]
    fn test_internal_helpers_fail_soft_on_unknown_forms() {
        assert_eq!(parse_number_literal("not-a-number"), Value::Null);
        // 非有限浮点（serde_json 拒绝 NaN/∞）→ Null，不落非法数值
        assert_eq!(parse_number_literal("NaN"), Value::Null);
        assert_eq!(parse_number_literal("1.5"), json!(1.5));
        assert_eq!(arith("%", &json!(1), &json!(2)), Value::Null);
        assert!(!compare(&json!(1), "??", &json!(1)));
    }

    /// 平键前缀下钻（生产形态 track.llm_usage 平键持 dict）：整链平铺与前段
    /// 平铺两种写入形态都要命中；未命中的对象 state 回落到根名点链求值。
    #[test]
    fn test_flat_prefix_descent_variants() {
        // 前段平铺：前缀 "a.b" 命中后剩余 Field 步骤在命中值内下钻
        let partial = json!({ "a.b": { "c": 1 } });
        assert!(eval_condition("a.b.c == 1", &partial));
        assert!(!eval_condition("a.b.c == 2", &partial));
        // 整链平铺
        let full = json!({ "a.b.c": 5 });
        assert!(eval_condition("a.b.c == 5", &full));
        // 对象 state 但无任何前缀命中：回落根名点链，判假不 panic
        assert!(!eval_condition("a.b.c == 5", &json!({ "x": 1 })));
        // 对照：嵌套写入形态经另一条求值路径给出同一结果
        assert!(eval_condition(
            "a.b.c == 3",
            &json!({ "a": { "b": { "c": 3 } } })
        ));
    }

    /// 标量 state（非对象根）上取路径名：不 panic、判假，与对象 state 形态对照。
    #[test]
    fn test_resolve_name_on_scalar_state_is_null() {
        assert!(!eval_condition("a == 1", &json!(5)));
        assert!(!eval_condition("a", &json!(5)));
        assert!(eval_condition("a == 5", &json!({ "a": 5 })));
    }
}

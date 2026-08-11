//! # WASM 沙箱化科学计算器插件
//!
//! 取代 Python `plugins/shared/tools/simple/calc_tools.py` 的 `eval()` 方案。
//!
//! ## 为什么 WASM 化
//!
//! Python 版用 `eval(expr, {"__builtins__": {}}, {"math", "abs", "round"})` 计算
//! 用户输入的算式——**典型需要沙箱**的场景：用户算式是不可信输入。WASM 天然
//! 隔离（无文件系统/网络/进程/宿主内存访问），且本实现用**手写递归下降解析器**
//! 取代 `eval`，彻底消除 `eval` 的逃逸面（Python eval 的 `__builtins__` 空字典
//! 并非真正沙箱，构造恶意字面量仍可能逃逸）。
//!
//! ## 行为对齐
//!
//! 输入（作为 tool 调用，state 即工具参数）：
//! ```json
//! {"state": {"operation": "calculate", "expression": "1+2*3"}, "config": {}}
//! ```
//! 或
//! ```json
//! {"state": {"operation": "evaluate", "func": "sqrt", "value": 16}, "config": {}}
//! ```
//!
//! 输出（PluginResult，state_updates 即工具返回数据）：
//! ```json
//! {"state_updates": {"result": 7, "expression": "1+2*3"}}
//! ```
//!
//! ## ABI 契约（与内核 wasm_loader.rs 共享）
//!
//! 导出 `memory` + `allocate(len) -> ptr` + `deallocate(ptr, len)`
//! + `execute(in_ptr, in_len) -> packed(out_ptr | out_len << 32)`。
//!
//! 输入/输出都是 JSON 字符串。

use serde_json::{json, Value};

// ── WASM 线性内存 ABI（bump 分配器，对齐 wasm_hello 样例） ──────────────────

/// bump 指针存放偏移（跳过偏移 0..4 保留区）。
const BUMP_LOC: usize = 4;
/// 堆起始（跳过 bump 指针自身 4 字节 + 对齐）。
const HEAP_START: usize = 8;

fn load_bump() -> usize {
    unsafe { load_i32(BUMP_LOC) as usize }
}
fn store_bump(val: usize) {
    unsafe { store_i32(BUMP_LOC, val as i32) };
}

fn ensure_init() {
    if load_bump() == 0 {
        store_bump(HEAP_START);
    }
}

fn align4(n: usize) -> usize {
    (n + 3) & !3
}

#[no_mangle]
pub extern "C" fn allocate(len: i32) -> i32 {
    ensure_init();
    let len = len as usize;
    let ptr = load_bump();
    store_bump(align4(ptr + len));
    ptr as i32
}

#[no_mangle]
pub extern "C" fn deallocate(_ptr: i32, _len: i32) {
    // bump 分配器不回收（单次 invoke 生命周期内足够）
}

/// execute(in_ptr, in_len) -> packed(out_ptr | out_len << 32)
#[no_mangle]
pub extern "C" fn execute(in_ptr: i32, in_len: i32) -> i64 {
    ensure_init();
    // 读入输入 JSON
    let in_bytes = unsafe {
        core::slice::from_raw_parts(in_ptr as usize as *const u8, in_len as usize)
    };
    let input_str = match core::str::from_utf8(in_bytes) {
        Ok(s) => s,
        Err(_) => {
            let err = error_result("输入非 UTF-8");
            return pack_output(&err.to_string());
        }
    };

    let out_json = match run(input_str) {
        Ok(v) => v,
        Err(msg) => error_result(&msg),
    };
    pack_output(&out_json.to_string())
}

/// 把输出 JSON 写入线性内存，返回 packed(ptr | len << 32)。
fn pack_output(s: &str) -> i64 {
    let out_bytes = s.as_bytes();
    let out_len = out_bytes.len();
    let out_ptr = allocate(out_len as i32);
    for (i, &b) in out_bytes.iter().enumerate() {
        unsafe { store_u8(out_ptr as usize + i, b) };
    }
    (((out_len as u64) << 32) | (out_ptr as u32 as u64)) as i64
}

// ── 线性内存 raw 读写 ─────────────────────────────────────────────────────

#[inline]
unsafe fn load_i32(offset: usize) -> i32 {
    let p = offset as *mut u8;
    let b0 = *p as u32;
    let b1 = *p.add(1) as u32;
    let b2 = *p.add(2) as u32;
    let b3 = *p.add(3) as u32;
    (b0 | (b1 << 8) | (b2 << 16) | (b3 << 24)) as i32
}

#[inline]
unsafe fn store_i32(offset: usize, val: i32) {
    let p = offset as *mut u8;
    let v = val as u32;
    *p = (v & 0xFF) as u8;
    *p.add(1) = ((v >> 8) & 0xFF) as u8;
    *p.add(2) = ((v >> 16) & 0xFF) as u8;
    *p.add(3) = ((v >> 24) & 0xFF) as u8;
}

#[inline]
unsafe fn store_u8(offset: usize, val: u8) {
    *(offset as *mut u8) = val;
}

// ── 主入口：解析输入 JSON，分派到 calculate / evaluate ──────────────────────

/// 工具入参（state 字段，对齐 calc_tools.py 的 schema）。
#[derive(Debug)]
enum Operation {
    Calculate { expression: String },
    Evaluate { func: String, value: Option<f64>, values: Option<Vec<f64>> },
}

fn run(input_str: &str) -> Result<Value, String> {
    let input: Value = serde_json::from_str(input_str)
        .map_err(|e| format!("输入 JSON 解析失败: {e}"))?;

    // WASM tool 调用约定：state 即工具参数。
    let params = input.get("state").unwrap_or(&input);

    let op = parse_operation(params)?;
    let result = match op {
        Operation::Calculate { expression } => {
            if expression.trim().is_empty() {
                return Err("表达式不能为空".into());
            }
            let val = eval_expression(&expression)?;
            json!({
                "expression": expression,
                "result": normalize_number(val)?,
            })
        }
        Operation::Evaluate { func, value, values } => {
            if func.trim().is_empty() {
                return Err("函数名不能为空".into());
            }
            // 克隆 value/values：eval_single_func 取所有权，这里还要再用 value/values
            // 构造 "input" 字段。克隆成本可忽略（一次工具调用）。
            let input_field = if let Some(v) = value { json!(v) } else { json!(values.clone()) };
            let val = eval_single_func(&func, value, values)?;
            // NaN / Inf 检查（对齐 Python 的 math.isnan/isinf 错误返回）
            if val.is_nan() {
                return Err("计算结果为非数值（NaN）".into());
            }
            if val.is_infinite() {
                return Err("计算结果为无穷大".into());
            }
            json!({
                "function": func,
                "input": input_field,
                "result": normalize_number(val)?,
            })
        }
    };

    // 包成 PluginResult.state_updates（invoke_wasm_tool 把 state_updates 当工具返回数据）。
    Ok(json!({ "state_updates": result }))
}

/// 从 params 解析 operation 字段，分派到 Calculate / Evaluate。
fn parse_operation(params: &Value) -> Result<Operation, String> {
    let operation = params
        .get("operation")
        .and_then(|v| v.as_str())
        .ok_or("缺少 operation 参数")?;
    match operation {
        "calculate" => {
            let expression = params
                .get("expression")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            Ok(Operation::Calculate { expression })
        }
        "evaluate" => {
            let func = params
                .get("func")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let value = params.get("value").and_then(num_from_value);
            let values = params
                .get("values")
                .and_then(|v| v.as_array())
                .map(|arr| arr.iter().filter_map(num_from_value).collect::<Vec<_>>());
            Ok(Operation::Evaluate { func, value, values })
        }
        other => Err(format!("不支持的运算类型: {other}")),
    }
}

/// 从 JSON Value 提取 f64（兼容整数 / 浮点字面量）。
fn num_from_value(v: &Value) -> Option<f64> {
    v.as_f64()
}

/// 把浮点结果规范化为 JSON 友好形态（对齐 Python：整数结果返回 int）。
fn normalize_number(val: f64) -> Result<Value, String> {
    if val.is_nan() {
        return Err("计算结果为非数值（NaN）".into());
    }
    if val.is_infinite() {
        return Err("计算结果为无穷大".into());
    }
    // 整数值 → int，否则 round 到 10 位小数（对齐 Python round(result, 10)）。
    if val.fract() == 0.0 && val.abs() < 1e15 {
        Ok(json!(val as i64))
    } else {
        Ok(json!(round10(val)))
    }
}

/// 四舍五入到 10 位小数（对齐 Python round(x, 10)）。
fn round10(x: f64) -> f64 {
    let factor = 1e10;
    (x * factor).round() / factor
}

// ── evaluate（单函数求值，对齐 calc_tools._evaluate_single） ────────────────

fn eval_single_func(func: &str, value: Option<f64>, values: Option<Vec<f64>>) -> Result<f64, String> {
    let f = func.to_lowercase();
    // 常量（对齐 _CONSTANTS）
    match f.as_str() {
        "pi" => return Ok(core::f64::consts::PI),
        "e" => return Ok(core::f64::consts::E),
        "tau" => return Ok(core::f64::consts::TAU),
        "inf" => return Ok(f64::INFINITY),
        _ => {}
    }

    // 三角（对齐 _OPERATIONS：sin/cos/tan 输入角度，asin/acos/atan 输出角度）
    let unary = |v: f64| -> Result<f64, String> {
        match f.as_str() {
            "sin" => Ok(v.to_radians().sin()),
            "cos" => Ok(v.to_radians().cos()),
            "tan" => Ok(v.to_radians().tan()),
            "asin" => Ok(v.asin().to_degrees()),
            "acos" => Ok(v.acos().to_degrees()),
            "atan" => Ok(v.atan().to_degrees()),
            "sinh" => Ok(v.sinh()),
            "cosh" => Ok(v.cosh()),
            "tanh" => Ok(v.tanh()),
            "ln" => Ok(v.ln()),
            "log10" => Ok(v.log10()),
            "log2" => Ok(v.log2()),
            "exp" => Ok(v.exp()),
            "sqrt" => Ok(v.sqrt()),
            "abs" => Ok(v.abs()),
            "ceil" => Ok(v.ceil()),
            "floor" => Ok(v.floor()),
            "degrees" => Ok(v.to_degrees()),
            "radians" => Ok(v.to_radians()),
            // cbrt：copysign(|x|^(1/3), x)（对齐 Python lambda）
            "cbrt" => Ok(v.abs().powf(1.0 / 3.0).copysign(v)),
            // factorial：仅非负整数
            "factorial" => {
                if v < 0.0 || v.fract() != 0.0 || v > 170.0 {
                    return Err("函数 factorial 需要非负整数（0..=170）".to_string());
                }
                Ok((1..=v as u64).product::<u64>() as f64)
            }
            _ => Err(format!("不支持的函数: {func}")),
        }
    };

    // 双参数函数（对齐 pow / log / gcd）
    match f.as_str() {
        "pow" | "log" | "gcd" => {
            let vals = values.as_ref().filter(|v| v.len() >= 2)
                .ok_or(format!("函数 {func} 需要两个参数（values 数组）"))?;
            let (a, b) = (vals[0], vals[1]);
            return match f.as_str() {
                "pow" => Ok(a.powf(b)),
                "log" => {
                    if b == 0.0 {
                        Ok(a.ln()) // base 为 0/缺省 → 自然对数（对齐 Python: base if base else log(x)）
                    } else {
                        Ok(a.log(b))
                    }
                }
                "gcd" => {
                    if a.fract() != 0.0 || b.fract() != 0.0 || a < 0.0 || b < 0.0 {
                        return Err("gcd 需要非负整数".into());
                    }
                    Ok(gcd(a as u64, b as u64) as f64)
                }
                _ => unreachable!(),
            };
        }
        _ => {}
    }

    let v = value.ok_or(format!("函数 {func} 需要 value 参数"))?;
    unary(v)
}

fn gcd(a: u64, b: u64) -> u64 {
    if b == 0 { a } else { gcd(b, a % b) }
}

// ── calculate（表达式求值，手写递归下降解析器取代 Python eval） ──────────────
//
// 文法（优先级从低到高）：
//   expr    := term (('+' | '-') term)*
//   term    := factor (('*' | '/' | '%') factor)*
//   factor  := power
//   power   := unary ('^' unary)*      // 右结合
//   unary   := ('+' | '-') unary | atom
//   atom    := number | constant | func '(' expr (',' expr)* ')' | '(' expr ')'
//
// 支持的常量：pi / e / tau / inf
// 支持的函数：sin cos tan asin acos atan sinh cosh tanh ln log10 log2 exp sqrt abs
//             ceil floor degrees radians cbrt factorial（与 _safe_eval.safe_funcs 对齐）
//
// 不支持的（故意收紧）：Python eval 的任意表达式 / 列表推导 / 字符串字面量 /
// 属性访问 / 调用任意可调用对象。本解析器只接受上述文法——任何越界输入直接报错。

fn eval_expression(expr: &str) -> Result<f64, String> {
    // 常量替换（对齐 Python _safe_eval 把 pi/e/tau/inf 替成数值）——
    // 这里改为在 atom 阶段识别常量名，避免字符串替换的歧义（如变量名含 "e"）。
    let mut p = Parser::new(expr);
    let val = p.parse_expr()?;
    p.skip_ws();
    if !p.at_end() {
        return Err(format!("表达式末尾有未识别字符: {:?}", p.rest()));
    }
    Ok(val)
}

struct Parser<'a> {
    chars: &'a [u8],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn new(s: &'a str) -> Self {
        Self { chars: s.as_bytes(), pos: 0 }
    }

    fn rest(&self) -> &str {
        core::str::from_utf8(&self.chars[self.pos..]).unwrap_or("<bad utf8>")
    }

    fn at_end(&self) -> bool {
        self.pos >= self.chars.len()
    }

    fn peek(&self) -> Option<u8> {
        self.chars.get(self.pos).copied()
    }

    fn skip_ws(&mut self) {
        while let Some(c) = self.peek() {
            if c.is_ascii_whitespace() {
                self.pos += 1;
            } else {
                break;
            }
        }
    }

    /// expr := term (('+' | '-') term)*
    fn parse_expr(&mut self) -> Result<f64, String> {
        let mut acc = self.parse_term()?;
        loop {
            self.skip_ws();
            match self.peek() {
                Some(b'+') => { self.pos += 1; acc += self.parse_term()?; }
                Some(b'-') => { self.pos += 1; acc -= self.parse_term()?; }
                _ => break,
            }
        }
        Ok(acc)
    }

    /// term := factor (('*' | '/' | '%') factor)*
    fn parse_term(&mut self) -> Result<f64, String> {
        let mut acc = self.parse_factor()?;
        loop {
            self.skip_ws();
            match self.peek() {
                Some(b'*') => {
                    self.pos += 1;
                    // 误把 ** 当 * 处理：power 在 factor 内已消费，这里只接单 *
                    acc *= self.parse_factor()?;
                }
                Some(b'/') => {
                    self.pos += 1;
                    let d = self.parse_factor()?;
                    if d == 0.0 {
                        return Err("除数不能为零".into());
                    }
                    acc /= d;
                }
                Some(b'%') => {
                    self.pos += 1;
                    let d = self.parse_factor()?;
                    if d == 0.0 {
                        return Err("模数不能为零".into());
                    }
                    acc %= d;
                }
                _ => break,
            }
        }
        Ok(acc)
    }

    /// factor := power
    fn parse_factor(&mut self) -> Result<f64, String> {
        self.parse_power()
    }

    /// power := unary ('^' power)   // 右结合
    fn parse_power(&mut self) -> Result<f64, String> {
        let base = self.parse_unary()?;
        self.skip_ws();
        if self.peek() == Some(b'^') {
            self.pos += 1;
            let exp = self.parse_power()?; // 右结合：递归调 parse_power
            Ok(base.powf(exp))
        } else {
            Ok(base)
        }
    }

    /// unary := ('+' | '-') unary | atom
    fn parse_unary(&mut self) -> Result<f64, String> {
        self.skip_ws();
        match self.peek() {
            Some(b'+') => { self.pos += 1; self.parse_unary() }
            Some(b'-') => { self.pos += 1; Ok(-self.parse_unary()?) }
            _ => self.parse_atom(),
        }
    }

    /// atom := number | constant | func '(' args ')' | '(' expr ')'
    fn parse_atom(&mut self) -> Result<f64, String> {
        self.skip_ws();
        match self.peek() {
            Some(b'(') => {
                self.pos += 1;
                let v = self.parse_expr()?;
                self.skip_ws();
                if self.peek() != Some(b')') {
                    return Err(format!("缺少右括号: {:?}", self.rest()));
                }
                self.pos += 1;
                Ok(v)
            }
            Some(c) if c.is_ascii_digit() || c == b'.' => self.parse_number(),
            Some(c) if c.is_ascii_alphabetic() || c == b'_' => self.parse_name_or_func(),
            other => Err(format!("意外的字符: {:?}", other.map(|c| c as char))),
        }
    }

    fn parse_number(&mut self) -> Result<f64, String> {
        let start = self.pos;
        let mut seen_dot = false;
        let mut seen_e = false;
        while let Some(c) = self.peek() {
            match c {
                b'0'..=b'9' => self.pos += 1,
                b'.' if !seen_dot && !seen_e => { seen_dot = true; self.pos += 1; }
                b'e' | b'E' if !seen_e => {
                    seen_e = true;
                    self.pos += 1;
                    // 指数符号
                    if matches!(self.peek(), Some(b'+') | Some(b'-')) {
                        self.pos += 1;
                    }
                }
                _ => break,
            }
        }
        let s = core::str::from_utf8(&self.chars[start..self.pos])
            .map_err(|_| "数字字面量非 UTF-8".to_string())?;
        s.parse::<f64>().map_err(|e| format!("数字解析失败 {s:?}: {e}"))
    }

    /// 标识符：可能是常量（pi/e/tau/inf）或函数调用 name(args)。
    fn parse_name_or_func(&mut self) -> Result<f64, String> {
        let start = self.pos;
        while let Some(c) = self.peek() {
            if c.is_ascii_alphanumeric() || c == b'_' {
                self.pos += 1;
            } else {
                break;
            }
        }
        let name = core::str::from_utf8(&self.chars[start..self.pos])
            .map_err(|_| "标识符非 UTF-8".to_string())?;
        let lower = name.to_lowercase();
        self.skip_ws();
        if self.peek() == Some(b'(') {
            // 函数调用
            self.pos += 1;
            let mut args: Vec<f64> = Vec::new();
            self.skip_ws();
            if self.peek() != Some(b')') {
                args.push(self.parse_expr()?);
                self.skip_ws();
                while self.peek() == Some(b',') {
                    self.pos += 1;
                    args.push(self.parse_expr()?);
                    self.skip_ws();
                }
            }
            if self.peek() != Some(b')') {
                return Err(format!("函数 {name} 缺少右括号"));
            }
            self.pos += 1;
            apply_func(&lower, &args)
        } else {
            // 常量
            match lower.as_str() {
                "pi" => Ok(core::f64::consts::PI),
                "e" => Ok(core::f64::consts::E),
                "tau" => Ok(core::f64::consts::TAU),
                "inf" => Ok(f64::INFINITY),
                _ => Err(format!("未知标识符: {name}")),
            }
        }
    }
}

/// 应用数学函数（表达式内调用，单/双参数分派）。
fn apply_func(name: &str, args: &[f64]) -> Result<f64, String> {
    let unary = |v: f64| -> Result<f64, String> {
        match name {
            "sin" => Ok(v.to_radians().sin()),
            "cos" => Ok(v.to_radians().cos()),
            "tan" => Ok(v.to_radians().tan()),
            "asin" => Ok(v.asin().to_degrees()),
            "acos" => Ok(v.acos().to_degrees()),
            "atan" => Ok(v.atan().to_degrees()),
            "sinh" => Ok(v.sinh()),
            "cosh" => Ok(v.cosh()),
            "tanh" => Ok(v.tanh()),
            "ln" => Ok(v.ln()),
            "log10" => Ok(v.log10()),
            "log2" => Ok(v.log2()),
            "exp" => Ok(v.exp()),
            "sqrt" => Ok(v.sqrt()),
            "abs" => Ok(v.abs()),
            "ceil" => Ok(v.ceil()),
            "floor" => Ok(v.floor()),
            "degrees" => Ok(v.to_degrees()),
            "radians" => Ok(v.to_radians()),
            "cbrt" => Ok(v.abs().powf(1.0 / 3.0).copysign(v)),
            "factorial" => {
                if v < 0.0 || v.fract() != 0.0 || v > 170.0 {
                    return Err("factorial 需要非负整数（0..=170）".into());
                }
                Ok((1..=v as u64).product::<u64>() as f64)
            }
            _ => Err(format!("不支持的函数: {name}")),
        }
    };
    match name {
        "pow" => {
            check_args(name, args, 2)?;
            Ok(args[0].powf(args[1]))
        }
        "log" => {
            check_args(name, args, 2)?;
            if args[1] == 0.0 {
                Ok(args[0].ln())
            } else {
                Ok(args[0].log(args[1]))
            }
        }
        "gcd" => {
            check_args(name, args, 2)?;
            if args[0].fract() != 0.0 || args[1].fract() != 0.0
                || args[0] < 0.0 || args[1] < 0.0 {
                return Err("gcd 需要非负整数".into());
            }
            Ok(gcd(args[0] as u64, args[1] as u64) as f64)
        }
        _ => {
            check_args(name, args, 1)?;
            unary(args[0])
        }
    }
}

fn check_args(name: &str, args: &[f64], expected: usize) -> Result<(), String> {
    if args.len() != expected {
        return Err(format!("函数 {name} 需要 {expected} 个参数，实际 {}", args.len()));
    }
    Ok(())
}

// ── 错误结果构造（对齐 Python calc_tools 的 {"error": ...} 返回） ────────────

fn error_result(msg: &str) -> Value {
    json!({ "state_updates": { "error": msg } })
}

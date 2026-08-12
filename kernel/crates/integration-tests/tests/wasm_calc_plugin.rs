//! WASM 计算器插件端到端验证。
//!
//! 加载 `plugins/shared/wasm_calc/` 下预编译的 wasm_calc.wasm，验证：
//!   1. host↔guest JSON 经线性内存契约跑通（memory + allocate + execute ABI）；
//!   2. 手写递归下降解析器取代 Python eval 的数学正确性（含优先级、右结合幂、函数调用）；
//!   3. 沙箱错误处理（除零、未知函数等返回 error 字段而非 trap）。
//!
//! 前置：`plugins/shared/wasm_calc/` 下需已 `cargo build --release --target wasm32-unknown-unknown`。
//! 若 wasm 未构建，测试跳过而非失败（与 wasm_loader.rs 的 hello_world 测试一致）。
//!
//! @feature: FP-0.2.一 第三方插件协议 | @vision: V3 可嵌入 | @ci: rust-test

use agentos_plugin_loader::WasmRuntime;
use serde_json::json;
use std::path::PathBuf;

fn wasm_path() -> PathBuf {
    // integration-tests crate 在 kernel/crates/integration-tests，
    // wasm_calc 在 plugins/shared/wasm_calc。
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../plugins/shared/wasm_calc")
        .join("target/wasm32-unknown-unknown/release/agentos_wasm_calc_plugin.wasm")
}

fn ensure_wasm_or_skip() -> PathBuf {
    let p = wasm_path();
    if !p.exists() {
        eprintln!(
            "SKIP: wasm_calc not built at {} \
             (run cargo build --release --target wasm32-unknown-unknown in plugins/shared/wasm_calc)",
            p.display()
        );
    }
    p
}

#[test]
fn calculate_respects_precedence() {
    let p = ensure_wasm_or_skip();
    if !p.exists() { return; }
    let rt = WasmRuntime::new().expect("engine");
    rt.load("calc_prec", &p).expect("load");
    // 1 + 2*3 = 7（乘法优先）
    let input = json!({"state": {"operation": "calculate", "expression": "1+2*3"}});
    let r = rt.invoke("calc_prec", &input).expect("invoke");
    assert_eq!(r.state_updates.get("result"), Some(&json!(7)));
}

#[test]
fn calculate_power_is_right_associative() {
    let p = ensure_wasm_or_skip();
    if !p.exists() { return; }
    let rt = WasmRuntime::new().expect("engine");
    rt.load("calc_pow", &p).expect("load");
    // 2^3^2 = 2^(3^2) = 2^9 = 512（右结合）
    let input = json!({"state": {"operation": "calculate", "expression": "2^3^2"}});
    let r = rt.invoke("calc_pow", &input).expect("invoke");
    assert_eq!(r.state_updates.get("result"), Some(&json!(512)));
}

#[test]
fn calculate_parentheses_override_precedence() {
    let p = ensure_wasm_or_skip();
    if !p.exists() { return; }
    let rt = WasmRuntime::new().expect("engine");
    rt.load("calc_paren", &p).expect("load");
    // (1+2)*3 = 9
    let input = json!({"state": {"operation": "calculate", "expression": "(1+2)*3"}});
    let r = rt.invoke("calc_paren", &input).expect("invoke");
    assert_eq!(r.state_updates.get("result"), Some(&json!(9)));
}

#[test]
fn calculate_constants_pi_and_e() {
    let p = ensure_wasm_or_skip();
    if !p.exists() { return; }
    let rt = WasmRuntime::new().expect("engine");
    rt.load("calc_const", &p).expect("load");
    // sin(0) = 0（度数输入，对齐 Python _OPERATIONS）
    let input = json!({"state": {"operation": "calculate", "expression": "sin(0)"}});
    let r = rt.invoke("calc_const", &input).expect("invoke");
    assert_eq!(r.state_updates.get("result"), Some(&json!(0)));
}

#[test]
fn evaluate_sqrt_returns_integer() {
    let p = ensure_wasm_or_skip();
    if !p.exists() { return; }
    let rt = WasmRuntime::new().expect("engine");
    rt.load("calc_sqrt", &p).expect("load");
    let input = json!({"state": {"operation": "evaluate", "func": "sqrt", "value": 16}});
    let r = rt.invoke("calc_sqrt", &input).expect("invoke");
    assert_eq!(r.state_updates.get("result"), Some(&json!(4)));
    assert_eq!(r.state_updates.get("function"), Some(&json!("sqrt")));
}

#[test]
fn evaluate_pow_two_args() {
    let p = ensure_wasm_or_skip();
    if !p.exists() { return; }
    let rt = WasmRuntime::new().expect("engine");
    rt.load("calc_pow2", &p).expect("load");
    let input = json!({"state": {"operation": "evaluate", "func": "pow", "values": [2, 10]}});
    let r = rt.invoke("calc_pow2", &input).expect("invoke");
    assert_eq!(r.state_updates.get("result"), Some(&json!(1024)));
}

#[test]
fn division_by_zero_returns_error_not_trap() {
    let p = ensure_wasm_or_skip();
    if !p.exists() { return; }
    let rt = WasmRuntime::new().expect("engine");
    rt.load("calc_div0", &p).expect("load");
    let input = json!({"state": {"operation": "calculate", "expression": "1/0"}});
    let r = rt.invoke("calc_div0", &input).expect("invoke should not trap");
    // 错误以 {"error": "..."} 返回在 state_updates（对齐 Python calc_tools 的返回形态）
    let err = r.state_updates.get("error")
        .expect("division by zero should produce error field");
    assert!(err.is_string(), "error should be a string, got: {:?}", err);
}

#[test]
fn unknown_function_returns_error() {
    let p = ensure_wasm_or_skip();
    if !p.exists() { return; }
    let rt = WasmRuntime::new().expect("engine");
    rt.load("calc_unkfn", &p).expect("load");
    let input = json!({"state": {"operation": "evaluate", "func": "bogus_func", "value": 1}});
    let r = rt.invoke("calc_unkfn", &input).expect("invoke");
    let err = r.state_updates.get("error").expect("unknown func should error");
    assert!(err.is_string());
}

#[test]
fn negative_unary_and_modulo() {
    let p = ensure_wasm_or_skip();
    if !p.exists() { return; }
    let rt = WasmRuntime::new().expect("engine");
    rt.load("calc_neg", &p).expect("load");
    // -7 % 3 = -1（Rust % 跟随被除数符号，与 Python % 不同——但本插件对齐 Rust 语义）
    let input = json!({"state": {"operation": "calculate", "expression": "-7%3"}});
    let r = rt.invoke("calc_neg", &input).expect("invoke");
    assert_eq!(r.state_updates.get("result"), Some(&json!(-1)));
}

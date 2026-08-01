//! # WASM hello world 插件样例（task_11 N10）
//!
//! 验证内核 `WasmRuntime` 的 host↔guest JSON 经线性内存契约 + 实测产物体积。
//!
//! ## 编译
//!
//! ```bash
//! cargo build --release --target wasm32-unknown-unknown
//! # 产物：target/wasm32-unknown-unknown/release/agentos_wasm_hello_plugin.wasm
//! ```
//!
//! 不依赖 WASI——纯 core wasm（`wasm32-unknown-unknown`），体积更小。
//! 完整 WIT/组件模型（wasm32-wasip2）待工具链 PoC（计划文档 §八 #1）。
//!
//! ## ABI 契约（与内核 `wasm_loader.rs` 共享）
//!
//! 导出 `memory` + `allocate(len) -> ptr` + `deallocate(ptr, len)`
//! + `execute(in_ptr, in_len) -> packed(out_ptr | out_len << 32)`。
//! 输入/输出都是 JSON 字符串（PluginInput / PluginResult）。
//!
//! ## 行为
//!
//! 返回固定的成功 PluginResult：`{"state_updates":{"processed_by":"wasm_hello"}}`。
//! 输入经线性内存传入但不解析（最小样例，避免引入 JSON 库增大体积）。
//! 真实插件可链接 serde_json（或后续 WIT 方案）做完整解析。

#![no_std]

use core::panic::PanicInfo;

/// 全局 bump 分配器指针（线性内存偏移）。
/// 放在偏移 4 处（偏移 0..4 保留，避免解引用近 null 偏移被编译器当 UB 优化成 trap）。
const BUMP_LOC: usize = 4;
const HEAP_START: usize = 8;

/// 读取/写入 bump 指针（i32，存放在线性内存偏移 BUMP_LOC）。
fn load_bump() -> usize {
    unsafe { load_i32(BUMP_LOC) as usize }
}
fn store_bump(val: usize) {
    unsafe { store_i32(BUMP_LOC, val as i32) };
}

/// 初始化 bump 指针（首次调用时）。
fn ensure_init() {
    if load_bump() == 0 {
        store_bump(HEAP_START);
    }
}

/// 4 字节对齐。
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
    // bump 分配器不回收（样例简化）
}

/// execute(in_ptr, in_len) -> packed(out_ptr | out_len << 32)
///
/// 返回固定的 PluginResult JSON。
#[no_mangle]
pub extern "C" fn execute(_in_ptr: i32, _in_len: i32) -> i64 {
    ensure_init();
    // 固定输出：{"state_updates":{"processed_by":"wasm_hello"}}
    const OUT: &[u8] = b"{\"state_updates\":{\"processed_by\":\"wasm_hello\"}}";
    let out_len = OUT.len();
    let out_ptr = allocate(out_len as i32);
    // 把 OUT 字节写入线性内存 out_ptr..
    for (i, &b) in OUT.iter().enumerate() {
        unsafe { store_u8(out_ptr as usize + i, b) };
    }
    (((out_len as u64) << 32) | (out_ptr as u32 as u64)) as i64
}

// ── 线性内存 raw 读写（wasm32：memory 默认导出） ──────────────────────────

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

/// panic handler（no_std 必需）——样例里直接 abort。
#[panic_handler]
fn panic(_info: &PanicInfo) -> ! {
    // WASM 没有 abort 语义，用 unreachable trap
    loop {
        // 编译为 unreachable（trap）
        unsafe {
            core::hint::unreachable_unchecked();
        }
    }
}

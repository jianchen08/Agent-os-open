//! # Lingxi AgentOS 原生插件 SDK（cdylib + C-ABI）
//!
//! task_11（N3）：简化 Rust 原生插件作者负担。插件作者写：
//!
//! ```ignore
//! use agentos_native_sdk::{plugin_entry, PluginInput, PluginResult};
//!
//! plugin_entry!(|input: PluginInput| -> PluginResult {
//!     PluginResult::ok()
//! });
//! ```
//!
//! 宏展开成 `#[no_mangle] extern "C" fn plugin_execute(...)` + `plugin_free(...)`，
//! 作者不碰 unsafe / FFI / 内存分配。
//!
//! ## C-ABI 契约（与内核 `NativePluginLoader` 共享，N2）
//!
//! 插件导出两个 `extern "C"` 符号：
//!
//! ```text
//! // 执行插件。返回码：0 = 成功，1 = 插件业务错误（out 仍是合法 JSON PluginResult），
//! // -1 = 致命错误（序列化/分配失败）。
//! plugin_execute(
//!     input_ptr: *const u8,   // 输入 JSON（PluginInput 序列化），内核拥有，只读
//!     input_len: usize,
//!     out_ptr:   *mut *mut u8, // 输出参数：插件写入分配的缓冲区指针
//!     out_len:   *mut usize,   // 输出参数：插件写入缓冲区长度
//! ) -> i32;
//!
//! // 释放插件分配的缓冲区（与 plugin_execute 的分配器匹配，跨分配器安全）。
//! plugin_free(ptr: *mut u8, len: usize);
//! ```
//!
//! 输入/输出都是 JSON 字符串（ABI 解耦：Rust ABI 跨编译器/依赖版本不稳定，
//! 序列化解耦彻底。详见计划文档 §3.1）。
//!
//! ## 为什么单独有 plugin_free
//!
//! 插件可能是不同 rustc/不同分配器编译的 cdylib。内核 free 插件分配的内存
//! 会 UB。约定插件自己 export `plugin_free`，内核调用它释放——分配/释放在同一
//! 分配器内，零 UB。

#![forbid(unsafe_op_in_unsafe_fn)]

use serde::{Deserialize, Serialize};

pub use serde_json;

// ── 对外类型（与内核 PluginContext / PluginResult 对齐，但解耦） ─────────

/// 插件输入——内核把 PluginContext 的可序列化子集注入。
///
/// 与 `agentos_core::types::PluginContext` 字段对齐，但只保留可跨进程/跨 ABI
/// 传递的部分（state / config / tenant / session_id / task_id）。
/// ContentLoader 等非序列化句柄不传——原生插件需要消息内容时由内核预注入到 state。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PluginInput {
    /// 管道当前状态（JSON Value）。
    #[serde(default)]
    pub state: serde_json::Value,
    /// 插件配置（内核按 config_files 映射注入）。
    #[serde(default)]
    pub config: serde_json::Value,
    /// 租户 ID。
    #[serde(default)]
    pub tenant_id: String,
    /// 会话 ID。
    #[serde(default)]
    pub session_id: String,
    /// 任务 ID。
    #[serde(default)]
    pub task_id: String,
}

impl PluginInput {
    /// 创建一个空输入（测试用）。
    pub fn new() -> Self {
        Self::default()
    }
}

/// 插件输出错误（与内核 PluginError 对齐）。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PluginResultError {
    #[serde(default)]
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub code: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
}

/// 路由信号（与内核 RouteSignal 对齐的简化子集）。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PluginRouteSignal {
    /// 路由类型字符串："next_llm" / "next_tool" / "end" / "wait"。
    #[serde(default)]
    pub route_type: String,
}

/// 插件执行结果——与 `agentos_core::types::PluginResult` 字段对齐。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PluginResult {
    /// 状态更新 Patch（key → value）。
    #[serde(default)]
    pub state_updates: std::collections::HashMap<String, serde_json::Value>,
    /// 路由信号（仅 Output 插件）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub route_signal: Option<PluginRouteSignal>,
    /// 是否跳过后续插件。
    #[serde(default)]
    pub skip_remaining: bool,
    /// 执行异常（None = 成功）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<PluginResultError>,
}

impl PluginResult {
    /// 成功的空结果。
    pub fn ok() -> Self {
        Self::default()
    }

    /// 带状态更新。
    pub fn with_state_updates(
        mut self,
        updates: std::collections::HashMap<String, serde_json::Value>,
    ) -> Self {
        self.state_updates = updates;
        self
    }

    /// 构造一个错误结果。
    pub fn error(message: impl Into<String>, code: impl Into<String>) -> Self {
        Self {
            error: Some(PluginResultError {
                message: message.into(),
                code: Some(code.into()),
                source: None,
            }),
            ..Self::default()
        }
    }
}

// ── 内部：缓冲区分配/释放（插件侧分配器） ─────────────────────────────

/// 在插件分配器内分配一份字节缓冲区的副本，返回 (ptr, len)。
///
/// 用于 `plugin_execute` 写出输出参数。配套的 `plugin_free` 释放。
fn alloc_buf(bytes: &[u8]) -> (*mut u8, usize) {
    let len = bytes.len();
    if len == 0 {
        // 分配 1 字节占位，避免空指针歧义；len 仍记 0。
        let layout = std::alloc::Layout::new::<u8>();
        let p = unsafe { std::alloc::alloc(layout) };
        return (p, 0);
    }
    let layout = std::alloc::Layout::from_size_align(len, 1).expect("layout");
    let p = unsafe { std::alloc::alloc(layout) };
    if p.is_null() {
        return (std::ptr::null_mut(), 0);
    }
    unsafe {
        std::ptr::copy_nonoverlapping(bytes.as_ptr(), p, len);
    }
    (p, len)
}

/// 释放由 `alloc_buf` 分配的缓冲区。
///
/// # Safety
/// `ptr` 必须由本 crate 的 `alloc_buf` 返回，`len` 必须与当时返回的一致。
pub unsafe fn free_buf(ptr: *mut u8, len: usize) {
    if ptr.is_null() {
        return;
    }
    if len == 0 {
        let layout = std::alloc::Layout::new::<u8>();
        unsafe { std::alloc::dealloc(ptr, layout) };
        return;
    }
    let layout = std::alloc::Layout::from_size_align(len, 1).expect("layout");
    unsafe { std::alloc::dealloc(ptr, layout) };
}

// ── plugin_entry! 宏 ────────────────────────────────────────────────

/// 注册插件入口。作者传入一个闭包 `Fn(PluginInput) -> PluginResult`，
/// 宏生成 `plugin_execute` + `plugin_free` 两个 `#[no_mangle] extern "C"` 符号。
///
/// # Example
/// ```ignore
/// use agentos_native_sdk::{plugin_entry, PluginInput, PluginResult};
///
/// plugin_entry!(|input: PluginInput| -> PluginResult {
///     let mut updates = std::collections::HashMap::new();
///     updates.insert("greeting".to_string(), serde_json::json!("hello"));
///     PluginResult::ok().with_state_updates(updates)
/// });
/// ```
#[macro_export]
macro_rules! plugin_entry {
    ($handler:expr) => {
        /// 插件执行入口（C-ABI）。详见 crate 顶部文档。
        #[no_mangle]
        pub extern "C" fn plugin_execute(
            input_ptr: *const u8,
            input_len: usize,
            out_ptr: *mut *mut u8,
            out_len: *mut usize,
        ) -> i32 {
            $crate::plugin_entry_inner!(input_ptr, input_len, out_ptr, out_len, $handler)
        }

        /// 释放插件分配的缓冲区（C-ABI）。
        #[no_mangle]
        pub extern "C" fn plugin_free(ptr: *mut u8, len: usize) {
            unsafe { $crate::free_buf(ptr, len) }
        }
    };
}

/// 内部：宏实现体（避免污染插件作者命名空间）。
///
/// 逻辑：读输入 JSON → 反序列化 PluginInput → 调 handler → 序列化 PluginResult → 写输出。
/// 任何步骤失败返回 -1（致命），handler 正常返回 0，handler panic 被捕获返回 -1。
#[doc(hidden)]
#[macro_export]
macro_rules! plugin_entry_inner {
    ($input_ptr:expr, $input_len:expr, $out_ptr:expr, $out_len:expr, $handler:expr) => {{
        use std::panic;

        // 读输入
        let input_bytes: Vec<u8> = if $input_ptr.is_null() || $input_len == 0 {
            Vec::new()
        } else {
            unsafe { std::slice::from_raw_parts($input_ptr, $input_len) }.to_vec()
        };

        let parsed_input: $crate::PluginInput = if input_bytes.is_empty() {
            $crate::PluginInput::default()
        } else {
            match serde_json::from_slice(&input_bytes) {
                Ok(v) => v,
                Err(_) => return -1,
            }
        };

        // 调用 handler（捕获 panic，防拖垮）
        let result: $crate::PluginResult = match panic::catch_unwind(panic::AssertUnwindSafe(
            || $handler(parsed_input),
        )) {
            Ok(r) => r,
            Err(_) => return -1,
        };

        // 序列化输出
        let out_bytes: Vec<u8> = match serde_json::to_vec(&result) {
            Ok(b) => b,
            Err(_) => return -1,
        };

        let (ptr, len) = $crate::alloc_buf_public(&out_bytes);
        if ptr.is_null() {
            return -1;
        }
        unsafe {
            *$out_ptr = ptr;
            *$out_len = len;
        }
        0
    }};
}

/// 公开别名（供宏调用），指向内部 `alloc_buf`。
#[doc(hidden)]
pub fn alloc_buf_public(bytes: &[u8]) -> (*mut u8, usize) {
    alloc_buf(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plugin_result_ok_is_empty() {
        let r = PluginResult::ok();
        assert!(r.state_updates.is_empty());
        assert!(r.error.is_none());
    }

    #[test]
    fn plugin_result_error_carries_code() {
        let r = PluginResult::error("boom", "E_X");
        assert_eq!(r.error.as_ref().unwrap().code.as_deref(), Some("E_X"));
        assert_eq!(r.error.as_ref().unwrap().message, "boom");
    }

    #[test]
    fn plugin_input_roundtrip() {
        let mut input = PluginInput::new();
        input.tenant_id = "t1".to_string();
        input.state = serde_json::json!({"k": 1});
        let s = serde_json::to_vec(&input).unwrap();
        let back: PluginInput = serde_json::from_slice(&s).unwrap();
        assert_eq!(back.tenant_id, "t1");
        assert_eq!(back.state["k"], 1);
    }

    #[test]
    fn alloc_free_buf_roundtrip() {
        let data = b"hello world buffer";
        let (ptr, len) = alloc_buf(data);
        assert_eq!(len, data.len());
        assert!(!ptr.is_null());
        let read_back = unsafe { std::slice::from_raw_parts(ptr, len) };
        assert_eq!(read_back, data);
        unsafe { free_buf(ptr, len) };
    }

    #[test]
    fn alloc_free_empty_buf() {
        let (ptr, len) = alloc_buf(&[]);
        assert_eq!(len, 0);
        unsafe { free_buf(ptr, 0) };
    }
}

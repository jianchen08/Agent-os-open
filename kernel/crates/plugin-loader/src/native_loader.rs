//! # 原生插件加载器（InProcess 执行器）
//!
//! task_11（N2）：用 libloading 加载 Rust cdylib 插件，通过 C-ABI 入口调用。
//!
//! 设计依据：《原生与WASM插件执行器实现计划》§3.2。
//!
//! ## C-ABI 契约（与 `agentos-native-sdk` 共享）
//!
//! 插件导出：
//! ```text
//! plugin_execute(input_ptr: *const u8, input_len: usize,
//!                 out_ptr: *mut *mut u8, out_len: *mut usize) -> i32
//! plugin_free(ptr: *mut u8, len: usize)
//! ```
//! - 输入/输出都是 JSON 字符串（PluginInput / PluginResult 序列化）。
//! - 返回码：0 = 成功，1 = 业务错误（out 仍是合法 PluginResult），-1 = 致命错误。
//! - 输出缓冲区由插件分配，内核调 `plugin_free` 释放（跨分配器安全）。
//!
//! ## 安全要点
//! - `catch_unwind` 包裹 FFI 调用，插件 panic 不崩内核（但仍可能 UB，插件需自检）。
//! - Library 句柄常驻 Loader 生命周期；**生产不做原地热卸载**（Windows dlclose 坑）。
//! - `NativePlugin::entry` / `free` 是函数指针（Copy），从 Symbol 拷贝出来，
//!   Symbol 本身仅在 load 期间存在——避免 `'static` 借用诡计。

use std::collections::HashMap;
use std::ffi::OsStr;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use agentos_core::types::{PluginError, PluginResult};
use libloading::{Library, Symbol};
use parking_lot::RwLock;
use serde_json::Value;
use tracing::{debug, warn};

/// C-ABI 入口签名（与 SDK `plugin_execute` 对齐）。
type ExecuteFn = unsafe extern "C" fn(*const u8, usize, *mut *mut u8, *mut usize) -> i32;
/// C-ABI 释放签名（与 SDK `plugin_free` 对齐）。
type FreeFn = unsafe extern "C" fn(*mut u8, usize);

/// 默认入口符号名（manifest.invoke_entry 未声明或为字面量时使用）。
pub const DEFAULT_ENTRY_SYMBOL: &[u8] = b"plugin_execute";
/// 默认释放符号名。
pub const DEFAULT_FREE_SYMBOL: &[u8] = b"plugin_free";

/// 一个已加载的原生插件实例（Library + 函数指针）。
#[derive(Debug)]
pub struct NativePlugin {
    /// Library 句柄——保活，函数指针指向其内代码。
    /// 永不单独释放（生产不做热卸载）。
    _lib: Library,
    /// 入口函数指针。
    entry: ExecuteFn,
    /// 释放函数指针。
    free: FreeFn,
    /// 加载来源路径（调试/日志用）。
    path: PathBuf,
}

/// 原生插件加载器：管理 cdylib 句柄，按 plugin_id 分发调用。
///
/// 线程安全（内部 RwLock）。多线程并发调用同一插件安全（函数指针无状态）。
pub struct NativePluginLoader {
    /// plugin_id → 已加载插件。
    loaded: RwLock<HashMap<String, Arc<NativePlugin>>>,
}

impl Default for NativePluginLoader {
    fn default() -> Self {
        Self::new()
    }
}

impl NativePluginLoader {
    /// 创建空加载器。
    pub fn new() -> Self {
        Self {
            loaded: RwLock::new(HashMap::new()),
        }
    }

    /// 加载（或复用已加载的）原生插件。
    ///
    /// `path` 指向 cdylib 文件；`entry_symbol` / `free_symbol` 可覆盖默认符号名
    /// （来自 manifest.invoke_entry，默认 `plugin_execute` / `plugin_free`）。
    /// 已加载同 plugin_id 则覆盖（重载新文件，旧 Library drop）。
    pub fn load(
        &self,
        plugin_id: &str,
        path: &Path,
        entry_symbol: Option<&[u8]>,
        free_symbol: Option<&[u8]>,
    ) -> Result<Arc<NativePlugin>, PluginError> {
        // 先检查缓存
        if let Some(p) = self.loaded.read().get(plugin_id) {
            if p.path == path {
                return Ok(Arc::clone(p));
            }
        }
        let plugin = Self::load_inner(path, entry_symbol, free_symbol)?;
        let arc = Arc::new(plugin);
        self.loaded.write().insert(plugin_id.to_string(), Arc::clone(&arc));
        debug!(plugin_id = plugin_id, path = ?path, "Native plugin loaded");
        Ok(arc)
    }

    fn load_inner(
        path: &Path,
        entry_symbol: Option<&[u8]>,
        free_symbol: Option<&[u8]>,
    ) -> Result<NativePlugin, PluginError> {
        let lib = unsafe { Library::new(path) }.map_err(|e| PluginError {
            message: format!("native plugin load failed ({}): {}", path.display(), e),
            code: Some("NATIVE_LOAD_FAILED".to_string()),
            source: Some("native-loader".to_string()),
        })?;

        let entry_sym = entry_symbol.unwrap_or(DEFAULT_ENTRY_SYMBOL);
        let free_sym = free_symbol.unwrap_or(DEFAULT_FREE_SYMBOL);

        // 安全性：符号在加载期借用 lib，拷出函数指针后立即丢弃 Symbol。
        // 只要 _lib 保活，函数指针有效（我们永不单独释放 lib）。
        let entry: ExecuteFn = unsafe {
            let sym: Symbol<ExecuteFn> = lib
                .get(entry_sym)
                .map_err(|e| PluginError {
                    message: format!("symbol {} not found: {}", String::from_utf8_lossy(entry_sym), e),
                    code: Some("NATIVE_SYMBOL_MISSING".to_string()),
                    source: Some("native-loader".to_string()),
                })?;
            *sym
        };
        let free: FreeFn = unsafe {
            let sym: Symbol<FreeFn> = lib
                .get(free_sym)
                .map_err(|e| PluginError {
                    message: format!("symbol {} not found: {}", String::from_utf8_lossy(free_sym), e),
                    code: Some("NATIVE_SYMBOL_MISSING".to_string()),
                    source: Some("native-loader".to_string()),
                })?;
            *sym
        };

        Ok(NativePlugin {
            _lib: lib,
            entry,
            free,
            path: path.to_path_buf(),
        })
    }

    /// 调用已加载的插件。输入经 JSON 序列化传给插件，输出反序列化为 PluginResult。
    ///
    /// `catch_unwind` 包裹 FFI 调用——插件 panic 不拖垮内核（仍可能 UB，但至少不崩）。
    pub fn invoke(
        &self,
        plugin_id: &str,
        input: &Value,
    ) -> Result<PluginResult, PluginError> {
        let plugin = {
            let loaded = self.loaded.read();
            loaded
                .get(plugin_id)
                .map(Arc::clone)
                .ok_or_else(|| PluginError {
                    message: format!("native plugin not loaded: {}", plugin_id),
                    code: Some("NATIVE_NOT_LOADED".to_string()),
                    source: Some("native-loader".to_string()),
                })?
        };

        let input_bytes = serde_json::to_vec(input).map_err(|e| PluginError {
            message: format!("input serialize failed: {}", e),
            code: Some("NATIVE_INPUT_SERIALIZE".to_string()),
            source: Some("native-loader".to_string()),
        })?;

        // catch_unwind 防 panic 越界到内核栈
        let raw = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            Self::call_entry(&plugin, &input_bytes)
        }))
        .map_err(|_| PluginError {
            message: format!("native plugin '{}' panicked during invoke", plugin_id),
            code: Some("NATIVE_PLUGIN_PANICKED".to_string()),
            source: Some("native-loader".to_string()),
        })??;

        let result: PluginResult = serde_json::from_slice(&raw).map_err(|e| PluginError {
            message: format!("native plugin output parse failed: {}", e),
            code: Some("NATIVE_OUTPUT_PARSE".to_string()),
            source: Some("native-loader".to_string()),
        })?;

        Ok(result)
    }

    /// 执行单次 C-ABI 调用，返回输出字节（已用 plugin_free 释放原缓冲区）。
    fn call_entry(plugin: &NativePlugin, input: &[u8]) -> Result<Vec<u8>, PluginError> {
        let mut out_ptr: *mut u8 = std::ptr::null_mut();
        let mut out_len: usize = 0;
        let (ip, il) = if input.is_empty() {
            (std::ptr::null(), 0usize)
        } else {
            (input.as_ptr(), input.len())
        };

        // 安全性：插件按契约写出 out_ptr/out_len。返回码 < 0 视为致命错误。
        let rc = unsafe { (plugin.entry)(ip, il, &mut out_ptr as *mut *mut u8, &mut out_len) };

        if rc < 0 {
            // 致命错误：插件可能未分配 out_ptr，但仍保险释放一下（null 无操作）。
            unsafe { (plugin.free)(out_ptr, out_len) };
            return Err(PluginError {
                message: format!("native plugin fatal error (rc={})", rc),
                code: Some("NATIVE_FATAL".to_string()),
                source: Some("native-loader".to_string()),
            });
        }

        if out_ptr.is_null() {
            // 无输出（合法的空结果情况）
            return Ok(Vec::new());
        }

        // 拷贝出输出字节，然后让插件释放原缓冲区。
        let bytes = unsafe { std::slice::from_raw_parts(out_ptr, out_len) }.to_vec();
        unsafe { (plugin.free)(out_ptr, out_len) };

        if rc != 0 {
            // 业务错误：out 仍是合法 PluginResult（含 error 字段）——解析后返回。
            warn!(rc = rc, "native plugin returned business error code");
        }
        Ok(bytes)
    }

    /// 卸载指定插件（释放 Library）。
    ///
    /// **注意**：Windows 下 dlclose 有已知坑（计划文档 §3.2），生产环境慎用。
    /// 此处从表移除，Arc 引用计数归零时真正 drop。
    pub fn unload(&self, plugin_id: &str) -> Result<(), PluginError> {
        match self.loaded.write().remove(plugin_id) {
            Some(_) => {
                debug!(plugin_id = plugin_id, "Native plugin unloaded");
                Ok(())
            }
            None => Err(PluginError {
                message: format!("native plugin not loaded (cannot unload): {}", plugin_id),
                code: Some("NATIVE_NOT_LOADED".to_string()),
                source: Some("native-loader".to_string()),
            }),
        }
    }

    /// 查询插件是否已加载。
    pub fn is_loaded(&self, plugin_id: &str) -> bool {
        self.loaded.read().contains_key(plugin_id)
    }

    /// 列出已加载插件 ID（调试用）。
    #[allow(dead_code)]
    pub fn list_loaded(&self) -> Vec<String> {
        self.loaded.read().keys().cloned().collect()
    }

    /// 按平台约定补全 cdylib 文件名前缀/后缀。
    ///
    /// 若 `artifact` 已带后缀（.dll/.so/.dylib）则原样返回；否则按平台补：
    /// - Windows: `{}.dll`
    /// - macOS: `lib{}.dylib`
    /// - Linux/Unix: `lib{}.so`
    pub fn platform_artifact_name(artifact: &str) -> String {
        let has_ext = [".dll", ".so", ".dylib"]
            .iter()
            .any(|e| artifact.to_lowercase().ends_with(e));
        if has_ext {
            return artifact.to_string();
        }
        if cfg!(windows) {
            format!("{}.dll", artifact)
        } else if cfg!(target_os = "macos") {
            format!("lib{}.dylib", artifact)
        } else {
            format!("lib{}.so", artifact)
        }
    }
}

// OsStr 兼容（路径操作工具，未来扩展用）
#[allow(dead_code)]
fn _osstr(_s: &OsStr) {}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// 平台文件名补全：已带后缀的原样返回。
    #[test]
    fn platform_artifact_keeps_existing_ext() {
        assert_eq!(NativePluginLoader::platform_artifact_name("foo.dll"), "foo.dll");
        assert_eq!(NativePluginLoader::platform_artifact_name("libfoo.so"), "libfoo.so");
    }

    /// 平台文件名补全：无后缀按平台加。
    #[test]
    fn platform_artifact_adds_ext() {
        let name = NativePluginLoader::platform_artifact_name("foo");
        if cfg!(windows) {
            assert_eq!(name, "foo.dll");
        } else if cfg!(target_os = "macos") {
            assert_eq!(name, "libfoo.dylib");
        } else {
            assert_eq!(name, "libfoo.so");
        }
    }

    /// 未加载的插件 invoke 返回 NATIVE_NOT_LOADED。
    #[test]
    fn invoke_not_loaded_errors() {
        let loader = NativePluginLoader::new();
        let err = loader.invoke("nope", &json!({})).unwrap_err();
        assert_eq!(err.code.as_deref(), Some("NATIVE_NOT_LOADED"));
    }

    /// 文件不存在时 load 返回 NATIVE_LOAD_FAILED。
    #[test]
    fn load_missing_file_errors() {
        let loader = NativePluginLoader::new();
        let err = loader
            .load("p", Path::new("/nonexistent/does_not_exist_xyz.dll"), None, None)
            .unwrap_err();
        assert_eq!(err.code.as_deref(), Some("NATIVE_LOAD_FAILED"));
    }
}

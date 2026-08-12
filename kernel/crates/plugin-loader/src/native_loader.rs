//! # 原生插件加载器（InProcess，直接 trait 对象）
//!
//! 用 libloading dlopen 加载 Rust cdylib 插件，拿构造函数符号
//! `agentos_plugin_create`，调用得到 `Box<dyn PipelinePlugin>` 裸指针，还原为
//! trait 对象后直接调 `execute()`——无 abi_stable 运行时校验（避开其 Windows
//! release ABI 坑），依赖 toolchain 锁定保证 vtable 一致。
//!
//! ## 契约
//!
//! 插件 cdylib export `extern "C" fn agentos_plugin_create() -> *mut ()`，
//! 返回 `plugin_into_raw(impl)` 的双重 Box 裸指针（见 native-sdk）。
//!
//! ## 安全要点
//!
//! - unsafe 仅在 dlopen + 指针还原（loader 内部），插件作者代码零 unsafe。
//! - Library 句柄常驻 loader 生命周期；**生产不做原地热卸载**（Windows dlclose 坑）。
//! - `catch_unwind` 包裹 execute，插件 panic 不拖垮内核（panic=abort 时直接终止进程，
//!   那是预期行为——cdylib panic=abort 是跨边界标准做法）。

use std::collections::HashMap;
use std::ffi::OsStr;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use agentos_core::types::PluginError;
use agentos_native_sdk::{box_from_raw, HostServices, PipelinePlugin, PluginCtx, CREATE_FN_NAME};
use libloading::{Library, Symbol};
use parking_lot::RwLock;
use tracing::{debug, warn};

/// 构造函数签名：返回双重 Box 裸指针（实为 `Box<Box<dyn PipelinePlugin>>`）。
type CreateFn = unsafe extern "C" fn() -> *mut ();

/// 一个已加载的原生插件实例：Library（保活）+ trait 对象。
pub struct NativePlugin {
    /// Library 句柄——保活，trait 对象的代码指向其内。
    /// 永不单独释放（生产不做热卸载）。
    _lib: Library,
    /// 插件构造出的 trait 对象。
    instance: Box<dyn PipelinePlugin>,
    /// 加载来源路径（调试/日志用）。
    path: PathBuf,
}

/// 原生插件加载器：管理 cdylib 句柄，按 plugin_id 分发调用。
///
/// 线程安全（内部 RwLock）。trait 对象 `execute(&self)` 不可变，多线程并发调用安全。
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
    pub fn load(&self, plugin_id: &str, path: &Path) -> Result<Arc<NativePlugin>, PluginError> {
        // 先检查缓存
        if let Some(p) = self.loaded.read().get(plugin_id) {
            if p.path == path {
                return Ok(Arc::clone(p));
            }
        }
        let plugin = Self::load_inner(path)?;
        let arc = Arc::new(plugin);
        self.loaded
            .write()
            .insert(plugin_id.to_string(), Arc::clone(&arc));
        debug!(plugin_id = plugin_id, path = ?path, "Native plugin loaded (direct trait object)");
        Ok(arc)
    }

    fn load_inner(path: &Path) -> Result<NativePlugin, PluginError> {
        // SAFETY: `path` 由 loader 校验为插件目录下的 cdylib（plugin.json host_type=native 指定）。
        // dlopen 会执行库的构造段——这是动态插件加载的固有契约；Library 句柄存入
        // NativePlugin._lib 随插件生命周期持有，drop 时 dlclose，不会提前卸载。
        let lib = unsafe { Library::new(path) }.map_err(|e| PluginError {
            message: format!("native plugin load failed ({}): {}", path.display(), e),
            code: Some("NATIVE_LOAD_FAILED".to_string()),
            source: Some("native-loader".to_string()),
        })?;

        // SAFETY: lib.get 要求符号的函数签名与 CreateFn 一致。CREATE_FN_NAME
        // (b"plugin_entry") 是 native-sdk 约定的 C ABI 导出（extern "C"），签名固定为
        // `unsafe extern "C" fn() -> *mut ()`，C ABI 无名称修饰，类型匹配成立。
        let create_fn: CreateFn = unsafe {
            let sym: Symbol<CreateFn> = lib.get(CREATE_FN_NAME).map_err(|e| PluginError {
                message: format!(
                    "symbol {} not found: {}",
                    String::from_utf8_lossy(CREATE_FN_NAME),
                    e
                ),
                code: Some("NATIVE_SYMBOL_MISSING".to_string()),
                source: Some("native-loader".to_string()),
            })?;
            *sym
        };

        // 调构造函数拿裸指针，还原为 Box<dyn PipelinePlugin>。
        // SAFETY: create_fn 由插件 cdylib 导出，返回 plugin_into_raw 产生的双重 Box 指针。
        let ptr = unsafe { create_fn() };
        // SAFETY: ptr 由 create_fn()（插件 plugin_entry 导出）产生，按 native-sdk 契约
        // 必须是 plugin_into_raw 生成的双重 Box 指针；box_from_raw 还原所有权并转移给 loader。
        // null 已由 box_from_raw 内部处理（返回 None → 下游 NATIVE_CREATE_NULL 错误）。
        let instance: Box<dyn PipelinePlugin> = unsafe { box_from_raw(ptr) }.ok_or_else(|| PluginError {
            message: format!("native plugin create returned null pointer: {}", path.display()),
            code: Some("NATIVE_CREATE_NULL".to_string()),
            source: Some("native-loader".to_string()),
        })?;

        Ok(NativePlugin {
            _lib: lib,
            instance,
            path: path.to_path_buf(),
        })
    }

    /// 调用插件的 execute（直接 trait 对象派发）。
    ///
    /// 返回 state_updates 的 JSON 字符串。`host` 为 None 时插件降级（不调 capability）。
    pub fn execute(
        &self,
        plugin_id: &str,
        ctx: &PluginCtx,
        host: Option<&dyn HostServices>,
    ) -> Result<String, PluginError> {
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

        // 构造执行上下文（ctx + host），调 trait 对象 execute。
        let ectx = agentos_native_sdk::ExecContext {
            ctx: ctx.clone(),
            host,
        };
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            plugin.instance.execute(&ectx)
        }))
        .map_err(|_| PluginError {
            message: format!("native plugin '{}' panicked during execute", plugin_id),
            code: Some("NATIVE_PLUGIN_PANICKED".to_string()),
            source: Some("native-loader".to_string()),
        })?;

        match result {
            Ok(state_updates_json) => Ok(state_updates_json),
            Err(err) => {
                warn!(plugin_id = plugin_id, error = %err, "native plugin returned error");
                Err(PluginError {
                    message: err,
                    code: Some("NATIVE_PLUGIN_ERROR".to_string()),
                    source: Some("native-loader".to_string()),
                })
            }
        }
    }

    /// 卸载指定插件（从表移除，Arc 引用计数归零时 drop）。
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

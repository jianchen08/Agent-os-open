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
//! - **永不 dlclose**：Library 句柄以 `ManuallyDrop` 持有，drop 阶段不释放——
//!   Windows 上 `FreeLibrary` 卸载带 Rust 静态的 cdylib 会触发静态析构顺序错位，
//!   产生 `STATUS_ACCESS_VIOLATION`（e2e 实测：测试 teardown drop NativePlugin →
//!   `_lib` 释放 → 0xc0000005；`mem::forget` 验证后全绿）。句柄随进程退出由 OS 回收。
//!   生产本就是进程级单例永不 drop；此设计让测试也遵守同一"不热卸载"契约，跨平台统一。
//! - `catch_unwind` 包裹 execute，插件 panic 不拖垮内核（panic=abort 时直接终止进程，
//!   那是预期行为——cdylib panic=abort 是跨边界标准做法）。

use std::collections::HashMap;
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
    /// Library 句柄——以 ManuallyDrop 持有，**drop 阶段刻意不释放**（见模块注释：
    /// Windows 上 dlclose 带 Rust 静态的 cdylib 会 AV）。进程退出时由 OS 回收。
    #[allow(dead_code)]
    _lib: std::mem::ManuallyDrop<Library>,
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
        // (b"agentos_plugin_create") 是 native-sdk 约定的 C ABI 导出（extern "C"），签名固定为
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
        // SAFETY: ptr 由 create_fn()（插件 agentos_plugin_create 导出）产生，按 native-sdk 契约
        // 必须是 plugin_into_raw 生成的双重 Box 指针；box_from_raw 还原所有权并转移给 loader。
        // null 已由 box_from_raw 内部处理（返回 None → 下游 NATIVE_CREATE_NULL 错误）。
        let instance: Box<dyn PipelinePlugin> =
            unsafe { box_from_raw(ptr) }.ok_or_else(|| PluginError {
                message: format!(
                    "native plugin create returned null pointer: {}",
                    path.display()
                ),
                code: Some("NATIVE_CREATE_NULL".to_string()),
                source: Some("native-loader".to_string()),
            })?;

        Ok(NativePlugin {
            _lib: std::mem::ManuallyDrop::new(lib),
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

    /// 卸载指定插件（从表移除，Arc 引用计数归零时 drop；视为"逻辑卸载"——
    /// cdylib 句柄按契约泄漏保活，物理卸载随进程退出由 OS 完成）。
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

    /// 解析产物实际路径：声明名（含已带后缀）原样优先，磁盘缺失时按**本平台**
    /// 重映射回退（manifest 常见写死 `.dll`，Linux 产物是 `lib{}.so`——跨平台
    /// 声明兼容，CI/Linux boot 必需）。两处消费（loader 预检 + invoker 实加载）
    /// 同规则。仍未命中返回 None（调用方各自报错）。
    pub fn resolve_artifact(dir: &std::path::Path, artifact: &str) -> Option<std::path::PathBuf> {
        let primary = dir.join(Self::platform_artifact_name(artifact));
        if primary.exists() {
            return Some(primary);
        }
        let lower = artifact.to_lowercase();
        let stem = [".dll", ".so", ".dylib"]
            .iter()
            .find_map(|e| lower.strip_suffix(e))
            .unwrap_or(artifact);
        let mapped = dir.join(Self::platform_artifact_name(stem));
        if mapped.exists() {
            return Some(mapped);
        }
        None
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolve_artifact_falls_back_cross_platform() {
        let dir = std::env::temp_dir().join("agentos_resolve_artifact_test");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        // 只放本平台产物名（Linux=libx.so / Windows=x.dll）——声明却写死异平台后缀
        let local = NativePluginLoader::platform_artifact_name("x");
        std::fs::write(dir.join(&local), b"").unwrap();
        let got = NativePluginLoader::resolve_artifact(&dir, "x.dll");
        assert_eq!(
            got.as_ref()
                .and_then(|p| p.file_name())
                .map(|n| n.to_string_lossy().to_string()),
            Some(local.clone())
        );
        // 声明即本平台名 → 原样命中
        assert_eq!(
            NativePluginLoader::resolve_artifact(&dir, &local)
                .as_ref()
                .and_then(|p| p.file_name())
                .map(|n| n.to_string_lossy().to_string()),
            Some(local.clone())
        );
        // 两者皆无 → None
        assert_eq!(NativePluginLoader::resolve_artifact(&dir, "y.dll"), None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    fn platform_artifact_name_keeps_known_extensions() {
        assert_eq!(NativePluginLoader::platform_artifact_name("p.dll"), "p.dll");
        assert_eq!(
            NativePluginLoader::platform_artifact_name("P.SO"),
            "P.SO",
            "大小写后缀保留"
        );
        assert_eq!(
            NativePluginLoader::platform_artifact_name("x.dylib"),
            "x.dylib"
        );
        assert_eq!(
            NativePluginLoader::platform_artifact_name("liby.so"),
            "liby.so"
        );
    }

    #[test]
    fn platform_artifact_name_appends_platform_suffix_for_bare_names() {
        let name = NativePluginLoader::platform_artifact_name("my_plugin");
        if cfg!(windows) {
            assert_eq!(name, "my_plugin.dll");
        } else if cfg!(target_os = "macos") {
            assert_eq!(name, "libmy_plugin.dylib");
        } else {
            assert_eq!(name, "libmy_plugin.so");
        }
    }

    #[test]
    fn load_nonexistent_path_returns_native_load_failed() {
        let loader = NativePluginLoader::new();
        let err = match loader.load("ghost", Path::new("C:/definitely/not/exists/ghost.dll")) {
            Ok(_) => panic!("load should fail"),
            Err(e) => e,
        };
        assert_eq!(err.code.as_deref(), Some("NATIVE_LOAD_FAILED"));
        assert!(!loader.is_loaded("ghost"));
    }

    #[test]
    fn load_non_dll_file_returns_native_load_failed() {
        // 把文本文件当 cdylib 加载 → 系统加载器拒绝 → NATIVE_LOAD_FAILED（不 panic）。
        let tmp = tempfile::tempdir().unwrap();
        let fake = tmp.path().join("fake.dll");
        std::fs::write(&fake, b"this is not a dll").unwrap();
        let loader = NativePluginLoader::new();
        let err = match loader.load("fake", &fake) {
            Ok(_) => panic!("load should fail"),
            Err(e) => e,
        };
        assert_eq!(err.code.as_deref(), Some("NATIVE_LOAD_FAILED"));
    }

    #[test]
    fn execute_not_loaded_returns_native_not_loaded() {
        let loader = NativePluginLoader::new();
        let ctx = PluginCtx {
            state_json: "{}".to_string(),
            config_json: "{}".to_string(),
            tenant_id: "t1".to_string(),
            session_id: "s1".to_string(),
            task_id: "task1".to_string(),
            pipeline_id: "p1".to_string(),
            tool_call_json: None,
        };
        let err = loader.execute("never_loaded", &ctx, None).unwrap_err();
        assert_eq!(err.code.as_deref(), Some("NATIVE_NOT_LOADED"));
    }

    #[test]
    fn unload_not_loaded_returns_error() {
        let loader = NativePluginLoader::new();
        let err = loader.unload("never_loaded").unwrap_err();
        assert_eq!(err.code.as_deref(), Some("NATIVE_NOT_LOADED"));
    }

    #[test]
    fn new_loader_starts_empty() {
        let loader = NativePluginLoader::new();
        assert!(loader.list_loaded().is_empty());
        assert!(!loader.is_loaded("anything"));
        let default = NativePluginLoader::default();
        assert!(default.list_loaded().is_empty());
    }
}

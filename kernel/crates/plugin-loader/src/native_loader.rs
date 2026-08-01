//! # 原生插件加载器（InProcess，abi_stable trait 对象）
//!
//! 用 abi_stable 加载 Rust cdylib 插件，拿到 FFI-safe 的 `PipelinePlugin` trait
//! 对象，直接调 `execute()`——零 C-ABI 手搓、零 JSON 序列化中间层。
//!
//! ## 加载契约
//!
//! 插件 cdylib 用 `#[export_root_module]` 导出 `NativePluginModule`（见
//! `agentos-native-sdk`）。本 loader 经 abi_stable 的 `RootModule::load_root_module`
//! 加载，校验类型布局（跨 rustc 版本安全），拿 `NativePluginModule_Ref`，调
//! `create_plugin()` 得到 `PipelinePlugin_TO` trait 对象。
//!
//! ## 安全要点
//!
//! - abi_stable 在加载期校验类型布局（比裸 C-ABI 更安全：版本不匹配立即报错而非 UB）。
//! - Library 句柄常驻 loader 生命周期；**生产不做原地热卸载**（Windows dlclose 坑）。
//! - `load_root_module` 内部含 unsafe dlopen，由 abi_stable 封装。

use std::collections::HashMap;
use std::ffi::OsStr;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use agentos_core::types::PluginError;
use agentos_native_sdk::{NativePluginModule_Ref, PipelinePlugin_TO};
use parking_lot::RwLock;
use tracing::{debug, warn};

use abi_stable::library::RootModule;
use abi_stable::std_types::{RBox, RResult, RString};

/// 一个已加载的原生插件实例：RootModule + 构造出的 trait 对象。
pub struct NativePlugin {
    /// RootModule 引用（保活 abi_stable 加载的库元数据）。
    #[allow(dead_code)]
    root: NativePluginModule_Ref,
    /// 插件构造出的 trait 对象（调 execute 用）。
    instance: PipelinePlugin_TO<'static, RBox<()>>,
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
    ///
    /// `path` 指向 cdylib 文件。已加载同 plugin_id 且同 path 则复用缓存。
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
        debug!(plugin_id = plugin_id, path = ?path, "Native plugin loaded (abi_stable)");
        Ok(arc)
    }

    fn load_inner(path: &Path) -> Result<NativePlugin, PluginError> {
        // abi_stable 加载 root module：校验类型布局 + 拿模块引用。
        let root = NativePluginModule_Ref::load_from_file(path).map_err(|e| PluginError {
            message: format!("native plugin load failed ({}): {:?}", path.display(), e),
            code: Some("NATIVE_LOAD_FAILED".to_string()),
            source: Some("native-loader".to_string()),
        })?;

        // 调构造函数拿 trait 对象。create_plugin() 返回函数指针，需再调一次。
        let instance: PipelinePlugin_TO<'static, RBox<()>> = (root.create_plugin())();

        Ok(NativePlugin {
            root,
            instance,
            path: path.to_path_buf(),
        })
    }

    /// 调用插件的 execute（直接 trait 对象派发，无 JSON 序列化中间层）。
    ///
    /// 返回 state_updates 的 JSON 字符串（插件侧序列化）。
    /// `state_json` / `config_json` 由调用方（invoker）准备。
    /// `host` 为 None 时插件降级（不调 capability）。
    pub fn execute(
        &self,
        plugin_id: &str,
        state_json: &str,
        config_json: &str,
        tenant_id: &str,
        session_id: &str,
        task_id: &str,
        pipeline_id: &str,
        host: Option<agentos_native_sdk::HostServicesBox>,
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

        let ctx = agentos_native_sdk::PluginCtx {
            state_json: RString::from(state_json),
            config_json: RString::from(config_json),
            tenant_id: RString::from(tenant_id),
            session_id: RString::from(session_id),
            task_id: RString::from(task_id),
            pipeline_id: RString::from(pipeline_id),
            host: match host {
                Some(h) => abi_stable::std_types::ROption::RSome(h),
                None => abi_stable::std_types::ROption::RNone,
            },
        };

        // trait 对象派发——catch panic 防拖垮内核。
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            plugin.instance.execute(&ctx)
        }))
        .map_err(|_| PluginError {
            message: format!("native plugin '{}' panicked during execute", plugin_id),
            code: Some("NATIVE_PLUGIN_PANICKED".to_string()),
            source: Some("native-loader".to_string()),
        })?;

        match result {
            RResult::ROk(state_updates_json) => Ok(state_updates_json.into_string()),
            RResult::RErr(err) => {
                warn!(plugin_id = plugin_id, error = %err.as_str(), "native plugin returned error");
                Err(PluginError {
                    message: err.into_string(),
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
    ///
    /// 若 `artifact` 已带后缀（.dll/.so/.dylib）则原样返回；否则按平台补。
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

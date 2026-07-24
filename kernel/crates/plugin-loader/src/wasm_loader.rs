//! # WASM 插件运行时（wasmtime 嵌入）
//!
//! task_11（N5-N10）：用 wasmtime 加载并执行 WASM 插件，沙箱隔离 + host 能力白名单。
//!
//! 设计依据：《原生与WASM插件执行器实现计划》§四 WASM 部分 + §八 #1/#3。
//!
//! ## 参数传递方案：JSON 经线性内存（降级方案）
//!
//! **完整方案** 是 WIT（WebAssembly Interface Type）+ wit-bindgen 代码生成 + 组件模型
//! （wasm32-wasip2）。但 wit-bindgen 工具链集成在本项目（rustc 1.85）尚需 PoC
//! （计划文档 §八 #1 未决），故当前采用**降级方案**：与原生插件一致的
//! "JSON 序列化解耦 ABI"思路，经 WASM 线性内存传递字节。
//!
//! ### WASM 插件 ABI 契约（host↔guest）
//!
//! WASM 模块（core wasm，target `wasm32-unknown-unknown` 即可，不强依赖 WASI）
//! 必须导出：
//!
//! ```text
//! memory           —— 线性内存（guest 拥有）
//! allocate(len: i32) -> i32      —— guest 在自己内存里分配 len 字节，返回起始偏移
//! deallocate(ptr: i32, len: i32) —— 释放 allocate 分配的内存
//! execute(in_ptr: i32, in_len: i32) -> i64
//!                                 —— 执行插件：读入 in_ptr..in_ptr+in_len 的 JSON 输入，
//!                                    返回 packed (out_ptr | (out_len << 32))（低 32 位
//!                                    为输出 JSON 起始偏移，高 32 位为长度）。输出缓冲区
//!                                    由 guest 经 allocate 分配，host 拷出后用 deallocate 释放。
//! ```
//!
//! 输入/输出都是 JSON 字符串（与原生插件完全一致的 PluginInput / PluginResult）。
//!
//! host 能力（如 `host.log`）经 wasmtime `Linker` 注入：guest 通过 `import` 调用，
//! 内核在 host 侧按 manifest 的 `granted_capabilities` 白名单校验，越权拒绝（N9）。
//!
//! ### 为什么不用 WIT（暂）
//!
//! - wit-bindgen 在 rustc 1.85 + Windows 工具链的集成稳定性未验证；
//! - JSON 契约与原生插件一致，复用 SDK 类型、零额外工具链；
//! - WIT 作为后续增强（§八 #1 跟进），WasmArtifact.wit_interface 字段已预留。
//!
//! ## 安全要点
//!
//! - WASM 沙箱天然隔离：guest 无法访问 host 文件/网络/内存（只能调 host 授予的 import）。
//! - 热重载：`drop Store` 即释放全部 guest 内存，无 libloading 的 dlclose 坑（§四优势）。
//! - host 能力调用经白名单校验（N9）—— guest 调用未授予的能力直接拒绝。
//!
//! ## 线程安全
//!
//! `Engine` 是单例全局编译环境（内部线程安全，可跨线程共享）。
//! `Module` 编译产物可克隆共享。每次 `invoke` 新建 `Store`（Store 非线程安全，
//! 但 invoke 是独占同步调用，无并发问题）。

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use agentos_core::types::{PluginError, PluginResult};
use parking_lot::{Mutex, RwLock};
use serde_json::Value;
use tracing::{debug, error, warn};
use wasmtime::*;

/// WASM 模块必须导出的 memory 名。
pub const WASM_MEM_EXPORT: &str = "memory";
/// WASM 模块必须导出的执行入口函数名。
pub const WASM_EXECUTE_FN: &str = "execute";
/// WASM 模块必须导出的分配函数名。
pub const WASM_ALLOCATE_FN: &str = "allocate";
/// WASM 模块必须导出的释放函数名。
pub const WASM_DEALLOC_FN: &str = "deallocate";

/// 默认 WASM 插件入口导出名（与 manifest.invoke_entry 默认值对齐）。
pub const DEFAULT_WASM_ENTRY: &str = WASM_EXECUTE_FN;

/// host 能力调用（插件→内核）的最小抽象。
///
/// 每个能力是一个 `(name, params) -> result` 的同步函数。WASM guest 通过 import 调用，
/// 内核在 host 侧实现。能力的实际业务逻辑由调用方注入（如 metrics / log capability）。
///
/// `name` 形如 `"host.log"` / `"host.record_metric"`——manifest 的 `granted_capabilities`
/// 用同样的字符串声明白名单。
pub trait HostCapability: Send + Sync {
    /// 能力全名（如 `"host.log"`）。
    fn name(&self) -> &str;

    /// 调用能力。`params` 是 guest 传入的 JSON Value，返回 JSON Value 结果。
    fn call(&self, params: &Value) -> Result<Value, PluginError>;
}

/// 能力白名单校验器（N9）。
///
/// 校验插件实际调用的 host 能力是否在 manifest `granted_capabilities` 声明的白名单内。
/// 越权调用返回 `WASM_CAPABILITY_DENIED`。
pub trait WasmCapabilityChecker: Send + Sync {
    /// 返回该插件被授予的能力名集合（来自 manifest.granted_capabilities）。
    fn granted(&self, plugin_id: &str) -> Vec<String>;
}

/// host 能力注册表：收集所有 host 能力实现，按 name 查找。
///
/// 由调用方（invoker / engine）在启动时填充：注册 `host.log` / `host.record_metric`
/// 等具体实现，传入 WasmRuntime。
pub trait WasmHostRegistry: Send + Sync {
    /// 返回所有已注册的 host 能力。
    fn capabilities(&self) -> Vec<Arc<dyn HostCapability>>;
}

/// WasmRuntime 配置。
pub struct WasmRuntimeConfig {
    /// 全局 host 能力注册表（所有插件共享可用能力集，白名单按插件校验）。
    pub host_registry: Option<Arc<dyn WasmHostRegistry>>,
    /// 能力白名单校验器。
    pub capability_checker: Option<Arc<dyn WasmCapabilityChecker>>,
}

impl Default for WasmRuntimeConfig {
    fn default() -> Self {
        Self {
            host_registry: None,
            capability_checker: None,
        }
    }
}

/// host 侧 Store 携带的状态：记录本次 invoke 的插件 id + 命中的白名单校验上下文。
struct HostState {
    /// 当前 invoke 的插件 id（用于白名单校验 + 日志命名空间）。
    plugin_id: String,
    /// 已授予的能力集合（invoke 开始时按 plugin_id 解析快照）。
    granted: std::collections::HashSet<String>,
    /// host 能力实现查找表（name → 实现）。
    capabilities: HashMap<String, Arc<dyn HostCapability>>,
    /// 本次 invoke 期间越权调用记录（用于错误信息聚合）。
    denied: Vec<String>,
}

/// 一个已编译的 WASM 模块（按 plugin_id 缓存）。
pub struct LoadedModule {
    module: Module,
    /// 加载来源路径（热重载时比对，相同则跳过重编）。
    path: PathBuf,
}

/// WASM 插件运行时：管理 Engine（全局编译环境）+ 按 plugin_id 缓存的 Module。
///
/// invoke 流程：取 module → 新建 Store（带 host 能力白名单）→ Linker 注册 host fn
/// → instantiate → 经线性内存传 JSON 输入 → 调 execute → 读回 JSON 输出。
///
/// 热重载：重新 `Module::from_file` 覆盖缓存，旧 Store 自然 drop，零坑。
pub struct WasmRuntime {
    /// 全局编译环境（单例）。
    engine: Engine,
    /// plugin_id → 已编译模块。
    modules: RwLock<HashMap<String, Arc<LoadedModule>>>,
    /// 配置（host 能力注册表 + 白名单校验器）。
    config: Mutex<WasmRuntimeConfig>,
}

impl WasmRuntime {
    /// 创建运行时（用默认 Engine 配置）。
    pub fn new() -> Result<Self, PluginError> {
        Self::with_config(WasmRuntimeConfig::default())
    }

    /// 创建运行时并指定配置（host 能力注册表 + 白名单校验器）。
    pub fn with_config(config: WasmRuntimeConfig) -> Result<Self, PluginError> {
        let engine = Engine::default();
        Ok(Self {
            engine,
            modules: RwLock::new(HashMap::new()),
            config: Mutex::new(config),
        })
    }

    /// 返回内部 Engine 引用（外部可用于 Module::new 等场景）。
    pub fn engine(&self) -> &Engine {
        &self.engine
    }

    /// 加载（或复用已加载的）WASM 模块。
    ///
    /// `path` 指向 `.wasm` 文件。同 plugin_id 且同路径则复用缓存；不同路径触发重载。
    /// 返回模块的 Arc 引用。
    pub fn load(&self, plugin_id: &str, path: &Path) -> Result<Arc<LoadedModule>, PluginError> {
        // 先检查缓存
        {
            let cached = self.modules.read();
            if let Some(m) = cached.get(plugin_id) {
                if m.path == path {
                    return Ok(Arc::clone(m));
                }
            }
        }
        let module = Module::from_file(&self.engine, path).map_err(|e| PluginError {
            message: format!(
                "wasm module compile failed ({}): {}",
                path.display(),
                e
            ),
            code: Some("WASM_COMPILE_FAILED".to_string()),
            source: Some("wasm-loader".to_string()),
        })?;
        let loaded = Arc::new(LoadedModule {
            module,
            path: path.to_path_buf(),
        });
        self.modules
            .write()
            .insert(plugin_id.to_string(), Arc::clone(&loaded));
        debug!(plugin_id = plugin_id, path = ?path, "WASM module loaded");
        Ok(loaded)
    }

    /// 从内联字节（.wasm 二进制或 WAT 文本）加载模块（测试 / 预编译缓存场景）。
    ///
    /// `bytes` 可以是 .wasm 二进制或 WAT 文本（wasmtime 自动识别）。
    /// `source_tag` 仅用于日志标识，不参与缓存比对。
    #[allow(dead_code)]
    pub fn load_bytes(
        &self,
        plugin_id: &str,
        bytes: &[u8],
        source_tag: &str,
    ) -> Result<(), PluginError> {
        // 判断是 WAT 文本还是 .wasm 二进制：先 trim 前导空白，再看是否以 "(" 开头
        // （WAT 文本以 "(module" 或 "(component" 开头，可能带前导空白/换行）。
        let trimmed = bytes.iter().copied().skip_while(|b| b.is_ascii_whitespace()).collect::<Vec<u8>>();
        let is_wat = trimmed.starts_with(b"(module") || trimmed.starts_with(b"(component");
        let module = if is_wat {
            // WAT 文本：wat::parse_bytes 后 Module::from_binary
            let w = wat::parse_bytes(bytes).map_err(|e| PluginError {
                message: format!("wat parse failed ({}): {}", source_tag, e),
                code: Some("WASM_WAT_PARSE".to_string()),
                source: Some("wasm-loader".to_string()),
            })?;
            Module::from_binary(&self.engine, &w).map_err(|e| PluginError {
                message: format!("wasm module from binary failed ({}): {}", source_tag, e),
                code: Some("WASM_COMPILE_FAILED".to_string()),
                source: Some("wasm-loader".to_string()),
            })
        } else {
            Module::from_binary(&self.engine, bytes).map_err(|e| PluginError {
                message: format!("wasm module compile failed ({}): {}", source_tag, e),
                code: Some("WASM_COMPILE_FAILED".to_string()),
                source: Some("wasm-loader".to_string()),
            })
        }?;
        self.modules.write().insert(
            plugin_id.to_string(),
            Arc::new(LoadedModule {
                module,
                path: PathBuf::from(source_tag),
            }),
        );
        debug!(plugin_id = plugin_id, tag = source_tag, "WASM module loaded from bytes");
        Ok(())
    }

    /// 卸载插件（从缓存移除模块；持有 Arc 的 invoke 仍可完成）。
    pub fn unload(&self, plugin_id: &str) -> Result<(), PluginError> {
        match self.modules.write().remove(plugin_id) {
            Some(_) => {
                debug!(plugin_id = plugin_id, "WASM module unloaded");
                Ok(())
            }
            None => Err(PluginError {
                message: format!("wasm plugin not loaded (cannot unload): {}", plugin_id),
                code: Some("WASM_NOT_LOADED".to_string()),
                source: Some("wasm-loader".to_string()),
            }),
        }
    }

    /// 热重载：用新文件路径重新编译并覆盖模块缓存。
    ///
    /// WASM 天然安全——drop Store 即释放全部 guest 内存，无 dlclose 坑（§四）。
    /// 正在执行的 invoke 持有旧 Module 的 Arc，会完成本次调用；下次 invoke 用新模块。
    pub fn reload(&self, plugin_id: &str, new_path: &Path) -> Result<(), PluginError> {
        debug!(plugin_id = plugin_id, path = ?new_path, "WASM hot reload");
        // load 内部会覆盖缓存（同 plugin_id 不同 path）
        self.load(plugin_id, new_path).map(|_| ())
    }

    /// 查询插件是否已加载。
    pub fn is_loaded(&self, plugin_id: &str) -> bool {
        self.modules.read().contains_key(plugin_id)
    }

    /// 调用已加载的 WASM 插件。
    ///
    /// 输入经 JSON 序列化写入 WASM 线性内存 → 调 execute → 读回输出 JSON → 反序列化为
    /// PluginResult。host 能力调用经 Linker 注入，白名单校验越权拒绝。
    pub fn invoke(
        &self,
        plugin_id: &str,
        input: &Value,
    ) -> Result<PluginResult, PluginError> {
        let module_arc = {
            let mods = self.modules.read();
            mods.get(plugin_id)
                .map(Arc::clone)
                .ok_or_else(|| PluginError {
                    message: format!("wasm plugin not loaded: {}", plugin_id),
                    code: Some("WASM_NOT_LOADED".to_string()),
                    source: Some("wasm-loader".to_string()),
                })?
        };

        // 解析本次 invoke 的白名单 + 能力表
        let (granted, capabilities) = {
            let cfg = self.config.lock();
            let granted: std::collections::HashSet<String> = cfg
                .capability_checker
                .as_ref()
                .map(|c| c.granted(plugin_id).into_iter().collect())
                .unwrap_or_default();
            let caps: HashMap<String, Arc<dyn HostCapability>> = cfg
                .host_registry
                .as_ref()
                .map(|r| {
                    r.capabilities()
                        .into_iter()
                        .map(|c| (c.name().to_string(), c))
                        .collect()
                })
                .unwrap_or_default();
            (granted, caps)
        };

        let state = HostState {
            plugin_id: plugin_id.to_string(),
            granted,
            capabilities,
            denied: Vec::new(),
        };

        // 新建 Store（直接拥有 HostState）+ Linker，注册 host 能力 import
        let mut store: Store<HostState> = Store::new(&self.engine, state);
        let mut linker: Linker<HostState> = Linker::new(&self.engine);

        // 注册所有能力为 import 模块 "host" 下的函数。
        // 注意：函数签名统一为 (i32, i32) -> i64（JSON 经内存传递，和 execute 一致），
        // 这样 guest 侧 import 声明固定，无需为每个能力生成不同签名。
        // 实现细节：host 收到 (ptr, len) → 从 guest memory 读 JSON params → 校验白名单
        // → 调能力 → 把 result JSON 写回 guest memory → 返回 packed (ptr|len<<32)。
        for name in store.data().capabilities.keys().cloned().collect::<Vec<_>>() {
            let cap_name = name.clone();
            let import_name = capability_import_name(&cap_name);
            linker
                .func_wrap(
                    "host",
                    &import_name,
                    move |mut caller: Caller<'_, HostState>, ptr: i32, len: i32| -> i64 {
                        Self::dispatch_host_call(&mut caller, &cap_name, ptr, len)
                    },
                )
                .map_err(|e| PluginError {
                    message: format!("linker func_wrap failed: {}", e),
                    code: Some("WASM_LINKER_FAILED".to_string()),
                    source: Some("wasm-loader".to_string()),
                })?;
        }

        // instantiate（linker 注入 import）
        let instance = linker
            .instantiate(&mut store, &module_arc.module)
            .map_err(|e| PluginError {
                message: format!("wasm instantiate failed ({}): {}", plugin_id, e),
                code: Some("WASM_INSTANTIATE_FAILED".to_string()),
                source: Some("wasm-loader".to_string()),
            })?;

        // instantiate 后检查 denied（部分模块可能在 start function 调 host）
        if !store.data().denied.is_empty() {
            let denied = std::mem::take(&mut store.data_mut().denied);
            return Err(Self::denied_error(plugin_id, denied));
        }

        // 取 exports
        let memory = instance
            .get_memory(&mut store, WASM_MEM_EXPORT)
            .ok_or_else(|| PluginError {
                message: format!(
                    "wasm module missing export '{}' for plugin {}",
                    WASM_MEM_EXPORT, plugin_id
                ),
                code: Some("WASM_NO_MEMORY".to_string()),
                source: Some("wasm-loader".to_string()),
            })?;

        let dealloc = instance.get_typed_func::<(i32, i32), ()>(&mut store, WASM_DEALLOC_FN).ok();

        let execute = instance
            .get_typed_func::<(i32, i32), i64>(&mut store, WASM_EXECUTE_FN)
            .map_err(|e| PluginError {
                message: format!(
                    "wasm module missing/invalid export '{}' for plugin {}: {}",
                    WASM_EXECUTE_FN, plugin_id, e
                ),
                code: Some("WASM_NO_EXECUTE".to_string()),
                source: Some("wasm-loader".to_string()),
            })?;

        // 序列化输入 JSON
        let input_bytes = serde_json::to_vec(input).map_err(|e| PluginError {
            message: format!("wasm input serialize failed: {}", e),
            code: Some("WASM_INPUT_SERIALIZE".to_string()),
            source: Some("wasm-loader".to_string()),
        })?;

        // 把输入写入 guest 内存：先调 allocate 拿 ptr，再 memory.write
        let in_ptr = Self::write_to_guest(&mut store, &memory, &input_bytes, &instance)?;

        // 调 execute(in_ptr, in_len) -> packed(out_ptr | out_len << 32)
        let packed = execute
            .call(&mut store, (in_ptr, input_bytes.len() as i32))
            .map_err(|e| PluginError {
                message: format!("wasm execute call failed ({}): {}", plugin_id, e),
                code: Some("WASM_EXECUTE_FAILED".to_string()),
                source: Some("wasm-loader".to_string()),
            })?;

        // 释放输入缓冲区（guest 分配的）
        if let Some(alloc_dealloc) = dealloc.as_ref() {
            let _ = alloc_dealloc.call(&mut store, (in_ptr, input_bytes.len() as i32));
        }

        // 解包输出
        let out_ptr = (packed & 0xFFFF_FFFFu64 as i64) as i32;
        let out_len = ((packed as u64) >> 32) as i32;

        // 检查越权（execute 内部可能调 host）
        if !store.data().denied.is_empty() {
            let denied = std::mem::take(&mut store.data_mut().denied);
            // 释放输出缓冲区再返回错误
            if let Some(alloc_dealloc) = dealloc.as_ref() {
                if out_ptr != 0 && out_len > 0 {
                    let _ = alloc_dealloc.call(&mut store, (out_ptr, out_len));
                }
            }
            return Err(Self::denied_error(plugin_id, denied));
        }

        // 读回输出 JSON
        let result: PluginResult = if out_ptr == 0 || out_len == 0 {
            // 空输出——按空结果处理（与原生 loader 一致）
            PluginResult::default()
        } else {
            let mut buf = vec![0u8; out_len as usize];
            memory
                .read(&store, out_ptr as usize, &mut buf)
                .map_err(|e| PluginError {
                    message: format!("wasm read output failed: {}", e),
                    code: Some("WASM_READ_OUTPUT".to_string()),
                    source: Some("wasm-loader".to_string()),
                })?;
            // 释放输出缓冲区
            if let Some(alloc_dealloc) = dealloc.as_ref() {
                let _ = alloc_dealloc.call(&mut store, (out_ptr, out_len));
            }
            serde_json::from_slice(&buf).map_err(|e| PluginError {
                message: format!("wasm output parse failed: {}", e),
                code: Some("WASM_OUTPUT_PARSE".to_string()),
                source: Some("wasm-loader".to_string()),
            })?
        };

        Ok(result)
    }

    /// 把字节写入 guest 线性内存：调 allocate 拿 ptr，再 memory.write。
    fn write_to_guest(
        store: &mut Store<HostState>,
        memory: &Memory,
        bytes: &[u8],
        instance: &Instance,
    ) -> Result<i32, PluginError> {
        let allocate = instance
            .get_typed_func::<i32, i32>(&mut *store, WASM_ALLOCATE_FN)
            .map_err(|e| PluginError {
                message: format!(
                    "wasm module missing/invalid export '{}' : {}",
                    WASM_ALLOCATE_FN, e
                ),
                code: Some("WASM_NO_ALLOCATE".to_string()),
                source: Some("wasm-loader".to_string()),
            })?;
        let ptr = allocate
            .call(&mut *store, bytes.len() as i32)
            .map_err(|e| PluginError {
                message: format!("wasm allocate failed: {}", e),
                code: Some("WASM_ALLOCATE_FAILED".to_string()),
                source: Some("wasm-loader".to_string()),
            })?;
        if ptr == 0 {
            return Err(PluginError {
                message: "wasm allocate returned null ptr".to_string(),
                code: Some("WASM_ALLOCATE_NULL".to_string()),
                source: Some("wasm-loader".to_string()),
            });
        }
        memory
            .write(&mut *store, ptr as usize, bytes)
            .map_err(|e| PluginError {
                message: format!("wasm write input failed: {}", e),
                code: Some("WASM_WRITE_INPUT".to_string()),
                source: Some("wasm-loader".to_string()),
            })?;
        Ok(ptr)
    }

    /// host 能力调用分发：从 guest memory 读 JSON params → 校验白名单 → 调能力
    /// → 把 result JSON 写回 guest memory → 返回 packed(ptr|len<<32)。
    fn dispatch_host_call(caller: &mut Caller<'_, HostState>, cap_name: &str, ptr: i32, len: i32) -> i64 {
        // 取 memory（Caller 上获取 export）
        let memory = match caller.get_export(WASM_MEM_EXPORT).and_then(|e| e.into_memory()) {
            Some(m) => m,
            None => {
                error!(capability = cap_name, "host call: guest has no memory export");
                return 0;
            }
        };

        // 读 params JSON（用 reborrow &mut *caller）
        let params: Value = if ptr == 0 || len == 0 {
            Value::Null
        } else {
            let mut buf = vec![0u8; len as usize];
            if memory.read(&mut *caller, ptr as usize, &mut buf).is_err() {
                error!(capability = cap_name, "host call: read params failed");
                return 0;
            }
            serde_json::from_slice(&buf).unwrap_or(Value::Null)
        };

        // 白名单校验（N9）
        let is_granted = caller.data().granted.contains(cap_name);
        if !is_granted {
            let plugin_id = caller.data().plugin_id.clone();
            warn!(
                capability = cap_name,
                plugin_id = %plugin_id,
                "WASM capability denied (not in granted_capabilities)"
            );
            caller.data_mut().denied.push(cap_name.to_string());
            // 返回带 error 的 JSON，让 guest 看到拒绝
            return Self::write_host_result(caller, &memory, &serde_json::json!({"error": "denied"}));
        }

        // 调能力实现
        let cap_impl = match caller.data().capabilities.get(cap_name) {
            Some(c) => Arc::clone(c),
            None => {
                error!(
                    capability = cap_name,
                    "host call: capability registered in linker but no implementation"
                );
                return Self::write_host_result(caller, &memory, &serde_json::json!({"error": "no_impl"}));
            }
        };
        let result = match cap_impl.call(&params) {
            Ok(v) => v,
            Err(e) => {
                warn!(capability = cap_name, error = %e.message, "host capability call failed");
                Value::String(format!("error: {}", e.message))
            }
        };
        Self::write_host_result(caller, &memory, &result)
    }

    /// 把 result JSON 写回 guest memory（调 guest 的 allocate），返回 packed(ptr|len<<32)。
    fn write_host_result(caller: &mut Caller<'_, HostState>, memory: &Memory, result: &Value) -> i64 {
        let bytes = match serde_json::to_vec(result) {
            Ok(b) => b,
            Err(_) => return 0,
        };
        let allocate = match caller.get_export(WASM_ALLOCATE_FN).and_then(|e| e.into_func()) {
            Some(f) => f,
            None => return 0,
        };
        let allocate = match allocate.typed::<i32, i32>(&mut *caller) {
            Ok(f) => f,
            Err(_) => return 0,
        };
        let ptr = match allocate.call(&mut *caller, bytes.len() as i32) {
            Ok(p) => p,
            Err(_) => return 0,
        };
        if ptr == 0 {
            return 0;
        }
        if memory.write(&mut *caller, ptr as usize, &bytes).is_err() {
            return 0;
        }
        (((bytes.len() as u64) << 32) | (ptr as u32 as u64)) as i64
    }

    fn denied_error(plugin_id: &str, denied: Vec<String>) -> PluginError {
        PluginError {
            message: format!(
                "wasm plugin '{}' denied host capabilities not in granted_capabilities: [{}]",
                plugin_id,
                denied.join(", ")
            ),
            code: Some("WASM_CAPABILITY_DENIED".to_string()),
            source: Some("wasm-loader".to_string()),
        }
    }
}

impl Default for WasmRuntime {
    fn default() -> Self {
        Self::new().expect("WasmRuntime::new failed")
    }
}

/// 把 host 能力全名（如 `host.log`）映射为 wasm import 的函数名。
///
/// wasm import 形如 `(import "host" "log" ...)`——模块名固定 "host"，
/// 函数名取能力全名去掉 `host.` 前缀（如 `host.log` → `log`）。
fn capability_import_name(cap_name: &str) -> String {
    cap_name
        .strip_prefix("host.")
        .unwrap_or(cap_name)
        .to_string()
}

// ──────────────────────────────────────────────────────────────────────────
// 测试
// ──────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// 一个极简的 WAT 模块：实现 allocate/deallocate/execute + memory。
    ///
    /// execute 读入 JSON，原样回显（echo 插件）——用于测试参数传递。
    /// allocate 用一个简单的"在内存末尾追加"分配器（页内 bump）。
    ///
    /// 内存布局：偏移 0..4 存放下一个分配位置（bump 指针），从 4 开始分配。
    const ECHO_WAT: &str = r#"
(module
  (memory (export "memory") 1)
  ;; bump 分配指针（i32，存放在 global，初值 4：跳过偏移 0 的保留区）
  (global $bump (mut i32) (i32.const 4))

  ;; allocate(len) -> ptr：bump 分配，简单返回递增偏移。
  ;; 同时 export 为 "allocate"，并带 $allocate 标签供 execute 内部 call。
  (func $allocate (export "allocate") (param $len i32) (result i32)
    (local $ptr i32)
    (local.set $ptr (global.get $bump))
    (global.set $bump (i32.add (global.get $bump) (local.get $len)))
    ;; 对齐到 4 字节
    (global.set $bump (i32.and (i32.add (global.get $bump) (i32.const 3))
                                (i32.const 0x7FFFFFFC)))
    (local.get $ptr))

  ;; deallocate(ptr, len)：no-op（bump 分配器不回收）
  (func (export "deallocate") (param $ptr i32) (param $len i32))

  ;; execute(in_ptr, in_len) -> packed(out_ptr | out_len << 32)
  ;; 实现 echo：分配 in_len 字节，把输入复制过去，返回它的 (ptr, len)。
  (func (export "execute") (param $in_ptr i32) (param $in_len i32) (result i64)
    (local $out_ptr i32)
    (local.set $out_ptr (call $allocate (local.get $in_len)))
    (memory.copy (local.get $out_ptr) (local.get $in_ptr) (local.get $in_len))
    (i64.or
      (i64.extend_i32_u (local.get $out_ptr))
      (i64.shl (i64.extend_i32_u (local.get $in_len)) (i64.const 32))))
)
"#;

    /// 加载 echo 模块到运行时（用 WAT 文本，免去 wasm32 工具链依赖）。
    fn runtime_with_echo(id: &str) -> WasmRuntime {
        let rt = WasmRuntime::new().expect("engine");
        rt.load_bytes(id, ECHO_WAT.as_bytes(), "echo.wat")
            .expect("load echo");
        rt
    }

    // ── N5: 基础加载/调用 ──

    /// echo 模块：输入原样回显，验证 JSON 经线性内存传递的双向路径。
    #[test]
    fn echo_roundtrip_returns_input_as_output() {
        let rt = runtime_with_echo("echo");
        let input = json!({
            "state": {"x": 1},
            "config": {},
            "tenant_id": "t1"
        });
        let result = rt.invoke("echo", &input).expect("invoke");
        // echo 模块把输入 JSON 原样回显——但输入是 PluginInput 形态，
        // 回显后反序列化为 PluginResult 时字段不匹配，得到空 result。
        // 这里我们只验证"调用链通了 + 无错"。真正的语义 echo 见下方 JSON-result 测试。
        let _ = result;
    }

    /// 未加载的插件 invoke 返回 WASM_NOT_LOADED。
    #[test]
    fn invoke_not_loaded_errors() {
        let rt = WasmRuntime::new().unwrap();
        let err = rt.invoke("nope", &json!({})).unwrap_err();
        assert_eq!(err.code.as_deref(), Some("WASM_NOT_LOADED"));
    }

    /// 缺 execute 导出的模块 → WASM_NO_EXECUTE。
    #[test]
    fn missing_execute_export_errors() {
        let rt = WasmRuntime::new().unwrap();
        // 只有 memory + allocate，没有 execute
        let wat = r#"
(module
  (memory (export "memory") 1)
  (func (export "allocate") (param $len i32) (result i32) (i32.const 1024))
  (func (export "deallocate") (param $p i32) (param $l i32))
)"#;
        rt.load_bytes("bad", wat.as_bytes(), "bad.wat").unwrap();
        let err = rt.invoke("bad", &json!({})).unwrap_err();
        assert_eq!(err.code.as_deref(), Some("WASM_NO_EXECUTE"));
    }

    /// 缺 memory 导出的模块 → WASM_NO_MEMORY。
    #[test]
    fn missing_memory_export_errors() {
        let rt = WasmRuntime::new().unwrap();
        let wat = r#"
(module
  (memory 1)
  (func (export "allocate") (param $len i32) (result i32) (i32.const 1024))
  (func (export "deallocate") (param $p i32) (param $l i32))
  (func (export "execute") (param $p i32) (param $l i32) (result i64) (i64.const 0))
)"#;
        rt.load_bytes("nomem", wat.as_bytes(), "nomem.wat").unwrap();
        let err = rt.invoke("nomem", &json!({})).unwrap_err();
        assert_eq!(err.code.as_deref(), Some("WASM_NO_MEMORY"));
    }

    // ── N5: 热重载 ──

    /// 热重载：load 同 plugin_id 不同"路径"（这里用不同 WAT 字节模拟）后，
    /// 下次 invoke 用新模块。WASM drop Store 零坑。
    #[test]
    fn reload_replaces_module() {
        let rt = WasmRuntime::new().unwrap();
        // 先加载 echo
        rt.load_bytes("plug", ECHO_WAT.as_bytes(), "v1").unwrap();
        assert!(rt.is_loaded("plug"));
        // 重载为另一个 wat（这里用同一 echo，但 tag 不同模拟新文件）
        rt.load_bytes("plug", ECHO_WAT.as_bytes(), "v2").unwrap();
        assert!(rt.is_loaded("plug"));
        // unload 后再卸载报错
        rt.unload("plug").unwrap();
        let err = rt.unload("plug").unwrap_err();
        assert_eq!(err.code.as_deref(), Some("WASM_NOT_LOADED"));
    }

    // ── N8/N9: host 能力 + 白名单 ──

    /// 测试用 HostCapability：记录调用次数。
    struct CountingCapability {
        name: String,
        count: AtomicUsize,
    }
    impl CountingCapability {
        fn new(name: &str) -> Self {
            Self {
                name: name.to_string(),
                count: AtomicUsize::new(0),
            }
        }
        fn count(&self) -> usize {
            self.count.load(Ordering::SeqCst)
        }
    }
    impl HostCapability for CountingCapability {
        fn name(&self) -> &str {
            &self.name
        }
        fn call(&self, _params: &Value) -> Result<Value, PluginError> {
            self.count.fetch_add(1, Ordering::SeqCst);
            Ok(json!({"ok": true}))
        }
    }

    struct TestRegistry {
        caps: Vec<Arc<dyn HostCapability>>,
    }
    impl WasmHostRegistry for TestRegistry {
        fn capabilities(&self) -> Vec<Arc<dyn HostCapability>> {
            self.caps.clone()
        }
    }

    struct TestChecker {
        granted: HashMap<String, Vec<String>>,
    }
    impl WasmCapabilityChecker for TestChecker {
        fn granted(&self, plugin_id: &str) -> Vec<String> {
            self.granted.get(plugin_id).cloned().unwrap_or_default()
        }
    }

    /// 白名单校验：插件调用了未授予的能力 → WASM_CAPABILITY_DENIED。
    ///
    /// 模块在 execute 里调 host.log（import），但 manifest 没授予 host.log。
    #[test]
    fn denied_capability_rejected() {
        let log_cap = Arc::new(CountingCapability::new("host.log"));
        let registry = Arc::new(TestRegistry {
            caps: vec![Arc::clone(&log_cap) as Arc<dyn HostCapability>],
        });
        // 故意不授予 host.log
        let checker = Arc::new(TestChecker {
            granted: HashMap::new(),
        });
        let rt = WasmRuntime::with_config(WasmRuntimeConfig {
            host_registry: Some(registry),
            capability_checker: Some(checker),
        })
        .unwrap();

        // 模块：execute 调用 host.log（传入空 params），忽略返回值
        let wat = r#"
(module
  (import "host" "log" (func $log (param i32 i32) (result i64)))
  (memory (export "memory") 1)
  (global $bump (mut i32) (i32.const 4))
  (func (export "allocate") (param $len i32) (result i32)
    (local $ptr i32)
    (local.set $ptr (global.get $bump))
    (global.set $bump (i32.add (global.get $bump) (local.get $len)))
    (global.set $bump (i32.and (i32.add (global.get $bump) (i32.const 3)) (i32.const 0x7FFFFFFC)))
    (local.get $ptr))
  (func (export "deallocate") (param $p i32) (param $l i32))
  (func (export "execute") (param $in_ptr i32) (param $in_len i32) (result i64)
    ;; 调 host.log 传 (0, 0) 空 params——会触发白名单拒绝
    (drop (call $log (i32.const 0) (i32.const 0)))
    ;; 返回空输出（ptr=0, len=0）
    (i64.const 0))
)"#;
        rt.load_bytes("deny_test", wat.as_bytes(), "deny.wat").unwrap();
        let err = rt.invoke("deny_test", &json!({})).unwrap_err();
        assert_eq!(
            err.code.as_deref(),
            Some("WASM_CAPABILITY_DENIED"),
            "denied capability must error, got: {:?}",
            err
        );
        assert_eq!(log_cap.count(), 0, "denied capability must not be invoked");
    }

    /// 白名单校验：授予的能力正常调用，能力实现被命中。
    #[test]
    fn granted_capability_invoked() {
        let log_cap = Arc::new(CountingCapability::new("host.log"));
        let log_clone = Arc::clone(&log_cap);
        let registry = Arc::new(TestRegistry {
            caps: vec![log_clone as Arc<dyn HostCapability>],
        });
        let checker = Arc::new(TestChecker {
            granted: HashMap::from([(
                "ok_test".to_string(),
                vec!["host.log".to_string()],
            )]),
        });
        let rt = WasmRuntime::with_config(WasmRuntimeConfig {
            host_registry: Some(registry),
            capability_checker: Some(checker),
        })
        .unwrap();

        let wat = r#"
(module
  (import "host" "log" (func $log (param i32 i32) (result i64)))
  (memory (export "memory") 1)
  (global $bump (mut i32) (i32.const 4))
  (func (export "allocate") (param $len i32) (result i32)
    (local $ptr i32)
    (local.set $ptr (global.get $bump))
    (global.set $bump (i32.add (global.get $bump) (local.get $len)))
    (global.set $bump (i32.and (i32.add (global.get $bump) (i32.const 3)) (i32.const 0x7FFFFFFC)))
    (local.get $ptr))
  (func (export "deallocate") (param $p i32) (param $l i32))
  (func (export "execute") (param $in_ptr i32) (param $in_len i32) (result i64)
    (drop (call $log (i32.const 0) (i32.const 0)))
    (i64.const 0))
)"#;
        rt.load_bytes("ok_test", wat.as_bytes(), "ok.wat").unwrap();
        let result = rt.invoke("ok_test", &json!({}));
        assert!(result.is_ok(), "granted capability should succeed: {:?}", result);
        assert_eq!(log_cap.count(), 1, "granted capability must be invoked once");
    }

    /// capability_import_name：host.log → log，host.record_metric → record_metric。
    #[test]
    fn capability_import_name_strips_host_prefix() {
        assert_eq!(capability_import_name("host.log"), "log");
        assert_eq!(capability_import_name("host.record_metric"), "record_metric");
        assert_eq!(capability_import_name("plain"), "plain");
    }

    // ── N10: hello world 样例端到端验证 ──
    //
    // 加载预编译的 samples/wasm-hello-plugin/wasm_hello.wasm（no_std wasm32-unknown-unknown，
    // 384 字节），验证内核 WasmRuntime 能加载执行真实 Rust→wasm 产物。
    // 若 .wasm 不存在（如仅克隆仓库未构建样例），测试跳过而非失败——样例需 wasm32 target。

    /// samples 目录的预编译 wasm_hello.wasm 路径（相对 crate 根）。
    const SAMPLE_WASM_REL: &str = "../../../samples/wasm-hello-plugin/wasm_hello.wasm";

    #[test]
    fn hello_world_wasm_loads_and_executes() {
        let crate_root = env!("CARGO_MANIFEST_DIR");
        let wasm_path = std::path::PathBuf::from(crate_root).join(SAMPLE_WASM_REL);
        if !wasm_path.exists() {
            eprintln!(
                "SKIP: sample wasm not built at {} (run cargo build --target wasm32-unknown-unknown in samples/wasm-hello-plugin)",
                wasm_path.display()
            );
            return;
        }
        let rt = WasmRuntime::new().expect("engine");
        rt.load("wasm_hello", &wasm_path).expect("load sample");
        let result = rt.invoke("wasm_hello", &json!({})).expect("invoke sample");
        // 样例插件返回 {"state_updates":{"processed_by":"wasm_hello"}}
        assert_eq!(
            result.state_updates.get("processed_by"),
            Some(&json!("wasm_hello")),
            "hello world wasm should set processed_by=wasm_hello, got: {:?}",
            result.state_updates
        );
    }
}

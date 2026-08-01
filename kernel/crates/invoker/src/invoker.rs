//! PluginInvoker 实现
//!
//! 按 host_type 透明分发调用：
//! - InProcess: 经 `NativePluginLoader` 加载 cdylib，走 C-ABI 调用（JSON 经内存传递）
//! - Wasm: 经 `WasmRuntime`（wasmtime）加载执行 `.wasm`（JSON 经线性内存传递）
//! - Sidecar: 通过 MCP 客户端走 JSON-RPC 协议调用（进程隔离）
//!
//! 三种 host_type 共用 PluginInput / PluginResult JSON 契约，invoker 透明分发。
//!
//! [来源: docs/tasks/task_05_plugin_system.md AC-04-5/AC-04-6]

use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};

use async_trait::async_trait;
use agentos_core::traits::{
    HookContext, HostType, LifecycleHook, PluginInvoker, PluginLoader, PluginManifest, PluginType,
};
use agentos_core::types::{PluginContext, PluginError, PluginResult, ToolExecutionResult};
use agentos_mcp::{CapabilityRouter, McpClient, McpError};
use agentos_plugin_loader::{NativePluginLoader, WasmRuntime};
use parking_lot::RwLock;
use serde_json::{json, Value};
use tracing::{error, info, warn};

/// Pull 热加载的检测 TTL：缓存进程在 1 秒内不重复 stat 指纹，热路径零额外开销。
/// TTL 过期后才 stat 插件目录关键文件 mtime，发现变化才 kill 旧进程 respawn。
/// （未用到也不检测——纯按需 pull。）
const PLUGIN_FINGERPRINT_TTL: Duration = Duration::from_secs(1);

/// 计算插件指纹：对该插件目录下的**源码文件** + plugin.json 声明的 config_files 路径
/// 取 mtime（秒级精度），拼接为字符串后做简单 hash。
///
/// 设计权衡：
/// - 用 mtime 而非内容 hash：stat 是微秒级，内容 hash 要读全部文件（毫秒级），
///   热路径上 stat 性能可接受，mtime 精度足够捕获代码/配置修改。
/// - **只扫源码文件**（.py/.rs/.js/.ts/.wasm/.json/.yaml/.yml/.toml），跳过运行时
///   产生的杂物（.log/.pyc/__pycache__/临时文件）。否则 sidecar 运行时若在插件目录
///   写入临时文件（如诊断日志），会误触发 respawn——实测踩过这个坑。
/// - config_files 指向的配置文件可能在插件目录外（如 config/models/llm.yaml），
///   单独 stat 纳入指纹，配置变更也能触发 respawn。
fn compute_plugin_fingerprint(plugin_dir: &Path, manifest: &PluginManifest) -> u64 {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::Hasher;
    let mut hasher = DefaultHasher::new();

    let mtime_str = |p: &Path| -> String {
        std::fs::metadata(p)
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.duration_since(SystemTime::UNIX_EPOCH).ok())
            .map(|d| format!("{}:{}", d.as_secs(), d.subsec_nanos()))
            .unwrap_or_else(|| "0:0".to_string())
    };

    // 只纳入源码/配置扩展名，跳过 .log/.pyc 等运行时产物。
    const SOURCE_EXTS: &[&str] = &[
        "py", "rs", "js", "ts", "jsx", "tsx", "wasm", "json", "yaml", "yml", "toml", "txt", "md",
    ];
    let is_source = |name: &str| -> bool {
        name.rsplit_once('.')
            .map(|(_, ext)| SOURCE_EXTS.contains(&ext.to_lowercase().as_str()))
            .unwrap_or(false)
    };

    // 扫插件目录下的源码文件（非递归，跳过子目录如 __pycache__）
    if let Ok(entries) = std::fs::read_dir(plugin_dir) {
        let mut paths: Vec<_> = entries
            .filter_map(|e| e.ok())
            .filter_map(|e| {
                let p = e.path();
                if !p.is_file() {
                    return None;
                }
                let name = p.file_name()?.to_string_lossy().to_string();
                // 跳过隐藏文件（.开头，如运行时产生的临时日志）和非源码扩展名
                if name.starts_with('.') || !is_source(&name) {
                    return None;
                }
                Some((name, mtime_str(&p)))
            })
            .collect();
        paths.sort_by(|a, b| a.0.cmp(&b.0));
        for (name, mtime) in paths {
            hasher.write(name.as_bytes());
            hasher.write(b"|");
            hasher.write(mtime.as_bytes());
            hasher.write(b";");
        }
    }

    // 声明的 config_files 路径（可能在插件目录外，如 config/models/llm.yaml）
    // path 是相对 config/ 的，需结合 config_root 解析——但 invoker 无 config_root，
    // 这里对存在的绝对/相对路径直接 stat，不存在则跳过（降级：配置变更靠目录内文件指纹）。
    for mapping in &manifest.config_files {
        let p = Path::new(&mapping.path);
        let mtime = mtime_str(p);
        hasher.write(mapping.id.as_bytes());
        hasher.write(b"|");
        hasher.write(mtime.as_bytes());
        hasher.write(b";");
    }

    hasher.finish()
}

/// 把共享内核路由器包成 per-plugin 路由器——在 params 里注入 `_plugin_id`，
/// 供内核 metrics.record 等需要知道调用方插件的能力使用（监控设计 §三 通道2 + §十
/// 安全：内核强制 plugin_id，插件无法伪报他人指标）。
struct PluginScopedRouter {
    plugin_id: String,
    inner: Arc<dyn CapabilityRouter>,
}

#[async_trait]
impl CapabilityRouter for PluginScopedRouter {
    async fn handle(
        &self,
        capability: &str,
        method: &str,
        mut params: Value,
    ) -> Result<Value, McpError> {
        // 在 params 注入 _plugin_id（内核侧 metrics.record 读取它做命名空间）。
        // 这是信任锚点：plugin_id 来自 invoker 的 manifest，不是 sidecar 上报。
        if let Some(obj) = params.as_object_mut() {
            obj.insert("_plugin_id".to_string(), Value::String(self.plugin_id.clone()));
        } else {
            params = json!({ "_plugin_id": self.plugin_id, "value": params });
        }
        self.inner.handle(capability, method, params).await
    }
}

/// 从 MCP tools/call 响应中提取内部 JSON 值。
///
/// Python SDK 的 McpServer 返回格式为：
/// ```json
/// { "content": [{ "type": "text", "text": "<json_string>" }], "isError": false }
/// ```
/// 其中 `text` 字段是工具实际返回值的 JSON 字符串。
/// 本函数提取 `content[0].text`，解析为 `serde_json::Value` 返回。
///
/// 如果 `isError` 为 true 或解析失败，返回包含错误信息的 JSON 对象。
fn extract_mcp_content(mcp_result: &serde_json::Value) -> serde_json::Value {
    // 检查 isError 标志
    if mcp_result
        .get("isError")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
    {
        let err_msg = mcp_result
            .get("content")
            .and_then(|c| c.as_array())
            .and_then(|arr| arr.first())
            .and_then(|item| item.get("text"))
            .and_then(|t| t.as_str())
            .unwrap_or("MCP tool returned isError=true");
        return serde_json::json!({"error": err_msg});
    }

    // 提取 content[0].text 并解析为 JSON
    let extracted = mcp_result
        .get("content")
        .and_then(|c| c.as_array())
        .and_then(|arr| arr.first())
        .and_then(|item| item.get("text"))
        .and_then(|t| t.as_str())
        .and_then(|s| serde_json::from_str(s).ok());

    match extracted {
        Some(val) => val,
        None => {
            warn!(
                "MCP response content extraction failed, returning raw result: {:?}",
                mcp_result
            );
            mcp_result.clone()
        }
    }
}

/// 内核侧 HostServices 实现：包 `CapabilityRouter`，供原生插件调 capability。
///
/// 与 sidecar（JSON-RPC 反调）、wasm（host.call import）走同一 router，三家对齐。
/// trait 方法是 sync（abi_stable FFI-safe 约定），内部用 block_in_place + block_on
/// 跑 async router.handle（与 capability.rs 的 block_on_router 同手法）。
struct NativeHostServices {
    router: Arc<dyn CapabilityRouter>,
}

impl agentos_native_sdk::HostServices for NativeHostServices {
    fn call_capability(
        &self,
        capability: abi_stable::std_types::RString,
        method: abi_stable::std_types::RString,
        params_json: abi_stable::std_types::RString,
    ) -> abi_stable::std_types::RResult<abi_stable::std_types::RString, abi_stable::std_types::RString>
    {
        let router = Arc::clone(&self.router);
        let cap = capability.into_string();
        let mth = method.into_string();
        let params: Value = serde_json::from_str(params_json.as_str()).unwrap_or(Value::Null);
        // sync→async：多线程 runtime 下 block_in_place 让出 worker 再 block_on，避免死锁。
        // （生产内核是 multi_thread；单线程 runtime 不可用，测试需 multi_thread flavor。）
        let result = tokio::task::block_in_place(|| {
            tokio::runtime::Handle::current().block_on(async move {
                router.handle(&cap, &mth, params).await
            })
        });
        match result {
            Ok(v) => abi_stable::std_types::RResult::ROk(
                abi_stable::std_types::RString::from(serde_json::to_string(&v).unwrap_or_default()),
            ),
            Err(e) => abi_stable::std_types::RResult::RErr(abi_stable::std_types::RString::from(format!("{e}"))),
        }
    }
}

/// PluginInvoker 实现。
///
/// 管理插件实例和 MCP 客户端连接，按 host_type 透明分发调用。
/// 支持崩溃隔离：检测子进程崩溃后卸载能力 + 告警。
pub struct PluginInvokerImpl {
    /// 插件加载器（用于查找 manifest）
    loader: Arc<dyn PluginLoader>,
    /// 已连接的 MCP 客户端 {plugin_id: McpClient}
    mcp_clients: RwLock<HashMap<String, Arc<tokio::sync::Mutex<McpClient>>>>,
    /// per-plugin spawn 互斥锁（single-flight，防并发请求竞态 spawn 多个 sidecar）。
    /// 同一 plugin_id 的 spawn 串行化：首个请求持锁 spawn 并写缓存，后续请求拿锁后
    /// 二次查缓存命中直接复用。修复「并发触发 → 竞态创建多个孤儿 sidecar」泄漏。
    spawn_locks: RwLock<HashMap<String, Arc<tokio::sync::Mutex<()>>>>,
    /// 崩溃回调（插件崩溃时调用）
    #[allow(clippy::type_complexity)]
    crash_callbacks: RwLock<Vec<Arc<dyn Fn(&str) + Send + Sync>>>,
    /// Capability 路由器——sidecar→内核反向调用通道。
    /// 设置后，新建的 MCP 客户端会带上路由器；已有客户端需重连才生效。
    router: RwLock<Option<Arc<dyn CapabilityRouter>>>,
    /// WASM 插件运行时（task_11 N7）：host_type==Wasm 时用于加载/执行 .wasm。
    /// None 时 WASM 插件调用返回 WASM_RUNTIME_NOT_CONFIGURED。
    wasm_runtime: Option<Arc<WasmRuntime>>,
    /// 原生插件加载器（task_11 N2）：host_type==InProcess 时用于加载/执行 cdylib。
    /// None 时原生插件调用返回 NATIVE_LOADER_NOT_CONFIGURED。
    native_loader: Option<Arc<NativePluginLoader>>,
    /// Pull 热加载指纹缓存 {plugin_id: (上次指纹, 上次检测时刻)}。
    /// 调用 sidecar 时比对：TTL 内跳过 stat（零开销），TTL 过后 stat mtime 比对，
    /// 指纹变化则 kill 旧进程走 respawn 路径加载新代码/配置。
    /// （未用到也不检测——纯按需 pull。）
    fingerprints: RwLock<HashMap<String, (u64, Instant)>>,
    /// sidecar 最后调用时刻 {plugin_id: Instant}——空闲软卸载依据。
    /// 每次 get_or_create_mcp_client 命中/创建时刷新；后台 GC 据此判定是否空闲超时。
    last_used: RwLock<HashMap<String, Instant>>,
    /// wasm 最后调用时刻 {plugin_id: Instant}——同上，wasm 模块的空闲软卸载依据。
    wasm_last_used: RwLock<HashMap<String, Instant>>,
    /// 注入给 sidecar 子进程的 PYTHONPATH 项目根目录（project_root）。
    ///
    /// sidecar 的 import 有两种历史写法并存（见 `resolve_pythonpath_src` 注释）：
    /// - `from src.core.logging import ...`（带 src. 前缀，需 project_root 在 sys.path）
    /// - `from config.settings import ...`（不带前缀，需 project_root/src 在 sys.path）
    /// 因此实际注入的 PYTHONPATH 同时含 project_root 和 project_root/src。
    ///
    /// 历史上 PYTHONPATH 注入依赖 `AGENTOS_PLUGINS_DIR` 环境变量推算，但启动方式
    /// （如 Git Bash 的 start_web_02.sh）未必设置该变量，导致 sidecar 的 plugin.py
    /// 无法 import 公共包，sidecar 启动即崩溃、initialize 永久卡到超时。这里改由内核
    /// 构造期显式注入 project_root，环境变量仅作向后兼容兜底。
    pythonpath_src: RwLock<Option<std::path::PathBuf>>,
}

impl PluginInvokerImpl {
    /// 创建 PluginInvoker。
    pub fn new(loader: Arc<dyn PluginLoader>) -> Self {
        Self {
            loader,
            mcp_clients: RwLock::new(HashMap::new()),
            spawn_locks: RwLock::new(HashMap::new()),
            crash_callbacks: RwLock::new(Vec::new()),
            router: RwLock::new(None),
            wasm_runtime: None,
            native_loader: None,
            fingerprints: RwLock::new(HashMap::new()),
            last_used: RwLock::new(HashMap::new()),
            wasm_last_used: RwLock::new(HashMap::new()),
            pythonpath_src: RwLock::new(None),
        }
    }

    /// 创建带 WASM 运行时的 PluginInvoker（task_11 N7）。
    ///
    /// `wasm_runtime` 由调用方（engine 启动期）构造并注入，便于共享 host 能力注册表
    /// 与白名单校验器。None 等价于 [`Self::new`]。
    pub fn with_wasm_runtime(loader: Arc<dyn PluginLoader>, wasm_runtime: Arc<WasmRuntime>) -> Self {
        Self {
            loader,
            mcp_clients: RwLock::new(HashMap::new()),
            spawn_locks: RwLock::new(HashMap::new()),
            crash_callbacks: RwLock::new(Vec::new()),
            router: RwLock::new(None),
            wasm_runtime: Some(wasm_runtime),
            native_loader: None,
            fingerprints: RwLock::new(HashMap::new()),
            last_used: RwLock::new(HashMap::new()),
            wasm_last_used: RwLock::new(HashMap::new()),
            pythonpath_src: RwLock::new(None),
        }
    }

    /// 显式设置注入给 sidecar 的 PYTHONPATH 项目根目录（project_root）。
    ///
    /// 应传入 **`project_root`**（`src/` 的父目录），而非 `src/` 本身。
    /// [`resolve_pythonpath_src`] 会据此推导出完整的 PYTHONPATH（同时含 project_root
    /// 和 project_root/src，兼容两种 import 写法）。
    ///
    /// 优先级高于 `AGENTOS_PLUGINS_DIR` 环境变量推算。内核 main 在构造 invoker 后、
    /// spawn 任何 sidecar 前调用，确保无论用何种方式启动（.bat / .sh / IDE），
    /// sidecar 的 plugin.py 都能 import 公共业务包（src.core.logging / config.settings 等）。
    pub fn set_pythonpath_src(&self, project_root: impl Into<std::path::PathBuf>) {
        let path = project_root.into();
        if path.is_dir() {
            *self.pythonpath_src.write() = Some(path);
        } else {
            warn!(path = %path.display(), "set_pythonpath_src: 目录不存在，忽略");
        }
    }

    /// 解析注入给 sidecar 的完整 PYTHONPATH 字符串（多路径，用 OS 分隔符连接）。
    ///
    /// sidecar 的 import 历史上有两种写法并存，必须同时满足：
    /// - `from src.core.logging import ...`（带 src. 前缀）→ 需 **project_root** 在
    ///   sys.path，Python 才在 `<project_root>/src/core/...` 解析。
    /// - `from config.settings import ...`（不带前缀）→ 需 **project_root/src** 在
    ///   sys.path，Python 才在 `<project_root>/src/config/...` 解析。
    /// 故 PYTHONPATH 同时含两者。只放其一会导致另一种写法的插件 sidecar 启动即崩
    /// （实测：prompt_build 用 `from config.settings`、SDK 用 `from src.core.logging`）。
    ///
    /// 返回的字符串已拼上原有的 PYTHONPATH 环境变量（若存在）。
    fn resolve_pythonpath_src(&self) -> Option<String> {
        // ① 显式注入的 project_root（最可靠）
        let project_root = self.pythonpath_src.read().clone().or_else(|| {
            // ② 环境变量兜底（向后兼容 .bat 等显式设置 AGENTOS_PLUGINS_DIR 的启动方式）
            let plugins_dir = std::env::var("AGENTOS_PLUGINS_DIR").ok()?;
            let plugins_path = std::path::Path::new(&plugins_dir);
            // plugins/shared → plugins/ → project_root
            Some(plugins_path.parent()?.parent()?.to_path_buf())
        })?;

        // 拼接两个候选目录：project_root（解 src. 前缀）+ project_root/src（解裸 import）。
        let mut dirs: Vec<std::path::PathBuf> = vec![project_root.clone()];
        let src_dir = project_root.join("src");
        if src_dir.is_dir() {
            dirs.push(src_dir);
        }

        // PYTHONPATH 是 env 变量，路径间用 **环境变量分隔符**（Windows ';'、Unix ':'）
        // 连接——注意这跟路径组件分隔符 MAIN_SEPARATOR（Windows '\'、Unix '/'）是两回事。
        // 历史实现误用 MAIN_SEPARATOR 导致路径粘连成 "D:\...\D:\...\src"（实测踩过）。
        const ENV_PATH_SEP: char = if cfg!(windows) { ';' } else { ':' };
        let existing = std::env::var("PYTHONPATH").unwrap_or_default();
        let joined = dirs
            .iter()
            .map(|d| d.to_string_lossy().into_owned())
            .collect::<Vec<_>>()
            .join(&ENV_PATH_SEP.to_string());
        let result = if existing.is_empty() {
            joined
        } else {
            format!("{}{}{}", joined, ENV_PATH_SEP, existing)
        };
        Some(result)
    }

    /// 链式注入 WASM 运行时（启动期装配用）。
    pub fn set_wasm_runtime(mut self, wasm_runtime: Arc<WasmRuntime>) -> Self {
        self.wasm_runtime = Some(wasm_runtime);
        self
    }

    /// 链式注入原生插件加载器（启动期装配用）。
    ///
    /// 启用 `host_type == InProcess` 的 cdylib 插件：放进插件目录 + 重启即用，
    /// 无需改任何代码（详见 [`Self::resolve_native_artifact`]）。
    pub fn set_native_loader(mut self, native_loader: Arc<NativePluginLoader>) -> Self {
        self.native_loader = Some(native_loader);
        self
    }

    /// 解析 WASM 插件产物路径（manifest.wasm.artifact 相对插件目录）。
    fn resolve_wasm_artifact(
        &self,
        plugin_id: &str,
        manifest: &PluginManifest,
    ) -> Result<std::path::PathBuf, PluginError> {
        let artifact = manifest
            .wasm
            .as_ref()
            .map(|w| w.artifact.as_str())
            .ok_or_else(|| PluginError {
                message: format!(
                    "Wasm plugin '{}' missing manifest.wasm.artifact (ADR 附录 D②)",
                    plugin_id
                ),
                code: Some("MISSING_WASM_ARTIFACT".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;
        let dir = self.loader.get_plugin_dir(plugin_id).ok_or_else(|| PluginError {
            message: format!(
                "Wasm plugin '{}' directory not found (not discovered?)",
                plugin_id
            ),
            code: Some("WASM_PLUGIN_DIR_NOT_FOUND".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;
        Ok(std::path::PathBuf::from(dir).join(artifact))
    }

    /// 解析原生插件产物路径（manifest.native.artifact 相对插件目录）。
    ///
    /// 与 [`Self::resolve_wasm_artifact`] 对称：`artifact` 可写裸名（如 `my_plugin`）
    /// 或带平台后缀（如 `my_plugin.dll`）。裸名时按平台补 `.dll`/`.so`/`.dylib`
    /// （[`NativePluginLoader::platform_artifact_name`]）。
    fn resolve_native_artifact(
        &self,
        plugin_id: &str,
        manifest: &PluginManifest,
    ) -> Result<std::path::PathBuf, PluginError> {
        let artifact = manifest
            .native
            .as_ref()
            .map(|n| n.artifact.as_str())
            .ok_or_else(|| PluginError {
                message: format!(
                    "Native plugin '{}' missing manifest.native.artifact (ADR 附录 D②)",
                    plugin_id
                ),
                code: Some("MISSING_NATIVE_ARTIFACT".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;
        let dir = self.loader.get_plugin_dir(plugin_id).ok_or_else(|| PluginError {
            message: format!(
                "Native plugin '{}' directory not found (not discovered?)",
                plugin_id
            ),
            code: Some("NATIVE_PLUGIN_DIR_NOT_FOUND".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;
        // 裸名自动补平台后缀，带后缀的原样保留
        let resolved = NativePluginLoader::platform_artifact_name(artifact);
        Ok(std::path::PathBuf::from(dir).join(resolved))
    }

    /// 取 native loader 或返回配置缺失错误（三个 native 调用路径共用）。
    fn native_loader_or_err(&self, plugin_id: &str) -> Result<&Arc<NativePluginLoader>, PluginError> {
        self.native_loader.as_ref().ok_or_else(|| PluginError {
            message: format!(
                "Native plugin '{}' invoked but no NativePluginLoader configured",
                plugin_id
            ),
            code: Some("NATIVE_LOADER_NOT_CONFIGURED".to_string()),
            source: Some("plugin-invoker".to_string()),
        })
    }

    /// 解析 native 插件的 C-ABI 入口符号（默认 plugin_execute 时返回 None 走 loader 默认）。
    fn native_entry_symbol<'a>(manifest: &'a PluginManifest) -> Option<&'a [u8]> {
        manifest
            .invoke_entry
            .as_deref()
            .filter(|s| *s != "plugin_execute")
            .map(str::as_bytes)
    }

    /// 加载 cdylib（resolve artifact + abi_stable load，三处 native 路径共用）。
    fn load_native(
        &self,
        loader: &NativePluginLoader,
        plugin_id: &str,
        manifest: &PluginManifest,
    ) -> Result<(), PluginError> {
        let native_path = self.resolve_native_artifact(plugin_id, manifest)?;
        loader.load(plugin_id, &native_path)?;
        Ok(())
    }

    /// 崩溃回调：调用失败且为 panic/fatal 时通知（native pipeline/tool 共用）。
    fn notify_if_crash(&self, plugin_id: &str, result: &Result<PluginResult, PluginError>) {
        if let Err(ref e) = result {
            if e
                .code
                .as_deref()
                .map(|c| c.contains("PANICKED") || c.contains("FATAL"))
                .unwrap_or(false)
            {
                self.notify_crash(plugin_id);
            }
        }
    }

    /// 取 wasm runtime 或返回配置缺失错误（wasm 调用路径共用）。
    fn wasm_runtime_or_err(&self, plugin_id: &str) -> Result<&Arc<WasmRuntime>, PluginError> {
        self.wasm_runtime.as_ref().ok_or_else(|| PluginError {
            message: format!(
                "Wasm plugin '{}' invoked but no WasmRuntime configured",
                plugin_id
            ),
            code: Some("WASM_RUNTIME_NOT_CONFIGURED".to_string()),
            source: Some("plugin-invoker".to_string()),
        })
    }

    /// 加载 wasm 模块（resolve artifact + load，wasm 路径共用）。
    fn load_wasm(
        &self,
        runtime: &WasmRuntime,
        plugin_id: &str,
        manifest: &PluginManifest,
    ) -> Result<(), PluginError> {
        let wasm_path = self.resolve_wasm_artifact(plugin_id, manifest)?;
        runtime.load(plugin_id, &wasm_path)?;
        Ok(())
    }

    /// 原生插件 pipeline 调用：config 注入 + 热重载 + 崩溃回调，与 sidecar 对齐。
    ///
    /// abi_stable 版：构造 HostServices（包 router）注入 PluginCtx，调 loader.execute
    /// 直接 trait 对象派发。不再经 JSON 序列化中间层，不再用 host_call 函数指针。
    async fn invoke_native_pipeline(
        &self,
        plugin_id: &str,
        manifest: &PluginManifest,
        ctx: &PluginContext,
    ) -> Result<PluginResult, PluginError> {
        let loader = self.native_loader_or_err(plugin_id)?;

        // 热重载指纹检测（与 sidecar 同逻辑）：代码/配置变更告警。
        if self.is_plugin_stale(plugin_id, manifest).await {
            warn!(
                "Native plugin '{}' source changed but cdylib cannot hot-unload (dlclose limit); \
                 restart kernel to pick up changes",
                plugin_id
            );
        }

        self.load_native(loader, plugin_id, manifest)?;

        // config 注入（shared::build_injected_config，按 manifest.config_files 命名空间）。
        let config = crate::shared::build_injected_config(
            &self.loader.load_config().await.unwrap_or(serde_json::Value::Null),
            manifest,
        );

        // 构造 HostServices（包 router）。router 缺失则 host=None，插件降级。
        let host = self.router.read().as_ref().map(|router| {
            agentos_native_sdk::HostServicesBox::from_value(
                NativeHostServices {
                    router: Arc::clone(router),
                },
                abi_stable::sabi_trait::prelude::TD_Opaque,
            )
        });

        let state_json = serde_json::to_string(&ctx.state).unwrap_or_else(|_| "{}".into());
        let config_json = serde_json::to_string(&config).unwrap_or_else(|_| "{}".into());

        let result = loader.execute(
            plugin_id,
            &state_json,
            &config_json,
            &ctx.tenant.tenant_id,
            &ctx.tenant.session_id,
            &ctx.task_id,
            &ctx.pipeline_id.to_string(),
            host,
        );

        // execute 返回 state_updates JSON 字符串 → 解析为 PluginResult。
        let plugin_result = result.and_then(|state_updates_json| {
            let state_updates: std::collections::HashMap<String, serde_json::Value> =
                serde_json::from_str(&state_updates_json).map_err(|e| PluginError {
                    message: format!("native plugin state_updates parse failed: {}", e),
                    code: Some("NATIVE_OUTPUT_PARSE".to_string()),
                    source: Some("plugin-invoker".to_string()),
                })?;
            Ok(PluginResult {
                state_updates,
                route_signal: None,
                skip_remaining: false,
                error: None,
            })
        });

        // 包装成 Result<Result<...>, _> 复用 notify_if_crash。
        let as_result = plugin_result;
        self.notify_if_crash(plugin_id, &as_result);
        as_result
    }

    /// WASM 插件 pipeline 调用：config 注入 + 热重载 + 崩溃回调，与 sidecar 对齐。
    async fn invoke_wasm_pipeline(
        &self,
        plugin_id: &str,
        manifest: &PluginManifest,
        ctx: &PluginContext,
    ) -> Result<PluginResult, PluginError> {
        let runtime = self.wasm_runtime_or_err(plugin_id)?;

        // 热重载：指纹变化则重新编译模块（wasm 可安全 drop Store，无 dlclose 坑）。
        if self.is_plugin_stale(plugin_id, manifest).await {
            if let Some(dir) = self.loader.get_plugin_dir(plugin_id) {
                let artifact = manifest
                    .wasm
                    .as_ref()
                    .map(|w| w.artifact.as_str())
                    .unwrap_or("");
                let wasm_path = std::path::PathBuf::from(dir).join(artifact);
                if let Err(e) = runtime.reload(plugin_id, &wasm_path) {
                    warn!("Wasm hot reload failed for {}: {}", plugin_id, e);
                }
            }
        }

        self.load_wasm(runtime, plugin_id, manifest)?;
        let input = crate::shared::build_plugin_input(self.loader.as_ref(), ctx, manifest).await?;
        let result = runtime.invoke(plugin_id, &input);

        if result.is_ok() {
            self.touch_wasm_last_used(plugin_id);
        } else {
            // trap 视为崩溃，触发回调（与 sidecar 进程崩溃一致）。
            self.notify_crash(plugin_id);
        }
        result
    }

    /// 原生插件 tool 调用：inputs 作 state + config 注入 + 崩溃回调。
    async fn invoke_native_tool(
        &self,
        _plugin_id: &str,
        _manifest: &PluginManifest,
        _tool_name: &str,
        _inputs: &Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        // 当前原生插件仅支持 pipeline 类型（经 PipelinePlugin trait）。
        // InProcess 工具插件尚未支持——工具（bash 等）目前都是 sidecar。
        // 待将来出现原生工具插件时，扩展 PipelinePlugin trait 之外的 Tool 契约。
        Err(PluginError {
            message: "InProcess tool plugin not yet supported (only pipeline plugins)".to_string(),
            code: Some("NATIVE_TOOL_UNSUPPORTED".to_string()),
            source: Some("plugin-invoker".to_string()),
        })
    }

    /// WASM 插件 tool 调用：inputs 作 state + config 注入 + 崩溃回调。
    async fn invoke_wasm_tool(
        &self,
        plugin_id: &str,
        manifest: &PluginManifest,
        _tool_name: &str,
        inputs: &Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        let runtime = self.wasm_runtime_or_err(plugin_id)?;
        self.load_wasm(runtime, plugin_id, manifest)?;
        let config = crate::shared::injected_config(self.loader.as_ref(), manifest).await?;
        let input = json!({ "state": inputs, "config": config });
        let result = runtime.invoke(plugin_id, &input);
        if result.is_ok() {
            self.touch_wasm_last_used(plugin_id);
        } else {
            self.notify_crash(plugin_id);
        }
        let plugin_result = result?;
        Ok(ToolExecutionResult::success(
            serde_json::to_value(plugin_result).unwrap_or_default(),
        ))
    }

    /// 设置 Capability 路由器（启用 sidecar→内核反向调用）。
    ///
    /// 必须在 engine 创建后调用（路由器需要 engine 句柄）。
    /// 之后新建的 MCP 客户端会自动带上路由器；已连接的客户端下次重连时生效。
    pub fn set_router(&self, router: Arc<dyn CapabilityRouter>) {
        // sidecar：存 router，新建 MCP client 时带上（PluginScopedRouter）。
        // wasm：用 router 建 host_registry 注入 WasmRuntime——guest 的 host.call
        //       import 即转发到 router（与 sidecar JSON-RPC 反向调用同一 router，对齐）。
        // native：router 存此，host_call 入口经 capability::call_capability 转发。
        // 一行接通三种 host_type 的 capability 反向调用。
        if let Some(rt) = &self.wasm_runtime {
            rt.set_host_registry(crate::capability::wasm_host_registry(router.clone()));
            rt.set_capability_checker(Arc::new(crate::capability::AllowHostCallChecker));
        }
        *self.router.write() = Some(router);
    }

    /// 注册崩溃回调。
    pub fn on_crash(&self, callback: Arc<dyn Fn(&str) + Send + Sync>) {
        self.crash_callbacks.write().push(callback);
    }

    /// 通知崩溃回调。
    fn notify_crash(&self, plugin_id: &str) {
        let callbacks = self.crash_callbacks.read();
        for cb in callbacks.iter() {
            cb(plugin_id);
        }
    }

    /// 获取或创建 MCP 客户端（按需加载）。
    ///
    /// 并发安全：用 per-plugin spawn 锁（single-flight）保证同一 plugin_id 的 spawn
    /// 串行化，修复「并发请求竞态创建多个孤儿 sidecar」的进程泄漏。
    async fn get_or_create_mcp_client(
        &self,
        manifest: &PluginManifest,
    ) -> Result<Arc<tokio::sync::Mutex<McpClient>>, PluginError> {
        // Fast path：无锁查缓存，命中且存活直接返回（热路径，避开 spawn 锁开销）。
        {
            let cached = {
                let clients = self.mcp_clients.read();
                clients.get(&manifest.id).cloned()
            };
            if let Some(client) = cached {
                let mut client_guard = client.lock().await;
                if client_guard.is_alive().await {
                    // ── Pull 热加载：TTL 门 + 指纹比对 ──
                    // 缓存进程存活时，检查插件代码/配置是否变更。TTL 内跳过 stat（零开销），
                    // TTL 过后 stat 目录 mtime，发现变化则 kill 旧进程走下面的 respawn 路径
                    // 加载新代码/配置；没变则直接复用缓存进程。未用到也不检测——纯按需 pull。
                    if self.is_plugin_stale(&manifest.id, manifest).await {
                        info!(
                            "Plugin code/config changed, reloading sidecar: {}",
                            manifest.id
                        );
                        // 复用下方「进程已崩溃」的 kill+remove 逻辑：kill 旧进程后
                        // 自然进入 slow path respawn 新进程（加载最新磁盘代码）。
                        let _ = client_guard.kill().await;
                        drop(client_guard);
                        self.mcp_clients.write().remove(&manifest.id);
                        // 不调 notify_crash（这不是崩溃，是主动热更新）
                    } else {
                        self.touch_last_used(&manifest.id);
                        return Ok(Arc::clone(&client));
                    }
                } else {
                    // 进程已崩溃——显式 kill 旧客户端再创建新的
                    error!("Plugin process crashed: {}", manifest.id);
                    let _ = client_guard.kill().await;
                    drop(client_guard);
                    self.notify_crash(&manifest.id);
                    self.mcp_clients.write().remove(&manifest.id);
                }
            }
        }

        // Slow path：拿 per-plugin spawn 锁，串行化同 plugin_id 的 spawn。
        // 取（或创建）该 plugin 的专用锁 Arc——锁本身常驻 spawn_locks，不随调用释放。
        let spawn_lock = {
            let mut locks = self.spawn_locks.write();
            locks
                .entry(manifest.id.clone())
                .or_insert_with(|| Arc::new(tokio::sync::Mutex::new(())))
                .clone()
        };
        let _spawn_guard = spawn_lock.lock().await;

        // Double-check：持 spawn 锁后再查缓存——前一个持锁者可能已创建好 client。
        // 命中则直接复用，避免重复 spawn。
        {
            let cached = {
                let clients = self.mcp_clients.read();
                clients.get(&manifest.id).cloned()
            };
            if let Some(client) = cached {
                let mut client_guard = client.lock().await;
                if client_guard.is_alive().await {
                    self.touch_last_used(&manifest.id);
                    return Ok(Arc::clone(&client));
                }
                // 极端情况：double-check 时又崩溃——kill 后继续 spawn
                let _ = client_guard.kill().await;
                drop(client_guard);
                self.mcp_clients.write().remove(&manifest.id);
            }
        }

        // 创建新的 MCP 客户端（持有 spawn 锁，保证串行）
        let (command, args) = self.parse_entry(&manifest.entry)?;
        let mut client = McpClient::new_stdio(command, args)
            // plugin_id 用于 stderr 转发时区分 sidecar 日志来源（[plugin_id] 前缀）。
            .with_plugin_id(&manifest.id);

        // 应用 Capability 路由器（启用 sidecar→内核反向调用通道）。
        // 用 PluginScopedRouter 包装，把 manifest.id 注入每次反向调用的 params，
        // 内核侧 metrics.record 据此做命名空间（监控设计 §三 通道2 + §十 安全）。
        {
            let router_guard = self.router.read();
            if let Some(router) = router_guard.as_ref() {
                let scoped: Arc<dyn CapabilityRouter> = Arc::new(PluginScopedRouter {
                    plugin_id: manifest.id.clone(),
                    inner: Arc::clone(router),
                });
                client = client.with_router(scoped);
            }
        }

        // 设置工作目录为插件目录（确保 server.py 等相对路径可解析）
        if let Some(plugin_dir) = self.loader.get_plugin_dir(&manifest.id) {
            client = client.with_working_dir(plugin_dir);
        }

        // 注入 PYTHONPATH：把 project_root（及 project_root/src）加进 sidecar 子进程搜索路径，
        // 让两种 import 写法都能解析（见 resolve_pythonpath_src 注释）。
        //
        // 来源优先级：
        // ① set_pythonpath_src 显式注入（内核构造期由 main 传入，最可靠）；
        // ② AGENTOS_PLUGINS_DIR 环境变量推算（plugins/shared → project_root，向后兼容）。
        // 两者都不可用时收空 PYTHONPATH——此时依赖 src.* / config.* 的插件会启动失败
        // （import error），但不会静默退化（早暴露比晚卡死好）。
        let mut extra_env: Vec<(String, String)> = Vec::new();
        if let Some(new_path) = self.resolve_pythonpath_src() {
            extra_env.push(("PYTHONPATH".to_string(), new_path));
        }
        // 注入日志配置 env（进程级常量，适合走 env；per-request 上下文走 JSON-RPC）。
        // 仅当父进程已设置时透传，否则让 sidecar SDK 用其默认（INFO + stderr）。
        // SDK 启动时读这些 env 调用 setup_logging，使 sidecar 日志走统一基础设施。
        for key in &["LOG_LEVEL", "LOG_JSON", "LOG_FORMAT"] {
            if let Ok(val) = std::env::var(key) {
                if !val.is_empty() {
                    extra_env.push(((*key).to_string(), val));
                }
            }
        }
        if !extra_env.is_empty() {
            client = client.with_extra_env(extra_env);
        }

        client.connect().await.map_err(|e| PluginError {
            message: format!("MCP connect failed: {}", e),
            code: Some("MCP_CONNECT_FAILED".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;

        // initialize 握手（携带插件配置）
        // 配置加载失败时分级处理：IO 错误（目录不存在等）可降级为空配置；
        // 解析错误（YAML 语法错误）应报错，让插件启动失败比悄悄降级更安全。
        let full_config = match self.loader.load_config().await {
            Ok(config) => config,
            Err(e) => {
                if e
                    .code
                    .as_deref()
                    .map(|c| c.contains("PARSE"))
                    .unwrap_or(false)
                {
                    return Err(PluginError {
                        message: format!("Plugin config parse error: {}", e),
                        code: Some("CONFIG_PARSE_ERROR".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    });
                }
                warn!("Failed to load plugin config, using empty: {}", e);
                serde_json::json!({})
            }
        };
        // 按需注入（ADR §4.3，P6）：只走 config_files 映射；未声明则收空配置。
        // 避免把全系统配置（含其他插件凭证）泄漏给每个 sidecar。
        // 复用 shared::build_injected_config——native/wasm 分支也走同一函数，三家对齐。
        let config = crate::shared::build_injected_config(&full_config, manifest);
        client.initialize(&config).await.map_err(|e| PluginError {
            message: format!("MCP initialize failed: {}", e),
            code: Some("MCP_INIT_FAILED".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;

        // 发送 on_load 通知——触发 Python 插件的 @plugin.on_load 回调，
        // 初始化插件实例（如 _instance = MyPlugin(config)）。
        // 不等待响应（fire-and-forget）；失败仅 warn 不阻断。
        let _ = client
            .send_notification("notifications/on_load", Some(config.clone()))
            .await
            .inspect_err(|e| {
                warn!("on_load notification failed for {}: {}", manifest.id, e)
            });

        info!(
            "MCP client connected and initialized: plugin={}",
            manifest.id
        );

        let client_arc = Arc::new(tokio::sync::Mutex::new(client));

        // 缓存
        {
            let mut clients = self.mcp_clients.write();
            clients.insert(manifest.id.clone(), Arc::clone(&client_arc));
        }
        // 新 spawn 即"活跃"，记录最后调用时刻（空闲软卸载依据）
        self.touch_last_used(&manifest.id);

        Ok(client_arc)
    }

    /// 解析 entry 字段为 command + args。
    ///
    /// entry 格式：`python3 -m my_plugin` 或 `/usr/bin/python3 server.py`
    fn parse_entry(&self, entry: &str) -> Result<(String, Vec<String>), PluginError> {
        let parts: Vec<&str> = entry.split_whitespace().collect();
        if parts.is_empty() {
            return Err(PluginError {
                message: "empty entry".to_string(),
                code: Some("EMPTY_ENTRY".to_string()),
                source: Some("plugin-invoker".to_string()),
            });
        }
        let command = parts[0].to_string();
        let args = parts[1..].iter().map(|s| s.to_string()).collect();
        Ok((command, args))
    }

    /// 检查插件进程健康状态。
    pub async fn check_health(&self, plugin_id: &str) -> bool {
        let client_arc = {
            let clients = self.mcp_clients.read();
            clients.get(plugin_id).cloned()
        };
        if let Some(client) = client_arc {
            let guard = client.lock().await;
            guard.is_alive().await
        } else {
            false
        }
    }

    /// 插件权限声明的前置日志校验（P2-2）。
    ///
    /// 0.2 只做声明 + 日志告警，不做硬 enforce（filesystem/system_calls 留 0.3 沙箱）。
    /// 当前检测项：
    /// - 若 manifest 声明了 network 权限（`permissions.network.allowed_hosts` 非空），
    ///   则认为该插件可能联网；这是声明性记录，不阻断调用。
    /// - 若 manifest 同时声明了 network 权限但 `allowed_hosts` 为空，
    ///   说明声明含糊（声称要联网却未指定可信主机），记 warning。
    ///
    /// 该函数不返回错误，**永远不阻断** invoke 流程。
    fn check_permissions(&self, plugin_id: &str, manifest: &PluginManifest) {
        let perms = &manifest.permissions;

        // 记录声明：network/filesystem/env_vars/system_calls 是否声明
        let has_network = !perms.network.allowed_hosts.is_empty();
        let has_fs =
            !perms.filesystem.read_paths.is_empty() || !perms.filesystem.write_paths.is_empty();
        let has_env = !perms.env_vars.is_empty();
        let has_syscalls = !perms.system_calls.is_empty();

        info!(
            plugin_id = plugin_id,
            network = has_network,
            filesystem = has_fs,
            env_vars = has_env,
            system_calls = has_syscalls,
            "Plugin permission declaration"
        );

        // 声明含糊检测：声明了要联网（有非空 host 列表）说明确实要联网；
        // 若声明了 network 权限意图但 allowed_hosts 为空，
        // 说明声明不完整，记 warning（不阻断）。
        // 注：0.2 不强制 enforce，仅日志留痕供审计。
        if has_network {
            info!(
                plugin_id = plugin_id,
                hosts = ?perms.network.allowed_hosts,
                "Plugin declared network access"
            );
        }
    }

    /// 刷新 sidecar 的最后调用时刻（调用即"活跃"，重置空闲计时）。
    fn touch_last_used(&self, plugin_id: &str) {
        self.last_used
            .write()
            .insert(plugin_id.to_string(), Instant::now());
    }

    /// 刷新 wasm 的最后调用时刻。
    fn touch_wasm_last_used(&self, plugin_id: &str) {
        self.wasm_last_used
            .write()
            .insert(plugin_id.to_string(), Instant::now());
    }

    /// 统一软卸载：按 host_type 分流，进程/模块 kill 但 manifest 描述保留（下次调用重新 spawn）。
    ///
    /// - sidecar：复用 force_unload_impl（kill 进程 + 清缓存）
    /// - wasm：调 wasm_runtime.unload()（释放模块）+ 清 wasm_last_used
    /// - InProcess（rust 原生 cdylib）：跳过（Windows dlclose 限制），返回 false
    ///
    /// 返回 true 表示已卸载，false 表示未卸载（不支持的类型或未加载）。
    pub async fn unload_if_idle(&self, plugin_id: &str) -> bool {
        // 先查 manifest 的 host_type（loader 缓存里有）
        let host_type = {
            let m = self.loader.load(plugin_id).await;
            match m {
                Ok(loaded) => loaded.manifest.host_type,
                Err(_) => {
                    // 加载失败也可能意味着已不在——尝试清 sidecar 缓存
                    return self.force_unload_impl(plugin_id).await.is_ok();
                }
            }
        };

        match host_type {
            HostType::Sidecar => self.force_unload_impl(plugin_id).await.is_ok(),
            HostType::Wasm => {
                if let Some(rt) = &self.wasm_runtime {
                    match rt.unload(plugin_id) {
                        Ok(()) => {
                            self.wasm_last_used.write().remove(plugin_id);
                            info!("Plugin idle-unloaded (wasm): {}", plugin_id);
                            true
                        }
                        Err(e) => {
                            warn!("Failed to idle-unload wasm {}: {}", plugin_id, e);
                            false
                        }
                    }
                } else {
                    false
                }
            }
            HostType::InProcess => {
                // rust 原生 cdylib：dlclose 限制，不自动卸载
                tracing::debug!(
                    "Skip idle-unload for inprocess plugin {} (dlclose limit)",
                    plugin_id
                );
                false
            }
        }
    }

    /// 启动后台空闲软卸载 GC 任务。
    ///
    /// 每 30s 扫描 last_used + wasm_last_used，对空闲超过阈值的插件调 unload_if_idle
    /// （sidecar kill 进程 / wasm 释放模块，manifest 描述保留，下次调用重新 spawn）。
    /// 实现 trait 文档 :652 早就声明的"空闲超时自动卸载"设计原则。
    ///
    /// 必须用 Arc<Self> 调用（后台任务需 'static 持有 invoker）。在 main 启动期调一次。
    pub fn start_idle_gc(self: &Arc<Self>) {
        let invoker = Arc::clone(self);
        tokio::spawn(async move {
            // 扫描间隔：30s。比默认 300s 阈值短得多，保证空闲插件能在阈值后一个周期内被回收。
            let mut interval = tokio::time::interval(Duration::from_secs(30));
            interval.tick().await; // 跳过立即触发的第一次
            loop {
                interval.tick().await;
                invoker.run_idle_gc_pass().await;
            }
        });
        info!("Plugin idle-unload GC task started (scan every 30s)");
    }

    /// 单次 GC 扫描：收集所有已加载插件的 id + 最后调用时刻，对超时的软卸载。
    async fn run_idle_gc_pass(&self) {
        // 快照当前所有"活跃"插件 id（sidecar + wasm 合并），避免长时间持锁
        let candidates: Vec<String> = {
            let sidecar_ids: Vec<String> =
                self.last_used.read().keys().cloned().collect();
            let wasm_ids: Vec<String> =
                self.wasm_last_used.read().keys().cloned().collect();
            let mut all: Vec<String> = sidecar_ids;
            all.extend(wasm_ids);
            all.sort();
            all.dedup();
            all
        };

        let now = Instant::now();
        for plugin_id in candidates {
            // sidecar 空闲判定
            let sidecar_idle = self
                .last_used
                .read()
                .get(&plugin_id)
                .map(|t| now.duration_since(*t).as_secs());
            // wasm 空闲判定
            let wasm_idle = self
                .wasm_last_used
                .read()
                .get(&plugin_id)
                .map(|t| now.duration_since(*t).as_secs());

            let idle_secs = sidecar_idle.max(wasm_idle).unwrap_or(0);
            if idle_secs == 0 {
                continue;
            }
            let threshold = self.idle_timeout_secs_sync(&plugin_id);
            if idle_secs > threshold {
                info!(
                    plugin_id = %plugin_id,
                    idle_secs = idle_secs,
                    threshold = threshold,
                    "Plugin idle-unloading (soft): exceeds idle timeout"
                );
                let _ = self.unload_if_idle(&plugin_id).await;
            }
        }
    }

    /// idle_timeout_secs 的同步包装（run_idle_gc_pass 已在 async 上下文，但 load 是 async，
    /// 这里用同步读缓存的方式避免嵌套 await 复杂度——阈值取默认或环境变量即可，精确性非关键）。
    fn idle_timeout_secs_sync(&self, _plugin_id: &str) -> u64 {
        // 全局环境变量优先
        if let Ok(v) = std::env::var("AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS") {
            if let Ok(secs) = v.parse::<u64>() {
                if secs > 0 {
                    return secs;
                }
            }
        }
        // 默认 300s
        agentos_core::traits::default_idle_timeout()
    }

    /// 强制卸载插件的实现（供 trait 方法 force_unload 与内部热重载复用）。
    pub async fn force_unload_impl(&self, plugin_id: &str) -> Result<(), PluginError> {
        let client_arc = {
            let mut clients = self.mcp_clients.write();
            clients.remove(plugin_id)
        };

        if let Some(client_arc) = client_arc {
            let mut client = client_arc.lock().await;
            if let Err(e) = client.kill().await {
                warn!("Failed to kill crashed plugin {}: {}", plugin_id, e);
            }
        }

        // 也通过 loader 卸载
        let _ = self.loader.unload(plugin_id).await;

        // 清除指纹缓存 + last_used，下次调用重新计算并 respawn
        self.fingerprints.write().remove(plugin_id);
        self.last_used.write().remove(plugin_id);

        info!("Force unloaded plugin: {}", plugin_id);
        Ok(())
    }

    /// Pull 热加载核心：判断缓存的 sidecar 进程是否因代码/配置变更而过期。
    ///
    /// 返回 `true` 表示插件已更新、需要 kill 旧进程 respawn 加载新版本；
    /// 返回 `false` 表示可直接复用缓存进程。
    ///
    /// 策略（双层短路，热路径零开销）：
    /// 1. **TTL 门**：距上次检测不足 `PLUGIN_FINGERPRINT_TTL`（1s）→ 直接返回 false。
    ///    高频调用同一插件时避免反复 stat 文件系统。
    /// 2. **指纹比对**：TTL 过期后 stat 插件目录 + config_files 的 mtime，与缓存指纹比对。
    ///    相同 → 刷新时刻戳返回 false（没更新）；不同 → 更新缓存返回 true（已更新）。
    ///
    /// 首次调用（缓存无记录）→ 计算指纹写入缓存返回 false（首次走 spawn，不是热更新）。
    async fn is_plugin_stale(&self, plugin_id: &str, manifest: &PluginManifest) -> bool {
        let now = Instant::now();
        // TTL 门 + 指纹比对在同一把读锁下原子完成（计算指纹前先快照缓存）。
        let cached = self.fingerprints.read().get(plugin_id).cloned();
        match cached {
            None => {
                // 首次：写入指纹，不算过期（此时进程刚 spawn，必是最新）
                let fp = self.resolve_fingerprint(manifest);
                self.fingerprints
                    .write()
                    .insert(plugin_id.to_string(), (fp, now));
                false
            }
            Some((old_fp, last_check)) => {
                // TTL 门：未到期直接复用
                if now.duration_since(last_check) < PLUGIN_FINGERPRINT_TTL {
                    return false;
                }
                // TTL 过期：stat 当前指纹比对
                let new_fp = self.resolve_fingerprint(manifest);
                let stale = new_fp != old_fp;
                // 无论是否过期都刷新检测时刻；指纹变了才更新缓存指纹
                let to_store = if stale { new_fp } else { old_fp };
                self.fingerprints
                    .write()
                    .insert(plugin_id.to_string(), (to_store, now));
                stale
            }
        }
    }

    /// 解析插件指纹：复用 loader 的 get_plugin_dir 拿目录，再调 compute_plugin_fingerprint。
    /// 拿不到目录（插件已被移除）→ 返回 0，调用方会把这当作"指纹变化"触发 respawn，
    /// respawn 时会正常报错（插件已不存在），行为合理。
    fn resolve_fingerprint(&self, manifest: &PluginManifest) -> u64 {
        match self.loader.get_plugin_dir(&manifest.id) {
            Some(dir) => compute_plugin_fingerprint(std::path::Path::new(&dir), manifest),
            None => 0,
        }
    }
}

#[async_trait]
impl PluginInvoker for PluginInvokerImpl {
    /// 调用管道插件执行。
    ///
    /// 按 manifest 的 host_type 透明分发：
    /// - InProcess: 经 NativePluginLoader 加载 cdylib 并走 C-ABI 调用（JSON 契约）
    /// - Wasm: 经 WasmRuntime 加载执行 .wasm（JSON 经线性内存）
    /// - Sidecar: 通过 MCP 客户端走 JSON-RPC tools/call
    async fn invoke_pipeline_plugin(
        &self,
        plugin_id: &str,
        ctx: &PluginContext,
    ) -> Result<PluginResult, PluginError> {
        let loaded = self.loader.load(plugin_id).await?;
        let manifest = &loaded.manifest;

        // P2-2 插件权限声明前置日志校验（不阻断）
        self.check_permissions(plugin_id, manifest);

        match manifest.host_type {
            HostType::InProcess => {
                // task_11 N2：经 NativePluginLoader 加载 cdylib 并通过 C-ABI 调用。
                // config 注入与 sidecar 同逻辑（shared::build_plugin_input）——三家对齐。
                self.invoke_native_pipeline(plugin_id, manifest, ctx).await
            }
            HostType::Wasm => {
                // task_11 N7：经 WasmRuntime 加载/执行 .wasm。
                // config 注入同 sidecar——三家对齐。
                self.invoke_wasm_pipeline(plugin_id, manifest, ctx).await
            }
            HostType::Sidecar => {
                // ADR 附录 D③（P6 命名治理）：从 manifest.invoke_entry 取 MCP 入口名
                // （如 "context_build.execute"）。不再回退 capabilities.tools 或字面量
                // "execute"——缺 invoke_entry 是 manifest 错误，必须显式暴露。
                // 启动期 discover 聚合校验是主门；此处为运行期防线（深度防御）。
                let tool_name = manifest.invoke_entry.as_deref().ok_or_else(|| PluginError {
                    message: format!(
                        "pipeline plugin '{}' missing manifest.invoke_entry (ADR 附录 D②)",
                        plugin_id
                    ),
                    code: Some("MISSING_INVOKE_ENTRY".to_string()),
                    source: Some("plugin-invoker".to_string()),
                })?;

                // Sidecar 模式：通过 MCP 客户端调用
                let client_arc = self.get_or_create_mcp_client(manifest).await?;
                let client = client_arc.lock().await;

                // 检查进程健康
                if !client.is_alive().await {
                    drop(client);
                    self.notify_crash(plugin_id);
                    return Err(PluginError {
                        message: format!("plugin process crashed: {}", plugin_id),
                        code: Some("PLUGIN_CRASHED".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    });
                }

                // 调用 tools/call
                // _log_ctx：per-request 日志上下文，从 ctx.state 抽取（真实数据所在，
                // 见 server.rs 把 pipeline_id/session_id 写进 state）。SDK 在调 handler
                // 前 LogContext.bind，使 sidecar 日志能带 pipeline_id 关联内核日志。
                let log_ctx = serde_json::json!({
                    "pipeline_id": ctx.state.get("pipeline_id").cloned().unwrap_or(Value::Null),
                    "request_id": ctx.state.get("request_id").cloned().unwrap_or(Value::Null),
                    "session_id": ctx.state.get("session_id").cloned().unwrap_or(Value::Null),
                    "agent_name": ctx.state.get("agent_id").cloned().unwrap_or(Value::Null),
                });
                let tool_args = serde_json::json!({
                    "state": ctx.state,
                    "config": ctx.config,
                    "_log_ctx": log_ctx,
                });

                let result = client.call_tool(tool_name, &tool_args).await.map_err(|e| {
                    let is_crash = matches!(e, McpError::Transport { .. });
                    if is_crash {
                        drop(client);
                        self.notify_crash(plugin_id);
                    }
                    PluginError {
                        message: format!("MCP call failed: {}", e),
                        code: Some("MCP_CALL_FAILED".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    }
                })?;

                // 解析 MCP 响应——Python SDK 返回格式为：
                // { "content": [{ "type": "text", "text": "<json_string>" }], "isError": false }
                // 提取 content[0].text 并反序列化为 PluginResult
                let inner = extract_mcp_content(&result);
                let plugin_result: PluginResult = serde_json::from_value(inner).map_err(|e| {
                    PluginError {
                        message: format!("failed to parse MCP response as PluginResult: {}", e),
                        code: Some("PARSE_ERROR".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    }
                })?;

                Ok(plugin_result)
            }
        }
    }

    /// 调用工具插件执行。
    ///
    /// 按 host_type 透明分发：
    /// - InProcess: 经 NativePluginLoader 加载 cdylib 走 C-ABI（inputs 作为 state）
    /// - Wasm: 经 WasmRuntime 加载执行 .wasm（inputs 作为 state）
    /// - Sidecar: 通过 MCP 客户端走 JSON-RPC tools/call
    async fn invoke_tool(
        &self,
        plugin_id: &str,
        tool_name: &str,
        inputs: &serde_json::Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        let loaded = self.loader.load(plugin_id).await?;
        let manifest = &loaded.manifest;

        // P2-2 插件权限声明前置日志校验（不阻断）
        self.check_permissions(plugin_id, manifest);

        match manifest.host_type {
            HostType::InProcess => {
                // task_11 N2：原生工具插件——inputs 作为 state，config 同 pipeline 路径注入。
                self.invoke_native_tool(plugin_id, manifest, tool_name, inputs).await
            }
            HostType::Wasm => {
                // task_11 N7：WASM 工具插件——inputs 作为 state，config 同 pipeline 路径注入。
                self.invoke_wasm_tool(plugin_id, manifest, tool_name, inputs).await
            }
            HostType::Sidecar => {
                let client_arc = self.get_or_create_mcp_client(manifest).await?;
                let client = client_arc.lock().await;

                if !client.is_alive().await {
                    drop(client);
                    self.notify_crash(plugin_id);
                    return Err(PluginError {
                        message: format!("plugin process crashed: {}", plugin_id),
                        code: Some("PLUGIN_CRASHED".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    });
                }

                let result =
                    client
                        .call_tool(tool_name, inputs)
                        .await
                        .map_err(|e| PluginError {
                            message: format!("MCP tool call failed: {}", e),
                            code: Some("MCP_TOOL_CALL_FAILED".to_string()),
                            source: Some("plugin-invoker".to_string()),
                        })?;

                // 解析 MCP 响应：extract_mcp_content 已提取 content[0].text 并反序列化为
                // 工具 handler 的原始返回（业务 dict，如 {"result": ...} / {"error": ...}）。
                //
                // 工具返回的是**纯业务数据**，不带 ToolExecutionResult 的 success/data 信封
                // （那是内核内部结构，插件不该感知）。所以这里不能直接 from_value 成
                // ToolExecutionResult——否则业务 dict 缺 success 字段会报
                // "missing field `success`"。这里按三层优先级智能构造：
                //   ① 若 isError=true（已被 extract 转成 {"error": "..."}）→ failure；
                //   ② 若返回值恰好已是 ToolExecutionResult 信封（带 success 字段）→ 直接用；
                //   ③ 否则视为纯业务数据 → success(data=inner)，与 pipeline 路径
                //      （ToolExecutionResult::success(to_value(plugin_result))）对齐。
                let inner = extract_mcp_content(&result);
                // 决策树（先判 success 字段，再判 error 字段，最后按纯数据兜底）：
                //   ② 返回已是 ToolExecutionResult 信封（带 success）→ 直接 from_value
                //   ① MCP isError=true / 工具返回 {"error":"..."}（无 success）→ failure
                //   ③ 纯业务数据 → success(data=inner)
                let tool_result = if inner.get("success").is_some() {
                    // ② 返回值已是 ToolExecutionResult 信封
                    serde_json::from_value(inner).map_err(|e| PluginError {
                        message: format!("failed to parse MCP response as ToolExecutionResult: {}", e),
                        code: Some("PARSE_ERROR".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    })?
                } else if let Some(err) = inner.get("error").and_then(|v| v.as_str()) {
                    // ① MCP isError=true 或工具自身返回 {"error": "..."} 且无 success 字段
                    ToolExecutionResult::failure(err)
                } else {
                    // ③ 纯业务数据 → 包成 success 信封
                    ToolExecutionResult::success(inner)
                };

                Ok(tool_result)
            }
        }
    }

    /// 发送生命周期钩子事件到指定插件。
    async fn send_lifecycle_hook(
        &self,
        plugin_id: &str,
        hook: LifecycleHook,
        context: &HookContext,
    ) -> Result<(), PluginError> {
        let loaded = self.loader.load(plugin_id).await?;
        let manifest = &loaded.manifest;

        // 组合插件不需要生命周期钩子（ADR ⑥）
        if manifest.plugin_type == PluginType::Composite {
            return Ok(());
        }

        let hook_name = match hook {
            LifecycleHook::OnLoad => "on_load",
            LifecycleHook::OnUnload => "on_unload",
            LifecycleHook::OnPipelineStart => "on_pipeline_start",
            LifecycleHook::OnPipelineEnd => "on_pipeline_end",
            LifecycleHook::OnError => "on_error",
        };
        let tags = serde_json::to_value(context.tags()).unwrap_or_default();

        match manifest.host_type {
            HostType::Sidecar => {
                // Sidecar：经 MCP notification 发送（fire-and-forget）。
                if let Ok(client_arc) = self.get_or_create_mcp_client(manifest).await {
                    let client = client_arc.lock().await;
                    if client.is_alive().await {
                        let hook_method = format!("notifications/{hook_name}");
                        if let Err(e) = client
                            .send_notification(&hook_method, Some(tags))
                            .await
                        {
                            warn!("Lifecycle notification failed for {}: {}", plugin_id, e);
                        }
                    }
                }
            }
            HostType::InProcess | HostType::Wasm => {
                // Native/Wasm：没有 MCP 通知通道，钩子经 execute 传递——PluginInput 带
                // `hook` 字段（值为钩子名）+ config。插件 SDK 见到 hook 字段走钩子逻辑。
                // 错误仅 warn（与 sidecar 的 fire-and-forget 语义一致，不阻断管道）。
                if let Err(e) = self.send_hook_via_execute(plugin_id, manifest, hook_name, &tags).await {
                    warn!("Lifecycle hook {hook_name} failed for {}: {}", plugin_id, e);
                }
            }
        }

        Ok(())
    }

    /// 强制卸载插件（覆盖 trait 默认实现）。
    /// 转发到 force_unload_impl：kill sidecar + 清缓存，下次调用自动 respawn 加载新代码。
    async fn force_unload(&self, plugin_id: &str) -> Result<(), PluginError> {
        self.force_unload_impl(plugin_id).await
    }

    /// 重新扫描插件目录（覆盖 trait 默认实现）。
    ///
    /// 从 AGENTOS_PLUGINS_DIR 递归收集含 plugin.json 的目录的父目录作为 roots
    /// （对齐 main 启动期的 discover_plugin_roots 逻辑，支持 tools/<plugin>/ 嵌套），
    /// 再传给 loader.discover。幂等：cache.clear + 重插，不杀已 spawn 的进程。
    /// 返回全量 manifest，由调用方对比已知集合找新增。
    async fn discover_new_plugins(&self) -> Result<Vec<PluginManifest>, PluginError> {
        let roots = self.collect_plugin_roots();
        let root_refs: Vec<&str> = roots.iter().map(|s| s.as_str()).collect();
        self.loader.discover(&root_refs).await
    }
}

impl PluginInvokerImpl {
    /// 经 execute 向 native/wasm 插件传递生命周期钩子。
    ///
    /// 这两类插件没有 MCP 通知通道，约定用 PluginInput 的 `hook` 字段表达钩子：
    /// 插件 SDK / guest 见到 `hook` 字段就走对应钩子逻辑，而非正常 execute。
    /// config 仍按 config_files 注入（与 sidecar 的 on_load 带 config 对齐）。
    async fn send_hook_via_execute(
        &self,
        plugin_id: &str,
        manifest: &PluginManifest,
        hook_name: &str,
        tags: &Value,
    ) -> Result<(), PluginError> {
        let config = crate::shared::injected_config(self.loader.as_ref(), manifest).await?;
        let input = json!({
            "state": tags,
            "config": config,
            "hook": hook_name,
        });
        match manifest.host_type {
            HostType::InProcess => {
                // abi_stable trait 对象模型：生命周期钩子经 PipelinePlugin trait 之外
                // 的独立契约传递（当前 tool_core 无 on_load/on_unload 需求，暂 no-op）。
                // 仅确保插件已加载（保持热重载检测一致性）。
                if let Some(loader) = self.native_loader.as_ref() {
                    self.load_native(loader, plugin_id, manifest)?;
                }
            }
            HostType::Wasm => {
                if let Some(runtime) = self.wasm_runtime.as_ref() {
                    let wasm_path = self.resolve_wasm_artifact(plugin_id, manifest)?;
                    runtime.load(plugin_id, &wasm_path)?;
                    runtime.invoke(plugin_id, &input)?;
                }
            }
            _ => {}
        }
        Ok(())
    }

    /// 递归收集插件 roots：从 AGENTOS_PLUGINS_DIR 出发，找所有含 plugin.json 的目录，
    /// 取其父目录去重作为 discover 的 root_paths（对齐 main 的 discover_plugin_roots）。
    fn collect_plugin_roots(&self) -> Vec<String> {
        let base = match std::env::var("AGENTOS_PLUGINS_DIR") {
            Ok(d) => d,
            Err(_) => return Vec::new(),
        };
        let base_path = std::path::Path::new(&base);
        let mut plugin_dirs: Vec<String> = Vec::new();
        Self::collect_plugin_dirs(base_path, &mut plugin_dirs);
        // 取父目录去重
        let mut parent_set = std::collections::HashSet::new();
        for dir in &plugin_dirs {
            if let Some(parent) = std::path::Path::new(dir).parent() {
                if let Some(s) = parent.to_str() {
                    parent_set.insert(s.to_string());
                }
            }
        }
        parent_set.into_iter().collect()
    }

    /// 递归找含 plugin.json/plugin.yaml 的目录（对齐 main 的 collect_plugin_dirs）。
    fn collect_plugin_dirs(dir: &std::path::Path, dirs: &mut Vec<String>) {
        let entries = match std::fs::read_dir(dir) {
            Ok(e) => e,
            Err(_) => return,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                if path.join("plugin.json").exists() || path.join("plugin.yaml").exists() {
                    if let Some(s) = path.to_str() {
                        dirs.push(s.to_string());
                    }
                } else {
                    Self::collect_plugin_dirs(&path, dirs);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::traits::{ConfigFileMapping, LoadedPlugin, PluginManifest, PluginStatus};
    use agentos_core::types::TenantContext;
    use serde_json::json;
    use uuid::Uuid;

    /// 串行化 abi_stable cdylib 加载的 native e2e 测试。
    /// abi_stable 的 root module 加载用全局初始化，多线程并发加载不同 cdylib 会竞争，
    /// 需串行（生产环境单调用串行，无此问题）。
    static NATIVE_E2E_LOCK: parking_lot::Mutex<()> = parking_lot::Mutex::new(());

    /// Mock PluginLoader for testing
    struct MockLoader {
        manifests: RwLock<HashMap<String, PluginManifest>>,
        loaded: RwLock<HashMap<String, LoadedPlugin>>,
        /// task_11 N7 测试用：plugin_id → 插件目录路径（get_plugin_dir 返回它）。
        plugin_dirs: RwLock<HashMap<String, String>>,
    }

    impl MockLoader {
        fn new() -> Self {
            Self {
                manifests: RwLock::new(HashMap::new()),
                loaded: RwLock::new(HashMap::new()),
                plugin_dirs: RwLock::new(HashMap::new()),
            }
        }

        fn add_manifest(&self, manifest: PluginManifest) {
            self.manifests.write().insert(manifest.id.clone(), manifest);
        }

        /// 设置插件目录（N7 测试：让 invoker 能找到 .wasm 产物）。
        fn set_plugin_dir(&self, plugin_id: &str, dir: impl Into<String>) {
            self.plugin_dirs.write().insert(plugin_id.to_string(), dir.into());
        }
    }

    #[async_trait]
    impl PluginLoader for MockLoader {
        async fn discover(&self, _root_paths: &[&str]) -> Result<Vec<PluginManifest>, PluginError> {
            Ok(self.manifests.read().values().cloned().collect())
        }

        fn validate_manifest(&self, _manifest: &PluginManifest) -> Result<(), PluginError> {
            Ok(())
        }

        async fn load(&self, plugin_id: &str) -> Result<LoadedPlugin, PluginError> {
            let manifests = self.manifests.read();
            let manifest = manifests.get(plugin_id).ok_or_else(|| PluginError {
                message: format!("plugin not found: {}", plugin_id),
                code: Some("NOT_FOUND".to_string()),
                source: None,
            })?;

            let loaded = LoadedPlugin {
                manifest: manifest.clone(),
                status: PluginStatus::Active,
                loaded_at: Some(chrono::Utc::now()),
            };

            self.loaded
                .write()
                .insert(plugin_id.to_string(), loaded.clone());

            Ok(loaded)
        }

        async fn unload(&self, plugin_id: &str) -> Result<(), PluginError> {
            self.loaded.write().remove(plugin_id);
            Ok(())
        }

        fn get_plugin_dir(&self, plugin_id: &str) -> Option<String> {
            self.plugin_dirs.read().get(plugin_id).cloned()
        }

        fn get_status(&self, plugin_id: &str) -> PluginStatus {
            self.loaded
                .read()
                .get(plugin_id)
                .map(|p| p.status.clone())
                .unwrap_or(PluginStatus::Discovered)
        }
    }

    #[allow(dead_code)]
    fn make_sidecar_manifest(id: &str, entry: &str) -> PluginManifest {
        PluginManifest {
            id: id.to_string(),
            name: format!("Test {}", id),
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Tool,
            pipeline_role: None,
            language: "python".to_string(),
            host_type: HostType::Sidecar,
            entry: entry.to_string(),
            capabilities: Default::default(),
            dependencies: vec![],
            permissions: Default::default(),
            error_policy: Default::default(),
            priority: 100,
            mcp: None,
            native: None,
            wasm: None,
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
        }
    }

    fn make_inprocess_manifest(id: &str) -> PluginManifest {
        PluginManifest {
            id: id.to_string(),
            name: format!("Test {}", id),
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            pipeline_role: None,
            language: "rust".to_string(),
            host_type: HostType::InProcess,
            entry: "test_entry".to_string(),
            capabilities: Default::default(),
            dependencies: vec![],
            permissions: Default::default(),
            error_policy: Default::default(),
            priority: 100,
            mcp: None,
            native: None,
            wasm: None,
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
        }
    }

    #[test]
    fn test_parse_entry_simple() {
        let loader = Arc::new(MockLoader::new());
        let invoker = PluginInvokerImpl::new(loader);
        let (cmd, args) = invoker.parse_entry("python3 server.py").unwrap();
        assert_eq!(cmd, "python3");
        assert_eq!(args, vec!["server.py"]);
    }

    #[test]
    fn test_parse_entry_with_args() {
        let loader = Arc::new(MockLoader::new());
        let invoker = PluginInvokerImpl::new(loader);
        let (cmd, args) = invoker
            .parse_entry("python3 -m my_plugin --port 8080")
            .unwrap();
        assert_eq!(cmd, "python3");
        assert_eq!(args, vec!["-m", "my_plugin", "--port", "8080"]);
    }

    #[test]
    fn test_parse_entry_empty() {
        let loader = Arc::new(MockLoader::new());
        let invoker = PluginInvokerImpl::new(loader);
        assert!(invoker.parse_entry("").is_err());
    }

    #[tokio::test]
    async fn test_invoke_inprocess_without_loader_errors() {
        // InProcess 插件路径已接通 NativePluginLoader：未注入 loader 时应返回
        // NATIVE_LOADER_NOT_CONFIGURED（而非旧的 INPROCESS_DIRECT_CALL 硬错误）。
        let loader = Arc::new(MockLoader::new());
        loader.add_manifest(make_inprocess_manifest("rust_plugin"));

        let invoker = PluginInvokerImpl::new(loader);
        let ctx = PluginContext::new(
            json!({}),
            json!({}),
            TenantContext::new("t1", "s1"),
            Uuid::new_v4(),
            agentos_core::types::ContentLoader::new(
                std::sync::Arc::new(MockStorage),
                "run1".to_string(),
                "main".to_string(),
                0,
            ),
        );

        let result = invoker.invoke_pipeline_plugin("rust_plugin", &ctx).await;
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code.as_deref(), Some("NATIVE_LOADER_NOT_CONFIGURED"));
    }

    // ── 端到端打通验证（真实插件产物，非 mock）──────────────────────────
    //
    // 这两个测试用 plugins/shared 下真实的 native_test（cdylib）和 wasm_hello（.wasm）
    // 插件，验证「放进插件目录 + 注入 runtime → 即可调用」的契约。
    // 这是 Native/WASM 两种执行模式真正端到端打通的最强证据。
    // 若产物未构建，测试 SKIP 而非失败（产物构建属独立步骤）。

    /// 仓库根（invoker crate 在 kernel/crates/invoker，项目根在其上三级）。
    fn repo_root() -> std::path::PathBuf {
        std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent() // kernel/crates
            .unwrap()
            .parent() // kernel
            .unwrap()
            .parent() // 项目根
            .unwrap()
            .to_path_buf()
    }

    /// 构造完整装配的 invoker：真实 loader discover plugins/shared + 注入 wasm/native runtime。
    async fn fully_wired_invoker_for_e2e() -> PluginInvokerImpl {
        let plugins_dir = repo_root().join("plugins/shared");
        let loader = Arc::new(
            agentos_plugin_loader::PluginLoaderImpl::new(plugins_dir.clone(), None),
        );
        loader.discover(&[]).await.unwrap();
        let wasm_runtime = Arc::new(WasmRuntime::new().unwrap());
        let native_loader = Arc::new(NativePluginLoader::new());
        PluginInvokerImpl::new(loader)
            .set_wasm_runtime(wasm_runtime)
            .set_native_loader(native_loader)
    }

    fn make_e2e_ctx() -> PluginContext {
        PluginContext::new(
            json!({}),
            json!({}),
            TenantContext::new("t1", "s1"),
            Uuid::new_v4(),
            agentos_core::types::ContentLoader::new(
                std::sync::Arc::new(MockStorage),
                "run1".to_string(),
                "main".to_string(),
                0,
            ),
        )
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn e2e_native_plugins_load_and_execute() {
        // 验证 abi_stable 改造后 tool_core 原生插件能加载 + 经 HostServices 真正执行工具。
        // 注：NativeHostServices 用 block_in_place（需 multi_thread runtime，生产内核即此配置）。
        //
        // 注意：abi_stable 的 RootModule 按 NativePluginModule_Ref 类型全局缓存
        // （root_module_statics 全局单例）。同进程加载多个用同一 RootModule 类型的 cdylib
        // 会互相覆盖。故本测试只验证单个原生插件（tool_core，生产环境的唯一原生插件）。
        let _guard = NATIVE_E2E_LOCK.lock();
        let plugins_dir = repo_root().join("plugins/shared");
        let tool_core_dll = plugins_dir.join("pipeline/core/tool_core/pipeline_tool_core_native.dll");
        if !tool_core_dll.exists() {
            eprintln!("SKIP: tool_core cdylib not built at {}", tool_core_dll.display());
            return;
        }
        let tool_core_parent = plugins_dir.join("pipeline/core");
        let tool_core_parent_str = tool_core_parent.to_string_lossy().to_string();
        let roots: Vec<&str> = vec![&tool_core_parent_str];
        let loader = Arc::new(
            agentos_plugin_loader::PluginLoaderImpl::new(plugins_dir, None),
        );
        loader.discover(&roots).await.unwrap();
        let native_loader = Arc::new(NativePluginLoader::new());
        let invoker = PluginInvokerImpl::new(loader).set_native_loader(native_loader);

        // 注入 mock router：tool-executor.invoke 模拟 bash_execute 执行成功，
        // 返回 ToolExecutionResult {success:true, data:{output:"agentos-native-ok"}}。
        // 证明原生 tool_core 经 HostServices → router 真正执行工具并拿到结果。
        struct ToolInvokeRouter;
        #[async_trait::async_trait]
        impl CapabilityRouter for ToolInvokeRouter {
            async fn handle(
                &self,
                capability: &str,
                method: &str,
                params: serde_json::Value,
            ) -> Result<serde_json::Value, agentos_mcp::McpError> {
                match (capability, method) {
                    ("tool-executor", "invoke") => {
                        // 回显 tool_name + 返回成功结果（模拟 bash 执行 echo）。
                        Ok(json!({
                            "success": true,
                            "data": {"output": "agentos-native-ok\n", "exit_code": 0},
                            "duration_ms": 1.5,
                        }))
                    }
                    ("event-bus", "emit") => Ok(json!({"status": "emitted"})),
                    _ => Ok(json!({})),
                }
            }
        }
        let router: Arc<dyn CapabilityRouter> = Arc::new(ToolInvokeRouter);
        invoker.set_router(router);

        // tool_core 原生插件（带 raw_tool_calls 触发执行路径）。
        let ctx_tool = PluginContext::new(
            json!({
                "raw_tool_calls": [
                    {"name": "bash_execute", "id": "call_test1", "args": {"command": "echo agentos-native-ok"}}
                ],
                "messages": [],
                "session_id": "test-session",
                "pipeline_id": "test-pipeline",
            }),
            json!({}),
            TenantContext::new("t1", "s1"),
            Uuid::new_v4(),
            agentos_core::types::ContentLoader::new(
                std::sync::Arc::new(MockStorage),
                "run1".to_string(),
                "main".to_string(),
                0,
            ),
        );
        let result = invoker.invoke_pipeline_plugin("pipeline_tool_core", &ctx_tool).await;
        assert!(result.is_ok(), "tool_core invoke failed: {:?}", result.err());
        let pr = result.unwrap();
        // tool_core 必然回写 tool_results + 清空 raw_tool_calls。
        assert!(
            pr.state_updates.contains_key("tool_results"),
            "tool_results missing: {:?}",
            pr.state_updates.keys().collect::<Vec<_>>()
        );
        assert_eq!(pr.state_updates.get("raw_tool_calls"), Some(&json!([])));
        // 关键断言：工具执行成功，结果回写 tool_results（success=true + 输出原文）。
        let tool_results = pr.state_updates.get("tool_results").and_then(|v| v.as_array()).cloned();
        let tr = tool_results.expect("should have tool result array");
        assert_eq!(tr.len(), 1, "should have 1 tool result");
        assert_eq!(tr[0]["success"], true, "tool should succeed: {:?}", tr[0]);
        assert_eq!(
            tr[0]["data"]["output"], "agentos-native-ok\n",
            "tool output should be returned: {:?}", tr[0]
        );
        // messages 重建：assistant tool_calls + tool 结果消息。
        let msgs = pr.state_updates.get("messages").and_then(|v| v.as_array()).cloned();
        let msgs = msgs.expect("messages should be rebuilt");
        assert!(msgs.iter().any(|m| m["role"] == "assistant" && m["tool_calls"].is_array()));
        assert!(
            msgs.iter().any(|m| m["role"] == "tool" && m["content"].as_str().map(|s| s.contains("agentos-native-ok")).unwrap_or(false)),
            "tool result message should carry output: {:?}",
            msgs
        );
    }

    #[tokio::test]
    #[ignore = "native_test 与 tool_core 共用 NativePluginModule_Ref 全局缓存，同进程并行会冲突；tool_core 已由 e2e_native_plugins 覆盖。单独跑：cargo test e2e_native_inprocess -- --ignored"]
    async fn e2e_native_inprocess_plugin_executes() {
        // 单独验证 native_test echo 插件（基础 abi_stable 链路）。
        // 与 e2e_native_plugins 分离：避免同进程两个同 RootModule 类型插件互相覆盖。
        let _guard = NATIVE_E2E_LOCK.lock();
        let dll = repo_root().join("plugins/shared/native_test/native_test_plugin.dll");
        if !dll.exists() {
            eprintln!("SKIP: native cdylib not built at {}", dll.display());
            return;
        }
        let invoker = fully_wired_invoker_for_e2e().await;
        let ctx = make_e2e_ctx();
        let result = invoker.invoke_pipeline_plugin("native_test", &ctx).await;
        assert!(result.is_ok(), "native plugin invoke failed: {:?}", result.err());
        let pr = result.unwrap();
        assert_eq!(
            pr.state_updates.get("processed_by"),
            Some(&json!("test_plugin")),
            "got: {:?}",
            pr.state_updates
        );
    }

    #[tokio::test]
    async fn e2e_wasm_plugin_executes() {
        let wasm = repo_root().join("plugins/shared/wasm_hello/wasm_hello.wasm");
        if !wasm.exists() {
            eprintln!("SKIP: wasm artifact not present at {}", wasm.display());
            return;
        }
        let invoker = fully_wired_invoker_for_e2e().await;
        let ctx = make_e2e_ctx();
        // 经 WasmRuntime（wasmtime）加载执行 .wasm
        let result = invoker.invoke_pipeline_plugin("wasm_hello", &ctx).await;
        assert!(
            result.is_ok(),
            "wasm plugin invoke failed: {:?}",
            result.err()
        );
        let pr = result.unwrap();
        // wasm_hello 返回 {"state_updates":{"processed_by":"wasm_hello"}}
        assert_eq!(
            pr.state_updates.get("processed_by"),
            Some(&json!("wasm_hello")),
            "got: {:?}",
            pr.state_updates
        );
    }

    #[tokio::test]
    async fn test_invoke_nonexistent_plugin() {
        let loader = Arc::new(MockLoader::new());
        let invoker = PluginInvokerImpl::new(loader);
        let ctx = PluginContext::new(
            json!({}),
            json!({}),
            TenantContext::new("t1", "s1"),
            Uuid::new_v4(),
            agentos_core::types::ContentLoader::new(
                std::sync::Arc::new(MockStorage),
                "run1".to_string(),
                "main".to_string(),
                0,
            ),
        );

        let result = invoker.invoke_pipeline_plugin("nonexistent", &ctx).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_crash_callback_invoked() {
        let loader = Arc::new(MockLoader::new());
        let invoker = PluginInvokerImpl::new(loader);

        let crashed = Arc::new(std::sync::Mutex::new(None::<String>));
        let crashed_clone = Arc::clone(&crashed);
        invoker.on_crash(Arc::new(move |plugin_id: &str| {
            *crashed_clone.lock().unwrap() = Some(plugin_id.to_string());
        }));

        invoker.notify_crash("test_plugin");

        assert_eq!(*crashed.lock().unwrap(), Some("test_plugin".to_string()));
    }

    #[tokio::test]
    async fn test_lifecycle_hook_composite_skipped() {
        // ADR ⑥: 组合插件不需要生命周期钩子
        let loader = Arc::new(MockLoader::new());
        let manifest = PluginManifest {
            id: "composite_test".to_string(),
            name: "Composite".to_string(),
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Composite,
            pipeline_role: None,
            language: "yaml".to_string(),
            host_type: HostType::InProcess,
            entry: String::new(),
            capabilities: Default::default(),
            dependencies: vec![],
            permissions: Default::default(),
            error_policy: Default::default(),
            priority: 100,
            mcp: None,
            native: None,
            wasm: None,
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
        };
        loader.add_manifest(manifest);

        let invoker = PluginInvokerImpl::new(loader);
        let ctx = HookContext::new();
        let result = invoker
            .send_lifecycle_hook("composite_test", LifecycleHook::OnLoad, &ctx)
            .await;
        assert!(result.is_ok()); // 组合插件直接返回 Ok
    }

    #[tokio::test]
    async fn test_check_health_not_connected() {
        let loader = Arc::new(MockLoader::new());
        let invoker = PluginInvokerImpl::new(loader);
        assert!(!invoker.check_health("nonexistent").await);
    }

    #[tokio::test]
    async fn test_force_unload_nonexistent() {
        let loader = Arc::new(MockLoader::new());
        let invoker = PluginInvokerImpl::new(loader);
        // force_unload 对不存在的插件也应该返回 Ok
        let result = invoker.force_unload("nonexistent").await;
        assert!(result.is_ok());
    }

    // ── task_11 N7：invoker 分发 host_type==Wasm ──

    /// 构造一个 Wasm pipeline manifest（host_type: wasm）。
    fn make_wasm_manifest(id: &str, artifact: &str) -> PluginManifest {
        PluginManifest {
            id: id.to_string(),
            name: format!("Wasm {}", id),
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            pipeline_role: None,
            language: "rust".to_string(),
            host_type: HostType::Wasm,
            entry: artifact.to_string(),
            capabilities: Default::default(),
            dependencies: vec![],
            permissions: Default::default(),
            error_policy: Default::default(),
            priority: 100,
            mcp: None,
            native: None,
            wasm: Some(agentos_core::traits::WasmArtifact {
                artifact: artifact.to_string(),
                wit_interface: None,
                granted_capabilities: vec![],
            }),
            requires_content: None,
            invoke_entry: Some("execute".to_string()),
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
        }
    }

    /// 一个 echo WAT（输入 JSON 原样回显为输出 JSON）。
    /// 注意：输出必须是合法的 PluginResult JSON，否则反序列化失败。
    /// 这里 execute 直接返回固定的成功 PluginResult JSON（不读输入），
    /// 避免 WAT 里实现 JSON 解析的复杂度——仅验证 host→guest→host 链路通。
    const ECHO_RESULT_WAT: &str = r#"
(module
  (memory (export "memory") 1)
  (global $bump (mut i32) (i32.const 4))
  (func $allocate (export "allocate") (param $len i32) (result i32)
    (local $ptr i32)
    (local.set $ptr (global.get $bump))
    (global.set $bump (i32.and
      (i32.add (global.get $bump) (local.get $len))
      (i32.const 0x7FFFFFFC)))
    (local.set $ptr (i32.or (local.get $ptr) (i32.const 0)))
    ;; 4 字节对齐
    (global.set $bump (i32.and
      (i32.add (global.get $bump) (i32.const 3))
      (i32.const 0x7FFFFFFC)))
    (local.get $ptr))
  (func (export "deallocate") (param $p i32) (param $l i32))
  ;; execute 返回固定的 PluginResult JSON：{"state_updates":{"echo":"ok"}} (31 字节)
  ;; 把这串字节用 (data) 写进内存，execute 复制并返回 (ptr,len)。
  (data (i32.const 1024) "{\"state_updates\":{\"echo\":\"ok\"}}")
  (func (export "execute") (param $in_ptr i32) (param $in_len i32) (result i64)
    (local $out_ptr i32)
    ;; 输出长度 = 31（上面的 JSON 字节数）
    (local.set $out_ptr (call $allocate (i32.const 31)))
    ;; 复制 data 到 out
    (memory.copy (local.get $out_ptr) (i32.const 1024) (i32.const 31))
    (i64.or
      (i64.extend_i32_u (local.get $out_ptr))
      (i64.shl (i64.extend_i32_u (i32.const 31)) (i64.const 32))))
)
"#;

    /// N7：invoker 经 WasmRuntime 加载并调用 WASM pipeline 插件，返回 PluginResult。
    #[tokio::test]
    async fn test_invoke_wasm_pipeline_plugin_dispatches_to_wasmruntime() {
        // 1. 写 .wasm 文件到临时目录（用 wat 编译 WAT）
        let tmp = tempfile::tempdir().unwrap();
        let wasm_bytes = wat::parse_bytes(ECHO_RESULT_WAT.as_bytes()).unwrap();
        let wasm_path = tmp.path().join("echo.wasm");
        std::fs::write(&wasm_path, &wasm_bytes).unwrap();

        // 2. 构造 loader + manifest，设置插件目录
        let loader = Arc::new(MockLoader::new());
        loader.add_manifest(make_wasm_manifest("wasm_echo", "echo.wasm"));
        loader.set_plugin_dir("wasm_echo", tmp.path().to_string_lossy().to_string());

        // 3. 构造带 WasmRuntime 的 invoker
        let runtime = Arc::new(WasmRuntime::new().unwrap());
        let invoker = PluginInvokerImpl::with_wasm_runtime(loader, runtime);

        let ctx = PluginContext::new(
            json!({}),
            json!({}),
            TenantContext::new("t1", "s1"),
            Uuid::new_v4(),
            agentos_core::types::ContentLoader::new(
                Arc::new(MockStorage),
                "run1".to_string(),
                "main".to_string(),
                0,
            ),
        );

        let result = invoker.invoke_pipeline_plugin("wasm_echo", &ctx).await;
        assert!(result.is_ok(), "WASM dispatch should succeed: {:?}", result);
        let pr = result.unwrap();
        // echo 模块返回 {"state_updates":{"echo":"ok"}}
        assert_eq!(pr.state_updates.get("echo"), Some(&json!("ok")));
    }

    /// N7：未配置 WasmRuntime 时，WASM 插件调用返回 WASM_RUNTIME_NOT_CONFIGURED。
    #[tokio::test]
    async fn test_invoke_wasm_without_runtime_errors() {
        let loader = Arc::new(MockLoader::new());
        loader.add_manifest(make_wasm_manifest("wasm_no_rt", "x.wasm"));

        // 用 new()——不注入 WasmRuntime
        let invoker = PluginInvokerImpl::new(loader);
        let ctx = PluginContext::new(
            json!({}),
            json!({}),
            TenantContext::new("t1", "s1"),
            Uuid::new_v4(),
            agentos_core::types::ContentLoader::new(
                Arc::new(MockStorage),
                "run1".to_string(),
                "main".to_string(),
                0,
            ),
        );

        let err = invoker.invoke_pipeline_plugin("wasm_no_rt", &ctx).await.unwrap_err();
        assert_eq!(err.code.as_deref(), Some("WASM_RUNTIME_NOT_CONFIGURED"));
    }

    /// N7：WASM 插件缺 manifest.wasm.artifact → MISSING_WASM_ARTIFACT。
    #[tokio::test]
    async fn test_invoke_wasm_missing_artifact_errors() {
        let tmp = tempfile::tempdir().unwrap();
        let loader = Arc::new(MockLoader::new());
        let mut manifest = make_wasm_manifest("wasm_no_art", "echo.wasm");
        manifest.wasm = None; // 故意去掉 wasm 字段
        loader.add_manifest(manifest);
        loader.set_plugin_dir("wasm_no_art", tmp.path().to_string_lossy().to_string());

        let runtime = Arc::new(WasmRuntime::new().unwrap());
        let invoker = PluginInvokerImpl::with_wasm_runtime(loader, runtime);
        let ctx = PluginContext::new(
            json!({}),
            json!({}),
            TenantContext::new("t1", "s1"),
            Uuid::new_v4(),
            agentos_core::types::ContentLoader::new(
                Arc::new(MockStorage),
                "run1".to_string(),
                "main".to_string(),
                0,
            ),
        );

        let err = invoker.invoke_pipeline_plugin("wasm_no_art", &ctx).await.unwrap_err();
        assert_eq!(err.code.as_deref(), Some("MISSING_WASM_ARTIFACT"));
    }

    // ── extract_mcp_content 辅助函数单元测试 ──

    #[test]
    fn test_extract_mcp_content_normal_response() {
        let inner_json = r#"{"state_updates":{"key":"value"}}"#;
        let mcp_result = json!({
            "content": [{"type": "text", "text": inner_json}],
            "isError": false
        });
        let extracted = extract_mcp_content(&mcp_result);
        assert_eq!(extracted["state_updates"]["key"], "value");
    }

    #[test]
    fn test_extract_mcp_content_is_error() {
        let mcp_result = json!({
            "content": [{"type": "text", "text": "something went wrong"}],
            "isError": true
        });
        let extracted = extract_mcp_content(&mcp_result);
        assert_eq!(extracted["error"], "something went wrong");
    }

    #[test]
    fn test_extract_mcp_content_empty_content_array() {
        let mcp_result = json!({
            "content": [],
            "isError": false
        });
        let extracted = extract_mcp_content(&mcp_result);
        // 空数组 → and_then 链返回 None → fallback 到 clone 原对象
        assert_eq!(extracted["content"], json!([]));
    }

    #[test]
    fn test_extract_mcp_content_text_not_json() {
        let mcp_result = json!({
            "content": [{"type": "text", "text": "not_a_json_string"}],
            "isError": false
        });
        let extracted = extract_mcp_content(&mcp_result);
        // text 不是合法 JSON → from_str().ok() 返回 None → fallback 到 clone
        assert_eq!(extracted["content"][0]["text"], "not_a_json_string");
    }

    #[test]
    fn test_extract_mcp_content_missing_content_field() {
        let mcp_result = json!({"isError": false});
        let extracted = extract_mcp_content(&mcp_result);
        // 无 content 字段 → fallback 到 clone 原对象
        assert_eq!(extracted["isError"], false);
    }

    // ── P6 命名治理（ADR 附录 D③）：invoke_pipeline_plugin 读 invoke_entry ──
    // 注：build_injected_config / resolve_config_path 及其测试已迁至 shared.rs。

    /// 辅助：构造一个 sidecar pipeline manifest（用于 invoke_entry 缺失测试）。
    fn make_pipeline_sidecar_manifest(id: &str, invoke_entry: Option<&str>) -> PluginManifest {
        PluginManifest {
            id: id.to_string(),
            name: format!("Test {}", id),
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            pipeline_role: None,
            language: "python".to_string(),
            host_type: HostType::Sidecar,
            entry: "python server.py".to_string(),
            capabilities: Default::default(),
            dependencies: vec![],
            permissions: Default::default(),
            error_policy: Default::default(),
            priority: 100,
            mcp: None,
            native: None,
            wasm: None,
            requires_content: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            invoke_entry: invoke_entry.map(str::to_string),
        }
    }

    /// P6：sidecar pipeline 插件缺 invoke_entry 时，invoke_pipeline_plugin 返回
    /// 明确的 MISSING_INVOKE_ENTRY 错误（不再静默回退字面量 "execute"）。
    /// 此为运行期防线；启动期聚合校验（plugin-loader discover）是主门。
    #[tokio::test]
    async fn test_invoke_pipeline_plugin_missing_invoke_entry_returns_error() {
        let loader = Arc::new(MockLoader::new());
        // 缺 invoke_entry 的 sidecar pipeline 插件
        loader.add_manifest(make_pipeline_sidecar_manifest("bad_pipeline", None));

        let invoker = PluginInvokerImpl::new(loader);
        let ctx = PluginContext::new(
            json!({}),
            json!({}),
            TenantContext::new("t1", "s1"),
            Uuid::new_v4(),
            agentos_core::types::ContentLoader::new(
                std::sync::Arc::new(MockStorage),
                "run1".to_string(),
                "main".to_string(),
                0,
            ),
        );

        let result = invoker.invoke_pipeline_plugin("bad_pipeline", &ctx).await;
        assert!(result.is_err(), "missing invoke_entry must error");
        let err = result.unwrap_err();
        assert_eq!(
            err.code.as_deref(),
            Some("MISSING_INVOKE_ENTRY"),
            "error code must be MISSING_INVOKE_ENTRY, got: {:?}",
            err.code
        );
        assert!(
            err.message.contains("bad_pipeline"),
            "error message must name the offending plugin: {}",
            err.message
        );
    }

    // Mock StorageBackend for test context
    struct MockStorage;

    #[async_trait::async_trait]
    impl agentos_core::traits::StorageBackend for MockStorage {
        async fn get_run(
            &self,
            _run_id: &str,
        ) -> Result<agentos_core::types::RunRecord, agentos_core::types::StorageError> {
            Err(agentos_core::types::StorageError::NotFound(
                "mock".to_string(),
            ))
        }
        async fn get_messages(
            &self,
            _run_id: &str,
            _branch_id: &str,
        ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError>
        {
            Ok(vec![])
        }
        async fn get_recent_messages(
            &self,
            _run_id: &str,
            _branch_id: &str,
            _n: usize,
        ) -> Result<Vec<agentos_core::types::Message>, agentos_core::types::StorageError> {
            Ok(vec![])
        }
        async fn get_blob(
            &self,
            _blob_id: &str,
        ) -> Result<Vec<u8>, agentos_core::types::StorageError> {
            Ok(vec![])
        }
        async fn append_trace(
            &self,
            _entry: agentos_core::types::TraceEntry,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn create_branch(
            &self,
            _branch: agentos_core::types::Branch,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn update_run_status(
            &self,
            _run_id: &str,
            _status: agentos_core::types::RunStatus,
            _branch: Option<&str>,
            _seq: Option<u32>,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn get_messages_by_pipeline(
            &self,
            _pipeline_id: &str,
            _opts: agentos_core::traits::MessageQueryOpts,
        ) -> Result<Vec<agentos_core::types::MessageRecord>, agentos_core::types::StorageError>
        {
            Ok(vec![])
        }
        async fn next_sequence(
            &self,
            _pipeline_id: &str,
        ) -> Result<u32, agentos_core::types::StorageError> {
            Ok(1)
        }
        async fn create_run(
            &self,
            _run_id: &str,
            _config_hash: &str,
            _tenant_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        #[allow(clippy::too_many_arguments)]
        async fn append_message(
            &self,
            _message_id: &str,
            _run_id: &str,
            _branch_id: &str,
            _seq_in_branch: u32,
            _role: &str,
            _blob_id: Option<&str>,
            _content_preview: Option<&str>,
            _pipeline_id: Option<&str>,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn store_blob(
            &self,
            _data: &[u8],
            _mime_type: &str,
        ) -> Result<String, agentos_core::types::StorageError> {
            Ok("mock_blob".to_string())
        }
        async fn create_session(
            &self,
            _session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn get_session(
            &self,
            _thread_id: &str,
        ) -> Result<Option<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            Ok(None)
        }
        async fn list_sessions(
            &self,
            _filter: agentos_core::traits::SessionListFilter,
        ) -> Result<Vec<agentos_core::types::SessionRecord>, agentos_core::types::StorageError>
        {
            Ok(vec![])
        }
        async fn update_session(
            &self,
            _session: &agentos_core::types::SessionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn delete_session(
            &self,
            _thread_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        // 以下方法 native 插件测试用不到，补空实现让测试编译通过（既有测试债）。
        async fn append_execution_record(
            &self,
            _record: &agentos_core::types::ExecutionRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn list_execution_records(
            &self,
            _pipeline_run_id: &str,
            _opts: agentos_core::traits::MessageQueryOpts,
        ) -> Result<Vec<agentos_core::types::ExecutionRecord>, agentos_core::types::StorageError>
        {
            Ok(vec![])
        }
        async fn count_execution_records(
            &self,
            _pipeline_run_id: &str,
        ) -> Result<u64, agentos_core::types::StorageError> {
            Ok(0)
        }
        async fn delete_execution_records_by_session(
            &self,
            _pipeline_run_id: &str,
        ) -> Result<u64, agentos_core::types::StorageError> {
            Ok(0)
        }
        async fn save_run_summary(
            &self,
            _summary: &agentos_core::types::PipelineRunSummary,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn get_run_summary(
            &self,
            _run_id: &str,
        ) -> Result<Option<agentos_core::types::PipelineRunSummary>, agentos_core::types::StorageError>
        {
            Ok(None)
        }
        async fn update_run_summary(
            &self,
            _run_id: &str,
            _updates: &serde_json::Value,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn list_run_summaries(
            &self,
            _limit: Option<usize>,
        ) -> Result<Vec<agentos_core::types::PipelineRunSummary>, agentos_core::types::StorageError>
        {
            Ok(vec![])
        }
        async fn create_memory(
            &self,
            _memory: &agentos_core::types::MemoryRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn get_memory(
            &self,
            _id: &str,
        ) -> Result<Option<agentos_core::types::MemoryRecord>, agentos_core::types::StorageError>
        {
            Ok(None)
        }
        async fn list_memory(
            &self,
            _memory_type: Option<&str>,
            _limit: usize,
            _offset: usize,
        ) -> Result<Vec<agentos_core::types::MemoryRecord>, agentos_core::types::StorageError>
        {
            Ok(vec![])
        }
        async fn search_memory(
            &self,
            _query: &str,
            _top_k: usize,
        ) -> Result<Vec<agentos_core::types::MemoryRecord>, agentos_core::types::StorageError>
        {
            Ok(vec![])
        }
        async fn delete_memory(
            &self,
            _memory_id: &str,
        ) -> Result<bool, agentos_core::types::StorageError> {
            Ok(false)
        }
    }

    // ── 阶段 1.1 pull 热加载单测 ────────────────────────────────────────────

    #[test]
    fn test_compute_plugin_fingerprint_stable_for_unchanged_dir() {
        // 同一目录两次计算指纹应相同（mtime 不变）
        let dir = std::env::temp_dir().join("invoker_fp_test_stable");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("plugin.json"), b"{}").unwrap();
        std::fs::write(dir.join("server.py"), b"print(1)").unwrap();
        let manifest = make_sidecar_manifest("test_fp", "python server.py");
        let fp1 = compute_plugin_fingerprint(&dir, &manifest);
        let fp2 = compute_plugin_fingerprint(&dir, &manifest);
        assert_eq!(fp1, fp2, "未变更的目录指纹应稳定");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_compute_plugin_fingerprint_changes_on_file_edit() {
        // 修改 server.py 内容（更新 mtime）后指纹应变化
        let dir = std::env::temp_dir().join("invoker_fp_test_change");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("server.py"), b"print(1)").unwrap();
        let manifest = make_sidecar_manifest("test_fp2", "python server.py");
        let fp1 = compute_plugin_fingerprint(&dir, &manifest);
        // 确保跨过 mtime 秒级精度边界
        std::thread::sleep(std::time::Duration::from_secs_f64(1.1));
        std::fs::write(dir.join("server.py"), b"print(2) # changed").unwrap();
        let fp2 = compute_plugin_fingerprint(&dir, &manifest);
        assert_ne!(
            fp1, fp2,
            "文件修改后指纹必须变化，否则热加载不会触发"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn test_is_plugin_stale_ttl_short_circuits() {
        // TTL 内（1s）重复检测不应过期：首次记录后立即再查应返回 false
        let loader = Arc::new(MockLoader::new());
        let invoker = PluginInvokerImpl::new(loader.clone());
        let manifest = make_sidecar_manifest("stale_ttl", "python server.py");
        // 首次：写入指纹，返回 false
        let first = invoker.is_plugin_stale("stale_ttl", &manifest).await;
        assert!(!first, "首次检测不应判为过期");
        // TTL 内立即再查：应短路返回 false
        let second = invoker.is_plugin_stale("stale_ttl", &manifest).await;
        assert!(!second, "TTL 内应短路返回 false（不算过期）");
    }

    #[tokio::test]
    async fn test_force_unload_via_trait_method() {
        // force_unload 是 trait 方法（trait 默认实现被 invoker 覆盖）。
        // 对未加载的插件调 force_unload 应返回 Ok（幂等，无 sidecar 可 kill）。
        let loader = Arc::new(MockLoader::new());
        let invoker: Arc<dyn PluginInvoker> = Arc::new(PluginInvokerImpl::new(loader.clone()));
        let result = invoker.force_unload("never_loaded_plugin").await;
        assert!(result.is_ok(), "force_unload 未加载插件应返回 Ok");
    }

    #[test]
    fn test_touch_last_used_records_activity() {
        // touch_last_used 应在 last_used 缓存写入当前时刻
        let loader = Arc::new(MockLoader::new());
        let invoker = PluginInvokerImpl::new(loader);
        assert!(
            invoker.last_used.read().is_empty(),
            "初始 last_used 应为空"
        );
        invoker.touch_last_used("plugin_a");
        invoker.touch_last_used("plugin_b");
        assert_eq!(
            invoker.last_used.read().len(),
            2,
            "touch 两个插件后 last_used 应有 2 条"
        );
        // 再次 touch 同一插件应更新（不新增）
        invoker.touch_last_used("plugin_a");
        assert_eq!(invoker.last_used.read().len(), 2, "重复 touch 不应新增条目");
    }

    #[tokio::test]
    async fn test_unload_if_idle_unloaded_sidecar_returns_true() {
        // 对已 force_unload（不在 mcp_clients）的插件，unload_if_idle 内部走 force_unload_impl
        // 路径，对未加载的返回 Ok → true。
        let loader = Arc::new(MockLoader::new());
        let invoker = PluginInvokerImpl::new(loader);
        // 未加载任何 sidecar，unload_if_idle 应走 force_unload_impl（Ok）→ true
        let unloaded = invoker.unload_if_idle("never_loaded").await;
        assert!(unloaded, "未加载插件的 unload_if_idle 应返回 true（软卸载幂等成功）");
    }

    #[tokio::test]
    async fn test_discover_new_plugins_returns_via_trait() {
        // discover_new_plugins 是 trait 方法，转发到 loader.discover。
        // MockLoader.discover 返回 manifests 缓存里的全部（默认空）。
        let loader = Arc::new(MockLoader::new());
        let invoker: Arc<dyn PluginInvoker> = Arc::new(PluginInvokerImpl::new(loader));
        let result = invoker.discover_new_plugins().await;
        assert!(result.is_ok(), "discover_new_plugins 应返回 Ok");
        assert_eq!(result.unwrap().len(), 0, "空 MockLoader 应发现 0 个插件");
    }
}

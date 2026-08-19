//! PluginInvoker 实现
//!
//! 按 host_type 透明分发调用：
//! - InProcess: 经 `NativePluginLoader` 加载 cdylib，走 C-ABI 调用（JSON 经内存传递）
//! - Sidecar: 通过 MCP 客户端走 JSON-RPC 协议调用（进程隔离）
//!
//! 两种 host_type 共用 PluginInput / PluginResult JSON 契约，invoker 透明分发。
//! （原 Wasm 轨已按两轨终局决策关闭摘除，见 core::traits::HostType 文档。）
//!
//! [来源: docs/tasks/task_05_plugin_system.md AC-04-5/AC-04-6]

use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};

use agentos_core::traits::{
    HookContext, HostType, LifecycleHook, PluginInvoker, PluginLoader, PluginManifest, PluginType,
};
use agentos_core::types::{PluginContext, PluginError, PluginResult, ToolExecutionResult};
use agentos_hooks::{EventTarget, HookEventBus, LifecycleEvent};
use agentos_mcp::{resolve_env_placeholders, CapabilityRouter, McpClient, McpError};
use agentos_plugin_loader::NativePluginLoader;
use async_trait::async_trait;
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
///   写入临时文件（如诊断日志），会误触发 respawn。
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

    // 项目根 .env 指纹：API Key 更新走 .env（写 .env 不一定改变 llm.yaml
    // 内容——占位符已是 ${VAR} 时 yaml 不变），必须靠 .env mtime 触发
    // sidecar 热重启，respawn 时经 env_delta_overlay 拿到新 key。
    if let Some(env_path) = agentos_mcp::env_file::project_env_path() {
        hasher.write(b".env|");
        hasher.write(mtime_str(&env_path).as_bytes());
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
            obj.insert(
                "_plugin_id".to_string(),
                Value::String(self.plugin_id.clone()),
            );
        } else {
            params = json!({ "_plugin_id": self.plugin_id, "value": params });
        }
        self.inner.handle(capability, method, params).await
    }

    /// 委托给 inner——让 inner（KernelCapabilityRouter）的动态 namespace
    /// （含 handler_registry 注册的 human-interaction 等）透传到 initialize 声明。
    /// 不覆盖的话走 trait 默认实现，只返回静态 STANDARD_CAPABILITIES，
    /// sidecar 拿不到插件自注册 namespace 的 CapabilityHandle。
    fn known_namespaces(&self) -> Vec<String> {
        self.inner.known_namespaces()
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

/// B2：native 工具调用返回归一（对齐 [`PluginInvokerImpl::invoke_tool`] sidecar
/// 决策树的归一层：纯业务数据包 success 信封）。
///
/// 插件 execute 的返回 JSON 按形状分流：
/// - 带 `success`（bool）→ ToolExecutionResult 信封形态（新工具插件的约定返回）：
///   - `success=true` → success(data 字段，缺省 Null)，保留 duration_ms；
///   - `success=false` → failure(error 字段，缺省通用文案)。
/// - 无 `success` → 旧 pipeline 插件（忽略 tool_call，返回 state_updates）→ 纯业务
///   数据包 success 信封——旧插件零破坏。
///
/// 与 sidecar 决策树的差异（有意）：sidecar 另有「`{error}` 无 success → failure」
/// 分支（MCP isError=true 提取的产物）；native 路径 execute 失败走 Err 错误通道、
/// 不产该形态，且旧插件 state_updates 可能天然含 `error` 键，误判会破坏零兼容
/// 承诺，故不设该分支。
fn normalize_native_tool_output(inner: &Value) -> ToolExecutionResult {
    match inner.get("success").and_then(|v| v.as_bool()) {
        Some(true) => {
            let mut result =
                ToolExecutionResult::success(inner.get("data").cloned().unwrap_or(Value::Null));
            result.duration_ms = inner.get("duration_ms").and_then(|v| v.as_u64());
            result
        }
        Some(false) => {
            let err_msg = inner
                .get("error")
                .and_then(|v| v.as_str())
                .unwrap_or("native tool execution failed");
            ToolExecutionResult::failure(err_msg)
        }
        None => ToolExecutionResult::success(inner.clone()),
    }
}

/// 内核侧 HostServices 实现：包 `CapabilityRouter`，供原生插件调 capability。
///
/// 与 sidecar（JSON-RPC 反调）走同一 router，两轨对齐。
/// trait 方法是 sync（插件经 C-ABI 同步调），内部用 block_in_place + block_on
/// 跑 async router.handle。
///
/// G6：`plugin_id` 是信任锚点（invoker 从 manifest 注入，插件无法伪造），
/// 每次调用自动写入 params._plugin_id——与 sidecar 的 PluginScopedRouter 同构，
/// 使 CapabilityRouter 单点授权校验对两轨同判。
struct NativeHostServices {
    router: Arc<dyn CapabilityRouter>,
    /// 调用方插件 id（信任锚点，G6 授权粒度统一）。
    plugin_id: String,
    /// G7：execute 是否跑在 spawn_blocking 线程上。blocking 线程不是
    /// runtime worker，`block_in_place` 在其上会 panic——改用直接
    /// `Handle::block_on`（blocking 线程不参与调度，阻塞安全）。
    on_blocking_thread: bool,
}

impl agentos_native_sdk::HostServices for NativeHostServices {
    fn call_capability(
        &self,
        capability: &str,
        method: &str,
        params_json: &str,
    ) -> Result<String, String> {
        let router = Arc::clone(&self.router);
        let cap = capability.to_string();
        let mth = method.to_string();
        // G6：注入 _plugin_id 信任锚点（插件侧参数不可覆盖——已存在时以内核注入为准）。
        let mut params: Value = serde_json::from_str(params_json).unwrap_or(Value::Null);
        if let Value::Object(ref mut map) = params {
            map.insert(
                "_plugin_id".to_string(),
                Value::String(self.plugin_id.clone()),
            );
        } else {
            let mut map = serde_json::Map::new();
            map.insert(
                "_plugin_id".to_string(),
                Value::String(self.plugin_id.clone()),
            );
            params = Value::Object(map);
        }
        // sync→async（G7 上下文感知）：
        // - async worker 线程：block_in_place 让出 worker 再 block_on，避免死锁；
        // - spawn_blocking 线程：直接 block_on（blocking 线程不参与调度，阻塞安全；
        //   block_in_place 在非 worker 线程会 panic）。
        let fut = async move { router.handle(&cap, &mth, params).await };
        let result = if self.on_blocking_thread {
            tokio::runtime::Handle::current().block_on(fut)
        } else {
            tokio::task::block_in_place(|| tokio::runtime::Handle::current().block_on(fut))
        };
        match result {
            Ok(v) => Ok(serde_json::to_string(&v).unwrap_or_default()),
            Err(e) => Err(format!("{e}")),
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
    mcp_clients: RwLock<HashMap<String, Arc<tokio::sync::RwLock<McpClient>>>>,
    /// per-plugin spawn 互斥锁（single-flight，防并发请求竞态 spawn 多个 sidecar）。
    /// 同一 plugin_id 的 spawn 串行化：首个请求持锁 spawn 并写缓存，后续请求拿锁后
    /// 二次查缓存命中直接复用。single-flight 锁保证并发触发只创建一个 sidecar。
    spawn_locks: RwLock<HashMap<String, Arc<tokio::sync::Mutex<()>>>>,
    /// 崩溃回调（插件崩溃时调用）
    #[allow(clippy::type_complexity)]
    crash_callbacks: RwLock<Vec<Arc<dyn Fn(&str) + Send + Sync>>>,
    /// Capability 路由器——sidecar→内核反向调用通道。
    /// 设置后，新建的 MCP 客户端会带上路由器；已有客户端需重连才生效。
    router: RwLock<Option<Arc<dyn CapabilityRouter>>>,
    /// 生命周期钩子事件总线（旁路广播，可选注入）。
    ///
    /// `None`（默认）时行为不变——sidecar spawn 的 `notifications/on_load` 仍是
    /// 点对点直调目标插件的权威路径；`Some` 时在直调**旁路**额外把 OnLoad 等事件
    /// fan-out 给订阅者（审计/指标），best-effort、非阻塞。镜像 `router` 字段的
    /// `RwLock<Option<Arc<_>>>` 形态，经 [`set_hook_bus`] 在构造后注入。
    hook_bus: RwLock<Option<Arc<HookEventBus>>>,
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
    /// 注入给 sidecar 子进程的 PYTHONPATH 项目根目录（project_root）。
    ///
    /// sidecar 的 import 有两种历史写法并存（见 `resolve_pythonpath_src` 注释）：
    /// - `from src.core.logging import ...`（带 src. 前缀，需 project_root 在 sys.path）
    /// - `from config.settings import ...`（不带前缀，需 project_root/src 在 sys.path）
    ///
    /// 因此实际注入的 PYTHONPATH 同时含 project_root 和 project_root/src。
    ///
    /// PYTHONPATH 注入由内核构造期显式注入 project_root，不依赖
    /// `AGENTOS_PLUGINS_DIR` 环境变量（启动方式如 Git Bash 的 start_web_02.sh
    /// 未必设置它，sidecar 的 plugin.py 会因无法 import 公共包而启动即崩溃、
    /// initialize 永久卡到超时）；环境变量仅作向后兼容兜底。
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
            hook_bus: RwLock::new(None),
            native_loader: None,
            fingerprints: RwLock::new(HashMap::new()),
            last_used: RwLock::new(HashMap::new()),
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
    /// sidecar 的 import 存在两种写法并存，必须同时满足：
    /// - `from src.core.logging import ...`（带 src. 前缀）→ 需 **project_root** 在
    ///   sys.path，Python 才在 `<project_root>/src/core/...` 解析。
    /// - `from config.settings import ...`（不带前缀）→ 需 **project_root/src** 在
    ///   sys.path，Python 才在 `<project_root>/src/config/...` 解析。
    ///
    /// 故 PYTHONPATH 同时含两者。只放其一会导致另一种写法的插件 sidecar 启动即崩
    /// （实测：prompt_build 用 `from config.settings`、SDK 用 `from src.core.logging`）。
    ///
    /// 额外注入 `project_root/plugins/sdk/src`——**agentos_plugin_sdk 源码目录**。
    /// 所有工具插件（simple/bash/download/human/builtin_tools 等）server.py 都
    /// `from agentos_plugin_sdk import AgentOSPlugin`，而 SDK 通常未 pip install
    /// （或版本与源码不同步），必须能从 PYTHONPATH 直接解析。缺失时 sidecar 启动即
    /// `ModuleNotFoundError: agentos_plugin_sdk` 崩溃 → 内核 initialize 等不到响应 →
    /// 工具调用"调用前卡死"（120s 超时，用户感知为无响应）。
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

        // 拼接候选目录：project_root（解 src. 前缀）+ project_root/src（解裸 import）
        // + project_root/plugins/sdk/src（agentos_plugin_sdk 源码，工具插件公共依赖）。
        let mut dirs: Vec<std::path::PathBuf> = vec![project_root.clone()];
        let src_dir = project_root.join("src");
        if src_dir.is_dir() {
            dirs.push(src_dir);
        }
        let sdk_dir = project_root.join("plugins/sdk/src");
        if sdk_dir.is_dir() {
            dirs.push(sdk_dir);
        }

        // PYTHONPATH 是 env 变量，路径间用 **环境变量分隔符**（Windows ';'、Unix ':'）
        // 连接——注意这跟路径组件分隔符 MAIN_SEPARATOR（Windows '\'、Unix '/'）是两回事。
        // 误用 MAIN_SEPARATOR 会把路径粘连成 "D:\...\D:\...\src"。
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

    /// 链式注入原生插件加载器（启动期装配用）。
    ///
    /// 启用 `host_type == InProcess` 的 cdylib 插件：放进插件目录 + 重启即用，
    /// 无需改任何代码（详见 [`Self::resolve_native_artifact`]）。
    pub fn set_native_loader(mut self, native_loader: Arc<NativePluginLoader>) -> Self {
        self.native_loader = Some(native_loader);
        self
    }

    /// 解析原生插件产物路径（manifest.native.artifact 相对插件目录）。
    ///
    /// `artifact` 可写裸名（如 `my_plugin`）
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
        let dir = self
            .loader
            .get_plugin_dir(plugin_id)
            .ok_or_else(|| PluginError {
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
    fn native_loader_or_err(
        &self,
        plugin_id: &str,
    ) -> Result<&Arc<NativePluginLoader>, PluginError> {
        self.native_loader.as_ref().ok_or_else(|| PluginError {
            message: format!(
                "Native plugin '{}' invoked but no NativePluginLoader configured",
                plugin_id
            ),
            code: Some("NATIVE_LOADER_NOT_CONFIGURED".to_string()),
            source: Some("plugin-invoker".to_string()),
        })
    }

    /// 加载 cdylib（resolve artifact + 直接 trait object load，三处 native 路径共用）。
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

    /// 崩溃回调：调用失败且为 panic/fatal 时通知（native pipeline/tool 共用，
    /// 泛型 T——只看 Err 侧错误码，pipeline 与 tool 两种结果类型通用）。
    fn notify_if_crash<T>(&self, plugin_id: &str, result: &Result<T, PluginError>) {
        if let Err(ref e) = result {
            if e.code
                .as_deref()
                .map(|c| c.contains("PANICKED") || c.contains("FATAL"))
                .unwrap_or(false)
            {
                self.notify_crash(plugin_id);
            }
        }
    }

    /// B1（M2-reactive 第一刀）：判定 MCP 客户端是否为「已死亡的 stdio sidecar 进程」。
    ///
    /// `is_alive()` 对 HTTP transport 恒为 false（无子进程），不能单独作死亡信号
    /// （会把外部远程 MCP 误判为崩溃）。`pid()` 仅 stdio spawn 后有值：
    /// - `Some(pid)` + `!is_alive()` → stdio sidecar 进程已退出（真死亡）；
    /// - `None` → HTTP transport（无进程概念，不判死，调用走网络错误语义）。
    async fn is_dead_sidecar(client: &McpClient) -> bool {
        client.pid().await.is_some() && !client.is_alive().await
    }

    /// B1：错误是否为「sidecar 死亡/传输断开」——可透明恢复类失败。
    ///
    /// 仅 `PLUGIN_CRASHED`（attempt 侧经 [`Self::is_dead_sidecar`] 判定）触发恢复；
    /// `MCP_CALL_FAILED`/`MCP_TOOL_CALL_FAILED`（进程仍存活的协议/工具错误）、
    /// `MCP_CONNECT_FAILED`（spawn 失败，重试同样失败）等不重试。
    fn is_recoverable_sidecar_death(err: &PluginError) -> bool {
        err.code.as_deref() == Some("PLUGIN_CRASHED")
    }

    /// B1（M2-reactive 第一刀）：sidecar 死亡**透明恢复**（brokered transparent
    /// recovery）。
    ///
    /// 计划 §9.4 缺口：长事务在途调用时其依赖 B 被 idle GC 杀掉，现状是错误返回
    /// 而非通知+等待，违反空间可组合性。修复：首次尝试失败且判定目标死亡
    /// （`PLUGIN_CRASHED`，见 [`Self::is_recoverable_sidecar_death`]）时：
    ///
    /// 1. `force_unload_impl` 清缓存（kill 残尸 + 清指纹/last_used + OnUnload 事件）；
    /// 2. attempt 内部经 `get_or_create_mcp_client` 重新 spawn；
    /// 3. **重试一次（仅一次，防循环）**；重试成功 → 对调用方完全透明；
    ///    重试仍失败 → 返回第一次的**原错误**（语义仍是「目标插件崩溃」）。
    ///
    /// 可观测性：重试路径记 info 日志（respawn 标记）；最终失败才 `notify_crash`
    /// （崩溃回调语义保留给「恢复失败」——透明恢复成功时不卸载能力、不记
    /// last_crash_ts，插件实际可用）。
    ///
    /// 这是运行时唯一保留的错误重试行为（ADR 2026-08-18：ErrorPolicy 收敛为
    /// 唯一值 RETRY，manifest 字段已清理，不再产生行为分发）。
    async fn with_transparent_recovery<T, F, Fut>(
        &self,
        plugin_id: &str,
        mut attempt: F,
    ) -> Result<T, PluginError>
    where
        F: FnMut() -> Fut,
        Fut: std::future::Future<Output = Result<T, PluginError>>,
    {
        let err = match attempt().await {
            Ok(v) => return Ok(v),
            Err(e) => e,
        };
        if !Self::is_recoverable_sidecar_death(&err) {
            return Err(err);
        }

        // 透明恢复：force_unload 清缓存（best-effort，失败也继续 respawn——
        // get_or_create_mcp_client 自身有「缓存进程已死 → kill+respawn」兜底）。
        info!(
            plugin_id = plugin_id,
            respawn = true,
            "M2-reactive: sidecar died mid-call, transparent recovery (force_unload + respawn + retry once)"
        );
        if let Err(unload_err) = self.force_unload_impl(plugin_id).await {
            warn!(
                plugin_id = plugin_id,
                error = %unload_err.message,
                "transparent recovery: force_unload failed (respawn anyway)"
            );
        }

        match attempt().await {
            Ok(v) => {
                info!(
                    plugin_id = plugin_id,
                    respawn = true,
                    recovered = true,
                    "M2-reactive: transparent recovery succeeded"
                );
                Ok(v)
            }
            Err(retry_err) => {
                warn!(
                    plugin_id = plugin_id,
                    respawn = true,
                    recovered = false,
                    retry_error = %retry_err.message,
                    "M2-reactive: retry after respawn failed, returning original error"
                );
                // 恢复失败 → 保留崩溃回调语义（卸载能力 + 告警 + last_crash_ts）。
                self.notify_crash(plugin_id);
                Err(err)
            }
        }
    }

    /// sidecar pipeline 调用**单次尝试**（B1：不含恢复逻辑，供
    /// [`Self::with_transparent_recovery`] 重试）。
    ///
    /// 死亡判定（`PLUGIN_CRASHED`，可透明恢复）出现在两处：
    /// ① 调用前存活检查（stdio 进程已死；HTTP transport 无进程不判死）；
    /// ② call_tool 失败后复查存活——在途调用中死亡（依赖被 idle GC 杀掉的
    ///    §9.4 场景；进程仍存活则归 MCP_CALL_FAILED，协议/工具错误重试无益）。
    async fn attempt_sidecar_pipeline(
        &self,
        plugin_id: &str,
        manifest: &PluginManifest,
        ctx: &PluginContext,
    ) -> Result<PluginResult, PluginError> {
        // ADR 附录 D③（P6 命名治理）：从 manifest.invoke_entry 取 MCP 入口名
        // （如 "context_build.execute"）。不再回退 capabilities.tools 或字面量
        // "execute"——缺 invoke_entry 是 manifest 错误，必须显式暴露。
        // 启动期 discover 聚合校验是主门；此处为运行期防线（深度防御）。
        let tool_name = manifest
            .invoke_entry
            .as_deref()
            .ok_or_else(|| PluginError {
                message: format!(
                    "pipeline plugin '{}' missing manifest.invoke_entry (ADR 附录 D②)",
                    plugin_id
                ),
                code: Some("MISSING_INVOKE_ENTRY".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;

        // Sidecar 模式：通过 MCP 客户端调用
        let client_arc = self.get_or_create_mcp_client(manifest).await?;
        let client = client_arc.read().await;

        // ① 调用前存活检查（B1：死亡 → PLUGIN_CRASHED，交由恢复包装器 respawn+重试）
        if Self::is_dead_sidecar(&client).await {
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

        let result = match client.call_tool(tool_name, &tool_args).await {
            Ok(v) => v,
            Err(e) => {
                // ② 失败后复查存活：死亡（含在途死亡）→ PLUGIN_CRASHED（可透明恢复）；
                //    存活 → MCP_CALL_FAILED 语义（协议/工具错误）。
                //    （call_tool 已把底层错误统一包成 ToolCallFailed，无法按错误
                //    类型区分崩溃，故用存活复查作权威判定。）
                let died_mid_call = Self::is_dead_sidecar(&client).await;
                return Err(if died_mid_call {
                    PluginError {
                        message: format!("plugin process died mid-call: {}: {}", plugin_id, e),
                        code: Some("PLUGIN_CRASHED".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    }
                } else {
                    PluginError {
                        message: format!("MCP call failed: {}", e),
                        code: Some("MCP_CALL_FAILED".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    }
                });
            }
        };

        // 解析 MCP 响应——Python SDK 返回格式为：
        // { "content": [{ "type": "text", "text": "<json_string>" }], "isError": false }
        // 提取 content[0].text 并反序列化为 PluginResult
        let inner = extract_mcp_content(&result);
        let plugin_result: PluginResult =
            serde_json::from_value(inner).map_err(|e| PluginError {
                message: format!("failed to parse MCP response as PluginResult: {}", e),
                code: Some("PARSE_ERROR".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;

        Ok(plugin_result)
    }

    /// sidecar tool 调用**单次尝试**（B1：与 [`Self::attempt_sidecar_pipeline`] 同构）。
    async fn attempt_sidecar_tool(
        &self,
        plugin_id: &str,
        manifest: &PluginManifest,
        tool_name: &str,
        inputs: &Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        let client_arc = self.get_or_create_mcp_client(manifest).await?;
        let client = client_arc.read().await;

        // ① 调用前存活检查（B1：死亡 → PLUGIN_CRASHED，可透明恢复）
        if Self::is_dead_sidecar(&client).await {
            return Err(PluginError {
                message: format!("plugin process crashed: {}", plugin_id),
                code: Some("PLUGIN_CRASHED".to_string()),
                source: Some("plugin-invoker".to_string()),
            });
        }

        let result = match client.call_tool(tool_name, inputs).await {
            Ok(v) => v,
            Err(e) => {
                // ② 失败后复查存活（在途死亡 → PLUGIN_CRASHED；否则原
                //    MCP_TOOL_CALL_FAILED 语义）
                let died_mid_call = Self::is_dead_sidecar(&client).await;
                return Err(if died_mid_call {
                    PluginError {
                        message: format!("plugin process died mid-call: {}: {}", plugin_id, e),
                        code: Some("PLUGIN_CRASHED".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    }
                } else {
                    PluginError {
                        message: format!("MCP tool call failed: {}", e),
                        code: Some("MCP_TOOL_CALL_FAILED".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    }
                });
            }
        };

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
        // 决策树（统一 Python ToolResult 与 ToolExecutionResult 两种信封）：
        //   ②-a 返回已是 ToolExecutionResult 信封（同时带 success + data）→ 直接 from_value
        //   ②-b Python ToolResult 形状（带 success 但无 data，有 output/error）→ 归一化：
        //        success=true  → success(data=output)
        //        success=false → failure(error)
        //   ①   MCP isError=true / 工具返回 {"error":"..."}（无 success）→ failure
        //   ③   纯业务数据（无 success 无 error）→ success(data=inner)
        let tool_result = if inner.get("success").is_some() && inner.get("data").is_some() {
            // ②-a 真 ToolExecutionResult 信封
            serde_json::from_value(inner).map_err(|e| PluginError {
                message: format!("failed to parse MCP response as ToolExecutionResult: {}", e),
                code: Some("PARSE_ERROR".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?
        } else if inner.get("success").is_some() {
            // ②-b 带 success 但无 data 字段。两种实际形态：
            //   (A) builtin_tools 的 ToolResult.to_dict() 信封 {success, output, error, metadata}
            //       → 业务数据在 output 里，取 output 作为 data。
            //   (B) memory 等 server.py 把 result.output 解包后直接返回（inner 本身就是业务 dict，
            //       恰好带 success 键，如 {success:true, memory_id:...}，无 output 键）
            //       → inner 本身即 data。
            //   区分：inner 有 "output" 键 → (A)；否则 → (B)。
            //   与流式 tool_result 事件使用同一个 success 信号（tool_core/src/lib.rs:351）。
            let ok = inner
                .get("success")
                .and_then(|v| v.as_bool())
                .unwrap_or(true);
            if ok {
                let data = if inner.get("output").is_some() {
                    inner
                        .get("output")
                        .cloned()
                        .unwrap_or(serde_json::Value::Null)
                } else {
                    inner.clone()
                };
                ToolExecutionResult::success(data)
            } else {
                let err_msg = inner
                    .get("error")
                    .and_then(|v| v.as_str())
                    .unwrap_or("tool execution failed");
                ToolExecutionResult::failure(err_msg)
            }
        } else if let Some(err) = inner.get("error").and_then(|v| v.as_str()) {
            // ① MCP isError=true 或工具自身返回 {"error": "..."} 且无 success 字段
            ToolExecutionResult::failure(err)
        } else {
            // ③ 纯业务数据 → 包成 success 信封
            ToolExecutionResult::success(inner)
        };

        Ok(tool_result)
    }

    /// 原生插件 pipeline 调用：config 注入 + 热重载 + 崩溃回调，与 sidecar 对齐。
    ///
    /// 直接 trait 对象版：构造 HostServices（包 router）+ PluginCtx，调 loader.execute
    /// 直接 trait 派发。不经 JSON 序列化中间层，无 host_call 函数指针。
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
            &self
                .loader
                .load_config()
                .await
                .unwrap_or(serde_json::Value::Null),
            manifest,
        );

        // 构造 PluginCtx（state/config 用 JSON 字符串；tool_call_json=None = pipeline 语义）。
        let plugin_ctx = agentos_native_sdk::PluginCtx {
            state_json: serde_json::to_string(&ctx.state).unwrap_or_else(|_| "{}".into()),
            config_json: serde_json::to_string(&config).unwrap_or_else(|_| "{}".into()),
            tenant_id: ctx.tenant.tenant_id.clone(),
            session_id: ctx.tenant.session_id.clone(),
            task_id: ctx.task_id.clone(),
            pipeline_id: ctx.pipeline_id.to_string(),
            tool_call_json: None,
        };

        // 构造 HostServices（包 router）。router 缺失则 host=None，插件降级。
        // G6：携带 plugin_id 信任锚点（单点授权在 CapabilityRouter 校验）。
        // G7：execute 跑在 spawn_blocking 线程（cdylib 同步 C-ABI 可能长计算，
        // 不阻塞 tokio worker）；HostServices 标记 on_blocking_thread，
        // 内部 bridge 用直接 block_on（block_in_place 在 blocking 线程会 panic）。
        let host_svc: Option<NativeHostServices> =
            self.router
                .read()
                .as_ref()
                .map(|router| NativeHostServices {
                    router: Arc::clone(router),
                    plugin_id: plugin_id.to_string(),
                    on_blocking_thread: true,
                });

        let loader = Arc::clone(loader);
        let pid = plugin_id.to_string();
        let result = tokio::task::spawn_blocking(move || {
            let host_ref: Option<&dyn agentos_native_sdk::HostServices> = host_svc
                .as_ref()
                .map(|h| h as &dyn agentos_native_sdk::HostServices);
            loader.execute(&pid, &plugin_ctx, host_ref)
        })
        .await
        .map_err(|join_err| PluginError {
            message: format!(
                "native plugin '{}' execute task panicked: {}",
                plugin_id, join_err
            ),
            code: Some("NATIVE_EXECUTE_PANICKED".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;

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

    /// 原生插件 tool 调用（B2：native 工具插件支持，M2 计划任务 B）。
    ///
    /// 复用生命周期钩子 `hook` 字段的既有模式：PluginCtx 加 **约定字段**
    /// `tool_call_json = {"name": tool_name}`（概念上即 PluginInput 的
    /// `{state: inputs, config: 注入配置, tool_call: {name: tool_name}}`），
    /// 经 native_loader 的 execute 入口调用——与 pipeline execute 同一 C-ABI 入口，
    /// 约定字段区分语义。
    ///
    /// 插件侧约定：execute 见 tool_call 字段走工具逻辑，返回 ToolExecutionResult
    /// 形状 JSON；**旧插件不认识该字段 → 忽略，按 pipeline 逻辑返回 state_updates**
    /// ——调用侧（[`normalize_native_tool_output`]）检测返回形状归一：能解析成
    /// ToolExecutionResult 信封就用，否则按现状包 success 信封（零破坏）。
    ///
    /// crash 语义对齐 [`Self::notify_if_crash`]（execute panic/fatal → 崩溃回调）。
    async fn invoke_native_tool(
        &self,
        plugin_id: &str,
        manifest: &PluginManifest,
        tool_name: &str,
        inputs: &Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        let loader = self.native_loader_or_err(plugin_id)?;

        // 热重载指纹检测（与 invoke_native_pipeline 同逻辑）：代码/配置变更告警。
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
            &self
                .loader
                .load_config()
                .await
                .unwrap_or(serde_json::Value::Null),
            manifest,
        );

        // 构造 PluginCtx：state=inputs（工具入参），tool_call 约定字段表达工具语义。
        // 工具调用无 PluginContext（invoke_tool 不带租户/管道），租户等字段留空——
        // 与 sidecar tools/call 直传 inputs（不带租户）对齐。
        let plugin_ctx = agentos_native_sdk::PluginCtx {
            state_json: serde_json::to_string(inputs).unwrap_or_else(|_| "{}".into()),
            config_json: serde_json::to_string(&config).unwrap_or_else(|_| "{}".into()),
            tenant_id: String::new(),
            session_id: String::new(),
            task_id: String::new(),
            pipeline_id: String::new(),
            tool_call_json: Some(
                serde_json::to_string(&serde_json::json!({ "name": tool_name }))
                    .unwrap_or_default(),
            ),
        };

        // 构造 HostServices（包 router）。G6/G7 语义与 invoke_native_pipeline 同构：
        // 携带 plugin_id 信任锚点；execute 跑 spawn_blocking（C-ABI 同步可能长计算）。
        let host_svc: Option<NativeHostServices> =
            self.router
                .read()
                .as_ref()
                .map(|router| NativeHostServices {
                    router: Arc::clone(router),
                    plugin_id: plugin_id.to_string(),
                    on_blocking_thread: true,
                });

        let loader = Arc::clone(loader);
        let pid = plugin_id.to_string();
        let raw = tokio::task::spawn_blocking(move || {
            let host_ref: Option<&dyn agentos_native_sdk::HostServices> = host_svc
                .as_ref()
                .map(|h| h as &dyn agentos_native_sdk::HostServices);
            loader.execute(&pid, &plugin_ctx, host_ref)
        })
        .await
        .map_err(|join_err| PluginError {
            message: format!(
                "native plugin '{}' execute task panicked: {}",
                plugin_id, join_err
            ),
            code: Some("NATIVE_EXECUTE_PANICKED".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;

        // execute 返回 JSON 字符串 → 解析 + 归一为 ToolExecutionResult（对齐
        // invoke_tool 的 sidecar 决策树：纯业务数据包 success 信封）。
        let result = raw.and_then(|out_json| {
            let inner: Value = serde_json::from_str(&out_json).map_err(|e| PluginError {
                message: format!(
                    "native plugin '{}' tool output parse failed: {}",
                    plugin_id, e
                ),
                code: Some("NATIVE_OUTPUT_PARSE".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;
            Ok(normalize_native_tool_output(&inner))
        });

        // crash 语义对齐（notify_if_crash：PANICKED/FATAL → 崩溃回调）。
        self.notify_if_crash(plugin_id, &result);
        result
    }

    /// 设置 Capability 路由器（启用 sidecar→内核反向调用）。
    ///
    /// 必须在 engine 创建后调用（路由器需要 engine 句柄）。
    /// 之后新建的 MCP 客户端会自动带上路由器；**已缓存的客户端直接废弃**
    /// （kill + 移除，下次调用 respawn）——缓存命中路径不会重连，若保留
    /// 旧实例，router 前 spawn 的 sidecar（如 boot 期 G2 存量校验窗口，
    /// 见 agentos-kernel.rs 注册闸 G2 块与 set_router 的顺序）将以空
    /// capabilities initialize 长存，插件反向调用（tool-executor/
    /// service-registry）永远 KeyError（2026-08-19 e2e 实测 memory
    /// 后端"未注入"根因）。
    pub fn set_router(&self, router: Arc<dyn CapabilityRouter>) {
        // sidecar：存 router，新建 MCP client 时带上（PluginScopedRouter）。
        // native：router 存此，execute 时包成 NativeHostServices 注入 ExecContext
        // （host 调 capability 经其走 router.handle）。
        // 一行接通两种 host_type 的 capability 反向调用。
        *self.router.write() = Some(router);

        // 废弃全部已缓存 sidecar：它们 initialize 时拿到的 capabilities 集合
        // 是旧 router 状态（可能为空）的快照，且永不重连。kill 是 best-effort
        // （失败仅 debug 日志——respawn 时 is_alive 会走崩溃清理兜底）。
        let invalidated: Vec<(String, Arc<tokio::sync::RwLock<McpClient>>)> =
            self.mcp_clients.write().drain().collect();
        if invalidated.is_empty() {
            return;
        }
        let kill = async move {
            for (id, client) in invalidated {
                if let Err(e) = client.write().await.kill().await {
                    tracing::debug!(
                        "set_router invalidate: best-effort kill of sidecar {id} failed: {e}"
                    );
                }
            }
        };
        // 同步上下文（无 tokio runtime 的测试等）下无法 spawn 异步 kill：
        // 缓存已清空（下次调用必然 respawn 新实例），旧进程交由 stdio 关闭/
        // idle GC 兜底，宁可短暂冗余也不 panic。
        match tokio::runtime::Handle::try_current() {
            Ok(handle) => {
                handle.spawn(kill);
            }
            Err(_) => {
                tracing::debug!(
                    "set_router: no tokio runtime, cached sidecars dropped without kill"
                );
            }
        }
    }

    /// 注入生命周期钩子事件总线（旁路广播，可选）。
    ///
    /// 镜像 [`set_router`] 的 `&self` 注入形态：内核 main 在创建总线后、spawn 任何
    /// sidecar 前调用，把同一 `Arc<HookEventBus>` 注入 invoker，使后续 sidecar spawn
    /// 的 OnLoad 等生命周期事件在点对点直调**旁路** fan-out 给审计/指标订阅者。
    /// 未调用时（`None`）行为完全不变——直调仍是唯一路径。
    pub fn set_hook_bus(&self, bus: Arc<HookEventBus>) {
        *self.hook_bus.write() = Some(bus);
    }

    /// 旁路广播 OnError 给总线订阅者（审计 / `lifecycle.plugin_error_total` 计数）。
    ///
    /// 与 OnLoad 的旁路 emit 对称：插件 execute/call 返回 `Err` 时由调用方（
    /// [`invoke_pipeline_plugin`] / [`invoke_tool`] 的中央错误返回处）调一次本方法，
    /// 把"插件调用失败"这一事实 fan-out 给观察层。**不改错误处理语义**——原 `Err`
    /// 照常向上传播，本方法仅 fire-and-forget 观察一次。
    ///
    /// best-effort、非阻塞：未注入总线（`None`，如单测）时 no-op，行为不变。
    fn emit_lifecycle_error(&self, plugin_id: &str, err: &PluginError) {
        let bus_guard = self.hook_bus.read();
        let Some(bus) = bus_guard.as_ref() else {
            return;
        };
        let mut ctx = HookContext::new();
        ctx.set("plugin_id", json!(plugin_id));
        ctx.set("error", json!(err.message));
        if let Some(code) = &err.code {
            ctx.set("error_code", json!(code));
        }
        bus.emit(LifecycleEvent {
            hook: LifecycleHook::OnError,
            ctx,
            target: EventTarget::Plugin(plugin_id.to_string()),
            ts: SystemTime::now(),
        });
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
    /// 串行化——并发触发只创建一个 sidecar，不遗留孤儿进程。
    async fn get_or_create_mcp_client(
        &self,
        manifest: &PluginManifest,
    ) -> Result<Arc<tokio::sync::RwLock<McpClient>>, PluginError> {
        // Fast path：无锁查缓存，命中且存活直接返回（热路径，避开 spawn 锁开销）。
        {
            let cached = {
                let clients = self.mcp_clients.read();
                clients.get(&manifest.id).cloned()
            };
            if let Some(client) = cached {
                let client_guard = client.read().await;
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
                        drop(client_guard);
                        if let Err(e) = client.write().await.kill().await {
                            tracing::debug!(
                                "hot-reload: best-effort kill of stale sidecar {} failed (will respawn): {e}",
                                manifest.id
                            );
                        }
                        self.mcp_clients.write().remove(&manifest.id);
                        // 不调 notify_crash（这不是崩溃，是主动热更新）
                    } else {
                        self.touch_last_used(&manifest.id);
                        return Ok(Arc::clone(&client));
                    }
                } else {
                    // 进程已崩溃——显式 kill 旧客户端再创建新的
                    error!("Plugin process crashed: {}", manifest.id);
                    drop(client_guard);
                    if let Err(e) = client.write().await.kill().await {
                        tracing::debug!(
                            "crash cleanup: best-effort kill of crashed sidecar {} failed: {e}",
                            manifest.id
                        );
                    }
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
                let client_guard = client.read().await;
                if client_guard.is_alive().await {
                    self.touch_last_used(&manifest.id);
                    return Ok(Arc::clone(&client));
                }
                // 极端情况：double-check 时又崩溃——kill 后继续 spawn
                drop(client_guard);
                if let Err(e) = client.write().await.kill().await {
                    tracing::debug!(
                        "double-check: best-effort kill of sidecar {} failed (will respawn): {e}",
                        manifest.id
                    );
                }
                self.mcp_clients.write().remove(&manifest.id);
            }
        }

        // 创建新的 MCP 客户端（持有 spawn 锁，保证串行）
        //
        // 传输三路分流（由 manifest.mcp 决定）：
        // ① mcp.transport=StreamableHttp + endpoint.url → 外部远程 MCP，走 HTTP（不 spawn）。
        // ② mcp.transport=Stdio + endpoint.command → 外部本地第三方命令 MCP（如 npx），
        //    spawn endpoint.command（不经 parse_entry，entry 仅作语义标记）。
        // ③ 其余（无 mcp 配置的项目自带 sidecar）→ parse_entry(entry) → stdio。
        let mut client = match manifest.mcp.as_ref() {
            Some(cfg) if cfg.transport == agentos_core::traits::McpTransport::StreamableHttp => {
                let ep = cfg.endpoint.as_ref().ok_or_else(|| PluginError {
                    message: format!(
                        "插件 {} 声明 streamable_http 但缺 endpoint.url",
                        manifest.id
                    ),
                    code: Some("MCP_CONFIG_INVALID".to_string()),
                    source: Some("plugin-invoker".to_string()),
                })?;
                let url = ep.url.clone().ok_or_else(|| PluginError {
                    message: format!("插件 {} 的 streamable_http endpoint 缺 url", manifest.id),
                    code: Some("MCP_CONFIG_INVALID".to_string()),
                    source: Some("plugin-invoker".to_string()),
                })?;
                tracing::info!(
                    "[invoker] 插件 {} 走 HTTP transport（外部 MCP）| url={}",
                    manifest.id,
                    url
                );
                McpClient::new_http(url, ep.headers.clone(), ep.auth.clone())
                    .with_plugin_id(&manifest.id)
            }
            Some(cfg)
                if cfg.transport == agentos_core::traits::McpTransport::Stdio
                    && cfg
                        .endpoint
                        .as_ref()
                        .and_then(|e| e.command.as_ref())
                        .is_some() =>
            {
                // 外部本地第三方命令 MCP（如 npx playwright）：spawn endpoint.command。
                let ep = cfg.endpoint.as_ref().expect("checked above");
                let command = ep.command.clone().unwrap_or_default();
                tracing::info!(
                    "[invoker] 插件 {} 走外部 stdio 命令 | command={} {}",
                    manifest.id,
                    command,
                    ep.args.join(" ")
                );
                let mut c =
                    McpClient::new_stdio(command, ep.args.clone()).with_plugin_id(&manifest.id);
                // env 值含 ${ENV_VAR} 占位 → 解析（缺失则启动失败早暴露）。
                let mut extra_env: Vec<(String, String)> = Vec::new();
                for (k, v) in &ep.env {
                    let resolved = resolve_env_placeholders(v).map_err(|e| PluginError {
                        message: format!("插件 {} env 解析失败: {}", manifest.id, e),
                        code: Some("MCP_CONFIG_INVALID".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    })?;
                    extra_env.push((k.clone(), resolved));
                }
                if !extra_env.is_empty() {
                    c = c.with_extra_env(extra_env);
                }
                c
            }
            _ => {
                let (command, args) = self.parse_entry(&manifest.entry)?;
                let mut c = McpClient::new_stdio(command, args)
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
                        c = c.with_router(scoped);
                    }
                }

                // 设置工作目录为插件目录（确保 server.py 等相对路径可解析）
                if let Some(plugin_dir) = self.loader.get_plugin_dir(&manifest.id) {
                    c = c.with_working_dir(plugin_dir);
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
                    c = c.with_extra_env(extra_env);
                }
                c
            }
        };

        // 插件级调用超时（manifest.mcp.request_timeout_secs）：长等待业务
        // （human-interaction.wait_for_choice 的 24h 审批等）必须显式声明，
        // 否则内核 MCP client 300s 默认兜底先于用户操作掐断调用（2026-08-17
        // 审批 5 分钟窗口实锤：-32001 超时 → 审批作废 → 引擎重试弹窗循环）。
        if let Some(secs) = manifest.mcp.as_ref().and_then(|m| m.request_timeout_secs) {
            client = client.with_request_timeout(std::time::Duration::from_secs(secs));
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
                if e.code
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
            .inspect_err(|e| warn!("on_load notification failed for {}: {}", manifest.id, e));

        // 旁路广播 OnLoad 给总线订阅者（审计/指标）。与上方 `notifications/on_load`
        // 直调属**同一生命周期事件**：直调是权威路径（通知目标插件初始化），
        // 总线是观察路径（审计日志 / `lifecycle.plugin_load_total` 计数）。
        // sidecar 走到此处即视为"已加载"（连接 + initialize 握手完成），无论直调
        // 通知成功与否——观察层关注的是"插件进程被加载起跑"这一事实。
        // best-effort、非阻塞；未注入总线（`None`，如单测）时无操作，行为不变。
        let bus_guard = self.hook_bus.read();
        if let Some(bus) = bus_guard.as_ref() {
            let mut ctx = HookContext::new();
            ctx.set("plugin_id", json!(manifest.id));
            bus.emit(LifecycleEvent {
                hook: LifecycleHook::OnLoad,
                ctx,
                target: EventTarget::Plugin(manifest.id.clone()),
                ts: SystemTime::now(),
            });
        }

        info!(
            "MCP client connected and initialized: plugin={}",
            manifest.id
        );

        let client_arc = Arc::new(tokio::sync::RwLock::new(client));

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
            let guard = client.read().await;
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

        // 声明了要联网（有非空 host 列表）说明确实要联网，记 info 留痕。
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

    /// 统一软卸载：按 host_type 分流，进程 kill 但 manifest 描述保留（下次调用重新 spawn）。
    ///
    /// - sidecar：复用 force_unload_impl（kill 进程 + 清缓存）
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
    /// 每 30s 扫描 last_used，对空闲超过阈值的插件调 unload_if_idle
    /// （sidecar kill 进程，manifest 描述保留，下次调用重新 spawn）。
    /// 对齐 trait 文档声明的「空闲超时自动卸载」设计原则。
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
        // 快照当前所有"活跃"插件 id，避免长时间持锁
        let candidates: Vec<String> = {
            let sidecar_ids: Vec<String> = self.last_used.read().keys().cloned().collect();
            let mut all: Vec<String> = sidecar_ids;
            all.sort();
            all.dedup();
            all
        };

        let now = Instant::now();
        for plugin_id in candidates {
            // sidecar 空闲判定
            let idle_secs = self
                .last_used
                .read()
                .get(&plugin_id)
                .map(|t| now.duration_since(*t).as_secs())
                .unwrap_or(0);
            if idle_secs == 0 {
                continue;
            }
            let threshold = self.idle_timeout_secs_sync(&plugin_id);
            // threshold == 0 表示该插件声明"永不空闲卸载"，跳过。
            if threshold != 0 && idle_secs > threshold {
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
    ///
    /// 优先级：插件 manifest 的 lifecycle.idle_timeout_secs > 全局环境变量 > 默认 300s。
    /// 返回 0 表示该插件声明"永不空闲卸载"。
    fn idle_timeout_secs_sync(&self, plugin_id: &str) -> u64 {
        // 1) 插件 manifest 声明优先——让每个插件自定义生命周期。
        //    Some(0) = 永不空闲卸载（human_interaction 等阻塞型工具等待用户输入）。
        if let Some(secs) = self
            .loader
            .get_manifest(plugin_id)
            .and_then(|m| m.lifecycle)
            .and_then(|lc| lc.idle_timeout_secs)
        {
            return secs;
        }
        // 2) 全局环境变量覆盖默认
        if let Ok(v) = std::env::var("AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS") {
            if let Ok(secs) = v.parse::<u64>() {
                if secs > 0 {
                    return secs;
                }
            }
        }
        // 3) 默认 300s
        agentos_core::traits::default_idle_timeout()
    }

    /// 强制卸载插件的实现（供 trait 方法 force_unload 与内部热重载复用）。
    pub async fn force_unload_impl(&self, plugin_id: &str) -> Result<(), PluginError> {
        // 旁路广播 OnUnload（杀进程/卸载之前）。与 get_or_create_mcp_client 里的 OnLoad
        // emit 对称：观察层（审计日志 / `lifecycle.plugin_*` 指标）关注"插件进程即将被
        // 卸载"这一事实。best-effort、非阻塞；未注入总线（`None`，如单测）时 no-op。
        {
            let bus_guard = self.hook_bus.read();
            if let Some(bus) = bus_guard.as_ref() {
                let mut ctx = HookContext::new();
                ctx.set("plugin_id", json!(plugin_id));
                bus.emit(LifecycleEvent {
                    hook: LifecycleHook::OnUnload,
                    ctx,
                    target: EventTarget::Plugin(plugin_id.to_string()),
                    ts: SystemTime::now(),
                });
            }
        }

        let client_arc = {
            let mut clients = self.mcp_clients.write();
            clients.remove(plugin_id)
        };

        if let Some(client_arc) = client_arc {
            let mut client = client_arc.write().await;
            // 镜像 OnLoad 的 notifications/on_load：杀进程之前发 on_unload 给插件自己一个
            // 收尾机会（fire-and-forget，不等响应）。失败仅 warn 不阻断——进程可能已崩溃
            // 或不响应该通知，该杀仍杀（卸载语义不变）。
            let _ = client
                .send_notification("notifications/on_unload", None)
                .await
                .inspect_err(|e| warn!("on_unload notification failed for {}: {}", plugin_id, e));
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

        let result = match manifest.host_type {
            HostType::InProcess => {
                // task_11 N2：经 NativePluginLoader 加载 cdylib 并通过 C-ABI 调用。
                // config 注入与 sidecar 同逻辑（shared::build_plugin_input）——两轨对齐。
                self.invoke_native_pipeline(plugin_id, manifest, ctx).await
            }
            HostType::Sidecar => {
                // B1（M2-reactive 第一刀）：sidecar 死亡透明恢复——单次尝试抽出为
                // attempt_sidecar_pipeline，死亡判定（PLUGIN_CRASHED）触发
                // force_unload + respawn + 重试一次（长事务在途调用不被依赖死亡破坏）。
                self.with_transparent_recovery(plugin_id, || async {
                    self.attempt_sidecar_pipeline(plugin_id, manifest, ctx)
                        .await
                })
                .await
            }
        };

        // 旁路广播 OnError：插件 execute/call 失败即在此中央错误返回处 emit 一次
        // （审计日志 / `lifecycle.plugin_error_total` 计数）。best-effort、非阻塞；
        // bus=None 时 no-op。**不改错误处理语义**——原 Err 照常向上传播。
        if let Err(ref e) = result {
            self.emit_lifecycle_error(plugin_id, e);
        }
        result
    }

    /// 调用工具插件执行。
    ///
    /// 按 host_type 透明分发：
    /// - InProcess: 经 NativePluginLoader 加载 cdylib 走 C-ABI（inputs 作为 state）
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

        let result = match manifest.host_type {
            HostType::InProcess => {
                // task_11 N2：原生工具插件——inputs 作为 state，config 同 pipeline 路径注入。
                // B2：经 execute 的 tool_call 约定字段表达工具语义（旧 pipeline 插件零破坏）。
                self.invoke_native_tool(plugin_id, manifest, tool_name, inputs)
                    .await
            }
            HostType::Sidecar => {
                // B1（M2-reactive 第一刀）：sidecar 死亡透明恢复——与 pipeline 路径同构。
                self.with_transparent_recovery(plugin_id, || async {
                    self.attempt_sidecar_tool(plugin_id, manifest, tool_name, inputs)
                        .await
                })
                .await
            }
        };

        // 旁路广播 OnError：工具插件 execute/call 失败即在此中央错误返回处 emit 一次
        // （审计日志 / `lifecycle.plugin_error_total` 计数）。best-effort、非阻塞；
        // bus=None 时 no-op。**不改错误处理语义**——原 Err 照常向上传播。
        if let Err(ref e) = result {
            self.emit_lifecycle_error(plugin_id, e);
        }
        result
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
            LifecycleHook::DomainEvent => "domain_event",
        };
        let tags = serde_json::to_value(context.tags()).unwrap_or_default();

        match manifest.host_type {
            HostType::Sidecar => {
                // Sidecar：经 MCP notification 发送（fire-and-forget）。
                if let Ok(client_arc) = self.get_or_create_mcp_client(manifest).await {
                    let client = client_arc.read().await;
                    if client.is_alive().await {
                        let hook_method = format!("notifications/{hook_name}");
                        if let Err(e) = client.send_notification(&hook_method, Some(tags)).await {
                            warn!("Lifecycle notification failed for {}: {}", plugin_id, e);
                        }
                    }
                }
            }
            HostType::InProcess => {
                // Native：没有 MCP 通知通道，钩子经 execute 传递——PluginInput 带
                // `hook` 字段（值为钩子名）+ config。插件 SDK 见到 hook 字段走钩子逻辑。
                // 错误仅 warn（与 sidecar 的 fire-and-forget 语义一致，不阻断管道）。
                if let Err(e) = self
                    .send_hook_via_execute(plugin_id, manifest, hook_name, &tags)
                    .await
                {
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

    /// G2：拉取插件实际上报的工具清单（`tools/list` 原始 JSON）。
    ///
    /// 仅 sidecar 支持（native/wasm 暂无 describe 通道，返回不支持错误——api 层
    /// 对这两类跳过校验，待 describe 机制落地后补全）。
    /// 语义：spawn/复用 MCP 连接 → `tools/list`；**本次是新 spawn 的连接则校验后
    /// 回收（kill + 移除缓存）**——安装期校验不破坏懒加载（复用中的连接不回收）。
    async fn list_plugin_tools(&self, plugin_id: &str) -> Result<serde_json::Value, PluginError> {
        let loaded = self.loader.load(plugin_id).await?;
        let manifest = &loaded.manifest;
        if manifest.host_type != HostType::Sidecar {
            return Err(PluginError {
                message: format!(
                    "list_plugin_tools 暂不支持 host_type={:?}（native/wasm 的 describe 通道待 G2 后续落地）",
                    manifest.host_type
                ),
                code: None,
                source: None,
            });
        }
        // 新 spawn 判定：校验前缓存里没有该插件的存活连接（本次会 spawn 新进程）
        let was_new = {
            let cached = self.mcp_clients.read().get(plugin_id).cloned();
            match cached {
                Some(client) => {
                    let guard = client.read().await;
                    !guard.is_alive().await
                }
                None => true,
            }
        };
        let client = self.get_or_create_mcp_client(manifest).await?;
        let raw = {
            let guard = client.read().await;
            guard.list_tools().await.map_err(|e| PluginError {
                message: format!("tools/list failed for {plugin_id}: {e}"),
                code: None,
                source: None,
            })?
        };
        // 本次新 spawn 的进程：校验完回收（kill + 移除缓存），懒加载语义不被破坏
        if was_new {
            if let Err(e) = client.write().await.kill().await {
                tracing::debug!(
                    "G2 verify: best-effort kill of freshly spawned sidecar {} failed (idle GC will reap): {e}",
                    plugin_id
                );
            }
            self.mcp_clients.write().remove(plugin_id);
        }
        Ok(raw)
    }
}

impl PluginInvokerImpl {
    /// 确保生命周期钩子时机 native 插件已加载（钩子当前无 native 消费者，显式 no-op）。
    ///
    /// native 插件没有 MCP 通知通道，钩子当前也无 native 侧消费者，本函数
    /// 不传递负载，仅在钩子时机触发一次加载（保持热重载检测一致性）。
    async fn send_hook_via_execute(
        &self,
        plugin_id: &str,
        manifest: &PluginManifest,
        _hook_name: &str,
        _tags: &Value,
    ) -> Result<(), PluginError> {
        // 钩子负载当前无 native 消费者，不构造；仅保留 config_files 读取失败的
        // Err 传播语义（injected_config 求值即校验）。
        let _config = crate::shared::injected_config(self.loader.as_ref(), manifest).await?;
        if manifest.host_type == HostType::InProcess {
            // 直接 trait 对象模型：生命周期钩子经 PipelinePlugin trait 之外
            // 的独立契约传递（当前 tool_core 无 on_load/on_unload 需求，暂 no-op）。
            // 仅确保插件已加载（保持热重载检测一致性）。
            //
            // B2 后注：PluginCtx 已有同构的「约定字段表达特殊调用」先例——
            // `tool_call_json`（工具调用语义，见 invoke_native_tool）。钩子若将来
            // 需要 native 侧消费，可按同一模式加 `hook_json` 字段经 execute 直调
            // （当前无消费者，保持 no-op）。
            if let Some(loader) = self.native_loader.as_ref() {
                self.load_native(loader, plugin_id, manifest)?;
            }
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
    use agentos_core::traits::{LoadedPlugin, PluginManifest, PluginStatus};
    use agentos_core::types::TenantContext;
    use serde_json::json;
    use uuid::Uuid;

    /// 串行化 cdylib 加载的 native e2e 测试。
    /// 直接 trait 对象的 root module 加载用全局初始化，多线程并发加载不同 cdylib 会竞争，
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

        fn get_manifest(&self, plugin_id: &str) -> Option<PluginManifest> {
            self.manifests.read().get(plugin_id).cloned()
        }
    }

    #[allow(dead_code)]
    fn make_sidecar_manifest(id: &str, entry: &str) -> PluginManifest {
        PluginManifest {
            id: id.to_string(),
            name: format!("Test {}", id),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Tool,
            pipeline_role: None,
            language: "python".to_string(),
            host_type: HostType::Sidecar,
            entry: entry.to_string(),
            capabilities: Default::default(),
            requires_services: vec![],
            permissions: Default::default(),
            error_policy: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            granted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
        }
    }

    fn make_inprocess_manifest(id: &str) -> PluginManifest {
        PluginManifest {
            id: id.to_string(),
            name: format!("Test {}", id),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            pipeline_role: None,
            language: "rust".to_string(),
            host_type: HostType::InProcess,
            entry: "test_entry".to_string(),
            capabilities: Default::default(),
            requires_services: vec![],
            permissions: Default::default(),
            error_policy: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            granted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
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

    /// 构造完整装配的 invoker：真实 loader discover plugins/shared + 注入 native runtime。
    async fn fully_wired_invoker_for_e2e() -> PluginInvokerImpl {
        let plugins_dir = repo_root().join("plugins/shared");
        let loader = Arc::new(agentos_plugin_loader::PluginLoaderImpl::new(
            plugins_dir.clone(),
            None,
        ));
        loader.discover(&[]).await.unwrap();
        let native_loader = Arc::new(NativePluginLoader::new());
        PluginInvokerImpl::new(loader).set_native_loader(native_loader)
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
    // 测试串行化锁需覆盖整个 async 测试体，刻意跨 await 持有，改异步锁会改变串行语义。
    #[allow(clippy::await_holding_lock)]
    async fn e2e_native_plugins_load_and_execute() {
        // 验证直接 trait 对象改造后 tool_core 原生插件能加载 + 经 HostServices 真正执行工具。
        // 注：NativeHostServices 用 block_in_place（需 multi_thread runtime，生产内核即此配置）。
        //
        // 注意：直接 trait 对象的 RootModule 按 NativePluginModule_Ref 类型全局缓存
        // （root_module_statics 全局单例）。同进程加载多个用同一 RootModule 类型的 cdylib
        // 会互相覆盖。故本测试只验证单个原生插件（tool_core，生产环境的唯一原生插件）。
        let _guard = NATIVE_E2E_LOCK.lock();
        let plugins_dir = repo_root().join("plugins/shared");
        // 按平台定位 tool_core cdylib 产物（与 manifest native.artifact 裸名 +
        // platform_artifact_name 补名逻辑一致：Windows→.dll、Linux→lib{}.so、macOS→lib{}.dylib）。
        // 避免硬编码单一平台后缀导致纯 Linux 环境（仅 .so）静默 SKIP，掩盖真实加载路径。
        let tool_core_artifact = if cfg!(windows) {
            plugins_dir.join("pipeline/core/tool_core/pipeline_tool_core_native.dll")
        } else if cfg!(target_os = "macos") {
            plugins_dir.join("pipeline/core/tool_core/libpipeline_tool_core_native.dylib")
        } else {
            plugins_dir.join("pipeline/core/tool_core/libpipeline_tool_core_native.so")
        };
        if !tool_core_artifact.exists() {
            eprintln!(
                "SKIP: tool_core cdylib not built at {}",
                tool_core_artifact.display()
            );
            return;
        }
        let tool_core_parent = plugins_dir.join("pipeline/core");
        let tool_core_parent_str = tool_core_parent.to_string_lossy().to_string();
        let roots: Vec<&str> = vec![&tool_core_parent_str];
        let loader = Arc::new(agentos_plugin_loader::PluginLoaderImpl::new(
            plugins_dir,
            None,
        ));
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
                _params: serde_json::Value,
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
        let result = invoker
            .invoke_pipeline_plugin("pipeline_tool_core", &ctx_tool)
            .await;
        assert!(
            result.is_ok(),
            "tool_core invoke failed: {:?}",
            result.err()
        );
        let pr = result.unwrap();
        // tool_core 必然回写 tool_results + 清空 raw_tool_calls。
        assert!(
            pr.state_updates.contains_key("tool_results"),
            "tool_results missing: {:?}",
            pr.state_updates.keys().collect::<Vec<_>>()
        );
        assert_eq!(pr.state_updates.get("raw_tool_calls"), Some(&json!([])));
        // 关键断言：工具执行成功，结果回写 tool_results（success=true + 输出原文）。
        let tool_results = pr
            .state_updates
            .get("tool_results")
            .and_then(|v| v.as_array())
            .cloned();
        let tr = tool_results.expect("should have tool result array");
        assert_eq!(tr.len(), 1, "should have 1 tool result");
        assert_eq!(tr[0]["success"], true, "tool should succeed: {:?}", tr[0]);
        assert_eq!(
            tr[0]["data"]["output"], "agentos-native-ok\n",
            "tool output should be returned: {:?}",
            tr[0]
        );
        // messages 重建：assistant tool_calls + tool 结果消息（op-based state-update
        // 协议——tool_core native 以 `{"_ops":[{op:"set",msg}]}` 增量下发，非裸数组）。
        let msgs_ops = pr
            .state_updates
            .get("messages")
            .and_then(|v| v.get("_ops"))
            .and_then(|v| v.as_array())
            .cloned();
        let msgs_ops = msgs_ops.expect("messages should be rebuilt (op-based _ops)");
        assert!(!msgs_ops.is_empty(), "新增消息应有 _ops 增量");
        let msgs: Vec<&Value> = msgs_ops.iter().filter_map(|op| op.get("msg")).collect();
        assert!(msgs
            .iter()
            .any(|m| m["role"] == "assistant" && m["tool_calls"].is_array()));
        assert!(
            msgs.iter().any(|m| m["role"] == "tool"
                && m["content"]
                    .as_str()
                    .map(|s| s.contains("agentos-native-ok"))
                    .unwrap_or(false)),
            "tool result message should carry output: {:?}",
            msgs
        );
    }

    #[tokio::test]
    #[ignore = "native_test 与 tool_core 共用 NativePluginModule_Ref 全局缓存，同进程并行会冲突；tool_core 已由 e2e_native_plugins 覆盖。单独跑：cargo test e2e_native_inprocess -- --ignored"]
    // 测试串行化锁需覆盖整个 async 测试体，刻意跨 await 持有，改异步锁会改变串行语义。
    #[allow(clippy::await_holding_lock)]
    async fn e2e_native_inprocess_plugin_executes() {
        // 单独验证 native_test echo 插件（基础 native 直接 trait 对象链路）。
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
        assert!(
            result.is_ok(),
            "native plugin invoke failed: {:?}",
            result.err()
        );
        let pr = result.unwrap();
        assert_eq!(
            pr.state_updates.get("processed_by"),
            Some(&json!("test_plugin")),
            "got: {:?}",
            pr.state_updates
        );
    }

    /// B2：native 工具调用端到端（tool_call 约定字段经 execute C-ABI 直调）。
    ///
    /// 测试形态取舍（诚实记录）：invoke_native_tool 依赖真实 cdylib（loader 是
    /// 具体类型 NativePluginLoader，无 mock 注入点），单测无法覆盖完整链路——
    /// 归一逻辑由 normalize_native_tool_output 单测覆盖，本测试用真 cdylib
    /// （native-sdk-test-plugin 构建产物）验证「PluginCtx.tool_call_json → 插件
    /// 工具分支 → ToolExecutionResult 信封」全链路。产物未构建时 SKIP；
    /// Windows 下与其他 e2e_native 同因（STATUS_ACCESS_VIOLATION，HEAD 已知），
    /// 跑法：cargo test -p agentos-invoker --lib -- --skip e2e_native。
    #[tokio::test(flavor = "multi_thread")]
    // 测试串行化锁需覆盖整个 async 测试体，刻意跨 await 持有，改异步锁会改变串行语义。
    #[allow(clippy::await_holding_lock)]
    async fn e2e_native_tool_call_via_execute() {
        let _guard = NATIVE_E2E_LOCK.lock();
        let dll = repo_root().join("plugins/shared/native_test/native_test_plugin.dll");
        if !dll.exists() {
            eprintln!(
                "SKIP: native cdylib not built at {} (build native-sdk-test-plugin and copy)",
                dll.display()
            );
            return;
        }
        let invoker = fully_wired_invoker_for_e2e().await;
        let result = invoker
            .invoke_tool("native_test", "echo_tool", &json!({"hello": "world"}))
            .await;
        assert!(
            result.is_ok(),
            "native tool invoke failed: {:?}",
            result.err()
        );
        let tr = result.unwrap();
        // native-sdk-test-plugin 的工具分支返回 {success:true, data:{tool, echo_args}}
        assert!(tr.success, "tool should succeed: {:?}", tr);
        assert_eq!(tr.data["tool"], "echo_tool");
        assert_eq!(tr.data["echo_args"]["hello"], "world");
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
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Composite,
            pipeline_role: None,
            language: "yaml".to_string(),
            host_type: HostType::InProcess,
            entry: String::new(),
            capabilities: Default::default(),
            requires_services: vec![],
            permissions: Default::default(),
            error_policy: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            granted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
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

    // ── B1（M2-reactive 第一刀）：透明恢复包装器单元测试 ──
    //
    // 测试形态取舍（诚实记录）：完整的「真 sidecar 死亡 → respawn → 重试成功」
    // 需要「成功握手后又死亡」的 MCP 进程（echo 插件 + kill 时机控制），单测内
    // 需真实 python 进程，太重且 Windows 易脆。这里测**重试逻辑函数层**：
    // with_transparent_recovery 以可注入 attempt 闭包暴露，闭包计数模拟
    // 「第一次死亡 / 重试成功 / 重试仍失败」三种序列，覆盖恢复决策全部分支；
    // 死亡判定（is_dead_sidecar）与错误分类（is_recoverable_sidecar_death）
    // 各自独立单测。真进程行为由 e2e_native 家族与集成链路兜底。

    #[tokio::test]
    async fn test_recovery_retry_once_then_success() {
        // 第一次尝试死亡（PLUGIN_CRASHED）→ force_unload + respawn → 第二次成功：
        // 调用方拿到 Ok（完全透明），且不触发崩溃回调（插件实际可用）。
        let loader = Arc::new(MockLoader::new());
        loader.add_manifest(make_sidecar_manifest("recover_ok", "python server.py"));
        let invoker = PluginInvokerImpl::new(loader);

        let crashed = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
        let crashed_clone = Arc::clone(&crashed);
        invoker.on_crash(Arc::new(move |plugin_id: &str| {
            crashed_clone.lock().unwrap().push(plugin_id.to_string());
        }));

        let attempts = Arc::new(std::sync::Mutex::new(0u32));
        let attempts_clone = Arc::clone(&attempts);
        let result: Result<serde_json::Value, PluginError> = invoker
            .with_transparent_recovery("recover_ok", || async {
                let n = {
                    let mut c = attempts_clone.lock().unwrap();
                    *c += 1;
                    *c
                };
                if n == 1 {
                    Err(PluginError {
                        message: "plugin process died mid-call".to_string(),
                        code: Some("PLUGIN_CRASHED".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    })
                } else {
                    Ok(serde_json::json!({"recovered": true}))
                }
            })
            .await;

        assert_eq!(*attempts.lock().unwrap(), 2, "死亡后必须恰好重试一次");
        assert_eq!(result.unwrap()["recovered"], true, "重试成功对调用方透明");
        assert!(
            crashed.lock().unwrap().is_empty(),
            "透明恢复成功不应触发崩溃回调"
        );
    }

    #[tokio::test]
    async fn test_recovery_retry_once_only_returns_original_error() {
        // 重试仍失败 → 返回第一次的**原错误**（仅一次重试，防循环），并触发
        // 崩溃回调（恢复失败保留崩溃语义：卸载能力 + last_crash_ts）。
        let loader = Arc::new(MockLoader::new());
        loader.add_manifest(make_sidecar_manifest("recover_fail", "python server.py"));
        let invoker = PluginInvokerImpl::new(loader);

        let crashed = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
        let crashed_clone = Arc::clone(&crashed);
        invoker.on_crash(Arc::new(move |plugin_id: &str| {
            crashed_clone.lock().unwrap().push(plugin_id.to_string());
        }));

        let attempts = Arc::new(std::sync::Mutex::new(0u32));
        let attempts_clone = Arc::clone(&attempts);
        let result: Result<serde_json::Value, PluginError> = invoker
            .with_transparent_recovery("recover_fail", || async {
                let n = {
                    let mut c = attempts_clone.lock().unwrap();
                    *c += 1;
                    *c
                };
                Err(PluginError {
                    message: format!("death #{}", n),
                    code: Some("PLUGIN_CRASHED".to_string()),
                    source: Some("plugin-invoker".to_string()),
                })
            })
            .await;

        assert_eq!(*attempts.lock().unwrap(), 2, "仅重试一次，不得循环");
        let err = result.unwrap_err();
        assert_eq!(err.code.as_deref(), Some("PLUGIN_CRASHED"));
        assert_eq!(err.message, "death #1", "重试失败返回第一次的原错误");
        assert_eq!(
            crashed.lock().unwrap().as_slice(),
            &["recover_fail".to_string()],
            "恢复失败必须触发一次崩溃回调"
        );
    }

    #[tokio::test]
    async fn test_recovery_non_death_error_no_retry() {
        // 非 death 类失败（MCP_CALL_FAILED 等）不重试——协议/工具错误 respawn 无益。
        let loader = Arc::new(MockLoader::new());
        loader.add_manifest(make_sidecar_manifest("no_retry", "python server.py"));
        let invoker = PluginInvokerImpl::new(loader);

        let attempts = Arc::new(std::sync::Mutex::new(0u32));
        let attempts_clone = Arc::clone(&attempts);
        let result: Result<serde_json::Value, PluginError> = invoker
            .with_transparent_recovery("no_retry", || async {
                *(attempts_clone.lock().unwrap()) += 1;
                Err(PluginError {
                    message: "MCP call failed: protocol error".to_string(),
                    code: Some("MCP_CALL_FAILED".to_string()),
                    source: Some("plugin-invoker".to_string()),
                })
            })
            .await;

        assert_eq!(*attempts.lock().unwrap(), 1, "非 death 错误不重试");
        assert_eq!(result.unwrap_err().code.as_deref(), Some("MCP_CALL_FAILED"));
    }

    #[test]
    fn test_is_recoverable_sidecar_death_classification() {
        // 死亡分类：仅 PLUGIN_CRASHED 可透明恢复。
        let mk = |code: &str| PluginError {
            message: "x".to_string(),
            code: Some(code.to_string()),
            source: None,
        };
        assert!(PluginInvokerImpl::is_recoverable_sidecar_death(&mk(
            "PLUGIN_CRASHED"
        )));
        assert!(!PluginInvokerImpl::is_recoverable_sidecar_death(&mk(
            "MCP_CALL_FAILED"
        )));
        assert!(!PluginInvokerImpl::is_recoverable_sidecar_death(&mk(
            "MCP_TOOL_CALL_FAILED"
        )));
        assert!(!PluginInvokerImpl::is_recoverable_sidecar_death(&mk(
            "MCP_CONNECT_FAILED"
        )));
        assert!(!PluginInvokerImpl::is_recoverable_sidecar_death(
            &PluginError {
                message: "x".to_string(),
                code: None,
                source: None,
            }
        ));
    }

    #[test]
    fn test_is_dead_sidecar_http_client_never_dead() {
        // HTTP transport 无子进程（pid=None）→ 永不判死（is_alive 恒 false 的坑）。
        // 用未连接的 HTTP 客户端模拟（child=None，与 HTTP 连接后同构——
        // connect 的 HTTP 分支不设置 child）。
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let client = McpClient::new_http(
            "http://127.0.0.1:1/mcp",
            std::collections::HashMap::new(),
            None,
        );
        let dead = rt.block_on(async { PluginInvokerImpl::is_dead_sidecar(&client).await });
        assert!(!dead, "HTTP transport（无子进程）不得判为死亡");
    }

    // ── B2：native 工具调用返回归一单元测试 ──

    #[test]
    fn test_normalize_native_tool_output_envelope_shapes() {
        // 新工具插件的约定返回：{success, data} / {success:false, error} 信封直用。
        let ok = normalize_native_tool_output(&json!({
            "success": true, "data": {"output": "hi"}, "duration_ms": 7
        }));
        assert!(ok.success);
        assert_eq!(ok.data["output"], "hi");
        assert_eq!(ok.duration_ms, Some(7), "信封带的 duration_ms 应保留");

        // 失败信封可缺 data（serde 直解析会报 missing field，归一层手构造）
        let fail = normalize_native_tool_output(&json!({
            "success": false, "error": "boom"
        }));
        assert!(!fail.success);
        assert_eq!(fail.error.as_deref(), Some("boom"));

        // 失败信封缺 error 字段 → 通用文案，不 panic
        let fail_no_msg = normalize_native_tool_output(&json!({"success": false}));
        assert!(!fail_no_msg.success);
        assert!(fail_no_msg.error.is_some());
    }

    #[test]
    fn test_normalize_native_tool_output_legacy_pipeline_shape_wraps_success() {
        // 旧 pipeline 插件（忽略 tool_call）返回 state_updates → 纯业务数据包
        // success 信封（零破坏）。注意 state_updates 可能天然含 error 键——
        // 无 success 字段一律按业务数据处理，不误判 failure。
        let legacy = normalize_native_tool_output(&json!({
            "processed_by": "test_plugin",
            "error": null
        }));
        assert!(
            legacy.success,
            "无 success 字段 = 旧插件业务数据，包 success"
        );
        assert_eq!(legacy.data["processed_by"], "test_plugin");
        assert!(
            legacy.data.get("error").is_some(),
            "业务数据原样保留在 data"
        );
    }

    #[test]
    fn test_normalize_native_tool_output_success_without_data() {
        // 带 success=true 但无 data → data=Null（不报 missing field）。
        let r = normalize_native_tool_output(&json!({"success": true}));
        assert!(r.success);
        assert_eq!(r.data, serde_json::Value::Null);
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
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Pipeline,
            pipeline_role: None,
            language: "python".to_string(),
            host_type: HostType::Sidecar,
            entry: "python server.py".to_string(),
            capabilities: Default::default(),
            requires_services: vec![],
            permissions: Default::default(),
            error_policy: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            granted_capabilities: vec![],
            requires_content: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            provides: None,
            invoke_entry: invoke_entry.map(str::to_string),
            persistent_fields: vec![],
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
        // 注：旧 trait 方法 get_messages/get_recent_messages/next_sequence 已随
        // StorageBackend 演进移除，mock 同步删除（修复 HEAD 上 lib test 编译失败）。
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
        async fn create_run(
            &self,
            _run_id: &str,
            _config_hash: &str,
            _tenant_id: &str,
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
        async fn link_pipeline_session(
            &self,
            _pipeline_id: &str,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn list_pipeline_ids_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<String>, agentos_core::types::StorageError> {
            Ok(vec![])
        }
        async fn get_step_traces_by_thread(
            &self,
            _thread_id: &str,
            _tenant_id: &str,
        ) -> Result<Vec<agentos_core::types::TraceEntry>, agentos_core::types::StorageError>
        {
            Ok(vec![])
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
        ) -> Result<
            Option<agentos_core::types::PipelineRunSummary>,
            agentos_core::types::StorageError,
        > {
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

        // ── users（0.5.0 最小持久化）：MockStorage 不实现，返回空
        async fn create_user(
            &self,
            _user: &agentos_core::types::UserRecord,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn get_user_by_id(
            &self,
            _user_id: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            Ok(None)
        }
        async fn get_user_by_username(
            &self,
            _username: &str,
        ) -> Result<Option<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            Ok(None)
        }
        async fn list_users(
            &self,
        ) -> Result<Vec<agentos_core::types::UserRecord>, agentos_core::types::StorageError>
        {
            Ok(Vec::new())
        }
        async fn update_last_login(
            &self,
            _user_id: &str,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn delete_user(
            &self,
            _user_id: &str,
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
        assert_ne!(fp1, fp2, "文件修改后指纹必须变化，否则热加载不会触发");
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
        assert!(invoker.last_used.read().is_empty(), "初始 last_used 应为空");
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
        assert!(
            unloaded,
            "未加载插件的 unload_if_idle 应返回 true（软卸载幂等成功）"
        );
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

    #[test]
    fn test_resolve_pythonpath_src_includes_sdk_dir() {
        // 根因回归：resolve_pythonpath_src 注入的 PYTHONPATH 必须包含
        // project_root/plugins/sdk/src（agentos_plugin_sdk 源码目录）。
        // 缺失时 sidecar 启动即 `ModuleNotFoundError: agentos_plugin_sdk` 崩溃，
        // 内核 initialize 永远等不到响应 → 工具调用"调用前卡死"（120s 超时）。
        let loader = Arc::new(MockLoader::new());
        let invoker = PluginInvokerImpl::new(loader);
        let root = repo_root();
        invoker.set_pythonpath_src(root.clone());

        let py = invoker
            .resolve_pythonpath_src()
            .expect("resolve_pythonpath_src 应返回 Some");
        let sdk_dir = root.join("plugins/sdk/src").to_string_lossy().into_owned();
        assert!(
            py.contains(&sdk_dir),
            "PYTHONPATH 必须包含 plugins/sdk/src（agentos_plugin_sdk 源码目录），实际: {}",
            py
        );
    }

    // ── 补充分支覆盖：extract_mcp_content 错误/多元素/非字符串 ──

    #[test]
    fn test_extract_mcp_content_is_error_without_text_uses_default() {
        // isError=true 但 content[0] 无 text 字段 → 用默认错误文案。
        let mcp_result = json!({
            "content": [{"type": "text"}],
            "isError": true
        });
        let extracted = extract_mcp_content(&mcp_result);
        assert_eq!(extracted["error"], "MCP tool returned isError=true");
    }

    #[test]
    fn test_extract_mcp_content_is_error_without_content_uses_default() {
        // isError=true 且无 content 字段 → 用默认错误文案。
        let mcp_result = json!({"isError": true});
        let extracted = extract_mcp_content(&mcp_result);
        assert_eq!(extracted["error"], "MCP tool returned isError=true");
    }

    #[test]
    fn test_extract_mcp_content_multiple_items_takes_first() {
        // content 多元素时取第一项（Python SDK 只产出单元素数组）。
        let mcp_result = json!({
            "content": [
                {"type": "text", "text": r#"{"first": true}"#},
                {"type": "text", "text": r#"{"second": true}"#}
            ],
            "isError": false
        });
        let extracted = extract_mcp_content(&mcp_result);
        assert_eq!(extracted["first"], true);
        assert!(extracted.get("second").is_none());
    }

    #[test]
    fn test_extract_mcp_content_non_string_text_falls_back() {
        // text 不是字符串（如数字）→ 提取失败 → fallback 返回原对象。
        let mcp_result = json!({
            "content": [{"type": "text", "text": 12345}],
            "isError": false
        });
        let extracted = extract_mcp_content(&mcp_result);
        assert_eq!(extracted["content"][0]["text"], 12345);
    }

    // ── sidecar spawn 失败降级（无真实插件进程）──

    /// 构造带 permissions 声明的 sidecar tool manifest（entry 指向不存在的命令）。
    fn make_bad_entry_tool_manifest(id: &str) -> PluginManifest {
        let mut m = make_sidecar_manifest(id, "definitely_missing_command_98765 --flag");
        m.permissions = agentos_core::traits::ManifestPermissions {
            network: agentos_core::traits::NetworkPermission {
                allowed_hosts: vec!["example.com".to_string()],
            },
            filesystem: agentos_core::traits::FilesystemPermission {
                read_paths: vec!["/tmp".to_string()],
                write_paths: vec![],
            },
            env_vars: vec!["HOME".to_string()],
            system_calls: vec!["exec".to_string()],
        };
        m
    }

    #[tokio::test]
    async fn test_invoke_tool_sidecar_spawn_failure_returns_mcp_connect_failed() {
        // 入口命令不存在 → spawn 失败 → 降级为 MCP_CONNECT_FAILED 错误（不 panic、不卡死）。
        let loader = Arc::new(MockLoader::new());
        loader.add_manifest(make_bad_entry_tool_manifest("tool_spawn_fail"));

        let invoker = PluginInvokerImpl::new(loader);
        let err = invoker
            .invoke_tool("tool_spawn_fail", "some_tool", &json!({"x": 1}))
            .await
            .unwrap_err();
        assert_eq!(err.code.as_deref(), Some("MCP_CONNECT_FAILED"));
    }

    #[tokio::test]
    async fn test_invoke_pipeline_sidecar_spawn_failure_returns_mcp_connect_failed() {
        // pipeline 类型 sidecar：入口命令不存在 → MCP_CONNECT_FAILED。
        let loader = Arc::new(MockLoader::new());
        let mut m = make_pipeline_sidecar_manifest("pipe_spawn_fail", Some("ctx_build.execute"));
        m.entry = "definitely_missing_command_98765 --flag".to_string();
        loader.add_manifest(m);

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
        let err = invoker
            .invoke_pipeline_plugin("pipe_spawn_fail", &ctx)
            .await
            .unwrap_err();
        assert_eq!(err.code.as_deref(), Some("MCP_CONNECT_FAILED"));
    }

    // ── manifest.mcp 配置错误早暴露（不 spawn）──

    #[tokio::test]
    async fn test_invoke_streamable_http_missing_endpoint_errors() {
        // transport=StreamableHttp 但无 endpoint → MCP_CONFIG_INVALID。
        let loader = Arc::new(MockLoader::new());
        let mut m = make_sidecar_manifest("http_no_ep", "unused entry");
        m.mcp = Some(agentos_core::traits::McpConfig {
            transport: agentos_core::traits::McpTransport::StreamableHttp,
            endpoint: None,
            idle_timeout_secs: 300,
            protocol_version: "2025-06-18".to_string(),
            request_timeout_secs: None,
        });
        loader.add_manifest(m);

        let invoker = PluginInvokerImpl::new(loader);
        let err = invoker
            .invoke_tool("http_no_ep", "t", &json!({}))
            .await
            .unwrap_err();
        assert_eq!(err.code.as_deref(), Some("MCP_CONFIG_INVALID"));
    }

    #[tokio::test]
    async fn test_invoke_streamable_http_endpoint_missing_url_errors() {
        // transport=StreamableHttp 且有 endpoint 但缺 url → MCP_CONFIG_INVALID。
        let loader = Arc::new(MockLoader::new());
        let mut m = make_sidecar_manifest("http_no_url", "unused entry");
        m.mcp = Some(agentos_core::traits::McpConfig {
            transport: agentos_core::traits::McpTransport::StreamableHttp,
            endpoint: Some(agentos_core::traits::McpEndpoint {
                url: None,
                ..Default::default()
            }),
            idle_timeout_secs: 300,
            protocol_version: "2025-06-18".to_string(),
            request_timeout_secs: None,
        });
        loader.add_manifest(m);

        let invoker = PluginInvokerImpl::new(loader);
        let err = invoker
            .invoke_tool("http_no_url", "t", &json!({}))
            .await
            .unwrap_err();
        assert_eq!(err.code.as_deref(), Some("MCP_CONFIG_INVALID"));
    }

    #[tokio::test]
    async fn test_invoke_stdio_external_bad_env_placeholder_errors() {
        // Stdio 外部命令的 env 含无法解析的 ${VAR} 占位 → MCP_CONFIG_INVALID
        // （在 spawn 前暴露，避免启动后 import error 卡死）。
        let loader = Arc::new(MockLoader::new());
        let mut m = make_sidecar_manifest("stdio_bad_env", "unused entry");
        let mut env = std::collections::HashMap::new();
        env.insert(
            "PLUGIN_HOME".to_string(),
            "${DEFINITELY_UNSET_VAR_XYZ}".to_string(),
        );
        m.mcp = Some(agentos_core::traits::McpConfig {
            transport: agentos_core::traits::McpTransport::Stdio,
            endpoint: Some(agentos_core::traits::McpEndpoint {
                command: Some("npx".to_string()),
                args: vec!["-y".to_string()],
                env,
                ..Default::default()
            }),
            idle_timeout_secs: 300,
            protocol_version: "2025-06-18".to_string(),
            request_timeout_secs: None,
        });
        loader.add_manifest(m);

        let invoker = PluginInvokerImpl::new(loader);
        let err = invoker
            .invoke_tool("stdio_bad_env", "t", &json!({}))
            .await
            .unwrap_err();
        assert_eq!(err.code.as_deref(), Some("MCP_CONFIG_INVALID"));
        assert!(
            err.message.contains("env 解析失败"),
            "错误应点名 env 解析失败: {}",
            err.message
        );
    }

    // ── native tool 调用错误路径（B2 后语义）──

    #[tokio::test]
    async fn test_invoke_native_tool_without_loader_errors() {
        // B2：invoke_native_tool 已接通 execute 的 tool_call 约定字段——
        // 未注入 loader 时返回 NATIVE_LOADER_NOT_CONFIGURED（与 pipeline 路径一致），
        // 不再是旧的 NATIVE_TOOL_UNSUPPORTED 硬错误。
        let loader = Arc::new(MockLoader::new());
        loader.add_manifest(make_inprocess_manifest("native_tool_plug"));

        let invoker = PluginInvokerImpl::new(loader);
        let err = invoker
            .invoke_tool("native_tool_plug", "some_tool", &json!({}))
            .await
            .unwrap_err();
        assert_eq!(err.code.as_deref(), Some("NATIVE_LOADER_NOT_CONFIGURED"));
    }

    #[tokio::test]
    async fn test_invoke_native_tool_missing_artifact_errors() {
        // 注入了 native loader 但 manifest 缺 native.artifact → MISSING_NATIVE_ARTIFACT
        // （B2 后工具路径与 pipeline 路径同门：resolve artifact 是第一道校验）。
        let loader = Arc::new(MockLoader::new());
        loader.add_manifest(make_inprocess_manifest("native_tool_no_artifact"));

        let invoker =
            PluginInvokerImpl::new(loader).set_native_loader(Arc::new(NativePluginLoader::new()));
        let err = invoker
            .invoke_tool("native_tool_no_artifact", "some_tool", &json!({}))
            .await
            .unwrap_err();
        assert_eq!(err.code.as_deref(), Some("MISSING_NATIVE_ARTIFACT"));
    }

    #[tokio::test]
    async fn test_invoke_native_tool_bad_artifact_errors() {
        // artifact 指向不存在的文件 → NATIVE_LOAD_FAILED（真实 NativePluginLoader 路径）。
        let loader = Arc::new(MockLoader::new());
        let mut m = make_inprocess_manifest("native_tool_bad_artifact");
        m.native = Some(agentos_core::traits::NativeArtifact {
            artifact: "definitely_missing_plugin.dll".to_string(),
        });
        loader.add_manifest(m);
        loader.plugin_dirs.write().insert(
            "native_tool_bad_artifact".to_string(),
            std::env::temp_dir().to_string_lossy().to_string(),
        );

        let invoker =
            PluginInvokerImpl::new(loader).set_native_loader(Arc::new(NativePluginLoader::new()));
        let err = invoker
            .invoke_tool("native_tool_bad_artifact", "some_tool", &json!({}))
            .await
            .unwrap_err();
        assert_eq!(err.code.as_deref(), Some("NATIVE_LOAD_FAILED"));
    }

    // ── unload_if_idle 各 host_type 分支 ──

    #[tokio::test]
    async fn test_unload_if_idle_inprocess_false() {
        // InProcess（rust cdylib）：dlclose 限制，永不软卸载 → false
        let loader = Arc::new(MockLoader::new());
        loader.add_manifest(make_inprocess_manifest("native_idle"));
        let invoker = PluginInvokerImpl::new(loader);
        assert!(!invoker.unload_if_idle("native_idle").await);
    }

    // ── idle_timeout_secs_sync 优先级链 ──

    #[tokio::test]
    async fn test_idle_timeout_secs_sync_manifest_never_unload() {
        // manifest 声明 Some(0) = 永不空闲卸载。
        let loader = Arc::new(MockLoader::new());
        let mut m = make_sidecar_manifest("never_idle", "python server.py");
        m.lifecycle = Some(agentos_core::traits::PluginLifecycle {
            idle_timeout_secs: Some(0),
        });
        loader.add_manifest(m);
        let invoker = PluginInvokerImpl::new(loader);
        assert_eq!(invoker.idle_timeout_secs_sync("never_idle"), 0);
    }

    #[tokio::test]
    async fn test_idle_timeout_secs_sync_manifest_value() {
        let loader = Arc::new(MockLoader::new());
        let mut m = make_sidecar_manifest("custom_idle", "python server.py");
        m.lifecycle = Some(agentos_core::traits::PluginLifecycle {
            idle_timeout_secs: Some(42),
        });
        loader.add_manifest(m);
        let invoker = PluginInvokerImpl::new(loader);
        assert_eq!(invoker.idle_timeout_secs_sync("custom_idle"), 42);
    }

    #[tokio::test]
    async fn test_idle_timeout_secs_sync_default() {
        // 未声明 lifecycle → 内核默认 300s。
        let loader = Arc::new(MockLoader::new());
        loader.add_manifest(make_sidecar_manifest("default_idle", "python server.py"));
        let invoker = PluginInvokerImpl::new(loader);
        assert_eq!(
            invoker.idle_timeout_secs_sync("default_idle"),
            agentos_core::traits::default_idle_timeout()
        );
    }

    // ── PluginScopedRouter：_plugin_id 注入（信任锚点）──

    /// 记录收到的 capability 调用（供断言 _plugin_id 注入）。
    struct RecordRouter {
        calls: std::sync::Arc<std::sync::Mutex<Vec<(String, String, Value)>>>,
    }
    #[async_trait]
    impl CapabilityRouter for RecordRouter {
        async fn handle(
            &self,
            capability: &str,
            method: &str,
            params: Value,
        ) -> Result<Value, agentos_mcp::McpError> {
            self.calls
                .lock()
                .unwrap()
                .push((capability.to_string(), method.to_string(), params));
            Ok(json!({"ok": true}))
        }
        fn known_namespaces(&self) -> Vec<String> {
            vec!["custom-ns".to_string()]
        }
    }

    #[tokio::test]
    async fn test_plugin_scoped_router_injects_plugin_id_into_object_params() {
        let calls = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let scoped: Arc<dyn CapabilityRouter> = Arc::new(PluginScopedRouter {
            plugin_id: "plugin_a".to_string(),
            inner: Arc::new(RecordRouter {
                calls: calls.clone(),
            }),
        });
        let res = scoped
            .handle("metrics", "record", json!({"name": "calls", "value": 1}))
            .await
            .unwrap();
        assert_eq!(res["ok"], true);
        let got = calls.lock().unwrap().clone();
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].2["_plugin_id"], "plugin_a");
        // 原始参数保留
        assert_eq!(got[0].2["name"], "calls");
    }

    #[tokio::test]
    async fn test_plugin_scoped_router_wraps_non_object_params() {
        // 非对象 params（如字符串）→ 包成 {"_plugin_id", "value": params}。
        let calls = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let scoped: Arc<dyn CapabilityRouter> = Arc::new(PluginScopedRouter {
            plugin_id: "plugin_b".to_string(),
            inner: Arc::new(RecordRouter {
                calls: calls.clone(),
            }),
        });
        let _ = scoped.handle("ping", "pong", json!("hello")).await.unwrap();
        let got = calls.lock().unwrap().clone();
        assert_eq!(got[0].2["_plugin_id"], "plugin_b");
        assert_eq!(got[0].2["value"], "hello");
    }

    #[tokio::test]
    async fn test_plugin_scoped_router_known_namespaces_delegates() {
        // known_namespaces 委托给 inner（sidecar initialize 才能拿到插件自注册 namespace）。
        let calls = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let scoped: Arc<dyn CapabilityRouter> = Arc::new(PluginScopedRouter {
            plugin_id: "plugin_c".to_string(),
            inner: Arc::new(RecordRouter { calls }),
        });
        assert_eq!(scoped.known_namespaces(), vec!["custom-ns".to_string()]);
    }
}

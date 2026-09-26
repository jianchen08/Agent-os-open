//! PluginInvoker 实现
//!
//! 按 host_type 透明分发调用：
//! - InProcess: 经 `NativePluginLoader` 加载 cdylib，走 C-ABI 调用（JSON 经内存传递）
//! - Sidecar: 通过 MCP 客户端走 JSON-RPC 协议调用（进程隔离）
//!
//! 两种 host_type 共用 PluginInput / PluginResult JSON 契约，invoker 透明分发。
//! （原 Wasm 轨已按两轨终局决策关闭摘除，见 core::traits::HostType 文档。）
//!

use std::collections::HashMap;
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
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

/// 轻量合宿：manifest.host_group 声明即准入（合宿进程模型 §4.1 的多组扩展）。
/// 组名任意（`light` = 易变/通用组，`light_stable` 等独立组名承载稳定观察面），
/// 装箱槽位按组名分域（`group:{组名}:{n}`），驱逐连坐半径 = 本组宿主。
/// 白名单制担保语义不变：声明即插件作者担保「无同步阻塞调用、无 C 扩展、
/// 无重依赖」，内核不做推断。
const DEFAULT_HOST_GROUP: &str = "light";

/// 合宿宿主目录名（plugins/shared/_host/，host.py 由宿主侧任务承载）。
const GROUP_HOST_DIR: &str = "_host";

/// 内核 → 宿主的成员粒度热重载请求方法（SDK CohostServer/host.py 注册同名
/// 处理器；带应答，失败以协议错误应答，内核回退 force_unload）。
const RELOAD_MEMBER_METHOD: &str = "agentos/reload_member";

/// 内核 → 宿主的成员粒度卸载请求方法（带应答；ADR
/// 2026-09-24-member-granularity-unload-mode-panel-warmup）：宿主定向 on_unload
/// → 成员表摘除 → 三张聚合表原位重聚合；最后成员以协议错误拒绝，内核对
/// 最后成员走整组回收。失败（旧宿主不认识该方法/协议错误/超时）一律回退
/// 整组驱逐——语义终点与现状一致。
const UNLOAD_MEMBER_METHOD: &str = "agentos/unload_member";

/// reload_member 请求超时上界（控制面操作必须有界）。请求窗口自宿主侧
/// on_load 定向派发落地起覆盖「模块重 exec + on_load 重放」——慢而未死的
/// 成员初始化（慢盘/慢解释器/预热 IO）必须等足预算完成，超时即回退整组
/// 驱逐（fail-closed 不变），而预算过紧会把慢初始化放大成全组陪葬的驱逐
/// 风暴（BUG-19）：30s 明显大于合理慢初始化上界，仍远小于任何挂死宿主的
/// 可容忍驻留。
const RELOAD_MEMBER_TIMEOUT: Duration = Duration::from_secs(30);

/// 合法组名字符集（进宿主键/日志/spawn 参数）：ASCII 字母数字下划线连字符，
/// ≤32 字符。非法值一律独占（保守，与缺省同）。
fn is_valid_group_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 32
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

/// 单个合宿宿主的同时挂载成员数上限（合宿进程模型 §4.5）。
/// 默认 6，可用环境变量 `AGENTOS_LIGHT_HOST_MAX_MEMBERS` 覆盖（<=0 视为无效回退默认）。
const LIGHT_HOST_DEFAULT_MAX_MEMBERS: usize = 6;

/// 读取合宿宿主挂载成员上限（环境变量优先，全组名共用同一上限）。
fn light_host_max_members() -> usize {
    std::env::var("AGENTOS_LIGHT_HOST_MAX_MEMBERS")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .filter(|n| *n > 0)
        .unwrap_or(LIGHT_HOST_DEFAULT_MAX_MEMBERS)
}

/// 组宿主键：`group:{group}:{slot}`（slot 从 1 起，按组内装箱顺序分配；
/// 回收后槽位可复用）。
fn group_host_key(group: &str, slot: u64) -> String {
    format!("group:{group}:{slot}")
}

/// 从宿主键解析组名与槽位号；非组宿主键返回 None。
/// 组名合法字符集不含冒号，`rsplit_once` 足以切分。
fn parse_group_slot(host_key: &str) -> Option<(&str, u64)> {
    let rest = host_key.strip_prefix("group:")?;
    let (group, slot) = rest.rsplit_once(':')?;
    Some((group, slot.parse().ok()?))
}

/// 独占宿主键：`plugin:{plugin_id}`（每插件独占宿主，现状语义统一走宿主键路径）。
fn solo_host_key(plugin_id: &str) -> String {
    format!("plugin:{plugin_id}")
}

/// 合宿成员装箱状态（合宿进程模型 §4.5，多组扩展）。
///
/// - `assignments`：分配表 {plugin_id → host_key}。粘性：成员一旦分配，宿主
///   存活期间归属不变（respawn 按表重建成员集）；宿主被 idle GC 回收时其全部成员
///   条目随之清除（槽位释放复用）；manifest 声明组名变更后旧条目跨组过期，
///   装箱判定按当前声明组重装箱（成员搬组，旧宿主成员集收缩）。
/// - `next_slot`：各组名已开过的最大槽位号（只增，按组记账）。装箱时从低槽位
///   找「当前挂载数 < 上限」的宿主塞入——已回收宿主（成员条目已清、计数归 0）
///   自然优先复用，而不是无限开新组；全满才开新槽位。
#[derive(Default)]
struct LightPacking {
    assignments: HashMap<String, String>,
    next_slot: HashMap<String, u64>,
}

/// 合宿成员判定：host_group 声明合法组名 且 sidecar 且非外部 MCP（多组扩展）。
///
/// 外部 MCP（StreamableHttp 远端 / stdio 第三方命令）的进程归外部所有，
/// 内核只是客户端（方案 §〇 三类宿主形态表），不进合宿组；host_type=InProcess
/// 天生单进程无独立内存底座，同样不适用。缺省或非法组名一律独占（保守）。
pub fn is_cohost_member(manifest: &PluginManifest) -> bool {
    let Some(group) = manifest.host_group.as_deref() else {
        return false;
    };
    if !is_valid_group_name(group) || manifest.host_type != HostType::Sidecar {
        return false;
    }
    let is_external = match manifest.mcp.as_ref() {
        Some(cfg) => match cfg.transport {
            agentos_core::traits::McpTransport::StreamableHttp => {
                cfg.endpoint.as_ref().and_then(|e| e.url.as_ref()).is_some()
            }
            agentos_core::traits::McpTransport::Stdio => cfg
                .endpoint
                .as_ref()
                .and_then(|e| e.command.as_ref())
                .is_some(),
        },
        None => false,
    };
    !is_external
}

/// 反向调用连接身份（G6 信任锚点 + metrics 命名空间）：合宿 = 宿主键（进程即
/// 授权主体，内核按 group_granted_capabilities 归并成员白名单判）；独占 =
/// manifest.id。身份取进程而非成员，保证不随组重生换人。
fn scoped_router_identity(manifest: &PluginManifest, host_key: &str) -> String {
    if is_cohost_member(manifest) {
        host_key.to_string()
    } else {
        manifest.id.clone()
    }
}

/// 合宿成员的 MCP 工具名命名空间（§4.2 第 3 条）：宿主把成员插件的每个工具
/// 注册为 `{plugin_id}.{tool_name}`，调用侧拼前缀分发；独占宿主无前缀（现状不变）。
fn namespaced_tool_name(manifest: &PluginManifest, tool_name: &str) -> String {
    if is_cohost_member(manifest) {
        format!("{}.{}", manifest.id, tool_name)
    } else {
        tool_name.to_string()
    }
}

/// 从成员插件目录向上找合宿宿主目录（含 `_host` 子目录的最近祖先）。
///
/// 插件目录层级不固定（plugins/shared/<type>/<phase>/<name>），不能按固定
/// 深度回溯；逐级向上探测 `_host` 子目录，到文件系统根仍无则 None。
/// pub 供 venv 自愈（venv_provision）复用同一探测序，避免两处定位漂移。
pub fn find_group_host_dir(member_dir: &Path) -> Option<std::path::PathBuf> {
    let mut cur = Some(member_dir);
    while let Some(dir) = cur {
        let candidate = dir.join(GROUP_HOST_DIR);
        if candidate.is_dir() {
            return Some(candidate);
        }
        cur = dir.parent();
    }
    None
}

/// 内置根（插件装载根 `AGENTOS_PLUGINS_DIR`，获取方式与 collect_plugin_roots
/// 同源）下的合宿宿主目录：`<内置根>/_host/`（host.py 与共享 venv 的仓库原件）。
///
/// 仅作 find_group_host_dir 探测全空时的回退定位（见 resolve_group_host_command），
/// 不参与常规探测序。pub 供 venv 自愈（venv_provision）复用同一定位序。
pub fn builtin_group_host_dir() -> Option<std::path::PathBuf> {
    let root = std::env::var("AGENTOS_PLUGINS_DIR")
        .ok()
        .filter(|d| !d.trim().is_empty())?;
    let candidate = Path::new(&root).join(GROUP_HOST_DIR);
    candidate.is_dir().then_some(candidate)
}

/// 计算插件指纹：对该插件目录下的**源码文件** + plugin.json 声明的 config_files 路径
/// 取 mtime（秒级精度），拼接为字符串后做简单 hash。
///
/// 设计权衡：
/// 插件源码/配置指纹（mtime 基）。invoker 的 respawn 判定与 watcher 的 G2
/// 复验触发共用同一把指纹——两边对"代码变更"的判定必须同源，否则会出现
/// respawn 了但复验不触发（或反之）的判据分叉。
///
/// - 用 mtime 而非内容 hash：stat 是微秒级，内容 hash 要读全部文件（毫秒级），
///   热路径上 stat 性能可接受，mtime 精度足够捕获代码/配置修改。
/// - **只扫源码文件**（.py/.rs/.js/.ts/.wasm/.json/.yaml/.yml/.toml），跳过运行时
///   产生的杂物（.log/.pyc/__pycache__/临时文件）。否则 sidecar 运行时若在插件目录
///   写入临时文件（如诊断日志），会误触发 respawn。
/// - config_files 指向的配置文件可能在插件目录外（如 config/models/llm.yaml），
///   单独 stat 纳入指纹，配置变更也能触发 respawn。
pub fn compute_plugin_fingerprint(plugin_dir: &Path, manifest: &PluginManifest) -> u64 {
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
    /// （含 handler_registry 注册的交互等 namespace）透传到 initialize 声明。
    /// 不覆盖的话走 trait 默认实现，只返回静态 STANDARD_CAPABILITIES，
    /// sidecar 拿不到插件自注册 namespace 的 CapabilityHandle。
    fn known_namespaces(&self) -> Vec<String> {
        self.inner.known_namespaces()
    }
}

/// venv 解释器路径（纯路径选择逻辑，`windows` 参数使两平台分支皆可测）。
///
/// uv venv 标准布局：Windows `.venv/Scripts/python.exe`、Unix `.venv/bin/python`。
fn venv_interpreter_layout(plugin_dir: &Path, windows: bool) -> std::path::PathBuf {
    if windows {
        plugin_dir.join(".venv").join("Scripts").join("python.exe")
    } else {
        plugin_dir.join(".venv").join("bin").join("python")
    }
}

/// 探测插件目录的 venv 解释器：按本平台布局优先，另一平台布局作回退
/// （探测无害——命中即返回绝对路径，缺失返回 None，不执行解释器）。
/// pub：boot 期 venv 自愈（api::venv_provision）与 spawn 期解析共用同一探测。
pub fn find_venv_interpreter(plugin_dir: &Path) -> Option<std::path::PathBuf> {
    [
        venv_interpreter_layout(plugin_dir, cfg!(windows)),
        venv_interpreter_layout(plugin_dir, !cfg!(windows)),
    ]
    .into_iter()
    .find(|p| p.is_file())
}

/// 判定 entry 首词是否 PATH 裸 python 命令（`python` / `python3`，含 Windows
/// 可执行扩展 `.exe`/`.bat`/`.cmd`）。带路径分隔符的绝对/相对路径不判——venv
/// 只替代"靠 PATH 解析的裸解释器"，显式绝对路径 entry 是刻意选择，不动。
pub fn is_plain_python_command(command: &str) -> bool {
    if command.contains('\\') || command.contains('/') {
        return false;
    }
    let lower = command.to_ascii_lowercase();
    let name = lower
        .strip_suffix(".exe")
        .or_else(|| lower.strip_suffix(".bat"))
        .or_else(|| lower.strip_suffix(".cmd"))
        .unwrap_or(&lower);
    matches!(name, "python" | "python3")
}

/// 标准 MCP 出参归一：参数白名单按工具声明的 input_schema 过滤。
///
/// 内核/管道会向工具参数注入会话上下文（_call_context/session_id/workspace/
/// pipeline_id/thread_id/parent_agent_level/…，清单随调用方增长），外部标准
/// MCP server（FastMCP 等）对未声明参数严格校验直接拒——所以以 tools/list
/// 观测回填的 input_schema.properties 为**白名单**（声明即透传，标准协议
/// 字段进内部不变形）；schema 未知的工具退化为黑名单（剥下划线前缀 + 已知
/// 注入键）。外部工具声明下划线参数属非标准用法，约定不支持。
fn sanitize_external_mcp_arguments(
    manifest: &PluginManifest,
    tool_name: &str,
    inputs: &serde_json::Value,
) -> serde_json::Value {
    const INJECTED: [&str; 14] = [
        "session_id",
        "workspace",
        "working_dir",
        "tenant_id",
        "plugin_id",
        "_log_ctx",
        "pipeline_id",
        "thread_id",
        "parent_agent_level",
        "user_id",
        "client_message_id",
        "message_id",
        "intent",
        "timestamp",
    ];
    let declared_props: Option<&serde_json::Map<String, serde_json::Value>> = manifest
        .capabilities
        .tools
        .iter()
        .find(|t| t.name == tool_name)
        .and_then(|t| t.input_schema.as_ref())
        .and_then(|s| s.get("properties"))
        .and_then(|p| p.as_object());

    match inputs {
        serde_json::Value::Object(map) => serde_json::Value::Object(
            map.iter()
                .filter(|(k, v)| {
                    if k.starts_with('_') || INJECTED.contains(&k.as_str()) {
                        return false;
                    }
                    match declared_props {
                        Some(props) => props.contains_key(*k) && !v.is_null(),
                        None => true,
                    }
                })
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect(),
        ),
        other => other.clone(),
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

    // ── 标准 MCP content 协议归一（external MCP 一行接入配套）────────
    // 内部 sidecar 约定：content[0].text 是业务 JSON 字符串（优先照旧直通）。
    // 其余一律按标准 MCP 语义转换：text 项拼接、image 项转内部 images 通道
    // （tool_core inject_multimodal 来源 2，截图回传视觉模型）、resource 取
    // text 并入——标准协议字段进来，内部多模态通道出去，转换只此一处。
    let items = mcp_result.get("content").and_then(|c| c.as_array());
    let Some(items) = items else {
        return mcp_result.clone();
    };

    let first_text_json = items
        .first()
        .and_then(|item| item.get("text"))
        .and_then(|t| t.as_str())
        .and_then(|s| serde_json::from_str::<serde_json::Value>(s).ok());
    let has_image = items
        .iter()
        .any(|item| item.get("type").and_then(|v| v.as_str()) == Some("image"));

    if let (Some(val), false) = (first_text_json, has_image) {
        return val;
    }

    let mut texts: Vec<String> = Vec::new();
    let mut images: Vec<serde_json::Value> = Vec::new();
    for item in items {
        match item.get("type").and_then(|v| v.as_str()) {
            Some("text") => {
                let t = item.get("text").and_then(|t| t.as_str()).unwrap_or("");
                if !t.is_empty() {
                    texts.push(t.to_string());
                }
            }
            Some("image") => images.push(serde_json::json!({
                "base64": item.get("data").and_then(|d| d.as_str()).unwrap_or(""),
                "mime_type": item
                    .get("mimeType")
                    .and_then(|m| m.as_str())
                    .unwrap_or("image/png"),
            })),
            Some("resource") => {
                let t = item
                    .get("resource")
                    .and_then(|r| r.get("text"))
                    .and_then(|t| t.as_str())
                    .unwrap_or("");
                if !t.is_empty() {
                    texts.push(t.to_string());
                }
            }
            _ => {}
        }
    }
    let mut output = serde_json::Map::new();
    if !texts.is_empty() {
        output.insert(
            "text".into(),
            serde_json::Value::String(texts.join(
                "
",
            )),
        );
    }
    if !images.is_empty() {
        output.insert("images".into(), serde_json::Value::Array(images));
    }
    serde_json::json!({ "success": true, "output": output })
}

/// 中央返回处的 OnError 判定（pipeline 形态）：调用层 `Err`（崩溃/MCP/解析）
/// 或 Ok 携带 error 字段都算插件报错——业务失败若不计，`lifecycle.plugin_error_total`
/// 恒零。None = 无报错（不发事件）。
fn pipeline_error_params(
    result: &Result<PluginResult, PluginError>,
) -> Option<(String, Option<String>)> {
    match result {
        Err(e) => Some((e.message.clone(), e.code.clone())),
        Ok(r) => r
            .error
            .as_ref()
            .map(|e| (e.message.clone(), e.code.clone())),
    }
}

/// 同 [`pipeline_error_params`]（tool 形态）：Ok 携带 success=false 也算报错。
fn tool_error_params(
    result: &Result<ToolExecutionResult, PluginError>,
) -> Option<(String, Option<String>)> {
    match result {
        Err(e) => Some((e.message.clone(), e.code.clone())),
        Ok(r) if !r.success => Some((
            r.error
                .clone()
                .unwrap_or_else(|| "tool execution failed".to_string()),
            None,
        )),
        Ok(_) => None,
    }
}

/// MCP 工具调用返回归一（sidecar 决策树，`invoke_mcp_tool` 的纯函数核心）。
///
/// 输入是 `extract_mcp_content` 提取的工具 handler 原始返回（业务 dict）。按形状分流：
/// - ②-a 真 ToolExecutionResult 信封（同时带 success + data）→ 直接 from_value；
/// - ②-b Python ToolResult 形状（带 success 但无 data）→ 归一化：
///   - (A) 带 output 键（builtin_tools 的 ToolResult.to_dict()）→ data = output；
///   - (B) 无 output 键（server.py 解包直返、恰带 success 键的业务 dict）→ data = inner；
///   - success=false → failure(error)；
/// - ① MCP isError=true / 工具返回 `{"error":"..."}`（无 success）→ failure；
/// - ③ 纯业务数据（无 success 无 error）→ success(data=inner)。
///
/// K7：②-b 的 `success` 键存在但非 bool（字符串/整数状态码等信封漂移）→
/// `PARSE_ERROR`（与 ②-a 对信封解析失败同判）。禁止 `unwrap_or(true)` 把失败
/// 工具调用包装成成功污染下游 state——原始信封此时已丢，成功偏置无法追溯。
fn normalize_mcp_tool_result(
    inner: Value,
    tool_name: &str,
) -> Result<ToolExecutionResult, PluginError> {
    if inner.get("success").is_some() && inner.get("data").is_some() {
        // ②-a 真 ToolExecutionResult 信封（from_value 对非 bool success 同样报错）
        serde_json::from_value(inner).map_err(|e| PluginError {
            message: format!("failed to parse MCP response as ToolExecutionResult: {e}"),
            code: Some("PARSE_ERROR".to_string()),
            source: Some("plugin-invoker".to_string()),
        })
    } else if inner.get("success").is_some() {
        // ②-b 带 success 但无 data。与流式 tool_result 事件使用同一个 success
        // 信号（tool_core/src/lib.rs:351）。
        let ok = inner
            .get("success")
            .and_then(|v| v.as_bool())
            .ok_or_else(|| PluginError {
                message: format!(
                    "MCP tool '{}' returned non-boolean 'success' field: {:?} (envelope drift, refusing to default to success)",
                    tool_name, inner["success"]
                ),
                code: Some("PARSE_ERROR".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;
        if ok {
            let data = if inner.get("output").is_some() {
                inner
                    .get("output")
                    .cloned()
                    .unwrap_or(serde_json::Value::Null)
            } else {
                inner.clone()
            };
            Ok(ToolExecutionResult {
                success: true,
                data,
                error: None,
                duration_ms: None,
                metadata: inner.get("metadata").cloned(),
            })
        } else {
            let err_msg = inner
                .get("error")
                .and_then(|v| v.as_str())
                .unwrap_or("tool execution failed");
            Ok(ToolExecutionResult {
                success: false,
                data: Value::Null,
                error: Some(err_msg.to_string()),
                duration_ms: None,
                metadata: inner.get("metadata").cloned(),
            })
        }
    } else if let Some(err) = inner.get("error").and_then(|v| v.as_str()) {
        // ① MCP isError=true 或工具自身返回 {"error": "..."} 且无 success 字段
        Ok(ToolExecutionResult::failure(err))
    } else {
        // ③ 纯业务数据 → 包成 success 信封
        Ok(ToolExecutionResult::success(inner))
    }
}

/// B2：native 工具调用返回归一（对齐 [`PluginInvokerImpl::invoke_tool`] sidecar
/// 决策树的归一层：纯业务数据包 success 信封）。
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
            result.metadata = inner.get("metadata").cloned();
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
    /// capability 调用结果缓冲（**exe 堆**，本结构持有）。跨分配器契约：
    /// `call_capability` 把结果写进这里再借 `&str` 给插件——exe 分配、exe 释放，
    /// 零跨界所有权转移。线程安全：execute 在 blocking 线程串行（同 host 一次
    /// 一个调用），`&self` 独占借用期间写读不重叠，UnsafeCell 足够。
    response_buf: std::cell::UnsafeCell<String>,
    /// capability 调用**错误**缓冲（**exe 堆**，本结构持有）。Err 分支同契约
    /// （2026-09-02 收口）：错误消息写这里再借 `&str`——旧 `Err(String)` 把 exe
    /// 堆 String 交 dll 侧 drop = 反方向跨堆 free UB，真机 SIGSEGV 实测。
    err_buf: std::cell::UnsafeCell<String>,
}

// SAFETY: response_buf/err_buf 的 UnsafeCell 仅在 `&self` 独占借用期间写入；借出的 &str
// 由调用方（插件，blocking 线程内同步消费）在下一次同 host 调用前读完。execute 的
// 调用形态=单 blocking 线程串行，无并发写面。
unsafe impl Send for NativeHostServices {}
unsafe impl Sync for NativeHostServices {}

impl agentos_native_sdk::HostServices for NativeHostServices {
    fn call_capability(
        &self,
        capability: &str,
        method: &str,
        params_json: &str,
    ) -> Result<&str, &str> {
        let router = Arc::clone(&self.router);
        let cap = capability.to_string();
        let mth = method.to_string();
        // G6：注入 _plugin_id 信任锚点（插件侧参数不可覆盖——已存在时以内核注入为准）。
        // 入参非合法 JSON = 插件侧调用缺陷，早失败回传根因，不得静默降级为 Null 续跑。
        let mut params: Value = match serde_json::from_str(params_json) {
            Ok(v) => v,
            Err(e) => {
                let buf = unsafe { &mut *self.err_buf.get() };
                buf.clear();
                buf.push_str(&format!(
                    "capability {capability}.{method} params JSON invalid: {e}"
                ));
                return Err(buf.as_str());
            }
        };
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
            // 跨分配器契约：结果写进自我持有的缓冲（exe 堆），返回 &str 借用——
            // dll 只读消费（立即转 Value），不存引用、不释放。旧 `String` 返回会
            // 把 exe 分配的串交 dll 侧 drop = 反方向跨堆 free UB。
            Ok(v) => {
                // SAFETY(跨分配器契约)：response_buf 由本结构（exe 侧）持有，
                // 此处写、借出 &str 给 dll 只读——分配/释放全在 exe 堆。
                // 并发安全性：execute 在单个 blocking 线程串行，同 host 的
                // call_capability 不重入（tool_core 同轮串行调用），&self 已独占。
                let buf = unsafe { &mut *self.response_buf.get() };
                buf.clear();
                buf.push_str(&serde_json::to_string(&v).unwrap_or_default());
                Ok(buf.as_str())
            }
            // Err 分支同契约（2026-09-02 收口）：错误消息写 err_buf（exe 堆）
            // 借 &str 返回——dll 只读消费（立即拷贝/格式化），不持有、不释放。
            // 旧 `Err(format!("{e}"))` 把 exe 堆 String 交 dll 侧 drop = 反方向
            // 跨堆 free UB（真机 SIGSEGV 实测，与 execute 的 Err(String) 同根）。
            Err(e) => {
                let buf = unsafe { &mut *self.err_buf.get() };
                buf.clear();
                buf.push_str(&format!("{e}"));
                Err(buf.as_str())
            }
        }
    }
}

/// 共享 MCP 客户端句柄：宿主键粒度缓存条目——外层 [`RwLock`] 护表，内层
/// tokio RwLock 护连接本身的并发读（一次 execute 持读锁全程）。
type SharedMcpClient = Arc<tokio::sync::RwLock<McpClient>>;

/// capability 调用 in-flight guard（RAII）：构造时计数已 +1，`Drop` 时 -1。
///
/// 成功/失败/超时（上层 `tokio::time::timeout` 放弃内层 future）/panic 任何退出
/// 路径都随 future drop 走 `Drop`，计数必然回落（ADR 2026-09-07 P12）。
struct InflightGuard {
    counter: Arc<AtomicUsize>,
}

impl Drop for InflightGuard {
    fn drop(&mut self) {
        self.counter.fetch_sub(1, Ordering::SeqCst);
    }
}

/// spawn 窗口记账 guard（RAII，D1 宽限面）：构造时成员 id 已写入
/// `starting_plugins`，`Drop` 时成对摘除。成功/失败/panic 任何退出路径都随
/// future drop 走 `Drop`，窗口记账必然出清——泄漏一个条目 = 该插件端点
/// 宽限常开（dispatcher 预算暂停失效），必须出窗即清。
struct StartingPluginsGuard<'a> {
    set: &'a RwLock<std::collections::HashSet<String>>,
    members: Vec<String>,
}

impl Drop for StartingPluginsGuard<'_> {
    fn drop(&mut self) {
        let mut set = self.set.write();
        for member in &self.members {
            set.remove(member);
        }
    }
}

/// 工具调用的有效总时长超时（毫秒）；`None` = 显式豁免（P12，ADR 2026-09-07）。
///
/// 优先级：工具声明 `capabilities.tools[].timeout_ms` > 插件级
/// `mcp.request_timeout_secs`（既有声明，秒→毫秒）> 内核默认
/// [`agentos_core::traits::default_capability_timeout_ms`]。插件级声明兜底
/// 保证长等待插件（human 审批 86400s 等，BUG-60 审批族统一值）既有声明继续
/// 生效，不被 300s 工具执行默认掐断；工具声明 `Some(0)` 为 0 哨兵豁免
/// （对齐 `lifecycle.idle_timeout_secs`）。
fn tool_timeout_ms(manifest: &PluginManifest, tool_name: &str) -> Option<u64> {
    let declared = manifest
        .capabilities
        .tools
        .iter()
        .find(|t| t.name == tool_name)
        .and_then(|t| t.timeout_ms);
    match declared {
        Some(0) => None,
        Some(ms) => Some(ms),
        None => Some(
            manifest
                .mcp
                .as_ref()
                .and_then(|m| m.request_timeout_secs)
                .map(|secs| secs.saturating_mul(1000))
                .unwrap_or_else(agentos_core::traits::default_capability_timeout_ms),
        ),
    }
}

/// 非流式 capability 调用的总时长超时包装（P12，ADR 2026-09-07）。
///
/// `timeout_ms` 为 `None` 直接透传（豁免）；否则到点放弃内层 future——
/// 其持有的 in-flight guard 随 drop 回落——并返回结构化 `CAPABILITY_TIMEOUT`
/// （message 含插件 id、工具名与时长毫秒数）。
async fn with_capability_timeout<T>(
    timeout_ms: Option<u64>,
    plugin_id: &str,
    tool_name: &str,
    fut: impl std::future::Future<Output = Result<T, PluginError>>,
) -> Result<T, PluginError> {
    let Some(ms) = timeout_ms else {
        return fut.await;
    };
    match tokio::time::timeout(Duration::from_millis(ms), fut).await {
        Ok(result) => result,
        Err(_) => Err(PluginError {
            message: format!(
                "capability call timed out after {ms}ms: plugin '{plugin_id}' tool '{tool_name}'"
            ),
            code: Some("CAPABILITY_TIMEOUT".to_string()),
            source: Some("plugin-invoker".to_string()),
        }),
    }
}

/// PluginInvoker 实现。
///
/// 管理插件实例和 MCP 客户端连接，按 host_type 透明分发调用。
/// 支持崩溃隔离：检测子进程崩溃后卸载能力 + 告警。
pub struct PluginInvokerImpl {
    /// 插件加载器（用于查找 manifest）
    loader: Arc<dyn PluginLoader>,
    /// 已连接的 MCP 客户端 {宿主键: McpClient}（合宿进程模型 §4.2）。
    ///
    /// 宿主键：light 合宿插件 → `group:light:{n}`（同宿主成员共享一个客户端/
    /// 进程）；其余 sidecar → `plugin:{plugin_id}`（独占宿主，现状语义）。
    mcp_clients: RwLock<HashMap<String, SharedMcpClient>>,
    /// per-host spawn 互斥锁（single-flight，防并发请求竞态 spawn 多个宿主进程）。
    ///
    /// 同一宿主键的 spawn 串行化：首个请求持锁 spawn 并写缓存，后续请求拿锁后
    /// 二次查缓存命中直接复用；跨宿主并行互不阻塞（合宿进程模型 §4.2 第 4 条：
    /// spawn 锁粒度从 per-plugin 改为 per-host-key）。
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
    /// Pull 热加载指纹缓存 {宿主键: (上次指纹, 上次检测时刻)}。
    /// 调用 sidecar 时比对：TTL 内跳过 stat（零开销），TTL 过后 stat mtime 比对，
    /// 指纹变化则 kill 旧进程走 respawn 路径加载新代码/配置。
    /// （未用到也不检测——纯按需 pull。）
    /// 合宿宿主指纹 = 当前成员指纹并集 + 成员集本身（§4.6）：任一成员代码变更或
    /// 成员集变化 → kill 整宿主 respawn。
    fingerprints: RwLock<HashMap<String, (u64, Instant)>>,
    /// 成员最后调用时刻 {plugin_id: Instant}——空闲软卸载依据（成员粒度记账，
    /// ADR 2026-09-24-member-granularity-unload-mode-panel-warmup 取代原宿主键
    /// 记账：合宿卸载粒度成员化后，每个成员按自己的调用时钟与自己的
    /// `lifecycle.idle_timeout_secs` 独立判定，不再整组连坐）。
    ///
    /// 每次 get_or_create_mcp_client 命中/创建时刷新（调用路径已知 plugin_id）；
    /// 后台 GC 据此判定成员是否空闲超时。
    member_last_used: RwLock<HashMap<String, Instant>>,
    /// 假死 respawn 熔断账本 {宿主键: Vec<respawn 时刻>}（进程治理三信号模型）。
    ///
    /// 10 分钟窗口内 ≥3 次假死 respawn 则停手只告警——防循环重启掩盖根因
    /// （同构 K8s CrashLoopBackOff 退避语义）。窗口滑出后自然解除。
    freeze_respawns: RwLock<HashMap<String, Vec<Instant>>>,
    /// 宿主进程 spawn 时刻（监控 M3 进程态轮询的 uptime 源；同宿主键 respawn 覆盖写，
    /// 驱逐不清理——宿主键集合有界 `plugin:{id}`/`group:light:{n}`，快照按
    /// mcp_clients 键取值，陈旧条目无副作用）。
    host_spawned_at: RwLock<HashMap<String, Instant>>,
    /// light 组运行时装箱状态（分配表 + 槽位计数，见 [`LightPacking`]）。
    light_packing: RwLock<LightPacking>,
    /// light 宿主**实际 spawn 时**的成员集快照 {宿主键: 成员集}（惰性装箱漂移
    /// 检测基准，§4.5）。
    ///
    /// 进程成员集在 spawn 时定格，而分配表随惰性装箱动态变化——新成员装箱进
    /// 已存活宿主后两者漂移，fast path 据此判定 stale 触发整宿主 respawn（见
    /// [`Self::is_host_stale`]）。纯内存比对（无 stat），不受指纹 TTL 门约束：
    /// 装箱调用本身落在 TTL 窗口内，靠指纹过期检测会漏（指纹记录的是分配表
    /// 期望态，漂移对指纹不可见）。
    ///
    /// 生命周期与 `mcp_clients` 条目同步：spawn 时写入（[`Self::build_sidecar_transport_client`]），
    /// 进程 kill/驱逐时清除（[`Self::unload_host`] / fast path 驱逐）。
    spawned_members: RwLock<HashMap<String, Vec<String>>>,
    /// 预热常驻集（boot 预热的管道引用插件 id）：这些插件所在宿主组豁免
    /// 空闲回收（预热常驻语义——否则预热被 GC 架空，首条消息重新全价冷启动）。
    /// 集合只增（boot 预热一次定型）；`AGENTOS_DISABLE_SIDECAR_WARMUP=1` 时
    /// 预热不运行、集合恒空，回到纯懒加载 + 全量 GC。
    keep_warm_plugins: RwLock<std::collections::HashSet<String>>,
    /// 宿主 in-flight capability 调用计数 {宿主键: 原子计数}（P12/P22，ADR 2026-09-07）。
    ///
    /// sidecar 统一调用入口（`invoke_pipeline_plugin` / `invoke_tool` 的 Sidecar
    /// 分支，覆盖管道步骤、tool-executor 反向调用含 llm_service 流式、http.handle
    /// 分发）+1（RAII [`InflightGuard`]，Drop -1；成功/失败/超时/流式中断随
    /// future drop 必然回落），空闲 GC 判定以 `inflight == 0` 为前置门——调用
    /// 进行中的宿主不被回收。流式调用的 MCP 响应在流完全结束/断连才返回，
    /// 计数即覆盖整个流生命周期。条目与 `mcp_clients` 同生命周期：
    /// [`Self::unload_host`] 清理；调用先于卸载重建条目也无碍（guard 持 Arc
    /// 克隆，减数不依赖条目存活）。
    inflight_calls: RwLock<HashMap<String, Arc<AtomicUsize>>>,
    /// 成员 in-flight capability 调用计数 {plugin_id: 原子计数}（成员粒度 GC 门：
    /// 该成员有调用在飞时不卸该成员；宿主仍由 [`Self::inflight_calls`] 守整组
    /// kill 面）。RAII 语义与 [`Self::inflight_calls`] 同构。
    member_inflight_calls: RwLock<HashMap<String, Arc<AtomicUsize>>>,
    /// 正在 spawn/respawn 窗口内的插件 id 集（宿主连接 + initialize 握手未完成）。
    ///
    /// D1 宽限面（宿主重载风暴 2026-09-25）：HTTP dispatcher 据此区分「插件处理
    /// 慢」与「基础设施冷启动等待」，前者烧端点超时预算、后者暂停预算（等待
    /// 就绪而非 504，见 http_dispatcher 的分发循环）。spawn 锁内写入本次加载的
    /// 全体成员（组宿主 = 成员集，独占 = 单插件），spawn 出窗（成功/失败/panic）
    /// 由 RAII guard 摘除。键为 plugin_id 扁平集：dispatcher 侧按路由插件 id
    /// O(1) 探测，无需 plugin→宿主键反查。
    starting_plugins: RwLock<std::collections::HashSet<String>>,
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
            member_last_used: RwLock::new(HashMap::new()),
            freeze_respawns: RwLock::new(HashMap::new()),
            host_spawned_at: RwLock::new(HashMap::new()),
            light_packing: RwLock::new(LightPacking::default()),
            spawned_members: RwLock::new(HashMap::new()),
            keep_warm_plugins: RwLock::new(std::collections::HashSet::new()),
            inflight_calls: RwLock::new(HashMap::new()),
            member_inflight_calls: RwLock::new(HashMap::new()),
            starting_plugins: RwLock::new(std::collections::HashSet::new()),
        }
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
                    "Native plugin '{plugin_id}' missing manifest.native.artifact (ADR 附录 D②)"
                ),
                code: Some("MISSING_NATIVE_ARTIFACT".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;
        let dir = self
            .loader
            .get_plugin_dir(plugin_id)
            .ok_or_else(|| PluginError {
                message: format!(
                    "Native plugin '{plugin_id}' directory not found (not discovered?)"
                ),
                code: Some("NATIVE_PLUGIN_DIR_NOT_FOUND".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;
        // 裸名自动补平台后缀；声明带异平台后缀时回退本平台重映射（与 loader 预检同规则）
        let dir_path = std::path::PathBuf::from(dir);
        NativePluginLoader::resolve_artifact(&dir_path, artifact).ok_or_else(|| PluginError {
            message: format!(
                "Native plugin '{}' artifact not found: {}（已按声明与本平台名双查；cdylib 未构建？）",
                plugin_id,
                dir_path.join(artifact).display()
            ),
            code: Some("NATIVE_ARTIFACT_NOT_FOUND".to_string()),
            source: Some("plugin-invoker".to_string()),
        })
    }

    /// 取 native loader 或返回配置缺失错误（三个 native 调用路径共用）。
    fn native_loader_or_err(
        &self,
        plugin_id: &str,
    ) -> Result<&Arc<NativePluginLoader>, PluginError> {
        self.native_loader.as_ref().ok_or_else(|| PluginError {
            message: format!(
                "Native plugin '{plugin_id}' invoked but no NativePluginLoader configured"
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
    /// 委托 [`McpClient::is_dead`] 三证据判定：stdio 写失败标记（进程死亡铁证，
    /// 弥补 try_wait 快照在退出通知未到/被强杀时的竞态盲区）/ child 缺失
    /// （kill 后残留缓存）/ 退出快照。HTTP transport 不判死（远端进程不在
    /// 本地，网络错误走调用侧错误语义）。
    async fn is_dead_sidecar(client: &McpClient) -> bool {
        // 委托 client 自身状态：stdio 写失败标记 / child 缺失（kill 后残留缓存）
        // / 退出快照 三证据（见 McpClient::is_dead）。HTTP 运输恒 false。
        client.is_dead().await
    }

    /// 假死判定与取证（进程治理三信号模型的执行端，评估+取证阶段）。
    ///
    /// 调用时机：call_tool 失败且进程存活（[`Self::is_dead_sidecar`] 已排除
    /// 真死亡）。三条件交叉判定假死：
    /// ① 进程存活（try_wait 无退出——调用方已查）；
    /// ② 事件循环心跳停跳超过 FREEZE_HEARTBEAT_STALE_SECS（SDK 心跳协程
    ///    跑在 sidecar 主事件循环，冻结即停跳；从未上报心跳的旧版本 sidecar
    ///    返回 None → 不判定，平滑兼容）；
    /// ③ 本次调用失败（调用时机已保证——本函数只在 call_tool Err 分支进入）。
    ///
    /// 命中即做冻结取证：判定命中时刻正是冻结窗口，respawn 抹掉现场之前抓
    /// py-spy 线程栈（熔断停手分支同样抓——反复冻结是最需要现场的时刻）。
    ///
    /// 返回 `true` 表示应触发 force_unload 自愈（与 idle-unload 同一回收路径：
    /// kill 进程树 + 驱逐客户端缓存，下次调用自动 respawn 干净实例），调用
    /// 本身仍以 MCP_CALL_FAILED 返回交由既有恢复包装器处理。
    /// 熔断：10 分钟窗口 >= 3 次（FREEZE_RESPAWN_WINDOW_SECS / MAX）停手只
    /// 取证告警，防循环重启掩盖根因（同构 K8s CrashLoopBackOff）。
    ///
    /// 锁契约：本函数只做判定与取证，**不执行 unload**——unload 内部对同一
    /// client Arc 取写锁（[`Self::unload_host`]），调用方读锁存活时执行 =
    /// 同任务自等写锁永久悬挂。调用方必须先释放 `client` 读锁再触发回收
    /// （见 [`Self::attempt_sidecar_pipeline`] 的失败分支）。
    async fn evaluate_frozen_sidecar(
        &self,
        client: &McpClient,
        plugin_id: &str,
        call_err_message: &str,
    ) -> bool {
        const FREEZE_HEARTBEAT_STALE_SECS: u64 = 30;
        const FREEZE_RESPAWN_WINDOW_SECS: u64 = 600;
        const FREEZE_RESPAWN_MAX: usize = 3;
        const FREEZE_DUMP_TIMEOUT: u64 = 10;

        let Some(age) = client.heartbeat_age_secs() else {
            return false; // 旧版本 sidecar / HTTP transport：无心跳数据，兼容期不判死
        };
        if age <= FREEZE_HEARTBEAT_STALE_SECS {
            return false; // 心跳新鲜：非假死（协议/工具错误走既有语义）
        }

        // 熔断检查（滑窗计数）
        let now = Instant::now();
        let circuit_broken = {
            let mut ledger = self.freeze_respawns.write();
            let entries = ledger.entry(plugin_id.to_string()).or_default();
            entries.retain(|t| now.duration_since(*t).as_secs() < FREEZE_RESPAWN_WINDOW_SECS);
            if entries.len() >= FREEZE_RESPAWN_MAX {
                tracing::error!(
                    target: "sidecar",
                    "[{}] 疑似事件循环冻结（心跳停跳 {}s）但 10 分钟内已假死 respawn {} 次，熔断停手——人工介入排查根因",
                    plugin_id, age, entries.len()
                );
                true
            } else {
                entries.push(now);
                false
            }
        };

        // 冻结现场取证：判定命中时刻正是冻结窗口，respawn 抹掉现场之前抓
        // py-spy 线程栈（熔断停手分支同样抓——反复冻结是最需要现场的时刻）。
        let dump_dir = std::path::PathBuf::from("logs").join("freeze_dumps");
        let spy_bin = std::env::var("AGENTOS_PY_SPY_BIN").unwrap_or_else(|_| "py-spy".to_string());
        match client.pid().await {
            Some(pid) => {
                Self::run_freeze_dump(
                    &spy_bin,
                    plugin_id,
                    pid,
                    age,
                    &dump_dir,
                    FREEZE_DUMP_TIMEOUT,
                )
                .await;
            }
            None => {
                tracing::warn!(target: "sidecar", "[{}] 冻结取证跳过：无进程 pid", plugin_id);
            }
        }
        if circuit_broken {
            return false;
        }

        tracing::warn!(
            target: "sidecar",
            "[{}] 疑似事件循环冻结：进程存活但心跳停跳 {}s（阈值 {}s），本次调用失败: {} —— Force unload 触发自愈 respawn",
            plugin_id, age, FREEZE_HEARTBEAT_STALE_SECS, call_err_message
        );
        true
    }

    /// 冻结现场取证：对疑似冻结的 sidecar 进程执行 py-spy dump 抓线程栈。
    ///
    /// 产物落 `dir/{plugin_id}_{时间戳}_pid{pid}.txt`（头部带 plugin/pid/
    /// 心跳停跳时长元信息，正文为 py-spy stdout/stderr）。取证是尽力而为：
    /// py-spy 缺失/超时只降级告警返回 None，绝不 panic/长阻塞——调用点
    /// （[`Self::evaluate_frozen_sidecar`]）评估完成后调用方要做 force_unload
    /// 自愈，回收是硬时限。二进制由调用方解析（默认 PATH 上的 `py-spy`，可用
    /// `AGENTOS_PY_SPY_BIN` 覆盖，venv 部署 PATH 不含 Scripts 目录的场景）。
    /// D3：freeze 取证 dump 保留上限（份）——取证场景无法预知何时需要现场，
    /// 保留最新 10 份 + 写入时顺带清扫，防 logs/freeze_dumps 随冻结次数无界增长。
    const FREEZE_DUMP_KEEP: usize = 10;

    /// 清扫 freeze 取证目录只留最新 `keep` 份（按文件写时间升序删最旧）。
    /// 跨插件共用一个总上限（取证无法预知哪个插件需要现场）；best-effort：
    /// 枚举失败静默返回（目录不存在 = 无可清扫），单个删除失败 warn 留痕继续。
    fn sweep_freeze_dump_dir(dir: &std::path::Path, keep: usize) {
        let Ok(entries) = std::fs::read_dir(dir) else {
            return;
        };
        let mut files: Vec<(std::time::SystemTime, std::path::PathBuf)> = entries
            .filter_map(|e| e.ok())
            .filter(|e| e.path().is_file())
            .filter_map(|e| {
                let modified = e.metadata().ok()?.modified().ok()?;
                Some((modified, e.path()))
            })
            .collect();
        if files.len() <= keep {
            return;
        }
        let excess = files.len() - keep;
        files.sort();
        for (modified, path) in files.into_iter().take(excess) {
            if let Err(e) = std::fs::remove_file(&path) {
                tracing::warn!(
                    target: "sidecar",
                    "freeze 取证清扫失败 {}: {}（mtime {:?}）",
                    path.display(),
                    e,
                    modified
                );
            }
        }
    }

    async fn run_freeze_dump(
        spy_bin: &str,
        plugin_id: &str,
        pid: u32,
        heartbeat_age_secs: u64,
        dir: &std::path::Path,
        timeout_secs: u64,
    ) -> Option<std::path::PathBuf> {
        if let Err(e) = std::fs::create_dir_all(dir) {
            tracing::warn!(target: "sidecar", "[{}] 冻结取证目录创建失败: {}（跳过抓取）", plugin_id, e);
            return None;
        }
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let path = dir.join(format!("{plugin_id}_{stamp}_pid{pid}.txt"));
        let spawned = tokio::process::Command::new(spy_bin)
            .args(["dump", "--pid", &pid.to_string()])
            .output();
        match tokio::time::timeout(std::time::Duration::from_secs(timeout_secs), spawned).await {
            Ok(Ok(output)) => {
                let report = format!(
                    "# sidecar freeze dump\n# plugin: {plugin_id}\n# pid: {pid}\n\
                     # heartbeat_stale_secs: {heartbeat_age_secs}\n\
                     # captured_at_unix: {stamp}\n\
                     # command: {spy_bin} dump --pid {pid}\n\n--- stdout ---\n{}\n--- stderr ---\n{}\n",
                    String::from_utf8_lossy(&output.stdout),
                    String::from_utf8_lossy(&output.stderr),
                );
                match std::fs::write(&path, report) {
                    Ok(_) => {
                        tracing::info!(
                            target: "sidecar",
                            "[{}] 冻结现场已抓取: {}（py-spy exit={}）",
                            plugin_id,
                            path.display(),
                            output.status
                        );
                        // D3：取证 dump 保留上限——写入时顺带清扫只留最新 10 份
                        Self::sweep_freeze_dump_dir(dir, Self::FREEZE_DUMP_KEEP);
                        Some(path)
                    }
                    Err(e) => {
                        tracing::warn!(
                            target: "sidecar",
                            "[{}] 冻结现场写盘失败 {}: {}",
                            plugin_id,
                            path.display(),
                            e
                        );
                        None
                    }
                }
            }
            Ok(Err(e)) => {
                tracing::warn!(
                    target: "sidecar",
                    "[{}] py-spy（{}）启动失败: {}——冻结现场未抓取（安装 py-spy 或设 AGENTOS_PY_SPY_BIN）",
                    plugin_id,
                    spy_bin,
                    e
                );
                None
            }
            Err(_) => {
                tracing::warn!(
                    target: "sidecar",
                    "[{}] py-spy dump 超时（{}s）——放弃取证继续自愈",
                    plugin_id,
                    timeout_secs
                );
                None
            }
        }
    }

    /// B1：错误是否为「sidecar 死亡/传输断开」——可透明恢复类失败。
    ///
    /// 命中条件：
    /// - `PLUGIN_CRASHED`（attempt 侧经 [`Self::is_dead_sidecar`] 判定）；
    /// - `MCP_CALL_FAILED` 携带 stdio 运输写失败特征（transport error + write/
    ///   flush/broken pipe）——写失败只可能发生在已死进程的句柄上，是死亡铁证；
    ///   既往归为"进程仍存活的协议/工具错误"导致进程死后既不驱逐也不重试
    ///   （与 is_dead_sidecar 的写失败标记互补，双保险）。
    /// - `MCP_TOOL_CALL_FAILED`（协议/工具错误）、`MCP_CONNECT_FAILED`（spawn
    ///   失败，重试同样失败）等不重试。
    fn is_recoverable_sidecar_death(err: &PluginError) -> bool {
        if err.code.as_deref() == Some("PLUGIN_CRASHED") {
            return true;
        }
        // call_tool 把底层错误统一包成 "transport error: <write/flush/broken pipe>..."
        // （Windows os error 232 "管道正在被关闭" 等被本地化成消息文本）。
        if err.code.as_deref() == Some("MCP_CALL_FAILED") {
            let msg = err.message.to_lowercase();
            return msg.contains("transport error")
                && (msg.contains("write") || msg.contains("flush") || msg.contains("broken pipe"));
        }
        false
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
    /// ① 调用前存活检查（stdio 进程已死——写失败标记/child 缺失/退出快照三
    ///    证据，见 [`McpClient::is_dead`]；HTTP transport 无进程不判死）；
    /// ② call_tool 失败后复查存活——在途调用中死亡（依赖被 idle GC 杀掉的
    ///    §9.4 场景；进程仍存活则归 MCP_CALL_FAILED，协议/工具错误重试无益，
    ///    运输写失败则由 is_recoverable_sidecar_death 特征兜底恢复）。
    async fn attempt_sidecar_pipeline(
        &self,
        plugin_id: &str,
        manifest: &PluginManifest,
        ctx: &PluginContext<'_>,
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
                    "pipeline plugin '{plugin_id}' missing manifest.invoke_entry (ADR 附录 D②)"
                ),
                code: Some("MISSING_INVOKE_ENTRY".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;
        // 合宿宿主以工具名命名空间区分成员（§4.2 第 3 条）：light 成员的每个
        // 工具在宿主侧注册为 `{plugin_id}.{tool_name}`，调用侧拼前缀分发；
        // 独占宿主无前缀（现状不变）。
        let mcp_tool_name = namespaced_tool_name(manifest, tool_name);

        // Sidecar 模式：通过 MCP 客户端调用
        let client_arc = self.get_or_create_mcp_client(manifest).await?;
        let client = client_arc.read().await;

        // ① 调用前存活检查（B1：死亡 → PLUGIN_CRASHED，交由恢复包装器 respawn+重试）
        if Self::is_dead_sidecar(&client).await {
            return Err(PluginError {
                message: format!("plugin process crashed: {plugin_id}"),
                code: Some("PLUGIN_CRASHED".to_string()),
                source: Some("plugin-invoker".to_string()),
            });
        }

        // _log_ctx：per-request 日志上下文，从 ctx.state 抽取（真实数据所在，
        // 见 server.rs 把 pipeline_id/session_id 写进 state）。SDK 在调 handler
        // 前 LogContext.bind，使 sidecar 日志能带 pipeline_id 关联内核日志。
        let log_ctx = serde_json::json!({
            "pipeline_id": ctx.state.get("pipeline_id").cloned().unwrap_or(Value::Null),
            "request_id": ctx.state.get("request_id").cloned().unwrap_or(Value::Null),
            "session_id": ctx.state.get("session_id").cloned().unwrap_or(Value::Null),
            "agent_name": ctx.state.get("agent.id").cloned().unwrap_or(Value::Null),
        });
        // 每调用 config 注入（对齐 native 管道 invoke_native_pipeline）：引擎
        // 步骤 config 只携带 inputs/_step_method，config_files 注入按调用方
        // manifest 现算（shared::injected_config）——合宿宿主不共享注入，
        // 各成员只收自己的 config_files 命名空间，再与步骤 config 合并。
        let injected = crate::shared::injected_config(self.loader.as_ref(), manifest).await?;
        let call_config = crate::shared::merge_injected_with_step_config(injected, &ctx.config);
        // arguments 直接拼 JSON 串（call_tool_raw 原文透传）：state 每步整包
        // 过境，走中间 Value 树 = 每步多一次全量深拷（json! 宏隐式复制）。
        // state/config/_log_ctx 各自一遍 to_writer 进同一缓冲，帧语义不变。
        // state.reads 切片投喂（组装单点 shared.rs）：声明非空 → 白名单控制键
        // ∪ 声明键切片；未声明 = 全量借用（零拷贝，迁移期默认字节不变）。
        let state_payload = crate::shared::state_feed_payload(manifest, ctx.state);
        // M3b：三段序列化直写 client 复用缓冲（闭包借用 state_payload/config/
        // log_ctx——均活到本调用结束），arguments String 独立分配清零。
        let result = match client
            .call_tool_raw_fragments(&mcp_tool_name, |buf| {
                buf.extend_from_slice(b"{\"state\":");
                serde_json::to_writer(&mut *buf, &state_payload)?;
                buf.extend_from_slice(b",\"config\":");
                serde_json::to_writer(&mut *buf, &call_config)?;
                buf.extend_from_slice(b",\"_log_ctx\":");
                serde_json::to_writer(&mut *buf, &log_ctx)?;
                buf.extend_from_slice(b"}");
                Ok(())
            })
            .await
        {
            Ok(v) => v,
            Err(e) => {
                // ② 失败后复查存活：死亡（含在途死亡）→ PLUGIN_CRASHED（可透明恢复）；
                //    存活 → MCP_CALL_FAILED 语义（协议/工具错误）。
                //    （call_tool 已把底层错误统一包成 ToolCallFailed，无法按错误
                //    类型区分崩溃，故用存活复查作权威判定。）
                let died_mid_call = Self::is_dead_sidecar(&client).await;
                // ③ 假死复查（进程治理三信号模型）：进程存活但事件循环心跳停跳
                //    超过阈值 → 事件循环冻结（「活着却不处理任何请求」）。判定
                //    与取证只需 &client（读锁内完成），命中与否都不在此持锁做
                //    回收——force_unload 内部对同一 client Arc 取写锁（unload_host），
                //    读锁存活时执行 = 同任务自等写锁永久悬挂（请求卡死、该宿主
                //    全部写治理排队、InflightGuard 永不回落致 GC 跳过）。假死时
                //    调用仍按 MCP_CALL_FAILED 返回（透明恢复包装器按既有语义
                //    处理重试）。
                let frozen = !died_mid_call
                    && self
                        .evaluate_frozen_sidecar(&client, plugin_id, &e.to_string())
                        .await;
                // 评估取证完成，先释放读锁再触发 Force unload（unload 从
                // mcp_clients 注册表自取 Arc，锁契约见 evaluate_frozen_sidecar）。
                drop(client);
                if frozen {
                    if let Err(unload_err) = self.force_unload_impl(plugin_id).await {
                        tracing::error!(
                            target: "sidecar",
                            "[{}] 假死自愈 Force unload 失败: {}（残留进程待 idle GC 兜底）",
                            plugin_id, unload_err.message
                        );
                    }
                }
                return Err(if died_mid_call {
                    PluginError {
                        message: format!("plugin process died mid-call: {plugin_id}: {e}"),
                        code: Some("PLUGIN_CRASHED".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    }
                } else {
                    PluginError {
                        message: format!("MCP call failed: {e}"),
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
                message: format!("failed to parse MCP response as PluginResult: {e}"),
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
                message: format!("plugin process crashed: {plugin_id}"),
                code: Some("PLUGIN_CRASHED".to_string()),
                source: Some("plugin-invoker".to_string()),
            });
        }

        // 合宿宿主工具名命名空间前缀（§4.2 第 3 条，与 pipeline 路径同构）。
        let mcp_tool_name = namespaced_tool_name(manifest, tool_name);
        // 标准 MCP 出参归一（external MCP 一行接入配套）：外部标准 MCP server
        // （FastMCP 等）对未声明参数严格校验，内核/管道注入的会话上下文键
        // （_call_context/session_id/workspace 等）必须调用前剥离；内部 sidecar
        // 的 SDK 按签名过滤不受影响。
        let mcp_arguments = if manifest.entry == "mcp:external" {
            sanitize_external_mcp_arguments(manifest, tool_name, inputs)
        } else {
            inputs.clone()
        };
        let result = match client.call_tool(&mcp_tool_name, &mcp_arguments).await {
            Ok(v) => v,
            Err(e) => {
                // ② 失败后复查存活（在途死亡 → PLUGIN_CRASHED；否则原
                //    MCP_TOOL_CALL_FAILED 语义）
                let died_mid_call = Self::is_dead_sidecar(&client).await;
                return Err(if died_mid_call {
                    PluginError {
                        message: format!("plugin process died mid-call: {plugin_id}: {e}"),
                        code: Some("PLUGIN_CRASHED".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    }
                } else {
                    PluginError {
                        message: format!("MCP tool call failed: {e}"),
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
        let tool_result = normalize_mcp_tool_result(inner, tool_name)?;

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
        ctx: &PluginContext<'_>,
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

        // config 注入（shared::injected_config，按 manifest.config_files 命名空间）。
        let config = crate::shared::injected_config(self.loader.as_ref(), manifest).await?;

        // state.reads 切片投喂（与 sidecar 路径同一组装语义，shared.rs 单点）：
        // 声明非空 → 白名单控制键 ∪ 声明键切片；未声明 = 全量（迁移期默认）。
        let state_json = crate::shared::pipeline_state_json(manifest, ctx.state);

        // 构造 PluginCtx（state/config 用 JSON 字符串；tool_call_json=None = pipeline 语义）。
        let plugin_ctx = agentos_native_sdk::PluginCtx {
            state_json,
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
                    response_buf: std::cell::UnsafeCell::new(String::with_capacity(4096)),
                    err_buf: std::cell::UnsafeCell::new(String::with_capacity(256)),
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
            message: format!("native plugin '{plugin_id}' execute task panicked: {join_err}"),
            code: Some("NATIVE_EXECUTE_PANICKED".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;

        // execute 返回 state_updates JSON 字符串 → 解析为 PluginResult。
        let plugin_result = result.and_then(|state_updates_json| {
            let state_updates: std::collections::HashMap<String, serde_json::Value> =
                serde_json::from_str(&state_updates_json).map_err(|e| PluginError {
                    message: format!("native plugin state_updates parse failed: {e}"),
                    code: Some("NATIVE_OUTPUT_PARSE".to_string()),
                    source: Some("plugin-invoker".to_string()),
                })?;
            Ok(PluginResult {
                state_updates,
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

        // config 注入（shared::injected_config，按 manifest.config_files 命名空间）。
        let config = crate::shared::injected_config(self.loader.as_ref(), manifest).await?;

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
                    response_buf: std::cell::UnsafeCell::new(String::with_capacity(4096)),
                    err_buf: std::cell::UnsafeCell::new(String::with_capacity(256)),
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
            message: format!("native plugin '{plugin_id}' execute task panicked: {join_err}"),
            code: Some("NATIVE_EXECUTE_PANICKED".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;

        // execute 返回 JSON 字符串 → 解析 + 归一为 ToolExecutionResult（对齐
        // invoke_tool 的 sidecar 决策树：纯业务数据包 success 信封）。
        let result = raw.and_then(|out_json| {
            let inner: Value = serde_json::from_str(&out_json).map_err(|e| PluginError {
                message: format!("native plugin '{plugin_id}' tool output parse failed: {e}"),
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
    /// service-registry）永远 KeyError。
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
        // spawn 成员集快照与缓存条目同生命周期：全量驱逐一并清空（respawn 重记）
        self.spawned_members.write().clear();
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

    /// 内核停机口：drain 全部 `mcp_clients` 并逐个 kill（0.2 收尾 §3.3a）。
    ///
    /// 直接仿 [`set_router`] 的 drain+kill 既有模式；差异在两点：
    /// - **await 内联**：停机路径进程即将退出，不存在 respawn 竞态窗口，无需
    ///   fire-and-forget——调用方（graceful shutdown / exit 75 排空）等 kill
    ///   完成再退出，保证零孤儿；
    /// - **逐 kill 超时保护**：每个 kill（含写锁获取）上限 2s——个别 sidecar
    ///   卡死（管道阻塞 / kill 挂起）不得拖住退出路径（best-effort，超时记
    ///   warn 后继续下一个）。
    ///
    /// 空（或已 drain 空）缓存 no-op。
    pub async fn shutdown_all(&self) {
        let drained: Vec<(String, Arc<tokio::sync::RwLock<McpClient>>)> =
            self.mcp_clients.write().drain().collect();
        if drained.is_empty() {
            return;
        }
        // spawn 成员集快照与缓存条目同生命周期：停机 drain 一并清空
        self.spawned_members.write().clear();
        let total = drained.len();
        for (id, client) in drained {
            // 超时盖住整个「取写锁 + kill」——写锁死锁与 kill 挂起同等会卡退出。
            let kill_fut = async { client.write().await.kill().await };
            match tokio::time::timeout(Duration::from_secs(2), kill_fut).await {
                Ok(Ok(())) => {}
                Ok(Err(e)) => {
                    warn!("shutdown_all: best-effort kill of sidecar {id} failed: {e}")
                }
                Err(_) => {
                    warn!("shutdown_all: kill of sidecar {id} timed out (2s), skipping")
                }
            }
        }
        info!("shutdown_all: drained and killed {total} cached sidecar(s)");
    }

    /// disable 窄口 kill（0.2 收尾 §3.3b）：`mcp_clients` 移除该插件条目，有则
    /// kill，无则 no-op。
    ///
    /// 刻意**不走 [`Self::force_unload`]**：它含 OnUnload 事件广播 +
    /// loader.unload + 指纹清理，对"仅禁用"语义过重（插件仍在 loader 内，热
    /// 发现不失效）；sidecar 本就按调用懒 spawn，kill 后 reenable 下次调用
    /// 自然重生。kill（含写锁获取）带 2s 超时保护，防个别卡死阻塞 HTTP
    /// handler。幂等：缓存无条目（从未 spawn / 已是 HTTP 无子进程 / 重复调用）
    /// 直接返回。
    pub async fn kill_sidecar_if_any(&self, plugin_id: &str) {
        // 宿主粒度：light 成员 disable 连坐整组（其他成员下次调用 respawn，
        // 分配表条目保留——reenable 后回到原宿主）。
        let Some(host_key) = self.existing_host_key_for(plugin_id) else {
            return;
        };
        let Some(client) = self.mcp_clients.write().remove(&host_key) else {
            return;
        };
        // spawn 成员集快照与缓存条目同生命周期：kill 即清（respawn 重记）
        self.spawned_members.write().remove(&host_key);
        let kill_fut = async { client.write().await.kill().await };
        match tokio::time::timeout(Duration::from_secs(2), kill_fut).await {
            Ok(Ok(())) => {
                info!("kill_sidecar_if_any: killed host {host_key} of disabled plugin {plugin_id}")
            }
            Ok(Err(e)) => {
                warn!("kill_sidecar_if_any: best-effort kill of {host_key} failed: {e}")
            }
            Err(_) => {
                warn!("kill_sidecar_if_any: kill of {plugin_id} timed out (2s)")
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

    /// 预热插件宿主：确保 sidecar 已 spawn 并完成 initialize 握手（幂等——
    /// 缓存命中直接返回，不重 spawn；single-flight 语义与正常调用路径同一把
    /// per-host 锁，与在途调用并发安全）。
    ///
    /// boot 后台预热用：把"启动后首次调用"的宿主冷启动（spawn→MCP initialize
    /// 每宿主秒级）提前到用户尚未发消息的窗口。**不执行任何插件逻辑**——
    /// 仅建连；native（InProcess）无进程模型，no-op Ok。预热集插件登记进
    /// keep-warm 常驻集：其所在宿主组豁免 idle GC（预热常驻——预热若仍被
    /// 空闲回收架空，新会话首条消息会重新全价冷启动，预热失去意义）。非预热
    /// 插件照常懒 spawn + idle GC 治理。失败返回 Err 交调用方记日志跳过
    /// （常驻登记照常生效：spawn 失败的宿主组豁免回收，懒 spawn 兜底补齐）。
    pub async fn warmup_sidecar(&self, manifest: &PluginManifest) -> Result<(), PluginError> {
        if manifest.host_type != HostType::Sidecar {
            return Ok(());
        }
        self.keep_warm_plugins.write().insert(manifest.id.clone());
        self.get_or_create_mcp_client(manifest).await.map(|_| ())
    }

    /// 插件源码目录解析（watcher G2 复验用）：复用 loader 的发现结果拿插件根
    /// 目录。未发现（未装载/已卸载）返回 None。
    pub fn plugin_source_dir(&self, plugin_id: &str) -> Option<std::path::PathBuf> {
        self.loader
            .get_plugin_dir(plugin_id)
            .map(std::path::PathBuf::from)
    }

    /// 预分配宿主键（纯内存，无 IO 无 spawn）：light 成员写入装箱分配表，
    /// solo 成员仅构造键（无状态）。
    ///
    /// 批量预热**必须先逐个预分配、再并发 warmup**：合宿宿主 spawn 的
    /// `--members` 与组指纹取自分配表实时快照——若边分配边 spawn，先到的
    /// 成员会把宿主以"单成员集"spawn 定型，后到成员在指纹 TTL（1s）内被
    /// 快速路径短路复用，宿主里实际没有该成员（预热假成功）。预分配先行
    /// 让每个组宿主一次 spawn 即装载完整成员集，组指纹一次定型零 respawn。
    pub fn preassign_host_key(&self, manifest: &PluginManifest) {
        self.resolve_host_key(manifest);
    }

    /// 旁路广播 OnError 给总线订阅者（审计 / `lifecycle.plugin_error_total` 计数）。
    ///
    /// 与 OnLoad 的旁路 emit 对称：插件调用**失败**时由调用方（[`invoke_pipeline_plugin`]
    /// / [`invoke_tool`] 的中央返回处）调一次本方法，把"插件调用失败"这一事实
    /// fan-out 给观察层。失败两种形态都计：调用层 `Err`（崩溃/MCP/解析）与
    /// Ok 携带失败结果（pipeline error 字段 / tool success=false）。
    /// **不改错误处理语义**——原错误/结果照常向上传播，本方法仅 fire-and-forget
    /// 观察一次。
    ///
    /// best-effort、非阻塞：未注入总线（`None`，如单测）时 no-op，行为不变。
    fn emit_lifecycle_error(&self, plugin_id: &str, message: &str, code: Option<&str>) {
        let bus_guard = self.hook_bus.read();
        let Some(bus) = bus_guard.as_ref() else {
            return;
        };
        let mut ctx = HookContext::new();
        ctx.set("plugin_id", json!(plugin_id));
        ctx.set("error", json!(message));
        if let Some(code) = code {
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

    /// 获取或创建 MCP 客户端（按需加载，宿主键粒度——合宿进程模型 §4.2）。
    ///
    /// light 合宿插件首次调用时经 [`Self::resolve_host_key`] 动态装箱到
    /// `group:light:{n}` 宿主；其余 sidecar 走 `plugin:{plugin_id}` 独占宿主。
    /// 并发安全：用 per-host spawn 锁（single-flight）保证同一宿主键的 spawn
    /// 串行化——并发触发只创建一个宿主进程，不遗留孤儿进程；跨宿主并行。
    async fn get_or_create_mcp_client(
        &self,
        manifest: &PluginManifest,
    ) -> Result<Arc<tokio::sync::RwLock<McpClient>>, PluginError> {
        // 宿主键解析：light 首次调用装箱分配（粘性），此后命中分配表直读。
        let host_key = self.resolve_host_key(manifest);

        // Fast path：无锁查缓存，命中且未死直接复用（热路径，避开 spawn 锁开销）；
        // 已死/宿主指纹过期就地 kill+驱逐，落到 slow path respawn。判死与复活
        // 门控细节见 reuse_cached_host_fast_path。
        if let Some(client) = self.reuse_cached_host_fast_path(&host_key, manifest).await {
            return Ok(client);
        }

        // Slow path：拿 per-host spawn 锁，串行化同宿主键的 spawn（single-flight
        // ——并发触发只创建一个宿主进程，不遗留孤儿进程；跨宿主并行）。
        // 取（或创建）该宿主的专用锁 Arc——锁本身常驻 spawn_locks，不随调用释放。
        let spawn_lock = {
            let mut locks = self.spawn_locks.write();
            locks
                .entry(host_key.clone())
                .or_insert_with(|| Arc::new(tokio::sync::Mutex::new(())))
                .clone()
        };
        let _spawn_guard = spawn_lock.lock().await;

        // Double-check：持 spawn 锁后再查缓存——前一个持锁者可能已创建好 client，
        // 命中即复用避免重复 spawn（判死门控与 fast path 同款；极端情况 double-check
        // 时又崩溃 → kill 后继续走下方 spawn）。细节见 reuse_fresh_host_locked。
        if let Some(client) = self.reuse_fresh_host_locked(&host_key, &manifest.id).await {
            return Ok(client);
        }

        // D1 宽限面：spawn 窗口记账（本次加载的全体成员，组宿主 = 成员集快照
        // 基准即当前分配表，独占 = 单插件）。guard 持写锁摘除，成功/失败/panic
        // 出窗必清——HTTP dispatcher 在窗口内暂停端点超时预算（等待就绪而非
        // 504），泄漏一个条目 = 该插件端点宽限常开，必须出窗即清。
        let starting_members = match parse_group_slot(&host_key) {
            Some(_) => self.host_members(&host_key),
            None => vec![manifest.id.clone()],
        };
        {
            let mut starting = self.starting_plugins.write();
            for member in &starting_members {
                starting.insert(member.clone());
            }
        }
        let _starting_guard = StartingPluginsGuard {
            set: &self.starting_plugins,
            members: starting_members,
        };

        // 构造新客户端（持有 spawn 锁保证串行）：传输三路分流由 manifest.mcp
        // 决定（HTTP 远程 / 外部 stdio 命令 / 项目自带 sidecar），见 build_mcp_client。
        let client = self.build_mcp_client(&host_key, manifest)?;

        // 连接 + initialize 握手 + on_load 通知/总线广播 → 按宿主键注册缓存
        // （细节见 connect_initialize_and_cache）。
        self.connect_initialize_and_cache(client, &host_key, manifest)
            .await
    }

    /// Fast path 复用判定（无锁查缓存）：命中且存活按指纹新旧二分——
    ///
    /// - **新鲜**：`touch_last_used` 后返回 Some（直接复用，零 spawn）。
    /// - **Pull 热加载触发**（TTL 过后 stat mtime 发现变化）：kill 旧宿主并驱逐，
    ///   不调 notify_crash（主动热更新非崩溃），返回 None 进 slow path respawn；
    ///   合宿宿主按当前分配表重建成员集。TTL 门 + 指纹比对语义：合宿宿主 =
    ///   成员指纹并集；未用到也不检测——纯按需 pull。
    /// - **stdio 宿主真死**（pid=Some 且进程退出；HTTP transport pid=None 恒不进
    ///   此分支）：error 留痕 + kill 驱逐；合宿宿主死亡 = 全组成员进程死，逐成员
    ///   触发崩溃回调（能力卸载语义）。返回 None 进 slow path。
    /// - **未命中**：直接 None（无副作用）。
    async fn reuse_cached_host_fast_path(
        &self,
        host_key: &str,
        manifest: &PluginManifest,
    ) -> Option<SharedMcpClient> {
        let cached = {
            let clients = self.mcp_clients.read();
            clients.get(host_key).cloned()
        };
        let client = cached?;
        let client_guard = client.read().await;
        if Self::is_dead_sidecar(&client_guard).await {
            error!("Host process crashed: {}", host_key);
            drop(client_guard);
            for member in self.host_members(host_key) {
                self.notify_crash(&member);
            }
            self.kill_and_evict_if_current(host_key, &client).await;
            return None;
        }
        if !self.is_host_stale(host_key, manifest).await {
            self.touch_member_last_used(&manifest.id);
            return Some(Arc::clone(&client));
        }
        info!(
            "Host code/config or member set changed, reloading host: {}",
            host_key
        );
        drop(client_guard);
        self.kill_and_evict_if_current(host_key, &client).await;
        None
    }

    /// 驱逐已判定死亡/过期的宿主实例（kill + 缓存/快照逐出，按条目身份条件执行）。
    ///
    /// BUG-44 根因修复：kill 会因在飞调用持客户端读锁而阻塞数秒，苏醒后
    /// mcp_clients 条目可能已被并发 respawn 替换（新实例入缓存 + 新 spawn
    /// 成员集快照已写入）。此处若无条件 remove，会把并发 respawn 写入的
    /// 新鲜快照连带删掉，留下「缓存存活但快照缺失」的宿主——成员集漂移
    /// 检测（快照比对）失去基准，新装箱成员首调用复用旧成员集进程报
    /// MCP [-32602] tool not found（2026-09-18 00:21 实测序列）。
    /// 因此逐出前按 Arc 身份复核：条目已非本次判定的实例（被并发 respawn
    /// 替换/移除）则不动缓存与快照，只杀死本次判定的旧实例。
    ///
    /// BUG-81 根因修复：条目摘除**先于** kill，且 kill 绝不 inline 等写锁——
    /// 长等待调用（审批 wait_for_choice 24h）持读锁期间，`write().await` 的
    /// 公平排队会把全部后续读者（fast path 的 `client.read()`）队头阻塞在
    /// 驱逐者身后，整宿主所有端点连锁超时（装机版 2026-09-23T19:21 实测
    /// approval_service 504 风暴 5400+ 条）。对齐 [`Self::unload_host`] 的
    /// 既有模式：身份命中即摘条目与快照（新调用立即走 respawn 新实例），
    /// `try_write` 拿不到写锁（在飞调用未排空）就放弃本次 kill——在飞 Arc
    /// 全部释放时由 kill_on_drop 兜底终止残留进程。
    async fn kill_and_evict_if_current(&self, host_key: &str, client: &SharedMcpClient) {
        let evicted = {
            let mut clients = self.mcp_clients.write();
            let is_current = clients
                .get(host_key)
                .is_some_and(|cur| Arc::ptr_eq(cur, client));
            if is_current {
                clients.remove(host_key);
            }
            is_current
        };
        if evicted {
            self.spawned_members.write().remove(host_key);
        }
        match client.try_write() {
            Ok(mut guard) => {
                if let Err(e) = guard.kill().await {
                    tracing::debug!(
                        "evict: best-effort kill of host {} failed (will respawn): {e}",
                        host_key
                    );
                }
            }
            Err(_) => {
                tracing::debug!(
                    "evict: kill of host {} deferred (in-flight calls hold read lock; kill_on_drop reaps)",
                    host_key
                );
            }
        }
    }

    /// Double-check 复用判定（持 spawn 锁后调用）：前一个持锁者可能已创建好
    /// client。命中且存活 → touch 后返回 Some；又崩溃 → kill+驱逐返回 None 继续
    /// spawn。判死与 fast path 同用 is_dead_sidecar 门控（HTTP transport 不判死，
    /// 防误报）；不做指纹 staleness 检测（刚 spawn/校验过的进程无需 stat），
    /// 但做成员集漂移检测——spawn 窗口内新成员装箱进本宿主（分配表已更新、
    /// 快照未写入）时，前一个持锁者 spawn 的进程成员集已过期，必须 kill 重来。
    ///
    /// BUG-81 同源修复（与 [`Self::kill_and_evict_if_current`] 同理）：条目/快照
    /// 摘除先于 kill，kill 用 `try_write` 不 inline 等写锁——在飞长调用持读锁时
    /// inline 等待会在持 spawn 锁状态下悬挂，该宿主的全部 respawn/复用连锁排队。
    /// 写锁拿不到就放弃本次 kill，残留进程由 kill_on_drop 兜底。
    async fn reuse_fresh_host_locked(
        &self,
        host_key: &str,
        plugin_id: &str,
    ) -> Option<SharedMcpClient> {
        let cached = {
            let clients = self.mcp_clients.read();
            clients.get(host_key).cloned()
        };
        let client = cached?;
        let client_guard = client.read().await;
        if !Self::is_dead_sidecar(&client_guard).await && !self.light_host_members_drifted(host_key)
        {
            self.touch_member_last_used(plugin_id);
            return Some(Arc::clone(&client));
        }
        drop(client_guard);
        self.mcp_clients.write().remove(host_key);
        self.spawned_members.write().remove(host_key);
        match client.try_write() {
            Ok(mut guard) => {
                if let Err(e) = guard.kill().await {
                    tracing::debug!(
                        "double-check: best-effort kill of host {} failed (will respawn): {e}",
                        host_key
                    );
                }
            }
            Err(_) => {
                tracing::debug!(
                    "double-check: kill of host {} deferred (in-flight calls hold read lock; kill_on_drop reaps)",
                    host_key
                );
            }
        }
        None
    }

    /// 构造新 MCP 客户端（未连接）：传输三路分流（由 manifest.mcp 决定）——
    ///
    /// ① mcp.transport=StreamableHttp + endpoint.url → 外部远程 MCP，走 HTTP
    ///    （不 spawn），见 [`Self::build_http_transport_client`]；
    /// ② mcp.transport=Stdio + endpoint.command → 外部本地第三方命令 MCP
    ///    （如 npx），spawn endpoint.command（不经 parse_entry，entry 仅作语义
    ///    标记），见 [`Self::build_external_stdio_transport_client`]；
    /// ③ 其余（无 mcp 配置的项目自带 sidecar）→ parse_entry(entry) → stdio，
    ///    见 [`Self::build_sidecar_transport_client`]。
    ///
    /// 尾部统一应用插件级调用超时（manifest.mcp.request_timeout_secs）：长等待
    /// 业务（交互插件 wait_for_choice 的 24h 审批等）以声明值为准；未声明时
    /// 落内核 mcp 客户端默认（86400s，BUG-60 审批族统一值）——不再有 300s 默认
    /// 先于用户操作掐断调用（-32001 超时 → 审批作废 → 引擎重试弹窗循环）。
    fn build_mcp_client(
        &self,
        host_key: &str,
        manifest: &PluginManifest,
    ) -> Result<McpClient, PluginError> {
        let mut client = match manifest.mcp.as_ref() {
            Some(cfg) if cfg.transport == agentos_core::traits::McpTransport::StreamableHttp => {
                Self::build_http_transport_client(cfg, manifest)?
            }
            Some(cfg)
                if cfg.transport == agentos_core::traits::McpTransport::Stdio
                    && cfg
                        .endpoint
                        .as_ref()
                        .and_then(|e| e.command.as_ref())
                        .is_some() =>
            {
                Self::build_external_stdio_transport_client(cfg, manifest)?
            }
            _ => self.build_sidecar_transport_client(host_key, manifest)?,
        };

        if let Some(secs) = manifest.mcp.as_ref().and_then(|m| m.request_timeout_secs) {
            client = client.with_request_timeout(std::time::Duration::from_secs(secs));
        }
        Ok(client)
    }

    /// 分流①：外部远程 HTTP MCP 客户端。endpoint.url 缺失即协议级配置错误
    /// （fail-closed，MCP_CONFIG_INVALID），不降级不回退。
    fn build_http_transport_client(
        cfg: &agentos_core::traits::McpConfig,
        manifest: &PluginManifest,
    ) -> Result<McpClient, PluginError> {
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
        Ok(
            McpClient::new_http(url, ep.headers.clone(), ep.auth.clone())
                .with_plugin_id(&manifest.id),
        )
    }

    /// 分流②：外部本地第三方命令 MCP（如 npx playwright）——spawn
    /// endpoint.command。env 值含 ${ENV_VAR} 占位 → 解析（缺失则启动失败早暴露）。
    fn build_external_stdio_transport_client(
        cfg: &agentos_core::traits::McpConfig,
        manifest: &PluginManifest,
    ) -> Result<McpClient, PluginError> {
        let ep = cfg.endpoint.as_ref().expect("checked above");
        let command = match ep.command.clone() {
            Some(c) if !c.is_empty() => c,
            _ => {
                return Err(PluginError {
                    message: format!("插件 {} stdio endpoint 缺 command", manifest.id),
                    code: Some("MCP_CONFIG_INVALID".to_string()),
                    source: Some("plugin-invoker".to_string()),
                })
            }
        };
        tracing::info!(
            "[invoker] 插件 {} 走外部 stdio 命令 | command={} {}",
            manifest.id,
            command,
            ep.args.join(" ")
        );
        let mut c = McpClient::new_stdio(command, ep.args.clone()).with_plugin_id(&manifest.id);
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
        Ok(c)
    }

    /// 分流③：项目自带 sidecar 的 stdio 客户端。第四维分流（合宿进程模型 §4.2
    /// 第 2 条）：light 合宿成员 → 共享组宿主命令（host.py）；独占 → 插件自身
    /// sidecar 命令（现状语义）。两形态共用同一 spawn 锁/判死/respawn 代码路径，
    /// 仅命令与工作目录不同。
    fn build_sidecar_transport_client(
        &self,
        host_key: &str,
        manifest: &PluginManifest,
    ) -> Result<McpClient, PluginError> {
        let (command, args, working_dir) = if is_cohost_member(manifest) {
            let (group, slot) = parse_group_slot(host_key)
                .expect("合宿成员宿主键必含组名与槽位号（resolve_host_key 分配保证）");
            let members = self.host_members(host_key);
            // 记录实际 spawn 的成员集快照（漂移检测基准，见 spawned_members 字段
            // 注释）。先装箱后 spawn 的调用序保证：此处快照 = 本次 spawn 的
            // --members 注入集；快照写入先于客户端入缓存，fast path 判定时必已
            // 就位。失败路径（resolve_group_host_command 报错）不写快照——进程
            // 未 spawn，无漂移可言。
            self.spawned_members
                .write()
                .insert(host_key.to_string(), members.clone());
            let (command, args, workdir) = self.resolve_group_host_command(
                group,
                slot,
                &members,
                builtin_group_host_dir().as_deref(),
            )?;
            (command, args, Some(workdir))
        } else {
            let (command, args) = self.resolve_sidecar_command(manifest)?;
            let workdir = self.loader.get_plugin_dir(&manifest.id);
            (command, args, workdir)
        };
        let mut c = McpClient::new_stdio(command, args)
            // 宿主键用于 stderr 转发时区分宿主日志来源（[host_key] 前缀）——
            // 合宿连接是组共享进程，按宿主键归因日志。
            .with_plugin_id(host_key);

        // 应用 Capability 路由器（启用 sidecar→内核反向调用通道）。
        // PluginScopedRouter 把连接身份注入每次反向调用的 params（G6 信任锚点 +
        // metrics 命名空间）。身份 = 进程身份：独占 = manifest.id；合宿 = 宿主键
        // ——合宿进程是一个授权主体，身份不随成员重生漂移；G6 侧按
        // group_granted_capabilities 归并成员白名单判，任一成员的个人白名单
        // 都不作全组调用的判据。
        {
            let router_guard = self.router.read();
            if let Some(router) = router_guard.as_ref() {
                let scoped: Arc<dyn CapabilityRouter> = Arc::new(PluginScopedRouter {
                    plugin_id: scoped_router_identity(manifest, host_key),
                    inner: Arc::clone(router),
                });
                c = c.with_router(scoped);
            }
        }

        // 设置工作目录（light 合宿 = plugins/shared/_host/；独占 = 插件目录，
        // 确保 server.py 等相对路径可解析）
        if let Some(dir) = working_dir {
            c = c.with_working_dir(dir);
        }

        // PYTHONPATH 不注入：SDK 由 per-plugin venv 的 editable install 解析，
        // env 注入会形成第二套解析路径（版本不同步事故温床）。
        // 这里只透传日志配置 env（进程级常量，适合走 env；per-request 上下文
        // 走 JSON-RPC）。仅当父进程已设置时透传，否则让 sidecar SDK 用其默认
        // （INFO + stderr）。SDK 启动时读这些 env 调用 setup_logging，使 sidecar
        // 日志走统一基础设施。
        let mut extra_env: Vec<(String, String)> = Vec::new();
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
        Ok(c)
    }

    /// 新客户端激活链：connect → 装载配置 → initialize 握手 → on_load 直调 +
    /// 总线旁路广播 → 按宿主键写入缓存并记活跃时刻。成功即返回可复用句柄
    /// （拿走客户端所有权——连接后的进程句柄归宿主键缓存条目所有）。
    async fn connect_initialize_and_cache(
        &self,
        mut client: McpClient,
        host_key: &str,
        manifest: &PluginManifest,
    ) -> Result<SharedMcpClient, PluginError> {
        client.connect().await.map_err(|e| PluginError {
            message: format!("MCP connect failed: {e}"),
            code: Some("MCP_CONNECT_FAILED".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;

        // initialize 握手（携带插件配置）。配置加载分级语义（PARSE 上抛/
        // IO 降级空配置）由 shared::injected_config 统一承载。
        // 按需注入（ADR §4.3，P6）：只走 config_files 映射；未声明则收空配置。
        // 避免把全系统配置（含其他插件凭证）泄漏给每个 sidecar。
        // 复用 shared::build_injected_config——native/wasm 分支也走同一函数，三家对齐。
        let config = crate::shared::injected_config(self.loader.as_ref(), manifest).await?;
        client.initialize(&config).await.map_err(|e| PluginError {
            message: format!("MCP initialize failed: {e}"),
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
            "MCP client connected and initialized: host={}, caller={}",
            host_key, manifest.id
        );

        let client_arc = Arc::new(tokio::sync::RwLock::new(client));

        // 缓存（按宿主键——合宿成员共享同一客户端条目）
        {
            let mut clients = self.mcp_clients.write();
            clients.insert(host_key.to_string(), Arc::clone(&client_arc));
        }
        // 监控 M3：记录宿主 spawn 时刻（进程态轮询的 uptime 源；respawn 覆盖写）
        {
            let mut spawned = self.host_spawned_at.write();
            spawned.insert(host_key.to_string(), Instant::now());
        }
        // 新 spawn 即"活跃"，记录成员最后调用时刻（空闲软卸载依据，成员粒度记账）
        self.touch_member_last_used(&manifest.id);

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

    /// 解析 sidecar 启动命令（uv 迁移，**单轨 fail-closed**）。
    ///
    /// entry 首词是 PATH 裸 python/python3（含 `.exe`/`.bat`/`.cmd` 扩展）的 Python
    /// sidecar **必须**命中插件目录的 venv 解释器：uv 迁移契约双标志
    /// （**pyproject.toml + `.venv`**）缺任一、或 loader 查不到插件目录 → 直接返回
    /// Err（带可读修复指引），**不再回退 PATH 裸 python**——兜底会
    /// 把"venv 未建"静默糊弄成"裸解释器缺依赖启动即崩/行为漂移"，单轨契约下
    /// 早失败比晚卡死好。
    ///
    /// 不受影响的形态：
    /// - entry 首词非 python（如 `node server.js`）→ 原样（本函数只管 Python sidecar）；
    /// - entry 首词带路径分隔符的显式解释器（如 `/usr/bin/python3`）→ 刻意选择，原样；
    /// - external MCP（streamable_http / 外部 stdio command）/ native dll → 不走本函数。
    ///
    /// venv 解释器以**绝对路径**替代裸命令——SDK/第三方依赖由 venv 内 editable
    /// install 解析（不走 PYTHONPATH env）。绝对路径含路径分隔符与
    /// `.`，天然绕过 mcp 侧 `resolve_windows_command` 的 PATHEXT 探测（该函数对
    /// 带分隔符/扩展名的命令原样返回），不会被误解析。
    fn resolve_sidecar_command(
        &self,
        manifest: &PluginManifest,
    ) -> Result<(String, Vec<String>), PluginError> {
        let (mut command, args) = self.parse_entry(&manifest.entry)?;
        if !is_plain_python_command(&command) {
            return Ok((command, args));
        }

        // fail-closed：Python sidecar 必须有 venv（uv 单轨，无 plain 回退轨）。
        let dir = self.loader.get_plugin_dir(&manifest.id).ok_or_else(|| {
            PluginError {
                message: format!(
                    "插件 {} 是 Python sidecar（entry 首词为 PATH 裸 python），但 loader 无法定位其插件目录——\
                     无法解析 venv 解释器（uv 单轨契约：pyproject.toml + .venv 双标志；仅支持 venv 解释器，无 PATH python 回退）",
                    manifest.id
                ),
                code: Some("PLUGIN_DIR_NOT_FOUND".to_string()),
                source: Some("plugin-invoker".to_string()),
            }
        })?;
        let plugin_path = std::path::Path::new(&dir);
        if !plugin_path.join("pyproject.toml").is_file() {
            return Err(PluginError {
                message: format!(
                    "插件 {} 缺 pyproject.toml——uv 单轨契约（pyproject.toml + .venv 双标志）不满足，\
                     Python sidecar 不回退 PATH 裸 python。\
                     修复：为插件补 pyproject.toml（模板见 scripts/migrate_plugins_to_uv.py），\
                     依赖清单参照 docs/working/uv依赖人工确认清单_20260819.md（插件目录 {}）",
                    manifest.id,
                    plugin_path.display()
                ),
                code: Some("PYPROJECT_MISSING".to_string()),
                source: Some("plugin-invoker".to_string()),
            });
        }
        let interpreter = find_venv_interpreter(plugin_path).ok_or_else(|| PluginError {
            message: format!(
                "插件 {} 的 .venv 解释器缺失（探测 .venv/Scripts/python.exe 与 .venv/bin/python 均不存在）——\
                 Python sidecar 不回退 PATH 裸 python。\
                 修复：在插件目录 {} 下 `uv venv --python 3.12 && uv pip install -e <repo>/plugins/sdk + 确认清单依赖`，\
                 或 `uv sync --project <插件目录>`（重建口径见 docs/working/插件uv运行时迁移方案_20260819.md）",
                manifest.id,
                plugin_path.display()
            ),
            code: Some("VENV_INTERPRETER_MISSING".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;
        info!(
            "[invoker] 插件 {} 走 venv 解释器（uv 单轨）| interpreter={}",
            manifest.id,
            interpreter.display()
        );
        command = interpreter.to_string_lossy().into_owned();
        Ok((command, args))
    }

    // ── 宿主键路由（合宿进程模型 §4.2/§4.5）────────────────────────────

    /// 解析插件的宿主键：合宿成员动态装箱到声明组的组宿主，其余独占。
    ///
    /// 合宿成员首次调用时经 [`Self::assign_light_host`] 装箱（粘性，宿主存活期间
    /// 归属不变，声明组名变更即搬组重装箱）；此后每次调用命中分配表直读。
    /// 独占宿主键纯函数构造，无状态。
    fn resolve_host_key(&self, manifest: &PluginManifest) -> String {
        if is_cohost_member(manifest) {
            self.assign_light_host(manifest)
        } else {
            solo_host_key(&manifest.id)
        }
    }

    /// 合宿成员装箱分配宿主键（粘性 + 未满宿主优先 + 溢出开新宿主，按声明组分域）。
    fn assign_light_host(&self, manifest: &PluginManifest) -> String {
        let group = manifest.host_group.as_deref().unwrap_or(DEFAULT_HOST_GROUP);
        self.assign_light_host_with(&manifest.id, group, light_host_max_members())
    }

    /// 装箱核心逻辑（组名与 max_members 参数化以便单测注入，不读环境变量）。
    ///
    /// 落点规则（§4.5，按声明组分域）：
    /// 1. 分配表已有该插件且宿主键属当前声明组 → 返回既有宿主键（粘性，宿主
    ///    存活期间归属不变）；条目属其他组（manifest 改组名 = 搬组）→ 按新组
    ///    重装箱，旧宿主成员集随条目覆盖而收缩；
    /// 2. 从槽位 1 起找**本组**「当前挂载数 < 上限」的宿主塞入——已 idle GC
    ///    回收的宿主（成员条目已清、计数归 0）自然优先复用，而不是无限开新组；
    /// 3. 本组全部满 → 开本组新槽位（序号 = 组内 next_slot+1，装箱顺序即序号）。
    fn assign_light_host_with(&self, plugin_id: &str, group: &str, max_members: usize) -> String {
        let mut packing = self.light_packing.write();
        if let Some(host_key) = packing.assignments.get(plugin_id) {
            let same_group = parse_group_slot(host_key).is_some_and(|(g, _)| g == group);
            if same_group {
                return host_key.clone();
            }
        }
        let mut counts: HashMap<u64, usize> = HashMap::new();
        for host_key in packing.assignments.values() {
            if let Some((g, slot)) = parse_group_slot(host_key) {
                if g == group {
                    *counts.entry(slot).or_default() += 1;
                }
            }
        }
        let group_next_slot = packing.next_slot.get(group).copied().unwrap_or(0);
        for slot in 1..=group_next_slot {
            if counts.get(&slot).copied().unwrap_or(0) < max_members {
                let host_key = group_host_key(group, slot);
                packing
                    .assignments
                    .insert(plugin_id.to_string(), host_key.clone());
                return host_key;
            }
        }
        let new_slot = group_next_slot + 1;
        packing.next_slot.insert(group.to_string(), new_slot);
        let host_key = group_host_key(group, new_slot);
        packing
            .assignments
            .insert(plugin_id.to_string(), host_key.clone());
        host_key
    }

    /// 列出宿主的全部成员 plugin_id（排序后返回，作为稳定契约喂给 spawn/指纹/GC）。
    ///
    /// - light 组宿主：分配表反查（成员集随装箱动态变化，respawn 按当前表重建）；
    /// - 独占宿主：键内嵌的 plugin_id 本身。
    fn host_members(&self, host_key: &str) -> Vec<String> {
        if parse_group_slot(host_key).is_some() {
            let packing = self.light_packing.read();
            let mut members: Vec<String> = packing
                .assignments
                .iter()
                .filter(|(_, hk)| hk.as_str() == host_key)
                .map(|(pid, _)| pid.clone())
                .collect();
            members.sort();
            members
        } else {
            host_key
                .strip_prefix("plugin:")
                .map(|pid| vec![pid.to_string()])
                .unwrap_or_default()
        }
    }

    /// 组主体的 granted_capabilities 归并（G6 组身份配套，合宿进程 = 一个授权主体）。
    ///
    /// 组声明 = 当前装箱成员声明的并集（排序去重）。任一成员 manifest 缺失或未
    /// 声明 granted_capabilities → 组视为未声明（返回 None），沿用未声明语义开关
    /// （默认全授予 / AGENTOS_GRANTS_STRICT=1 拒绝）——与单插件"未声明"语义同构，
    /// 存量未声明成员不因合宿收窄。每次调用从装箱分配表现算：成员集随装箱/重生
    /// 动态变化，归并结果自动跟随，无快照、无失效清理面。
    pub fn group_granted_capabilities(&self, host_key: &str) -> Option<Vec<String>> {
        parse_group_slot(host_key)?;
        let members = self.host_members(host_key);
        if members.is_empty() {
            return None;
        }
        let mut union: Vec<String> = Vec::new();
        for pid in members {
            match self.loader.get_manifest(&pid) {
                Some(m) if !m.granted_capabilities.is_empty() => {
                    union.extend(m.granted_capabilities)
                }
                // 成员未声明 = 组未声明（同进程互信，成员的默认授予即组的默认授予）
                _ => return None,
            }
        }
        union.sort();
        union.dedup();
        Some(union)
    }

    /// light 宿主成员集漂移检测：分配表当前成员集 vs 实际 spawn 时的成员集快照。
    ///
    /// 返回 true = 漂移（有新成员装箱进已存活宿主，进程成员集已过期，必须
    /// kill 整宿主 respawn 按新成员集重建）。非 light 宿主恒 false（独占宿主
    /// 成员集 = 键内嵌 plugin_id，spawn 后不变）。
    ///
    /// 纯内存比对（无 stat、无锁竞争窗口），不受指纹 TTL 门约束——装箱调用
    /// 本身落在 TTL 窗口内，指纹过期检测会漏（指纹记录的是分配表期望态，
    /// 漂移对指纹不可见）。spawn 时快照与分配表更新同序（先装箱后 spawn），
    /// 快照写入先于客户端入缓存，fast path 判定时快照必已就位。
    ///
    /// 快照缺失（缓存条目存在但无快照）按**漂移**处理（BUG-44 安全网）：
    /// 快照与缓存条目在生产路径同生命周期（spawn 时写入先于入缓存、驱逐时
    /// 成对移除），缺失 = 成员集未知——复用成员集未知的进程正是 BUG-44
    /// T1 复用旧宿主报 MCP [-32602] 的形态，fail-closed 重建（respawn 按
    /// 当前分配表重写快照，自愈有界：一次 respawn 后快照必就位）。
    fn light_host_members_drifted(&self, host_key: &str) -> bool {
        if parse_group_slot(host_key).is_none() {
            return false;
        }
        let current = self.host_members(host_key);
        match self.spawned_members.read().get(host_key).cloned() {
            Some(spawned) => current != spawned,
            None => true,
        }
    }

    /// 已 spawn 宿主的宿主键反查（kill/unload 入口按 plugin_id 进来时用）。
    ///
    /// 优先级：light 分配表条目（manifest 可能已不可得但分配仍在）→ manifest
    /// 判定（light 未分配 = 从未 spawn，无宿主；InProcess 无 sidecar 宿主）→
    /// 独占键兜底（manifest 缺失时保守按独占处理，命中缓存即生效）。
    fn existing_host_key_for(&self, plugin_id: &str) -> Option<String> {
        if let Some(host_key) = self
            .light_packing
            .read()
            .assignments
            .get(plugin_id)
            .cloned()
        {
            return Some(host_key);
        }
        match self.loader.get_manifest(plugin_id) {
            Some(m) if is_cohost_member(&m) => None,
            Some(m) if m.host_type != HostType::Sidecar => None,
            _ => Some(solo_host_key(plugin_id)),
        }
    }

    /// 解析合宿宿主 spawn 命令（§4.2 第 2 条：`python host.py --group light
    /// --slot {n} --members {逗号分隔成员列表}`）。
    ///
    /// 返回 (command, args, 工作目录)。工作目录固定为合宿宿主目录
    /// `plugins/shared/_host/`（host.py 与共享 venv 的所在地，由宿主侧任务承载，
    /// 内核只管 spawn 参数契约）；解释器走共享 venv（uv 单轨 fail-closed：
    /// 缺 `.venv` 直接报错，不回退 PATH 裸 python）。
    ///
    /// 宿主目录定位序：①从成员插件目录向上探测（现状语义，仓库侧成员路径行为
    /// 不变）；②探测全空时回退 `fallback_host_dir`（生产传 builtin_group_host_dir()
    /// 即内置根 `AGENTOS_PLUGINS_DIR` 下的 `_host/`）——双源用户副本赢后全组成员
    /// 可能落用户空间，祖先链永不含内置 `_host/`，回退使宿主进程与共享 venv 仍由
    /// 内置仓库侧承载（None = 无回退，探测全空照旧 HOST_DIR_NOT_FOUND）。
    fn resolve_group_host_command(
        &self,
        group: &str,
        slot: u64,
        members: &[String],
        fallback_host_dir: Option<&Path>,
    ) -> Result<(String, Vec<String>, String), PluginError> {
        // _host 目录定位：优先从任一成员插件目录向上找含 _host 子目录的祖先
        // （插件目录层级不固定：plugins/shared/<type>/<phase>/<name>）；探测
        // 全空才回退内置根 _host/，命中即 warn 留痕（可观测的路径漂移）。
        let host_dir = members
            .iter()
            .filter_map(|pid| self.loader.get_plugin_dir(pid))
            .find_map(|dir| find_group_host_dir(Path::new(&dir)))
            .or_else(|| {
                let fallback = fallback_host_dir
                    .map(|root| root.join(GROUP_HOST_DIR))
                    .filter(|candidate| candidate.is_dir());
                if let Some(dir) = &fallback {
                    warn!(
                        target: "sidecar",
                        members = ?members,
                        host_dir = %dir.display(),
                        "合宿 _host/ 从成员插件目录向上探测未命中，回退内置根 _host/（全组成员落非内置根场景）"
                    );
                }
                fallback
            })
            .ok_or_else(|| PluginError {
                message: format!(
                    "light 合宿宿主目录 {GROUP_HOST_DIR}/ 未定位到（从成员 {members:?} 插件目录向上探测均未命中）——\
                     host.py 由合宿宿主侧任务承载，请确认 plugins/shared/_host/ 已就位"
                ),
                code: Some("HOST_DIR_NOT_FOUND".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;
        // 共享 venv 解释器（fail-closed，与 resolve_sidecar_command 的 uv 单轨同轨）。
        let interpreter = find_venv_interpreter(&host_dir).ok_or_else(|| PluginError {
            message: format!(
                "light 合宿宿主 {} 的共享 venv 解释器缺失（探测 {}/.venv/Scripts/python.exe 与 \
                 .venv/bin/python 均不存在）——共享 venv（plugins/shared/_host/.venv）由宿主侧任务构建",
                host_dir.display(),
                GROUP_HOST_DIR
            ),
            code: Some("HOST_VENV_MISSING".to_string()),
            source: Some("plugin-invoker".to_string()),
        })?;
        let mut sorted_members = members.to_vec();
        sorted_members.sort();
        let args = vec![
            "host.py".to_string(),
            "--group".to_string(),
            group.to_string(),
            "--slot".to_string(),
            slot.to_string(),
            "--members".to_string(),
            sorted_members.join(","),
        ];
        Ok((
            interpreter.to_string_lossy().into_owned(),
            args,
            host_dir.to_string_lossy().into_owned(),
        ))
    }

    /// 合宿宿主指纹：当前成员指纹并集 + 成员集本身（§4.5 第 4 条/§4.6）。
    ///
    /// 成员集排序后逐个并入（plugin_id + 各自目录指纹）；任一成员代码/配置
    /// 变更（单成员指纹变）或成员集变化（新成员加入/移出）都会使宿主指纹变化，
    /// 触发 kill 整宿主 respawn。成员 manifest 不可得（插件已被移除）按指纹 0
    /// 纳入（对齐 resolve_fingerprint 拿不到目录返回 0 的哲学）。
    fn host_union_fingerprint(&self, host_key: &str) -> u64 {
        use std::collections::hash_map::DefaultHasher;
        use std::hash::Hasher;
        let mut hasher = DefaultHasher::new();
        hasher.write(host_key.as_bytes());
        for pid in self.host_members(host_key) {
            let fp = match self.loader.get_manifest(&pid) {
                Some(manifest) => self.resolve_fingerprint(&manifest),
                None => 0,
            };
            hasher.write(pid.as_bytes());
            hasher.write(b"|");
            hasher.write(&fp.to_le_bytes());
        }
        hasher.finish()
    }

    /// 宿主过期检测（Pull 热加载，TTL 门 + 指纹比对——宿主键粒度）。
    ///
    /// 独占宿主指纹 = 插件自身指纹（现状语义）；light 宿主 = 成员指纹并集。
    /// 首次调用（缓存无记录）写入指纹返回 false（首次走 spawn，必是最新）。
    ///
    /// light 宿主额外做**成员集漂移检测**（[`Self::light_host_members_drifted`]）：
    /// 新成员惰性装箱进已存活宿主后，进程成员集（spawn 时定格）与分配表漂移，
    /// 必须 kill 整宿主 respawn——否则复用旧进程会报 MCP [-32602] tool not found。
    /// 漂移检测在 TTL 门**之前**（纯内存比对，无 stat 开销；指纹记录的是分配表
    /// 期望态，漂移对指纹不可见，靠指纹过期检测会漏）。
    async fn is_host_stale(&self, host_key: &str, caller: &PluginManifest) -> bool {
        if self.light_host_members_drifted(host_key) {
            return true;
        }
        let now = Instant::now();
        // TTL 门 + 指纹比对在同一把读锁下原子完成（计算指纹前先快照缓存）。
        let cached = self.fingerprints.read().get(host_key).cloned();
        let current_fp = |hk: &str| {
            if parse_group_slot(hk).is_some() {
                self.host_union_fingerprint(hk)
            } else {
                self.resolve_fingerprint(caller)
            }
        };
        match cached {
            None => {
                // 首次：写入指纹，不算过期（此时进程刚 spawn，必是最新）
                let fp = current_fp(host_key);
                self.fingerprints
                    .write()
                    .insert(host_key.to_string(), (fp, now));
                false
            }
            Some((old_fp, last_check)) => {
                // TTL 门：未到期直接复用
                if now.duration_since(last_check) < PLUGIN_FINGERPRINT_TTL {
                    return false;
                }
                // TTL 过期：算当前指纹比对
                let new_fp = current_fp(host_key);
                let stale = new_fp != old_fp;
                // 无论是否过期都刷新检测时刻；指纹变了才更新缓存指纹
                let to_store = if stale { new_fp } else { old_fp };
                self.fingerprints
                    .write()
                    .insert(host_key.to_string(), (to_store, now));
                stale
            }
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

    /// 刷新成员的最后调用时刻（调用即"活跃"，重置空闲计时——成员粒度记账）。
    ///
    /// 成员粒度卸载（ADR 2026-09-24-member-granularity-unload-mode-panel-warmup）
    /// 下每个成员按自己的调用时钟独立判定空闲，不再整组续命。
    fn touch_member_last_used(&self, plugin_id: &str) {
        self.member_last_used
            .write()
            .insert(plugin_id.to_string(), Instant::now());
    }

    /// 进入一次 capability 调用：宿主 in-flight 计数 +1，返回 RAII guard。
    ///
    /// guard drop（成功/失败/超时/panic）时计数 -1（P12，ADR 2026-09-07）。
    fn enter_inflight(&self, host_key: &str) -> InflightGuard {
        let counter = {
            let mut map = self.inflight_calls.write();
            map.entry(host_key.to_string())
                .or_insert_with(|| Arc::new(AtomicUsize::new(0)))
                .clone()
        };
        counter.fetch_add(1, Ordering::SeqCst);
        InflightGuard { counter }
    }

    /// 进入一次 capability 调用：成员 in-flight 计数 +1（成员粒度 GC 门）。
    fn enter_member_inflight(&self, plugin_id: &str) -> InflightGuard {
        let counter = {
            let mut map = self.member_inflight_calls.write();
            map.entry(plugin_id.to_string())
                .or_insert_with(|| Arc::new(AtomicUsize::new(0)))
                .clone()
        };
        counter.fetch_add(1, Ordering::SeqCst);
        InflightGuard { counter }
    }

    /// sidecar 统一入口 in-flight 计入（P22，ADR 2026-09-07）：
    /// 按 manifest 解析宿主键并 +1（宿主面 + 成员面），返回 (宿主键, 宿主 guard, 成员 guard)。
    ///
    /// [`Self::invoke_pipeline_plugin`] / [`Self::invoke_tool`] 的 Sidecar 分支
    /// 是所有 sidecar capability 调用的必经点（管道步骤 / tool-executor 反向
    /// 调用 / http.handle 分发），计数在入口一次持有、覆盖透明恢复全程；
    /// 单次尝试函数（attempt_sidecar_*）不再各自计数。
    fn enter_sidecar_inflight(
        &self,
        manifest: &PluginManifest,
    ) -> (String, InflightGuard, InflightGuard) {
        let host_key = self.resolve_host_key(manifest);
        let guard = self.enter_inflight(&host_key);
        let member_guard = self.enter_member_inflight(&manifest.id);
        (host_key, guard, member_guard)
    }

    /// 读取宿主当前 in-flight capability 调用数（空闲 GC 回收门，P12）。
    fn host_inflight(&self, host_key: &str) -> usize {
        self.inflight_calls
            .read()
            .get(host_key)
            .map(|c| c.load(Ordering::SeqCst))
            .unwrap_or(0)
    }

    /// 读取成员当前 in-flight capability 调用数（成员粒度 GC 回收门）。
    fn member_inflight(&self, plugin_id: &str) -> usize {
        self.member_inflight_calls
            .read()
            .get(plugin_id)
            .map(|c| c.load(Ordering::SeqCst))
            .unwrap_or(0)
    }

    /// 启动后台空闲软卸载 GC 任务。
    ///
    /// 每 30s 扫描 last_used（宿主键粒度），对空闲超过阈值的宿主整组回收
    /// （sidecar kill 进程，manifest 描述保留，下次调用重新 spawn）。
    /// 预热集（keep-warm 常驻集，见 [`Self::warmup_sidecar`]）成员豁免回收
    /// ——预热常驻语义，否则预热被架空、首条消息重新全价冷启动。
    /// 对齐 trait 文档声明的「空闲超时自动卸载」设计原则。
    ///
    /// 必须用 Arc<Self> 调用（后台任务需 'static 持有 invoker）。在 main 启动期调一次。
    pub fn start_idle_gc(self: &Arc<Self>) {
        let invoker = Arc::clone(self);
        tokio::spawn(async move {
            // 扫描间隔：30s。比默认 300s 阈值短得多，保证空闲宿主能在阈值后一个周期内被回收。
            let mut interval = tokio::time::interval(Duration::from_secs(30));
            interval.tick().await; // 跳过立即触发的第一次
            loop {
                interval.tick().await;
                invoker.run_idle_gc_pass().await;
            }
        });
        info!("Plugin idle-unload GC task started (scan every 30s)");
    }

    /// 单次 GC 扫描：按成员粒度回收空闲超时的插件（成员级卸载，ADR
    /// 2026-09-24-member-granularity-unload-mode-panel-warmup 取代原 §4.8 整组
    /// 回收的记账面）。
    ///
    /// - 非最后成员：进程内摘除（`unload_group_member`），宿主与其余成员不动；
    /// - 独占宿主 / 组内最后成员：走既有整组回收（kill 进程 + 槽位释放），
    ///   进程级清理兜底不变。
    async fn run_idle_gc_pass(&self) {
        // 快照当前所有"活跃"成员 id，避免长时间持锁
        let candidates: Vec<String> = {
            let mut keys: Vec<String> = self.member_last_used.read().keys().cloned().collect();
            keys.sort();
            keys.dedup();
            keys
        };

        let now = Instant::now();
        for plugin_id in candidates {
            // 预热常驻豁免（成员粒度）：属于预热集的成员自己不回收，其余成员
            // 照常回收——常驻不再连坐同宿主插件；显式卸载（force_unload）不受
            // 此豁免约束。
            if self.keep_warm_plugins.read().contains(&plugin_id) {
                continue;
            }
            let idle_secs = self
                .member_last_used
                .read()
                .get(&plugin_id)
                .map(|t| now.duration_since(*t).as_secs())
                .unwrap_or(0);
            if idle_secs == 0 {
                continue;
            }
            let threshold = self.idle_timeout_secs_sync(&plugin_id);
            // threshold == 0 表示该成员持久保活（声明"永不空闲卸载"，如模式面板包）。
            if threshold == 0 || idle_secs <= threshold {
                continue;
            }
            let Some(host_key) = self.existing_host_key_for(&plugin_id) else {
                // 无宿主（从未 spawn / InProcess / 已卸载）：清掉陈旧记账。
                self.member_last_used.write().remove(&plugin_id);
                continue;
            };
            // 软卸载 vs 整组回收的判定口径 = **spawn 快照**（进程实际服务面），
            // 不是分配表：成员粒度软卸载保留分配条目（待重加入标记，见
            // finalize_member_unload），表长含「进程已卸、待下次 respawn 回归」
            // 的成员——按表长判定会把「进程内最后一名活跃成员」误走软卸载路径
            // （宿主协议拒绝卸最后成员 → GC 只 warn 不回收，宿主永不释放）。
            // 快照缺失（进程服务面未知）回退分配表口径：生产路径 spawn 先写
            // 快照后入缓存（BUG-44 不变量），缺省仅为异常态兜底，与既有行为
            // 一致。
            let snapshot_len = match self.spawned_members.read().get(&host_key).cloned() {
                Some(spawned) => spawned.len(),
                None => self.host_members(&host_key).len(),
            };
            if snapshot_len > 1 {
                // 成员有调用在飞不卸（同 P12 门：空闲计时在长调用期间会虚假超期）。
                let member_inflight = self.member_inflight(&plugin_id);
                if member_inflight > 0 {
                    info!(
                        plugin = %plugin_id,
                        host = %host_key,
                        inflight = member_inflight,
                        idle_secs = idle_secs,
                        threshold = threshold,
                        "Member idle-unload skipped: capability calls in flight"
                    );
                    continue;
                }
                info!(
                    plugin = %plugin_id,
                    host = %host_key,
                    idle_secs = idle_secs,
                    threshold = threshold,
                    "Member idle-unloading (soft): exceeds idle timeout"
                );
                if let Err(e) = self.unload_group_member(&plugin_id).await {
                    warn!("idle GC member unload {} failed: {}", plugin_id, e);
                }
                continue;
            }
            // 独占宿主 / 组内最后成员：整组回收（kill 进程 + 槽位释放）。
            // in-flight 门（P12，ADR 2026-09-07）：有调用在飞不回收。
            let inflight = self.host_inflight(&host_key);
            if inflight > 0 {
                info!(
                    host = %host_key,
                    members = ?self.host_members(&host_key),
                    inflight = inflight,
                    idle_secs = idle_secs,
                    threshold = threshold,
                    "Host idle-unload skipped: capability calls in flight"
                );
                continue;
            }
            info!(
                host = %host_key,
                members = ?self.host_members(&host_key),
                idle_secs = idle_secs,
                threshold = threshold,
                "Host idle-unloading (soft): exceeds idle timeout"
            );
            if let Err(e) = self.unload_host(&host_key, true).await {
                warn!("idle GC unload {} failed: {}", host_key, e);
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
    ///
    /// 宿主语义（合宿进程模型 §4.2）：kill 插件所在**宿主**——light 成员连坐整组
    /// （组指纹变化触发整组 respawn 的对称面），独占成员杀单进程。分配表保留
    /// （respawn 按当前表重建成员集，§4.5 第 2 条分配粘性），槽位回收只在
    /// idle GC 路径发生。
    pub async fn force_unload_impl(&self, plugin_id: &str) -> Result<(), PluginError> {
        // 成员粒度优先（ADR 2026-09-24-member-granularity-unload-mode-panel-warmup）：
        // 存活组宿主的非最后成员先试进程内摘除；失败/不适用回退整组驱逐
        // （语义终点一致：下个调用者拿新码，爆炸半径收缩）。
        if let Some(host_key) = self.existing_host_key_for(plugin_id) {
            if parse_group_slot(&host_key).is_some()
                && self.mcp_clients.read().contains_key(&host_key)
                && self
                    .spawned_members
                    .read()
                    .get(&host_key)
                    .is_some_and(|members| !members.iter().any(|m| m == plugin_id))
            {
                // 软卸载待重加入成员（分配在、宿主存活、快照已知且不含它）：
                // 无进程内服务面可摘，清掉分配标记即完成强制语义（该成员不随
                // 下次 respawn 回归），与「无宿主」路径同一终点——不得为它整组
                // kill。快照未知（None）不进此分支：进程服务面不明按既有
                // fail-closed 路径处理（BUG-44 同哲学）。
                self.light_packing.write().assignments.remove(plugin_id);
                self.emit_lifecycle_unload(plugin_id);
                if let Err(e) = self.loader.unload(plugin_id).await {
                    warn!(
                        "loader unload {} (force unload pending-rejoin member) failed: {}",
                        plugin_id, e
                    );
                }
                return Ok(());
            }
            let group_with_peers =
                parse_group_slot(&host_key).is_some() && self.host_members(&host_key).len() > 1;
            if group_with_peers && self.mcp_clients.read().contains_key(&host_key) {
                match self.unload_group_member(plugin_id).await {
                    Ok(()) => {
                        // 强制语义：撤销「待重加入」分配标记（软卸载保留分配是
                        // idle 语境——成员随下次 respawn 回归；强制卸载要求它
                        // 不再自动回归，见 finalize_member_unload）。
                        self.light_packing.write().assignments.remove(plugin_id);
                        return Ok(());
                    }
                    Err(e) => {
                        warn!(
                            "unload_member fallback to whole-host unload for {}: {}",
                            plugin_id, e
                        );
                    }
                }
            }
        }
        self.unload_plugin_host(plugin_id, false).await
    }

    /// 成员粒度热重载实现：向成员所在组宿主发 `agentos/reload_member` 请求
    /// （带应答），宿主进程内重载该成员代码，其余成员进程不动——驱逐爆炸
    /// 半径从整组收缩到单成员（BUG-3 504 的根治面）。
    ///
    /// 三路判定（与 [`PluginInvoker::reload_member`] 契约一致）：
    /// - 独占宿主 → Err（调用方回退 force_unload，单进程杀即等价粒度）；
    /// - 无存活进程（未装箱/未 spawn/已驱逐）→ Ok(no-op)：无进程内代码，
    ///   下次 spawn 必用新码；
    /// - 存活组宿主 → 应答成功 Ok；宿主协议错误/超时 Err（重载失败不破坏
    ///   宿主状态——旧成员仍在服务，回退整组驱逐路径安全）。
    ///
    /// 成功后刷新宿主并集指纹记账：指纹是 spawn 时刻快照，进程内代码已被
    /// 重载为磁盘现码——不刷新会让 pull 路径（is_host_stale）按指纹差误判
    /// stale 而整组 respawn，热重载被架空。
    pub async fn reload_group_member(&self, plugin_id: &str) -> Result<(), PluginError> {
        let Some(host_key) = self.existing_host_key_for(plugin_id) else {
            return Ok(());
        };
        if parse_group_slot(&host_key).is_none() {
            return Err(PluginError {
                message: format!("reload_member: {plugin_id} 不在合宿组宿主（独占无进程内重载面）"),
                code: None,
                source: None,
            });
        }
        let Some(client) = self.mcp_clients.read().get(&host_key).cloned() else {
            return Ok(());
        };
        let params = json!({ "plugin_id": plugin_id });
        let outcome = tokio::time::timeout(RELOAD_MEMBER_TIMEOUT, async move {
            client
                .read()
                .await
                .request(RELOAD_MEMBER_METHOD, params)
                .await
        })
        .await;
        match outcome {
            Ok(Ok(result)) => {
                if result.get("reloaded").and_then(Value::as_bool) != Some(true) {
                    return Err(PluginError {
                        message: format!(
                            "reload_member: 宿主应答缺少 reloaded=true（{plugin_id}）"
                        ),
                        code: None,
                        source: None,
                    });
                }
                let fp = self.host_union_fingerprint(&host_key);
                self.fingerprints
                    .write()
                    .insert(host_key.clone(), (fp, Instant::now()));
                info!(
                    plugin_id = plugin_id,
                    host = %host_key,
                    "成员粒度热重载完成（宿主进程内重载，其余成员不受影响；并集指纹已刷新）"
                );
                Ok(())
            }
            Ok(Err(e)) => Err(PluginError {
                message: format!("reload_member: 宿主应答错误（{plugin_id}）: {e}"),
                code: None,
                source: None,
            }),
            Err(_) => Err(PluginError {
                message: format!(
                    "reload_member: 宿主应答超时（{plugin_id}，{}s）",
                    RELOAD_MEMBER_TIMEOUT.as_secs()
                ),
                code: None,
                source: None,
            }),
        }
    }

    /// 成员粒度卸载实现：向成员所在组宿主发 `agentos/unload_member` 请求
    /// （带应答），宿主进程内摘除该成员（定向 on_unload + 三张聚合表原位
    /// 重聚合 + 模块缓存摘除），其余成员进程不动——卸载爆炸半径从整组收缩到
    /// 单成员（ADR 2026-09-24-member-granularity-unload-mode-panel-warmup）。
    ///
    /// 三路判定（与 [`PluginInvoker::reload_member`] 契约同构）：
    /// - 独占宿主 / 组内最后成员 → Err（调用方回退整组路径：独占单进程杀即
    ///   等价粒度；最后成员卸载 = 进程级回收，空成员集破坏合宿不变量）；
    /// - 无存活进程（未装箱/未 spawn/已驱逐）→ Ok(no-op)：无进程内服务面，
    ///   记账由调用方按需清理；
    /// - 存活组宿主 → 应答成功 Ok 后完成内核侧记账收尾（见
    ///   [`Self::finalize_member_unload`]）；宿主协议错误/超时 Err——宿主状态
    ///   未被破坏（成员仍在服务），回退整组驱逐路径安全。
    pub async fn unload_group_member(&self, plugin_id: &str) -> Result<(), PluginError> {
        let Some(host_key) = self.existing_host_key_for(plugin_id) else {
            return Ok(());
        };
        if parse_group_slot(&host_key).is_none() {
            return Err(PluginError {
                message: format!("unload_member: {plugin_id} 不在合宿组宿主（独占无进程内卸载面）"),
                code: None,
                source: None,
            });
        }
        // 最后成员判定口径 = **spawn 快照**（进程实际服务面）：软卸载保留分配
        // 条目（待重加入标记，见 finalize_member_unload），按分配表长判定会被
        // 待重加入成员虚胖——卸掉进程内最后一名服务成员会得到空成员集进程，
        // 破坏合宿不变量（host.py 侧同样拒绝，双保险同口径）。
        let process_members = self
            .spawned_members
            .read()
            .get(&host_key)
            .cloned()
            .unwrap_or_else(|| self.host_members(&host_key));
        if process_members.len() <= 1 {
            return Err(PluginError {
                message: format!(
                    "unload_member: {plugin_id} 是宿主 {host_key} 的最后成员（须走整组回收）"
                ),
                code: None,
                source: None,
            });
        }
        let Some(client) = self.mcp_clients.read().get(&host_key).cloned() else {
            return Ok(());
        };
        let params = json!({ "plugin_id": plugin_id });
        let outcome = tokio::time::timeout(RELOAD_MEMBER_TIMEOUT, async move {
            client
                .read()
                .await
                .request(UNLOAD_MEMBER_METHOD, params)
                .await
        })
        .await;
        match outcome {
            Ok(Ok(result)) => {
                if result.get("unloaded").and_then(Value::as_bool) != Some(true) {
                    return Err(PluginError {
                        message: format!(
                            "unload_member: 宿主应答缺少 unloaded=true（{plugin_id}）"
                        ),
                        code: None,
                        source: None,
                    });
                }
                self.finalize_member_unload(&host_key, plugin_id).await;
                info!(
                    plugin_id = plugin_id,
                    host = %host_key,
                    "成员粒度卸载完成（宿主进程内摘除，其余成员不受影响；记账与并集指纹已刷新）"
                );
                Ok(())
            }
            Ok(Err(e)) => Err(PluginError {
                message: format!("unload_member: 宿主应答错误（{plugin_id}）: {e}"),
                code: None,
                source: None,
            }),
            Err(_) => Err(PluginError {
                message: format!(
                    "unload_member: 宿主应答超时（{plugin_id}，{}s）",
                    RELOAD_MEMBER_TIMEOUT.as_secs()
                ),
                code: None,
                source: None,
            }),
        }
    }

    /// 成员卸载成功后的内核侧记账收尾（请求应答成功后调用，恰一次）。
    ///
    /// - OnUnload 旁路广播 + loader 卸载（能力/路由登记面摘除）；
    /// - `spawned_members` 快照摘除该成员（进程服务面已收缩）；**分配表条目保留**
    ///   ——保留即「待重加入」标记：漂移检测（分配表 ≠ 快照）会在该宿主下一次
    ///   被调用时触发整宿主 respawn，按分配表重建成员集，把全部软卸载成员**一次
    ///   性**带回进程。若像旧版那样同摘分配条目，每个被卸成员都要各自重装箱 →
    ///   各自触发一次整宿主 respawn——装机版 2026-09-25 实测：GC 每轮软卸载
    ///   15-20 名成员，下个繁忙期逐成员重装箱引发分钟级重载风暴（宿主重载
    ///   风暴 → 插件 http 端点 504），k 次整杀收敛为 1 次；
    /// - 清成员空闲时钟与成员 in-flight 条目；刷新宿主并集指纹（分配表未变，
    ///   刷新后与旧值一致——指纹是 spawn 期望态，不因软卸载漂移）。
    async fn finalize_member_unload(&self, host_key: &str, plugin_id: &str) {
        self.emit_lifecycle_unload(plugin_id);
        if let Err(e) = self.loader.unload(plugin_id).await {
            warn!(
                "loader unload {} (member unload from {}) failed: {}",
                plugin_id, host_key, e
            );
        }
        if let Some(members) = self.spawned_members.write().get_mut(host_key) {
            members.retain(|m| m != plugin_id);
        }
        self.member_last_used.write().remove(plugin_id);
        self.member_inflight_calls.write().remove(plugin_id);
        let fp = self.host_union_fingerprint(host_key);
        self.fingerprints
            .write()
            .insert(host_key.to_string(), (fp, Instant::now()));
    }

    /// 按插件 id 卸载其宿主（idle GC 与 force_unload 的公共实现）。
    ///
    /// `reclaim_assignments`：idle 语境（GC 回收）为 true——连同清掉分配表内
    /// 该宿主的全部成员条目，槽位释放供后续装箱复用（§4.8「回收即清空」）；
    /// 热重载/崩溃恢复语境为 false——保留分配表，respawn 按表重建成员集。
    async fn unload_plugin_host(
        &self,
        plugin_id: &str,
        reclaim_assignments: bool,
    ) -> Result<(), PluginError> {
        match self.existing_host_key_for(plugin_id) {
            Some(host_key) => self.unload_host(&host_key, reclaim_assignments).await,
            None => {
                // 无宿主（light 从未分配 / InProcess / manifest 已不可得且无分配）：
                // 无进程可杀，仅做 loader 侧卸载 + OnUnload 旁路广播（与无缓存路径
                // 的既有语义对齐）。
                self.emit_lifecycle_unload(plugin_id);
                if let Err(e) = self.loader.unload(plugin_id).await {
                    warn!("loader unload {} (no-host path) failed: {}", plugin_id, e);
                }
                Ok(())
            }
        }
    }

    /// 卸载宿主：kill 进程 + 清缓存/指纹/last_used（记账粒度全部按宿主键）。
    ///
    /// 宿主是进程所有权单位（方案 §〇）：合宿宿主的 kill 连坐全部成员——
    /// OnUnload 旁路广播与 loader.unload 逐成员执行；分配表条目按
    /// `reclaim_assignments` 决定是否释放（见 [`Self::unload_plugin_host`]）。
    /// kill 段（取写锁 → on_unload → kill）限时 2s：在飞调用持读锁时放弃
    /// kill 照常完成驱逐与记账（残留进程由 kill_on_drop 兜底），GC 单循环
    /// 不因单个宿主阻塞。
    async fn unload_host(
        &self,
        host_key: &str,
        reclaim_assignments: bool,
    ) -> Result<(), PluginError> {
        let members = self.host_members(host_key);

        // 旁路广播 OnUnload（杀进程/卸载之前，逐成员）。与 get_or_create_mcp_client
        // 里的 OnLoad emit 对称：观察层（审计日志 / `lifecycle.plugin_*` 指标）关注
        // "插件进程即将被卸载"这一事实。best-effort、非阻塞；未注入总线（`None`，
        // 如单测）时 no-op。
        for member in &members {
            self.emit_lifecycle_unload(member);
        }

        let client_arc = {
            let mut clients = self.mcp_clients.write();
            clients.remove(host_key)
        };

        if let Some(client_arc) = client_arc {
            // 卸载尾段（取写锁 → on_unload 通知 → kill）限时 2s：长流式调用
            // 持读锁时，无超时写锁会让 GC 单循环在此等数小时（全局空闲回收
            // 瘫痪）。超时放弃本次 kill——缓存条目已驱逐、记账清理由下方照常
            // 完成（下次调用 respawn 新实例），残留进程由 kill_on_drop 在最后
            // 一个 Arc 释放时兜底终止。先例：shutdown_all / kill_sidecar_if_any
            // 的逐 kill 2s 超时。
            let teardown = async {
                let mut client = client_arc.write().await;
                // 镜像 OnLoad 的 notifications/on_load：杀进程之前发 on_unload 给宿主一个
                // 收尾机会（fire-and-forget，不等响应）。失败仅 warn 不阻断——进程可能已崩溃
                // 或不响应该通知，该杀仍杀（卸载语义不变）。
                let _ = client
                    .send_notification("notifications/on_unload", None)
                    .await
                    .inspect_err(|e| {
                        warn!("on_unload notification failed for {}: {}", host_key, e)
                    });
                if let Err(e) = client.kill().await {
                    warn!("Failed to kill host {}: {}", host_key, e);
                }
            };
            if tokio::time::timeout(Duration::from_secs(2), teardown)
                .await
                .is_err()
            {
                warn!(
                    "unload_host: teardown of {} timed out (2s, write lock held by in-flight caller); \
                     host evicted, residual process left to kill_on_drop backstop",
                    host_key
                );
            }
        }

        // 也通过 loader 卸载（宿主粒度：合宿组连坐全部成员）
        for member in &members {
            if let Err(e) = self.loader.unload(member).await {
                warn!(
                    "loader unload {} (host {} teardown) failed: {}",
                    member, host_key, e
                );
            }
        }

        // 清除指纹缓存 + 成员记账（空闲时钟/in-flight 按成员键），下次调用重新计算并 respawn
        self.fingerprints.write().remove(host_key);
        {
            let mut last_used = self.member_last_used.write();
            let mut member_inflight = self.member_inflight_calls.write();
            for member in &members {
                last_used.remove(member);
                member_inflight.remove(member);
            }
        }
        // 清除 spawn 成员集快照（与 mcp_clients 条目同生命周期，见字段注释）
        self.spawned_members.write().remove(host_key);
        // 清除 in-flight 计数条目（与 mcp_clients 同生命周期；在飞 guard 持有
        // Arc 克隆，减数不依赖条目存活，见 inflight_calls 字段注释）
        self.inflight_calls.write().remove(host_key);

        // idle GC 回收语境：清分配表内该宿主的全部成员条目——槽位全部释放，
        // 后续新插件装箱时优先复用（§4.5 第 3 条 / §4.8「回收即清空」）。
        if reclaim_assignments {
            self.light_packing
                .write()
                .assignments
                .retain(|_, hk| hk.as_str() != host_key);
        }

        info!(
            "Force unloaded host: {} (members: {:?}, reclaim_slots: {})",
            host_key, members, reclaim_assignments
        );
        Ok(())
    }

    /// 旁路广播单成员 OnUnload（unload_host 逐成员调用）。
    fn emit_lifecycle_unload(&self, plugin_id: &str) {
        let bus_guard = self.hook_bus.read();
        let Some(bus) = bus_guard.as_ref() else {
            return;
        };
        let mut ctx = HookContext::new();
        ctx.set("plugin_id", json!(plugin_id));
        bus.emit(LifecycleEvent {
            hook: LifecycleHook::OnUnload,
            ctx,
            target: EventTarget::Plugin(plugin_id.to_string()),
            ts: SystemTime::now(),
        });
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
    async fn invoke_pipeline_plugin<'a>(
        &self,
        plugin_id: &str,
        ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        let loaded = self.loader.load(plugin_id).await?;
        let manifest = &loaded.manifest;

        // P2-2 插件权限声明前置日志校验（不阻断）
        self.check_permissions(plugin_id, manifest);

        let result = match manifest.host_type {
            HostType::InProcess => {
                // task_11 N2：经 NativePluginLoader 加载 cdylib 并通过 C-ABI 调用。
                // config 注入与 sidecar 同源（shared::injected_config）——两轨对齐。
                self.invoke_native_pipeline(plugin_id, manifest, ctx).await
            }
            HostType::Sidecar => {
                // in-flight 计入（P22 统一入口）：本 trait 方法是所有 sidecar
                // pipeline 调用的必经点（引擎管道步骤 / 直调），计数在此 +1，
                // RAII guard 跨越透明恢复的 respawn+重试全程（成功/失败/超时/
                // panic 随 future drop 回落），空闲 GC 不得回收本宿主。
                let (_host_key, _inflight, _member_inflight) =
                    self.enter_sidecar_inflight(manifest);
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

        // 旁路广播 OnError：插件调用失败即在此中央返回处 emit 一次（审计日志 /
        // `lifecycle.plugin_error_total` 计数）。失败 = Err（进程崩溃/MCP/解析）
        // **或** Ok 携带失败结果（error 字段）——业务失败若不计，计数器恒零。
        // best-effort、非阻塞；bus=None 时 no-op。**不改错误处理语义**——原
        // Err 照常向上传播。
        if let Some((message, code)) = pipeline_error_params(&result) {
            self.emit_lifecycle_error(plugin_id, &message, code.as_deref());
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

        // 非流式调用总时长超时（P12，ADR 2026-09-07）：整体包界（含冷启动
        // spawn），到点放弃内层 future 返回结构化 CAPABILITY_TIMEOUT，in-flight
        // 计数随 drop 回落。流式豁免：流式走 invoke_pipeline_plugin（event-bus
        // 旁路 chunk），该路径不包界——流式语义是长连接，不做总时长熔断。
        // （P22 勘误：经 tool-executor 反向调用的流式工具如 llm_service
        // llm.complete_stream 实际也流经本方法——其等待界 = 插件自声明
        // `mcp.request_timeout_secs`（内核界与传输层界同源同值，行为与 P12 前
        // 一致）；需要超越插件声明的流式工具经 `timeout_ms: Some(0)` 声明豁免。）
        let result = with_capability_timeout(
            tool_timeout_ms(manifest, tool_name),
            plugin_id,
            tool_name,
            async {
                match manifest.host_type {
                    HostType::InProcess => {
                        // task_11 N2：原生工具插件——inputs 作为 state，config 同 pipeline 路径注入。
                        // B2：经 execute 的 tool_call 约定字段表达工具语义（旧 pipeline 插件零破坏）。
                        self.invoke_native_tool(plugin_id, manifest, tool_name, inputs)
                            .await
                    }
                    HostType::Sidecar => {
                        // in-flight 计入（P22 统一入口，包在超时 future 内——超时放弃
                        // 内层 future 时 guard 随 drop 回落）：本 trait 方法是所有
                        // sidecar 工具调用的必经点（tool-executor 反向调用含
                        // llm.complete_stream 流式、http.handle 分发），计数覆盖
                        // 透明恢复的 respawn+重试全程；流式调用的 MCP 响应在流
                        // 完全结束才返回，guard 即覆盖整个流生命周期。
                        let (_host_key, _inflight, _member_inflight) =
                            self.enter_sidecar_inflight(manifest);
                        // B1（M2-reactive 第一刀）：sidecar 死亡透明恢复——与 pipeline 路径同构。
                        self.with_transparent_recovery(plugin_id, || async {
                            self.attempt_sidecar_tool(plugin_id, manifest, tool_name, inputs)
                                .await
                        })
                        .await
                    }
                }
            },
        )
        .await;

        // 旁路广播 OnError：工具插件调用失败即在此中央返回处 emit 一次（审计日志 /
        // `lifecycle.plugin_error_total` 计数）。失败 = Err（进程崩溃/MCP/解析）
        // **或** Ok 携带 success=false（isError/失败信封）——业务失败若不计，
        // 计数器恒零。best-effort、非阻塞；bus=None 时 no-op。**不改错误处理
        // 语义**——原 Err 照常向上传播。
        if let Some((message, code)) = tool_error_params(&result) {
            self.emit_lifecycle_error(plugin_id, &message, code.as_deref());
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
                // Sidecar：经 MCP notification 发送（fire-and-forget）。客户端获取/
                // 创建失败同样仅 warn——对齐下方 InProcess 分支与 fire-and-forget
                // 语义（不阻断管道），但失败必须留痕、不得静默。
                match self.get_or_create_mcp_client(manifest).await {
                    Ok(client_arc) => {
                        let client = client_arc.read().await;
                        if client.is_alive().await {
                            let hook_method = format!("notifications/{hook_name}");
                            if let Err(e) = client.send_notification(&hook_method, Some(tags)).await
                            {
                                warn!("Lifecycle notification failed for {}: {}", plugin_id, e);
                            }
                        }
                    }
                    Err(e) => {
                        warn!(
                            "Lifecycle hook {hook_name} not delivered for {}: {}",
                            plugin_id, e
                        );
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

    /// 宿主键解析（trait 默认按独占语义返回 plugin_id；此处交真实装箱表）。
    ///
    /// light 合宿成员经装箱分配表拿组键（粘性分配，与 spawn 路由同一把尺子），
    /// 域事件广播据此把同宿主订阅插件去重为一次投递；manifest 缺席按独占
    /// 语义回退（不抛错——广播是 fire-and-forget 面，缺席成员由 send 侧留痕）。
    fn host_key_of(&self, plugin_id: &str) -> String {
        match self.loader.get_manifest(plugin_id) {
            Some(manifest) => self.resolve_host_key(&manifest),
            None => plugin_id.to_string(),
        }
    }

    /// 强制卸载插件（覆盖 trait 默认实现）。
    /// 转发到 force_unload_impl：kill sidecar + 清缓存，下次调用自动 respawn 加载新代码。
    async fn force_unload(&self, plugin_id: &str) -> Result<(), PluginError> {
        self.force_unload_impl(plugin_id).await
    }

    /// 成员粒度热重载（覆盖 trait 默认实现，合宿多组扩展的姊妹能力）。
    ///
    /// 见 [`Self::reload_group_member`]：仅对「已装箱到组宿主且有存活进程」
    /// 的成员生效；独占插件返回 Err（独占本就无连坐面，调用方回退
    /// [`Self::force_unload`] 杀单进程即等价粒度）。
    async fn reload_member(&self, plugin_id: &str) -> Result<(), PluginError> {
        self.reload_group_member(plugin_id).await
    }

    async fn unload_member(&self, plugin_id: &str) -> Result<(), PluginError> {
        self.unload_group_member(plugin_id).await
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
        // 新 spawn 判定：校验前缓存里没有该插件所在宿主的存活连接（本次会 spawn 新进程）
        let host_key_probe = self.resolve_host_key(manifest);
        let was_new = {
            let cached = self.mcp_clients.read().get(&host_key_probe).cloned();
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
        // 本次新 spawn 的宿主进程：校验完回收（kill + 移除缓存），懒加载语义不被
        // 破坏（宿主粒度——light 探测会 spawn 整组宿主，校验完连同整组回收）。
        // 例外（2026-08-20）：声明了 lifecycle_hooks 的插件 on_load 有副作用
        // （起子进程/绑端口——hindsight-api 占 8420 即实测案例），探测完 kill 会
        // 把初始化成果毁掉且孤儿子进程占端口 → 此类插件不 kill，进程交 idle GC
        // 空闲回收。
        if was_new && manifest.capabilities.lifecycle_hooks.is_empty() {
            // 回收 kill（含写锁获取）限时 2s：并发在飞调用持读锁时无超时写锁
            // 会让本请求无限悬挂。超时 warn 并返回明确错误（缓存条目保留——
            // 宿主是合法已初始化实例，后续调用照常复用，仅本次未回收）。
            let kill_fut = async { client.write().await.kill().await };
            match tokio::time::timeout(Duration::from_secs(2), kill_fut).await {
                Ok(Ok(())) => {}
                Ok(Err(e)) => {
                    tracing::debug!(
                        "G2 verify: best-effort kill of freshly spawned host {} failed (idle GC will reap): {e}",
                        host_key_probe
                    );
                }
                Err(_) => {
                    warn!(
                        "G2 verify: reclaim kill of freshly spawned host {} timed out (2s), returning error",
                        host_key_probe
                    );
                    return Err(PluginError {
                        message: format!(
                            "G2 verify: reclaim kill of freshly spawned host {host_key_probe} timed out (2s) — write lock held by in-flight caller"
                        ),
                        code: Some("HOST_RECLAIM_TIMEOUT".to_string()),
                        source: Some("plugin-invoker".to_string()),
                    });
                }
            }
            self.mcp_clients.write().remove(&host_key_probe);
            // spawn 成员集快照与缓存条目同生命周期：回收即清（下次调用重记）
            self.spawned_members.write().remove(&host_key_probe);
        }
        Ok(raw)
    }

    /// 内核停机口（覆盖 trait 默认 no-op）：drain + 逐个 kill 全部缓存 sidecar。
    ///
    /// 见 inherent [`PluginInvokerImpl::shutdown_all`]（0.2 收尾 §3.3a）。
    async fn shutdown_all(&self) {
        PluginInvokerImpl::shutdown_all(self).await
    }

    /// disable 窄口 kill（覆盖 trait 默认 no-op）。
    ///
    /// 见 inherent [`PluginInvokerImpl::kill_sidecar_if_any`]（0.2 收尾 §3.3b）。
    async fn kill_sidecar_if_any(&self, plugin_id: &str) {
        PluginInvokerImpl::kill_sidecar_if_any(self, plugin_id).await
    }

    /// 目标插件所在宿主是否在 spawn/respawn 窗口（覆盖 trait 默认 false）。
    ///
    /// D1 宽限面：HTTP dispatcher 据此区分「插件处理慢」与「基础设施冷启动
    /// 等待」，后者暂停端点超时预算（等待就绪而非 504，宿主重载风暴
    /// 2026-09-25）。记账见 [`PluginInvokerImpl::starting_plugins`]。
    fn is_host_starting(&self, plugin_id: &str) -> bool {
        self.starting_plugins.read().contains(plugin_id)
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

    /// 递归收集插件 roots：从 AGENTOS_PLUGINS_DIR **与用户插件根**出发，找所有含
    /// plugin.json 的目录，取其父目录去重作为 discover 的 root_paths（对齐 main 的
    /// discover_plugin_roots）。
    ///
    /// 双根都要递归：`scan_root` 只扫一级，`tools/<name>/` 这类二级嵌套靠本函数补
    /// roots。此前只从内置根出发，用户根里放 `tools/foo/` 布局根本不会被发现
    /// （ADR 2026-09-13 §2.3 缺口二）；用户根因此被迫只支持平铺。
    fn collect_plugin_roots(&self) -> Vec<String> {
        let mut base_paths: Vec<std::path::PathBuf> = Vec::new();
        if let Ok(d) = std::env::var("AGENTOS_PLUGINS_DIR") {
            if !d.trim().is_empty() {
                base_paths.push(std::path::PathBuf::from(d));
            }
        }
        if let Some(user_root) = agentos_core::user_space::user_plugins_dir() {
            if !base_paths.contains(&user_root) {
                base_paths.push(user_root);
            }
        }

        let mut parent_set = std::collections::HashSet::new();
        for base_path in &base_paths {
            let mut plugin_dirs: Vec<String> = Vec::new();
            Self::collect_plugin_dirs(base_path, &mut plugin_dirs);
            // 取父目录去重
            for dir in &plugin_dirs {
                if let Some(parent) = std::path::Path::new(dir).parent() {
                    if let Some(s) = parent.to_str() {
                        parent_set.insert(s.to_string());
                    }
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

// ═══════════════════════════════════════════════════════════════════════════
// 监控 M3：宿主进程态快照（api crate 进程态轮询任务的取数面）
// ═══════════════════════════════════════════════════════════════════════════

/// 宿主进程态快照行（一次采集、一个宿主进程；合宿宿主的成员共享该进程）。
#[derive(Debug, Clone)]
pub struct HostProcSnapshot {
    pub host_key: String,
    /// stdio 宿主的 OS 进程号；HTTP transport / 未连接 = None
    pub pid: Option<u32>,
    /// 未判死即 true（判死门控同 is_dead_sidecar；HTTP transport 恒 true）
    pub alive: bool,
    /// 宿主进程运行秒数（spawn 时刻起算；无记账时 None）
    pub uptime_secs: Option<u64>,
    /// 本宿主承载的成员插件 id（合宿宿主 = 多个；轻量组按分配表，独占按宿主键前缀）
    pub plugin_ids: Vec<String>,
}

impl PluginInvokerImpl {
    /// 枚举全部在册宿主的进程态快照（只读：不判死驱逐、不 touch、不 spawn）。
    ///
    /// 锁序纪律：mcp_clients / host_spawned_at 是 parking_lot 锁，先克隆收窄
    /// 立即放锁，再对每个 client 做 tokio 锁内的 async 判死/pid 读取——保证
    /// 返回 future Send（调用方在 tokio::spawn 任务里 await）。
    pub async fn host_proc_snapshots(&self) -> Vec<HostProcSnapshot> {
        let pairs: Vec<(String, SharedMcpClient)> = {
            let clients = self.mcp_clients.read();
            clients
                .iter()
                .map(|(k, v)| (k.clone(), Arc::clone(v)))
                .collect()
        };
        let uptimes: HashMap<String, Option<u64>> = {
            let spawned = self.host_spawned_at.read();
            spawned
                .iter()
                .map(|(k, t)| (k.clone(), Some(t.elapsed().as_secs())))
                .collect()
        };
        let members: HashMap<String, Vec<String>> = pairs
            .iter()
            .map(|(k, _)| (k.clone(), self.host_members(k)))
            .collect();

        let mut out = Vec::with_capacity(pairs.len());
        for (host_key, client) in pairs {
            let guard = client.read().await;
            let alive = !Self::is_dead_sidecar(&guard).await;
            let pid = guard.pid().await;
            drop(guard);
            out.push(HostProcSnapshot {
                uptime_secs: uptimes.get(&host_key).copied().flatten(),
                plugin_ids: members.get(&host_key).cloned().unwrap_or_default(),
                host_key,
                pid,
                alive,
            });
        }
        out
    }
}

#[cfg(test)]
mod tests;

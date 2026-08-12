//! Lingxi AgentOS 0.2 内核二进制入口。
//!
//! 启动 Axum HTTP/WebSocket API 服务器，提供 /health、/api/v1/* 端点和 /ws WebSocket。
//!
//! 集成插件系统：
//! 1. 扫描 plugins/shared/ 目录发现插件 manifest
//! 2. 将工具能力注册到 CapabilityRegistry
//! 3. 初始化管道引擎（AdrEngineImpl）
//! 4. 将所有组件注入 AppState
//!
//! 环境变量：
//! - AGENTOS_KERNEL_PORT：监听端口（默认 9100）
//! - AGENTOS_KERNEL_HOST：监听地址（默认 0.0.0.0）
//! - AGENTOS_PLUGINS_DIR：内置插件根目录（默认 plugins/shared）
//! - LINGXI_USER_PLUGINS_DIR / AGENTOS_USER_PLUGINS_DIR：用户插件根目录（默认 OS 标准目录 agentos/plugins）

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use agentos_api::{
    load_pipeline_config, load_step_library, routes::AppState, start_server,
    validate_no_name_conflicts, KernelCapabilityRouter,
};
use agentos_core::traits::{
    CapabilityRegistry, PluginLoader, PluginType, StorageBackend, ToolDescriptor,
};
use agentos_core::types::{ToolCategory, ToolSource, UserRecord};
use agentos_engine::{AdrEngineImpl, SqliteStore};
use agentos_invoker::PluginInvokerImpl;
use agentos_plugin_loader::{
    CapabilityRegistryImpl, NativePluginLoader, PluginLoaderImpl, WasmRuntime,
};
use tracing::{info, warn};
use tracing_subscriber::{fmt, prelude::*};

/// 播种内置 admin 用户（首次启动插入，已存在则跳过）。
///
/// 0.5.0 最小持久化地基：auth 由硬编码占位改为查 DB，启动时确保 admin 存在。
/// tenant_id = "default"（与多租户隔离地基的默认租户一致），保证旧数据
/// （0.5.0 前以 default 写入的会话/消息）仍归 admin 可见。
async fn seed_admin_user(store: Arc<SqliteStore>) {
    const ADMIN_ID: &str = "00000000-0000-0000-0000-000000000001";
    match store.get_user_by_id(ADMIN_ID).await {
        Ok(Some(_)) => {
            // admin 已存在（非首次启动），跳过
        }
        Ok(None) => {
            // 首次启动：插入 admin 种子用户。
            // get_user_by_id 按 task_local tenant（此处为空→default）查，admin 的
            // tenant_id 正是 default，所以 None 表示确实没播过种。
            let now = chrono::Utc::now().to_rfc3339();
            let admin = UserRecord {
                user_id: ADMIN_ID.to_string(),
                username: "admin".to_string(),
                password: "admin12345".to_string(), // 明文（DEBT: 0.5.0 哈希）
                email: Some("admin@agentos.dev".to_string()),
                role: "admin".to_string(),
                tenant_id: "default".to_string(),
                created_at: now,
                last_login_at: None,
            };
            match store.create_user(&admin).await {
                Ok(()) => info!(target: "agentos-kernel", "已播种内置 admin 用户 (tenant=default)"),
                Err(e) => warn!(target: "agentos-kernel", error = %e, "播种 admin 用户失败（login 将回退内置硬编码）"),
            }
        }
        Err(e) => {
            warn!(target: "agentos-kernel", error = %e, "查询 admin 用户失败，跳过播种")
        }
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // 初始化日志
    tracing_subscriber::registry()
        .with(fmt::layer().with_target(false))
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let host = std::env::var("AGENTOS_KERNEL_HOST").unwrap_or_else(|_| "0.0.0.0".into());
    let port: u16 = std::env::var("AGENTOS_KERNEL_PORT")
        .unwrap_or_else(|_| "9100".into())
        .parse()
        .unwrap_or(9100);

    let addr: SocketAddr = format!("{}:{}", host, port).parse()?;

    info!(target: "agentos-kernel", "========================================");
    info!(target: "agentos-kernel", "  Lingxi AgentOS 0.2 内核启动");
    info!(target: "agentos-kernel", "  监听地址: http://{}", addr);
    info!(target: "agentos-kernel", "  健康检查: http://{}/health", addr);
    info!(target: "agentos-kernel", "  WebSocket: ws://{}/ws", addr);
    info!(target: "agentos-kernel", "  Schema: http://{}/api/v1/schema", addr);
    info!(target: "agentos-kernel", "========================================");

    // ── 插件系统初始化 ──

    // 确定插件目录
    let plugins_dir = std::env::var("AGENTOS_PLUGINS_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            // 默认路径：工作区根目录下的 plugins/shared/
            // 尝试从 CARGO_MANIFEST_DIR 向上查找，否则用相对路径
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .and_then(|p| p.parent())
                .and_then(|p| p.parent())
                .map(|root| root.join("plugins/shared"))
                .unwrap_or_else(|| PathBuf::from("plugins/shared"))
        });

    info!(
        target: "agentos-kernel",
        "Plugin directory (builtin root): {}",
        plugins_dir.display()
    );

    // 确定用户插件根目录（可写，第三方插件安装位置）
    // 解析优先级：环境变量 > OS 标准目录 > None（保持原行为）
    let user_plugins_dir = resolve_user_plugins_dir();
    if let Some(ref user_dir) = user_plugins_dir {
        info!(
            target: "agentos-kernel",
            "User plugin root: {}",
            user_dir.display()
        );
    } else {
        info!(
            target: "agentos-kernel",
            "User plugin root: disabled (no env var, OS data dir unavailable)"
        );
    }

    // config_root = 工作区根目录下的 config/（与 plugins_dir 同级基准）
    // 必须在创建 loader 之前推导：loader 需要 config_root 才能让 load_config() 不返回空 {}
    let config_root = std::env::var("AGENTOS_CONFIG_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .and_then(|p| p.parent())
                .and_then(|p| p.parent())
                .map(|root| root.join("config"))
                .unwrap_or_else(|| PathBuf::from("config"))
        });

    info!(
        target: "agentos-kernel",
        "Config root: {}",
        config_root.display()
    );

    // 加载项目根 .env 到进程环境（config_root 的父目录）。
    // sidecar 子进程默认继承父进程环境变量（tokio Command 无 env_clear），
    // 这样 sidecar 能解析配置里的 ${API_KEY} 等占位符（ADR §4.3 secrets）。
    // 仅设置进程未已有的变量（系统环境变量优先于 .env）。
    if let Some(project_root) = config_root.parent() {
        let env_path = project_root.join(".env");
        if env_path.is_file() {
            if let Ok(content) = std::fs::read_to_string(&env_path) {
                let mut loaded = 0usize;
                for line in content.lines() {
                    let line = line.trim();
                    if line.is_empty() || line.starts_with('#') {
                        continue;
                    }
                    if let Some((key, value)) = line.split_once('=') {
                        let key = key.trim();
                        // 仅当进程环境未已有该变量时设置（系统环境 > .env）
                        if std::env::var(key).is_err() {
                            let value = value.trim().trim_matches('"');
                            std::env::set_var(key, value);
                            loaded += 1;
                        }
                    }
                }
                info!(target: "agentos-kernel", "Loaded {} vars from {}", loaded, env_path.display());
            }
        }
    }

    // 创建插件加载器——以 plugins/shared/ 为内置根，启用 user_root 覆盖语义，
    // 并接入 config_root（P0-1：否则 load_config() 恒返回空 {}，插件收不到配置）
    let loader = build_plugin_loader(&plugins_dir, user_plugins_dir, &config_root);

    // 递归扫描插件目录——scan_root 只扫描一级子目录，
    // plugins/shared/ 的结构是 tools/simple/plugin.json（二级嵌套），
    // 需要收集所有包含 plugin.json 的目录的父目录传给 discover。
    let root_paths = discover_plugin_roots(&plugins_dir);

    info!(
        target: "agentos-kernel",
        "Scanning {} root directories under {}",
        root_paths.len(),
        plugins_dir.display()
    );

    let manifests = loader.discover(&root_paths.iter().map(|s| s.as_str()).collect::<Vec<_>>()).await.unwrap_or_else(|e| {
        warn!(
            target: "agentos-kernel",
            "Failed to discover plugins: {}. Continuing with empty plugin list.", e.message
        );
        Vec::new()
    });

    info!(
        target: "agentos-kernel",
        "Discovered {} plugin manifests",
        manifests.len()
    );

    // 创建能力注册表
    let registry = Arc::new(CapabilityRegistryImpl::new());

    // 安装触发模型 L1：加载 default_profile.yaml 启用层。
    // disabled 的插件不进注册表出口（tools/route_signals/http_routes 不暴露）。
    // 优先级：manifest.enabled > profile.plugins[id] > defaults > enabled=true。
    let enablement = agentos_plugin_loader::PluginEnablement::load(&config_root);

    // 将 manifest 中声明的工具注册到 CapabilityRegistry
    let mut tool_count = 0usize;
    let mut skipped_internal = 0usize;
    let mut skipped_disabled = 0usize;
    for manifest in &manifests {
        // L1 Enabled 过滤：disabled 插件不进出口（安装触发模型 §1.1）
        if !enablement.is_enabled(&manifest.id, manifest.enabled) {
            skipped_disabled += 1;
            continue;
        }
        // ADR 附录D①（task_11）：只有 plugin_type == tool 的插件，其
        // capabilities.tools 才是"给大模型调用的工具"，注册进 tools 维。
        // pipeline 的 `*.execute` 是内部调用入口、system 的是服务能力，
        // 不暴露给 LLM（/api/v1/tools）；P6 invoke_entry 治理后彻底分离。
        if manifest.plugin_type == PluginType::Tool {
            for tool_cap in &manifest.capabilities.tools {
                let category = tool_cap
                    .category
                    .clone()
                    .unwrap_or(ToolCategory::System);
                let descriptor = ToolDescriptor {
                    name: tool_cap.name.clone(),
                    description: tool_cap
                        .description
                        .clone()
                        .unwrap_or_else(|| format!("Tool from {}", manifest.name)),
                    plugin_id: manifest.id.clone(),
                    input_schema: tool_cap
                        .input_schema
                        .clone()
                        .unwrap_or(serde_json::json!({})),
                    output_schema: tool_cap.output_schema.clone(),
                    category,
                    source: if manifest.host_type
                        == agentos_core::traits::HostType::Sidecar
                    {
                        ToolSource::Mcp
                    } else {
                        ToolSource::Builtin
                    },
                };
                registry.register_tool(&manifest.id, descriptor);
                tool_count += 1;
            }
        } else {
            skipped_internal += manifest.capabilities.tools.len();
        }

        // 注册路由信号
        if !manifest.capabilities.route_signals.is_empty() {
            registry.register_route_signals(
                &manifest.id,
                manifest.capabilities.route_signals.clone(),
            );
        }
    }

    info!(
        target: "agentos-kernel",
        "Registered {} tools from {} plugins (filtered out {} internal/service entries, {} disabled by profile, ADR 附录D①)",
        tool_count,
        manifests.len(),
        skipped_internal,
        skipped_disabled
    );

    // P3：注册插件 HTTP 端点（ADR §3.3）——聚合报错（fail-closed，不逐个 panic）。
    // 只注册 enabled 插件的 http_endpoints（安装触发模型 L1 过滤）。
    let enabled_manifests: Vec<agentos_core::traits::PluginManifest> = manifests
        .iter()
        .filter(|m| enablement.is_enabled(&m.id, m.enabled))
        .cloned()
        .collect();
    let http_errors =
        agentos_api::http_dispatcher::register_manifest_http_routes(&registry, &enabled_manifests);
    let http_route_count = registry.list_http_routes().len();
    if !http_errors.is_empty() {
        panic!(
            "插件 HTTP 端点注册失败（路由治理 fail-closed），拒绝启动内核:\n{}",
            http_errors.join("\n")
        );
    }
    info!(
        target: "agentos-kernel",
        "Registered {} plugin HTTP endpoints",
        http_route_count
    );

    // 初始化管道引擎——SQLite 持久化文件（默认项目根 agentos.db，可用环境变量覆盖）。
    // 进程重启后保留 runs/messages/traces/blobs 历史；多轮对话靠 messages 表按
    // session_id 恢复上下文。开发/测试可用 AGENTOS_DB_PATH=:memory: 切回内存库。
    //
    // 0.2 用独立 db 文件（agentos_kernel.db），与 0.1 的 agentos.db 物理隔离：
    // 0.2 是全新四表 schema（runs/messages/traces/blobs），不迁 0.1 数据，
    // 隔离避免 0.1 老库同名表 schema 漂移的隐患（IF NOT EXISTS 不会改老表结构）。
    let db_path = std::env::var("AGENTOS_DB_PATH").unwrap_or_else(|_| {
        config_root
            .parent()
            .map(|root| root.join("agentos_kernel.db").to_string_lossy().to_string())
            .unwrap_or_else(|| "agentos_kernel.db".to_string())
    });
    info!(target: "agentos-kernel", "SQLite store: {}", db_path);
    let store = Arc::new(if db_path == ":memory:" {
        SqliteStore::open_memory()?
    } else {
        SqliteStore::open(&db_path)?
    });

    // 播种内置 admin 用户（0.5.0 最小持久化地基）。
    // 首次启动时若 users 表无 admin，插入（admin/admin12345/tenant=default）。
    // 之后 login/me/register 均查 DB；进程重启用户不丢。幂等：已存在则跳过。
    seed_admin_user(store.clone()).await;

    // 创建真实插件调用器——按 host_type 透明分发：
    //   Sidecar: 通过 MCP stdio fork Python sidecar 执行插件
    //   InProcess: 经 NativePluginLoader 加载 cdylib 走 C-ABI（放进插件目录即用）
    //   Wasm: 经 WasmRuntime（wasmtime）加载执行 .wasm（放进插件目录即用）
    // 默认配置即可运行；host 能力注册表/白名单校验器留空（按需后续注入）。
    // 阶段3 遗留：在 loader 被 move 进 Arc 之前，先取出插件根目录映射，
    // 后续注入 AppState 启用 /ext/{plugin_id}/assets/** 静态资源托管。
    let plugin_dirs = loader.get_plugin_dirs();
    eprintln!(
        "[boot-diag] plugin_dirs loaded: {} entries",
        plugin_dirs.len()
    );
    let loader_arc = Arc::new(loader);
    let wasm_runtime = Arc::new(WasmRuntime::new()?);
    let native_loader = Arc::new(NativePluginLoader::new());
    let invoker = Arc::new(
        PluginInvokerImpl::new(loader_arc.clone())
            .set_wasm_runtime(wasm_runtime)
            .set_native_loader(native_loader),
    );
    // 显式注入 PYTHONPATH 候选目录：sidecar SDK 统一用 `from src.core.logging import`
    // 这种带 src. 前缀的 import，要让其解析成功，sys.path 必须含 src/ 的**父目录**
    // （project_root），而非 src/ 本身（否则 Python 在 <src>/src/core 找模块，报
    // `No module named 'src.core'`）。历史上靠 AGENTOS_PLUGINS_DIR 环境变量推算，
    // 但不同启动方式（.sh / IDE）未必设置该变量，导致 sidecar 启动即 import 失败、
    // initialize 卡到超时。这里由内核直接从已知 plugins_dir 推算 project_root 注入，
    // 不依赖外部环境。plugins/shared → plugins/ → project_root。
    if let Some(project_root) = plugins_dir
        .parent()
        .and_then(|p| p.parent())
        .filter(|p| p.join("src").is_dir())
    {
        invoker.set_pythonpath_src(project_root);
    }
    // 启动插件空闲软卸载 GC（生命周期管理：用到才加载 + 长时间不用自动 kill 进程，
    // manifest 保留，下次调用重新 spawn）。每 30s 扫描，阈值默认 300s。
    invoker.start_idle_gc();

    // 生命周期钩子事件总线（多消费者广播）：在既有"点对点"分发旁路接入一条 broadcast 通道，
    // 把 OnPipelineStart/OnPipelineEnd 等事件 fan-out 给审计日志 + 指标等订阅者。
    // 容量 1024：生命周期事件低频，足够吸收突发；emit best-effort 非阻塞，绝不拖慢引擎热路径。
    let hook_bus = Arc::new(agentos_hooks::HookEventBus::new(1024));
    let engine = Arc::new(
        AdrEngineImpl::new(store.clone(), invoker.clone(), "default")
            .with_hook_bus(hook_bus.clone()),
    );

    // 监控 M1：创建指标聚合器（三通道汇聚：内核自采 + 插件 record_metric + invoker 代采进程态）。
    // M4：router 持聚合器，metrics.record 反向调用写入它。
    let metrics_aggregator = agentos_api::metrics::MetricsAggregator::new();
    // 监控 M2：内核自采 A 类指标计数器注册中心。
    let kernel_counters = Arc::new(agentos_api::metrics::KernelCounters::new());

    // 生命周期事件订阅者接线：
    // - 审计订阅者（agentos-hooks）：每个生命周期事件记 structured log（hook + 目标）。
    // - 指标订阅者（metrics/lifecycle）：按 hook 类型 inc lifecycle.* 计数器，
    //   经 KernelCounters → flush_to → MetricsAggregator → Prometheus 链路暴露。
    // 两者均 spawn 后台任务，慢消费者 Lagged 自动 warn 恢复（绝不 fatal）。
    let _audit_handle = agentos_hooks::spawn_audit_subscriber(hook_bus.clone());
    let _lifecycle_metrics_handle =
        agentos_api::metrics::spawn_lifecycle_metrics_subscriber(hook_bus.clone(), kernel_counters.clone());
    info!(target: "agentos-kernel", "lifecycle hook event bus + subscribers (audit/metrics) started");

    // 启用 sidecar→内核反向 capability 通道（审批暂停/恢复、复盘调管道、event-bus 的地基）。
    // 监控 M4：router 持聚合器，metrics.record 分支写聚合器（第 6 个 capability）。
    // tool-executor：tool_core sidecar 委托内核执行 tool 插件 sidecar（bash_execute 等）。
    // event-bus.emit：流式 chunk 推前端——session 提前创建并注入 router + 后续 enable_session 复用。
    // service-registry.*（M2）：基础设施下沉内核——插件经此 capability 访问内核共享存储
    //   （execution-records/pipeline-summaries/memory，M1 落地）。复用同一 SqliteStore 实例。
    let session_coord = Arc::new(agentos_session::SessionCoordinator::new());

    // M5：动态 capability handler 注册表 + McpBridge。
    // 扫描已启用 manifest 的 provides.capabilities，注册成 handler；
    // McpBridge 把 capability 调用转发到对应 sidecar 插件的工具。
    // 这让 human-interaction 等插件自注册的 namespace 经 reader loop → router →
    // handler → bridge → invoker.invoke_tool → sidecar 完成闭环。
    // 路由完全从 manifest provides.capabilities 声明派生（含 tool_prefix），
    // 内核零硬编码——新插件声明 provides 即自动注册，无需改内核。
    let handler_registry = Arc::new(agentos_mcp::CapabilityHandlerRegistry::new());
    let mcp_bridge = Arc::new(agentos_plugin_loader::McpBridge::new(invoker.clone() as Arc<dyn agentos_core::traits::PluginInvoker>));
    mcp_bridge.add_routes_from_manifests(&enabled_manifests);
    let registered = agentos_plugin_loader::register_provided_capabilities(
        &handler_registry,
        &enabled_manifests,
        Some(mcp_bridge.clone()),
    );
    info!(
        target: "agentos-kernel",
        "Registered {} provided capabilities from plugin manifests (handler registry)",
        registered
    );

    let router = Arc::new(
        KernelCapabilityRouter::with_metrics(
            engine.clone(),
            metrics_aggregator.clone(),
        )
        .with_invoker(invoker.clone())
        .with_registry(registry.clone())
        .with_session(session_coord.clone())
        .with_store(store.clone())
        .with_handler_registry(handler_registry),
    );
    invoker.set_router(router);

    // 监控 M3：注册崩溃回调——invoker 检测到插件崩溃时记时间戳到聚合器（last_crash_ts）。
    // 进程态轮询（memory_rss/uptime/alive/pid）由独立后台任务周期采（见下方 spawn）。
    {
        let agg_for_crash = metrics_aggregator.clone();
        invoker.on_crash(Arc::new(move |plugin_id: &str| {
            let snap = agentos_api::metrics::ProcStateSnapshot {
                plugin_id: plugin_id.to_string(),
                alive: false,
                pid: None,
                memory_rss_bytes: None,
                uptime_secs: None,
                last_crash_ts: Some(agentos_api::metrics::now_secs()),
            };
            agentos_api::metrics::collect_proc_state(&agg_for_crash, &snap);
            info!(target: "agentos-kernel", plugin = plugin_id, "Plugin crash recorded as process.last_crash_ts metric");
        }));
    }

    info!(
        target: "agentos-kernel",
        "Pipeline engine initialized (in-memory SQLite, reverse capability channel + metrics aggregator enabled)"
    );

    // ── 0.2 引擎接线：加载管道配置 + 公共 step 库 + 重名检测 ──
    // （config_root 已在上方 loader 创建前推导，此处复用）

    let pipeline_config =
        load_pipeline_config(&config_root).unwrap_or_else(|e| {
            panic!("加载管道配置失败 ({}): {}", config_root.display(), e);
        });
    let step_library =
        load_step_library(&config_root).unwrap_or_else(|e| {
            panic!("加载公共 step 库失败 ({}): {}", config_root.display(), e);
        });

    info!(
        target: "agentos-kernel",
        "Loaded pipeline '{}' with {} steps, step library with {} entries",
        pipeline_config.name,
        pipeline_config.steps.len(),
        step_library.steps.len()
    );

    // 收集已知插件 id（命中规则③判定 + 重名检测用）
    let plugin_ids: std::collections::HashSet<String> =
        manifests.iter().map(|m| m.id.clone()).collect();

    // 启动期重名检测：冲突则 panic 退出（fail-fast，避免运行期歧义）
    if let Err(conflict) =
        validate_no_name_conflicts(&pipeline_config, &step_library, &plugin_ids)
    {
        panic!(
            "命名冲突检测失败，拒绝启动内核（修复配置后重试）: {}",
            conflict
        );
    }
    eprintln!("[boot-diag] 重名检测完成"); use std::io::Write; std::io::stderr().flush().ok();

    // 构建 AppState（注入 pipeline_config / step_library / invoker / store / plugin_ids / project_root）
    let project_root = config_root
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."));
    let store_dyn: Arc<dyn agentos_core::traits::StorageBackend> = store.clone();
    eprintln!("[boot-diag] store_dyn ok"); std::io::stderr().flush().ok();
    let invoker_dyn: Arc<dyn agentos_core::traits::PluginInvoker> = invoker.clone();
    eprintln!("[boot-diag] invoker_dyn ok"); std::io::stderr().flush().ok();
    // P3：HTTP 端点 dispatcher 的生产 handler（经 invoker 调插件 http.handle）
    let http_handler: Arc<dyn agentos_core::traits::HttpHandleCapability> =
        Arc::new(agentos_api::http_dispatcher::SidecarHttpHandler::new(
            invoker_dyn.clone(),
        ));
    eprintln!("[boot-diag] http_handler ok"); std::io::stderr().flush().ok();
    // L1 启用集合（schema 据此过滤 contributes）
    let enabled_plugin_ids: std::collections::HashSet<String> = manifests
        .iter()
        .filter(|m| enablement.is_enabled(&m.id, m.enabled))
        .map(|m| m.id.clone())
        .collect();
    eprintln!("[boot-diag] enabled_plugin_ids ok"); std::io::stderr().flush().ok();
    let state = AppState::with_plugins(
        manifests.clone(),
        registry,
        engine,
        Arc::new(pipeline_config),
        Arc::new(step_library),
        invoker_dyn,
        store_dyn,
        plugin_ids,
        project_root,
        enabled_plugin_ids,
    );
    eprintln!("[boot-diag] with_plugins 返回"); std::io::stderr().flush().ok();
    // task_01：注入统一数据接口专用 SqliteStore 句柄（/api/v1/db/* 用，表驱动动态枚举）。
    // 与 store_dyn（trait object，业务语义方法）互补；with_db 不改任何持久化方式。
    let state = state.with_db(store.clone());
    let state = state.with_http_handler(http_handler);
    eprintln!("[boot-diag] with_http_handler 返回"); std::io::stderr().flush().ok();
    // 阶段3 遗留：注入插件根目录映射，启用静态资源托管
    // （/ext/{plugin_id}/assets/{*path} → <plugin_dir>/web/<path> 直读）。
    // 插件只需在自己的目录下放 web/ 子目录即可被内核自动托管，无需声明 http_endpoints。
    let state = state.with_plugin_dirs(plugin_dirs);
    eprintln!("[boot-diag] with_plugin_dirs 返回"); std::io::stderr().flush().ok();
    // 统一配置加载方案 TDD-4：构造 ConfigCenter 注入 AppState。
    // 后续 loader（agent/pipeline/plugin config_files）经此统一走 load()/load_dir()/store()。
    let state = if let Some(root) = state.project_root.as_ref() {
        let cc = std::sync::Arc::new(agentos_config::config_center::ConfigCenter::new(root.join("config")));
        state.with_config_center(cc)
    } else {
        state
    };
    eprintln!("[boot-diag] with_config_center 返回"); std::io::stderr().flush().ok();
    let state = state
    // 监控 M1/M5/M5b：注入指标聚合器（启用 /api/v1/metrics + /metrics 端点）
    .with_metrics(metrics_aggregator.clone());
    eprintln!("[boot-diag] with_metrics 返回"); std::io::stderr().flush().ok();
    // P2：启用会话内核（WS 握手鉴权 + 连接注册 + 入站路由 + 断线重放）。
    // 复用 router 已持有的 session_coord（流式 chunk 推送与 WS 出站共享同一 SessionCoordinator）。
    let state = state.enable_session_with(session_coord);
    eprintln!("[boot-diag] AppState 构造完成（enable_session_with 之后）"); std::io::stderr().flush().ok();

    // 监控 M2/M6：后台任务——每秒把内核自采计数器 flush 到聚合器 + 滚动桶降采样。
    // M6：每秒采样关键指标广播给订阅 statusBar 的连接（widget_event）。
    if let Some(session) = state.session.clone() {
        let agg_flush = metrics_aggregator.clone();
        let kc_flush = kernel_counters.clone();
        let agg_rollup = metrics_aggregator.clone();
        // MetricBroadcaster::spawn 需要 Arc<MetricsAggregator>；MetricsAggregator 内部已
        // 用 Arc<RwLock> 共享，这里再包一层 Arc 满足签名（spawn 内只读快照）。
        let agg_bcast: Arc<agentos_api::metrics::MetricsAggregator> =
            Arc::new(metrics_aggregator.clone());
        let kc_bcast = kernel_counters.clone();
        let session_bcast = session.clone();
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(std::time::Duration::from_secs(1));
            tick.tick().await; // 跳过首次立即触发
            loop {
                tick.tick().await;
                // M2：内核自采计数器快照 → 聚合器
                kc_flush.flush_to(&agg_flush);
                // M1：滚动桶降采样（1s→10s 合并 + 超 2h 清理）
                agg_rollup.rollup();
            }
        });
        // M6：每秒采样关键指标广播（widget_event → 前端 statusBar）
        let _bcast_handle = agentos_api::metrics::MetricBroadcaster::spawn(
            agg_bcast,
            Some(kc_bcast),
            session_bcast,
            std::time::Duration::from_secs(1),
        );
        // ADR §3.5'：插件 widget 配置驱动推送——按 contributes.widgets[].metric_bindings
        // 把插件已上报的指标定时推给前端。插件被动（照常 metrics.record），内核统一编排。
        let widget_bindings = {
            let entries: Vec<(&str, Option<&serde_json::Value>)> = manifests
                .iter()
                .map(|m| (m.id.as_str(), m.contributes.as_ref()))
                .collect();
            agentos_api::metrics::collect_all_bindings(entries)
        };
        if !widget_bindings.is_empty() {
            let agg_widget: Arc<agentos_api::metrics::MetricsAggregator> =
                Arc::new(metrics_aggregator.clone());
            let session_widget: Arc<dyn agentos_api::metrics::WidgetEmitter> = session.clone();
            let _widget_bcast_handle =
                agentos_api::metrics::PluginWidgetBroadcaster::spawn(
                    agg_widget,
                    widget_bindings.clone(),
                    session_widget,
                );
            info!(
                target: "agentos-kernel",
                count = widget_bindings.len(),
                "PluginWidgetBroadcaster started ({} metric_bindings)",
                widget_bindings.len()
            );
        }
        info!(target: "agentos-kernel", "Metrics background tasks started (M2 flush + M1 rollup + M6 broadcast, 1s interval)");
    }

    start_server(addr, state).await?;
    eprintln!("[boot-diag] start_server 返回（不应到达）");

    Ok(())
}

/// 解析用户插件根目录（可写，第三方插件安装位置）。
///
/// 解析优先级：
/// 1. 环境变量 `LINGXI_USER_PLUGINS_DIR`（与 `LINGXI_PLUGINS_DIR` 命名风格一致）
/// 2. 环境变量 `AGENTOS_USER_PLUGINS_DIR`（与 `AGENTOS_PLUGINS_DIR` 命名风格一致）
/// 3. `dirs::data_dir().join("agentos").join("plugins")`
///    （Win=`%APPDATA%/agentos/plugins`，macOS=`~/Library/Application Support/agentos/plugins`，
///    Linux=`~/.local/share/agentos/plugins`）
/// 4. 均不可用则返回 `None`（保持原行为：不启用 user_root）
///
/// 注意使用 `dirs::data_dir()` 而非 `data_local_dir()`（后者是 `%LOCALAPPDATA%`，
/// 不随用户漫游，不适合作为第三方插件安装位置）。
fn resolve_user_plugins_dir() -> Option<PathBuf> {
    // 1. 环境变量（LINGXI_ 前缀优先，回退 AGENTOS_ 前缀）
    if let Ok(val) = std::env::var("LINGXI_USER_PLUGINS_DIR") {
        if !val.trim().is_empty() {
            return Some(PathBuf::from(val));
        }
    }
    if let Ok(val) = std::env::var("AGENTOS_USER_PLUGINS_DIR") {
        if !val.trim().is_empty() {
            return Some(PathBuf::from(val));
        }
    }

    // 2. OS 标准目录
    dirs::data_dir().map(|d| d.join("agentos").join("plugins"))
}

/// 递归发现包含 plugin.json 的目录的父目录路径列表。
///
/// `scan_root(root)` 扫描 root 的直接子目录，查找 `<child>/plugin.json`。
/// plugins/shared/ 的结构是 `tools/simple/plugin.json`（二级嵌套），
/// 因此需要收集所有直接包含 plugin.json 的目录的**父目录**作为扫描根。
fn discover_plugin_roots(base: &std::path::Path) -> Vec<String> {
    let mut plugin_dirs = Vec::new();
    if !base.exists() {
        return plugin_dirs;
    }
    collect_plugin_dirs(base, &mut plugin_dirs);

    // 收集父目录并去重
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

fn collect_plugin_dirs(dir: &std::path::Path, dirs: &mut Vec<String>) {
    let entries = match std::fs::read_dir(dir) {
        Ok(e) => e,
        Err(_) => return,
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            // 检查该目录是否直接包含 plugin.json
            if path.join("plugin.json").exists() || path.join("plugin.yaml").exists() {
                if let Some(s) = path.to_str() {
                    dirs.push(s.to_string());
                }
            } else {
                // 递归搜索子目录
                collect_plugin_dirs(&path, dirs);
            }
        }
    }
}

/// 构建插件加载器，并接入配置根目录（task_11 P0-1）。
///
/// 将 loader 创建从 `main` 抽出，便于单测验证「config_root 已接到 loader」
/// （历史 bug：`main` 创建 loader 时漏调 `.with_config_root(config_root)`，
/// 导致 `load_config()` 恒返回空 `{}`，插件收不到任何配置）。
///
/// # Arguments
/// * `plugins_dir` - 内置插件根目录（只读）
/// * `user_plugins_dir` - 用户插件根目录（可选，可写）
/// * `config_root` - 配置文件根目录（如 `config/`），loader 据此加载 YAML
pub(crate) fn build_plugin_loader(
    plugins_dir: &std::path::Path,
    user_plugins_dir: Option<PathBuf>,
    config_root: &std::path::Path,
) -> PluginLoaderImpl {
    PluginLoaderImpl::new(plugins_dir, user_plugins_dir)
        // 接入 config_root：否则 load_config() 因 config_root=None 恒返回空 {}
        .with_config_root(config_root)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// P0-1：build_plugin_loader 接入 config_root 后，load_config 返回非空（含 models 节）。
    ///
    /// 回归保护：若有人移除 `.with_config_root(config_root)`，此测试会失败——
    /// 因为 loader 的 config_root 为 None 时 load_config 恒返回空 `{}`。
    #[tokio::test]
    async fn build_plugin_loader_wires_config_root() {
        let plugins_dir = tempfile::tempdir().unwrap();
        let config_dir = tempfile::tempdir().unwrap();

        // 构造与真实 config/ 同构的最小结构：config/models/llm.yaml
        let models_dir = config_dir.path().join("models");
        std::fs::create_dir_all(&models_dir).unwrap();
        std::fs::write(
            models_dir.join("llm.yaml"),
            "models:\n  glm-5.2:\n    provider: zhipu_coding\nproviders:\n  zhipu_coding:\n    type: openai\n",
        )
        .unwrap();

        let loader = build_plugin_loader(plugins_dir.path(), None, config_dir.path());

        let config = loader.load_config().await.expect("load_config 应成功");
        let obj = config
            .as_object()
            .expect("load_config 应返回对象，而非空 {{}}");

        // P0-1 验收：非空 + 含 models 节（对应 config_files 映射 models 的插件）
        assert!(
            !obj.is_empty(),
            "config_root 接入后 load_config 不应返回空 {{}}（P0-1 bug 回归）"
        );
        assert!(
            obj.contains_key("models"),
            "应含 models 节（config/models/llm.yaml 经 collect_yaml_configs 递归收录）"
        );

        // 递归结构验证：models.llm 应是 llm.yaml 的解析结果
        let models = obj
            .get("models")
            .and_then(|v| v.as_object())
            .expect("models 节应为对象");
        assert!(
            models.contains_key("llm"),
            "models 节下应含 llm.yaml 的解析结果"
        );
        let llm = models.get("llm").and_then(|v| v.as_object()).unwrap();
        assert!(llm.contains_key("providers"), "llm.yaml 内容应含 providers");
    }
}



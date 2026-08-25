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
//! - AGENTOS_USER_PLUGINS_DIR：用户插件根目录（默认 OS 标准目录 agentos/plugins）

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use agentos_api::{
    load_pipeline_config, load_step_library, routes::AppState, start_server,
    validate_no_name_conflicts, KernelCapabilityRouter,
};
use agentos_core::traits::{CapabilityRegistry, PluginLoader, ToolDescriptor};
use agentos_core::types::{ToolCategory, ToolSource, UserRecord};
use agentos_invoker::PluginInvokerImpl;
use agentos_plugin_loader::{CapabilityRegistryImpl, NativePluginLoader, PluginLoaderImpl};
use tracing::{error, info, warn};
use tracing_subscriber::{fmt, prelude::*};

/// 播种内置 admin 用户（首次启动插入，已存在则跳过）。
///
/// 0.5.0 最小持久化地基：auth 由硬编码占位改为查 DB，启动时确保 admin 存在。
/// tenant_id = "default"（与多租户隔离地基的默认租户一致），保证旧数据
/// （0.5.0 前以 default 写入的会话/消息）仍归 admin 可见。
async fn seed_admin_user(store: Arc<dyn agentos_core::traits::StorageBackend>) {
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
                Err(e) => {
                    warn!(target: "agentos-kernel", error = %e, "播种 admin 用户失败（DB 查询将查不到 admin，登录不可用）")
                }
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

    // 内核能力契约加载（2026-08-20 Part B：定义驱动入口校验 + schema 聚合的
    // 单一真值源）。目录缺失 = 未启用（宽泛放行）；文件损坏 = fail-fast 拒启
    // ——契约是校验器的眼睛，坏契约静默跳过等于校验器装瞎。
    let capability_contracts: Arc<Vec<agentos_api::kernel_capabilities::KernelCapabilityContract>> =
        Arc::new(
            agentos_api::kernel_capabilities::load_contracts(
                &config_root.join("kernel_capabilities"),
            )
            .unwrap_or_else(|e| panic!("内核能力契约文件加载失败（fail-closed）: {e}")),
        );
    info!(
        target: "agentos-kernel",
        namespaces = capability_contracts.len(),
        "Loaded kernel capability contracts (definition-driven entry validation)"
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

    // 把 config_root 发布到进程环境：invoker 插件指纹与 mcp spawn 的
    // .env 增量叠加（env_file 模块）靠它定位项目根 .env——用户在设置页
    // 填写 API Key 后无需重启内核即可生效。
    if std::env::var("AGENTOS_CONFIG_ROOT").is_err() {
        std::env::set_var("AGENTOS_CONFIG_ROOT", &config_root);
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

    // A10：discover 失败 fail-fast——IO 故障/manifest 损坏意味着插件面不可用，
    // 静默降级为空集会让内核以"无插件"假象运行（工具/能力全缺，问题后置难查），
    // 故拒绝启动。逃生门：AGENTOS_ALLOW_EMPTY_PLUGINS=1（嵌入式/最小化部署/
    // 沙箱场景显式声明接受空插件集）时保留旧的降级启动行为。
    let manifests = match loader
        .discover(&root_paths.iter().map(|s| s.as_str()).collect::<Vec<_>>())
        .await
    {
        Ok(m) => m,
        Err(e) if std::env::var("AGENTOS_ALLOW_EMPTY_PLUGINS").as_deref() == Ok("1") => {
            warn!(
                target: "agentos-kernel",
                "Failed to discover plugins: {}. AGENTOS_ALLOW_EMPTY_PLUGINS=1 → continuing with empty plugin list.", e.message
            );
            Vec::new()
        }
        Err(e) => {
            eprintln!(
                "[boot] 插件 discover 失败，拒绝启动: {}（设 AGENTOS_ALLOW_EMPTY_PLUGINS=1 可强制以空插件集启动）",
                e.message
            );
            std::io::Write::flush(&mut std::io::stderr()).ok();
            return Err(Box::<dyn std::error::Error>::from(format!(
                "plugin discover failed at boot: {}",
                e.message
            )));
        }
    };

    // 注册闸-服务依赖解析（fail-closed，唯一依赖轴）：任意插件的 requires_services
    // 不满足（能力角色无人提供 / 服务端点未注册）→ 拒绝启动；服务→插件映射由服务面注册表
    // 完成，消费者不点名插件 id。把"依赖不满足"从运行期谜题提前到启动期暴露。
    agentos_plugin_loader::resolve_requires_services(&manifests).map_err(|e| {
        eprintln!("[boot] 插件服务依赖解析失败，拒绝启动: {}", e);
        std::io::Write::flush(&mut std::io::stderr()).ok();
        Box::<dyn std::error::Error>::from(format!(
            "plugin service dependency resolution failed at boot: {}",
            e
        ))
    })?;

    // M2-static：启动期按 requires_services（服务边）静态拓扑排序——插件间启动顺序从
    // HashMap 任意序变为显式可证明的依赖序（依赖者后加载；tie-break 字典序）。
    // 服务依赖环 fail-fast（与 pipeline load_and_compile 的坏配置拒绝启动一致）。
    let manifests =
        agentos_plugin_loader::sort_manifests_topologically(&manifests).map_err(|e| {
            eprintln!("[boot] 插件依赖环检测失败，拒绝启动: {}", e);
            std::io::Write::flush(&mut std::io::stderr()).ok();
            Box::<dyn std::error::Error>::from(format!(
                "circular plugin dependencies at boot: {}",
                e
            ))
        })?;

    info!(
        target: "agentos-kernel",
        "Discovered {} plugin manifests",
        manifests.len()
    );

    // 创建能力注册表
    let registry = Arc::new(CapabilityRegistryImpl::new());
    // M1：per-plugin 注册账本（guard 化）——启动注册循环经 guarded 注册入账本，
    // disable/unload 路径经 revoke 结构性收回（registry 四维 + broadcaster 绑定）。
    let plugin_scopes = Arc::new(agentos_plugin_loader::PluginScopeRegistry::new());

    // 安装触发模型 L1：加载 default_profile.yaml 启用层。
    // disabled 的插件不进注册表出口（tools/route_signals/http_routes 不暴露）。
    // 优先级：manifest.enabled > profile.plugins[id] > defaults > enabled=true。
    // K6：profile 解析失败 → 保守全禁（is_enabled 恒 false），此处升级为 error
    // 级启动报告——下方注册循环会以"全部插件 skipped_disabled"落地该裁决。
    let enablement = agentos_plugin_loader::PluginEnablement::load(&config_root);
    if enablement.is_corrupted() {
        error!(
            target: "agentos-kernel",
            "default_profile.yaml 解析失败：启用层进入保守全禁（K6 fail-closed），\
             所有插件本次启动不注册；修复 config/plugins/default_profile.yaml 后重启"
        );
    }

    // 将 manifest 中声明的工具注册到 CapabilityRegistry
    let mut tool_count = 0usize;
    let mut skipped_disabled = 0usize;
    // K9：缺 input_schema 而以 {} 补注册的工具计数（进启动报告；2026-08-20
    // 全量统计 plugins/ 下 84 个 manifest 工具中 28 个缺声明）。
    let mut missing_input_schema = 0usize;
    for manifest in &manifests {
        // L1 Enabled 过滤：disabled 插件不进出口（安装触发模型 §1.1）
        if !enablement.is_enabled(&manifest.id, manifest.enabled) {
            skipped_disabled += 1;
            continue;
        }
        // D.6 槽位拆分（2026-08-15，废止原附录D① 类型门控）：
        // capabilities.tools 语义唯一 = 给 LLM 的工具，声明即注册（不看类型）；
        // 内部服务方法在 capabilities.services（不注册，走 invoke_entry/
        // http_endpoints/显式 plugin_id/provides 通道）。
        {
            let scope = plugin_scopes.scope_of(&manifest.id);
            for tool_cap in &manifest.capabilities.tools {
                let category = tool_cap.category.clone().unwrap_or(ToolCategory::System);
                // K9：缺 input_schema 以 {} 补注册但必须可见（warn + 计数进启动
                // 报告）。{} 是 object，LLM 侧 input_schema.is_object() 过滤对它
                // 恒不触发——零参数描述进工具面，LLM 只能盲调；补声明属
                // plugins/ 侧治理。运行时新增插件注册路径同款 warn 见
                // plugin_lifecycle::register_plugin_capabilities。
                if tool_cap.input_schema.is_none() {
                    missing_input_schema += 1;
                    warn!(
                        target: "plugin-registration",
                        plugin_id = %manifest.id,
                        tool = %tool_cap.name,
                        "tool manifest 缺 input_schema，以 {{}} 补注册（LLM 侧 object 过滤恒不触发，LLM 只能盲调；请补声明）"
                    );
                }
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
                    source: if manifest.host_type == agentos_core::traits::HostType::Sidecar {
                        ToolSource::Mcp
                    } else {
                        ToolSource::Builtin
                    },
                    ui: tool_cap.ui.clone(),
                    render: tool_cap.render.clone(),
                };
                // M1：guarded 注册——撤销 guard 入 scope，disable 时结构性收回。
                scope.track(registry.register_tool_guarded(&manifest.id, descriptor));
                tool_count += 1;
            }
        }

        // 注册路由信号（M1 guarded）
        if !manifest.capabilities.route_signals.is_empty() {
            let scope = plugin_scopes.scope_of(&manifest.id);
            scope.track(registry.register_route_signals_guarded(
                &manifest.id,
                manifest.capabilities.route_signals.clone(),
            ));
        }
    }

    info!(
        target: "agentos-kernel",
        "Registered {} tools from {} plugins (declaration-based, D.6 slot split; {} disabled by profile; {} missing input_schema registered as {{}} — K9)",
        tool_count,
        manifests.len(),
        skipped_disabled,
        missing_input_schema
    );

    // P3：注册插件 HTTP 端点（ADR §3.3）——聚合报错（fail-closed，不逐个 panic）。
    // 只注册 enabled 插件的 http_endpoints（安装触发模型 L1 过滤）。
    let enabled_manifests: Vec<agentos_core::traits::PluginManifest> = manifests
        .iter()
        .filter(|m| enablement.is_enabled(&m.id, m.enabled))
        .cloned()
        .collect();
    let http_errors = agentos_api::http_dispatcher::register_manifest_http_routes(
        &registry,
        &enabled_manifests,
        Some(&plugin_scopes),
    );
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

    // 初始化存储——StorageBackend driver 化（§9.6）：按 config/storage.yaml 或
    // 环境变量选 driver（sqlite | memory；postgres 留桩），默认 sqlite +
    // 项目根 agentos_kernel.db（AGENTOS_DB_PATH/:memory: 兼容）。
    // 返回双句柄：store_dyn（业务账本 trait 面，runs/messages/traces/blobs/memory/
    // users——换 driver 时完全可用）+ sqlite_db（SQLite 专有 db-admin 表驱动接口，
    // 非 SQLite driver 下为 None → db-admin capability 诚实降级）。
    // 存储是自举必需件 + 审计真相源，driver 编译进内核而非插件轨（§9.6 判据）。
    // resolve_storage_config：config/storage.yaml 存在但损坏 → Err 拒绝启动
    // （数据正确性优先，不静默落默认库）。
    let storage_cfg = agentos_engine::storage_factory::resolve_storage_config(&config_root)?;
    info!(
        target: "agentos-kernel",
        driver = %storage_cfg.driver,
        sqlite_path = %storage_cfg.sqlite_path,
        "Storage driver resolved (config/storage.yaml > env > default sqlite)"
    );
    let (store, sqlite_db) = agentos_engine::storage_factory::open_storage(&storage_cfg)?;
    let store_dyn: Arc<dyn agentos_core::traits::StorageBackend> = store.clone();

    // 播种内置 admin 用户（0.5.0 最小持久化地基）。
    // 首次启动时若 users 表无 admin，插入（admin/admin12345/tenant=default）。
    // 之后 login/me/register 均查 DB；进程重启用户不丢。幂等：已存在则跳过。
    seed_admin_user(store.clone()).await;

    // B2：启动时清扫孤儿 run——上次进程崩溃留下的 status='running' 的 run
    // 标记为 failed + 补 ended_at（persist_run_end 未执行的真实表现；不清扫会永远卡
    // running，历史/会话状态悬空）。已结束的 run 不受影响。
    // B2 清扫是 SQLite 专有路径（reap_orphan_runs 固有方法）——非 SQLite driver
    // 跳过（孤儿 run 清扫属启动家政，跳过不影响正确性，仅留状态悬空到下次 sqlite 起时清）。
    if let Some(sqlite) = sqlite_db.as_ref() {
        let reaped = sqlite.reap_orphan_runs("default").unwrap_or(0);
        if reaped > 0 {
            warn!(target: "agentos-kernel", reaped = reaped, "启动清扫孤儿 run（标记为 failed）");
        }
    }

    // 创建真实插件调用器——按 host_type 透明分发：
    //   Sidecar: 通过 MCP stdio fork Python sidecar 执行插件
    //   InProcess: 经 NativePluginLoader 加载 cdylib 走 C-ABI（放进插件目录即用）
    // 默认配置即可运行。原 Wasm 轨已按两轨终局决策关闭摘除。
    // 在 loader 被 move 进 Arc 之前，先取出插件根目录映射，
    // 后续注入 AppState 启用 /ext/{plugin_id}/assets/** 静态资源托管。
    let plugin_dirs = loader.get_plugin_dirs();
    let loader_arc = Arc::new(loader);
    let native_loader = Arc::new(NativePluginLoader::new());
    let invoker =
        Arc::new(PluginInvokerImpl::new(loader_arc.clone()).set_native_loader(native_loader));
    // 显式注入 PYTHONPATH 候选目录：sidecar SDK 统一用 `from src.core.logging import`
    // 这种带 src. 前缀的 import，要让其解析成功，sys.path 必须含 src/ 的**父目录**
    // （project_root），而非 src/ 本身（否则 Python 在 <src>/src/core 找模块，报
    // `No module named 'src.core'`）。AGENTOS_PLUGINS_DIR 环境变量推算不可靠
    // （不同启动方式 .sh / IDE 未必设置该变量，会导致 sidecar 启动即 import 失败、
    // initialize 卡到超时），这里由内核直接从已知 plugins_dir 推算 project_root 注入，
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
    // 把 OnPipelineStart/OnPipelineEnd（engine）+ OnLoad（invoker sidecar spawn）等事件
    // fan-out 给审计日志 + 指标等订阅者。
    // 容量 1024：生命周期事件低频，足够吸收突发；emit best-effort 非阻塞，绝不拖慢引擎热路径。
    let hook_bus = Arc::new(agentos_hooks::HookEventBus::new(1024));
    // 把同一总线注入 invoker：sidecar spawn 的 OnLoad 事件在点对点直调（notifications/on_load）
    // 旁路 fan-out 给审计/指标订阅者（与 engine 的 OnPipelineStart/End 同一总线）。
    // 必须在 spawn 任何 sidecar 前完成（start_idle_gc 之后、请求接入之前即满足）。
    invoker.set_hook_bus(hook_bus.clone());
    // 域事件发射点（session_routes / ws_session 的 handler）经全局单例访问同一总线
    // （它们只持 AppState，不便穿层传总线句柄；未注册时观察层静默降级）。
    agentos_hooks::set_global(hook_bus.clone());

    // 闸2·观测：契约状态账本（boot 收口全量插件健康度，后续 reenable/热发现/
    // validate-all 共享写入；与 AppState 同一实例注入，`GET /contract-status` 消费）。
    let contract_states = Arc::new(agentos_api::contract::ContractLedger::new());
    // 注册闸 G2 启动期存量校验（与热发现同源公共函数）：对 enabled 的 sidecar
    // tool 插件 spawn → tools/list → 对照声明。判定失败（tools/list 成功但声明
    // 工具缺失）→ 剔除漂移工具并按净化后 manifest 重注册（前端经契约状态页可见）。
    // 观测失败（spawn/list 重试后仍失败）≠ 判定失败（2026-08-20 裁定）：保留
    // 声明注册 + 账本标记"校验未完成"，30s 后后台复验——复验出真漂移才净化。
    // 只验 tools 非空的 sidecar（services 方向 schema 契约在 Phase 1 补）。
    {
        use agentos_api::plugin_watcher::g2_verify_and_sanitize;
        let mut verified = 0usize;
        let mut drifted = 0usize;
        let mut spawn_failed = 0usize;
        let mut observe_incomplete: Vec<agentos_core::traits::PluginManifest> = Vec::new();
        for manifest in &manifests {
            let enabled = enablement.is_enabled(&manifest.id, manifest.enabled);
            let g2_applicable = enabled
                && manifest.host_type == agentos_core::traits::HostType::Sidecar
                && (!manifest.capabilities.tools.is_empty()
                    || !manifest.capabilities.services.is_empty());
            if !g2_applicable {
                // 非 G2 覆盖（禁用/非 sidecar/无 tools+services）：登记 not_covered 缺省
                contract_states.upsert(agentos_api::contract::PluginContractState::not_covered(
                    manifest, enabled,
                ));
                continue;
            }
            let outcome = g2_verify_and_sanitize(invoker.as_ref(), manifest.clone()).await;
            contract_states.upsert(agentos_api::contract::PluginContractState::derived(
                manifest,
                enabled,
                Some(&outcome),
            ));
            if outcome.spawn_failed {
                // 观测失败：声明注册不动（启动注册循环已按声明注册），待复验。
                spawn_failed += 1;
                observe_incomplete.push(manifest.clone());
                warn!(
                    target: "plugin-g2-boot",
                    plugin = %manifest.id,
                    "注册闸 G2（boot）：观测失败（重试后仍 spawn/tools-list 失败）——保留声明注册，30s 后复验"
                );
                continue;
            }
            if !outcome.drift {
                verified += 1;
                continue;
            }
            drifted += 1;
            // 判定失败：用净化后 manifest 重注册该插件能力（复用 re-enable：scope revoke + 重注册）。
            let (tools, http_routes) = agentos_api::plugin_lifecycle::reenable_plugin_capabilities(
                &outcome.manifest,
                &registry,
                &plugin_scopes,
            );
            // §3.4（0.2 收尾批次 1）：拒注是"声明与实现不一致已实际收口"的异常
            // 事件，从 info 提升为 warn——消除"被拒数日无人知晓"（e2e G5；清单
            // 明细另经 GET /api/v1/plugins/contract-status 暴露）。下方汇总日志
            // 保持 info。
            warn!(
                target: "plugin-g2-boot",
                plugin = %manifest.id,
                rejected = ?outcome.rejected_tools,
                tools,
                http_routes,
                "注册闸 G2（boot）：插件声明与实现不一致，已按净化后 manifest 重注册（需修改插件）"
            );
        }
        info!(
            target: "plugin-g2-boot",
            verified,
            drifted,
            spawn_failed,
            "注册闸 G2 启动期存量校验完成"
        );
        // 观测失败复验（fire-and-forget）：30s 后重验，复验出真漂移（判定失败）
        // 才净化重注册；复验仍观测失败则保持声明注册（下次 boot/热校验再试）。
        if !observe_incomplete.is_empty() {
            let inv2 = invoker.clone();
            let reg2 = registry.clone();
            let scopes2 = plugin_scopes.clone();
            let ledger2 = contract_states.clone();
            tokio::spawn(async move {
                tokio::time::sleep(std::time::Duration::from_secs(30)).await;
                for manifest in &observe_incomplete {
                    let outcome = g2_verify_and_sanitize(inv2.as_ref(), manifest.clone()).await;
                    ledger2.upsert(agentos_api::contract::PluginContractState::derived(
                        manifest,
                        true,
                        Some(&outcome),
                    ));
                    if outcome.spawn_failed {
                        warn!(
                            target: "plugin-g2-boot",
                            plugin = %manifest.id,
                            "注册闸 G2（复验）：观测仍失败——保持声明注册，待下次校验"
                        );
                        continue;
                    }
                    if outcome.drift {
                        let (tools, http_routes) =
                            agentos_api::plugin_lifecycle::reenable_plugin_capabilities(
                                &outcome.manifest,
                                &reg2,
                                &scopes2,
                            );
                        warn!(
                            target: "plugin-g2-boot",
                            plugin = %manifest.id,
                            rejected = ?outcome.rejected_tools,
                            tools,
                            http_routes,
                            "注册闸 G2（复验）：判定声明与实现不一致，已按净化后 manifest 重注册（需修改插件）"
                        );
                    } else {
                        info!(
                            target: "plugin-g2-boot",
                            plugin = %manifest.id,
                            "注册闸 G2（复验）：观测恢复，校验通过"
                        );
                    }
                }
            });
        }
    }

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
    let _lifecycle_metrics_handle = agentos_api::metrics::spawn_lifecycle_metrics_subscriber(
        hook_bus.clone(),
        kernel_counters.clone(),
    );
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
    let mcp_bridge = Arc::new(agentos_plugin_loader::McpBridge::new(
        invoker.clone() as Arc<dyn agentos_core::traits::PluginInvoker>
    ));
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

    // boot-plugin 第一刀：db-admin capability（SQL 能力层留内核，HTTP 面在
    // plugins/shared/db_admin 插件）。handler 直连 SqliteStore（cdylib 级信任件，
    // 无 IPC），鉴权在 handler 内核侧执行（插件仅转发 Authorization 头，见
    // db-admin/src/capability.rs 模块文档）。注册先于任何 sidecar spawn——
    // initialize 握手的 build_declared_capabilities_from_namespaces 据此把
    // db-admin 声明给 db_admin 插件（SDK 创建 CapabilityHandle）。
    // db-admin 的 db 句柄按 driver 注入：非 SQLite driver 为 None（handler 的
    // get_db 返回"统一数据接口未启用"400，诚实降级）。
    handler_registry.register(std::sync::Arc::new(
        agentos_db_admin::DbAdminCapabilityHandler::new(Some(store_dyn.clone()), sqlite_db.clone()),
    ));
    info!(
        target: "agentos-kernel",
        "Registered db-admin capability handler (7 methods, SQL layer in-kernel, HTTP face in db_admin plugin)"
    );

    // boot-plugin 第二刀：user-admin capability（用户管理**策略面**留内核，
    // HTTP 面在 plugins/shared/user_admin 插件）。§9.6 精确拆分：auth 执行门
    // （login/logout/me/register/refresh 的验签与路由准入）永留内核（auth.rs
    // 一行不动）；本 handler 只承载管理性质操作（list_users/update_role/
    // update_tenant/delete_user——内核此前无这些端点，直接以插件化形态新建）。
    // 鉴权与 self-service 防护（admin 不能删自己/降自己角色/改自己租户）在
    // handler 内核侧执行（插件仅转发 Authorization 头，见
    // user-admin/src/capability.rs 模块文档）。注册先于任何 sidecar spawn——
    // initialize 握手据此把 user-admin 声明给 user_admin 插件。
    // update_role/update_tenant 的 db 句柄按 driver 注入：非 SQLite driver 为
    // None（handler 诚实降级 400）；list/delete 走 StorageBackend trait（跨 driver）。
    handler_registry.register(std::sync::Arc::new(
        agentos_user_admin::UserAdminCapabilityHandler::new(
            Some(store_dyn.clone()),
            sqlite_db.clone(),
        ),
    ));
    info!(
        target: "agentos-kernel",
        "Registered user-admin capability handler (4 methods, user-management policy layer in-kernel, HTTP face in user_admin plugin)"
    );

    // boot-plugin 第三刀：metrics-admin capability（指标读面留内核，HTTP 面在
    // plugins/shared/metrics_admin 插件）。写面 metrics.record（插件上报指标的
    // 热路径反向调用）仍是 KernelCapabilityRouter 内置 match，不经此 handler。
    // 聚合器与 router/AppState 共享同一实例（Clone 内部 Arc），查询读到实时数据。
    // 鉴权（admin/viewer 读面）在 handler 内核侧执行（插件仅转发 Authorization
    // 头，见 metrics/capability.rs 模块文档）。/metrics（Prometheus 抓取）作为
    // 运维契约保留内核路由，不经插件。
    handler_registry.register(std::sync::Arc::new(
        agentos_api::metrics::MetricsAdminCapabilityHandler::new(
            Some(store_dyn.clone()),
            Some(metrics_aggregator.clone()),
        ),
    ));
    info!(
        target: "agentos-kernel",
        "Registered metrics-admin capability handler (3 methods: query/list/prometheus, read layer in-kernel, HTTP face in metrics_admin plugin)"
    );

    // G6：granted_capabilities 白名单查询器——声明非空即白名单制，未声明默认
    // 全授予（存量插件零迁移）。执行点在 KernelCapabilityRouter::handle 单点，
    // sidecar（PluginScopedRouter 注 _plugin_id）与 native（NativeHostServices 注
    // _plugin_id）同判。
    let loader_for_grants = loader_arc.clone();
    let grants_lookup: agentos_api::capability_router::GrantsLookupFn =
        Arc::new(move |plugin_id| {
            loader_for_grants.get_manifest(plugin_id).and_then(|m| {
                if m.granted_capabilities.is_empty() {
                    None
                } else {
                    Some(m.granted_capabilities.clone())
                }
            })
        });

    // G3：动态工具注册器——enablement 闸 + 写入注册表（M1 guarded 入 scope，
    // disable 即结构性收回）。2026-08-19 用户裁定：dynamic_tools 表退役——
    // 动态注册的工具是 state 域数据不落内核存储，跨重启重建由插件自持
    // state/config 承担（registry 内存注册机制与 capability 面不变）。
    // 信封闸（granted 须含 "registry"）已由 router 入口的 G6 单点校验覆盖。
    // enabled 集合提前构造（后续 AppState 复用同一 Arc）。
    let enabled_plugin_ids: Arc<tokio::sync::RwLock<std::collections::HashSet<String>>> =
        Arc::new(tokio::sync::RwLock::new(
            manifests
                .iter()
                .filter(|m| enablement.is_enabled(&m.id, m.enabled))
                .map(|m| m.id.clone())
                .collect(),
        ));
    let dynamic_registrar: agentos_api::capability_router::DynamicToolRegistrar = {
        let registry_for_dyn = registry.clone();
        let scopes_for_dyn = plugin_scopes.clone();
        let enabled_for_dyn = enabled_plugin_ids.clone();
        Arc::new(
            move |plugin_id: &str, tool: agentos_core::traits::ToolDescriptor| {
                // enablement 闸：disabled 插件不得注册。try_read 竞争失败时宽容放行
                // （注册低频，禁用竞态毫秒级窗口可接受；错误方向是"多注册一次"而非丢注册）。
                if let Ok(ids) = enabled_for_dyn.try_read() {
                    if !ids.contains(plugin_id) {
                        return Err(format!(
                            "plugin '{}' is disabled (L1 Enabled 闸)",
                            plugin_id
                        ));
                    }
                }
                // 写入注册表（guarded：guard 入 scope，禁用插件时一次性收回）。
                scopes_for_dyn
                    .scope_of(plugin_id)
                    .track(registry_for_dyn.register_tool_guarded(plugin_id, tool.clone()));
                Ok(())
            },
        )
    };

    // 域事件广播闭包：capability_router 收到域事件名单（approval.created）时
    // 投递给声明 domain_event 的启用插件。manifests 用共享 RwLock（AppState
    // 构造后替换为同一副本，watcher 热发现同步可见）。
    let manifests_shared: Arc<tokio::sync::RwLock<Vec<agentos_core::traits::PluginManifest>>> =
        Arc::new(tokio::sync::RwLock::new(manifests.clone()));
    let domain_broadcaster: agentos_api::capability_router::DomainBroadcaster = {
        let inv_for_domain: Arc<dyn agentos_core::traits::PluginInvoker> = invoker.clone();
        let enabled_for_domain = enabled_plugin_ids.clone();
        let manifests_for_domain = manifests_shared.clone();
        Arc::new(
            move |event_name: &str, tags: Vec<(&str, serde_json::Value)>| {
                let inv = inv_for_domain.clone();
                let enabled = enabled_for_domain.clone();
                let manifests = manifests_for_domain.clone();
                let name = event_name.to_string();
                tokio::spawn(async move {
                    agentos_api::plugin_lifecycle::broadcast_domain_event_from(
                        &inv, &enabled, &manifests, &name, tags,
                    )
                    .await;
                });
            },
        )
    };

    // 流式声明查询闭包（ADR 2026-08-22）：capability_router 收到流式事件时查
    // 插件 capabilities.streaming 声明（未声明即拒，fail-closed）。manifests 用
    // 共享 RwLock（与 domain_broadcaster 同源）——watcher 热发现同步可见。
    let streaming_declaration_lookup: agentos_api::capability_router::StreamingDeclarationLookupFn = {
        let manifests_for_streaming = manifests_shared.clone();
        Arc::new(move |plugin_id: &str| {
            let guard = manifests_for_streaming.try_read().ok()?;
            guard
                .iter()
                .find(|m| m.id == plugin_id)
                .and_then(|m| m.capabilities.streaming.clone())
        })
    };

    let router = Arc::new(
        KernelCapabilityRouter::with_metrics(metrics_aggregator.clone())
            .with_invoker(invoker.clone())
            .with_registry(registry.clone())
            .with_session(session_coord.clone())
            .with_store(store.clone())
            .with_handler_registry(handler_registry.clone())
            .with_grants_lookup(grants_lookup)
            .with_dynamic_tool_registrar(dynamic_registrar.clone())
            .with_domain_broadcaster(domain_broadcaster)
            .with_streaming_declaration_lookup(streaming_declaration_lookup)
            .with_capability_contracts(capability_contracts.clone())
            // 工具连续失败告警器（2026-08-23）：挂默认实现，统一经 invoke 结果
            // 归一化点计数（见 capability_router handle 的 tool-executor 分支）。
            .with_tool_failure_tracker(Arc::new(
                agentos_api::tools::ConsecutiveFailureTracker::default(),
            )),
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

    let pipeline_config = load_pipeline_config(&config_root).unwrap_or_else(|e| {
        warn!(
            "加载管道配置失败，以内置默认配置启动（chat 走降级路径，修复配置后热重载自动生效）: {}: {}",
            config_root.display(),
            e
        );
        agentos_core::types::PipelineConfig::default()
    });
    let step_library = load_step_library(&config_root).unwrap_or_else(|e| {
        warn!(
            "加载公共 step 库失败，以空库启动（修复配置后热重载自动生效）: {}: {}",
            config_root.display(),
            e
        );
        agentos_core::types::StepLibrary::default()
    });

    info!(
        target: "agentos-kernel",
        "Loaded pipeline '{}' with {} steps, step library with {} entries",
        pipeline_config.name,
        pipeline_config.loop_bodies.len(),
        step_library.steps.len()
    );

    // 收集已知插件 id（命中规则③判定 + 重名检测用）
    let plugin_ids: std::collections::HashSet<String> =
        manifests.iter().map(|m| m.id.clone()).collect();

    // 启动期重名检测：冲突不阻断启动（warn 留痕；运行时热重载每次请求重新
    // 校验并在修复后自动生效——配置问题不应让内核整体不可用）。
    if let Err(conflict) = validate_no_name_conflicts(&pipeline_config, &step_library, &plugin_ids)
    {
        warn!("命名冲突检测失败（内核继续启动，修复配置后热重载自动生效）: {conflict}");
    }

    // G10：加载期编译。when 语法错误 / 引用不存在的 step 或插件 / composite
    // 引用环——不阻断启动：warn 留痕，运行时热重载路径（server.rs
    // maybe_reload_compiled_pipeline）在每次请求前重新加载+编译，配置修复后
    // 自动生效；修复前 chat 走空管道降级（与"缺省配置下内核可启动"一致）。
    if let Err(compile_err) = agentos_api::server::load_and_compile(&config_root, &plugin_ids) {
        warn!("管道加载期编译失败（内核继续启动，修复配置后热重载自动生效）: {compile_err}");
    }

    // 构建 AppState（注入 pipeline_config / step_library / invoker / store / plugin_ids / project_root）
    let project_root = config_root
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."));
    let store_dyn: Arc<dyn agentos_core::traits::StorageBackend> = store.clone();
    let invoker_dyn: Arc<dyn agentos_core::traits::PluginInvoker> = invoker.clone();
    // P3：HTTP 端点 dispatcher 的生产 handler（经 invoker 调插件 http.handle）
    let http_handler: Arc<dyn agentos_core::traits::HttpHandleCapability> = Arc::new(
        agentos_api::http_dispatcher::SidecarHttpHandler::new(invoker_dyn.clone()),
    );
    // L1 启用集合（schema 据此过滤 contributes）——G3 时已提前构造（router 的
    // enablement 闸共享同一 Arc），此处仅取快照喂 with_plugins（其签名收 HashSet）。
    let enabled_snapshot: std::collections::HashSet<String> =
        enabled_plugin_ids.read().await.clone();
    let mut state = AppState::with_plugins(
        manifests.clone(),
        registry,
        Arc::new(pipeline_config),
        Arc::new(step_library),
        invoker_dyn,
        store_dyn,
        plugin_ids,
        project_root,
        enabled_snapshot,
    );
    // 共享 manifests 副本（域事件广播闭包与 watcher 热发现读同一份）。
    state.manifests = manifests_shared.clone();
    // task_01：注入统一数据接口专用 SqliteStore 句柄（/api/v1/db/* 用，表驱动动态枚举）。
    // 与 store_dyn（trait object，业务语义方法）互补；with_db 不改任何持久化方式。
    // with_db 按 driver 注入（sqlite/memory → Some；其它 driver → None，
    // 统一数据接口与 G8 排空的 SQLite 专有路径诚实降级）。
    let state = match sqlite_db.clone() {
        Some(db) => state.with_db(db),
        None => state,
    };
    let state = state.with_http_handler(http_handler);
    // 内核能力契约注入 AppState（/api/v1/schema 聚合透出——消费端同源）。
    let state = state.with_kernel_capability_contracts(capability_contracts.clone());
    // 注入插件根目录映射，启用静态资源托管
    // （/ext/{plugin_id}/assets/{*path} → <plugin_dir>/web/<path> 直读）。
    // 插件只需在自己的目录下放 web/ 子目录即可被内核自动托管，无需声明 http_endpoints。
    let state = state.with_plugin_dirs(plugin_dirs);
    // 统一配置加载方案 TDD-4：构造 ConfigCenter 注入 AppState。
    // 后续 loader（agent/pipeline/plugin config_files）经此统一走 load()/load_dir()/store()。
    let state = if let Some(root) = state.project_root.as_ref() {
        let cc = std::sync::Arc::new(agentos_config::config_center::ConfigCenter::new(
            root.join("config"),
        ));
        state.with_config_center(cc)
    } else {
        state
    };
    // ADR §3.5'：插件 widget 绑定表（共享化，M1）。此处先建表注入 AppState，
    // broadcaster 在 session 启用后 spawn（见下方 Metrics 后台任务段）。
    let widget_bindings_shared: agentos_api::metrics::SharedBindings = {
        let entries: Vec<(&str, Option<&serde_json::Value>)> = manifests
            .iter()
            .map(|m| (m.id.as_str(), m.contributes.as_ref()))
            .collect();
        Arc::new(parking_lot::RwLock::new(
            agentos_api::metrics::collect_all_bindings(entries),
        ))
    };
    // M1：每个有绑定的插件登记一条 broadcaster 维度 guard（revoke = 移除其全部绑定，
    // 禁用插件时随 scope 结构性收回）。
    {
        let owners: std::collections::HashSet<String> = widget_bindings_shared
            .read()
            .iter()
            .map(|b| b.owner_plugin_id.clone())
            .collect();
        for owner in owners {
            let shared = Arc::clone(&widget_bindings_shared);
            plugin_scopes
                .scope_of(&owner)
                .track(agentos_core::traits::RegistrationGuard::new(move || {
                    agentos_api::metrics::plugin_widget_broadcast::remove_plugin_bindings(
                        &shared, &owner,
                    );
                }));
        }
    }
    let state = state
        // 监控 M1/M5/M5b：注入指标聚合器（启用 /api/v1/metrics + /metrics 端点）
        .with_metrics(metrics_aggregator.clone())
        // M1：注册账本 + widget 绑定表（disable 结构性收回）
        .with_plugin_scopes(plugin_scopes.clone())
        .with_widget_bindings(Arc::clone(&widget_bindings_shared));
    // P2：启用会话内核（WS 握手鉴权 + 连接注册 + 入站路由 + 断线重放）。
    // 复用 router 已持有的 session_coord（流式 chunk 推送与 WS 出站共享同一 SessionCoordinator）。
    let state = state.enable_session_with(session_coord);

    // chat namespace capability：把"向会话投递消息并跑管道"暴露给 sidecar。
    // 触发器（trigger_setup_tool）到期触发时经 chat.send_message 复用前端同一条
    // WS 派发（dispatch_user_input → process_via_engine）。0.1 的 pipeline.message_bus
    // 在 0.2 已删，sidecar 此前只能推展示事件、不能唤醒 agent；本 handler 补上注入通道。
    // AppState 在 router 之后构造，故在此（启动末期）注册到既有 handler_registry。
    {
        let dispatcher: std::sync::Arc<dyn agentos_session::router::PipelineDispatcher> =
            std::sync::Arc::new(agentos_api::ws_session::EngineDispatcher::new(
                state.clone(),
            ));
        handler_registry.register(std::sync::Arc::new(
            agentos_api::chat_send_handler::ChatSendHandler::with_store(
                dispatcher,
                state.store.clone(),
            ),
        ));
        info!(target: "agentos-kernel", "Registered chat.send_message capability (trigger fire path)");
    }

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
        // M1：绑定表已共享化注入 AppState（禁用插件时从表移除其绑定，guard 已挂 scope）。
        let widget_bindings = match state.widget_bindings.as_ref() {
            Some(b) => Arc::clone(b),
            None => Arc::new(parking_lot::RwLock::new(Vec::new())),
        };
        if !widget_bindings.read().is_empty() {
            let agg_widget: Arc<agentos_api::metrics::MetricsAggregator> =
                Arc::new(metrics_aggregator.clone());
            let session_widget: Arc<dyn agentos_api::metrics::WidgetEmitter> = session.clone();
            let _widget_bcast_handle = agentos_api::metrics::PluginWidgetBroadcaster::spawn(
                agg_widget,
                Arc::clone(&widget_bindings),
                session_widget,
            );
            info!(
                target: "agentos-kernel",
                count = widget_bindings.read().len(),
                "PluginWidgetBroadcaster started ({} metric_bindings)",
                widget_bindings.read().len()
            );
        }
        info!(target: "agentos-kernel", "Metrics background tasks started (M2 flush + M1 rollup + M6 broadcast, 1s interval)");
    }

    // 插件运行时自动发现（notify watch + 轮询兜底）：往 plugins/ 丢新插件目录即生效，
    // 无需重启内核、无需手动调 reload-all。复用启动期已构造的 invoker / registry；
    // initial_ids 取启动 manifests，避免把已注册插件重复注册（与 reload-all 新插件序列对齐）。
    // 注入 enablement profile：热发现路径同样按 L1 过滤 disabled 插件（注册闸对齐启动期）。
    //
    // 关键前置：discover_new_plugins 内部读 AGENTOS_PLUGINS_DIR 推导 roots。启动期若走
    // 默认 plugins_dir（未设该环境变量），须在此补设，保证 watcher 监听目录与 invoker
    // 发现目录同源——否则 watcher 触发同步、discover 却读到空 roots、发现不到新插件。
    if std::env::var("AGENTOS_PLUGINS_DIR").is_err() {
        std::env::set_var("AGENTOS_PLUGINS_DIR", &plugins_dir);
        info!(
            target: "agentos-kernel",
            "AGENTOS_PLUGINS_DIR unset; defaulting to {} for hot-discover",
            plugins_dir.display()
        );
    }
    {
        let watcher_invoker: Arc<dyn agentos_core::traits::PluginInvoker> =
            state.invoker.clone().expect("invoker present at boot");
        let watcher_registry: Arc<CapabilityRegistryImpl> = state
            .capability_registry
            .clone()
            .expect("capability_registry present at boot");
        let initial_ids: std::collections::HashSet<String> =
            manifests.iter().map(|m| m.id.clone()).collect();
        // A3：cdylib 集合基线（boot manifests 的 InProcess id）——watcher 首轮
        // sync 即可 diff，能捕捉 boot→首轮 sync 窗口内的装/卸。
        let initial_cdylib_ids: std::collections::HashSet<String> = manifests
            .iter()
            .filter(|m| m.host_type == agentos_core::traits::HostType::InProcess)
            .map(|m| m.id.clone())
            .collect();
        // A3：cdylib 集合变更重启回调——复用 G8 排空+退出（在途 runs → suspended
        // 后 exit 75，监督者拉起新进程）；AGENTOS_DISABLE_SELF_EXIT=1 逃生门在
        // 函数内部生效（只排空不退出）。watcher 经 env 开关
        // AGENTOS_AUTO_RESTART_ON_CDYLIB_CHANGE（默认开，0 关）自行把关。
        let hook_db = state.db.clone();
        let hook_invoker = state.invoker.clone();
        let restart_hook: Arc<dyn Fn() + Send + Sync> = Arc::new(move || {
            let db = hook_db.clone();
            let invoker = hook_invoker.clone();
            tokio::spawn(async move {
                agentos_api::routes::drain_and_exit75(
                    db.as_ref(),
                    invoker,
                    "plugin_watcher: InProcess(cdylib) plugin set changed",
                )
                .await;
            });
        });
        let _watcher_handle = agentos_api::plugin_watcher::PluginWatcher::new(
            plugins_dir.clone(),
            watcher_invoker,
            watcher_registry,
            initial_ids,
        )
        .with_scopes(plugin_scopes.clone())
        .with_initial_cdylib_ids(initial_cdylib_ids)
        .with_restart_hook(restart_hook)
        .with_manifests_store(state.manifests.clone())
        .with_enablement(enablement.clone())
        // 2026-08-23：enablement 每次 sync 从盘上 profile 现读——boot 快照看不到
        // 运行期 PUT enabled 的写盘结果，卸载→重装按旧快照会把已禁用插件重新
        // 注册（运行期禁用被静默撤销，e2e test_07 实测）。
        .with_profile_reload(config_root.clone())
        .spawn();
        info!(target: "agentos-kernel", "Plugin hot-discover watcher spawned (notify + polling fallback; cdylib change -> G8 auto-restart)");
    }

    start_server(addr, state).await?;

    Ok(())
}

/// 解析用户插件根目录（可写，第三方插件安装位置）。
///
/// 解析优先级：
/// 1. 环境变量 `AGENTOS_USER_PLUGINS_DIR`（与 `AGENTOS_PLUGINS_DIR` 命名风格一致）
/// 2. `dirs::data_dir().join("agentos").join("plugins")`
///    （Win=`%APPDATA%/agentos/plugins`，macOS=`~/Library/Application Support/agentos/plugins`，
///    Linux=`~/.local/share/agentos/plugins`）
/// 3. 均不可用则返回 `None`（保持原行为：不启用 user_root）
///
/// 注意使用 `dirs::data_dir()` 而非 `data_local_dir()`（后者是 `%LOCALAPPDATA%`，
/// 不随用户漫游，不适合作为第三方插件安装位置）。
fn resolve_user_plugins_dir() -> Option<PathBuf> {
    // 1. 环境变量
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
/// loader 必须带 `.with_config_root(config_root)` 接入配置根目录——漏接会使
/// `load_config()` 恒返回空 `{}`，插件收不到任何配置。从 `main` 抽出便于
/// 单测验证「config_root 已接到 loader」。
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
    // P0-2：allowlist 生产接线——config/system/plugin_allowlist.yaml 从"空挂"变真准入
    // （permissive 默认：放行 + 条目 sha256 校验，真实语料零误伤；strict 由部署方显式
    // 启用：白名单外插件 load 失败 fail-closed，与 deny_unknown_fields 一致）。
    let allowlist = agentos_plugin_loader::load_allowlist_file(
        &config_root.join("system/plugin_allowlist.yaml"),
    );
    PluginLoaderImpl::new(plugins_dir, user_plugins_dir)
        // 接入 config_root：否则 load_config() 因 config_root=None 恒返回空 {}
        .with_config_root(config_root)
        .with_allowlist(allowlist)
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

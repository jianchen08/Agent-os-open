//! AgentOS 0.2 内核二进制入口。
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
//! - AGENTOS_BIND：监听地址（默认 127.0.0.1；显式设 0.0.0.0 开启外网可达，
//!   AGENTOS_KERNEL_HOST 为过渡期别名，弃用中）
//! - AGENTOS_ADMIN_PASSWORD：内置 admin 口令（未设置时首启生成随机口令并打印
//!   一次；对已存在的 admin 设置不同口令即重置）
//! - AGENTOS_TOKEN_SECRET：token 签名密钥（未设置时进程随机，重启即全量失效）
//! - AGENTOS_PLUGINS_DIR：内置插件根目录（默认 plugins/shared）
//! - AGENTOS_USER_PLUGINS_DIR：用户插件根目录（默认 OS 标准目录 agentos/plugins）
//! - AGENTOS_MAX_BLOCKING_THREADS：tokio 阻塞池上限（默认 512 = tokio 原生默认）
//! - AGENTOS_THREAD_STACK_KIB：线程栈大小 KiB（默认 2048 = tokio 默认 2MiB）
//! - AGENTOS_MEM_WATERMARK_MB：水位自愈阈值 MB（**默认关闭**——2026-09-10
//!   用户裁定水位重启不得默认触发；显式正值才启用排空重启自愈）
//! - AGENTOS_MEM_WATERMARK_MIN：水位需持续的分钟数（启用时默认 10）
//! - AGENTOS_TRACE_RETENTION_DAYS：traces 保留天数（默认 90；0 = 禁用清扫；
//!   超出上限按上限钳制，解析失败回落默认）——traces 过期行与孤儿 blob 由
//!   保留清扫周期回收（启动清一次 + 每 6h）

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

/// 管理员初始口令来源（D1）：
/// - 环境变量 `AGENTOS_ADMIN_PASSWORD` 非空 → 显式指定；
/// - 未设置 → 生成随机口令（128 bit uuid 对），仅在首次播种时打印一次到控制台。
///
/// 不存在任何硬编码默认口令。
fn resolve_admin_password() -> (String, bool) {
    match std::env::var("AGENTOS_ADMIN_PASSWORD") {
        Ok(p) if !p.trim().is_empty() => (p, false),
        _ => {
            let random = format!(
                "{}{}",
                uuid::Uuid::new_v4().simple(),
                uuid::Uuid::new_v4().simple()
            );
            (random, true)
        }
    }
}

/// 播种内置 admin 用户（首次启动插入，已存在则跳过；显式环境变量可重置）。
///
/// tenant_id = "default"（与多租户隔离地基的默认租户一致），保证旧数据
/// （0.5.0 前以 default 写入的会话/消息）仍归 admin 可见。
///
/// D1：口令 argon2id 哈希落库，播种账号 `must_change_password = true`
/// （login 响应携带标记，前端拦截强制改密）。对已存在的 admin，若
/// `AGENTOS_ADMIN_PASSWORD` 与当前口令不符即重置（操作员恢复通道：
/// 迁移失败/口令遗失时设置该变量重启）。
async fn seed_admin_user(store: Arc<dyn agentos_core::traits::StorageBackend>) {
    const ADMIN_ID: &str = "00000000-0000-0000-0000-000000000001";
    let (password, generated) = resolve_admin_password();
    match store.get_user_by_id(ADMIN_ID).await {
        Ok(Some(existing)) => {
            // admin 已存在。环境变量提供且与当前口令不符 → 显式重置。
            if !generated && !agentos_http::auth::verify_password(&password, &existing.password) {
                let password_hash =
                    agentos_http::auth::hash_password(&password).unwrap_or_else(|e| {
                        panic!("AGENTOS_ADMIN_PASSWORD 重置口令哈希失败（fail-closed）: {e}")
                    });
                match store
                    .update_user_password(ADMIN_ID, &password_hash, false)
                    .await
                {
                    Ok(true) => warn!(
                        target: "agentos-kernel",
                        "已按 AGENTOS_ADMIN_PASSWORD 重置内置 admin 口令（旧会话全部失效）"
                    ),
                    Ok(false) => {
                        warn!(target: "agentos-kernel", "重置 admin 口令失败：用户行不存在")
                    }
                    Err(e) => warn!(target: "agentos-kernel", error = %e, "重置 admin 口令失败"),
                }
            }
        }
        Ok(None) => {
            // 首次启动：插入 admin 种子用户。
            // get_user_by_id 按 task_local tenant（此处为空→default）查，admin 的
            // tenant_id 正是 default，所以 None 表示确实没播过种。
            let password_hash = agentos_http::auth::hash_password(&password)
                .unwrap_or_else(|e| panic!("播种口令哈希失败（fail-closed）: {e}"));
            let now = chrono::Utc::now().to_rfc3339();
            let admin = UserRecord {
                user_id: ADMIN_ID.to_string(),
                username: "admin".to_string(),
                password: password_hash,
                email: Some("admin@agentos.dev".to_string()),
                role: "admin".to_string(),
                tenant_id: "default".to_string(),
                created_at: now,
                last_login_at: None,
                must_change_password: true,
            };
            match store.create_user(&admin).await {
                Ok(()) => {
                    if generated {
                        // 随机口令仅首启打印一次（控制台直出，不经日志管线过滤）
                        println!("============================================================");
                        println!("  已播种内置 admin 用户 (tenant=default)");
                        println!("  本次初始口令（仅此一次显示，请立即登录并修改）:");
                        println!("    {password}");
                        println!("============================================================");
                    } else {
                        info!(target: "agentos-kernel",
                            "已播种内置 admin 用户 (tenant=default)，初始口令取自 AGENTOS_ADMIN_PASSWORD")
                    }
                }
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

/// 启动迁移：存量明文口令行 argon2id 哈希化回写（D1-2）。
///
/// 判据：password 列非 `$argon2` 前缀即视为待迁移明文。任一行哈希或回写失败
/// → Err 拒绝启动（fail-closed，不留明文兼容路径）——操作员按提示设置
/// `AGENTOS_ADMIN_PASSWORD` 重启重置，或修复库后重试。
async fn migrate_plaintext_passwords(
    store: Arc<dyn agentos_core::traits::StorageBackend>,
) -> Result<(), String> {
    let users = store
        .list_users()
        .await
        .map_err(|e| format!("迁移前读取用户表失败: {e}"))?;
    let mut migrated = 0usize;
    for u in users {
        if agentos_http::auth::is_password_hash(&u.password) {
            continue;
        }
        let password_hash = agentos_http::auth::hash_password(&u.password)?;
        let updated = store
            .update_user_password(&u.user_id, &password_hash, u.must_change_password)
            .await
            .map_err(|e| format!("用户 {} 口令哈希回写失败: {e}", u.username))?;
        if !updated {
            return Err(format!("用户 {} 口令回写未命中（行消失？）", u.username));
        }
        migrated += 1;
        warn!(
            target: "agentos-kernel",
            user = %u.username,
            "存量明文口令已哈希化回写（D1 启动迁移）"
        );
    }
    if migrated > 0 {
        info!(target: "agentos-kernel", migrated, "明文口令迁移完成");
    }
    Ok(())
}

/// 解析监听地址（D12）：默认 127.0.0.1，仅本机可达；
/// `AGENTOS_BIND=0.0.0.0` 显式开启外网可达（打印绑定面告警）。
/// `AGENTOS_KERNEL_HOST` 为弃用中的过渡别名。
fn resolve_bind_host() -> String {
    match std::env::var("AGENTOS_BIND") {
        Ok(h) if !h.trim().is_empty() => h,
        _ => match std::env::var("AGENTOS_KERNEL_HOST") {
            Ok(h) if !h.trim().is_empty() => {
                warn!(
                    target: "agentos-kernel",
                    "AGENTOS_KERNEL_HOST 已弃用，请改用 AGENTOS_BIND（本进程按其值继续启动）"
                );
                h
            }
            _ => "127.0.0.1".to_string(),
        },
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // 线程治理：#[tokio::main] 宏暴露不了 max_blocking_threads / thread_stack_size，
    // 手动构建等价 multi-thread runtime（enable_all = 宏默认；worker_threads 不设
    // = 核数，同为宏默认）。启动逻辑顺序不变（原 main 体整体移入 async_main）。
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .max_blocking_threads(resolve_max_blocking_threads())
        .thread_stack_size(resolve_thread_stack_size())
        .build()?;
    runtime.block_on(async_main())
}

async fn async_main() -> Result<(), Box<dyn std::error::Error>> {
    // 全局分配器（mimalloc + purge_delay=0）：必须在任何分配前安装。
    // Windows 段堆并发高水位滞留修复（ADR 2026-08-31-mimalloc-global-allocator）。
    agentos_api::allocator::install_global_allocator();

    // 日志契约（2026-09-15 单日志面）：文件层是唯一常驻日志——tracing-appender
    // daily 轮转写 logs/kernel.log.YYYY-MM-DD（相对进程工作目录），保留上限
    // 30 份（D3：daily 轮转无限累积会让 logs/ 目录随运行时长无界增长）。
    // 轮转口径为纯 UTC（BUG-71 裁决 2026-09-23）：文件名 = 写入时刻的 UTC 日期，
    // 轮转边界 = UTC 午夜 = 本地 08:00（UTC+8）；本地日 X 的日志行因此横跨两个
    // 文件，且本地午夜 00:00 时最新文件仍叫 X-1——按本地日期找文件会扑空，外部
    // 判活统一走 logs/kernel.liveness（见 log_liveness）。
    // stdout 不再双写（supervisor 侧重定向已退役：OS 级追加重定向是无界
    // 增长面，内核只持有无路径的 fd、无法自轮替）；仅当文件层构建失败
    // （只读盘/无目录权限）时才挂 stdout 层降级——此时它是唯一诊断面。
    let (file_layer, log_guard) = match tracing_appender::rolling::RollingFileAppender::builder()
        .rotation(tracing_appender::rolling::Rotation::DAILY)
        .filename_prefix("kernel.log")
        // D3：保留上限 30 份——daily 轮转无限累积会让 logs/ 目录随运行时长
        // 无界增长（长期驻留内核的磁盘治理面）。
        .max_log_files(30)
        .build("logs")
    {
        Ok(appender) => {
            // 活性面（BUG-71 裁决）：成功写打点、写失败显式告警（上游 worker 对
            // IO 错误全静默）、持续静默超阈值报警并停止刷新 logs/kernel.liveness。
            // 告警面 stderr 在装机部署被丢弃（electron stdio=ignore），标记文件
            // 是外部判活权威面。
            let liveness = agentos_api::log_liveness::LogLiveness::new();
            let watched = agentos_api::log_liveness::LivenessWriter::new(
                appender,
                liveness.clone(),
                Arc::new(|msg: String| eprintln!("{msg}")),
                agentos_api::log_liveness::ERR_REPORT_RATE_LIMIT,
            );
            agentos_api::log_liveness::spawn_silence_watchdog(
                liveness,
                std::path::Path::new("logs"),
                agentos_api::log_liveness::WatchdogConfig::production(),
                Arc::new(|msg: String| eprintln!("{msg}")),
            );
            let (writer, guard) = tracing_appender::non_blocking(watched);
            (
                Some(
                    fmt::layer()
                        .with_target(false)
                        .with_ansi(false)
                        .with_writer(writer),
                ),
                Some(guard),
            )
        }
        Err(e) => {
            eprintln!("[boot] 日志文件轮转初始化失败（仅 stdout 日志可用）: {e}");
            (None, None)
        }
    };
    // guard 必须活到进程退出（提前丢 guard = 缓冲日志丢失），绑定在 async_main 帧
    let _log_guard = log_guard;

    // stdout 降级层：仅文件层缺席时挂载（None = 不挂载）。正常运行全部日志
    // 只走文件层；boot eprintln 与 panic 不走 tracing，不经此层。
    let stdout_fallback_layer = if file_layer.is_none() {
        Some(fmt::layer().with_target(false))
    } else {
        None
    };

    tracing_subscriber::registry()
        .with(file_layer)
        .with(stdout_fallback_layer)
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let host = resolve_bind_host();
    let port: u16 = std::env::var("AGENTOS_KERNEL_PORT")
        .unwrap_or_else(|_| "9100".into())
        .parse()
        .unwrap_or(9100);

    let addr: SocketAddr = format!("{}:{}", host, port).parse()?;
    if !addr.ip().is_loopback() {
        warn!(
            target: "agentos-kernel",
            bind = %addr,
            "内核绑定在非回环地址：HTTP/WS API 外网可达。仅限可信网络部署"
        );
    }

    info!(target: "agentos-kernel", "========================================");
    info!(target: "agentos-kernel", "  AgentOS 0.2 内核启动");
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

    // 模式种子对账（设计稿 2026-09-15 §2「版本管理」）：必须在插件扫描之前——
    // 播种/静默升级要先落盘，discover 才能扫到正确的用户副本内容。
    let seed_outcomes = reconcile_mode_seeds_at_boot(&plugins_dir, user_plugins_dir.as_deref());

    // 用户仓 git 化（设计稿 2026-09-15 §2.2「用户目录 git 化边界」）：对账完成后
    // 确保用户仓就绪；对账有落盘变化时自动提交一笔（用户自己的改动不自动提交）。
    init_user_repo_at_boot();
    commit_seed_reconciliation_at_boot(&seed_outcomes);

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

    // 内核能力契约加载（定义驱动入口校验 + schema 聚合的
    // 单一真值源）。目录缺失 = 未启用（宽泛放行）；文件损坏 = fail-fast 拒启
    // ——契约是校验器的眼睛，坏契约静默跳过等于校验器装瞎。
    let capability_contracts: Arc<Vec<agentos_api::kernel_capabilities::KernelCapabilityContract>> =
        Arc::new(
            agentos_api::kernel_capabilities::load_contracts(
                &config_root.join("kernel/kernel_capabilities"),
            )
            .unwrap_or_else(|e| panic!("内核能力契约文件加载失败（fail-closed）: {e}")),
        );
    info!(
        target: "agentos-kernel",
        namespaces = capability_contracts.len(),
        "Loaded kernel capability contracts (definition-driven entry validation)"
    );

    // 加载 .env 到进程环境（用户空间优先，回落项目根）。
    // sidecar 子进程默认继承父进程环境变量（tokio Command 无 env_clear），
    // 这样 sidecar 能解析配置里的 ${API_KEY} 等占位符（ADR §4.3 secrets）。
    // 仅设置进程未已有的变量（系统环境变量优先于 .env）。
    //
    // 用 mcp::env_file::env_path_for_root 而非自己拼 project_root/.env：
    // 写侧（设置页填 key）走同一函数，两侧必须同源——否则会出现"key 写进
    // 用户空间、启动读的还是项目根"这类静默分叉。
    if let Some(project_root) = config_root.parent() {
        let env_path = agentos_mcp::env_file::env_path_for_root(project_root);
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
    // 用户模式包根先于 move 取出（模式包目录扫描注册要用，见下方注册段）。
    let user_modes_dir = user_plugins_dir.as_deref().map(|p| p.join("modes"));
    // 启动扫描根 = 内置根 + 用户根（与热路径 discover_new_plugins 同源）：模式包在
    // `modes/<mode_id>/plugin.json` 二级嵌套，discover 内建的用户根扫描（一级
    // 子目录）看不到；只传内置根会让启动装载 repo 份，而热路径同 id 解析用户份
    // （loader 用户根子树赢）——首个 sync 周期源码目录翻转触发复验驱逐（/ext
    // 路由空窗）。双根同 id 用户赢必须在启动路径同样成立。
    let root_paths = collect_boot_plugin_roots(&plugins_dir, user_plugins_dir.as_deref());
    let loader = build_plugin_loader(&plugins_dir, user_plugins_dir, &config_root);

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
             所有插件本次启动不注册；修复 config/kernel/default_profile.yaml 后重启"
        );
    }

    // 将 manifest 中声明的工具注册到 CapabilityRegistry
    let mut tool_count = 0usize;
    let mut skipped_disabled = 0usize;
    // K9：缺 input_schema 而以 {} 补注册的工具计数（进启动报告）。
    let mut missing_input_schema = 0usize;
    for manifest in &manifests {
        // L1 Enabled 过滤：disabled 插件不进出口（安装触发模型 §1.1）
        if !enablement.is_enabled(&manifest.id, manifest.enabled) {
            skipped_disabled += 1;
            continue;
        }
        // D.6 槽位拆分：
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

    // 模式包目录扫描注册（设计稿 2026-09-15 §2.3「约定即注册」，内核唯一 additive
    // 接触点）：装载期扫描模式包（出厂 modes/ 与用户 modes/，双根同 id 用户赢）
    // 的 agents/ pipelines/ 约定子目录，注册进包命名空间——agent 键/编排键
    // `mode_X/<stem>`（编排键是管道定义，与运行实例 pipeline_id 概念分离）。
    // 只登记通过 G2 且启用的插件包；约定子目录缺席 = 零注册不报错。
    // fail-closed：yaml 不可解析/结构非法/键冲突 → 拒绝启动（不静默降级）。
    // 消费取数：registry 模式维度（get/list_mode_agents / get/list_mode_pipelines，
    // 经 AppState.capability_registry 可查）；guard 入 plugin_scopes，禁用即同源
    // 消失（与工具/HTTP 路由维度同一套 M1 语义）。
    let registrable_ids: std::collections::HashSet<String> =
        enabled_manifests.iter().map(|m| m.id.clone()).collect();
    let mode_packages = agentos_plugin_loader::scan_mode_package_resources(
        &plugins_dir.join("modes"),
        user_modes_dir.as_deref(),
        &registrable_ids,
    )
    .map_err(|e| {
        eprintln!("[boot] 模式包约定资源扫描失败，拒绝启动: {e}");
        std::io::Write::flush(&mut std::io::stderr()).ok();
        Box::<dyn std::error::Error>::from(format!(
            "mode package resource scan failed at boot: {e}"
        ))
    })?;
    let mut mode_agent_count = 0usize;
    let mut mode_pipeline_count = 0usize;
    for package in mode_packages {
        let scope = plugin_scopes.scope_of(&package.plugin_id);
        mode_agent_count += package.agents.len();
        mode_pipeline_count += package.pipelines.len();
        scope.track(
            agentos_plugin_loader::register_mode_package_guarded(&registry, package).map_err(
                |e| {
                    eprintln!("[boot] 模式包资源键冲突，拒绝启动: {e}");
                    std::io::Write::flush(&mut std::io::stderr()).ok();
                    Box::<dyn std::error::Error>::from(format!(
                        "mode resource registration failed at boot: {e}"
                    ))
                },
            )?,
        );
    }
    info!(
        target: "agentos-kernel",
        "Registered {} mode agents / {} mode pipelines (convention-scan)",
        mode_agent_count, mode_pipeline_count
    );

    // 初始化存储——StorageBackend driver 化（§9.6）：按 config/kernel/storage.yaml 或
    // 环境变量选 driver（sqlite | memory；postgres 留桩），默认 sqlite +
    // 项目根 agentos_kernel.db（AGENTOS_DB_PATH/:memory: 兼容）。
    // 返回双句柄：store_dyn（业务账本 trait 面，runs/messages/traces/blobs/memory/
    // users——换 driver 时完全可用）+ sqlite_db（SQLite 专有 db-admin 表驱动接口，
    // 非 SQLite driver 下为 None → db-admin capability 诚实降级）。
    // 存储是自举必需件 + 审计真相源，driver 编译进内核而非插件轨（§9.6 判据）。
    // resolve_storage_config：config/kernel/storage.yaml 存在但损坏 → Err 拒绝启动
    // （数据正确性优先，不静默落默认库）。
    let storage_cfg = agentos_engine::storage_factory::resolve_storage_config(&config_root)?;
    info!(
        target: "agentos-kernel",
        driver = %storage_cfg.driver,
        sqlite_path = %storage_cfg.sqlite_path,
        "Storage driver resolved (config/kernel/storage.yaml > env > default sqlite)"
    );

    // 迁移护栏（ADR 2026-09-13-unified-user-root §2.5）：默认库位置从项目根迁到
    // 用户空间后，存量部署若原样启动会**开出一个全新的空库**——数据没丢，但用户
    // 看到的是"全没了"，且此后写入都进新库、两边分叉。这里检测该形态并显式告警
    // 给出迁移命令；**不静默自动迁移**（magic 迁移不可观测，失败/半途中断时状态
    // 不明——沿用仓内 migrate_legacy_data_to_default 的显式调用裁定）。
    warn_legacy_db_not_migrated(&storage_cfg, &config_root);

    let (store, sqlite_db) = agentos_engine::storage_factory::open_storage(&storage_cfg)?;
    let store_dyn: Arc<dyn agentos_core::traits::StorageBackend> = store.clone();

    // 播种内置 admin 用户（0.5.0 最小持久化地基）：argon2 哈希落库 +
    // must_change_password=true；存量明文口令行启动迁移哈希化（D1，fail-closed）。
    seed_admin_user(store.clone()).await;
    if let Err(e) = migrate_plaintext_passwords(store.clone()).await {
        eprintln!(
            "[boot] 明文口令迁移失败，拒绝启动: {e}\n\
             处置：设置 AGENTOS_ADMIN_PASSWORD=<新口令> 重启（重置 admin 口令），或修复存储后重试"
        );
        std::io::Write::flush(&mut std::io::stderr()).ok();
        return Err(Box::<dyn std::error::Error>::from(format!(
            "plaintext password migration failed at boot: {e}"
        )));
    }

    // B2：启动时清扫孤儿 run——上次进程崩溃留下的 status='running' 的 run
    // 标记为 failed + 补 ended_at（persist_run_end 未执行的真实表现；不清扫会永远卡
    // running，历史/会话状态悬空）。已结束的 run 不受影响。
    // B2 清扫是 SQLite 专有路径（reap_orphan_runs 固有方法）——非 SQLite driver
    // 跳过（孤儿 run 清扫属启动家政，跳过不影响正确性，仅留状态悬空到下次 sqlite 起时清）。
    if let Some(sqlite) = sqlite_db.as_ref() {
        let reaped = sqlite.reap_orphan_runs().unwrap_or(0);
        if reaped > 0 {
            warn!(target: "agentos-kernel", reaped = reaped, "启动清扫孤儿 run（标记为 failed）");
        }
    }

    // traces/blobs 保留清扫（ADR 2026-09-11）：traces 按
    // AGENTOS_TRACE_RETENTION_DAYS（默认 90，0 = 禁用）保留窗口滚动清除，
    // 孤儿 blob（不被 message_slots 引用）同步回收——无保留机制两表无界增长。
    // purge SQL 与保留期解析在 engine store 侧，此处只接线：interval 首 tick
    // 立即到期 = 启动清一次 + 之后每 6h；关停随 runtime drop 自然退出。
    // 单轮失败 warn 留痕、下周期重试（家政失败不阻断内核）。
    if let Some(sqlite) = sqlite_db.clone() {
        let retention_days = agentos_engine::store::trace_retention_days();
        if retention_days == 0 {
            info!(
                target: "agentos-kernel",
                "traces/blobs 保留清扫禁用（AGENTOS_TRACE_RETENTION_DAYS=0）"
            );
        } else {
            info!(
                target: "agentos-kernel",
                retention_days,
                "traces/blobs 保留清扫启用（启动清一次 + 每 6h 周期）"
            );
            tokio::spawn(async move {
                let mut ticker = tokio::time::interval(std::time::Duration::from_secs(6 * 60 * 60));
                ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
                loop {
                    ticker.tick().await;
                    // retention_days 经 trace_retention_days 钳制（≤36_500 天），
                    // cutoff 必在 DateTime 值域内。
                    let cutoff = chrono::Utc::now()
                        - chrono::Duration::seconds(retention_days as i64 * 86_400);
                    match sqlite.purge_traces_older_than(cutoff).await {
                        Ok(0) => {}
                        Ok(purged) => {
                            info!(target: "agentos-kernel", purged, "trace 保留清扫完成")
                        }
                        Err(e) => warn!(
                            target: "agentos-kernel",
                            error = %e,
                            "trace 保留清扫失败（下周期重试）"
                        ),
                    }
                    match sqlite.purge_orphan_blobs().await {
                        Ok(0) => {}
                        Ok(purged) => {
                            info!(target: "agentos-kernel", purged, "孤儿 blob 清扫完成")
                        }
                        Err(e) => warn!(
                            target: "agentos-kernel",
                            error = %e,
                            "孤儿 blob 清扫失败（下周期重试）"
                        ),
                    }
                }
            });
        }
    }

    // 创建真实插件调用器——按 host_type 透明分发：
    //   Sidecar: 通过 MCP stdio fork Python sidecar 执行插件
    //   InProcess: 经 NativePluginLoader 加载 cdylib 走 C-ABI（放进插件目录即用）
    // 默认配置即可运行。原 Wasm 轨已按两轨终局决策关闭摘除。
    // 在 loader 被 move 进 Arc 之前，先取出插件根目录映射，
    // 后续注入 AppState 启用 /ext/{plugin_id}/assets/** 静态资源托管。
    let plugin_dirs = loader.get_plugin_dirs();
    // venv 自愈（装机形态，BUG-55 方向③，ADR 2026-09-20 决策②）：门开时 boot
    // 后台对缺 venv 解释器的 Python sidecar 跑 `uv sync` 重建——打包资源排除
    // .venv 且装机链没有 dev launcher，不自愈则 sidecar spawn 期永久 fail-closed。
    // 共享合宿宿主（_host）优先重建：合宿成员 spawn 只认它（HOST_VENV_MISSING
    // 无回退）；合宿成员自身 venv 不建（运行期零消费，去重 ADR 2026-09-07）。
    // dev 门不开（重建权在 launcher）。uv 缺席/失败只 warn 降级，不阻断启动。
    if agentos_api::venv_provision::autoprovision_enabled() {
        let targets =
            agentos_api::venv_provision::select_missing_venv_plugins(&manifests, &plugin_dirs);
        let host = agentos_api::venv_provision::select_shared_host_target(&manifests, &plugin_dirs);
        if !targets.is_empty() || host.is_some() {
            info!(
                target: "agentos-kernel",
                count = targets.len(),
                host = host.as_ref().map(|d| d.display().to_string()),
                "检测到缺 venv 解释器的 Python sidecar，boot 后台 uv sync 自愈（共享合宿宿主优先；完成前相应 sidecar spawn 仍会失败）"
            );
            tokio::spawn(agentos_api::venv_provision::provision_and_log(
                targets, host,
            ));
        }
    }
    let loader_arc = Arc::new(loader);
    let native_loader = Arc::new(NativePluginLoader::new());
    let invoker =
        Arc::new(PluginInvokerImpl::new(loader_arc.clone()).set_native_loader(native_loader));
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
    // 观测失败（spawn/list 重试后仍失败）≠ 判定失败：保留
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
            // light 合宿成员跳过 boot 个体探测：宿主按成员集整组装载有时序窗口，
            // 探测可能早于成员工具登记 → 空列表误判漂移剔光（08-31 实测 memory
            // 三试俱败、task_manage 靠时序侥幸过）。成员能力随宿主装载统一登记，
            // 一致性由 watcher 复验（宿主已定型后探测可信 + 前缀归一）兜底。
            let is_light_member = agentos_invoker::is_cohost_member(manifest);
            if !g2_applicable || is_light_member {
                // 非 G2 覆盖（禁用/非 sidecar/无 tools+services/合宿成员）：登记 not_covered 缺省
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
            // 拒注是"声明与实现不一致已实际收口"的异常
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
    // 这让交互等插件自注册的 namespace 经 reader loop → router →
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
    // update_tenant/delete_user——内核直接以插件化形态提供这些端点）。
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
    // 全授予（存量插件零迁移）。AGENTOS_GRANTS_STRICT=1 时经 with_grants_strict
    // 置位，未声明一律拒绝（fail-closed，新插件必须显式声明）。执行点在
    // KernelCapabilityRouter::handle 单点，sidecar（PluginScopedRouter 注
    // _plugin_id）与 native（NativeHostServices 注 _plugin_id）同判。
    // 组主体分支：合宿连接的 _plugin_id = 宿主键（进程即主体），按当前装箱成员
    // 声明归并判（group_granted_capabilities；任一成员未声明 = 组未声明）。
    let loader_for_grants = loader_arc.clone();
    let invoker_for_grants = invoker.clone();
    let grants_lookup: agentos_api::capability_router::GrantsLookupFn =
        Arc::new(move |plugin_id| {
            if plugin_id.starts_with("group:") {
                return invoker_for_grants.group_granted_capabilities(plugin_id);
            }
            loader_for_grants.get_manifest(plugin_id).and_then(|m| {
                if m.granted_capabilities.is_empty() {
                    None
                } else {
                    Some(m.granted_capabilities.clone())
                }
            })
        });

    // G3：动态工具注册器——enablement 闸 + 写入注册表（M1 guarded 入 scope，
    // disable 即结构性收回）。动态注册的工具是 state 域数据不落内核存储，
    // 跨重启重建由插件自持 state/config 承担（registry 内存注册机制与
    // capability 面不变）。
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
            move |event_name: &str, tags: Vec<(String, serde_json::Value)>| {
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

    // state 出口声明查询闭包（ADR 2026-08-28 声明化出口）：pipeline-state.list
    // 摘要与 GET /pipelines/state 同源收集 manifest export_fields 并集。
    // 锁被占用（热重载写中）时按无声明降级——仅内核基线出口，不阻塞读面。
    let export_fields_lookup: agentos_api::capability_router::ExportFieldsLookupFn = {
        let manifests_for_export = manifests_shared.clone();
        Arc::new(move || {
            let Ok(guard) = manifests_for_export.try_read() else {
                return agentos_api::routes::ExportFields::default();
            };
            agentos_api::routes::ExportFields::from_manifests(guard.iter())
        })
    };

    // 强制注入工具声明查询闭包（P1-5 声明化）：tool-surface 过滤时收集全部
    // 插件 manifest force_include_tools 并集（如 spill_guard 声明 spill_retrieve）。
    // 锁被占用（热重载写中）时按无声明降级——零强制注入，不阻塞读面。
    let force_include_tools_lookup: agentos_api::capability_router::ForceIncludeToolsLookupFn = {
        let manifests_for_forced = manifests_shared.clone();
        Arc::new(move || {
            manifests_for_forced
                .try_read()
                .map(|guard| {
                    guard
                        .iter()
                        .flat_map(|m| m.force_include_tools.iter().cloned())
                        .collect()
                })
                .unwrap_or_default()
        })
    };

    // 工具面遮挡名单查询闭包（BUG-51 注册门禁）：G2 观测失败（账本
    // verify_incomplete）与复验超限终态（verify_failed）插件的工具不进 LLM
    // 工具面（必败面遮挡），复验通过（账本 ok）即自动转正——实时读契约账本
    // 现值，无独立状态需要维护。
    let shadowed_plugin_ids_lookup: agentos_api::capability_router::ShadowedPluginIdsLookupFn = {
        let ledger_for_shadow = contract_states.clone();
        Arc::new(move || ledger_for_shadow.verify_pending_plugin_ids())
    };

    // 管道恢复派发闭包（resume_pipeline 续跑拉起）：经 EngineDispatcher 走与
    // 聊天/催促同一条派发链。dispatcher 构造晚于 router（AppState 之后，见下方
    // chat handler 注册块），经 OnceLock 槽位二阶段接线——未 set 前调用报
    // 显式错误（装配期窗口极短，不静默降级）。
    // 空 content + _skip_user_append overlay：续跑轮不落 user 消息、纯按快照
    // 恢复执行（stage_recover_history 热/冷路径全量恢复 state），源标记 Trigger
    // （系统发起，与任务派发同源）。
    let resume_dispatcher_slot: Arc<
        std::sync::OnceLock<std::sync::Arc<dyn agentos_session::router::PipelineDispatcher>>,
    > = Arc::new(std::sync::OnceLock::new());
    let pipeline_resumer: agentos_api::capability_router::PipelineResumerFn = {
        let slot = resume_dispatcher_slot.clone();
        Arc::new(
            move |pipeline_id: String,
                  thread_id: String,
                  user_id: String,
                  state_overlay: Option<serde_json::Value>| {
                let slot = slot.clone();
                Box::pin(async move {
                    let dispatcher = slot
                        .get()
                        .ok_or_else(|| "恢复派发通道未就绪（dispatcher 尚未装配）".to_string())?;
                    // 调用方 overlay（如任务域清 task.status 终态键）不透明透传，
                    // 并入 _skip_user_append（续跑轮恒不落 user 消息）。
                    let mut overlay = state_overlay.unwrap_or_else(|| serde_json::json!({}));
                    if let Some(obj) = overlay.as_object_mut() {
                        obj.insert("_skip_user_append".into(), serde_json::json!(true));
                    }
                    dispatcher
                        .dispatch_user_input(
                            &thread_id,
                            &user_id,
                            "",
                            &pipeline_id,
                            "",
                            None,
                            Some(&overlay),
                            "",
                            "",
                            agentos_core::types::PendingInputSource::Trigger,
                        )
                        .await
                })
                    as std::pin::Pin<
                        Box<dyn std::future::Future<Output = Result<(), String>> + Send>,
                    >
            },
        )
    };

    let mut router_builder = KernelCapabilityRouter::with_metrics(metrics_aggregator.clone())
        .with_invoker(invoker.clone())
        .with_registry(registry.clone())
        // BUG-37 调用路径注册表自愈：反查 miss 时按共享 manifest store 重注册
        // （仅启用插件；禁用/已删插件保持 fail-closed）。
        .with_tool_registry_heal(agentos_api::plugin_lifecycle::tool_registry_heal_fn(
            registry.clone(),
            plugin_scopes.clone(),
            manifests_shared.clone(),
            enabled_plugin_ids.clone(),
        ))
        .with_session(session_coord.clone())
        .with_store(store.clone())
        .with_handler_registry(handler_registry.clone())
        .with_grants_lookup(grants_lookup)
        .with_dynamic_tool_registrar(dynamic_registrar.clone())
        .with_domain_broadcaster(domain_broadcaster)
        .with_streaming_declaration_lookup(streaming_declaration_lookup)
        .with_export_fields_lookup(export_fields_lookup)
        .with_force_include_tools_lookup(force_include_tools_lookup)
        .with_shadowed_plugin_ids_lookup(shadowed_plugin_ids_lookup)
        .with_pipeline_resumer(pipeline_resumer)
        .with_capability_contracts(capability_contracts.clone());
    // SQLite 固有面：suspend/resume 挂起凭据读写（审批挂起恢复链写侧）。
    if let Some(sqlite) = sqlite_db.as_ref() {
        router_builder = router_builder.with_sqlite(sqlite.clone());
    }
    // AGENTOS_GRANTS_STRICT=1：未声明 granted_capabilities 的插件反向调用一律
    // 拒绝（fail-closed）。启动期读取——开关随进程生效，不随插件热发现翻转。
    if std::env::var("AGENTOS_GRANTS_STRICT").as_deref() == Ok("1") {
        info!(
            target: "agentos-kernel",
            "AGENTOS_GRANTS_STRICT=1：插件未声明 granted_capabilities 时反向调用一律拒绝（fail-closed）"
        );
        router_builder = router_builder.with_grants_strict();
    }
    let router = Arc::new(
        router_builder
            // 工具连续失败告警器：挂默认实现，统一经 invoke 结果
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

    // 监控 M3 后半：进程态周期轮询（每 10s）——遍历活宿主（含 light 合宿组）按
    // 成员插件写 process.alive/pid/memory_rss_bytes/uptime_seconds gauge；
    // last_crash_ts 由上方崩溃回调单独写，轮询快照 None 不覆盖。
    let _proc_state_poller = agentos_api::metrics::spawn_proc_state_poller(
        Arc::clone(&invoker),
        metrics_aggregator.clone(),
        std::time::Duration::from_secs(10),
    );

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
    // 自动生效；编译产物缺失/失败期间 chat 走空管道降级（与"缺省配置下内核
    // 可启动"一致）。
    match agentos_api::server::load_and_compile(&config_root, &plugin_ids) {
        Ok(compiled) => {
            // boot 后台预热：管道引用的 sidecar 宿主提前 spawn 进缓存，消除
            // "启动后首次消息"的管道链串行冷启动（每宿主 spawn→initialize 秒级，
            // 实测首条消息 41.5s vs 第二条 1.9s）。预热集只含管道引用插件
            // （referenced_plugin_ids 单一来源）——工具/服务插件保持纯懒加载，
            // idle GC 治理不变。预热须在 set_router 之后（spawn 的宿主拿到完整
            // capabilities 声明，反向调用不空快照）。
            agentos_api::sidecar_warmup::spawn_pipeline_sidecar_warmup(
                invoker.clone(),
                Arc::new(compiled),
                enabled_manifests.clone(),
            );
        }
        Err(compile_err) => {
            warn!("管道加载期编译失败（内核继续启动，修复配置后热重载自动生效）: {compile_err}");
        }
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
        project_root,
        enabled_snapshot,
    )
    .with_capability_handlers(handler_registry.clone());
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
        .with_widget_bindings(Arc::clone(&widget_bindings_shared))
        // 闸2·观测：boot 期 G2 校验结果与 /plugins/contract-status 面板同账本
        // （boot upsert 的 not_covered/derived 行落同一 Arc 实例，面板可见）。
        .with_contract_states(contract_states.clone());
    // P2：启用会话内核（WS 握手鉴权 + 连接注册 + 入站路由 + 断线重放）。
    // 复用 router 已持有的 session_coord（流式 chunk 推送与 WS 出站共享同一 SessionCoordinator）。
    let state = state.enable_session_with(session_coord);

    // chat namespace capability：把"向会话投递消息并跑管道"暴露给 sidecar。
    // 触发器（trigger_setup_tool）到期触发时经 chat.send_message 复用前端同一条
    // WS 派发（dispatch_user_input → process_via_engine）。本 handler 补上注入通道。
    // AppState 在 router 之后构造，故在此（启动末期）注册到既有 handler_registry。
    {
        let dispatcher: std::sync::Arc<dyn agentos_session::router::PipelineDispatcher> =
            std::sync::Arc::new(agentos_api::ws_session::EngineDispatcher::new(
                state.clone(),
            ));
        // 管道恢复派发通道接线（resume_pipeline 续跑拉起，见 router 装配处）。
        let _ = resume_dispatcher_slot.set(dispatcher.clone());
        handler_registry.register(std::sync::Arc::new(
            agentos_api::chat_send_handler::ChatSendHandler::with_session(
                dispatcher,
                state.store.clone(),
                state
                    .session
                    .clone()
                    .expect("session 必须在 chat handler 注册前装配"),
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
        // 代码指纹复验：sidecar 实现修复（.py 改动不改 manifest）后，watcher
        // 轮询即可复验恢复被 G2 净化剔除的工具——目录解析与 invoker respawn
        // 判据同源（同一 loader 发现结果 + 同一指纹函数）。
        .with_code_dir_resolver({
            let fp_invoker = invoker.clone();
            std::sync::Arc::new(move |plugin_id: &str| fp_invoker.plugin_source_dir(plugin_id))
        })
        .with_manifests_store(state.manifests.clone())
        .with_enabled_ids(state.enabled_plugin_ids.clone())
        .with_enablement(enablement.clone())
        // enablement 每次 sync 从盘上 profile 现读——boot 快照看不到
        // 运行期 PUT enabled 的写盘结果，卸载→重装按旧快照会把已禁用插件重新
        // 注册（运行期禁用被静默撤销）。
        .with_profile_reload(config_root.clone())
        // 闸2·观测：热发现校验结果收口进同一账本（新插件契约状态写入，
        // 与 boot/validate-all 面板数据同源）。
        .with_contract_states(contract_states.clone())
        .spawn();
        info!(target: "agentos-kernel", "Plugin hot-discover watcher spawned (notify + polling fallback; cdylib change -> G8 auto-restart)");
    }

    // 内存维护（2026-09-09 零页普查：旧进程 860MB Private 中 557MB 为全零页）：
    // purge_delay=0 只归还"整体变空的页"，多 arena 成长后空块散布在各页内，
    // 需周期性 mi_collect 强制扫描收缩空闲 arena。
    // M1 测量面：同一循环内每 60s 采集 mimalloc 统计快照打日志（先采样后
    // collect——collect 会收缩 committed，若先 collect 再采样会掩盖滞留自然值），
    // 增量判活增长 vs 滞留由消费方对两次快照相减（mimalloc 无增量 delta API）。
    // collect 保持原 300s 节奏（含启动首拍即 collect：ticks 1,6,11…）。
    // 注意：libmimalloc-sys release 构建下 MI_STAT=0，malloc 计量面
    // （live_mb/allocs）恒 0，可信信号是 committed/rss/commit/purged——
    // 见 agentos_api::allocator::MemStats 文档。
    {
        let mut maint = tokio::time::interval(std::time::Duration::from_secs(60));
        let mut ticks: u64 = 0;
        tokio::spawn(async move {
            loop {
                maint.tick().await;
                ticks += 1;
                let stats = agentos_api::allocator::snapshot_stats();
                info!(
                    target: "agentos-kernel",
                    committed_mb = stats.committed_bytes.map(|b| b / (1024 * 1024)),
                    rss_mb = stats.process_rss_bytes.map(|b| b / (1024 * 1024)),
                    commit_mb = stats.process_commit_bytes.map(|b| b / (1024 * 1024)),
                    purged_mb = stats.purged_bytes.map(|b| b / (1024 * 1024)),
                    reserved_mb = stats.reserved_bytes.map(|b| b / (1024 * 1024)),
                    abandoned_pages = stats.abandoned_pages,
                    live_mb = stats.in_use_bytes.map(|b| b / (1024 * 1024)),
                    allocs = stats.total_allocs,
                    "mimalloc stats snapshot"
                );
                if ticks % 5 == 1 {
                    unsafe { libmimalloc_sys::mi_collect(true) };
                }
            }
        });
    }

    // 内存水位监控（可选自愈兜底，**默认关闭**——2026-09-10 用户裁定：水位
    // 重启可能打断正常业务）：显式设置 AGENTOS_MEM_WATERMARK_MB 为正值才启用；
    // 启用后每 60s 采样自身 Private Bytes，连续 N 次超阈值 → 走与 G8 cdylib
    // 重启相同的 drain_and_exit75 排空路径（exit 75，监督脚本拉起新进程）。
    // AGENTOS_MEM_WATERMARK_MIN=持续分钟数（默认 10；采样间隔固定 60s）。
    {
        let watermark_db = state.db.clone();
        let watermark_invoker = state.invoker.clone();
        tokio::spawn(async move {
            const SAMPLE_INTERVAL_SECS: u64 = 60;
            let Some((threshold_mb, sustain_minutes)) = resolve_watermark_config() else {
                info!(
                    target: "agentos-kernel",
                    "内存水位监控默认关闭（不会触发重启）；显式设置 AGENTOS_MEM_WATERMARK_MB=<MB> 可启用自愈"
                );
                return;
            };
            let consecutive = ((sustain_minutes * 60) / SAMPLE_INTERVAL_SECS).max(1) as usize;
            info!(
                target: "agentos-kernel",
                threshold_mb,
                consecutive,
                sample_interval_secs = SAMPLE_INTERVAL_SECS,
                "内存水位监控已启动（持续超限即排空重启自愈）"
            );
            let mut recent: Vec<u64> = Vec::with_capacity(consecutive);
            let mut tick =
                tokio::time::interval(std::time::Duration::from_secs(SAMPLE_INTERVAL_SECS));
            loop {
                tick.tick().await;
                let Some(usage_bytes) = sample_private_bytes() else {
                    continue; // 采样不可用（非 Windows/API 失败）：缺数不判，绝不误自愈
                };
                recent.push(usage_bytes / (1024 * 1024));
                // 滑动窗口只留判定所需样本，长运行监控自身不积内存
                let keep_from = recent.len().saturating_sub(consecutive);
                recent.drain(..keep_from);
                if watermark_breach(&recent, threshold_mb, consecutive) {
                    warn!(
                        target: "agentos-kernel",
                        threshold_mb,
                        consecutive,
                        latest_mb = recent.last().copied().unwrap_or(0),
                        "内存水位持续超限（自愈）：排空在途 runs 后 exit 75，监督脚本拉起新进程"
                    );
                    agentos_api::routes::drain_and_exit75(
                        watermark_db.as_ref(),
                        watermark_invoker.clone(),
                        "memory watermark self-heal: private bytes 持续超阈值",
                    )
                    .await;
                    // 逃生门（AGENTOS_DISABLE_SELF_EXIT=1）下 drain 只排空不退出：
                    // 停止本监控，避免每 60s 重复排空；进程交人工处置。
                    break;
                }
            }
        });
    }

    start_server(addr, state).await?;

    Ok(())
}

/// 阻塞池上限（线程治理）：tokio 默认 512 且按需增长。默认值取 512 = tokio
/// 原生默认——b18a11312 曾默认 16，实测 30+ sidecar 并发阻塞调用（SQLite
/// state 读写等）排队饿死，`pipeline-state.list` 60s 超时成片失败（2026-09-09
/// 真机实证）。`AGENTOS_MAX_BLOCKING_THREADS` 可调；未设置/解析失败/非正值
/// 回落默认 512。
fn resolve_max_blocking_threads() -> usize {
    std::env::var("AGENTOS_MAX_BLOCKING_THREADS")
        .ok()
        .and_then(|v| v.trim().parse::<usize>().ok())
        .filter(|&n| n > 0)
        .unwrap_or(512)
}

/// 线程栈大小（线程治理，KiB 单位）：默认 2048 KiB = tokio 默认 2MiB（行为不变，
/// 长运行可调小压缩虚拟内存预留）。`AGENTOS_THREAD_STACK_KIB` 未设置/解析失败/
/// 非正值回落默认。
fn resolve_thread_stack_size() -> usize {
    std::env::var("AGENTOS_THREAD_STACK_KIB")
        .ok()
        .and_then(|v| v.trim().parse::<usize>().ok())
        .filter(|&kib| kib > 0)
        .map(|kib| kib * 1024)
        .unwrap_or(2048 * 1024)
}

/// 水位自愈配置（MB 阈值, 持续分钟数）。**默认关闭**（2026-09-10 用户裁定：
/// 水位重启可能打断正常业务，不得默认触发）——显式设置 `AGENTOS_MEM_WATERMARK_MB`
/// 为正值才启用；未设置 / 0 / 非法值 = 不启动监控。`AGENTOS_MEM_WATERMARK_MIN`
/// 仅在启用时生效（默认 10 分钟）。
fn resolve_watermark_config() -> Option<(u64, u64)> {
    let threshold_mb = std::env::var("AGENTOS_MEM_WATERMARK_MB")
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
        .filter(|&v| v > 0)?;
    let sustain_minutes = std::env::var("AGENTOS_MEM_WATERMARK_MIN")
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
        .filter(|&v| v > 0)
        .unwrap_or(10);
    Some((threshold_mb, sustain_minutes))
}

/// 水位判定（纯函数）：最近 `consecutive` 个采样全部**严格大于** `threshold_mb`
/// 才判突破（瞬时尖峰不触发，恰好等于阈值不算超）。样本不足 `consecutive`
/// 个一律不判（含 `consecutive == 0` 防御）。
fn watermark_breach(samples: &[u64], threshold_mb: u64, consecutive: usize) -> bool {
    if consecutive == 0 || samples.len() < consecutive {
        return false;
    }
    samples[samples.len() - consecutive..]
        .iter()
        .all(|&mb| mb > threshold_mb)
}

/// 采样当前进程 Private Bytes（字节）。经 windows-sys 的 K32GetProcessMemoryInfo
/// 读 PROCESS_MEMORY_COUNTERS_EX.PrivateUsage（commit charge，与任务管理器
/// "提交大小"同口径）。取舍：该 crate 仅 FFI 声明零运行时开销，且已是本仓依赖图
/// 既有传递依赖（Cargo.lock 0.52.0，零新增下载）；否决了每 60s spawn powershell
/// Get-Process 的替代方案（进程启动秒级开销 + 依赖 powershell 在 PATH）。
#[cfg(windows)]
fn sample_private_bytes() -> Option<u64> {
    use std::mem::zeroed;
    use windows_sys::Win32::System::ProcessStatus::{
        K32GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS, PROCESS_MEMORY_COUNTERS_EX,
    };
    use windows_sys::Win32::System::Threading::GetCurrentProcess;

    let mut counters: PROCESS_MEMORY_COUNTERS_EX = unsafe { zeroed() };
    counters.cb = std::mem::size_of::<PROCESS_MEMORY_COUNTERS_EX>() as u32;
    // K32GetProcessMemoryInfo 收基类指针；cb 声明为 EX 尺寸时 Windows 会补填
    // 尾部扩展字段 PrivateUsage（ProcessStatus API 契约）。
    let ok = unsafe {
        K32GetProcessMemoryInfo(
            GetCurrentProcess(),
            &mut counters as *mut PROCESS_MEMORY_COUNTERS_EX as *mut PROCESS_MEMORY_COUNTERS,
            counters.cb,
        )
    };
    if ok == 0 {
        return None;
    }
    Some(counters.PrivateUsage as u64)
}

/// 非 Windows 平台无采样实现：恒 None（监控诚实缺数，不判突破、不自愈）。
#[cfg(not(windows))]
fn sample_private_bytes() -> Option<u64> {
    None
}

/// 解析用户插件根目录（可写，第三方插件安装位置）。
///
/// 解析优先级：`AGENTOS_USER_PLUGINS_DIR` > `<USER_ROOT>/plugins`（默认根按
/// OS 不同，见 [`agentos_core::user_space::user_root`]）> `None`（不启用 user_root）。
///
/// 实现单点在 `agentos_core::user_space`——内核 bin、HTTP 面（config 读写）、
/// 插件 loader 共用同一份解析，杜绝"三处各拼一次路径"（单真值 ADR 的实证根因）。
fn resolve_user_plugins_dir() -> Option<PathBuf> {
    agentos_core::user_space::user_plugins_dir()
}

/// 模式种子对账（设计稿 2026-09-15 §2「版本管理」）：对出厂种子
/// `<plugins_dir>/modes/` 与用户副本 `<user_plugins>/modes/` 跑一轮播种/
/// 升级对账（语义见 [`agentos_core::user_space::reconcile_mode_seeds`]），
/// 返回本轮对账结论（供 git 化联动提交判断"有无落盘变化"）。
///
/// 对账是启动家政：出厂种子缺席（存量部署/种子未随包）或用户空间不可用
/// → no-op（双根兜底，内置根模式插件仍可用）；单轮失败只 warn 不阻断启动
/// （fail 方向是"本次不播种不升级"，绝不破坏既有用户副本——账本损坏时
/// 对账整体不动任何文件，fail-closed 语义在 core 侧）。
fn reconcile_mode_seeds_at_boot(
    plugins_dir: &std::path::Path,
    user_plugins_dir: Option<&std::path::Path>,
) -> Vec<agentos_core::user_space::ModeSeedOutcome> {
    use agentos_core::user_space::ModeSeedOutcome;
    let Some(user_plugins) = user_plugins_dir else {
        return Vec::new();
    };
    let factory_modes = plugins_dir.join("modes");
    if !factory_modes.is_dir() {
        return Vec::new();
    }
    let user_modes = user_plugins.join("modes");
    match agentos_core::user_space::reconcile_mode_seeds(&factory_modes, &user_modes) {
        Ok(outcomes) => {
            for outcome in &outcomes {
                match outcome {
                    ModeSeedOutcome::Seeded { mode_id, version } => info!(
                        target: "agentos-kernel",
                        mode = %mode_id, version = %version,
                        "模式种子已播种到用户空间"
                    ),
                    ModeSeedOutcome::Upgraded { mode_id, from, to } => info!(
                        target: "agentos-kernel",
                        mode = %mode_id, from = %from, to = %to,
                        "模式副本未定制，已静默升级到出厂种子"
                    ),
                    ModeSeedOutcome::UpgradeAvailable {
                        mode_id,
                        seeded_version,
                        factory_version,
                    } => warn!(
                        target: "agentos-kernel",
                        mode = %mode_id, seeded = %seeded_version, factory = %factory_version,
                        "模式副本已定制且出厂种子有更新——保留用户副本，升级可用（恢复出厂后重播种即得新版）"
                    ),
                    ModeSeedOutcome::BaselineRegistered { mode_id, version } => info!(
                        target: "agentos-kernel",
                        mode = %mode_id, version = %version,
                        "用户手工放置的模式副本已补记账本（不替换）"
                    ),
                    ModeSeedOutcome::VersionUnparseable {
                        mode_id,
                        factory_version,
                        ledger_version,
                    } => warn!(
                        target: "agentos-kernel",
                        mode = %mode_id, factory = %factory_version, ledger = %ledger_version,
                        "模式种子 version 非 semver，无法判定新旧——本次不动用户副本（请修正种子 version）"
                    ),
                    ModeSeedOutcome::Current { .. } => {}
                }
            }
            outcomes
        }
        Err(e) => {
            warn!(
                target: "agentos-kernel",
                error = %e,
                "模式种子对账失败（本次不播种不升级，用户副本原样保留；下次启动重试）"
            );
            Vec::new()
        }
    }
}

/// 用户仓 git 化引导挂点（设计稿 2026-09-15 §2.2）：模式种子对账完成后确保
/// 用户仓已初始化（管辖面 `<USER_ROOT>/{plugins,config}`，`.env`/`data/` 及
/// 其余一切排除）。已初始化 → 幂等跳过；git 缺席或初始化失败 → warn 降级
/// （git 化是增强不是依赖，绝不阻断启动），绝不 push。
fn init_user_repo_at_boot() {
    let Some(user_root) = agentos_core::user_space::user_root() else {
        return;
    };
    match agentos_core::user_space::ensure_user_repo(&user_root) {
        Ok(true) => info!(
            target: "agentos-kernel",
            path = %user_root.display(),
            "用户仓已初始化（plugins/ 与 config/ 入仓，.env 与 data/ 排除）"
        ),
        Ok(false) => {}
        Err(e) => warn!(
            target: "agentos-kernel",
            error = %e,
            "用户仓初始化失败，git 化降级（功能不受损）"
        ),
    }
}

/// 种子对账联动提交（设计稿 2026-09-15 §2.2）：本轮对账有落盘变化（播种/
/// 升级/补账）→ 自动提交一笔 `seed: reconcile <n> files`（只暂存对账写下的
/// 文件，用户工作区改动不自动提交）。失败只 warn（增强降级，绝不阻断启动）。
fn commit_seed_reconciliation_at_boot(outcomes: &[agentos_core::user_space::ModeSeedOutcome]) {
    if outcomes.is_empty() {
        return;
    }
    let Some(user_root) = agentos_core::user_space::user_root() else {
        return;
    };
    match agentos_core::user_space::commit_seed_reconciliation(&user_root, outcomes) {
        Ok(Some(hash)) => info!(
            target: "agentos-kernel",
            commit = %hash,
            "模式种子对账变更已自动提交到用户仓"
        ),
        Ok(None) => {}
        Err(e) => warn!(
            target: "agentos-kernel",
            error = %e,
            "模式种子对账自动提交失败（用户副本已落盘，git 历史缺一笔，可稍后手动提交）"
        ),
    }
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

/// 启动期 discover 的扫描根全集：内置根 + 用户根（用户根缺席 = 仅内置根）。
///
/// 双根都必须递归到插件目录父级：模式包 `modes/<mode_id>/plugin.json` 是二级
/// 嵌套，discover 内建的内置/用户根扫描（一级子目录）看不到，双根同 id 用户赢
/// 全靠 root_paths 里带进用户根父目录 + loader 的用户根子树优先裁决。与热路径
/// `PluginInvokerImpl::collect_plugin_roots` 同源（那里也是双根父目录全集）。
fn collect_boot_plugin_roots(
    plugins_dir: &std::path::Path,
    user_plugins_dir: Option<&std::path::Path>,
) -> Vec<String> {
    let mut roots = discover_plugin_roots(plugins_dir);
    if let Some(user_dir) = user_plugins_dir {
        roots.extend(discover_plugin_roots(user_dir));
    }
    roots
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

/// 迁移护栏：默认库位置迁到用户空间后，存量库仍在项目根时显式告警（不自动迁移）。
///
/// 触发条件（三者同时满足才告警，避免误报）：
/// 1. 本次解析出的库路径**不在**项目根 —— 即已按新默认（用户空间）开库；
/// 2. 项目根**存在** legacy 库文件；
/// 3. legacy 库**非空**（排除 0 字节残留——建了库没写过数据的部署无需迁移）。
///
/// 不阻断启动：告警 + 指明迁移命令。数据没丢（legacy 文件原样在），用户按提示
/// 迁移即可；若已在新库产生数据，迁移脚本的合并语义由脚本自己把关。
fn warn_legacy_db_not_migrated(
    storage_cfg: &agentos_engine::storage_factory::StorageConfig,
    config_root: &std::path::Path,
) {
    let Some(project_root) = config_root.parent() else {
        return;
    };
    let legacy = project_root.join(agentos_engine::storage_factory::DB_FILENAME);
    let current = std::path::Path::new(&storage_cfg.sqlite_path);

    // 当前库就在项目根（或 :memory:/非 sqlite）→ 无迁移问题
    if storage_cfg.driver != "sqlite" || storage_cfg.sqlite_path == ":memory:" {
        return;
    }
    if current.parent().map(|p| p == project_root).unwrap_or(false) {
        return;
    }
    let Ok(meta) = std::fs::metadata(&legacy) else {
        return; // 无 legacy 库
    };
    if meta.len() == 0 {
        return; // 空库残留，无数据可迁
    }

    eprintln!(
        "[boot] 检测到项目根存在存量库 {}（{} 字节），但本次按用户空间默认开库到 {}。\n\
         \x20     数据未丢失（老库文件原样保留）。两种处置：\n\
         \x20     ① 迁移存量数据（推荐）：python scripts/migrate_to_user_root.py --dry-run 预览，去掉 --dry-run 执行；\n\
         \x20     ② 继续用老库：设 AGENTOS_DB_PATH 指向老库路径后重启。",
        legacy.display(),
        meta.len(),
        current.display()
    );
    warn!(
        target: "agentos-kernel",
        legacy_db = %legacy.display(),
        current_db = %current.display(),
        "legacy project-root database detected; user-space default in effect (run scripts/migrate_to_user_root.py)"
    );
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
    // P0-2：allowlist 生产接线——config/kernel/plugin_allowlist.yaml 从"空挂"变真准入
    // （permissive 默认：放行 + 条目 sha256 校验，真实语料零误伤；strict 由部署方显式
    // 启用：白名单外插件 load 失败 fail-closed，与 deny_unknown_fields 一致）。
    let allowlist = agentos_plugin_loader::load_allowlist_file(
        &config_root.join("kernel/plugin_allowlist.yaml"),
    );
    // 双源同 id 裁决策略（ADR 2026-09-20-packaged-dual-source-adjudication）：
    // 装机链（electron buildKernelEnv）设 AGENTOS_PLUGIN_SOURCE_PRIORITY=builtin
    // → 打包副本压过用户空间陈旧副本（除非用户副本 semver 严格更新）；dev 不设
    // → UserFirst 历史口径逐位不变。
    let dual_source_policy = agentos_plugin_loader::DualSourcePolicy::from_env();
    PluginLoaderImpl::new(plugins_dir, user_plugins_dir)
        // 接入 config_root：否则 load_config() 因 config_root=None 恒返回空 {}
        .with_config_root(config_root)
        .with_allowlist(allowlist)
        .with_dual_source_policy(dual_source_policy)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 管理员初始口令解析（D1）：AGENTOS_ADMIN_PASSWORD 设置时原样生效；
    /// 未设置时生成随机口令——绝不等于任何硬编码值，且每次生成互不相同
    /// （性质断言）。两个分支串在一个测试内执行：环境变量是进程全局态，
    /// 拆两个测试会互相竞争。
    #[test]
    fn admin_password_env_overrides_and_random_fallback() {
        std::env::remove_var("AGENTOS_ADMIN_PASSWORD");
        let (random1, generated1) = resolve_admin_password();
        let (random2, generated2) = resolve_admin_password();
        assert!(generated1 && generated2, "未设置环境变量必须走随机生成");
        assert!(random1.len() >= 24, "随机口令须满足长度下限（128 bit hex）");
        assert_ne!(random1, random2, "两次生成必须互不相同（随机性）");
        std::env::set_var("AGENTOS_ADMIN_PASSWORD", "custom-secret-pw");
        let (explicit, generated) = resolve_admin_password();
        assert!(!generated, "显式环境变量不算生成");
        assert_eq!(explicit, "custom-secret-pw");
        std::env::remove_var("AGENTOS_ADMIN_PASSWORD");
    }

    /// 监听地址解析（D12）：默认 127.0.0.1；AGENTOS_BIND 显式生效；
    /// 弃用别名 AGENTOS_KERNEL_HOST 过渡期仍被采纳。
    #[test]
    fn bind_host_default_is_loopback() {
        for k in ["AGENTOS_BIND", "AGENTOS_KERNEL_HOST"] {
            std::env::remove_var(k);
        }
        assert_eq!(resolve_bind_host(), "127.0.0.1", "默认必须绑定回环地址");
        std::env::set_var("AGENTOS_BIND", "0.0.0.0");
        assert_eq!(
            resolve_bind_host(),
            "0.0.0.0",
            "AGENTOS_BIND 显式开启外网可达"
        );
        std::env::remove_var("AGENTOS_BIND");
        std::env::set_var("AGENTOS_KERNEL_HOST", "192.168.1.9");
        assert_eq!(resolve_bind_host(), "192.168.1.9", "弃用别名过渡期生效");
        std::env::remove_var("AGENTOS_KERNEL_HOST");
    }

    /// 模式种子对账挂点（设计稿 §2）：出厂种子在 → 副本播种进用户根并落账本；
    /// 出厂种子缺席（存量部署）→ no-op 且不建用户目录。真实临时目录，不 mock。
    #[test]
    fn reconcile_mode_seeds_at_boot_seeds_before_plugin_scan() {
        let tmp = tempfile::tempdir().unwrap();
        let plugins_dir = tmp.path().join("plugins/shared");
        let seed = plugins_dir.join("modes/mode_coding");
        std::fs::create_dir_all(&seed).unwrap();
        std::fs::write(
            seed.join("plugin.json"),
            r#"{"id":"mode_coding","version":"0.1.0"}"#,
        )
        .unwrap();
        std::fs::write(seed.join("profile.yaml"), "chain: v1\n").unwrap();
        let user_root = tmp.path().join("user-root");

        // 生产形态：挂点收用户插件根（<USER_ROOT>/plugins），副本落其下 modes/
        reconcile_mode_seeds_at_boot(&plugins_dir, Some(&user_root.join("plugins")));
        // 播种发生在插件扫描之前：discover 之前用户副本与账本已在盘上
        assert_eq!(
            std::fs::read_to_string(user_root.join("plugins/modes/mode_coding/profile.yaml"))
                .unwrap(),
            "chain: v1\n"
        );
        assert!(user_root.join("plugins/modes/.seeds.json").is_file());

        // 出厂种子缺席 → no-op，不建用户 modes 目录
        let user_root2 = tmp.path().join("user-root-2");
        let empty_plugins = tmp.path().join("plugins-empty");
        std::fs::create_dir_all(&empty_plugins).unwrap();
        reconcile_mode_seeds_at_boot(&empty_plugins, Some(&user_root2));
        assert!(!user_root2.join("plugins/modes").exists());

        // 用户空间不可用（None）→ no-op 不 panic
        reconcile_mode_seeds_at_boot(&plugins_dir, None);
    }

    /// 启动扫描根（双根用户赢·启动侧）：内置根与用户根的插件父目录都必须进
    /// root_paths——模式包 `modes/<id>/plugin.json` 二级嵌套只有父目录根扫得到，
    /// 缺用户根会让启动装载 repo 份、热路径解析用户份（首个 sync 周期源码目录
    /// 翻转触发复验驱逐，/ext 路由空窗）。
    #[test]
    fn collect_boot_plugin_roots_includes_user_root_parents() {
        let tmp = tempfile::tempdir().unwrap();
        let plugins_dir = tmp.path().join("plugins/shared");
        let repo_mode = plugins_dir.join("modes/mode_coding");
        std::fs::create_dir_all(&repo_mode).unwrap();
        std::fs::write(repo_mode.join("plugin.json"), r#"{"id":"mode_coding"}"#).unwrap();
        let user_plugins = tmp.path().join("user-root/plugins");
        let user_mode = user_plugins.join("modes/mode_coding");
        std::fs::create_dir_all(&user_mode).unwrap();
        std::fs::write(user_mode.join("plugin.json"), r#"{"id":"mode_coding"}"#).unwrap();

        let roots = collect_boot_plugin_roots(&plugins_dir, Some(&user_plugins));
        let repo_parent = plugins_dir.join("modes").to_string_lossy().to_string();
        let user_parent = user_plugins.join("modes").to_string_lossy().to_string();
        assert!(
            roots.contains(&repo_parent),
            "内置根模式包父目录必须在扫描根: {roots:?}"
        );
        assert!(
            roots.contains(&user_parent),
            "用户根模式包父目录必须在扫描根（双根同 id 用户赢的启动前提）: {roots:?}"
        );

        // 用户根缺席（存量部署）= 仅内置根，不建目录不报错。
        let only = collect_boot_plugin_roots(&plugins_dir, None);
        assert_eq!(only.len(), 1);
        assert_eq!(only[0], repo_parent);
    }

    /// git 化挂点（设计稿 §2.2）：对账后确保用户仓就绪 + 播种变更自动提交一笔；
    /// 重复引导零操作（对账幂等 → 无第二笔提交）。真实临时目录 + 真实 git。
    #[test]
    fn boot_initializes_user_repo_and_commits_seed_reconciliation() {
        use agentos_core::user_space::USER_ROOT_ENV;
        let _lock = user_space_env_lock();
        let tmp = tempfile::tempdir().unwrap();
        let plugins_dir = tmp.path().join("plugins/shared");
        let seed = plugins_dir.join("modes/mode_git");
        std::fs::create_dir_all(&seed).unwrap();
        std::fs::write(
            seed.join("plugin.json"),
            r#"{"id":"mode_git","version":"1.0.0"}"#,
        )
        .unwrap();
        std::fs::write(seed.join("profile.yaml"), "chain: v1\n").unwrap();
        let user_root = tmp.path().join("user-root");
        struct RootGuard(Option<String>);
        impl Drop for RootGuard {
            fn drop(&mut self) {
                match self.0.take() {
                    Some(v) => std::env::set_var(USER_ROOT_ENV, v),
                    None => std::env::remove_var(USER_ROOT_ENV),
                }
            }
        }
        let _guard = RootGuard(std::env::var(USER_ROOT_ENV).ok());
        std::env::set_var(USER_ROOT_ENV, &user_root);

        // 首次引导：播种 → 建仓 → 联动提交一笔
        let outcomes = reconcile_mode_seeds_at_boot(&plugins_dir, Some(&user_root.join("plugins")));
        init_user_repo_at_boot();
        assert!(user_root.join(".git").exists());
        assert!(user_root.join(".gitignore").is_file());
        commit_seed_reconciliation_at_boot(&outcomes);

        let head = |args: &[&str]| {
            let out = std::process::Command::new("git")
                .arg("-C")
                .arg(&user_root)
                .args(args)
                .output()
                .unwrap();
            assert!(out.status.success());
            String::from_utf8_lossy(&out.stdout).trim().to_string()
        };
        let count = head(&["rev-list", "--count", "HEAD"]);
        assert_eq!(count, "1", "联动提交恰好一笔");
        let subject = head(&["log", "-1", "--format=%s"]);
        assert!(subject.starts_with("seed: reconcile "));
        let tracked = head(&["ls-files"]);
        assert!(tracked.contains("plugins/modes/mode_git/profile.yaml"));
        assert!(tracked.contains("plugins/modes/.seeds.json"));

        // 重复引导：对账全 Current → 不再产生提交（幂等零操作）
        let rerun = reconcile_mode_seeds_at_boot(&plugins_dir, Some(&user_root.join("plugins")));
        init_user_repo_at_boot();
        commit_seed_reconciliation_at_boot(&rerun);
        let count = head(&["rev-list", "--count", "HEAD"]);
        assert_eq!(count, "1", "重复引导零新增提交");
    }

    /// P0-1：build_plugin_loader 接入 config_root 后，load_config 返回非空（含 models 节）。
    ///
    /// 回归保护：若有人移除 `.with_config_root(config_root)`，此测试会失败——
    /// 因为 loader 的 config_root 为 None 时 load_config 恒返回空 `{}`。
    ///
    /// 环境变量是进程全局态：读写 `AGENTOS_USER_*` 的用例互斥执行。
    ///
    /// 同二进制内 `build_plugin_loader_wires_config_root`（钉桩用户根）与
    /// `user_plugins_dir_env_first_then_data_dir`（断言分区解析）都读这些变量，
    /// 并行跑会互相踩（实测整套 workspace 连跑时偶发失败）。
    fn user_space_env_lock() -> std::sync::MutexGuard<'static, ()> {
        static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
        LOCK.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// 用户根钉到本用例的 config_dir：`load_config` 经 `apply_user_config_overlay`
    /// 做**用户层优先整体替换**（ADR 2026-09-13-unified-user-root），若用户层恰好存在
    /// 同路径文件（开发机的 `%APPDATA%/agentos/config/models/llm.yaml`），factory 的
    /// 用例夹具会被整个顶掉，断言依赖环境。钉桩保证只读本用例自己写的文件。
    #[tokio::test]
    // 刻意持 std Mutex 跨 await：锁串行化的正是进程全局 AGENTOS_USER_*，
    // 必须覆盖 load_config 的整个生命周期（测试独占语义）。
    #[allow(clippy::await_holding_lock)]
    async fn build_plugin_loader_wires_config_root() {
        let plugins_dir = tempfile::tempdir().unwrap();
        let config_dir = tempfile::tempdir().unwrap();

        // 隔离用户空间：本用例自带 config_dir 即用户配置层根，无残留文件
        struct UserRootGuard {
            _lock: std::sync::MutexGuard<'static, ()>,
            root: Option<String>,
            cfg: Option<String>,
        }
        impl Drop for UserRootGuard {
            fn drop(&mut self) {
                match self.root.take() {
                    Some(v) => std::env::set_var(agentos_core::user_space::USER_ROOT_ENV, v),
                    None => std::env::remove_var(agentos_core::user_space::USER_ROOT_ENV),
                }
                match self.cfg.take() {
                    Some(v) => std::env::set_var(agentos_core::user_space::USER_CONFIG_DIR_ENV, v),
                    None => std::env::remove_var(agentos_core::user_space::USER_CONFIG_DIR_ENV),
                }
            }
        }
        let user_root = tempfile::tempdir().unwrap();
        let _user_guard = UserRootGuard {
            _lock: user_space_env_lock(),
            root: std::env::var(agentos_core::user_space::USER_ROOT_ENV).ok(),
            cfg: std::env::var(agentos_core::user_space::USER_CONFIG_DIR_ENV).ok(),
        };
        std::env::set_var(agentos_core::user_space::USER_ROOT_ENV, user_root.path());
        std::env::set_var(
            agentos_core::user_space::USER_CONFIG_DIR_ENV,
            user_root.path().join("config"),
        );

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

    /// 线程治理可调项：未设置 → 默认（blocking 16、栈 2048KiB = tokio 默认
    /// 2MiB）；显式设置生效（KiB → 字节换算）；非法/非正值回落默认。环境变量
    /// 是进程全局态，分支串在一个测试内执行（同文件既有测试同款约定）。
    #[test]
    fn thread_governance_env_defaults_and_overrides() {
        std::env::remove_var("AGENTOS_MAX_BLOCKING_THREADS");
        std::env::remove_var("AGENTOS_THREAD_STACK_KIB");
        assert_eq!(resolve_max_blocking_threads(), 512);
        assert_eq!(resolve_thread_stack_size(), 2048 * 1024);

        std::env::set_var("AGENTOS_MAX_BLOCKING_THREADS", "32");
        std::env::set_var("AGENTOS_THREAD_STACK_KIB", "512");
        assert_eq!(resolve_max_blocking_threads(), 32);
        assert_eq!(resolve_thread_stack_size(), 512 * 1024);

        std::env::set_var("AGENTOS_MAX_BLOCKING_THREADS", "not-a-number");
        std::env::set_var("AGENTOS_THREAD_STACK_KIB", "0");
        assert_eq!(resolve_max_blocking_threads(), 512, "非法值回落默认");
        assert_eq!(resolve_thread_stack_size(), 2048 * 1024, "非正值回落默认");

        std::env::remove_var("AGENTOS_MAX_BLOCKING_THREADS");
        std::env::remove_var("AGENTOS_THREAD_STACK_KIB");
    }

    /// 水位配置：**默认 None（关闭）**；显式正值启用；0/非法值=关闭；
    /// 启用时 _MIN 缺省 10。环境变量进程全局态，分支串单测试执行。
    #[test]
    fn watermark_env_defaults_and_overrides() {
        std::env::remove_var("AGENTOS_MEM_WATERMARK_MB");
        std::env::remove_var("AGENTOS_MEM_WATERMARK_MIN");
        assert_eq!(resolve_watermark_config(), None, "未设置=默认关闭");

        std::env::set_var("AGENTOS_MEM_WATERMARK_MB", "500");
        std::env::set_var("AGENTOS_MEM_WATERMARK_MIN", "3");
        assert_eq!(resolve_watermark_config(), Some((500, 3)));

        // _MIN 未设/非法/非正：启用状态下回落默认 10（一维非法不拖累另一维）
        std::env::remove_var("AGENTOS_MEM_WATERMARK_MIN");
        assert_eq!(resolve_watermark_config(), Some((500, 10)));

        std::env::set_var("AGENTOS_MEM_WATERMARK_MB", "0");
        std::env::set_var("AGENTOS_MEM_WATERMARK_MIN", "0");
        assert_eq!(resolve_watermark_config(), None, "0=显式关闭");

        std::env::set_var("AGENTOS_MEM_WATERMARK_MB", "bogus");
        assert_eq!(resolve_watermark_config(), None, "非法值=关闭");

        std::env::remove_var("AGENTOS_MEM_WATERMARK_MB");
        std::env::remove_var("AGENTOS_MEM_WATERMARK_MIN");
    }

    /// 水位判定（纯函数）三组：正常（低于阈值不触发）、临界（恰等于阈值不算
    /// "超"；单次尖峰后回落不触发——连续性被打断）、持续超限（连续窗口全超
    /// 触发）。另覆盖：长样本只看最近 consecutive 个（性质：判定只依赖尾部
    /// 窗口）、样本不足不判、阈值升高翻转结果。
    #[test]
    fn watermark_breach_requires_sustained_strict_exceedance() {
        // 正常：全部低于阈值
        assert!(!watermark_breach(&[100, 200, 299], 300, 3));
        // 临界：恰好等于阈值不算超（严格大于）
        assert!(!watermark_breach(&[300, 300, 300], 300, 3));
        // 临界：尖峰后回落，连续性打断不触发（回落落在尾部窗口内）
        assert!(!watermark_breach(&[400, 400, 200, 400, 400], 300, 3));
        // 持续超限：恰好 consecutive 个全超 → 触发
        assert!(watermark_breach(&[301, 500, 900], 300, 3));
        // 性质：判定只依赖尾部 consecutive 窗口——前缀任意不影响结果
        assert!(watermark_breach(&[200, 301, 500, 900], 300, 3));
        assert!(!watermark_breach(&[900, 900, 900, 900, 200], 300, 3));
        // 样本不足 consecutive 个不判
        assert!(!watermark_breach(&[900], 300, 3));
        // consecutive = 0 防御（不判）
        assert!(!watermark_breach(&[900, 900], 300, 0));
        // 阈值升高翻转结果（单调性）
        assert!(!watermark_breach(&[301, 500, 900], 900, 3));
    }

    /// 播种与口令迁移（真实 SQLite 内存库，非 mock）：四阶段串行——
    /// A 首启播种（显式口令）、B 已存在+显式新口令=重置、C 已存在+随机生成
    /// =不动、D 首启+随机生成=播种并走 banner 分支；迁移三态：明文回写、
    /// 已哈希跳过、空表直通。环境变量进程全局态，串单测试执行（同文件约定）。
    #[tokio::test]
    async fn seed_admin_and_migrate_plaintext_passwords_lifecycle() {
        use agentos_engine::store::SqliteStore;

        fn fresh_store() -> Arc<dyn agentos_core::traits::StorageBackend> {
            Arc::new(SqliteStore::open_memory().expect("内存库创建失败"))
        }

        // ── 迁移：空表直通 ──
        let store = fresh_store();
        migrate_plaintext_passwords(store.clone())
            .await
            .expect("空表迁移应直通");

        // ── 迁移：明文行哈希化回写；已哈希行跳过 ──
        let now = chrono::Utc::now().to_rfc3339();
        let mk_user = |uid: &str, name: &str, pw: String| UserRecord {
            user_id: uid.to_string(),
            username: name.to_string(),
            password: pw,
            email: None,
            role: "user".to_string(),
            tenant_id: "default".to_string(),
            created_at: now.clone(),
            last_login_at: None,
            must_change_password: false,
        };
        store
            .create_user(&mk_user("u-plain", "plain_user", "明文口令".to_string()))
            .await
            .expect("建明文用户失败");
        let hashed = agentos_http::auth::hash_password("已是哈希").unwrap();
        store
            .create_user(&mk_user("u-hashed", "hashed_user", hashed))
            .await
            .expect("建哈希用户失败");
        migrate_plaintext_passwords(store.clone())
            .await
            .expect("明文迁移不应失败");
        let migrated = store
            .get_user_by_id("u-plain")
            .await
            .expect("查询失败")
            .expect("用户行应在");
        assert!(
            agentos_http::auth::is_password_hash(&migrated.password),
            "明文行迁移后必须是哈希"
        );
        assert!(
            agentos_http::auth::verify_password("明文口令", &migrated.password),
            "迁移哈希须能验出原口令"
        );
        let untouched = store
            .get_user_by_id("u-hashed")
            .await
            .expect("查询失败")
            .expect("用户行应在");
        assert!(
            agentos_http::auth::verify_password("已是哈希", &untouched.password),
            "已哈希行不应被改写"
        );

        // ── 播种 A：首启 + 显式口令 → 播种 admin（哈希、must_change）──
        std::env::set_var("AGENTOS_ADMIN_PASSWORD", "seed-phase-pw");
        let store = fresh_store();
        seed_admin_user(store.clone()).await;
        let admin = store
            .get_user_by_id("00000000-0000-0000-0000-000000000001")
            .await
            .expect("查询失败")
            .expect("首启必须播种 admin");
        assert_eq!(admin.username, "admin");
        assert_eq!(admin.tenant_id, "default");
        assert!(admin.must_change_password, "播种 admin 须标记改密");
        assert!(
            agentos_http::auth::verify_password("seed-phase-pw", &admin.password),
            "播种口令须与环境变量一致"
        );

        // ── 播种 B：已存在 + 显式新口令 → 重置 ──
        std::env::set_var("AGENTOS_ADMIN_PASSWORD", "reset-phase-pw");
        seed_admin_user(store.clone()).await;
        let admin = store
            .get_user_by_id("00000000-0000-0000-0000-000000000001")
            .await
            .expect("查询失败")
            .expect("admin 应在");
        assert!(
            agentos_http::auth::verify_password("reset-phase-pw", &admin.password),
            "显式新口令必须重置旧口令"
        );

        // ── 播种 C：已存在 + 随机生成 → 不重置 ──
        std::env::remove_var("AGENTOS_ADMIN_PASSWORD");
        seed_admin_user(store.clone()).await;
        let admin = store
            .get_user_by_id("00000000-0000-0000-0000-000000000001")
            .await
            .expect("查询失败")
            .expect("admin 应在");
        assert!(
            agentos_http::auth::verify_password("reset-phase-pw", &admin.password),
            "随机生成口令不得重置既有口令"
        );

        // ── 播种 D：首启 + 随机生成 → 播种且口令为合法哈希 ──
        let store = fresh_store();
        seed_admin_user(store.clone()).await;
        let admin = store
            .get_user_by_id("00000000-0000-0000-0000-000000000001")
            .await
            .expect("查询失败")
            .expect("随机首启也必须播种 admin");
        assert!(
            agentos_http::auth::is_password_hash(&admin.password),
            "随机口令同样落哈希"
        );
        std::env::remove_var("AGENTOS_ADMIN_PASSWORD");
    }

    /// 插件根目录发现（真实临时目录）：不存在根=空；二级嵌套 plugin.json 的
    /// 父目录入列；plugin.yaml 同权；同父多插件去重。
    #[test]
    fn discover_plugin_roots_parents_and_dedup() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let base = tmp.path().join("plugins");
        // 不存在的根
        assert!(discover_plugin_roots(&tmp.path().join("nope")).is_empty());

        // 空目录（无任何插件）= 空
        std::fs::create_dir_all(base.join("empty")).unwrap();
        assert!(discover_plugin_roots(&base).is_empty());

        // tools/simple/plugin.json（二级嵌套）→ 父目录 tools 入列
        std::fs::create_dir_all(base.join("tools").join("simple")).unwrap();
        std::fs::write(base.join("tools").join("simple").join("plugin.json"), "{}").unwrap();
        // 另一父下 yaml 插件 + 同父第二插件（去重）
        std::fs::create_dir_all(base.join("sys").join("a")).unwrap();
        std::fs::create_dir_all(base.join("sys").join("b")).unwrap();
        std::fs::write(base.join("sys").join("a").join("plugin.yaml"), "{}").unwrap();
        std::fs::write(base.join("sys").join("b").join("plugin.json"), "{}").unwrap();

        let mut roots = discover_plugin_roots(&base);
        roots.sort();
        let expect_tools = base.join("tools").to_string_lossy().to_string();
        let expect_sys = base.join("sys").to_string_lossy().to_string();
        assert_eq!(
            roots,
            vec![expect_sys, expect_tools],
            "父目录去重后恰为 tools 与 sys 两个扫描根"
        );
    }

    /// 用户插件目录解析：环境变量优先（含空值忽略回落），回落落 OS 数据目录
    /// 下的 agentos/plugins。
    #[test]
    fn user_plugins_dir_env_first_then_data_dir() {
        // 与 build_plugin_loader_wires_config_root 互斥：同读 AGENTOS_USER_* 进程全局态
        let _lock = user_space_env_lock();
        std::env::set_var("AGENTOS_USER_PLUGINS_DIR", "D:/custom/plugins");
        assert_eq!(
            resolve_user_plugins_dir(),
            Some(PathBuf::from("D:/custom/plugins"))
        );

        // 空白值视同未设置 → 回落 OS 数据目录
        std::env::set_var("AGENTOS_USER_PLUGINS_DIR", "   ");
        let fallback = resolve_user_plugins_dir();
        assert!(fallback.unwrap().to_string_lossy().contains("agentos"));

        std::env::remove_var("AGENTOS_USER_PLUGINS_DIR");
        assert!(
            resolve_user_plugins_dir().is_some(),
            "Windows 下 data_dir 恒可用"
        );
    }

    /// 私有内存采样（Windows 真实 API 冒烟）：进程自身采样必然成功且量级
    /// 合理（> 1MB——测试进程不可能更小）。
    #[cfg(windows)]
    #[test]
    fn sample_private_bytes_returns_plausible_value() {
        let bytes = sample_private_bytes().expect("进程采样不应失败");
        assert!(bytes > 1024 * 1024, "采样值量级不合理: {bytes}");
    }
}

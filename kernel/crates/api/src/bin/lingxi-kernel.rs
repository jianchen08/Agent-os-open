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
//! - LINGXI_KERNEL_PORT：监听端口（默认 9100）
//! - LINGXI_KERNEL_HOST：监听地址（默认 0.0.0.0）
//! - LINGXI_PLUGINS_DIR：插件根目录（默认 plugins/shared）

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use lingxi_api::{routes::AppState, start_server};
use lingxi_core::traits::{
    CapabilityRegistry, PluginLoader, ToolDescriptor,
};
use lingxi_core::types::{ToolCategory, ToolSource};
use lingxi_engine::{AdrEngineImpl, SqliteStore};
use lingxi_invoker::PluginInvokerImpl;
use lingxi_plugin_loader::{CapabilityRegistryImpl, PluginLoaderImpl};
use tracing::{info, warn};
use tracing_subscriber::{fmt, prelude::*};

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

    let host = std::env::var("LINGXI_KERNEL_HOST").unwrap_or_else(|_| "0.0.0.0".into());
    let port: u16 = std::env::var("LINGXI_KERNEL_PORT")
        .unwrap_or_else(|_| "9100".into())
        .parse()
        .unwrap_or(9100);

    let addr: SocketAddr = format!("{}:{}", host, port).parse()?;

    info!(target: "lingxi-kernel", "========================================");
    info!(target: "lingxi-kernel", "  Lingxi AgentOS 0.2 内核启动");
    info!(target: "lingxi-kernel", "  监听地址: http://{}", addr);
    info!(target: "lingxi-kernel", "  健康检查: http://{}/health", addr);
    info!(target: "lingxi-kernel", "  WebSocket: ws://{}/ws", addr);
    info!(target: "lingxi-kernel", "  Schema: http://{}/api/v1/schema", addr);
    info!(target: "lingxi-kernel", "========================================");

    // ── 插件系统初始化 ──

    // 确定插件目录
    let plugins_dir = std::env::var("LINGXI_PLUGINS_DIR")
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
        target: "lingxi-kernel",
        "Plugin directory: {}",
        plugins_dir.display()
    );

    // 创建插件加载器——以 plugins/shared/ 为内置根
    let loader = PluginLoaderImpl::new(&plugins_dir, None);

    // 递归扫描插件目录——scan_root 只扫描一级子目录，
    // plugins/shared/ 的结构是 tools/simple/plugin.json（二级嵌套），
    // 需要收集所有包含 plugin.json 的目录的父目录传给 discover。
    let root_paths = discover_plugin_roots(&plugins_dir);

    info!(
        target: "lingxi-kernel",
        "Scanning {} root directories under {}",
        root_paths.len(),
        plugins_dir.display()
    );

    let manifests = loader.discover(&root_paths.iter().map(|s| s.as_str()).collect::<Vec<_>>()).await.unwrap_or_else(|e| {
        warn!(
            target: "lingxi-kernel",
            "Failed to discover plugins: {}. Continuing with empty plugin list.", e.message
        );
        Vec::new()
    });

    info!(
        target: "lingxi-kernel",
        "Discovered {} plugin manifests",
        manifests.len()
    );

    // 创建能力注册表
    let registry = Arc::new(CapabilityRegistryImpl::new());

    // 将 manifest 中声明的工具注册到 CapabilityRegistry
    let mut tool_count = 0usize;
    for manifest in &manifests {
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
                    == lingxi_core::traits::HostType::Sidecar
                {
                    ToolSource::Mcp
                } else {
                    ToolSource::Builtin
                },
            };
            registry.register_tool(&manifest.id, descriptor);
            tool_count += 1;
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
        target: "lingxi-kernel",
        "Registered {} tools from {} plugins",
        tool_count,
        manifests.len()
    );

    // 初始化管道引擎
    // DEBT: 使用内存数据库，生产环境应使用持久化文件。ceiling: 进程重启丢失运行历史。
    // upgrade: 当需要跨重启会话持久化时，切换到 SqliteStore::open("lingxi.db")。
    let store = Arc::new(SqliteStore::open_memory()?);

    // 创建真实插件调用器——通过 MCP stdio fork Python sidecar 执行插件
    let loader_arc = Arc::new(loader);
    let invoker = Arc::new(PluginInvokerImpl::new(loader_arc.clone()));
    let engine = Arc::new(AdrEngineImpl::new(store, invoker, "default"));

    info!(
        target: "lingxi-kernel",
        "Pipeline engine initialized (in-memory SQLite)"
    );

    // 构建 AppState
    let state = AppState::with_plugins(manifests.clone(), registry, engine);
    start_server(addr, state).await?;

    Ok(())
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


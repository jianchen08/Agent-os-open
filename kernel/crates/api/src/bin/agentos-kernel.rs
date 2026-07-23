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
    CapabilityRegistry, PluginLoader, ToolDescriptor,
};
use agentos_core::types::{ToolCategory, ToolSource};
use agentos_engine::{AdrEngineImpl, SqliteStore};
use agentos_invoker::PluginInvokerImpl;
use agentos_plugin_loader::{CapabilityRegistryImpl, PluginLoaderImpl};
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
        "Registered {} tools from {} plugins",
        tool_count,
        manifests.len()
    );

    // 初始化管道引擎
    // DEBT: 使用内存数据库，生产环境应使用持久化文件。ceiling: 进程重启丢失运行历史。
    // upgrade: 当需要跨重启会话持久化时，切换到 SqliteStore::open("agentos.db")。
    let store = Arc::new(SqliteStore::open_memory()?);

    // 创建真实插件调用器——通过 MCP stdio fork Python sidecar 执行插件
    let loader_arc = Arc::new(loader);
    let invoker = Arc::new(PluginInvokerImpl::new(loader_arc.clone()));
    let engine = Arc::new(AdrEngineImpl::new(store.clone(), invoker.clone(), "default"));

    // 启用 sidecar→内核反向 capability 通道（审批暂停/恢复、复盘调管道、event-bus 的地基）
    let router = Arc::new(KernelCapabilityRouter::new(engine.clone()));
    invoker.set_router(router);

    info!(
        target: "agentos-kernel",
        "Pipeline engine initialized (in-memory SQLite, reverse capability channel enabled)"
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

    // 构建 AppState（注入 pipeline_config / step_library / invoker / store / plugin_ids / project_root）
    let project_root = config_root
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."));
    let store_dyn: Arc<dyn agentos_core::traits::StorageBackend> = store.clone();
    let invoker_dyn: Arc<dyn agentos_core::traits::PluginInvoker> = invoker.clone();
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
    );
    start_server(addr, state).await?;

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

        // P0-1 验收：非空 + 含 models 节（对应 config_refs=["models"] 的插件）
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



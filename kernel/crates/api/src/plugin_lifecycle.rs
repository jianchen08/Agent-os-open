//! 插件生命周期公共逻辑（启动期注册 + 运行时新增插件注册）。
//!
//! 把 main 启动期"遍历 manifest 注册 tools/route_signals 到 capability_registry"的逻辑
//! 抽成公共函数，供：
//! - 启动期（agentos-kernel.rs）
//! - 运行时新增插件（reload-all 端点发现新插件后注册）
//! 复用，避免逻辑重复。

use agentos_core::traits::{CapabilityRegistry, PluginManifest, PluginType, ToolDescriptor};
use agentos_core::types::{ToolCategory, ToolSource};
use agentos_plugin_loader::CapabilityRegistryImpl;

/// 把单个插件的 tools（仅 plugin_type==Tool）和 route_signals 注册到 capability_registry。
///
/// 对齐 main 启动期 ADR 附录D①：只有 tool 类型插件的 capabilities.tools 才是"给大模型
/// 调用的工具"。pipeline/system 的工具是内部入口，不暴露给 LLM。
///
/// 返回注册的 tool 数量。
pub fn register_plugin_capabilities(
    manifest: &PluginManifest,
    registry: &CapabilityRegistryImpl,
) -> usize {
    let mut count = 0usize;
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
                source: if manifest.host_type == agentos_core::traits::HostType::Sidecar {
                    ToolSource::Mcp
                } else {
                    ToolSource::Builtin
                },
            };
            registry.register_tool(&manifest.id, descriptor);
            count += 1;
        }
    }

    // 注册路由信号
    if !manifest.capabilities.route_signals.is_empty() {
        registry.register_route_signals(
            &manifest.id,
            manifest.capabilities.route_signals.clone(),
        );
    }

    count
}

/// 批量注册多个插件（启动期或 reload-all 发现新增时用）。
///
/// `existing_ids`：已注册的 plugin_id 集合，用于跳过重复（仅注册新增的）。
/// 返回 (新增注册的插件 id 列表, 注册的 tool 总数)。
pub fn register_new_plugins(
    all_manifests: &[PluginManifest],
    existing_ids: &std::collections::HashSet<String>,
    registry: &CapabilityRegistryImpl,
) -> (Vec<String>, usize) {
    let mut new_ids = Vec::new();
    let mut total_tools = 0usize;
    for manifest in all_manifests {
        if existing_ids.contains(&manifest.id) {
            continue;
        }
        total_tools += register_plugin_capabilities(manifest, registry);
        new_ids.push(manifest.id.clone());
    }
    (new_ids, total_tools)
}

/// 判断插件是否声明了 http_endpoints（用于 reload-all 的诚实降级提示）。
pub fn has_http_endpoints(manifest: &PluginManifest) -> bool {
    !manifest.http_endpoints.is_empty()
}

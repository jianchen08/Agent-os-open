//! 能力注册表 + 依赖解析器
//!
//! [来源: docs/tasks/task_05_plugin_system.md AC-04-3]

use std::collections::{HashMap, HashSet};

use async_trait::async_trait;
use lingxi_core::traits::{
    CapabilityRegistry, Dependency, DependencyError, DependencyResolver, ResourceDescriptor,
    ToolDescriptor,
};
use lingxi_core::types::{RouteType, ToolCategory};
use parking_lot::RwLock;
use tracing::info;

/// 能力注册表实现。
///
/// 管理三类能力：
/// 1. Tools: 工具插件/系统插件提供的工具
/// 2. Resources: 插件暴露的数据源
/// 3. RouteSignals: 管道插件声明的路由信号
pub struct CapabilityRegistryImpl {
    tools: RwLock<HashMap<String, ToolDescriptor>>,
    tools_by_plugin: RwLock<HashMap<String, Vec<String>>>,
    resources: RwLock<HashMap<String, ResourceDescriptor>>,
    resources_by_plugin: RwLock<HashMap<String, Vec<String>>>,
    route_signals: RwLock<HashSet<RouteType>>,
    route_signals_by_plugin: RwLock<HashMap<String, Vec<RouteType>>>,
}

impl CapabilityRegistryImpl {
    pub fn new() -> Self {
        Self {
            tools: RwLock::new(HashMap::new()),
            tools_by_plugin: RwLock::new(HashMap::new()),
            resources: RwLock::new(HashMap::new()),
            resources_by_plugin: RwLock::new(HashMap::new()),
            route_signals: RwLock::new(HashSet::new()),
            route_signals_by_plugin: RwLock::new(HashMap::new()),
        }
    }
}

impl Default for CapabilityRegistryImpl {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl CapabilityRegistry for CapabilityRegistryImpl {
    fn register_tool(&self, plugin_id: &str, tool: ToolDescriptor) {
        let name = tool.name.clone();
        self.tools.write().insert(name.clone(), tool);
        self.tools_by_plugin
            .write()
            .entry(plugin_id.to_string())
            .or_default()
            .push(name.clone());
        info!("Tool registered: plugin={} tool={}", plugin_id, name);
    }

    fn unregister_tools(&self, plugin_id: &str) {
        let mut by_plugin = self.tools_by_plugin.write();
        if let Some(names) = by_plugin.remove(plugin_id) {
            let mut tools = self.tools.write();
            for name in &names {
                tools.remove(name);
            }
            info!(
                "Tools unregistered: plugin={} count={}",
                plugin_id,
                names.len()
            );
        }
    }

    fn get_tool(&self, name: &str) -> Option<ToolDescriptor> {
        self.tools.read().get(name).cloned()
    }

    fn list_tools(&self) -> Vec<ToolDescriptor> {
        self.tools.read().values().cloned().collect()
    }

    fn list_tools_by_category(&self, category: &ToolCategory) -> Vec<ToolDescriptor> {
        self.tools
            .read()
            .values()
            .filter(|t| &t.category == category)
            .cloned()
            .collect()
    }

    fn register_resource(&self, plugin_id: &str, resource: ResourceDescriptor) {
        let uri = resource.uri.clone();
        self.resources.write().insert(uri.clone(), resource);
        self.resources_by_plugin
            .write()
            .entry(plugin_id.to_string())
            .or_default()
            .push(uri.clone());
        info!("Resource registered: plugin={} uri={}", plugin_id, uri);
    }

    fn unregister_resources(&self, plugin_id: &str) {
        let mut by_plugin = self.resources_by_plugin.write();
        if let Some(uris) = by_plugin.remove(plugin_id) {
            let mut resources = self.resources.write();
            for uri in &uris {
                resources.remove(uri);
            }
        }
    }

    fn list_resources(&self) -> Vec<ResourceDescriptor> {
        self.resources.read().values().cloned().collect()
    }

    fn register_route_signals(&self, plugin_id: &str, signals: Vec<RouteType>) {
        let mut sig_set = self.route_signals.write();
        let mut by_plugin = self.route_signals_by_plugin.write();
        for sig in &signals {
            sig_set.insert(sig.clone());
        }
        by_plugin.insert(plugin_id.to_string(), signals);
    }

    fn has_route_signal(&self, signal: &RouteType) -> bool {
        self.route_signals.read().contains(signal)
    }

    fn clear_plugin(&self, plugin_id: &str) {
        self.unregister_tools(plugin_id);
        self.unregister_resources(plugin_id);
        let mut by_plugin = self.route_signals_by_plugin.write();
        if let Some(signals) = by_plugin.remove(plugin_id) {
            let mut sig_set = self.route_signals.write();
            for sig in &signals {
                sig_set.remove(sig);
            }
        }
    }
}

/// 依赖解析器实现。
///
/// 根据插件 manifest 中的 `dependencies` 字段构建依赖图并执行拓扑排序。
pub struct DependencyResolverImpl {
    deps: RwLock<HashMap<String, Vec<Dependency>>>,
}

impl DependencyResolverImpl {
    pub fn new() -> Self {
        Self {
            deps: RwLock::new(HashMap::new()),
        }
    }

    fn topological_sort(&self) -> Result<Vec<String>, DependencyError> {
        let deps = self.deps.read();

        // 收集所有节点
        let mut all_nodes: HashSet<String> = HashSet::new();
        for (plugin_id, dep_list) in deps.iter() {
            all_nodes.insert(plugin_id.clone());
            for dep in dep_list {
                all_nodes.insert(dep.plugin_id.clone());
            }
        }

        // 构建邻接表（被依赖者 → 依赖者）
        let mut graph: HashMap<String, Vec<String>> = HashMap::new();
        let mut in_degree: HashMap<String, usize> = HashMap::new();

        for node in &all_nodes {
            graph.entry(node.clone()).or_default();
            in_degree.entry(node.clone()).or_insert(0);
        }

        for (plugin_id, dep_list) in deps.iter() {
            for dep in dep_list {
                // dep.plugin_id 必须先于 plugin_id 加载
                graph
                    .entry(dep.plugin_id.clone())
                    .or_default()
                    .push(plugin_id.clone());
                *in_degree.entry(plugin_id.clone()).or_insert(0) += 1;
            }
        }

        // Kahn's algorithm
        let mut queue: Vec<String> = in_degree
            .iter()
            .filter(|(_, &deg)| deg == 0)
            .map(|(k, _)| k.clone())
            .collect();
        queue.sort();

        let mut result = Vec::new();
        let mut remaining = in_degree.clone();

        while let Some(node) = queue.first().cloned() {
            queue.remove(0);
            result.push(node.clone());

            if let Some(neighbors) = graph.get(&node) {
                for neighbor in neighbors {
                    if let Some(deg) = remaining.get_mut(neighbor) {
                        *deg -= 1;
                        if *deg == 0 {
                            // 插入保持排序
                            let pos = queue.binary_search(neighbor).unwrap_or_else(|e| e);
                            queue.insert(pos, neighbor.clone());
                        }
                    }
                }
            }
        }

        // 检测循环依赖
        if result.len() != all_nodes.len() {
            let cycle: Vec<String> = all_nodes
                .iter()
                .filter(|n| !result.contains(*n))
                .cloned()
                .collect();
            return Err(DependencyError::Circular { cycle });
        }

        Ok(result)
    }
}

impl Default for DependencyResolverImpl {
    fn default() -> Self {
        Self::new()
    }
}

impl DependencyResolver for DependencyResolverImpl {
    fn add_dependency(&self, plugin_id: &str, dep: &Dependency) {
        self.deps
            .write()
            .entry(plugin_id.to_string())
            .or_default()
            .push(dep.clone());
    }

    fn resolve(&self) -> Result<Vec<String>, DependencyError> {
        self.topological_sort()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use lingxi_core::types::ToolSource;
    use serde_json::json;

    fn make_tool_descriptor(name: &str, plugin_id: &str, category: ToolCategory) -> ToolDescriptor {
        ToolDescriptor {
            name: name.to_string(),
            description: format!("Tool {}", name),
            plugin_id: plugin_id.to_string(),
            input_schema: json!({}),
            output_schema: None,
            category,
            source: ToolSource::Builtin,
        }
    }

    fn make_resource_descriptor(uri: &str, plugin_id: &str) -> ResourceDescriptor {
        ResourceDescriptor {
            uri: uri.to_string(),
            name: format!("Resource {}", uri),
            plugin_id: plugin_id.to_string(),
            description: None,
            mime_type: "application/json".to_string(),
        }
    }

    #[test]
    fn test_register_and_get_tool() {
        let registry = CapabilityRegistryImpl::new();
        let tool = make_tool_descriptor("search", "plugin_a", ToolCategory::Search);
        registry.register_tool("plugin_a", tool);

        let found = registry.get_tool("search");
        assert!(found.is_some());
        assert_eq!(found.unwrap().plugin_id, "plugin_a");
    }

    #[test]
    fn test_unregister_tools() {
        let registry = CapabilityRegistryImpl::new();
        registry.register_tool(
            "plugin_a",
            make_tool_descriptor("tool1", "plugin_a", ToolCategory::File),
        );
        registry.register_tool(
            "plugin_a",
            make_tool_descriptor("tool2", "plugin_a", ToolCategory::File),
        );

        assert_eq!(registry.list_tools().len(), 2);

        registry.unregister_tools("plugin_a");
        assert_eq!(registry.list_tools().len(), 0);
    }

    #[test]
    fn test_list_tools_by_category() {
        let registry = CapabilityRegistryImpl::new();
        registry.register_tool(
            "plugin_a",
            make_tool_descriptor("search", "plugin_a", ToolCategory::Search),
        );
        registry.register_tool(
            "plugin_b",
            make_tool_descriptor("file_read", "plugin_b", ToolCategory::File),
        );
        registry.register_tool(
            "plugin_c",
            make_tool_descriptor("web_fetch", "plugin_c", ToolCategory::Web),
        );

        let search_tools = registry.list_tools_by_category(&ToolCategory::Search);
        assert_eq!(search_tools.len(), 1);
        assert_eq!(search_tools[0].name, "search");
    }

    #[test]
    fn test_register_and_list_resources() {
        let registry = CapabilityRegistryImpl::new();
        registry.register_resource(
            "plugin_a",
            make_resource_descriptor("config://app", "plugin_a"),
        );

        let resources = registry.list_resources();
        assert_eq!(resources.len(), 1);
        assert_eq!(resources[0].uri, "config://app");
    }

    #[test]
    fn test_route_signals() {
        let registry = CapabilityRegistryImpl::new();
        registry.register_route_signals("plugin_a", vec![RouteType::NextLlm, RouteType::End]);

        assert!(registry.has_route_signal(&RouteType::NextLlm));
        assert!(registry.has_route_signal(&RouteType::End));
        assert!(!registry.has_route_signal(&RouteType::Wait));
    }

    #[test]
    fn test_clear_plugin() {
        let registry = CapabilityRegistryImpl::new();
        registry.register_tool(
            "plugin_a",
            make_tool_descriptor("tool1", "plugin_a", ToolCategory::File),
        );
        registry.register_resource("plugin_a", make_resource_descriptor("res://a", "plugin_a"));
        registry.register_route_signals("plugin_a", vec![RouteType::End]);

        registry.clear_plugin("plugin_a");

        assert_eq!(registry.list_tools().len(), 0);
        assert_eq!(registry.list_resources().len(), 0);
        assert!(!registry.has_route_signal(&RouteType::End));
    }

    #[test]
    fn test_dependency_resolution_simple() {
        let resolver = DependencyResolverImpl::new();
        // A depends on B → B should load first
        resolver.add_dependency(
            "plugin_a",
            &Dependency {
                plugin_id: "plugin_b".to_string(),
                optional: false,
                min_version: None,
            },
        );

        let order = resolver.resolve().unwrap();
        let a_pos = order.iter().position(|x| x == "plugin_a").unwrap();
        let b_pos = order.iter().position(|x| x == "plugin_b").unwrap();
        assert!(b_pos < a_pos, "B should load before A");
    }

    #[test]
    fn test_dependency_resolution_chain() {
        let resolver = DependencyResolverImpl::new();
        // C depends on B, B depends on A → order: A, B, C
        resolver.add_dependency(
            "plugin_c",
            &Dependency {
                plugin_id: "plugin_b".to_string(),
                optional: false,
                min_version: None,
            },
        );
        resolver.add_dependency(
            "plugin_b",
            &Dependency {
                plugin_id: "plugin_a".to_string(),
                optional: false,
                min_version: None,
            },
        );

        let order = resolver.resolve().unwrap();
        let a_pos = order.iter().position(|x| x == "plugin_a").unwrap();
        let b_pos = order.iter().position(|x| x == "plugin_b").unwrap();
        let c_pos = order.iter().position(|x| x == "plugin_c").unwrap();
        assert!(a_pos < b_pos);
        assert!(b_pos < c_pos);
    }

    #[test]
    fn test_dependency_circular_detected() {
        let resolver = DependencyResolverImpl::new();
        // A → B → A (circular)
        resolver.add_dependency(
            "plugin_a",
            &Dependency {
                plugin_id: "plugin_b".to_string(),
                optional: false,
                min_version: None,
            },
        );
        resolver.add_dependency(
            "plugin_b",
            &Dependency {
                plugin_id: "plugin_a".to_string(),
                optional: false,
                min_version: None,
            },
        );

        let result = resolver.resolve();
        assert!(result.is_err());
        match result {
            Err(DependencyError::Circular { cycle }) => {
                assert!(cycle.contains(&"plugin_a".to_string()));
                assert!(cycle.contains(&"plugin_b".to_string()));
            }
            _ => panic!("Expected Circular error"),
        }
    }

    #[test]
    fn test_dependency_no_deps() {
        let resolver = DependencyResolverImpl::new();
        let order = resolver.resolve().unwrap();
        assert!(order.is_empty());
    }
}

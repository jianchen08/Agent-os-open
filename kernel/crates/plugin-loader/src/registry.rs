//! 能力注册表 + 依赖解析器
//!
//! [来源: docs/tasks/task_05_plugin_system.md AC-04-3]

use std::collections::{HashMap, HashSet};

use async_trait::async_trait;
use agentos_core::traits::{
    CapabilityRegistry, Dependency, DependencyError, DependencyResolver, HttpEndpoint,
    HttpRouteDescriptor, ResourceDescriptor, ToolDescriptor,
};
use agentos_core::types::{RouteType, ToolCategory};
use parking_lot::RwLock;
use tracing::info;

/// 内核保留路径段 denylist（ADR 附录 E.1.3）：插件端点 path 任一段命中即拒。
const KERNEL_RESERVED_PATH_SEGMENTS: &[&str] = &["ws", "health"];

/// HTTP 路由 key：path + method 唯一标识一个端点（ADR §3.3：path+method 才算冲突）。
#[derive(Debug, Clone, Hash, PartialEq, Eq)]
struct RouteKey {
    path: String,
    method: String,
}

/// 能力注册表实现。
///
/// 管理四类能力：
/// 1. Tools: 工具插件/系统插件提供的工具
/// 2. Resources: 插件暴露的数据源
/// 3. RouteSignals: 管道插件声明的路由信号
/// 4. HttpRoutes: 插件贡献的 HTTP 端点（ADR §3.3）
pub struct CapabilityRegistryImpl {
    tools: RwLock<HashMap<String, ToolDescriptor>>,
    tools_by_plugin: RwLock<HashMap<String, Vec<String>>>,
    resources: RwLock<HashMap<String, ResourceDescriptor>>,
    resources_by_plugin: RwLock<HashMap<String, Vec<String>>>,
    route_signals: RwLock<HashSet<RouteType>>,
    route_signals_by_plugin: RwLock<HashMap<String, Vec<RouteType>>>,
    /// HTTP 端点维度（ADR §3.3）：RouteKey → 已注册描述符。
    http_routes: RwLock<HashMap<RouteKey, HttpRouteDescriptor>>,
    /// 插件 → 其注册的 RouteKey 列表（注销时清除用）。
    http_routes_by_plugin: RwLock<HashMap<String, Vec<RouteKey>>>,
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
            http_routes: RwLock::new(HashMap::new()),
            http_routes_by_plugin: RwLock::new(HashMap::new()),
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
        by_plugin.insert(plugin_id.to_string(), signals);
        // 重建全局集合为「所有 plugin 当前 signals 的并集」。旧实现只 insert 不
        // 移除，重注册或部分卸载后旧 signal 永久残留导致 has_route_signal 永真；
        // 此处重建保证全局集合始终反映当前并集，多 plugin 共享同一 signal 时
        // 只要还有任一 plugin 持有即保留。
        sig_set.clear();
        for sigs in by_plugin.values() {
            for sig in sigs {
                sig_set.insert(sig.clone());
            }
        }
    }

    fn has_route_signal(&self, signal: &RouteType) -> bool {
        self.route_signals.read().contains(signal)
    }

    fn register_http_route(
        &self,
        plugin_id: &str,
        endpoint: HttpEndpoint,
    ) -> Result<HttpRouteDescriptor, String> {
        // 路由治理（ADR 附录 E.1.3）：注册期校验命名空间 + denylist。
        if let Err(reason) = validate_http_route_path(plugin_id, &endpoint.path) {
            info!(
                "HTTP route rejected: plugin={} path={} reason={}",
                plugin_id, endpoint.path, reason
            );
            return Err(reason);
        }

        let key = RouteKey {
            path: endpoint.path.clone(),
            method: endpoint.method.clone(),
        };
        let descriptor = HttpRouteDescriptor::new(plugin_id.to_string(), endpoint);

        let mut routes = self.http_routes.write();
        // 冲突检测：同 path+method fail-closed（不静默覆盖）。
        if routes.contains_key(&key) {
            let reason = format!(
                "http route conflict: {} {} already registered (plugin={})",
                key.method, key.path, plugin_id
            );
            info!("{}", reason);
            return Err(reason);
        }
        routes.insert(key.clone(), descriptor.clone());
        self.http_routes_by_plugin
            .write()
            .entry(plugin_id.to_string())
            .or_default()
            .push(key);
        info!(
            "HTTP route registered: plugin={} {} {}",
            plugin_id, descriptor.endpoint.method, descriptor.endpoint.path
        );
        Ok(descriptor)
    }

    fn list_http_routes(&self) -> Vec<HttpRouteDescriptor> {
        self.http_routes.read().values().cloned().collect()
    }

    fn find_http_route(&self, path: &str, method: &str) -> Option<HttpRouteDescriptor> {
        let routes = self.http_routes.read();
        // 1) 精确匹配（无 param 路由的快路径）。
        let key = RouteKey {
            path: path.to_string(),
            method: method.to_string(),
        };
        if let Some(d) = routes.get(&key) {
            return Some(d.clone());
        }
        // 2) 模板匹配：支持 manifest 里 {param}（单段）与 {param:path}（多段通配）。
        // 只在 method 匹配的候选里找；模板按"段数从长到短、通配优先级低"扫，
        // 第一个 match 的返回（注册期冲突检测保证同模板不重复，故无歧义）。
        routes
            .values()
            .find(|d| d.endpoint.method.eq_ignore_ascii_case(method) && path_matches_template(&d.endpoint.path, path))
            .cloned()
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
        // 清除 HTTP 路由
        let mut http_by_plugin = self.http_routes_by_plugin.write();
        if let Some(keys) = http_by_plugin.remove(plugin_id) {
            let mut routes = self.http_routes.write();
            for k in &keys {
                routes.remove(k);
            }
        }
    }
}

/// 校验插件 HTTP 端点 path（ADR 附录 E.1.3 路由治理）。
///
/// 规则（注册期逐项校验，不符即拒绝）：
/// 1. **强制命名空间**：path 必须以 `/ext/{plugin_id}/` 为前缀（或恰好 `/ext/{plugin_id}`）；
/// 2. **denylist 段**：path 拆段后不得含内核保留段 `ws` / `health`；
/// 3. **denylist 子路径**：path 不得包含 `api/v1`（防 `/ext/{pid}/api/v1/...` 越界）。
fn validate_http_route_path(plugin_id: &str, path: &str) -> Result<(), String> {
    // 规则 1：强制 /ext/{plugin_id}/** 命名空间。
    let expected_ns = format!("/ext/{}/", plugin_id);
    let exact_ns = format!("/ext/{}", plugin_id);
    if path != exact_ns && !path.starts_with(&expected_ns) {
        return Err(format!(
            "http route path '{}' must be under namespace '/ext/{}/**'",
            path, plugin_id
        ));
    }

    // 规则 2：denylist 段（ws / health）。
    for seg in path.split('/') {
        if KERNEL_RESERVED_PATH_SEGMENTS.contains(&seg) {
            return Err(format!(
                "http route path '{}' contains kernel-reserved segment '{}'",
                path, seg
            ));
        }
    }

    // 规则 3：denylist 子路径 api/v1（覆盖 /api/v1/* 与 /ext/{pid}/api/v1/*）。
    if path.contains("api/v1") {
        return Err(format!(
            "http route path '{}' contains kernel-reserved subpath 'api/v1'",
            path
        ));
    }
    Ok(())
}

/// 判断 `incoming` 请求路径是否匹配已注册的 `template`（manifest path）。
///
/// 模板段约定（对齐 FastAPI/OpenAPI，插件 manifest 沿用）：
/// - `{name}` —— 匹配**单段**（该段不含 `/`）。
/// - `{name:path}` —— 匹配**剩余多段**（含 `/`，贪婪到末尾，最多一个且须在末尾）。
/// - 其他段 —— 字面精确匹配。
///
/// 例：模板 `/ext/p/models/{model_id}` 匹配 `/ext/p/models/gpt-4`；
/// `/ext/p/generic/{config_path:path}` 匹配 `/ext/p/generic/a/b/c`。
pub(crate) fn path_matches_template(template: &str, incoming: &str) -> bool {
    // 归一化：去尾部斜杠差异，空路径视作 "/"。
    let norm = |s: &str| -> String {
        let t = s.trim_end_matches('/');
        if t.is_empty() { "/".to_string() } else { t.to_string() }
    };
    let template = norm(template);
    let incoming = norm(incoming);
    if template == incoming {
        return true;
    }
    let t_segs: Vec<&str> = template.split('/').collect();
    let i_segs: Vec<&str> = incoming.split('/').collect();
    // 段数必须相等（多段通配 {x:path} 例外，见下）。
    let has_catchall = t_segs.iter().any(|s| {
        s.starts_with('{') && s.ends_with('}') && s.contains(":path")
    });
    if !has_catchall && t_segs.len() != i_segs.len() {
        return false;
    }
    if has_catchall && t_segs.len() > i_segs.len() {
        // 模板段比实际多（通配至少要 1 段）→ 不匹配。
        return false;
    }
    // 逐段比对；遇到通配段，剩余 incoming 全归它。
    let mut i = 0usize;
    for (ti, ts) in t_segs.iter().enumerate() {
        if ts.starts_with('{') && ts.ends_with('}') {
            if ts.contains(":path") {
                // 多段通配：吃掉 incoming 剩余全部段（至少 1 段，由上面段数检查保证）。
                // 末尾通配（约定 catch-all 必在末尾）。
                let _ = ti; // 仅用 incoming 剩余
                return i < i_segs.len();
            }
            // 单段 {param}：吃掉 incoming 当前一段（必须非空）。
            if i >= i_segs.len() || i_segs[i].is_empty() {
                return false;
            }
            i += 1;
        } else {
            // 字面段：精确匹配。
            if i >= i_segs.len() || i_segs[i] != *ts {
                return false;
            }
            i += 1;
        }
    }
    // 全部段消费完且无剩余（catch-all 已提前 return）。
    i == i_segs.len()
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
    use agentos_core::types::ToolSource;
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
            ui: None,
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

    // ── HTTP param-route 模板匹配测试（4c 解锁）──

    #[test]
    fn test_path_matches_template_static() {
        assert!(path_matches_template("/ext/p/foo", "/ext/p/foo"));
        assert!(!path_matches_template("/ext/p/foo", "/ext/p/bar"));
    }

    #[test]
    fn test_path_matches_template_single_param() {
        // {model_id} 单段
        assert!(path_matches_template("/ext/p/models/{model_id}", "/ext/p/models/gpt-4"));
        assert!(path_matches_template("/ext/p/models/{model_id}", "/ext/p/models/claude-3"));
        // 单段不跨 /
        assert!(!path_matches_template("/ext/p/models/{model_id}", "/ext/p/models/a/b"));
        // 段数不符
        assert!(!path_matches_template("/ext/p/models/{model_id}", "/ext/p/models"));
    }

    #[test]
    fn test_path_matches_template_catchall_param() {
        // {config_path:path} 多段通配
        assert!(path_matches_template(
            "/ext/p/generic/{config_path:path}",
            "/ext/p/generic/a"
        ));
        assert!(path_matches_template(
            "/ext/p/generic/{config_path:path}",
            "/ext/p/generic/models/llm"
        ));
        assert!(path_matches_template(
            "/ext/p/generic/{config_path:path}",
            "/ext/p/generic/a/b/c/d"
        ));
        // 通配至少要 1 段
        assert!(!path_matches_template(
            "/ext/p/generic/{config_path:path}",
            "/ext/p/generic"
        ));
    }

    #[test]
    fn test_find_http_route_template_matching() {
        let registry = CapabilityRegistryImpl::new();
        // 注册一个静态 + 一个单段 param + 一个多段通配
        let mk = |path: &str, method: &str| HttpEndpoint {
            route_id: format!("r_{path}"),
            method: method.to_string(),
            path: path.to_string(),
            auth: "none".to_string(),
            handler_capability: "http.handle".to_string(),
            timeout_ms: None,
            max_concurrency: None,
            description: None,
        };
        registry.register_http_route("p", mk("/ext/p/llm", "GET")).unwrap();
        registry
            .register_http_route("p", mk("/ext/p/models/{model_id}", "PUT"))
            .unwrap();
        registry
            .register_http_route("p", mk("/ext/p/generic/{config_path:path}", "GET"))
            .unwrap();

        // 精确匹配（快路径）
        assert!(registry.find_http_route("/ext/p/llm", "GET").is_some());
        // 单段 param 匹配
        let r = registry.find_http_route("/ext/p/models/gpt-4", "PUT").unwrap();
        assert_eq!(r.endpoint.path, "/ext/p/models/{model_id}");
        // method 不符不匹配
        assert!(registry.find_http_route("/ext/p/models/gpt-4", "GET").is_none());
        // 多段通配匹配
        let r2 = registry.find_http_route("/ext/p/generic/a/b/c", "GET").unwrap();
        assert_eq!(r2.endpoint.path, "/ext/p/generic/{config_path:path}");
        // 完全不匹配
        assert!(registry.find_http_route("/ext/p/nonexistent", "GET").is_none());
    }

    // ── HTTP 路由注册治理（ADR 附录 E.1.3）──

    fn mk_ep(path: &str, method: &str) -> HttpEndpoint {
        HttpEndpoint {
            route_id: format!("r_{path}"),
            method: method.to_string(),
            path: path.to_string(),
            auth: "none".to_string(),
            handler_capability: "http.handle".to_string(),
            timeout_ms: None,
            max_concurrency: None,
            description: None,
        }
    }

    #[test]
    fn test_register_http_route_rejects_path_outside_namespace() {
        // 规则 1：path 必须以 /ext/{plugin_id}/ 为前缀。
        let registry = CapabilityRegistryImpl::new();
        assert!(registry.register_http_route("p1", mk_ep("/other/foo", "GET")).is_err());
        assert!(registry.register_http_route("p1", mk_ep("/ext/p2/foo", "GET")).is_err());
        // 恰好 /ext/{plugin_id}（无尾斜杠）合法
        assert!(registry.register_http_route("p1", mk_ep("/ext/p1", "GET")).is_ok());
    }

    #[test]
    fn test_register_http_route_rejects_reserved_segments() {
        // 规则 2：denylist 段 ws / health。
        let registry = CapabilityRegistryImpl::new();
        assert!(registry.register_http_route("p1", mk_ep("/ext/p1/ws/chat", "GET")).is_err());
        assert!(registry.register_http_route("p1", mk_ep("/ext/p1/health", "GET")).is_err());
    }

    #[test]
    fn test_register_http_route_rejects_api_v1_subpath() {
        // 规则 3：denylist 子路径 api/v1。
        let registry = CapabilityRegistryImpl::new();
        assert!(registry.register_http_route("p1", mk_ep("/ext/p1/api/v1/models", "GET")).is_err());
        assert!(registry.register_http_route("p1", mk_ep("/api/v1/models", "GET")).is_err());
    }

    #[test]
    fn test_register_http_route_conflict_fails_closed() {
        // 同 path+method 冲突 → 第二次注册失败（不静默覆盖）。
        let registry = CapabilityRegistryImpl::new();
        registry.register_http_route("p1", mk_ep("/ext/p1/foo", "GET")).unwrap();
        let err = registry.register_http_route("p1", mk_ep("/ext/p1/foo", "GET"));
        assert!(err.is_err());
        let msg = err.unwrap_err();
        assert!(msg.contains("conflict"), "got: {msg}");
        // 不同 method 不冲突
        assert!(registry.register_http_route("p1", mk_ep("/ext/p1/foo", "POST")).is_ok());
    }

    #[test]
    fn test_find_http_route_method_case_insensitive() {
        let registry = CapabilityRegistryImpl::new();
        registry.register_http_route("p1", mk_ep("/ext/p1/llm", "GET")).unwrap();
        assert!(registry.find_http_route("/ext/p1/llm", "get").is_some());
        assert!(registry.find_http_route("/ext/p1/llm", "GeT").is_some());
    }

    #[test]
    fn test_clear_plugin_removes_http_routes_and_signals() {
        let registry = CapabilityRegistryImpl::new();
        registry.register_http_route("p1", mk_ep("/ext/p1/llm", "GET")).unwrap();
        registry.register_route_signals("p1", vec![RouteType::End, RouteType::Wait]);
        assert!(registry.has_route_signal(&RouteType::End));

        registry.clear_plugin("p1");

        assert!(registry.find_http_route("/ext/p1/llm", "GET").is_none());
        assert!(!registry.has_route_signal(&RouteType::End), "clear 后 signal 应移除");
        assert!(!registry.has_route_signal(&RouteType::Wait));
    }

    #[test]
    fn test_register_route_signals_rebuilds_union_on_reregister() {
        // 重注册（如部分卸载后）旧 signal 不得残留：全局集合 = 当前所有插件并集。
        let registry = CapabilityRegistryImpl::new();
        registry.register_route_signals("plugin_a", vec![RouteType::End, RouteType::Wait]);
        registry.register_route_signals("plugin_b", vec![RouteType::NextLlm]);
        assert!(registry.has_route_signal(&RouteType::End));
        assert!(registry.has_route_signal(&RouteType::Wait));
        assert!(registry.has_route_signal(&RouteType::NextLlm));

        // plugin_a 重注册为空 → End/Wait 必须消失（union 重建），NextLlm 保留
        registry.register_route_signals("plugin_a", vec![]);
        assert!(!registry.has_route_signal(&RouteType::End), "重注册后旧 signal 应移除");
        assert!(!registry.has_route_signal(&RouteType::Wait));
        assert!(registry.has_route_signal(&RouteType::NextLlm));
    }
}

//! 能力注册表 + 服务依赖解析（服务唯一轴）
//!

use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap, HashSet};
use std::sync::Arc;

use agentos_core::traits::{
    CapabilityRegistry, HttpEndpoint, HttpRouteDescriptor, PluginManifest, ToolDescriptor,
};
use agentos_core::types::{RouteType, ToolCategory};
use async_trait::async_trait;
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
/// 管理三类能力：
/// 1. Tools: 工具插件/系统插件提供的工具
/// 2. RouteSignals: 管道插件声明的路由信号
/// 3. HttpRoutes: 插件贡献的 HTTP 端点（ADR §3.3）
///
/// （原 Resources 维度已删除：注册链无消费方。）
pub struct CapabilityRegistryImpl {
    tools: RwLock<HashMap<String, ToolDescriptor>>,
    tools_by_plugin: RwLock<HashMap<String, Vec<String>>>,
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

// ── M1：PluginScope + RegistrationGuard（Cordis Fiber DisposableList 的 RAII 化）──

/// per-plugin 注册账本：插件在内核侧的一切注册都以 guard 形式登记在此，
/// scope 收回（drop 或显式 revoke_all）= 该插件全部注册一次性结构性收回。
///
/// 对应 Cordis `Fiber._disposables`（DisposableList）：每个注册自动挂进当前插件
/// 副作用清单，卸载逆序回收。区别是 Rust 用 Drop 语义做类型系统保证——
/// "禁用即摘除"不再依赖各注册点各自记得调 unregister。
pub struct PluginScope {
    plugin_id: String,
    guards: parking_lot::Mutex<Vec<agentos_core::traits::RegistrationGuard>>,
}

impl PluginScope {
    pub fn new(plugin_id: impl Into<String>) -> Self {
        Self {
            plugin_id: plugin_id.into(),
            guards: parking_lot::Mutex::new(Vec::new()),
        }
    }

    pub fn plugin_id(&self) -> &str {
        &self.plugin_id
    }

    /// 登记一条注册的撤销 guard。guard 被 track 后所有权归 scope，
    /// 单条提前注销用 [`Self::revoke_all`] 之外的按需 disarm 不支持
    /// （单条撤销语义由裸 guard 使用方持有；scope 只做整插件收回）。
    pub fn track(&self, guard: agentos_core::traits::RegistrationGuard) {
        self.guards.lock().push(guard);
    }

    /// 显式收回全部登记（与 drop 等价，供 disable 路径显式调用）。
    /// 逆序回收（与 Cordis LIFO 语义一致）：后注册的先撤。
    pub fn revoke_all(&self) {
        let mut guards = self.guards.lock();
        while let Some(g) = guards.pop() {
            drop(g);
        }
    }

    /// 已登记的注册条数（测试/诊断用）。
    pub fn len(&self) -> usize {
        self.guards.lock().len()
    }

    pub fn is_empty(&self) -> bool {
        self.guards.lock().is_empty()
    }
}

impl Drop for PluginScope {
    fn drop(&mut self) {
        // LIFO 逆序收回（与 revoke_all 一致；guards 字段取空后逐条 drop 触发撤销）。
        let mut guards = std::mem::take(&mut *self.guards.lock());
        while let Some(g) = guards.pop() {
            drop(g);
        }
    }
}

/// 全局插件 scope 表（plugin_id → PluginScope）。
///
/// disable/unload 路径调 [`Self::revoke`]：移除并收回该插件全部注册。
/// 注册路径经 guarded 注册方法自动登记（见 [`CapabilityRegistryImpl`]）。
#[derive(Default)]
pub struct PluginScopeRegistry {
    scopes: RwLock<HashMap<String, Arc<PluginScope>>>,
}

impl PluginScopeRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// 取（或建）某插件的 scope。幂等。
    pub fn scope_of(&self, plugin_id: &str) -> Arc<PluginScope> {
        if let Some(s) = self.scopes.read().get(plugin_id) {
            return Arc::clone(s);
        }
        let mut w = self.scopes.write();
        Arc::clone(
            w.entry(plugin_id.to_string())
                .or_insert_with(|| Arc::new(PluginScope::new(plugin_id))),
        )
    }

    /// 收回某插件的全部注册并移除 scope。不存在则 no-op（幂等）。
    pub fn revoke(&self, plugin_id: &str) {
        if let Some(scope) = self.scopes.write().remove(plugin_id) {
            scope.revoke_all();
        }
    }

    /// 当前登记的 scope 数（测试/诊断用）。
    pub fn len(&self) -> usize {
        self.scopes.read().len()
    }

    pub fn is_empty(&self) -> bool {
        self.scopes.read().is_empty()
    }
}

impl CapabilityRegistryImpl {
    // ── 单条移除（与 unregister_tools / clear_plugin 同族：map + by_plugin 索引）──

    /// 移除单个工具注册（guard revoke 用；与 unregister_tools 同族逻辑，按名单条移除）。
    fn remove_tool_entry(&self, plugin_id: &str, tool_name: &str) {
        self.tools.write().remove(tool_name);
        if let Some(names) = self.tools_by_plugin.write().get_mut(plugin_id) {
            names.retain(|n| n != tool_name);
        }
    }

    /// 移除该插件的路由信号声明。
    fn remove_route_signals_entry(&self, plugin_id: &str) {
        self.route_signals_by_plugin.write().remove(plugin_id);
    }

    /// 移除单条 HTTP 路由注册。
    fn remove_http_route_entry(&self, plugin_id: &str, path: &str, method: &str) {
        self.http_routes.write().remove(&RouteKey {
            path: path.to_string(),
            method: method.to_string(),
        });
        if let Some(keys) = self.http_routes_by_plugin.write().get_mut(plugin_id) {
            keys.retain(|k| k.path != path || k.method != method);
        }
    }

    // ── guarded 注册（M1）：注册 + 返回撤销 guard；语义与对应 register_* 完全一致 ──

    /// 注册工具并返回撤销 guard（revoke = 精确移除该工具注册）。
    pub fn register_tool_guarded(
        self: &Arc<Self>,
        plugin_id: &str,
        tool: ToolDescriptor,
    ) -> agentos_core::traits::RegistrationGuard {
        let name = tool.name.clone();
        self.register_tool(plugin_id, tool);
        let weak = Arc::downgrade(self);
        let pid = plugin_id.to_string();
        agentos_core::traits::RegistrationGuard::new(move || {
            if let Some(reg) = weak.upgrade() {
                reg.remove_tool_entry(&pid, &name);
            }
        })
    }

    /// 注册路由信号并返回撤销 guard（该维度按插件整体声明，revoke = 移除整个声明）。
    pub fn register_route_signals_guarded(
        self: &Arc<Self>,
        plugin_id: &str,
        signals: Vec<RouteType>,
    ) -> agentos_core::traits::RegistrationGuard {
        self.register_route_signals(plugin_id, signals);
        let weak = Arc::downgrade(self);
        let pid = plugin_id.to_string();
        agentos_core::traits::RegistrationGuard::new(move || {
            if let Some(reg) = weak.upgrade() {
                reg.remove_route_signals_entry(&pid);
            }
        })
    }

    /// 注册 HTTP 路由并返回 (描述符, 撤销 guard)。注册失败（冲突/越界）原样返回 Err。
    pub fn register_http_route_guarded(
        self: &Arc<Self>,
        plugin_id: &str,
        endpoint: HttpEndpoint,
    ) -> Result<(HttpRouteDescriptor, agentos_core::traits::RegistrationGuard), String> {
        let descriptor = self.register_http_route(plugin_id, endpoint)?;
        let (path, method) = (
            descriptor.endpoint.path.clone(),
            descriptor.endpoint.method.clone(),
        );
        let weak = Arc::downgrade(self);
        let pid = plugin_id.to_string();
        let guard = agentos_core::traits::RegistrationGuard::new(move || {
            if let Some(reg) = weak.upgrade() {
                reg.remove_http_route_entry(&pid, &path, &method);
            }
        });
        Ok((descriptor, guard))
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

    fn register_route_signals(&self, plugin_id: &str, signals: Vec<RouteType>) {
        self.route_signals_by_plugin
            .write()
            .insert(plugin_id.to_string(), signals);
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
            .find(|d| {
                d.endpoint.method.eq_ignore_ascii_case(method)
                    && path_matches_template(&d.endpoint.path, path)
            })
            .cloned()
    }

    fn clear_plugin(&self, plugin_id: &str) {
        self.unregister_tools(plugin_id);
        self.route_signals_by_plugin.write().remove(plugin_id);
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
        if t.is_empty() {
            "/".to_string()
        } else {
            t.to_string()
        }
    };
    let template = norm(template);
    let incoming = norm(incoming);
    if template == incoming {
        return true;
    }
    let t_segs: Vec<&str> = template.split('/').collect();
    let i_segs: Vec<&str> = incoming.split('/').collect();
    // 段数必须相等（多段通配 {x:path} 例外，见下）。
    let has_catchall = t_segs
        .iter()
        .any(|s| s.starts_with('{') && s.ends_with('}') && s.contains(":path"));
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

/// 服务依赖错误（2026-08-18 契约定型：插件↔插件唯一耦合轴）。
///
/// 消费者不点名插件 id，只声明所需能力角色/端点；映射由服务面注册表完成。
#[derive(Debug, Clone, thiserror::Error)]
pub enum ServiceDepError {
    /// 所需能力角色/端点无人提供。
    #[error(
        "service dependency unsatisfied: consumer='{consumer}' required='{service}' — {detail}"
    )]
    Unsatisfied {
        consumer: String,
        service: String,
        detail: String,
    },
    /// 服务依赖环（A 需 B 的服务、B 需 A 的服务）。
    #[error("service dependency cycle: {cycle:?}")]
    Cycle { cycle: Vec<String> },
}

/// 服务面：从 manifests 汇总"谁提供了哪些服务"——能力角色（namespace）与服务端点
/// （ns.method）→ 提供者映射。
///
/// 数据来源三路：`capabilities.services[]`（服务方法声明，D.6 槽位拆分，名字即
/// `ns.method`）、`provides.capabilities[]`（反向调用能力信封命名空间）、内核内置
/// 能力面（[`KERNEL_PROVIDED_SERVICES`]，CapabilityRouter 常驻）。`ns` 角色 = 该
/// namespace 下已有方法注册；`ns.method` 端点 = 具体方法已注册。`has_role`/
/// `has_method` 供 [`first_error_for`] 判定 `requires_services` 是否满足，不点名插件。
#[derive(Debug, Clone, Default)]
pub struct ServiceSurface {
    /// namespace → 已注册方法集合（角色即在表 = has_role 通过）。
    methods_by_ns: HashMap<String, HashSet<String>>,
    /// 端点（ns.method，无方法名的纯角色级记 ns）→ 提供者插件 id 列表（拓扑边/诊断用）。
    providers: HashMap<String, Vec<String>>,
}

/// 内核内置提供的能力角色（`api/src/capability_router.rs` 常驻分发，插件不声明也有）——
/// 服务面由「插件声明面 + 内核内置面」并集构成；否则 approval 等声明
/// `requires_services: ["pipeline-executor", "event-bus"]`（服务由内核注入，非插件
/// provides）会被误判「无人提供」→ 拒注册。端点级按 router 实际分支列方法；
/// `config-reader`/`logger` 走兜底分发，按规范方法名登记（角色可满足，端点精确性
/// 以 router 实测为准；若两端漂移由依赖不改契约者负责收敛）。
const KERNEL_PROVIDED_SERVICES: &[(&str, &[&str])] = &[
    (
        "pipeline-executor",
        &[
            "suspend",
            "resume",
            "suspend_pipeline",
            "resume_pipeline",
            "get_run_status",
            "delete_pipeline",
        ],
    ),
    ("event-bus", &["emit"]),
    ("tool-executor", &["invoke"]),
    ("pipeline-state", &["list", "update"]),
    // runs 快照列表（调试中心会话/执行记录数据源，与 GET /api/v1/pipelines/runs 同查询）
    ("pipeline-runs", &["list"]),
    ("config-reader", &["read"]),
    ("logger", &["log"]),
    (
        "execution-records",
        &["append", "count", "delete_by_session", "list"],
    ),
    ("frontend", &["emit"]),
    ("memory", &["create", "delete", "get", "list", "search"]),
    ("messages", &["list"]),
    ("metrics", &["record"]),
    ("pipeline-summaries", &["get", "list", "save", "update"]),
    ("registry", &["register_tool"]),
    ("tenant-context", &["get"]),
    ("traces", &["list"]),
];

impl ServiceSurface {
    /// 从 "将注册" 的 manifest 集合构建服务面（boot 全量 / 热发现 enabled 子集）。
    pub fn from_manifests(manifests: &[PluginManifest]) -> Self {
        let mut surface = ServiceSurface::default();
        // 内核内置能力面（CapabilityRouter 常驻，插件的 requires_services 可能依赖它）。
        for &(ns, methods) in KERNEL_PROVIDED_SERVICES {
            for method in methods {
                surface.insert_method(ns, method);
            }
        }
        for m in manifests {
            for svc in &m.capabilities.services {
                register_service(&mut surface, m, &svc.name);
            }
            if let Some(provides) = &m.provides {
                for cap in &provides.capabilities {
                    // 契约键 = namespace（D1：服务→插件的映射按
                    // provides.capabilities[].namespace 做）；`tool_prefix` 是 wire 形态
                    // （McpBridge 路由用），不是契约命名空间——不得当契约键用。
                    for method in &cap.methods {
                        register_method(&mut surface, m, &cap.namespace, method);
                    }
                }
            }
        }
        surface
    }

    /// 仅登记方法集（内核内置面用，无提供者插件、不产生拓扑边）。
    fn insert_method(&mut self, ns: &str, method: &str) {
        self.methods_by_ns
            .entry(ns.to_string())
            .or_default()
            .insert(method.to_string());
    }

    /// ns 角色是否已注册（该 namespace 下至少一个方法被提供）。
    pub fn has_role(&self, ns: &str) -> bool {
        self.methods_by_ns
            .get(ns)
            .is_some_and(|methods| !methods.is_empty())
    }

    /// ns.method 端点是否已注册。
    pub fn has_method(&self, ns: &str, method: &str) -> bool {
        self.methods_by_ns
            .get(ns)
            .is_some_and(|methods| methods.contains(method))
    }

    /// 单插件 `requires_services` 首错（fail-closed）：条目无人提供即返回错误。
    pub fn first_error_for(&self, m: &PluginManifest) -> Option<ServiceDepError> {
        for item in &m.requires_services {
            match parse_item(item) {
                (ns, Some(method)) => {
                    if !self.has_method(ns, method) {
                        return Some(ServiceDepError::Unsatisfied {
                            consumer: m.id.clone(),
                            service: item.clone(),
                            detail: format!(
                                "service endpoint '{ns}.{method}' is not registered by any plugin"
                            ),
                        });
                    }
                }
                (ns, None) => {
                    if !self.has_role(ns) {
                        return Some(ServiceDepError::Unsatisfied {
                            consumer: m.id.clone(),
                            service: item.clone(),
                            detail: format!("capability role '{ns}' has no registered method"),
                        });
                    }
                }
            }
        }
        None
    }

    /// `requires_services` 条目 → 提供者插件 id 列表（服务面拓扑边用）。
    pub fn provider_ids(&self, item: &str) -> Vec<String> {
        let (ns, method) = parse_item(item);
        match method {
            Some(m) => self
                .providers
                .get(&format!("{ns}.{m}"))
                .cloned()
                .unwrap_or_default(),
            None => {
                // 角色级：收集该 ns 下所有端点/角色的提供者（去重）。
                let mut out = Vec::new();
                let prefix = format!("{ns}.");
                for (endpoint, provs) in &self.providers {
                    if endpoint == ns || endpoint.starts_with(&prefix) {
                        for p in provs {
                            if !out.contains(p) {
                                out.push(p.clone());
                            }
                        }
                    }
                }
                out
            }
        }
    }
}

/// `ns.method` → (`ns`, `Some(method)`)；`ns` → (`ns`, `None`)。
fn parse_item(item: &str) -> (&str, Option<&str>) {
    match item.split_once('.') {
        Some((ns, method)) if !ns.is_empty() && !method.is_empty() => (ns, Some(method)),
        _ => (item, None),
    }
}

/// 注册一条 `capabilities.services[].name`（形态 `ns.method`；无 `.` 按角色登记）。
fn register_service(surface: &mut ServiceSurface, m: &PluginManifest, name: &str) {
    let (ns, method) = parse_item(name);
    if let Some(method) = method {
        register_method(surface, m, ns, method);
    } else {
        surface.methods_by_ns.entry(ns.to_string()).or_default();
        surface
            .providers
            .entry(ns.to_string())
            .or_default()
            .push(m.id.clone());
    }
}

/// 注册单方法到服务面（提供者插件伴随登记，供拓扑边/诊断用）。
fn register_method(surface: &mut ServiceSurface, m: &PluginManifest, ns: &str, method: &str) {
    surface.insert_method(ns, method);
    surface
        .providers
        .entry(format!("{ns}.{method}"))
        .or_default()
        .push(m.id.clone());
}

/// 注册闸：解析全量 manifests 的 `requires_services` 引用完整性（fail-closed，服务唯一轴）。
///
/// 任意条目无人提供 → `Err`，启动期拒绝；服务→插件映射由服务面完成，消费者不点名
/// 插件 id。现状 0.2 存量插件全部 `requires_services: []`，本校验不破坏既有启动；
/// 它把"引用了不存在的插件/服务"从运行期谜题提前到启动期暴露。
pub fn resolve_requires_services(manifests: &[PluginManifest]) -> Result<(), ServiceDepError> {
    let surface = ServiceSurface::from_manifests(manifests);
    for m in manifests {
        if let Some(err) = surface.first_error_for(m) {
            return Err(err);
        }
    }
    Ok(())
}

/// 按 `requires_services`（服务边）对插件列表做静态拓扑排序（M2-static）。
///
/// 把 discover 的返回序（HashMap 任意序）变为**显式可证明**的依赖序：依赖者后加载
/// （排序后靠后）；tie-break 字典序（Kahn 按 manifest id 排序）。服务依赖环返回
/// [`ServiceDepError::Cycle`]（含环节点）——启动期 fail-fast，与 pipeline
/// `load_and_compile` 的"坏配置拒绝启动"语义一致。
///
/// `requires_services` 引用不存在提供者的条目不进图（[`resolve_requires_services`]
/// 负责 fail-closed 拒绝；此处只排相对顺序）。无依赖时退化为字典序。
pub fn sort_manifests_topologically(
    manifests: &[PluginManifest],
) -> Result<Vec<PluginManifest>, ServiceDepError> {
    let surface = ServiceSurface::from_manifests(manifests);
    let index: HashMap<&str, usize> = manifests
        .iter()
        .enumerate()
        .map(|(i, m)| (m.id.as_str(), i))
        .collect();
    // 邻接表：provider(pi) 必须先于 consumer(i) 加载。
    let mut graph: HashMap<usize, Vec<usize>> = HashMap::new();
    let mut in_degree = vec![0usize; manifests.len()];
    for (i, m) in manifests.iter().enumerate() {
        for item in &m.requires_services {
            for provider in surface.provider_ids(item) {
                if let Some(&pi) = index.get(provider.as_str()) {
                    graph.entry(pi).or_default().push(i);
                    in_degree[i] += 1;
                }
            }
        }
    }

    // Kahn's algorithm（tie-break：manifest id 字典序）。就绪集用 min-heap 承载
    // `(id, 输入序)`，堆顶恒为当前最小 id——输出序与逐轮字典序挑选一致；均摊
    // O((V+E)·log V)。等 id 时按输入序稳定弹出（id 全局唯一时无差异）。
    let mut ready: BinaryHeap<Reverse<(&str, usize)>> = (0..manifests.len())
        .filter(|&i| in_degree[i] == 0)
        .map(|i| Reverse((manifests[i].id.as_str(), i)))
        .collect();
    let mut remaining = in_degree.clone();
    let mut result: Vec<usize> = Vec::with_capacity(manifests.len());
    while let Some(Reverse((_, node))) = ready.pop() {
        result.push(node);
        if let Some(neighbors) = graph.get(&node) {
            for &nb in neighbors {
                remaining[nb] -= 1;
                if remaining[nb] == 0 {
                    ready.push(Reverse((manifests[nb].id.as_str(), nb)));
                }
            }
        }
    }
    if result.len() != manifests.len() {
        let cycle: Vec<String> = (0..manifests.len())
            .filter(|&i| !result.contains(&i))
            .map(|i| manifests[i].id.clone())
            .collect();
        return Err(ServiceDepError::Cycle { cycle });
    }
    Ok(result.into_iter().map(|i| manifests[i].clone()).collect())
}

/// 合法 JSON Schema 类型（输出/输入契约的第一层 type 收口）。
const JSON_SCHEMA_TYPES: &[&str] = &[
    "object", "array", "string", "number", "integer", "boolean", "null",
];

/// 注册闸：工具 `output_schema` 声明合法性（2026-08-18，与插件其它声明同一套
/// fail-closed 机制——不再是 tool_core 运行时才暴露的特殊小岛）。
///
/// 校验声明的**形状**（首层是否良构 JSON Schema 对象），不校验业务语义：
/// 顶层层 `type` 合法（或 type 数组全合法）；`properties` 为对象；`required`
/// 为字符串数组；`items` 为对象。畸形声明 → `Some(错误说明)`，注册期拒绝该工具。
///
/// 真实语料校准（2026-08-18 扫描）：84 工具仅 5 个声明 output_schema，且全部只
/// 用 type/properties/required——本校验覆盖其全部形态，不产生误报。
pub fn output_schema_error(decl: &serde_json::Value) -> Option<String> {
    let Some(obj) = decl.as_object() else {
        return Some("output_schema 必须是 JSON Schema 对象".to_string());
    };
    if let Some(t) = obj.get("type") {
        let types_ok = if let Some(s) = t.as_str() {
            JSON_SCHEMA_TYPES.contains(&s)
        } else if let Some(arr) = t.as_array() {
            !arr.is_empty()
                && arr.iter().all(|v| {
                    v.as_str()
                        .map(|s| JSON_SCHEMA_TYPES.contains(&s))
                        .unwrap_or(false)
                })
        } else {
            false
        };
        if !types_ok {
            return Some(format!("output_schema.type 非法: {t}"));
        }
    }
    if let Some(props) = obj.get("properties") {
        if !props.is_object() {
            return Some("output_schema.properties 必须是对象".to_string());
        }
    }
    if let Some(req) = obj.get("required") {
        if !req
            .as_array()
            .map(|a| a.iter().all(|v| v.is_string()))
            .unwrap_or(false)
        {
            return Some("output_schema.required 必须是字符串数组".to_string());
        }
    }
    if let Some(items) = obj.get("items") {
        if !items.is_object() {
            return Some("output_schema.items 必须是对象".to_string());
        }
    }
    None
}

/// 注册闸：`provides.capabilities` 公告的每个方法必须有**已声明**的工具
/// （`{tool_prefix}.{method}`；tool_prefix 缺省 = namespace 的 '-'→'_'）。
///
/// 若方法公告了但对应工具未声明 → 消费者可见 `namespace.method` 却调不到 =
/// **"服务声明了但没注册"**（fail-closed）。G2 另外查"已声明 vs 实际暴露"，
/// 这里查"公告 vs 已声明"，两层合起来才保证公告的服务真能调。
///
/// 返回的未回铺方法条目由注册闸硬上报（drift + 工具面拒绝），不进服务面。
pub fn provides_methods_unbacked(m: &agentos_core::traits::PluginManifest) -> Vec<String> {
    let Some(provides) = &m.provides else {
        return Vec::new();
    };
    let declared_tools: std::collections::HashSet<&str> = m
        .capabilities
        .tools
        .iter()
        .map(|t| t.name.as_str())
        .collect();
    let mut out = Vec::new();
    for cap in &provides.capabilities {
        let prefix = cap
            .tool_prefix
            .clone()
            .unwrap_or_else(|| cap.namespace.replace('-', "_"));
        for method in &cap.methods {
            let expected = format!("{prefix}.{method}");
            if !declared_tools.contains(expected.as_str()) {
                out.push(expected);
            }
        }
    }
    out
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
            render: None,
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
    fn test_clear_plugin() {
        let registry = CapabilityRegistryImpl::new();
        registry.register_tool(
            "plugin_a",
            make_tool_descriptor("tool1", "plugin_a", ToolCategory::File),
        );
        registry.register_route_signals("plugin_a", vec![RouteType::End]);

        registry.clear_plugin("plugin_a");

        assert_eq!(registry.list_tools().len(), 0);
    }

    // ── 服务依赖（service-only axis，2026-08-18 契约定型）──────────

    /// 构造带 `capabilities.services`（提供的服务端点名列表）的 manifest。
    fn svc_manifest(id: &str, provides: &[&str]) -> PluginManifest {
        serde_json::from_value(json!({
            "id": id, "name": id, "version": "1.0.0",
            "plugin_type": "tool", "language": "rust",
            "host_type": "sidecar", "entry": "x",
            "capabilities": { "services":
                provides.iter().map(|n| json!({ "name": n })).collect::<Vec<_>>() }
        }))
        .expect("valid manifest")
    }

    #[test]
    fn test_service_dep_provider_loads_first() {
        // app 需要 audit.write；audit 插件提供它 → 提供者先加载、resolve 通过。
        let mut app = svc_manifest("app", &[]);
        app.requires_services = vec!["audit.write".into()];
        let audit = svc_manifest("audit", &["audit.write"]);
        let sorted = sort_manifests_topologically(&[app.clone(), audit.clone()]).unwrap();
        assert!(resolve_requires_services(&[app.clone(), audit.clone()]).is_ok());
        let app_pos = sorted.iter().position(|m| m.id == "app").unwrap();
        let audit_pos = sorted.iter().position(|m| m.id == "audit").unwrap();
        assert!(audit_pos < app_pos, "audit（提供者）必须先于 app 加载");
    }

    #[test]
    fn test_service_dep_role_level() {
        // 角色级条目 `audit`：该 namespace 下任意方法已注册即满足（不必点名方法）。
        let mut app = svc_manifest("app", &[]);
        app.requires_services = vec!["audit".into()];
        let audit = svc_manifest("audit", &["audit.write"]);
        assert!(resolve_requires_services(&[app, audit]).is_ok());
    }

    #[test]
    fn test_service_dep_unsatisfied_fail_closed() {
        // 引用了无人提供的服务端点 → fail-closed 拒绝，错误带上消费者与服务条目。
        let mut app = svc_manifest("app", &[]);
        app.requires_services = vec!["ghost.read".into()];
        let err = resolve_requires_services(&[app]).unwrap_err();
        assert!(
            err.to_string().contains("consumer='app'") && err.to_string().contains("ghost.read"),
            "{err}"
        );
    }

    #[test]
    fn test_service_dep_no_requires_passes_lexicographic() {
        // 存量插件全空 requires_services：resolve 恒过，拓扑退化为 id 字典序。
        let a = svc_manifest("b_plugin", &["b.x"]);
        let b = svc_manifest("a_plugin", &[]);
        assert!(resolve_requires_services(&[a.clone(), b.clone()]).is_ok());
        let sorted = sort_manifests_topologically(&[a, b]).unwrap();
        let ids: Vec<&str> = sorted.iter().map(|m| m.id.as_str()).collect();
        assert_eq!(ids, vec!["a_plugin", "b_plugin"]);
    }

    #[test]
    fn test_service_dep_circular_detected() {
        // a 需 b.x、b 需 a.y → 服务依赖环，启动期 fail-fast。
        let mut a = svc_manifest("a", &["a.y"]);
        a.requires_services = vec!["b.x".into()];
        let mut br = svc_manifest("b", &["b.x"]);
        br.requires_services = vec!["a.y".into()];
        let result = sort_manifests_topologically(&[a, br]);
        assert!(result.is_err());
        match result {
            Err(ServiceDepError::Cycle { cycle }) => {
                assert!(cycle.contains(&"a".to_string()));
                assert!(cycle.contains(&"b".to_string()));
            }
            Err(e) => panic!("期望 Cycle，得 {e:?}"),
            Ok(_) => panic!("期望环检测失败"),
        }
    }

    #[test]
    fn test_topo_sort_output_order_chain_with_free_node() {
        // 链 + 独立节点，输入乱序插入：输出必须是"字典序 tie-break 的依赖序"
        // 全序列（不只是相对序）。就绪集每轮取最小 id：a_base 先于独立节点
        // d_free 弹出后解锁 b_mid，故 d_free 排到整条链之后。
        let mut c_final = svc_manifest("c_final", &[]);
        c_final.requires_services = vec!["b.mid".into()];
        let mut b_mid = svc_manifest("b_mid", &["b.mid"]);
        b_mid.requires_services = vec!["a.base".into()];
        let d_free = svc_manifest("d_free", &[]);
        let a_base = svc_manifest("a_base", &["a.base"]);

        let sorted = sort_manifests_topologically(&[c_final, b_mid, d_free, a_base]).unwrap();
        let ids: Vec<&str> = sorted.iter().map(|m| m.id.as_str()).collect();
        assert_eq!(ids, vec!["a_base", "b_mid", "c_final", "d_free"]);
    }

    #[test]
    fn test_topo_sort_output_order_diamond_tiebreak() {
        // 菱形：单提供者两消费者在同一轮解锁 → 按 id 字典序排；另一独立节点
        // 先于提供者弹出。锁序性质：任一依赖边提供者位置严格先于消费者。
        let w_root = svc_manifest("w_root", &["w.any"]);
        let mut x_c1 = svc_manifest("x_c1", &[]);
        x_c1.requires_services = vec!["w.any".into()];
        let mut y_c2 = svc_manifest("y_c2", &[]);
        y_c2.requires_services = vec!["w.any".into()];
        let v_early = svc_manifest("v_early", &[]);

        let sorted = sort_manifests_topologically(&[y_c2, v_early, x_c1, w_root]).unwrap();
        let ids: Vec<&str> = sorted.iter().map(|m| m.id.as_str()).collect();
        assert_eq!(ids, vec!["v_early", "w_root", "x_c1", "y_c2"]);

        // 性质断言：所有 (提供者→消费者) 边在输出中保持先后
        let pos = |id: &str| ids.iter().position(|&i| i == id).unwrap();
        assert!(pos("v_early") < pos("w_root"));
        assert!(pos("w_root") < pos("x_c1") && pos("w_root") < pos("y_c2"));
    }

    #[test]
    fn test_service_surface_role_vs_method() {
        let surface =
            ServiceSurface::from_manifests(&[svc_manifest("p", &["audit.write", "audit.read"])]);
        assert!(surface.has_role("audit"));
        assert!(surface.has_method("audit", "write"));
        assert!(
            !surface.has_method("audit", "delete"),
            "未声明的方法不算注册"
        );
        assert!(!surface.has_role("ghost"));
    }

    #[test]
    fn test_service_surface_kernel_provided_roles() {
        // 内核内置能力面：空 manifests 也能满足 pipeline-executor/event-bus 角色——
        // approval 等声明 requires_services 依赖内核注入能力时不得误判"无人提供"。
        let surface = ServiceSurface::from_manifests(&[]);
        assert!(surface.has_role("pipeline-executor"));
        assert!(surface.has_role("event-bus"));
        assert!(surface.has_method("pipeline-executor", "suspend"));
        assert!(surface.has_method("event-bus", "emit"));
        assert!(!surface.has_role("ghost-core"));
    }

    #[test]
    fn test_service_surface_provides_uses_contract_namespace() {
        // provides.namespace 带连字符（human-interaction）：契约键 = 命名空间本身，
        // 不是 wire 形态 tool_prefix（interaction）——消费者按 ns 角色解析须命中。
        let m: PluginManifest = serde_json::from_value(json!({
            "id": "human", "name": "human", "version": "1.0.0",
            "plugin_type": "tool", "language": "python", "host_type": "sidecar",
            "entry": "python server.py", "capabilities": {},
            "provides": { "capabilities": [{
                "namespace": "human-interaction",
                "methods": ["create_choice", "wait_for_choice"]
            }]}
        }))
        .unwrap();
        let surface = ServiceSurface::from_manifests(&[m]);
        assert!(surface.has_role("human-interaction"));
        assert!(surface.has_method("human-interaction", "create_choice"));
        assert!(
            !surface.has_role("human_interaction"),
            "wire 前缀不是契约键"
        );
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
        assert!(path_matches_template(
            "/ext/p/models/{model_id}",
            "/ext/p/models/gpt-4"
        ));
        assert!(path_matches_template(
            "/ext/p/models/{model_id}",
            "/ext/p/models/claude-3"
        ));
        // 单段不跨 /
        assert!(!path_matches_template(
            "/ext/p/models/{model_id}",
            "/ext/p/models/a/b"
        ));
        // 段数不符
        assert!(!path_matches_template(
            "/ext/p/models/{model_id}",
            "/ext/p/models"
        ));
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
        registry
            .register_http_route("p", mk("/ext/p/llm", "GET"))
            .unwrap();
        registry
            .register_http_route("p", mk("/ext/p/models/{model_id}", "PUT"))
            .unwrap();
        registry
            .register_http_route("p", mk("/ext/p/generic/{config_path:path}", "GET"))
            .unwrap();

        // 精确匹配（快路径）
        assert!(registry.find_http_route("/ext/p/llm", "GET").is_some());
        // 单段 param 匹配
        let r = registry
            .find_http_route("/ext/p/models/gpt-4", "PUT")
            .unwrap();
        assert_eq!(r.endpoint.path, "/ext/p/models/{model_id}");
        // method 不符不匹配
        assert!(registry
            .find_http_route("/ext/p/models/gpt-4", "GET")
            .is_none());
        // 多段通配匹配
        let r2 = registry
            .find_http_route("/ext/p/generic/a/b/c", "GET")
            .unwrap();
        assert_eq!(r2.endpoint.path, "/ext/p/generic/{config_path:path}");
        // 完全不匹配
        assert!(registry
            .find_http_route("/ext/p/nonexistent", "GET")
            .is_none());
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
        assert!(registry
            .register_http_route("p1", mk_ep("/other/foo", "GET"))
            .is_err());
        assert!(registry
            .register_http_route("p1", mk_ep("/ext/p2/foo", "GET"))
            .is_err());
        // 恰好 /ext/{plugin_id}（无尾斜杠）合法
        assert!(registry
            .register_http_route("p1", mk_ep("/ext/p1", "GET"))
            .is_ok());
    }

    #[test]
    fn test_register_http_route_rejects_reserved_segments() {
        // 规则 2：denylist 段 ws / health。
        let registry = CapabilityRegistryImpl::new();
        assert!(registry
            .register_http_route("p1", mk_ep("/ext/p1/ws/chat", "GET"))
            .is_err());
        assert!(registry
            .register_http_route("p1", mk_ep("/ext/p1/health", "GET"))
            .is_err());
    }

    #[test]
    fn test_register_http_route_rejects_api_v1_subpath() {
        // 规则 3：denylist 子路径 api/v1。
        let registry = CapabilityRegistryImpl::new();
        assert!(registry
            .register_http_route("p1", mk_ep("/ext/p1/api/v1/models", "GET"))
            .is_err());
        assert!(registry
            .register_http_route("p1", mk_ep("/api/v1/models", "GET"))
            .is_err());
    }

    #[test]
    fn test_register_http_route_conflict_fails_closed() {
        // 同 path+method 冲突 → 第二次注册失败（不静默覆盖）。
        let registry = CapabilityRegistryImpl::new();
        registry
            .register_http_route("p1", mk_ep("/ext/p1/foo", "GET"))
            .unwrap();
        let err = registry.register_http_route("p1", mk_ep("/ext/p1/foo", "GET"));
        assert!(err.is_err());
        let msg = err.unwrap_err();
        assert!(msg.contains("conflict"), "got: {msg}");
        // 不同 method 不冲突
        assert!(registry
            .register_http_route("p1", mk_ep("/ext/p1/foo", "POST"))
            .is_ok());
    }

    #[test]
    fn test_find_http_route_method_case_insensitive() {
        let registry = CapabilityRegistryImpl::new();
        registry
            .register_http_route("p1", mk_ep("/ext/p1/llm", "GET"))
            .unwrap();
        assert!(registry.find_http_route("/ext/p1/llm", "get").is_some());
        assert!(registry.find_http_route("/ext/p1/llm", "GeT").is_some());
    }

    #[test]
    fn test_clear_plugin_removes_http_routes() {
        let registry = CapabilityRegistryImpl::new();
        registry
            .register_http_route("p1", mk_ep("/ext/p1/llm", "GET"))
            .unwrap();

        registry.clear_plugin("p1");

        assert!(registry.find_http_route("/ext/p1/llm", "GET").is_none());
    }

    // ── M1：PluginScope + RegistrationGuard（P2 验收：disable 后零残留）──

    fn make_http_endpoint(plugin_id: &str, suffix: &str) -> HttpEndpoint {
        HttpEndpoint {
            route_id: format!("{}-{}", plugin_id, suffix),
            method: "GET".to_string(),
            path: format!("/ext/{}/{}", plugin_id, suffix),
            auth: "none".to_string(),
            handler_capability: "http.handle".to_string(),
            timeout_ms: None,
            max_concurrency: None,
            description: None,
        }
    }

    /// guarded 注册 → scope revoke → 各维度零残留。
    /// （resources 维度已删除；route_signals 查询面已删除——本测试覆盖
    /// tool 与 http_route 两个仍有查询面的维度。）
    #[test]
    fn m1_scope_revoke_leaves_no_residue_in_all_dimensions() {
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let scopes = PluginScopeRegistry::new();
        let pid = "m1_plugin";

        let scope = scopes.scope_of(pid);
        scope.track(
            registry
                .register_tool_guarded(pid, make_tool_descriptor("t1", pid, ToolCategory::System)),
        );
        scope.track(registry.register_route_signals_guarded(pid, vec![RouteType::NextTool]));
        scope.track(
            registry
                .register_http_route_guarded(pid, make_http_endpoint(pid, "cb"))
                .expect("route should register")
                .1,
        );

        // 注册后各维度均可见。
        assert!(registry.get_tool("t1").is_some());
        assert!(registry
            .find_http_route("/ext/m1_plugin/cb", "GET")
            .is_some());

        // disable 语义：scope revoke → 全部收回。
        scopes.revoke(pid);
        assert!(
            registry.get_tool("t1").is_none(),
            "tool residue after revoke"
        );
        assert!(
            registry
                .find_http_route("/ext/m1_plugin/cb", "GET")
                .is_none(),
            "http route residue after revoke"
        );
        assert!(
            scopes.is_empty(),
            "scope table should drop the revoked entry"
        );
    }

    /// 单 guard drop 只移除单条注册；其他插件的注册不受影响。
    #[test]
    fn m1_single_guard_drop_removes_only_that_registration() {
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let guard = registry.register_tool_guarded(
            "p1",
            make_tool_descriptor("only_this", "p1", ToolCategory::System),
        );
        registry.register_tool(
            "p2",
            make_tool_descriptor("keep", "p2", ToolCategory::System),
        );

        drop(guard);
        assert!(registry.get_tool("only_this").is_none());
        assert!(
            registry.get_tool("keep").is_some(),
            "other plugin's tool must survive"
        );
    }

    /// disarm 放弃撤销：注册保留。
    #[test]
    fn m1_disarm_keeps_registration() {
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let guard = registry.register_tool_guarded(
            "p1",
            make_tool_descriptor("stay", "p1", ToolCategory::System),
        );
        guard.disarm();
        assert!(registry.get_tool("stay").is_some());
    }

    /// PluginScope drop（不显式 revoke）同样收回——RAII 保证。
    #[test]
    fn m1_scope_drop_revokes_like_revoke() {
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let scope = PluginScope::new("dropped_plugin");
        scope.track(registry.register_tool_guarded(
            "dropped_plugin",
            make_tool_descriptor("t", "dropped_plugin", ToolCategory::System),
        ));
        drop(scope);
        assert!(registry.get_tool("t").is_none());
    }

    /// registry 先行 drop 时 guard revoke 静默 no-op（弱引用语义，不 panic）。
    #[test]
    fn m1_guard_revoke_after_registry_drop_is_noop() {
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let scopes = PluginScopeRegistry::new();
        scopes.scope_of("p").track(
            registry
                .register_tool_guarded("p", make_tool_descriptor("t", "p", ToolCategory::System)),
        );
        drop(registry);
        scopes.revoke("p"); // 不应 panic
    }

    // ── M2-static：sort_manifests_topologically（服务边） + 注册闸 resolve ──

    fn make_manifest_for_sort(id: &str) -> agentos_core::traits::PluginManifest {
        use agentos_core::traits::{HostType, PluginManifest, PluginType};
        PluginManifest {
            id: id.to_string(),
            name: format!("P {}", id),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::System,
            pipeline_role: None,
            language: "python".to_string(),
            host_type: HostType::Sidecar,
            host_group: None,
            entry: "server.py".to_string(),
            capabilities: Default::default(),
            requires_services: vec![],
            permissions: Default::default(),
            priority: 100,
            granted_capabilities: vec![],
            mcp: None,
            lifecycle: None,
            native: None,
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
            export_fields: vec![],
        }
    }

    /// 构造排序用 manifest：`requires` = 需要的能力角色/端点（ns 或 ns.method），
    /// `provides` = 自身提供的服务端点（capabilities.services[].name，形态 ns.method）。
    fn svc_sort_manifest(
        id: &str,
        requires: &[&str],
        provides: &[&str],
    ) -> agentos_core::traits::PluginManifest {
        let mut m = make_manifest_for_sort(id);
        m.requires_services = requires.iter().map(|s| s.to_string()).collect();
        m.capabilities.services = provides
            .iter()
            .map(|n| serde_json::from_value(json!({ "name": n })).unwrap())
            .collect();
        m
    }

    #[test]
    fn m2_sort_puts_service_providers_before_consumers() {
        // 乱序输入：消费者在前 → 应被排为拓扑序（提供者 → 消费者）。
        let manifests = vec![
            svc_sort_manifest("z_depender", &["z_svc.read"], &[]),
            svc_sort_manifest("z_base", &[], &["z_svc.read"]),
        ];
        let sorted = sort_manifests_topologically(&manifests).unwrap();
        let ids: Vec<&str> = sorted.iter().map(|m| m.id.as_str()).collect();
        assert_eq!(ids, vec!["z_base", "z_depender"]);
    }

    #[test]
    fn m2_sort_transitive_service_chain() {
        let manifests = vec![
            svc_sort_manifest("c", &["b.x"], &[]),
            svc_sort_manifest("a", &[], &["a.x"]),
            svc_sort_manifest("b", &["a.x"], &["b.x"]),
        ];
        let sorted = sort_manifests_topologically(&manifests).unwrap();
        let ids: Vec<&str> = sorted.iter().map(|m| m.id.as_str()).collect();
        assert_eq!(ids, vec!["a", "b", "c"]);
    }

    #[test]
    fn m2_sort_detects_service_cycle() {
        let manifests = vec![
            svc_sort_manifest("x", &["y.m"], &["x.m"]),
            svc_sort_manifest("y", &["x.m"], &["y.m"]),
        ];
        let err = sort_manifests_topologically(&manifests).unwrap_err();
        assert!(matches!(err, ServiceDepError::Cycle { .. }));
    }

    // ── resolve_requires_services（注册闸：服务引用完整性，fail-closed） ──────────

    #[test]
    fn validate_reqs_satisfied_is_ok() {
        let manifests = vec![
            svc_sort_manifest("base", &[], &["base.x"]),
            svc_sort_manifest("app", &["base.x"], &[]),
        ];
        assert!(resolve_requires_services(&manifests).is_ok());
    }

    #[test]
    fn validate_reqs_unsatisfied_rejected() {
        let manifests = vec![svc_sort_manifest("app", &["ghost.read"], &[])];
        let err = resolve_requires_services(&manifests).unwrap_err();
        match err {
            ServiceDepError::Unsatisfied {
                consumer, service, ..
            } => {
                assert_eq!(consumer, "app");
                assert_eq!(service, "ghost.read");
            }
            other => panic!("expected Unsatisfied, got {other:?}"),
        }
    }

    // ── output_schema 声明合法性（注册闸，与插件其它声明同一套 fail-closed） ──

    #[test]
    fn output_schema_wellformed_declarations_pass() {
        // 真实语料校准：5/84 工具的 output_schema 全部只用 type/properties/required
        assert!(output_schema_error(&json!({
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a"],
        }))
        .is_none());
        assert!(output_schema_error(&json!({"type": ["object", "string"]})).is_none());
        assert!(output_schema_error(&json!({
            "type": "array", "items": {"type": "object", "properties": {}}
        }))
        .is_none());
        assert!(
            output_schema_error(&json!({})).is_none(),
            "空对象可解析（无 type 也允许）"
        );
    }

    #[test]
    fn output_schema_malformed_is_rejected() {
        assert!(
            output_schema_error(&json!("just-a-string")).is_some(),
            "顶层非对象必须报"
        );
        assert!(
            output_schema_error(&json!({"type": "hat"})).is_some(),
            "非法 type 必须报"
        );
        assert!(
            output_schema_error(&json!({"type": ["object", "hat"]})).is_some(),
            "type 数组含非法项"
        );
        assert!(
            output_schema_error(&json!({"type": 42})).is_some(),
            "type 非字符串/数组"
        );
        assert!(
            output_schema_error(&json!({"properties": "oops"})).is_some(),
            "properties 非对象"
        );
        assert!(
            output_schema_error(&json!({"required": ["a", 1]})).is_some(),
            "required 非纯字符串数组"
        );
        assert!(
            output_schema_error(&json!({"items": "oops"})).is_some(),
            "items 非对象"
        );
    }

    // ── provides 服务注册检查（公告的方法必须有已声明工具） ──────────────

    #[test]
    fn provides_methods_unbacked_detects_dead_advertisement() {
        // human_interaction_tool 历史真例：公告 create_conversation 但无对应工具
        let v = json!({
            "id": "svc", "name": "S", "version": "1.0.0",
            "plugin_type": "system", "language": "python",
            "host_type": "sidecar", "entry": "x",
            "capabilities": { "tools": [ {"name": "interaction.create_choice", "description": "d"} ] },
            "provides": { "capabilities": [ {
                "namespace": "human-interaction", "tool_prefix": "interaction",
                "methods": ["create_choice", "create_conversation"]
            } ] }
        });
        let m: agentos_core::traits::PluginManifest = serde_json::from_value(v).unwrap();
        assert_eq!(
            provides_methods_unbacked(&m),
            vec!["interaction.create_conversation".to_string()],
            "公告但无已声明工具 = 服务未注册，必须抓出"
        );
    }

    #[test]
    fn provides_methods_all_backed_is_clean() {
        let v = json!({
            "id": "svc", "name": "S", "version": "1.0.0",
            "plugin_type": "system", "language": "python",
            "host_type": "sidecar", "entry": "x",
            "capabilities": { "tools": [ {"name": "ns.foo", "description": "d"} ] },
            "provides": { "capabilities": [ { "namespace": "ns", "methods": ["foo"] } ] }
        });
        let m: agentos_core::traits::PluginManifest = serde_json::from_value(v).unwrap();
        assert!(provides_methods_unbacked(&m).is_empty());
    }
}

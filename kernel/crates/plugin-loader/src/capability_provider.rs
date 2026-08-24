//! 把 plugin manifest 的 provides.capabilities 注册成 CapabilityHandler。
//!
//! 当一个插件声明 `provides.capabilities`（如 `human_interaction_service` 声明
//! `human-interaction`），本模块在 loader 扫描后把这些声明注册进
//! [`CapabilityHandlerRegistry`]，使该 namespace 自动出现在：
//! - 反向调用白名单（reader loop 的 `parse_capability_method_with`）；
//! - initialize 握手声明（sidecar SDK 据此创建 CapabilityHandle）；
//! - 路由表（`CapabilityHandlerRegistry::route`）。
//!
//! ## 桥接层次（M4 分阶段）
//!
//! - **Cycle 3.2（本模块）**：注册链路打通。handler 持有 manifest 元数据，
//!   能正确响应 namespace/methods 查询；实际调用时返回 `NotBridged` 错误
//!   （明确的"未桥接到真实 service"提示，而非静默成功）。
//! - **Cycle 3.3（后续）**：为 `InProcess` host 注入真实 service 桥接
//!   （Python 对象引用或 IPC handle），handler 转发调用到真实实现。

use std::sync::Arc;

use agentos_core::traits::{PluginInvoker, PluginManifest, ProvidedCapability};
use agentos_mcp::{CapabilityHandler, CapabilityHandlerRegistry, McpError};
use async_trait::async_trait;
use serde_json::Value;

/// 能力桥接——把 Rust 内核的 capability 调用转发到真正的实现方（Python service /
/// 另一个 sidecar / 任何外部进程）。
///
/// 这个 trait 是 M4 Cycle 3.3 的抽象接缝：handler 不绑死任何具体传输方式
/// （HTTP / MCP / 共享内存），由调用方注入 bridge 实现。
///
/// 典型实现：
/// - `HttpBridge`：通过 HTTP 调 Python 主进程的 REST API（待 Python 侧补
///   `POST /interaction/create` 等端点后实现）；
/// - `McpBridge`：转发到目标插件的 MCP 连接（sidecar host 场景）；
/// - 测试 stub：直接返回固定值。
#[async_trait]
pub trait CapabilityBridge: Send + Sync {
    /// 转发一次 capability 调用。
    ///
    /// Args:
    /// - `plugin_id`: 提供capability 的插件 manifest id；
    /// - `namespace`: capability namespace（如 `human-interaction`）；
    /// - `method`: method 名（如 `create_choice`）；
    /// - `params`: JSON-RPC params。
    async fn forward(
        &self,
        plugin_id: &str,
        namespace: &str,
        method: &str,
        params: Value,
    ) -> Result<Value, McpError>;
}

/// 一个由 plugin manifest provides 声明注册的 capability handler。
///
/// 持有 manifest 元数据 + 可选的桥接对象。无 bridge 时返回 NotBridged 错误
/// （明确的"未桥接"提示）；有 bridge 时转发调用并返回结果。
pub struct ProvidedCapabilityHandler {
    namespace: String,
    methods: Vec<String>,
    plugin_id: String,
    bridge: Option<Arc<dyn CapabilityBridge>>,
}

impl ProvidedCapabilityHandler {
    pub fn new(plugin_id: String, capability: &ProvidedCapability) -> Self {
        Self {
            namespace: capability.namespace.clone(),
            methods: capability.methods.clone(),
            plugin_id,
            bridge: None,
        }
    }

    /// 注入桥接对象，让 handler 能转发调用到真实实现方。
    pub fn with_bridge(mut self, bridge: Arc<dyn CapabilityBridge>) -> Self {
        self.bridge = Some(bridge);
        self
    }
}

#[async_trait]
impl CapabilityHandler for ProvidedCapabilityHandler {
    fn namespace(&self) -> &str {
        &self.namespace
    }

    async fn handle(&self, method: &str, params: Value) -> Result<Value, McpError> {
        // method 白名单校验（manifest 声明了哪些 method 就只接受哪些）
        if !self.methods.iter().any(|m| m == method) {
            return Err(McpError::Protocol {
                message: format!(
                    "{}.{} not declared in provides.capabilities of plugin {} (declared: {:?})",
                    self.namespace, method, self.plugin_id, self.methods
                ),
            });
        }
        // 有 bridge：转发调用
        if let Some(bridge) = &self.bridge {
            return bridge
                .forward(&self.plugin_id, &self.namespace, method, params)
                .await;
        }
        // 无 bridge：明确提示未桥接（测试/过渡期/未配置时可见）
        Err(McpError::Protocol {
            message: format!(
                "{}.{} accepted (declared by plugin {}) but no bridge connected",
                self.namespace, method, self.plugin_id
            ),
        })
    }
}

/// 遍历 manifest 列表，把每个 `provides.capabilities` 注册进 registry。
///
/// 在 loader 完成 discover 后调用。返回实际注册的 namespace 数量。
///
/// `bridge` 为 Some 时，所有注册的 handler 共享这个桥接对象（转发到真实实现方）；
/// 为 None 时 handler 返回 NotBridged（测试/过渡期）。
///
/// 同一 namespace 被多个插件声明时，后注册者覆盖前者（`CapabilityHandlerRegistry`
/// 的热替换语义）。调用方如需确定性优先级，应按 plugin priority 排序后再传入。
pub fn register_provided_capabilities(
    registry: &CapabilityHandlerRegistry,
    manifests: &[PluginManifest],
    bridge: Option<Arc<dyn CapabilityBridge>>,
) -> usize {
    let mut count = 0;
    for manifest in manifests {
        let Some(provides) = &manifest.provides else {
            continue;
        };
        for cap in &provides.capabilities {
            let mut handler = ProvidedCapabilityHandler::new(manifest.id.clone(), cap);
            if let Some(b) = &bridge {
                handler = handler.with_bridge(Arc::clone(b));
            }
            registry.register(Arc::new(handler));
            tracing::info!(
                plugin_id = %manifest.id,
                namespace = %cap.namespace,
                methods = ?cap.methods,
                bridged = bridge.is_some(),
                "registered provided capability",
            );
            count += 1;
        }
    }
    count
}

// ── McpBridge：CapabilityBridge 的 sidecar 实现（M5-2）──

/// namespace → (plugin_id, tool_prefix) 路由表条目。
#[derive(Clone)]
pub struct CapabilityRoute {
    /// 提供该 capability 的插件 manifest id。
    pub plugin_id: String,
    /// 工具名前缀（如 namespace=`human-interaction` 对应 sidecar 工具前缀 `interaction`）。
    /// bridge 把 `<namespace>.<method>` 映射成 `<tool_prefix>.<method>` 调用 sidecar。
    pub tool_prefix: String,
}

/// 通过 `PluginInvoker` 把 capability 调用转发到 sidecar 插件的 bridge 实现。
///
/// 持有一个 namespace→路由 查找表（由 loader 在注册 provides 时填充）和一个
/// `Arc<dyn PluginInvoker>`（内核注入的真实 invoker）。forward 时：
/// 1. 查表拿到 plugin_id + tool_prefix；
/// 2. 拼 tool_name = `{tool_prefix}.{method}`；
/// 3. 调 `invoker.invoke_tool(plugin_id, tool_name, params)`；
/// 4. 把 ToolExecutionResult 转成 JSON 返回。
pub struct McpBridge {
    invoker: Arc<dyn PluginInvoker>,
    routes: parking_lot::RwLock<std::collections::HashMap<String, CapabilityRoute>>,
}

impl McpBridge {
    pub fn new(invoker: Arc<dyn PluginInvoker>) -> Self {
        Self {
            invoker,
            routes: parking_lot::RwLock::new(std::collections::HashMap::new()),
        }
    }

    /// 注册一条 namespace 路由。多个插件声明同一 namespace 时后者覆盖。
    pub fn add_route(&self, namespace: &str, route: CapabilityRoute) {
        self.routes.write().insert(namespace.to_string(), route);
    }

    /// 批量注册——从 manifest 列表的 provides 派生路由（纯声明式，零硬编码）。
    ///
    /// tool_prefix 优先用 manifest `provides.capabilities[].tool_prefix` 显式声明；
    /// 未声明时从 namespace 派生（连字符转下划线）作为合理默认。
    pub fn add_routes_from_manifests(&self, manifests: &[PluginManifest]) {
        for manifest in manifests {
            let Some(provides) = &manifest.provides else {
                continue;
            };
            for cap in &provides.capabilities {
                let prefix = cap
                    .tool_prefix
                    .clone()
                    .unwrap_or_else(|| cap.namespace.replace('-', "_"));
                self.add_route(
                    &cap.namespace,
                    CapabilityRoute {
                        plugin_id: manifest.id.clone(),
                        tool_prefix: prefix,
                    },
                );
            }
        }
    }
}

#[async_trait]
impl CapabilityBridge for McpBridge {
    async fn forward(
        &self,
        _plugin_id: &str,
        namespace: &str,
        method: &str,
        params: Value,
    ) -> Result<Value, McpError> {
        let route =
            self.routes
                .read()
                .get(namespace)
                .cloned()
                .ok_or_else(|| McpError::Protocol {
                    message: format!(
                        "McpBridge has no route for namespace '{namespace}' (known: {:?})",
                        self.routes.read().keys().collect::<Vec<_>>()
                    ),
                })?;

        let tool_name = format!("{}.{}", route.tool_prefix, method);
        let result = self
            .invoker
            .invoke_tool(&route.plugin_id, &tool_name, &params)
            .await
            .map_err(|e| McpError::Protocol {
                message: format!(
                    "invoke_tool({}/{tool_name}) failed: {}",
                    route.plugin_id, e.message
                ),
            })?;

        if result.success {
            Ok(result.data)
        } else {
            Err(McpError::Protocol {
                message: result
                    .error
                    .unwrap_or_else(|| "tool returned failure".to_string()),
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::traits::{
        HookContext, HostType, LifecycleHook, ManifestCapabilities, PluginInvoker, PluginManifest,
        PluginType, ProvidedCapability, ProvidedCapabilityHost, ProvidesCapabilities,
    };
    use agentos_core::types::{PluginContext, PluginError, PluginResult, ToolExecutionResult};
    use agentos_mcp::CapabilityRouter;
    use serde_json::json;

    fn make_manifest(id: &str, provides: Option<ProvidesCapabilities>) -> PluginManifest {
        PluginManifest {
            id: id.to_string(),
            name: id.to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::System,
            pipeline_role: None,
            language: "python".to_string(),
            host_type: HostType::Sidecar,
            entry: "python server.py".to_string(),
            capabilities: ManifestCapabilities::default(),
            requires_services: vec![],
            permissions: Default::default(),
            priority: 50,
            mcp: None,
            lifecycle: None,
            native: None,
            granted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            provides,
            persistent_fields: vec![],
        }
    }

    #[test]
    fn test_register_provided_capabilities_adds_namespaces() {
        let registry = CapabilityHandlerRegistry::new();
        let manifests = vec![make_manifest(
            "human_interaction_service",
            Some(ProvidesCapabilities {
                capabilities: vec![ProvidedCapability {
                    namespace: "human-interaction".to_string(),
                    methods: vec!["create_choice".to_string(), "wait_for_choice".to_string()],
                    host: ProvidedCapabilityHost::InProcess,
                    tool_prefix: None,
                }],
            }),
        )];

        let count = register_provided_capabilities(&registry, &manifests, None);
        assert_eq!(count, 1);
        assert!(registry.has_namespace("human-interaction"));
        assert!(registry
            .namespaces()
            .contains(&"human-interaction".to_string()));
    }

    #[test]
    fn test_register_skips_manifests_without_provides() {
        let registry = CapabilityHandlerRegistry::new();
        let manifests = vec![
            make_manifest("no_provides_plugin", None),
            make_manifest(
                "with_provides",
                Some(ProvidesCapabilities {
                    capabilities: vec![ProvidedCapability {
                        namespace: "my-cap".to_string(),
                        methods: vec!["do".to_string()],
                        host: ProvidedCapabilityHost::InProcess,
                        tool_prefix: None,
                    }],
                }),
            ),
        ];

        let count = register_provided_capabilities(&registry, &manifests, None);
        assert_eq!(count, 1, "只注册有 provides 的插件");
        assert!(!registry.has_namespace("no_provides_plugin"));
    }

    #[tokio::test]
    async fn test_handler_rejects_undeclared_method() {
        // manifest 声明了 create_choice / wait_for_choice，
        // 调用未声明的 do_something 应被拒绝。
        let registry = CapabilityHandlerRegistry::new();
        let manifests = vec![make_manifest(
            "p1",
            Some(ProvidesCapabilities {
                capabilities: vec![ProvidedCapability {
                    namespace: "ns1".to_string(),
                    methods: vec!["create_choice".to_string()],
                    host: ProvidedCapabilityHost::InProcess,
                    tool_prefix: None,
                }],
            }),
        )];
        register_provided_capabilities(&registry, &manifests, None);

        let err = registry.route("ns1", "do_something", json!({})).await;
        assert!(err.is_err());
        let msg = format!("{}", err.unwrap_err());
        assert!(
            msg.contains("not declared"),
            "未声明的 method 应被拒绝: {msg}"
        );
    }

    #[tokio::test]
    async fn test_handler_declared_method_returns_not_bridged() {
        // 无 bridge 时，声明的 method 应被接受但返回"未桥接"错误。
        let registry = CapabilityHandlerRegistry::new();
        let manifests = vec![make_manifest(
            "p1",
            Some(ProvidesCapabilities {
                capabilities: vec![ProvidedCapability {
                    namespace: "ns1".to_string(),
                    methods: vec!["create_choice".to_string()],
                    host: ProvidedCapabilityHost::InProcess,
                    tool_prefix: None,
                }],
            }),
        )];
        register_provided_capabilities(&registry, &manifests, None);

        let err = registry.route("ns1", "create_choice", json!({})).await;
        assert!(err.is_err(), "无 bridge 时应返回 NotBridged 错误");
        let msg = format!("{}", err.unwrap_err());
        assert!(
            msg.contains("no bridge connected"),
            "声明 method 应提示未桥接: {msg}"
        );
    }

    /// 测试用 bridge stub——记录调用并返回固定值。
    struct CapturingBridge {
        received: parking_lot::Mutex<Vec<(String, String, String, Value)>>,
        return_value: Value,
    }

    #[async_trait]
    impl CapabilityBridge for CapturingBridge {
        async fn forward(
            &self,
            plugin_id: &str,
            namespace: &str,
            method: &str,
            params: Value,
        ) -> Result<Value, McpError> {
            self.received.lock().push((
                plugin_id.to_string(),
                namespace.to_string(),
                method.to_string(),
                params.clone(),
            ));
            Ok(self.return_value.clone())
        }
    }

    #[tokio::test]
    async fn test_bridge_forwarded_when_connected() {
        // 注入 bridge 后，handler 应把调用转发给 bridge 并返回其结果。
        let bridge = Arc::new(CapturingBridge {
            received: parking_lot::Mutex::new(vec![]),
            return_value: json!({"request_id": "req-123", "status": "pending"}),
        });
        let registry = CapabilityHandlerRegistry::new();
        let manifests = vec![make_manifest(
            "human_interaction_service",
            Some(ProvidesCapabilities {
                capabilities: vec![ProvidedCapability {
                    namespace: "human-interaction".to_string(),
                    methods: vec!["create_choice".to_string(), "wait_for_choice".to_string()],
                    host: ProvidedCapabilityHost::InProcess,
                    tool_prefix: None,
                }],
            }),
        )];
        register_provided_capabilities(&registry, &manifests, Some(bridge.clone()));

        let result = registry
            .route(
                "human-interaction",
                "create_choice",
                json!({"title": "确认", "options": [{"id": "y", "label": "是"}]}),
            )
            .await
            .unwrap();
        assert_eq!(result["request_id"], "req-123");

        // 验证 bridge 收到了完整调用信息
        let calls = bridge.received.lock();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, "human_interaction_service");
        assert_eq!(calls[0].1, "human-interaction");
        assert_eq!(calls[0].2, "create_choice");
        assert_eq!(calls[0].3["title"], "确认");
    }

    #[tokio::test]
    async fn test_bridge_still_rejects_undeclared_method() {
        // 即便有 bridge，未声明的 method 仍应被 handler 拦截，不到 bridge。
        let bridge = Arc::new(CapturingBridge {
            received: parking_lot::Mutex::new(vec![]),
            return_value: json!({}),
        });
        let registry = CapabilityHandlerRegistry::new();
        let manifests = vec![make_manifest(
            "p1",
            Some(ProvidesCapabilities {
                capabilities: vec![ProvidedCapability {
                    namespace: "ns1".to_string(),
                    methods: vec!["allowed".to_string()],
                    host: ProvidedCapabilityHost::InProcess,
                    tool_prefix: None,
                }],
            }),
        )];
        register_provided_capabilities(&registry, &manifests, Some(bridge.clone()));

        let err = registry.route("ns1", "forbidden", json!({})).await;
        assert!(err.is_err(), "未声明 method 应被拦截");
        assert!(
            bridge.received.lock().is_empty(),
            "未声明 method 不应到达 bridge"
        );
    }

    #[tokio::test]
    async fn test_registered_via_trait_router_visible_in_known_namespaces() {
        // 注册后通过 CapabilityRouter trait 的 known_namespaces 可见，
        // 这意味着 reader loop 和 initialize 声明会自动包含它。
        let registry = Arc::new(CapabilityHandlerRegistry::new());
        let manifests = vec![make_manifest(
            "p1",
            Some(ProvidesCapabilities {
                capabilities: vec![ProvidedCapability {
                    namespace: "dynamic-from-manifest".to_string(),
                    methods: vec!["m1".to_string()],
                    host: ProvidedCapabilityHost::InProcess,
                    tool_prefix: None,
                }],
            }),
        )];
        register_provided_capabilities(&registry, &manifests, None);

        let router: Arc<dyn CapabilityRouter> = registry;
        let ns = router.known_namespaces();
        assert!(
            ns.contains(&"dynamic-from-manifest".to_string()),
            "manifest 注册的 namespace 必须出现在 known_namespaces: {ns:?}"
        );
    }

    // ── McpBridge 测试（M5-2）──

    /// 记录 invoke_tool 调用并返回可配置结果的 mock invoker。
    struct MockInvoker {
        calls: Arc<parking_lot::Mutex<Vec<(String, String, Value)>>>,
        return_result: ToolExecutionResult,
    }

    #[async_trait]
    impl PluginInvoker for MockInvoker {
        async fn invoke_pipeline_plugin(
            &self,
            _: &str,
            _: &PluginContext,
        ) -> Result<PluginResult, PluginError> {
            unreachable!("McpBridge 不调 invoke_pipeline_plugin")
        }
        async fn invoke_tool(
            &self,
            plugin_id: &str,
            tool_name: &str,
            inputs: &Value,
        ) -> Result<ToolExecutionResult, PluginError> {
            self.calls
                .lock()
                .push((plugin_id.to_string(), tool_name.to_string(), inputs.clone()));
            Ok(self.return_result.clone())
        }
        async fn send_lifecycle_hook(
            &self,
            _: &str,
            _: LifecycleHook,
            _: &HookContext,
        ) -> Result<(), PluginError> {
            unreachable!("McpBridge 不调 send_lifecycle_hook")
        }
    }

    #[tokio::test]
    async fn test_mcp_bridge_forwards_to_invoker() {
        // bridge 收到 (human-interaction, create_choice, params) 后，
        // 应查路由表得到 (human_interaction_service, interaction.create_choice)，
        // 调 invoker.invoke_tool 并返回 data。
        let invoker = Arc::new(MockInvoker {
            calls: Arc::new(parking_lot::Mutex::new(vec![])),
            return_result: ToolExecutionResult::success(json!({"request_id": "r1"})),
        });
        let bridge = McpBridge::new(invoker.clone());
        bridge.add_route(
            "human-interaction",
            CapabilityRoute {
                plugin_id: "human_interaction_service".to_string(),
                tool_prefix: "interaction".to_string(),
            },
        );

        let result = bridge
            .forward(
                "ignored",
                "human-interaction",
                "create_choice",
                json!({"title": "T"}),
            )
            .await
            .unwrap();
        assert_eq!(result["request_id"], "r1");

        let calls = invoker.calls.lock();
        assert_eq!(calls[0].0, "human_interaction_service");
        assert_eq!(calls[0].1, "interaction.create_choice");
        assert_eq!(calls[0].2["title"], "T");
    }

    #[tokio::test]
    async fn test_mcp_bridge_unknown_namespace_errors() {
        let invoker = Arc::new(MockInvoker {
            calls: Arc::new(parking_lot::Mutex::new(vec![])),
            return_result: ToolExecutionResult::success(json!({})),
        });
        let calls_ref = invoker.calls.clone();
        let bridge = McpBridge::new(invoker);

        let err = bridge.forward("p", "unknown-ns", "m", json!({})).await;
        assert!(err.is_err());
        assert!(
            calls_ref.lock().is_empty(),
            "未注册 namespace 不应调 invoker"
        );
    }

    #[tokio::test]
    async fn test_mcp_bridge_propagates_tool_failure() {
        // sidecar 工具返回 failure 时，bridge 应转成 McpError。
        let invoker = Arc::new(MockInvoker {
            calls: Arc::new(parking_lot::Mutex::new(vec![])),
            return_result: ToolExecutionResult::failure("interaction timed out"),
        });
        let bridge = McpBridge::new(invoker);
        bridge.add_route(
            "human-interaction",
            CapabilityRoute {
                plugin_id: "p".to_string(),
                tool_prefix: "interaction".to_string(),
            },
        );

        let err = bridge
            .forward("p", "human-interaction", "wait_for_choice", json!({}))
            .await;
        assert!(err.is_err());
        let msg = format!("{}", err.unwrap_err());
        assert!(msg.contains("timed out"), "应传播工具错误: {msg}");
    }

    #[test]
    fn test_add_routes_from_manifests_default_prefix() {
        // add_routes_from_manifests 默认把 namespace 的连字符转下划线作为 tool_prefix。
        let invoker = Arc::new(MockInvoker {
            calls: Arc::new(parking_lot::Mutex::new(vec![])),
            return_result: ToolExecutionResult::success(json!({})),
        });
        let bridge = McpBridge::new(invoker);
        let manifests = vec![make_manifest(
            "my_service",
            Some(ProvidesCapabilities {
                capabilities: vec![ProvidedCapability {
                    namespace: "my-cap".to_string(),
                    methods: vec!["do".to_string()],
                    host: ProvidedCapabilityHost::InProcess,
                    tool_prefix: None,
                }],
            }),
        )];
        bridge.add_routes_from_manifests(&manifests);

        let routes = bridge.routes.read();
        let r = routes.get("my-cap").unwrap();
        assert_eq!(r.plugin_id, "my_service");
        assert_eq!(r.tool_prefix, "my_cap", "连字符应转下划线");
    }

    #[test]
    fn test_add_routes_uses_explicit_tool_prefix_from_manifest() {
        // manifest 显式声明 tool_prefix 时，优先用它（不靠 namespace 推导）。
        // 这是声明式路由的关键：namespace=human-interaction + tool_prefix=interaction
        // → 路由到 interaction.<method>，无需内核硬编码。
        let invoker = Arc::new(MockInvoker {
            calls: Arc::new(parking_lot::Mutex::new(vec![])),
            return_result: ToolExecutionResult::success(json!({})),
        });
        let bridge = McpBridge::new(invoker);
        let manifests = vec![make_manifest(
            "human_interaction_tool",
            Some(ProvidesCapabilities {
                capabilities: vec![ProvidedCapability {
                    namespace: "human-interaction".to_string(),
                    methods: vec!["create_choice".to_string()],
                    host: ProvidedCapabilityHost::Sidecar,
                    tool_prefix: Some("interaction".to_string()),
                }],
            }),
        )];
        bridge.add_routes_from_manifests(&manifests);

        let routes = bridge.routes.read();
        let r = routes.get("human-interaction").unwrap();
        assert_eq!(r.plugin_id, "human_interaction_tool");
        assert_eq!(
            r.tool_prefix, "interaction",
            "显式 tool_prefix 应覆盖 namespace 默认推导"
        );
    }
}

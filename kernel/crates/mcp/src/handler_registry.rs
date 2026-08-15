//! Capability handler 注册表——sidecar 反向调用内核能力的动态路由层。
//!
//! 与 [`crate::capability::CapabilityRouter`] 的关系：
//! - `CapabilityRouter` 是 trait，定义"处理一次反向调用"的契约；
//! - 本模块的 [`CapabilityHandlerRegistry`] 是它的一个实现，按 namespace 动态分派到
//!   各个 [`CapabilityHandler`]。
//!
//! ## 为什么需要注册表
//!
//! 集中式 `match (capability, method)` 分派下，新增一个能力 namespace
//! （如 `human-interaction`）要同时改：
//! 1. `STANDARD_CAPABILITIES` 白名单常量；
//! 2. `build_declared_capabilities` 声明；
//! 3. `handle` 的 match 分支。
//!
//! 注册表把这三点收敛成"启动时 `register(Arc<dyn CapabilityHandler>)` 一处"，
//! namespace 查询、initialize 声明、路由分派全部从注册表派生，新增能力不再碰内核枚举。
//!
//! ## 内置能力与插件能力地位平等
//!
//! 内核自带能力（pipeline-executor / event-bus / metrics 等）在启动时也调
//! `register` 注册自己的 handler，与外部插件注册走同一条路。

use std::sync::Arc;

use async_trait::async_trait;
use parking_lot::RwLock;
use serde_json::Value;

use crate::capability::CapabilityRouter;
use crate::error::McpError;

/// 单个 capability namespace 的处理者。
///
/// 每个 handler 负责一个 namespace（如 `"metrics"` / `"human-interaction"`），
/// 处理该 namespace 下所有 method 的反向调用。实现方持有真实内核服务句柄。
#[async_trait]
pub trait CapabilityHandler: Send + Sync {
    /// 该 handler 负责的命名空间（如 `"pipeline-executor"`）。
    fn namespace(&self) -> &str;

    /// 处理一次反向调用。
    ///
    /// Args:
    /// - `method`: method 名（如 `"suspend"`，不含 namespace 前缀）；
    /// - `params`: JSON-RPC params。
    async fn handle(&self, method: &str, params: Value) -> Result<Value, McpError>;
}

/// Capability handler 注册表——动态路由 sidecar 反向调用到各 handler。
///
/// 线程安全（内部 `parking_lot::RwLock<HashMap>`）。`register`/`unregister` 拿写锁，
/// `route`/`has_namespace`/`namespaces` 拿读锁。
pub struct CapabilityHandlerRegistry {
    handlers: RwLock<std::collections::HashMap<String, Arc<dyn CapabilityHandler>>>,
}

impl CapabilityHandlerRegistry {
    /// 创建空注册表。
    pub fn new() -> Self {
        Self {
            handlers: RwLock::new(std::collections::HashMap::new()),
        }
    }

    /// 注册一个 handler。同名 namespace 覆盖旧 handler（支持热替换）。
    pub fn register(&self, handler: Arc<dyn CapabilityHandler>) {
        let ns = handler.namespace().to_string();
        self.handlers.write().insert(ns, handler);
    }

    /// 注销指定 namespace 的 handler（插件卸载时调用）。
    ///
    /// Returns:
    /// - `true`: 注销成功；
    /// - `false`: 该 namespace 本来就没注册。
    pub fn unregister(&self, namespace: &str) -> bool {
        self.handlers.write().remove(namespace).is_some()
    }

    /// 是否注册了指定 namespace。
    pub fn has_namespace(&self, namespace: &str) -> bool {
        self.handlers.read().contains_key(namespace)
    }

    /// 当前所有已注册 namespace（用于 initialize 声明、白名单派生）。
    pub fn namespaces(&self) -> Vec<String> {
        self.handlers.read().keys().cloned().collect()
    }

    /// 路由一次反向调用到对应 handler。
    ///
    /// Returns:
    /// - `Ok(value)`: handler 返回的成功结果；
    /// - `Err(McpError)`: namespace 未注册（method not found 语义）或 handler 内部失败。
    pub async fn route(
        &self,
        namespace: &str,
        method: &str,
        params: Value,
    ) -> Result<Value, McpError> {
        let handler = { self.handlers.read().get(namespace).cloned() };
        match handler {
            Some(h) => h.handle(method, params).await,
            None => Err(McpError::Protocol {
                message: format!(
                    "capability namespace '{}' not registered (known: {:?})",
                    namespace,
                    self.namespaces()
                ),
            }),
        }
    }
}

impl Default for CapabilityHandlerRegistry {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl CapabilityRouter for CapabilityHandlerRegistry {
    /// 实现 `CapabilityRouter` trait：按 `<capability>.<method>` 拆分后委托 route。
    ///
    /// 这让注册表本身可以作为一个 `CapabilityRouter` 注入到 McpClient，
    /// 内核无需再维护单独的 router 实现即可获得动态路由能力。
    async fn handle(
        &self,
        capability: &str,
        method: &str,
        params: Value,
    ) -> Result<Value, McpError> {
        self.route(capability, method, params).await
    }

    /// 覆盖默认实现，返回注册表里实际注册的 namespace（动态白名单）。
    fn known_namespaces(&self) -> Vec<String> {
        self.namespaces()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// 回显 handler——记录调用参数，用于验证路由正确性。
    struct EchoHandler {
        ns: String,
    }

    #[async_trait]
    impl CapabilityHandler for EchoHandler {
        fn namespace(&self) -> &str {
            &self.ns
        }
        async fn handle(&self, method: &str, params: Value) -> Result<Value, McpError> {
            Ok(json!({"namespace": self.ns, "method": method, "params": params}))
        }
    }

    #[tokio::test]
    async fn test_register_and_route_to_handler() {
        let registry = CapabilityHandlerRegistry::new();
        registry.register(Arc::new(EchoHandler {
            ns: "custom-cap".into(),
        }));

        let result = registry
            .route("custom-cap", "do_thing", json!({"x": 1}))
            .await
            .unwrap();
        assert_eq!(result["namespace"], "custom-cap");
        assert_eq!(result["method"], "do_thing");
        assert_eq!(result["params"]["x"], 1);
    }

    #[tokio::test]
    async fn test_namespaces_reflects_registration() {
        let registry = CapabilityHandlerRegistry::new();
        assert!(!registry.has_namespace("my-cap"));
        assert!(!registry.namespaces().contains(&"my-cap".to_string()));

        registry.register(Arc::new(EchoHandler {
            ns: "my-cap".into(),
        }));
        assert!(registry.has_namespace("my-cap"));
        assert!(registry.namespaces().contains(&"my-cap".to_string()));
    }

    #[tokio::test]
    async fn test_route_rejects_unregistered_namespace() {
        let registry = CapabilityHandlerRegistry::new();
        let err = registry.route("unknown", "method", json!({})).await;
        assert!(
            err.is_err(),
            "未注册的 namespace 必须返回错误，不能静默成功"
        );
        let msg = format!("{}", err.unwrap_err());
        assert!(
            msg.contains("not registered"),
            "错误信息应说明 namespace 未注册，实际: {msg}"
        );
    }

    #[tokio::test]
    async fn test_unregister_removes_handler() {
        let registry = CapabilityHandlerRegistry::new();
        registry.register(Arc::new(EchoHandler {
            ns: "temp-cap".into(),
        }));
        assert!(registry.has_namespace("temp-cap"));

        assert!(registry.unregister("temp-cap"));
        assert!(!registry.has_namespace("temp-cap"));
        assert!(!registry.unregister("temp-cap")); // 二次注销返回 false
    }

    #[tokio::test]
    async fn test_register_overrides_same_namespace() {
        // 同 namespace 重复注册 = 热替换，以最新 handler 为准。
        let registry = CapabilityHandlerRegistry::new();

        struct CountingHandler {
            ns: String,
            tag: &'static str,
        }
        #[async_trait]
        impl CapabilityHandler for CountingHandler {
            fn namespace(&self) -> &str {
                &self.ns
            }
            async fn handle(&self, _method: &str, _params: Value) -> Result<Value, McpError> {
                Ok(json!({"tag": self.tag}))
            }
        }

        registry.register(Arc::new(CountingHandler {
            ns: "hot-cap".into(),
            tag: "v1",
        }));
        registry.register(Arc::new(CountingHandler {
            ns: "hot-cap".into(),
            tag: "v2",
        }));

        let result = registry.route("hot-cap", "m", json!({})).await.unwrap();
        assert_eq!(result["tag"], "v2", "重复注册应覆盖，以最新为准");
    }

    #[tokio::test]
    async fn test_registry_itself_is_a_capability_router() {
        // CapabilityHandlerRegistry 实现了 CapabilityRouter trait，
        // 可直接注入 McpClient，无需额外适配层。
        let registry = CapabilityHandlerRegistry::new();
        registry.register(Arc::new(EchoHandler {
            ns: "via-trait".into(),
        }));

        let router: Arc<dyn CapabilityRouter> = Arc::new(registry);
        let result = router.handle("via-trait", "ping", json!({})).await.unwrap();
        assert_eq!(result["namespace"], "via-trait");
        assert_eq!(result["method"], "ping");
    }

    #[tokio::test]
    async fn test_router_trait_exposes_known_namespaces() {
        // 通过 CapabilityRouter trait 的 known_namespaces() 能拿到注册的 namespace。
        // 这是 reader loop 用 parse_capability_method_with 做动态白名单解析的依据。
        let registry = CapabilityHandlerRegistry::new();
        registry.register(Arc::new(EchoHandler {
            ns: "dynamic-ns".into(),
        }));
        registry.register(Arc::new(EchoHandler {
            ns: "another-ns".into(),
        }));

        let router: Arc<dyn CapabilityRouter> = Arc::new(registry);
        let mut ns = router.known_namespaces();
        ns.sort();
        assert_eq!(ns, vec!["another-ns", "dynamic-ns"]);
    }

    // ── Cycle 2.5：迁移可行性验证 ──
    // 用一个仿真 MetricsHandler 证明：现有 KernelCapabilityRouter 里 metrics.record
    // 的 match 分支可以无损抽成 CapabilityHandler 注册进 registry，行为不变。
    // 这验证了 M2 的核心承诺——现有能力可以渐进迁移到 handler 形态。

    /// 仿真 metrics 累加器（模拟 MetricsAggregator 的最小行为）。
    #[derive(Default)]
    struct FakeMetricsStore {
        counters: parking_lot::Mutex<std::collections::HashMap<String, f64>>,
    }

    /// 仿真 metrics handler——镜像 capability_router.rs:302-358 的 record 逻辑。
    struct MetricsHandler {
        store: Arc<FakeMetricsStore>,
    }

    #[async_trait]
    impl CapabilityHandler for MetricsHandler {
        fn namespace(&self) -> &str {
            "metrics"
        }
        async fn handle(&self, method: &str, params: Value) -> Result<Value, McpError> {
            match method {
                "record" => {
                    let plugin_id = params
                        .get("_plugin_id")
                        .and_then(|v| v.as_str())
                        .unwrap_or("unknown")
                        .to_string();
                    let name = params.get("name").and_then(|v| v.as_str()).ok_or_else(|| {
                        McpError::Protocol {
                            message: "metrics.record 缺少 name 参数".to_string(),
                        }
                    })?;
                    let value = params
                        .get("value")
                        .and_then(|v| v.as_f64())
                        .ok_or_else(|| McpError::Protocol {
                            message: "metrics.record 缺少或非法 value 参数".to_string(),
                        })?;
                    // 简化：只处理 counter 累加（真实 handler 还支持 gauge/histogram）
                    let key = format!("{plugin_id}:{name}");
                    let mut counters = self.store.counters.lock();
                    *counters.entry(key.clone()).or_insert(0.0) += value;
                    Ok(json!({"status": "recorded", "plugin_id": plugin_id, "name": name}))
                }
                _ => Err(McpError::Protocol {
                    message: format!("metrics.{method} not implemented"),
                }),
            }
        }
    }

    #[tokio::test]
    async fn test_metrics_handler_migrates_cleanly() {
        // 验证：metrics 能力以 handler 形式注册进 registry 后，
        // 经 CapabilityRouter trait 调用，行为与现有 match 分支一致。
        let store = Arc::new(FakeMetricsStore::default());
        let registry = CapabilityHandlerRegistry::new();
        registry.register(Arc::new(MetricsHandler {
            store: store.clone(),
        }));

        // 通过 trait 调用（模拟 reader loop 的真实路径）
        let router: Arc<dyn CapabilityRouter> = Arc::new(registry);

        // 第一次 record
        let r1 = router
            .handle(
                "metrics",
                "record",
                json!({"_plugin_id": "p1", "name": "tokens", "value": 100.0}),
            )
            .await
            .unwrap();
        assert_eq!(r1["status"], "recorded");
        assert_eq!(r1["plugin_id"], "p1");

        // 第二次 record 同一 counter，应累加
        router
            .handle(
                "metrics",
                "record",
                json!({"_plugin_id": "p1", "name": "tokens", "value": 50.0}),
            )
            .await
            .unwrap();

        // 验证累加生效（handler 内部状态正确）
        let accumulated = *store.counters.lock().get("p1:tokens").unwrap();
        assert!(
            (accumulated - 150.0).abs() < f64::EPSILON,
            "counter 应累加到 150，实际 {accumulated}"
        );

        // 验证 namespace 动态出现在 known_namespaces（无需改 STANDARD_CAPABILITIES）
        assert!(router.known_namespaces().contains(&"metrics".to_string()));
    }

    #[tokio::test]
    async fn test_metrics_handler_rejects_missing_name() {
        // 验证 handler 形态保留了原 match 分支的参数校验。
        let store = Arc::new(FakeMetricsStore::default());
        let registry = CapabilityHandlerRegistry::new();
        registry.register(Arc::new(MetricsHandler { store }));

        let router: Arc<dyn CapabilityRouter> = Arc::new(registry);
        let err = router
            .handle(
                "metrics",
                "record",
                json!({"_plugin_id": "p1", "value": 1.0}),
            )
            .await;
        assert!(err.is_err(), "缺少 name 参数应返回错误");
    }
}

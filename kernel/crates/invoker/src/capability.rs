//! 插件 → 内核 capability 反向调用的桥接（native 专用）。
//!
//! 边车走 MCP JSON-RPC 反向调用 router（`router.handle(cap, method, params)`）。
//! native 要拿到同样能力，本模块把同一个 `CapabilityRouter` 适配成
//! PluginInput 注入的同步 host_call 入口。
//!
//! 适配器只做"转发"——不实现任何业务逻辑，与 sidecar 用同一个 router，两轨对齐。

use std::sync::Arc;

use agentos_core::types::PluginError;
use agentos_mcp::CapabilityRouter;
use serde_json::Value;

/// 在同步上下文里调用 async 的 router.handle。
///
/// native 的 plugin_execute 是同步 C 函数，需要在同步调用里执行 async router。
///
/// 用 `block_in_place` + `Handle::block_on`：多线程 runtime 下 block_in_place 让出
/// 当前 worker 线程的调度权，避免在自身上再 block_on 导致死锁。单线程 runtime
/// 不可用（会 panic）——本内核用多线程（#[tokio::main] 默认 multi_thread）。
fn block_on_router(
    router: &Arc<dyn CapabilityRouter>,
    capability: &str,
    method: &str,
    params: Value,
) -> Result<Value, PluginError> {
    let router = Arc::clone(router);
    let cap = capability.to_string();
    let m = method.to_string();
    tokio::task::block_in_place(|| {
        tokio::runtime::Handle::current().block_on(async move {
            router
                .handle(&cap, &m, params)
                .await
                .map_err(router_err_to_plugin)
        })
    })
}

/// 把 router 的 McpError 转成 PluginError（native 路径统一用 PluginError）。
fn router_err_to_plugin(e: agentos_mcp::McpError) -> PluginError {
    PluginError {
        message: format!("capability call failed: {}", e),
        code: Some("CAPABILITY_FAILED".to_string()),
        source: Some("plugin-invoker".to_string()),
    }
}

/// 同步调用 router（native 插件的 host_call 入口直接用这个）。
///
/// native SDK 通过函数指针调到这里，转发到 router。
pub fn call_capability(
    router: &Arc<dyn CapabilityRouter>,
    capability: &str,
    method: &str,
    params: Value,
) -> Result<Value, PluginError> {
    block_on_router(router, capability, method, params)
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_mcp::McpError;
    use async_trait::async_trait;
    use serde_json::json;

    /// 一个 stub router：把 (cap, method, params) 回显，便于断言转发正确。
    struct EchoRouter;
    #[async_trait]
    impl CapabilityRouter for EchoRouter {
        async fn handle(
            &self,
            capability: &str,
            method: &str,
            params: Value,
        ) -> Result<Value, McpError> {
            Ok(json!({"cap": capability, "method": method, "params": params}))
        }
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn call_capability_helper_forwards() {
        let router: Arc<dyn CapabilityRouter> = Arc::new(EchoRouter);
        let result = call_capability(&router, "event-bus", "emit", json!({"x": 1})).unwrap();
        assert_eq!(result["cap"], "event-bus");
        assert_eq!(result["method"], "emit");
    }
}

//! 插件 → 内核 capability 反向调用的桥接（native / wasm 专用）。
//!
//! 边车走 MCP JSON-RPC 反向调用 router（`router.handle(cap, method, params)`）。
//! native/wasm 要拿到同样能力，本模块把同一个 `CapabilityRouter` 适配成：
//! - wasm：`WasmHostRegistry`（guest 经 `host.call` import 调用，wasmtime 同步分发）
//! - native：通过 PluginInput 注入的同步 host_call 入口
//!
//! 适配器只做"转发"——不实现任何业务逻辑，与 sidecar 用同一个 router，三家对齐。

use std::sync::Arc;

use agentos_core::types::PluginError;
use agentos_mcp::CapabilityRouter;
use agentos_plugin_loader::{HostCapability, WasmCapabilityChecker, WasmHostRegistry};
use serde_json::{json, Value};

/// 把 `host.call` 这个 wasm import 转发到 `CapabilityRouter::handle`。
///
/// guest 调用约定：`host.call(params)`，params 形如：
/// `{ "capability": "metrics", "method": "record", "params": {...} }`
/// 返回 router.handle 的结果 JSON，或 `{"error": "..."}`。
///
/// 名字固定为 `host.call`——一个入口覆盖全部 capability，避免为每个能力
/// 生成不同 import 函数（wasm import 名也不能含 `.`/`-`）。
pub const HOST_CALL_NAME: &str = "host.call";

/// 在同步上下文里调用 async 的 router.handle。
///
/// wasm 的 dispatch_host_call 是同步闭包（wasmtime func_wrap 不能 await），
/// native 的 plugin_execute 也是同步 C 函数。两者都需要在同步调用里执行 async router。
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
            router.handle(&cap, &m, params).await.map_err(router_err_to_plugin)
        })
    })
}

/// 把 router 的 McpError 转成 PluginError（native/wasm 路径统一用 PluginError）。
fn router_err_to_plugin(e: agentos_mcp::McpError) -> PluginError {
    PluginError {
        message: format!("capability call failed: {}", e),
        code: Some("CAPABILITY_FAILED".to_string()),
        source: Some("plugin-invoker".to_string()),
    }
}

/// 一个万能 host 能力：`host.call`，把 guest 的请求转发到 CapabilityRouter。
struct RouterHostCapability {
    router: Arc<dyn CapabilityRouter>,
}

impl HostCapability for RouterHostCapability {
    fn name(&self) -> &str {
        HOST_CALL_NAME
    }

    fn call(&self, params: &Value) -> Result<Value, PluginError> {
        let capability = params
            .get("capability")
            .and_then(|v| v.as_str())
            .ok_or_else(|| PluginError {
                message: "host.call missing 'capability' field".to_string(),
                code: Some("HOST_CALL_BAD_PARAMS".to_string()),
                source: Some("plugin-invoker".to_string()),
            })?;
        let method = params
            .get("method")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let inner_params = params.get("params").cloned().unwrap_or(Value::Null);
        block_on_router(&self.router, capability, method, inner_params)
    }
}

/// 把 CapabilityRouter 包装成 WasmHostRegistry（wasm 插件用）。
///
/// 注册唯一的 `host.call` 能力——guest 经它调用任意 capability。
/// 白名单校验：manifest.granted_capabilities 里声明 "host.call" 即授予全部能力，
/// 由 router 侧各 capability 自己鉴权（与 sidecar 一致——sidecar 也是 router 统一处理）。
pub fn wasm_host_registry(router: Arc<dyn CapabilityRouter>) -> Arc<dyn WasmHostRegistry> {
    Arc::new(WasmRouterRegistry {
        caps: vec![Arc::new(RouterHostCapability { router }) as Arc<dyn HostCapability>],
    })
}

struct WasmRouterRegistry {
    caps: Vec<Arc<dyn HostCapability>>,
}

impl WasmHostRegistry for WasmRouterRegistry {
    fn capabilities(&self) -> Vec<Arc<dyn HostCapability>> {
        self.caps.clone()
    }
}

/// 同步调用 router（native 插件的 host_call 入口直接用这个）。
///
/// native SDK 通过函数指针调到这里，转发到 router。与 wasm 共用同一套转发逻辑。
pub fn call_capability(
    router: &Arc<dyn CapabilityRouter>,
    capability: &str,
    method: &str,
    params: Value,
) -> Result<Value, PluginError> {
    block_on_router(router, capability, method, params)
}

/// 白名单校验器：对所有插件授予 `host.call`。
///
/// 为什么不在 wasm 这层做精细白名单：native/wasm/sidecar 三家应一致，而 sidecar 是
/// 由 router 侧各 capability 统一鉴权（如 pipeline-executor 操作校验 run_id 归属）。
/// 故 native/wasm 也只暴露 host.call 一个入口，细粒度鉴权交给 router——三家对齐。
pub struct AllowHostCallChecker;

impl WasmCapabilityChecker for AllowHostCallChecker {
    fn granted(&self, _plugin_id: &str) -> Vec<String> {
        vec![HOST_CALL_NAME.to_string()]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;
    use agentos_mcp::McpError;
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
    async fn host_call_forwards_to_router() {
        let router: Arc<dyn CapabilityRouter> = Arc::new(EchoRouter);
        let cap = RouterHostCapability { router };
        let params = json!({
            "capability": "metrics",
            "method": "record",
            "params": {"k": "v"}
        });
        let result = cap.call(&params).unwrap();
        assert_eq!(result["cap"], "metrics");
        assert_eq!(result["method"], "record");
        assert_eq!(result["params"]["k"], "v");
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn wasm_registry_exposes_host_call() {
        let router: Arc<dyn CapabilityRouter> = Arc::new(EchoRouter);
        let registry = wasm_host_registry(router);
        let caps = registry.capabilities();
        assert_eq!(caps.len(), 1);
        assert_eq!(caps[0].name(), HOST_CALL_NAME);
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn call_capability_helper_forwards() {
        let router: Arc<dyn CapabilityRouter> = Arc::new(EchoRouter);
        let result = call_capability(&router, "event-bus", "emit", json!({"x": 1})).unwrap();
        assert_eq!(result["cap"], "event-bus");
        assert_eq!(result["method"], "emit");
    }
}

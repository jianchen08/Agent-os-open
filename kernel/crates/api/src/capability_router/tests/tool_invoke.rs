use super::*;

use std::sync::Arc;

/// 捕获 invoke_tool 收到的 (plugin_id, tool_name, inputs)。
pub(super) struct CaptureInvoker {
    captured: std::sync::Arc<std::sync::Mutex<Vec<(String, String, Value)>>>,
}

impl CaptureInvoker {
    /// 外持 captured 桥构造：调用方保留句柄供断言。
    pub(super) fn with_captured(
        captured: std::sync::Arc<std::sync::Mutex<Vec<(String, String, Value)>>>,
    ) -> Self {
        Self { captured }
    }
}
#[async_trait::async_trait]
impl agentos_core::traits::PluginInvoker for CaptureInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        _ctx: &agentos_core::types::PluginContext<'a>,
    ) -> Result<agentos_core::types::PluginResult, agentos_core::types::PluginError> {
        Err(agentos_core::types::PluginError {
            message: "not used in test".into(),
            code: None,
            source: None,
        })
    }
    async fn invoke_tool(
        &self,
        plugin_id: &str,
        tool_name: &str,
        inputs: &Value,
    ) -> Result<agentos_core::types::ToolExecutionResult, agentos_core::types::PluginError> {
        self.captured.lock().unwrap().push((
            plugin_id.to_string(),
            tool_name.to_string(),
            inputs.clone(),
        ));
        Ok(agentos_core::types::ToolExecutionResult {
            success: true,
            data: json!({"output": "ok"}),
            error: None,
            duration_ms: Some(1),
            metadata: None,
        })
    }
    async fn send_lifecycle_hook(
        &self,
        _plugin_id: &str,
        _hook: agentos_core::traits::LifecycleHook,
        _context: &agentos_core::traits::HookContext,
    ) -> Result<(), agentos_core::types::PluginError> {
        Ok(())
    }
}

/// 构造带单工具注册表（bash_execute → plugin_bash）+ 捕获 invoker 的 router。
fn router_with_tool_invoke(
    captured: std::sync::Arc<std::sync::Mutex<Vec<(String, String, Value)>>>,
) -> KernelCapabilityRouter {
    use agentos_core::traits::ToolDescriptor;
    use agentos_core::types::{ToolCategory, ToolSource};
    use agentos_plugin_loader::CapabilityRegistryImpl;
    let registry = CapabilityRegistryImpl::new();
    registry.register_tool(
        "plugin_bash",
        ToolDescriptor {
            name: "bash_execute".into(),
            description: String::new(),
            plugin_id: "plugin_bash".into(),
            input_schema: json!({}),
            output_schema: None,
            category: ToolCategory::System,
            source: ToolSource::Mcp,
            ui: None,
            render: None,
            isolation_capable: None,
        },
    );
    KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_invoker(Arc::new(CaptureInvoker { captured }))
        .with_registry(Arc::new(registry))
}

#[tokio::test]
async fn test_tool_executor_invoke_merges_call_context_into_args() {
    // tool_core 在 params 级携带 _call_context（前端路由键）→
    // 内核合入 tool args 透传给工具 sidecar（bash 据此推 tool_progress）。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_tool_invoke(captured.clone());

    let params = json!({
        "tool_name": "bash_execute",
        "args": {"command": "echo hi", "session_id": "sess-1"},
        "_call_context": {
            "call_id": "call_abc",
            "pipeline_id": "pipe-1",
            "message_id": "msg-1",
            "thread_id": "sess-1"
        }
    });
    let res = router
        .handle("tool-executor", "invoke", params)
        .await
        .unwrap();
    assert_eq!(res["success"], true);

    let calls = captured.lock().unwrap().clone();
    assert_eq!(calls.len(), 1);
    let (plugin_id, tool_name, inputs) = &calls[0];
    assert_eq!(plugin_id, "plugin_bash");
    assert_eq!(tool_name, "bash_execute");
    assert_eq!(inputs["command"], "echo hi");
    assert_eq!(inputs["session_id"], "sess-1");
    assert_eq!(inputs["_call_context"]["call_id"], "call_abc");
    assert_eq!(inputs["_call_context"]["pipeline_id"], "pipe-1");
    assert_eq!(inputs["_call_context"]["message_id"], "msg-1");
    assert_eq!(inputs["_call_context"]["thread_id"], "sess-1");
}

#[tokio::test]
async fn test_tool_executor_invoke_without_call_context_untouched() {
    // 无 _call_context（旧 tool_core / 无进度需求的工具）→ args 原样透传。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_tool_invoke(captured.clone());

    let params = json!({
        "tool_name": "bash_execute",
        "args": {"command": "echo hi", "session_id": "sess-1"},
    });
    let _ = router
        .handle("tool-executor", "invoke", params)
        .await
        .unwrap();

    let calls = captured.lock().unwrap().clone();
    assert_eq!(calls.len(), 1);
    let inputs = &calls[0].2;
    assert!(inputs.get("_call_context").is_none());
    assert_eq!(inputs["command"], "echo hi");
}

// ── tool-executor.invoke 目标插件解析（显式 plugin_id 优先于注册表反查）──

#[tokio::test]
async fn test_tool_executor_explicit_plugin_id_wins_over_registry() {
    // 调用方显式传 plugin_id（系统插件工具如 hindsight.recall 不在 CapabilityRegistry，
    // 反查必然失败）→ 必须优先用显式 plugin_id，而不是注册表反查结果。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    // 注册表把 bash_execute → plugin_bash；但显式 plugin_id 指向 system 插件 hindsight。
    let router = router_with_tool_invoke(captured.clone());

    let params = json!({
        "tool_name": "bash_execute",
        "plugin_id": "hindsight",
        "args": {"query": "where did I leave the keys", "session_id": "sess-1"},
    });
    let res = router
        .handle("tool-executor", "invoke", params)
        .await
        .unwrap();
    assert_eq!(res["success"], true);

    let calls = captured.lock().unwrap().clone();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].0, "hindsight", "显式 plugin_id 应优先于注册表反查");
    assert_eq!(calls[0].1, "bash_execute");
}

#[tokio::test]
async fn test_tool_executor_empty_plugin_id_falls_back_to_registry() {
    // 显式 plugin_id 为空字符串 → 视为未提供，回退注册表反查。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_tool_invoke(captured.clone());

    let params = json!({
        "tool_name": "bash_execute",
        "plugin_id": "",
        "args": {"command": "echo hi"},
    });
    let _ = router
        .handle("tool-executor", "invoke", params)
        .await
        .unwrap();

    let calls = captured.lock().unwrap().clone();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].0, "plugin_bash", "空 plugin_id 应回退注册表反查");
}

#[tokio::test]
async fn test_tool_executor_internal_keys_stripped_from_args() {
    // _owner/_log_ctx/tenant_id/plugin_id 是内核/SDK 内部元数据，不得透传给工具 handler。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_tool_invoke(captured.clone());

    let params = json!({
        "tool_name": "bash_execute",
        "plugin_id": "plugin_bash",
        "args": {
            "command": "echo hi",
            "session_id": "sess-1",
            "_owner": "user-1",
            "_log_ctx": {"request_id": "req-1"},
            "tenant_id": "tenant-a",
            "plugin_id": "spoofed",
            "pipeline_id": "pipe-1"
        },
        "_log_ctx": {"request_id": "req-1"},
    });
    let _ = router
        .handle("tool-executor", "invoke", params)
        .await
        .unwrap();

    let calls = captured.lock().unwrap().clone();
    let inputs = &calls[0].2;
    // 内部元数据全部剥离
    assert!(inputs.get("_owner").is_none(), "_owner 应被剥离");
    assert!(inputs.get("_log_ctx").is_none(), "_log_ctx 应被剥离");
    assert!(inputs.get("tenant_id").is_none(), "tenant_id 应被剥离");
    assert!(
        inputs.get("plugin_id").is_none(),
        "args 级 plugin_id 应被剥离（防伪造）"
    );
    // 业务字段保留（session_id/pipeline_id 是 tool_args_inject 注入的显式参数）
    assert_eq!(inputs["command"], "echo hi");
    assert_eq!(inputs["session_id"], "sess-1");
    assert_eq!(inputs["pipeline_id"], "pipe-1");
}

#[tokio::test]
async fn test_tool_executor_registry_fallback_without_explicit_id() {
    // 无显式 plugin_id → 注册表 tool_name → plugin_id 反查。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = router_with_tool_invoke(captured.clone());

    let params = json!({
        "tool_name": "bash_execute",
        "args": {"command": "echo hi", "session_id": "sess-1"},
    });
    let _ = router
        .handle("tool-executor", "invoke", params)
        .await
        .unwrap();

    let calls = captured.lock().unwrap().clone();
    assert_eq!(calls[0].0, "plugin_bash");
}

#[tokio::test]
async fn test_tool_executor_unregistered_tool_fails_closed() {
    // 反查失败（无注册表/工具未注册）+ 无显式 plugin_id → fail-closed 报
    // "工具未注册"，不落 invoker（无"工具名当插件 ID"兜底）。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new()).with_invoker(
        Arc::new(CaptureInvoker {
            captured: captured.clone(),
        }),
    );

    let params = json!({
        "tool_name": "some_tool",
        "args": {"x": 1},
    });
    let res = router
        .handle("tool-executor", "invoke", params)
        .await
        .unwrap();

    assert_eq!(res["success"], false);
    assert!(
        res["error"].as_str().unwrap().contains("未注册"),
        "应报工具未注册，got: {}",
        res["error"].as_str().unwrap()
    );
    let calls = captured.lock().unwrap().clone();
    assert!(calls.is_empty(), "未注册工具不应触达 invoker");
}

// ── tool-executor.invoke 调用路径自愈（BUG-37）────────────────────────────
// 注册表条目因 G2 启动期误剔等原因丢失且 watcher 不重扫 → 工具永久不可用。
// 自愈闭包 = "未注册但 manifest 在册 → 重注册再反查"（有界：每次调用至多一次）。

/// 自愈测试夹具：共享注册表 + 记录 heal 触发次数。
/// heal 闭包模拟生产装配（manifest 在册 → 重注册 → 返回描述符）。
fn router_with_heal(
    captured: std::sync::Arc<std::sync::Mutex<Vec<(String, String, Value)>>>,
    registry: Arc<agentos_plugin_loader::CapabilityRegistryImpl>,
    heal_calls: std::sync::Arc<std::sync::Mutex<usize>>,
) -> KernelCapabilityRouter {
    use agentos_core::traits::ToolDescriptor;
    use agentos_core::types::{ToolCategory, ToolSource};
    let reg_for_heal = registry.clone();
    let heal: ToolRegistryHealFn = Arc::new(move |tool_name: &str| {
        let reg = reg_for_heal.clone();
        let calls = heal_calls.clone();
        Box::pin(async move {
            *calls.lock().unwrap() += 1;
            // 模拟生产自愈：manifest 在册 → reenable 重注册（幂等），返回描述符。
            if tool_name != "project_state" {
                return None;
            }
            let descriptor = ToolDescriptor {
                name: tool_name.to_string(),
                description: "healed".into(),
                plugin_id: "project_state_tool".into(),
                input_schema: json!({"type": "object"}),
                output_schema: None,
                category: ToolCategory::System,
                source: ToolSource::Mcp,
                ui: None,
                render: None,
                isolation_capable: None,
            };
            reg.register_tool("project_state_tool", descriptor.clone());
            Some(descriptor)
        })
    });
    KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_invoker(Arc::new(CaptureInvoker { captured }))
        .with_registry(registry)
        .with_tool_registry_heal(heal)
}

#[tokio::test]
async fn test_tool_executor_unregistered_tool_self_heals_and_invokes() {
    // 注册表条目丢失（G2 误剔形态：registry 无 project_state）→ 反查 miss
    // 触发自愈重注册 → 本次调用按重注册结果路由到正确插件，且注册表恢复。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let heal_calls = std::sync::Arc::new(std::sync::Mutex::new(0usize));
    let registry = Arc::new(agentos_plugin_loader::CapabilityRegistryImpl::new());
    let router = router_with_heal(captured.clone(), registry.clone(), heal_calls.clone());

    let params = json!({
        "tool_name": "project_state",
        "args": {"action": "query"},
    });
    let res = router
        .handle("tool-executor", "invoke", params)
        .await
        .unwrap();

    assert_eq!(res["success"], true, "自愈后调用应成功: {}", res["error"]);
    let calls = captured.lock().unwrap().clone();
    assert_eq!(calls.len(), 1, "自愈后应落 invoker 执行");
    assert_eq!(calls[0].0, "project_state_tool", "应路由到重注册的插件");
    assert_eq!(calls[0].1, "project_state");
    // 注册表条目恢复（后续反查不再依赖自愈）
    assert!(
        registry.get_tool("project_state").is_some(),
        "自愈应把工具重注册进共享注册表"
    );
    // 下次调用直接命中注册表，不再触发自愈（有界性）
    let _ = router
        .handle(
            "tool-executor",
            "invoke",
            json!({"tool_name": "project_state", "args": {"action": "query"}}),
        )
        .await
        .unwrap();
    assert_eq!(*heal_calls.lock().unwrap(), 1, "注册表恢复后不应再触发自愈");
}

#[tokio::test]
async fn test_tool_executor_heal_miss_still_fails_closed() {
    // 自愈闭包返回 None（manifest 不在册 / 插件已禁用）→ 保持 fail-closed
    // 报"未注册"，不落 invoker。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let heal_calls = std::sync::Arc::new(std::sync::Mutex::new(0usize));
    let registry = Arc::new(agentos_plugin_loader::CapabilityRegistryImpl::new());
    let router = router_with_heal(captured.clone(), registry.clone(), heal_calls.clone());

    let res = router
        .handle(
            "tool-executor",
            "invoke",
            json!({"tool_name": "ghost_tool", "args": {}}),
        )
        .await
        .unwrap();

    assert_eq!(res["success"], false);
    assert!(
        res["error"].as_str().unwrap().contains("未注册"),
        "自愈未命中应保持 fail-closed，got: {}",
        res["error"].as_str().unwrap()
    );
    assert_eq!(*heal_calls.lock().unwrap(), 1, "应触发过一次自愈尝试");
    assert!(
        captured.lock().unwrap().is_empty(),
        "自愈未命中不得触达 invoker"
    );
}

/// 固定返回错误的 invoker（验证 tool-executor.invoke 的错误归一化）。
struct ErroringInvoker;
#[async_trait::async_trait]
impl agentos_core::traits::PluginInvoker for ErroringInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        _ctx: &agentos_core::types::PluginContext<'a>,
    ) -> Result<agentos_core::types::PluginResult, agentos_core::types::PluginError> {
        Err(agentos_core::types::PluginError {
            message: "not used".into(),
            code: None,
            source: None,
        })
    }
    async fn invoke_tool(
        &self,
        _plugin_id: &str,
        _tool_name: &str,
        _inputs: &Value,
    ) -> Result<agentos_core::types::ToolExecutionResult, agentos_core::types::PluginError> {
        Err(agentos_core::types::PluginError {
            message: "sidecar crashed".into(),
            code: Some("MCP_TOOL_CALL_FAILED".into()),
            source: None,
        })
    }
    async fn send_lifecycle_hook(
        &self,
        _plugin_id: &str,
        _hook: agentos_core::traits::LifecycleHook,
        _context: &agentos_core::traits::HookContext,
    ) -> Result<(), agentos_core::types::PluginError> {
        Ok(())
    }
}

#[tokio::test]
async fn test_tool_executor_invoke_error_normalized_to_failure_json() {
    // invoke_tool 返回 Err → capability 层归一化为 {"success": false, "error": ...}，
    // 不把内核 PluginError 泄漏给 sidecar。
    use agentos_core::traits::ToolDescriptor;
    use agentos_core::types::{ToolCategory, ToolSource};
    use agentos_plugin_loader::CapabilityRegistryImpl;
    let registry = CapabilityRegistryImpl::new();
    registry.register_tool(
        "plugin_bash",
        ToolDescriptor {
            name: "bash_execute".into(),
            description: String::new(),
            plugin_id: "plugin_bash".into(),
            input_schema: json!({}),
            output_schema: None,
            category: ToolCategory::System,
            source: ToolSource::Mcp,
            ui: None,
            render: None,
            isolation_capable: None,
        },
    );
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_invoker(Arc::new(ErroringInvoker))
        .with_registry(Arc::new(registry));

    let res = router
        .handle(
            "tool-executor",
            "invoke",
            json!({"tool_name": "bash_execute", "args": {"command": "echo hi"}}),
        )
        .await
        .unwrap();
    assert_eq!(res["success"], false);
    assert!(res["error"].as_str().unwrap().contains("sidecar crashed"));
}

#[tokio::test]
async fn test_tool_executor_missing_invoker_returns_protocol_error() {
    // 未注入 invoker → Protocol 错误（配置缺失早暴露）。
    // 工具先经注册表反查可达（否则 fail-closed 走"未注册"分支）。
    use agentos_core::traits::ToolDescriptor;
    use agentos_core::types::{ToolCategory, ToolSource};
    use agentos_plugin_loader::CapabilityRegistryImpl;
    let registry = CapabilityRegistryImpl::new();
    registry.register_tool(
        "plugin_bash",
        ToolDescriptor {
            name: "bash_execute".into(),
            description: String::new(),
            plugin_id: "plugin_bash".into(),
            input_schema: json!({}),
            output_schema: None,
            category: ToolCategory::System,
            source: ToolSource::Mcp,
            ui: None,
            render: None,
            isolation_capable: None,
        },
    );
    let router = KernelCapabilityRouter::with_metrics(MetricsAggregator::new())
        .with_registry(Arc::new(registry));
    let res = router
        .handle(
            "tool-executor",
            "invoke",
            json!({"tool_name": "bash_execute", "args": {}}),
        )
        .await;
    assert!(res.is_err());
    let err = res.unwrap_err();
    match err {
        agentos_mcp::McpError::Protocol { message } => {
            assert!(message.contains("未配置 invoker"), "got: {message}")
        }
        other => panic!("expected Protocol error, got {other:?}"),
    }
}

// ── G2 注册门禁全能力面通用化：工具直调通道（tool-executor.invoke）同源拦截 ──
// 契约：账本 verify_incomplete/verify_failed（遮挡名单现值）内插件的工具，
// 直调通道一并拒绝（用户裁定 2026-10-26：校验没通过 = 整个插件不许上岗）；
// 拒绝带明确原因；复验通过（账本 ok）自动放行，与 LLM 工具面转正语义一致。

#[tokio::test]
async fn tool_executor_invoke_blocks_verify_pending_plugin() {
    // 注册表反查路径：遮挡名单内插件的工具直调被拒，调用不得到达 sidecar。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let lookup: crate::capability_router::ShadowedPluginIdsLookupFn =
        Arc::new(|| vec!["plugin_bash".to_string()]);
    let router = router_with_tool_invoke(captured.clone()).with_shadowed_plugin_ids_lookup(lookup);

    let res = router
        .handle(
            "tool-executor",
            "invoke",
            json!({"tool_name": "bash_execute", "args": {}}),
        )
        .await
        .unwrap();
    assert_eq!(res["success"], false, "必败工具直调必须被拒: {res}");
    let err = res["error"].as_str().unwrap_or_default();
    assert!(
        err.contains("校验未完成或校验失败") && err.contains("已拦截"),
        "拒绝必须带明确原因（校验未完成/校验失败，已拦截）: {err}"
    );
    assert!(err.contains("plugin_bash"), "原因要点名插件: {err}");
    assert!(
        captured.lock().unwrap().is_empty(),
        "被拦调用不得到达 sidecar"
    );
}

#[tokio::test]
async fn tool_executor_invoke_blocks_explicit_plugin_id_path() {
    // 显式 plugin_id 直调路径（系统插件工具不走注册表反查）同拦——同一账本源。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let lookup: crate::capability_router::ShadowedPluginIdsLookupFn =
        Arc::new(|| vec!["plugin_bash".to_string()]);
    let router = router_with_tool_invoke(captured.clone()).with_shadowed_plugin_ids_lookup(lookup);

    let res = router
        .handle(
            "tool-executor",
            "invoke",
            json!({"tool_name": "bash_execute", "plugin_id": "plugin_bash", "args": {}}),
        )
        .await
        .unwrap();
    assert_eq!(res["success"], false, "显式指定被拦插件也必须拒绝: {res}");
    assert!(
        res["error"].as_str().unwrap_or_default().contains("已拦截"),
        "拒绝原因必须显式: {res}"
    );
    assert!(
        captured.lock().unwrap().is_empty(),
        "显式路径被拦调用同样不得到达 sidecar"
    );
}

#[tokio::test]
async fn tool_executor_invoke_reopens_after_ledger_ok() {
    // 转正：复验通过（账本 ok，遮挡名单清除）后同一调用自动放行，无需重启。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let pending = std::sync::Arc::new(std::sync::Mutex::new(vec!["plugin_bash".to_string()]));
    let pending_for_lookup = pending.clone();
    let lookup: crate::capability_router::ShadowedPluginIdsLookupFn =
        Arc::new(move || pending_for_lookup.lock().unwrap().clone());
    let router = router_with_tool_invoke(captured.clone()).with_shadowed_plugin_ids_lookup(lookup);

    let blocked = router
        .handle(
            "tool-executor",
            "invoke",
            json!({"tool_name": "bash_execute", "args": {}}),
        )
        .await
        .unwrap();
    assert_eq!(blocked["success"], false, "待复验期间直调必须被拒");

    pending.lock().unwrap().clear();
    let ok = router
        .handle(
            "tool-executor",
            "invoke",
            json!({"tool_name": "bash_execute", "args": {}}),
        )
        .await
        .unwrap();
    assert_eq!(ok["success"], true, "账本 ok 后直调自动放行: {ok}");
    assert_eq!(captured.lock().unwrap().len(), 1, "放行后真实到达 sidecar");
}

#[tokio::test]
async fn tool_executor_invoke_unaffected_for_healthy_plugin() {
    // 区分度：名单只含他插件时，健康插件工具照常放行（不扩大打击面）。
    let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let lookup: crate::capability_router::ShadowedPluginIdsLookupFn =
        Arc::new(|| vec!["some_other_plugin".to_string()]);
    let router = router_with_tool_invoke(captured.clone()).with_shadowed_plugin_ids_lookup(lookup);

    let res = router
        .handle(
            "tool-executor",
            "invoke",
            json!({"tool_name": "bash_execute", "args": {}}),
        )
        .await
        .unwrap();
    assert_eq!(res["success"], true, "健康插件不受名单影响: {res}");
    assert_eq!(captured.lock().unwrap().len(), 1);
}

// ── event-bus.emit_domain（ADR 2026-08-28 事件下沉底座，用真实内存 SqliteStore 验证端到端）──

pub(super) type DomainSink =
    std::sync::Arc<std::sync::Mutex<Vec<(String, Vec<(String, serde_json::Value)>)>>>;

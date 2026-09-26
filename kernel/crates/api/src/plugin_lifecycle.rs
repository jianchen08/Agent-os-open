//! 插件生命周期公共逻辑（启动期注册 + 运行时新增插件注册）。
//!
//! 把 main 启动期"遍历 manifest 注册 tools/route_signals 到 capability_registry"的逻辑
//! 抽成公共函数，供：
//! - 启动期（agentos-kernel.rs）
//! - 运行时新增插件（reload-all 端点发现新插件后注册）
//!
//! 复用，避免逻辑重复。

use agentos_core::traits::{CapabilityRegistry, PluginManifest, ToolDescriptor};
use agentos_core::types::{ToolCategory, ToolSource};
use agentos_plugin_loader::{CapabilityRegistryImpl, PluginScopeRegistry};
use std::sync::Arc;

/// 把单个插件的 tools 和 route_signals 注册到 capability_registry。
///
/// D.6 槽位拆分（2026-08-15，废止原附录D① 类型门控）：`capabilities.tools`
/// 语义唯一化 = 给 LLM 的工具，**声明即注册**，不再看 plugin_type。
/// 多职能插件（system 适配器声明 tools + services 两块）自然成立；
/// 内部服务方法声明在 `capabilities.services`，不进本注册（调用走
/// invoke_entry / http_endpoints / 显式 plugin_id / provides 通道）。
///
/// M1：经 guarded 注册并把撤销 guard 登记进该插件的 PluginScope
/// （disable/unload 时结构性收回）。
/// 返回注册的 tool 数量。
pub fn register_plugin_capabilities(
    manifest: &PluginManifest,
    registry: &Arc<CapabilityRegistryImpl>,
    scopes: &PluginScopeRegistry,
) -> usize {
    let mut count = 0usize;
    let scope = scopes.scope_of(&manifest.id);
    {
        for tool_cap in &manifest.capabilities.tools {
            let category = tool_cap.category.clone().unwrap_or(ToolCategory::System);
            // K9 + 强制规则：input_schema 缺失时的行为按宿主分档——
            //   - external MCP 工具（entry="mcp:external"）：**拒注册**。此类工具
            //     的 manifest 声明是 LLM 工具面 input_schema 的唯一真值源（G2 只
            //     比对、不回填握手 schema），缺声明 = 注册出 {} = LLM 收到零参数
            //     工具，调用必因缺参被服务端校验拒绝；拒注册直接暴露问题，
            //     不给"盲调工具"留后门。
            //   - 内置/sidecar 自研（http.handle、*.status 哨兵等零参工具）：
            //     零参合法（HTTP 处理器/无参状态查询），维持 {} 补注册，
            //     仅 warn（K9 既有行为）。
            let is_external_mcp = manifest.entry == "mcp:external";
            if tool_cap.input_schema.is_none() {
                if is_external_mcp {
                    tracing::error!(
                        target: "plugin-registration",
                        plugin_id = %manifest.id,
                        tool = %tool_cap.name,
                        "external MCP 工具缺 input_schema，拒绝注册（LLM 工具面唯一真值源缺失，注册即零参数盲调；请按 MCP tools/list inputSchema 补声明）"
                    );
                    continue;
                }
                tracing::warn!(
                    target: "plugin-registration",
                    plugin_id = %manifest.id,
                    tool = %tool_cap.name,
                    "tool manifest 缺 input_schema，以 {{}} 补注册（LLM 侧 object 过滤恒不触发，LLM 只能盲调；请补声明）"
                );
            }
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
                ui: tool_cap.ui.clone(),
                render: tool_cap.render.clone(),
            };
            scope.track(registry.register_tool_guarded(&manifest.id, descriptor));
            count += 1;
        }
    }

    // 注册路由信号
    if !manifest.capabilities.route_signals.is_empty() {
        scope.track(registry.register_route_signals_guarded(
            &manifest.id,
            manifest.capabilities.route_signals.clone(),
        ));
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
    registry: &Arc<CapabilityRegistryImpl>,
    scopes: &PluginScopeRegistry,
) -> (Vec<String>, usize) {
    let mut new_ids = Vec::new();
    let mut total_tools = 0usize;
    for manifest in all_manifests {
        if existing_ids.contains(&manifest.id) {
            continue;
        }
        total_tools += register_plugin_capabilities(manifest, registry, scopes);
        new_ids.push(manifest.id.clone());
    }
    (new_ids, total_tools)
}

/// 插件重新启用后立即重注册其能力（G1 enable 对称化）。
///
/// 禁用路径 `clear_plugin` 按插件清四维；启用侧无需重启——`/ext/{*rest}`
/// 通配分发是注册表数据驱动，路由树无需重建。
/// 本函数补齐对称：tools + route_signals（经 [`register_plugin_capabilities`]）+
/// http_endpoints（对齐 watcher `apply_discovered_plugins` 的补注册段）。
///
/// 幂等：对已启用插件重复调用，tools 覆盖同名、http 路由同 path+method 冲突时忽略。
/// M1：先 revoke 该插件旧 scope（清残留 guard）再 guarded 重注册。
/// 返回 (注册的 tool 数, 注册的 http 路由数)。
pub fn reenable_plugin_capabilities(
    manifest: &PluginManifest,
    registry: &Arc<CapabilityRegistryImpl>,
    scopes: &PluginScopeRegistry,
) -> (usize, usize) {
    // 幂等基线：先收回旧 scope 的全部登记（禁用遗留或重复启用），
    // 再重注册拿全新 guard——避免 by_plugin 索引重复条目。
    scopes.revoke(&manifest.id);
    let tools = register_plugin_capabilities(manifest, registry, scopes);
    let scope = scopes.scope_of(&manifest.id);
    let mut http_routes = 0usize;
    for ep in &manifest.http_endpoints {
        let ok = registry
            .register_http_route_guarded(&manifest.id, ep.clone())
            .map(|(_d, guard)| scope.track(guard))
            .is_ok();
        if ok {
            http_routes += 1;
        }
    }
    (tools, http_routes)
}

/// 调用路径注册表自愈闭包工厂（BUG-37）。
///
/// 背景：注册表条目可能因 G2 启动期观测误剔（spawn 后进程早退 → tools/list
/// 缺失 → 误判漂移剔除）等注册面丢失而缺席；watcher 对无文件变更的插件不重扫
/// 不重注册 → 冷门工具自此永久"未注册"。
///
/// 语义：反查 miss 时按本闭包自愈——工具的**声明 manifest 在共享 store 且插件
/// 在启用集**才重注册（reenable 原语：收回残留 scope + guarded 重注册全部声明
/// 能力，与 enable 热路径/GAP-6 复验同一通道）；manifest 缺席或插件已禁用返回
/// None，调用方保持 fail-closed（禁用插件的摘除语义不被本自愈复活）。有界：
/// 每次反查 miss 至多触发一次；重注册成功后反查直接命中，不再进入本通道。
/// G2 若因实现真实漂移而剔除，下次声明/代码指纹变更时 GAP-6 复验照常重新
/// 净化——本自愈只恢复可用性，不改变复验裁决。
pub fn tool_registry_heal_fn(
    registry: Arc<CapabilityRegistryImpl>,
    scopes: Arc<PluginScopeRegistry>,
    manifests: crate::plugin_watcher::ManifestsStore,
    enabled_ids: Arc<tokio::sync::RwLock<std::collections::HashSet<String>>>,
) -> crate::capability_router::ToolRegistryHealFn {
    Arc::new(move |tool_name: &str| {
        let registry = Arc::clone(&registry);
        let scopes = Arc::clone(&scopes);
        let manifests = Arc::clone(&manifests);
        let enabled_ids = Arc::clone(&enabled_ids);
        Box::pin(async move {
            use agentos_core::traits::CapabilityRegistry;
            // 幂等基线：条目已在（并发下被 watcher/enable 路径恢复）→ 直接复用。
            if let Some(td) = registry.get_tool(tool_name) {
                return Some(td);
            }
            // manifest 在册且插件启用才自愈；二者缺一 = 无可自愈（fail-closed）。
            let enabled = enabled_ids.read().await.clone();
            let manifest = {
                let store = manifests.read().await;
                store
                    .iter()
                    .find(|m| {
                        enabled.contains(&m.id)
                            && m.capabilities.tools.iter().any(|t| t.name == tool_name)
                    })
                    .cloned()
            };
            let manifest = manifest?;
            let (tools, _) = reenable_plugin_capabilities(&manifest, &registry, &scopes);
            tracing::info!(
                target: "plugin-registration",
                plugin = %manifest.id,
                tool = %tool_name,
                tools,
                "调用路径自愈：注册表缺失的声明工具已按 manifest 重注册"
            );
            registry.get_tool(tool_name)
        })
    })
}

/// /ext 调用路径 HTTP 路由自愈（BUG-58 立通道；BUG-67 改判 **additive 语义**）。
///
/// 声明面在册（manifest 在共享 store）且插件在启用集、但注册表查不到请求路由
/// 时，按声明 manifest **逐条补注册缺失的 http 路由**。只增不毁：
///
/// - **不收回既有 scope**（BUG-67 裁决：原实现走 reenable 全量 revoke + 重建，
///   在请求路径上执行时与并发的 sibling 自愈 / watcher 复验互相绞杀——前一个
///   刚恢复的路由被后一个的 revoke 摘除，同一请求自愈后重试仍 miss → 503
///   持续，装机版 R239/R244 实证；且 revoke 连带摧毁 scope 里非 http 维度的
///   在册登记（模式包资源键 / widget 绑定），reenable 并不重建它们）。工具
///   缺席有 BUG-37 工具自愈通道、禁用摘除有 disable 语义，都不归本自愈处置。
/// - **幂等**：已在册的声明端点跳过；并发同键注册的冲突按「竞胜方条目已在」
///   计成功（additive 语义下冲突只可能来自并发补注册同一路由）。
///
/// 不动作（返回 false，fail-closed 语义不被本自愈复活）：manifest 缺席、插件
/// 已禁用、或声明无 http_endpoints。返回是否恢复了 http 路由，调用方据此重试
/// 当次请求。
pub async fn heal_plugin_http_routes(
    registry: &Arc<CapabilityRegistryImpl>,
    scopes: &PluginScopeRegistry,
    manifests: &crate::plugin_watcher::ManifestsStore,
    enabled_ids: &Arc<tokio::sync::RwLock<std::collections::HashSet<String>>>,
    plugin_id: &str,
) -> bool {
    if !enabled_ids.read().await.contains(plugin_id) {
        return false;
    }
    let manifest = {
        let store = manifests.read().await;
        store.iter().find(|m| m.id == plugin_id).cloned()
    };
    let Some(manifest) = manifest else {
        return false;
    };
    if manifest.http_endpoints.is_empty() {
        return false;
    }
    let scope = scopes.scope_of(plugin_id);
    let mut http_routes = 0usize;
    for ep in &manifest.http_endpoints {
        // 幂等基线：端点已在（并发自愈/watcher 先行恢复）→ 计成功，不动。
        if registry
            .find_http_route(&ep.path, &ep.method)
            .is_some_and(|d| d.plugin_id == manifest.id)
        {
            http_routes += 1;
            continue;
        }
        let ok = registry
            .register_http_route_guarded(plugin_id, ep.clone())
            .map(|(_d, guard)| scope.track(guard))
            .or_else(|_| {
                // 注册冲突 = 并发补注册同键竞胜；复核条目确属本插件 → 计成功。
                if registry
                    .find_http_route(&ep.path, &ep.method)
                    .is_some_and(|d| d.plugin_id == manifest.id)
                {
                    Ok(())
                } else {
                    Err(())
                }
            })
            .is_ok();
        if ok {
            http_routes += 1;
        }
    }
    tracing::info!(
        target: "plugin-registration",
        plugin = %manifest.id,
        http_routes,
        "调用路径自愈：注册表缺失的声明 http 路由已按 manifest 补注册（additive，不收回在册面）"
    );
    http_routes > 0
}

/// 广播域事件（通用域事件通道：`LifecycleHook::DomainEvent` + `ctx["event"]` 事件名）。
///
/// 两条投递路径：
/// - **观察总线**（audit/metrics 订阅者）：经 [`agentos_hooks::global`] 广播，
///   best-effort 非阻塞；总线未注册（测试/降级）时静默跳过。
/// - **点对点**：manifest `capabilities.lifecycle_hooks` 声明了 `domain_event`
///   且已启用的插件，经 `send_lifecycle_hook` 收到 `notifications/domain_event`
///   （sidecar 侧 SDK 自动分发到 `on_domain_event` 处理器）。fire-and-forget
///   spawn——通知失败只告警、绝不阻塞调用方主流程；副作用是声明订阅的
///   sidecar 可能因通知被懒 spawn，这是订阅的既定代价。
///
/// 发射点：会话创建/删除（session_routes）、活跃会话切换（ws_session）。
pub async fn broadcast_domain_event(
    state: &crate::routes::AppState,
    name: &str,
    tags: Vec<(String, serde_json::Value)>,
) {
    let Some(invoker) = state.invoker.clone() else {
        return;
    };
    let enabled = state.enabled_plugin_ids.clone();
    let manifests = state.manifests.clone();
    broadcast_domain_event_from(&invoker, &enabled, &manifests, name, tags).await;
}

/// 域事件投递（broadcast_domain_event 的组件版——供不持 AppState 的调用方
/// 复用，如 capability_router 的域事件广播闭包）。双通道：观察总线 +
/// 点对点推给声明 domain_event 的启用插件。
pub async fn broadcast_domain_event_from(
    invoker: &Arc<dyn agentos_core::traits::PluginInvoker>,
    enabled: &tokio::sync::RwLock<std::collections::HashSet<String>>,
    manifests: &tokio::sync::RwLock<Vec<agentos_core::traits::PluginManifest>>,
    name: &str,
    tags: Vec<(String, serde_json::Value)>,
) {
    if let Some(bus) = agentos_hooks::global() {
        bus.emit(agentos_hooks::domain_event(name, tags.clone()));
    }
    let enabled = enabled.read().await;
    let manifests = manifests.read().await;
    // 宿主去重（BUG-7）：合宿进程的生命周期通知是宿主级广播——聚合服务端把
    // 每条通知扇出给全部声明成员的 handler（P27 ADR 广播语义）。按订阅插件
    // 逐发会让同宿主 N 个声明成员各收到 N 次通知、每次扇出全成员，handler
    // 执行 N² 次（子任务完成通知 ×4 根因：task_service 重复派生 + trigger
    // 注入翻倍）。同宿主只发第一次，扇出保证其余成员各执行一次；独占宿主
    // 键互异不受影响。
    let mut notified_hosts: std::collections::HashSet<String> = std::collections::HashSet::new();
    for manifest in manifests.iter() {
        if !enabled.contains(&manifest.id) {
            continue;
        }
        if !manifest
            .capabilities
            .lifecycle_hooks
            .contains(&agentos_core::traits::LifecycleHook::DomainEvent)
        {
            continue;
        }
        if !notified_hosts.insert(invoker.host_key_of(&manifest.id)) {
            continue;
        }
        let mut ctx = agentos_core::traits::HookContext::new();
        ctx.set("event", serde_json::json!(name));
        for (key, value) in tags.clone() {
            ctx.set(key.as_str(), value);
        }
        let plugin_id = manifest.id.clone();
        let event_name = name.to_string();
        let inv = invoker.clone();
        tokio::spawn(async move {
            if let Err(e) = inv
                .send_lifecycle_hook(
                    &plugin_id,
                    agentos_core::traits::LifecycleHook::DomainEvent,
                    &ctx,
                )
                .await
            {
                tracing::warn!(
                    plugin = %plugin_id,
                    event = %event_name,
                    error = %e.message,
                    "域事件点对点通知失败"
                );
            }
        });
    }
}

#[cfg(test)]
mod domain_event_tests {
    use super::{broadcast_domain_event, broadcast_domain_event_from};
    use crate::routes::AppState;
    use agentos_core::traits::{
        HookContext, HostType, LifecycleHook, ManifestCapabilities, PluginInvoker, PluginManifest,
        PluginType,
    };
    use agentos_core::types::{PluginContext, PluginError, PluginResult, ToolExecutionResult};
    use serde_json::json;
    use std::collections::HashSet;
    use std::sync::{Arc, Mutex};

    /// 记录 send_lifecycle_hook 调用的 mock invoker（invoke_* 本测试不可达）。
    /// `hosts` 编排 plugin_id → 宿主键分组（合宿去重用例）；未登记按独占语义
    /// 回退 plugin_id 本身（与 trait 默认实现同构）。
    struct RecordingInvoker {
        hooks: Mutex<Vec<(String, String)>>, // (plugin_id, event)
        hosts: std::collections::HashMap<String, String>,
    }

    #[async_trait::async_trait]
    impl PluginInvoker for RecordingInvoker {
        async fn invoke_pipeline_plugin<'a>(
            &self,
            _plugin_id: &str,
            _ctx: &PluginContext<'a>,
        ) -> Result<PluginResult, PluginError> {
            unimplemented!("域事件测试不触达")
        }
        async fn invoke_tool(
            &self,
            _plugin_id: &str,
            _tool_name: &str,
            _inputs: &serde_json::Value,
        ) -> Result<ToolExecutionResult, PluginError> {
            unimplemented!("域事件测试不触达")
        }
        async fn send_lifecycle_hook(
            &self,
            plugin_id: &str,
            hook: LifecycleHook,
            context: &HookContext,
        ) -> Result<(), PluginError> {
            assert_eq!(hook, LifecycleHook::DomainEvent);
            let event = context
                .get("event")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            self.hooks
                .lock()
                .unwrap()
                .push((plugin_id.to_string(), event));
            Ok(())
        }
        fn host_key_of(&self, plugin_id: &str) -> String {
            self.hosts
                .get(plugin_id)
                .cloned()
                .unwrap_or_else(|| plugin_id.to_string())
        }
    }

    fn manifest(id: &str, declare_domain: bool) -> PluginManifest {
        PluginManifest {
            force_include_tools: Vec::new(),
            state: None,
            id: id.to_string(),
            name: id.to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::System,
            pipeline_role: None,
            language: "python".to_string(),
            host_type: HostType::Sidecar,
            host_group: None,
            entry: "python server.py".to_string(),
            capabilities: ManifestCapabilities {
                lifecycle_hooks: if declare_domain {
                    vec![LifecycleHook::DomainEvent]
                } else {
                    vec![]
                },
                ..Default::default()
            },
            requires_services: vec![],
            permissions: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            granted_capabilities: vec![],
            restricted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            persistent_fields: vec![],
            export_fields: vec![],
            provides: None,
        }
    }

    #[tokio::test]
    async fn domain_event_reaches_declaring_enabled_plugins_only() {
        let invoker = Arc::new(RecordingInvoker {
            hooks: Mutex::new(Vec::new()),
            hosts: std::collections::HashMap::new(),
        });
        let mut state = AppState::new();
        // A：声明 + 启用 → 收到；B：未声明 → 不收到；C：声明但禁用 → 不收到
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
            manifest("p_a", true),
            manifest("p_b", false),
            manifest("p_c", true),
        ]));
        state.enabled_plugin_ids = Arc::new(tokio::sync::RwLock::new(HashSet::from([
            "p_a".to_string(),
            "p_b".to_string(),
        ])));
        state.invoker = Some(invoker.clone());

        broadcast_domain_event(
            &state,
            "session.created",
            vec![
                ("session_id".to_string(), json!("t1")),
                ("pipeline_id".to_string(), json!("pipe1")),
            ],
        )
        .await;

        // 点对点是 fire-and-forget spawn：轮询等任务落地
        for _ in 0..200 {
            if !invoker.hooks.lock().unwrap().is_empty() {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        let got = invoker.hooks.lock().unwrap().clone();
        assert_eq!(
            got,
            vec![("p_a".to_string(), "session.created".to_string())],
            "只投递给声明了 domain_event 且启用的插件"
        );
    }

    /// BUG-7 回归：同一合宿宿主内的多个订阅插件只投递一次。
    ///
    /// 合宿聚合服务端把每条生命周期通知扇出给全部声明成员的 handler（广播
    /// 语义，P27 ADR），内核按订阅插件逐发会让每个成员 handler 执行 N 次
    ///（子任务完成通知 ×4 = 2 次派生 × 每次扇出 ×2 的根因）。
    #[tokio::test]
    async fn domain_event_delivered_once_per_host_for_cohosted_subscribers() {
        let invoker = Arc::new(RecordingInvoker {
            hooks: Mutex::new(Vec::new()),
            hosts: std::collections::HashMap::from([
                ("p_a".to_string(), "group:light:1".to_string()),
                ("p_c".to_string(), "group:light:1".to_string()),
            ]),
        });
        let mut state = AppState::new();
        // p_a / p_c 同宿主且都声明 domain_event；p_b 未声明不订阅。
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
            manifest("p_a", true),
            manifest("p_b", false),
            manifest("p_c", true),
        ]));
        state.enabled_plugin_ids = Arc::new(tokio::sync::RwLock::new(HashSet::from([
            "p_a".to_string(),
            "p_c".to_string(),
        ])));
        state.invoker = Some(invoker.clone());

        broadcast_domain_event(&state, "task_completed", vec![]).await;

        for _ in 0..200 {
            if !invoker.hooks.lock().unwrap().is_empty() {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        let got = invoker.hooks.lock().unwrap().clone();
        assert_eq!(
            got,
            vec![("p_a".to_string(), "task_completed".to_string())],
            "同宿主订阅插件只投递一次（宿主扇出保证其余成员各收到一次）"
        );
    }

    /// 去重不得欠投递：不同宿主的订阅插件各自收到一次。
    #[tokio::test]
    async fn domain_event_still_reaches_subscribers_in_distinct_hosts() {
        let invoker = Arc::new(RecordingInvoker {
            hooks: Mutex::new(Vec::new()),
            hosts: std::collections::HashMap::from([
                ("p_a".to_string(), "group:light:1".to_string()),
                ("p_c".to_string(), "group:light:2".to_string()),
            ]),
        });
        let mut state = AppState::new();
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
            manifest("p_a", true),
            manifest("p_c", true),
        ]));
        state.enabled_plugin_ids = Arc::new(tokio::sync::RwLock::new(HashSet::from([
            "p_a".to_string(),
            "p_c".to_string(),
        ])));
        state.invoker = Some(invoker.clone());

        broadcast_domain_event(&state, "task_completed", vec![]).await;

        let mut got = Vec::new();
        for _ in 0..200 {
            got = invoker.hooks.lock().unwrap().clone();
            if got.len() >= 2 {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        got.sort();
        assert_eq!(
            got,
            vec![
                ("p_a".to_string(), "task_completed".to_string()),
                ("p_c".to_string(), "task_completed".to_string()),
            ],
            "异宿主订阅插件各投递一次，去重不得吞投递"
        );
    }

    /// 不覆写 `host_key_of` 的裸 mock：钉住 trait 默认实现 = 独占逐发语义
    /// （无宿主分组知识的 invoker 实现行为不变，键互异不去重）。
    struct SoloDefaultInvoker {
        hooks: Mutex<Vec<String>>,
    }

    #[async_trait::async_trait]
    impl PluginInvoker for SoloDefaultInvoker {
        async fn invoke_pipeline_plugin<'a>(
            &self,
            _plugin_id: &str,
            _ctx: &PluginContext<'a>,
        ) -> Result<PluginResult, PluginError> {
            unimplemented!("域事件测试不触达")
        }
        async fn invoke_tool(
            &self,
            _plugin_id: &str,
            _tool_name: &str,
            _inputs: &serde_json::Value,
        ) -> Result<ToolExecutionResult, PluginError> {
            unimplemented!("域事件测试不触达")
        }
        async fn send_lifecycle_hook(
            &self,
            plugin_id: &str,
            _hook: LifecycleHook,
            _context: &HookContext,
        ) -> Result<(), PluginError> {
            self.hooks.lock().unwrap().push(plugin_id.to_string());
            Ok(())
        }
    }

    #[tokio::test]
    async fn default_host_key_keeps_per_plugin_delivery() {
        let invoker = Arc::new(SoloDefaultInvoker {
            hooks: Mutex::new(Vec::new()),
        });
        let mut state = AppState::new();
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![
            manifest("p_a", true),
            manifest("p_c", true),
        ]));
        state.enabled_plugin_ids = Arc::new(tokio::sync::RwLock::new(HashSet::from([
            "p_a".to_string(),
            "p_c".to_string(),
        ])));
        state.invoker = Some(invoker.clone());

        broadcast_domain_event(&state, "task_completed", vec![]).await;

        let mut got = Vec::new();
        for _ in 0..200 {
            got = invoker.hooks.lock().unwrap().clone();
            if got.len() >= 2 {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        got.sort();
        assert_eq!(
            got,
            vec!["p_a".to_string(), "p_c".to_string()],
            "默认独占键语义下每个订阅插件各投递一次"
        );
    }

    /// 观察总线（`agentos_hooks::global`）注册后 emit 真到达订阅者——
    /// 双通道之观察面的端到端落地。
    #[tokio::test]
    async fn domain_event_emits_to_observation_bus_when_registered() {
        use agentos_hooks::HookEventBus;
        // 进程级单例：同进程重复 set_global 静默忽略，故先 set 再取 global。
        let bus = std::sync::Arc::new(HookEventBus::new(64));
        agentos_hooks::set_global(bus);
        let mut rx = agentos_hooks::global()
            .expect("set_global 后 global 必有值")
            .subscribe();

        let invoker = Arc::new(RecordingInvoker {
            hooks: Mutex::new(Vec::new()),
            hosts: std::collections::HashMap::new(),
        });
        let mut state = AppState::new();
        state.manifests = Arc::new(tokio::sync::RwLock::new(vec![manifest("p_a", true)]));
        state.enabled_plugin_ids =
            Arc::new(tokio::sync::RwLock::new(HashSet::from(["p_a".to_string()])));
        state.invoker = Some(invoker);

        broadcast_domain_event(&state, "cov.probe.event", vec![]).await;

        // 总线是进程级单例，同进程其他用例可能并发 emit：按专用事件名过滤，
        // 不得把旁路事件当成用例证据。
        let ev = tokio::time::timeout(std::time::Duration::from_secs(2), async {
            loop {
                let ev = rx.recv().await.expect("通道未关闭");
                if ev.ctx.get("event").and_then(|v| v.as_str()) == Some("cov.probe.event") {
                    return ev;
                }
            }
        })
        .await
        .expect("总线应在 2s 内投递域事件");
        assert_eq!(ev.hook, LifecycleHook::DomainEvent);
    }

    /// `broadcast_domain_event`：invoker 未注入 → 直接返回（不 panic、无副作用）。
    #[tokio::test]
    async fn broadcast_without_invoker_is_noop() {
        let state = AppState::new();
        broadcast_domain_event(&state, "session.created", vec![]).await;
    }

    /// `broadcast_domain_event_from`：点对点通知失败只告警不阻断广播
    /// （fire-and-forget 语义），函数照常返回。
    #[tokio::test]
    async fn domain_event_delivery_failure_does_not_block_others() {
        struct FailingInvoker;
        #[async_trait::async_trait]
        impl PluginInvoker for FailingInvoker {
            async fn invoke_pipeline_plugin<'a>(
                &self,
                _plugin_id: &str,
                _ctx: &PluginContext<'a>,
            ) -> Result<PluginResult, PluginError> {
                unreachable!("不触达")
            }
            async fn invoke_tool(
                &self,
                _plugin_id: &str,
                _tool_name: &str,
                _inputs: &serde_json::Value,
            ) -> Result<ToolExecutionResult, PluginError> {
                unreachable!("不触达")
            }
            async fn send_lifecycle_hook(
                &self,
                _plugin_id: &str,
                hook: LifecycleHook,
                _context: &HookContext,
            ) -> Result<(), PluginError> {
                assert_eq!(hook, LifecycleHook::DomainEvent);
                Err(PluginError {
                    message: "host gone".to_string(),
                    code: None,
                    source: None,
                })
            }
        }

        let invoker: Arc<dyn PluginInvoker> = Arc::new(FailingInvoker);
        let enabled = tokio::sync::RwLock::new(HashSet::from(["p_a".to_string()]));
        let manifests = tokio::sync::RwLock::new(vec![manifest("p_a", true)]);

        broadcast_domain_event_from(
            &invoker,
            &enabled,
            &manifests,
            "session.created",
            vec![("session_id".to_string(), json!("t1"))],
        )
        .await;
        // fire-and-forget：给 spawn 的通知任务一点时间跑完失败分支
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
}

#[cfg(test)]
mod external_mcp_schema_gate_tests {
    use super::*;
    use agentos_core::traits::{
        CapabilityRegistry, HostType, ManifestCapabilities, PluginManifest, PluginType,
        ToolCapability,
    };
    use agentos_core::types::ToolCategory;

    fn manifest_with(entry: &str, tool_input_schema: Option<serde_json::Value>) -> PluginManifest {
        PluginManifest {
            force_include_tools: Vec::new(),
            state: None,
            id: "p_gate".to_string(),
            name: "p_gate".to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::Tool,
            pipeline_role: None,
            language: "external".to_string(),
            host_type: HostType::Sidecar,
            host_group: None,
            entry: entry.to_string(),
            capabilities: ManifestCapabilities {
                tools: vec![ToolCapability {
                    name: "ext_tool".to_string(),
                    description: Some("desc".to_string()),
                    input_schema: tool_input_schema,
                    output_schema: None,
                    smoke: None,
                    timeout_ms: None,
                    category: Some(ToolCategory::Search),
                    ui: None,
                    render: None,
                }],
                ..Default::default()
            },
            requires_services: vec![],
            permissions: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            granted_capabilities: vec![],
            restricted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            persistent_fields: vec![],
            export_fields: vec![],
            provides: None,
        }
    }

    #[test]
    fn external_mcp_tool_without_input_schema_rejected() {
        // external MCP 工具缺 input_schema → 拒注册。manifest 声明是 LLM 工具面
        // input_schema 的唯一真值源（G2 只比对不回填），缺声明即零参数工具、
        // 调用必因缺参被服务端校验拒绝。
        let m = manifest_with("mcp:external", None);
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let scopes = agentos_plugin_loader::PluginScopeRegistry::new();
        let n = register_plugin_capabilities(&m, &registry, &scopes);
        assert_eq!(n, 0, "external MCP 缺 schema 必须拒注册");
        assert!(
            registry.list_tools().iter().all(|t| t.plugin_id != m.id),
            "被拒工具不得出现在能力注册表"
        );
    }

    #[test]
    fn external_mcp_tool_with_input_schema_registered() {
        let m = manifest_with(
            "mcp:external",
            Some(serde_json::json!({
                "type": "object",
                "properties": {"mode": {"type": "string"}},
                "required": ["mode"],
            })),
        );
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let scopes = agentos_plugin_loader::PluginScopeRegistry::new();
        let n = register_plugin_capabilities(&m, &registry, &scopes);
        assert_eq!(n, 1, "带 schema 的 external MCP 工具正常注册");
        let tools = registry.list_tools();
        let t = tools.iter().find(|t| t.plugin_id == m.id).expect("应注册");
        assert_eq!(t.input_schema["required"][0], "mode");
    }

    #[test]
    fn sidecar_builtin_tool_without_input_schema_still_registered() {
        // 内置 sidecar 哨兵（http.handle / *.status 等零参工具）零参
        // 合法——维持 {} 补注册（K9 既有行为），不被新规则误伤
        let mut m2 = manifest_with("plugin:main", None);
        m2.plugin_type = PluginType::System;
        m2.language = "python".to_string();
        m2.capabilities.tools[0].name = "http.handle".to_string();
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let scopes = agentos_plugin_loader::PluginScopeRegistry::new();
        let n = register_plugin_capabilities(&m2, &registry, &scopes);
        assert_eq!(n, 1, "内置工具缺 schema 仍补注册");
        let tools = registry.list_tools();
        let t = tools.iter().find(|t| t.plugin_id == m2.id).expect("应注册");
        assert!(t.input_schema.is_object() && t.input_schema.as_object().unwrap().is_empty());
    }
}

/// 能力注册面补测（guarded 注册 / 批量注册幂等 / 重启用对称化）。
///
/// manifest 走 serde 构造：字段众多，字面量构造会在每次加字段时编译断裂；
/// JSON 形态与 plugin.json 真值源同构。
#[cfg(test)]
mod capability_registration_tests {
    use super::*;
    use agentos_core::traits::{CapabilityRegistry, HttpEndpoint};

    fn manifest(id: &str, tool: &str) -> PluginManifest {
        serde_json::from_value(serde_json::json!({
            "id": id,
            "name": id,
            "version": "1.0.0",
            "language": "python",
            "host_type": "sidecar",
            "entry": "python server.py",
            "plugin_type": "tool",
            "capabilities": {
                "tools": [{
                    "name": tool,
                    "description": "d",
                    "input_schema": {"type": "object"}
                }]
            }
        }))
        .expect("测试 manifest 应可反序列化")
    }

    fn endpoint(path: &str) -> HttpEndpoint {
        HttpEndpoint {
            route_id: "r".to_string(),
            method: "GET".to_string(),
            path: path.to_string(),
            auth: Some("none".to_string()),
            handler_capability: "http.handle".to_string(),
            timeout_ms: None,
            max_concurrency: None,
            description: None,
        }
    }

    /// 声明 route_signals：工具与信号一并登记，且都入该插件 scope；
    /// scope 收回（disable 语义）后注册表零残留。
    #[test]
    fn route_signals_registered_and_tracked_in_scope() {
        use agentos_core::types::RouteType;
        let mut m = manifest("p_sig", "t_sig");
        m.capabilities.route_signals = vec![RouteType::NextLlm, RouteType::NextTool];
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let scopes = agentos_plugin_loader::PluginScopeRegistry::new();

        let n = register_plugin_capabilities(&m, &registry, &scopes);
        assert_eq!(n, 1, "工具数与信号数互不影响");
        assert!(registry.get_tool("t_sig").is_some(), "工具应可查");
        assert_eq!(
            scopes.scope_of(&m.id).len(),
            2,
            "tool + route_signals 两条 guard 都应入账本"
        );

        scopes.revoke(&m.id);
        assert!(
            registry.get_tool("t_sig").is_none(),
            "revoke 后工具必须从注册表消失（结构性收回）"
        );
    }

    /// 无 route_signals 声明：不产生信号 guard（scope 只 1 条工具 guard）。
    #[test]
    fn empty_route_signals_adds_no_guard() {
        let m = manifest("p_nosig", "t_nosig");
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let scopes = agentos_plugin_loader::PluginScopeRegistry::new();
        let n = register_plugin_capabilities(&m, &registry, &scopes);
        assert_eq!(n, 1, "工具照常注册");
        assert_eq!(scopes.scope_of(&m.id).len(), 1, "只有工具 guard");
    }

    /// `register_new_plugins`：existing_ids 命中跳过（幂等），只注册新增。
    #[test]
    fn register_new_plugins_skips_known_ids() {
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let scopes = agentos_plugin_loader::PluginScopeRegistry::new();

        let existing: std::collections::HashSet<String> = ["p_a".to_string()].into_iter().collect();
        let (new_ids, tools) = register_new_plugins(
            &[manifest("p_a", "t_a"), manifest("p_b", "t_b")],
            &existing,
            &registry,
            &scopes,
        );
        assert_eq!(new_ids, vec!["p_b".to_string()], "已注册的 p_a 必须跳过");
        assert_eq!(tools, 1, "只注册 p_b 的 1 个工具");
        assert!(registry.get_tool("t_a").is_none(), "跳过的插件不得注册工具");
        assert!(registry.get_tool("t_b").is_some());

        let all: std::collections::HashSet<String> =
            ["p_a".to_string(), "p_b".to_string()].into_iter().collect();
        let (again, more_tools) = register_new_plugins(
            &[manifest("p_a", "t_a"), manifest("p_b", "t_b")],
            &all,
            &registry,
            &scopes,
        );
        assert!(again.is_empty() && more_tools == 0, "幂等：无新增");
    }

    /// `reenable_plugin_capabilities`：先 revoke 旧 scope 再重注册（不累积
    /// guard），http_endpoints 一并重注册并计数。
    #[test]
    fn reenable_revokes_old_scope_and_reregisters_http_routes() {
        use agentos_core::types::RouteType;
        let mut m = manifest("p_re", "t_re");
        m.capabilities.route_signals = vec![RouteType::NextLlm];
        m.http_endpoints = vec![endpoint("/ext/p_re/ping")];
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let scopes = agentos_plugin_loader::PluginScopeRegistry::new();

        let (tools, routes) = reenable_plugin_capabilities(&m, &registry, &scopes);
        assert_eq!(tools, 1);
        assert_eq!(routes, 1, "声明的 http 路由应重注册并计数");
        assert_eq!(
            scopes.scope_of(&m.id).len(),
            3,
            "tool + signal + http 三条 guard"
        );
        assert!(
            registry.find_http_route("/ext/p_re/ping", "GET").is_some(),
            "重启用后路由应可分发命中"
        );

        let (tools2, routes2) = reenable_plugin_capabilities(&m, &registry, &scopes);
        assert_eq!((tools2, routes2), (1, 1));
        assert_eq!(
            scopes.scope_of(&m.id).len(),
            3,
            "重复启用不得累积 guard：{}",
            scopes.scope_of(&m.id).len()
        );
    }

    /// http_endpoints 冲突（同 path+method 已被占位）→ 忽略该条，不阻断工具注册。
    #[test]
    fn reenable_ignores_conflicting_http_route() {
        let mut m = manifest("p_conf", "t_conf");
        m.http_endpoints = vec![endpoint("/ext/p_conf/dup")];
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let scopes = agentos_plugin_loader::PluginScopeRegistry::new();
        // 占位注册不归本插件 scope 管（disarm 后残留），reenable 的 revoke 收不回它。
        let (_, guard) = registry
            .register_http_route_guarded(&m.id, m.http_endpoints[0].clone())
            .expect("首个注册成功");
        guard.disarm();

        let (tools, routes) = reenable_plugin_capabilities(&m, &registry, &scopes);
        assert_eq!(tools, 1, "工具照常注册");
        assert_eq!(routes, 0, "冲突路由被忽略（不报错、不阻断）");
        assert!(
            registry.get_tool("t_conf").is_some(),
            "工具注册不受路由冲突影响"
        );
        assert!(
            registry.find_http_route("/ext/p_conf/dup", "GET").is_some(),
            "占位路由保持原状"
        );
    }

    /// 调用路径自愈工厂（BUG-37）：声明工具在册且插件启用 → 重注册并返回
    /// 描述符；manifest 缺席 / 插件禁用 → None（fail-closed，不复活禁用插件）。
    #[tokio::test]
    async fn heal_fn_reregisters_declared_tool_only_for_enabled_plugin() {
        let m = manifest("p_heal", "t_heal");
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let scopes = Arc::new(agentos_plugin_loader::PluginScopeRegistry::new());
        let manifests: crate::plugin_watcher::ManifestsStore =
            Arc::new(tokio::sync::RwLock::new(vec![m.clone()]));
        let enabled_ids = Arc::new(tokio::sync::RwLock::new(std::collections::HashSet::from([
            "p_heal".to_string(),
        ])));
        let heal = tool_registry_heal_fn(
            Arc::clone(&registry),
            Arc::clone(&scopes),
            Arc::clone(&manifests),
            Arc::clone(&enabled_ids),
        );

        // 启用插件 + 声明在册 → 重注册成功，注册表可反查。
        let healed = heal("t_heal").await.expect("启用插件的声明工具应自愈");
        assert_eq!(healed.plugin_id, "p_heal");
        assert!(registry.get_tool("t_heal").is_some(), "自愈应落注册表");

        // 幂等：条目已在 → 直接返回，不再重复重注册（scope guard 数不涨）。
        let guards_before = scopes.scope_of("p_heal").len();
        assert!(heal("t_heal").await.is_some());
        assert_eq!(
            scopes.scope_of("p_heal").len(),
            guards_before,
            "幂等自愈不得累积 guard"
        );

        // 插件禁用（不在启用集）→ 不复活（fail-closed）。
        registry.clear_plugin("p_heal");
        enabled_ids.write().await.remove("p_heal");
        assert!(heal("t_heal").await.is_none(), "禁用插件不得经自愈复活");
        assert!(registry.get_tool("t_heal").is_none());

        // manifest 缺席的工具 → None。
        enabled_ids.write().await.insert("p_heal".to_string());
        assert!(heal("ghost_tool").await.is_none(), "未知工具不应自愈");
    }

    // ── BUG-67：调用路径自愈必须 additive（只补缺失路由，不收回在册面）──
    //
    // 装机版实证（R239/R244 部署窗 kernel.log 13:27:56~13:28:07Z）：面板并行
    // 请求各自触发 heal，heal 经 reenable（全量 revoke + 重建）执行——并发的
    // heal 互相绞杀（前一个刚注册的路由被后一个的 revoke 摘除），同一请求
    // heal 后重试仍 miss → 503 持续（日志形态：12 条 route registered →
    // 自愈行 → "even after heal attempt" WARN，逐请求循环）；且 revoke 连带
    // 摧毁 scope 里非 http 维度的在册登记（模式包资源键 / widget 绑定——
    // reenable 不重建它们，只能等 watcher 重扫恢复）。

    fn manifest_with_routes_and_tool(id: &str, tool: &str) -> PluginManifest {
        let mut m = manifest(id, tool);
        m.http_endpoints = vec![
            endpoint(&format!("/ext/{id}/a")),
            endpoint(&format!("/ext/{id}/b")),
        ];
        m
    }

    fn sample_mode_package(plugin_id: &str) -> agentos_plugin_loader::ModePackageResources {
        agentos_plugin_loader::ModePackageResources {
            plugin_id: plugin_id.to_string(),
            mode_id: plugin_id.to_string(),
            agents: vec![agentos_plugin_loader::ModeAgentEntry {
                key: format!("{plugin_id}/main"),
                plugin_id: plugin_id.to_string(),
                path: std::path::PathBuf::from("agents/main.yaml"),
            }],
            pipelines: vec![],
        }
    }

    /// 生命周期空窗现场：非路由维度（工具 + 模式包资源键）live 且入 scope，
    /// 路由维度刚被摘除（guard 落测试手中，drop 即空窗）。
    #[allow(clippy::type_complexity)]
    async fn heal_fixture_with_route_window(
        id: &str,
    ) -> (
        Arc<CapabilityRegistryImpl>,
        Arc<agentos_plugin_loader::PluginScopeRegistry>,
        crate::plugin_watcher::ManifestsStore,
        Arc<tokio::sync::RwLock<std::collections::HashSet<String>>>,
    ) {
        let m = manifest_with_routes_and_tool(id, &format!("t_{id}"));
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let scopes = Arc::new(agentos_plugin_loader::PluginScopeRegistry::new());
        // live 面：工具入 scope（register 同源）；模式包资源键入 scope（boot 同源）。
        register_plugin_capabilities(&m, &registry, &scopes);
        let mode_guard = agentos_plugin_loader::register_mode_package_guarded(
            &registry,
            sample_mode_package(id),
        )
        .expect("模式包资源应注册");
        scopes.scope_of(id).track(mode_guard);
        // 路由维度：guard 由调用方持有（可精确制造空窗）。
        let route_guards: Vec<_> = m
            .http_endpoints
            .iter()
            .map(|ep| {
                registry
                    .register_http_route_guarded(id, ep.clone())
                    .expect("路由应注册")
                    .1
            })
            .collect();
        let manifests: crate::plugin_watcher::ManifestsStore =
            Arc::new(tokio::sync::RwLock::new(vec![m]));
        let enabled_ids = Arc::new(tokio::sync::RwLock::new(std::collections::HashSet::from([
            id.to_string(),
        ])));
        drop(route_guards); // 路由全量缺席 = 生命周期空窗
        (registry, scopes, manifests, enabled_ids)
    }

    async fn heal(
        registry: &Arc<CapabilityRegistryImpl>,
        scopes: &Arc<agentos_plugin_loader::PluginScopeRegistry>,
        manifests: &crate::plugin_watcher::ManifestsStore,
        enabled_ids: &Arc<tokio::sync::RwLock<std::collections::HashSet<String>>>,
        plugin_id: &str,
    ) -> bool {
        heal_plugin_http_routes(registry, scopes, manifests, enabled_ids, plugin_id).await
    }

    /// 路由全量缺席（生命周期空窗现场）→ 自愈补齐声明路由，且**不得连带
    /// 摧毁 scope 里非 http 维度的在册登记**（工具/模式包资源键是 live 面，
    /// 不是自愈的处置对象；装机版实测被 revoke 后只能等 watcher 重扫恢复）。
    #[tokio::test]
    async fn heal_restores_routes_without_revoking_live_registrations() {
        let id = "p_hx_full";
        let (registry, scopes, manifests, enabled_ids) = heal_fixture_with_route_window(id).await;
        assert!(
            registry
                .find_http_route(&format!("/ext/{id}/a"), "GET")
                .is_none(),
            "前置：路由维度缺席"
        );

        assert!(
            heal(&registry, &scopes, &manifests, &enabled_ids, id).await,
            "空窗自愈应恢复声明路由"
        );

        assert!(
            registry
                .find_http_route(&format!("/ext/{id}/a"), "GET")
                .is_some()
                && registry
                    .find_http_route(&format!("/ext/{id}/b"), "GET")
                    .is_some(),
            "自愈后声明路由必须可查（既有语义）"
        );
        assert!(
            registry.get_mode_agent(&format!("{id}/main")).is_some(),
            "自愈不得连带摧毁模式包资源键（BUG-67：heal 全量 revoke 的连带毁灭面）"
        );
        assert!(
            registry.get_tool(&format!("t_{id}")).is_some(),
            "自愈不得连带摧毁工具注册"
        );
    }

    /// 部分空窗：仅单端点缺席、其余面（/b 路由 + 工具 + 模式包资源键）在册时，
    /// 自愈只补缺失项，**在册面原样存活**（不 revoke、不重注册）。（装机版
    /// 实证：每次 503 触发的 heal 全量 revoke，把并发请求刚恢复的路由又摘掉，
    /// 503 持续；在册的模式包资源键连带被毁。）
    #[tokio::test]
    async fn heal_fills_only_missing_and_leaves_present_surface_intact() {
        let id = "p_hx_part";
        let m = manifest_with_routes_and_tool(id, &format!("t_{id}"));
        let registry = Arc::new(CapabilityRegistryImpl::new());
        let scopes = Arc::new(agentos_plugin_loader::PluginScopeRegistry::new());
        register_plugin_capabilities(&m, &registry, &scopes);
        let mode_guard = agentos_plugin_loader::register_mode_package_guarded(
            &registry,
            sample_mode_package(id),
        )
        .expect("模式包资源应注册");
        scopes.scope_of(id).track(mode_guard);
        // /a、/b 路由在册，guard 由测试持有——可精确摘除 /a 制造部分空窗。
        let (a_ep, b_ep) = (m.http_endpoints[0].clone(), m.http_endpoints[1].clone());
        let (_, a_guard) = registry
            .register_http_route_guarded(id, a_ep.clone())
            .expect("/a 注册成功");
        let (_, _b_guard) = registry
            .register_http_route_guarded(id, b_ep.clone())
            .expect("/b 注册成功");
        drop(a_guard); // 仅 /a 缺席
        assert!(
            registry.find_http_route(&a_ep.path, "GET").is_none(),
            "前置：/a 缺席"
        );
        assert!(
            registry.find_http_route(&b_ep.path, "GET").is_some(),
            "前置：/b 在册"
        );

        let manifests: crate::plugin_watcher::ManifestsStore =
            Arc::new(tokio::sync::RwLock::new(vec![m]));
        let enabled_ids = Arc::new(tokio::sync::RwLock::new(std::collections::HashSet::from([
            id.to_string(),
        ])));
        assert!(
            heal_plugin_http_routes(&registry, &scopes, &manifests, &enabled_ids, id).await,
            "部分空窗自愈应成功"
        );

        assert!(
            registry.find_http_route(&a_ep.path, "GET").is_some(),
            "缺失端点 /a 必须补齐"
        );
        assert!(
            registry.find_http_route(&b_ep.path, "GET").is_some(),
            "在册端点 /b 必须原样存活"
        );
        assert!(
            registry.get_mode_agent(&format!("{id}/main")).is_some(),
            "自愈不得连带摧毁模式包资源键（BUG-67 连带毁灭面）"
        );
        assert!(
            registry.get_tool(&format!("t_{id}")).is_some(),
            "自愈不得连带摧毁工具注册"
        );
    }
}

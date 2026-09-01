// @feature: FP-0.2.一 插件协议 | @ci: rust-test
// 由 plugin_watcher.rs 的主 #[cfg(test)] 测试块体平移而来（保留私有项访问）。

use super::*;
use agentos_core::traits::{CapabilityRegistry, HookContext, LifecycleHook};
use agentos_core::types::{PluginContext, PluginResult, ToolExecutionResult};
use async_trait::async_trait;
use serde_json::json;

/// 用 JSON 反序列化构造测试 manifest（省去手写全部字段；未给字段走 serde default）。
fn mk_manifest(id: &str, plugin_type: &str, tools: &[&str], http: bool) -> PluginManifest {
    let tools_json: Vec<_> = tools
        .iter()
        .map(|t| json!({ "name": t, "description": t }))
        .collect();
    let caps = if tools.is_empty() {
        json!({})
    } else {
        json!({ "tools": tools_json })
    };
    let http_eps = if http {
        json!([{
            "route_id": "r", "method": "GET",
            "path": format!("/ext/{}/foo", id),
            "auth": "none", "handler_capability": "http.handle",
        }])
    } else {
        json!([])
    };
    let v = json!({
        "id": id, "name": id, "version": "1.0.0",
        "plugin_type": plugin_type, "language": "rust",
        "host_type": "sidecar", "entry": "x",
        "capabilities": caps,
        "http_endpoints": http_eps,
    });
    serde_json::from_value(v).expect("valid manifest")
}

/// 构造 light 合宿成员 manifest（host_group="light"，模拟被装箱进 group:light:N）。
fn mk_manifest_light(id: &str, tools: &[&str]) -> PluginManifest {
    let mut m = mk_manifest(id, "tool", tools, false);
    m.host_group = Some("light".to_string());
    m
}

/// 测试用 PluginInvoker：仅 discover_new_plugins / list_plugin_tools 有意义，
/// 其余方法不可达。（仿 invoker.rs 的 MockLoader 风格手写，仓库无 mockall。）
struct MockInvoker {
    manifests: Vec<PluginManifest>,
    fail: bool,
    /// G2：list_plugin_tools 的可编程响应（plugin_id → tools/list 原始 JSON）。
    /// 缺省 = 返回空工具列表（等价"声明有实际无"——测试中显式给）。
    list_tools: std::collections::HashMap<String, serde_json::Value>,
    /// 置 true 时 list_plugin_tools 返回错误（模拟 spawn/上报失败）。
    list_tools_fail: bool,
    /// 前 N 次调用失败后恢复成功（模拟瞬态观测失败，测重试路径）。
    list_tools_fail_times: usize,
    /// list_plugin_tools 调用计数（断言重试次数）。
    list_calls: std::sync::atomic::AtomicUsize,
    /// 置 true 时 invoke_tool 返回 Err（模拟冒烟调用异常）。
    invoke_tool_fail: bool,
    /// 冒烟 invoke_tool 的调用计数（断言未声明 smoke 的工具不被冒烟）。
    invoke_calls: std::sync::atomic::AtomicUsize,
}

#[async_trait]
impl PluginInvoker for MockInvoker {
    async fn invoke_pipeline_plugin(
        &self,
        _plugin_id: &str,
        _ctx: &PluginContext,
    ) -> Result<PluginResult, PluginError> {
        unimplemented!("sync 不走 invoke 路径")
    }
    async fn invoke_tool(
        &self,
        _plugin_id: &str,
        _tool_name: &str,
        _inputs: &serde_json::Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        self.invoke_calls
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        if self.invoke_tool_fail {
            return Err(PluginError {
                message: "invoke_tool boom".into(),
                code: None,
                source: None,
            });
        }
        Ok(ToolExecutionResult::success(serde_json::json!({})))
    }
    async fn send_lifecycle_hook(
        &self,
        _plugin_id: &str,
        _hook: LifecycleHook,
        _context: &HookContext,
    ) -> Result<(), PluginError> {
        unimplemented!("sync 不走 hook 路径")
    }
    async fn discover_new_plugins(&self) -> Result<Vec<PluginManifest>, PluginError> {
        if self.fail {
            Err(PluginError {
                message: "discover boom".into(),
                code: None,
                source: None,
            })
        } else {
            Ok(self.manifests.clone())
        }
    }
    async fn list_plugin_tools(&self, plugin_id: &str) -> Result<serde_json::Value, PluginError> {
        let calls = self
            .list_calls
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        if self.list_tools_fail || calls < self.list_tools_fail_times {
            return Err(PluginError {
                message: "list_tools boom".into(),
                code: None,
                source: None,
            });
        }
        Ok(self
            .list_tools
            .get(plugin_id)
            .cloned()
            .unwrap_or(serde_json::json!({ "tools": [] })))
    }
}

impl MockInvoker {
    /// 构造并**自动回填与 manifest 一致的上报**（G2 默认一致——漂移场景测试
    /// 再显式覆盖 `list_tools` 或置 `list_tools_fail`）。
    fn new(manifests: Vec<PluginManifest>) -> Self {
        let mut list_tools = std::collections::HashMap::new();
        for m in &manifests {
            list_tools.insert(
                m.id.clone(),
                json!({ "tools": m.capabilities.tools.iter().map(|t| {
                        json!({"name": t.name, "description": t.description})
                    }).collect::<Vec<_>>() }),
            );
        }
        Self {
            manifests,
            fail: false,
            list_tools,
            list_tools_fail: false,
            list_tools_fail_times: 0,
            list_calls: std::sync::atomic::AtomicUsize::new(0),
            invoke_tool_fail: false,
            invoke_calls: std::sync::atomic::AtomicUsize::new(0),
        }
    }
}

#[tokio::test]
async fn sync_once_discovers_and_applies() {
    let invoker = MockInvoker::new(vec![
        mk_manifest("a", "tool", &["ta"], false),
        mk_manifest("b", "tool", &["tb"], false),
    ]);
    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let scopes = PluginScopeRegistry::new();
    let mut known = HashSet::new();
    let report = sync_once_with_store(
        &invoker,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut HashMap::new(),
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap();
    assert_eq!(report.new_plugin_ids.len(), 2);
    assert_eq!(report.tools_registered, 2);
    assert_eq!(known.len(), 2);
    assert_eq!(registry_arc.list_tools().len(), 2);
}

#[tokio::test]
async fn sync_once_idempotent_across_calls() {
    let invoker = MockInvoker::new(vec![mk_manifest("a", "tool", &["ta"], false)]);
    let scopes = PluginScopeRegistry::new();
    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let mut known = HashSet::new();
    let first = sync_once_with_store(
        &invoker,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut HashMap::new(),
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap();
    assert_eq!(first.tools_registered, 1);
    let second = sync_once_with_store(
        &invoker,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut HashMap::new(),
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap();
    assert!(second.is_empty());
}

#[tokio::test]
async fn sync_once_propagates_discover_error() {
    let invoker = MockInvoker {
        manifests: vec![],
        fail: true,
        list_tools: std::collections::HashMap::new(),
        list_tools_fail: false,
        list_tools_fail_times: 0,
        list_calls: std::sync::atomic::AtomicUsize::new(0),
        invoke_tool_fail: false,
        invoke_calls: std::sync::atomic::AtomicUsize::new(0),
    };
    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let scopes = PluginScopeRegistry::new();
    let mut known = HashSet::new();
    let err = sync_once_with_store(
        &invoker,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut HashMap::new(),
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap_err();
    assert!(err.message.contains("boom"));
    assert!(known.is_empty());
    assert!(registry_arc.list_tools().is_empty());
}

#[test]
fn apply_empty_manifests_noop() {
    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let scopes = PluginScopeRegistry::new();
    let mut known = HashSet::new();
    let report = apply_discovered_plugins(&[], &mut known, &registry_arc, &scopes);
    assert!(report.new_plugin_ids.is_empty());
    assert_eq!(report.tools_registered, 0);
    assert_eq!(report.http_routes_registered, 0);
    assert!(known.is_empty());
    assert!(registry_arc.list_tools().is_empty());
}

#[test]
fn apply_registers_new_tool_plugin_and_updates_known() {
    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let scopes = PluginScopeRegistry::new();
    let mut known = HashSet::new();
    let m = mk_manifest("p1", "tool", &["t1", "t2"], false);
    let report =
        apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry_arc, &scopes);
    assert_eq!(report.new_plugin_ids, vec!["p1".to_string()]);
    assert_eq!(report.tools_registered, 2);
    assert!(known.contains("p1"));
    let p1_tools: Vec<_> = registry_arc
        .list_tools()
        .into_iter()
        .filter(|t| t.plugin_id == "p1")
        .collect();
    assert_eq!(p1_tools.len(), 2);
}

#[test]
fn apply_is_idempotent() {
    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let scopes = PluginScopeRegistry::new();
    let mut known = HashSet::new();
    let m = mk_manifest("p1", "tool", &["t1"], false);
    let first =
        apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry_arc, &scopes);
    assert_eq!(first.tools_registered, 1);
    // 第二次：p1 已知，应跳过。
    let second =
        apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry_arc, &scopes);
    assert!(second.new_plugin_ids.is_empty());
    assert_eq!(second.tools_registered, 0);
}

#[test]
fn apply_skips_known_plugin() {
    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let scopes = PluginScopeRegistry::new();
    let mut known = HashSet::from(["p1".to_string()]);
    let m = mk_manifest("p1", "tool", &["t1"], false);
    let report =
        apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry_arc, &scopes);
    assert!(report.new_plugin_ids.is_empty());
    assert_eq!(report.tools_registered, 0);
    assert!(registry_arc.list_tools().is_empty());
}

#[test]
fn apply_registers_http_endpoints_for_new_plugin() {
    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let scopes = PluginScopeRegistry::new();
    let mut known = HashSet::new();
    let m = mk_manifest("p1", "tool", &["t1"], true); // 带 /ext/p1/foo GET
    let report =
        apply_discovered_plugins(std::slice::from_ref(&m), &mut known, &registry_arc, &scopes);
    assert_eq!(report.new_plugin_ids, vec!["p1".to_string()]);
    // http_endpoints 写进 registry → /ext/* catch-all 的 find_http_route 能查到（无需重启）。
    assert!(registry_arc.find_http_route("/ext/p1/foo", "GET").is_some());
    assert_eq!(report.http_routes_registered, 1);
    // tools 仍注册。
    assert_eq!(report.tools_registered, 1);
}

/// G2：新插件上报与声明一致 → 全部注册，drifted_plugins 为空。
#[tokio::test]
async fn sync_once_consistent_plugin_registers_all_tools() {
    let invoker = MockInvoker::new(vec![mk_manifest("p1", "tool", &["t1", "t2"], false)]);
    let scopes = PluginScopeRegistry::new();
    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let mut known = HashSet::new();
    let report = sync_once_with_store(
        &invoker,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut HashMap::new(),
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap();
    assert_eq!(report.tools_registered, 2);
    assert!(report.drifted_plugins.is_empty());
    assert_eq!(registry_arc.list_tools().len(), 2);
}

/// G2：新插件声明有、实际无（missing）→ 漂移工具拒绝注册，其余照常。
#[tokio::test]
async fn sync_once_drifted_tool_is_rejected_from_registration() {
    let mut invoker = MockInvoker::new(vec![mk_manifest("p1", "tool", &["t1", "ghost"], false)]);
    let scopes = PluginScopeRegistry::new();
    // 覆盖上报：只报 t1（ghost 声明有实际无 → missing 漂移）
    invoker.list_tools.insert(
        "p1".into(),
        json!({ "tools": [{"name": "t1", "description": "t1"}] }),
    );
    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let mut known = HashSet::new();
    let report = sync_once_with_store(
        &invoker,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut HashMap::new(),
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap();
    assert_eq!(
        report.drifted_plugins,
        vec!["p1".to_string()],
        "漂移插件报告可见"
    );
    assert_eq!(
        report.tools_registered, 1,
        "漂移工具 ghost 被拒绝，t1 照常注册"
    );
    let names: Vec<String> = registry_arc
        .list_tools()
        .into_iter()
        .map(|t| t.name)
        .collect();
    assert_eq!(names, vec!["t1".to_string()]);
}

/// G2：观测失败（list_tools 报错）≠ 判定失败——按声明注册不净化，
/// 注册流程继续不阻断（drifted_plugins 不含该插件，账本另标记
/// verify_incomplete 待复验）。
#[tokio::test]
async fn sync_once_verify_failure_does_not_block_install() {
    let mut invoker = MockInvoker::new(vec![mk_manifest("p1", "tool", &["t1"], false)]);
    let scopes = PluginScopeRegistry::new();
    invoker.list_tools_fail = true;
    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let mut known = HashSet::new();
    let report = sync_once_with_store(
        &invoker,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut HashMap::new(),
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap();
    assert!(
        !report.drifted_plugins.contains(&"p1".to_string()),
        "观测失败不是漂移：不进 drift 报告（账本标记 verify_incomplete）"
    );
    assert_eq!(
        report.tools_registered, 1,
        "观测失败：按声明注册，不净化工具"
    );
    assert_eq!(registry_arc.list_tools().len(), 1);
}

/// G2：校验失败 lenient（灰度回退）→ 保留声明工具（旧行为）。
#[tokio::test]
async fn sync_once_verify_failure_lenient_keeps_tools() {
    let mut invoker = MockInvoker::new(vec![mk_manifest("p1", "tool", &["t1"], false)]);
    let scopes = PluginScopeRegistry::new();
    invoker.list_tools_fail = true;
    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let mut known = HashSet::new();
    let report = sync_once_with_store(
        &invoker,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut HashMap::new(),
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap();
    assert!(report.drifted_plugins.is_empty());
    assert_eq!(
        report.tools_registered, 1,
        "lenient：校验失败仍按声明注册（warn）"
    );
    assert_eq!(registry_arc.list_tools().len(), 1);
}

#[test]
fn apply_multiple_new_mixed() {
    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let scopes = PluginScopeRegistry::new();
    let mut known = HashSet::new();
    let m1 = mk_manifest("p1", "tool", &["t1"], false);
    let m2 = mk_manifest("p2", "tool", &["t2"], true);
    let m3 = mk_manifest("p3", "pipeline", &[], false);
    let all = vec![m1, m2, m3];
    let report = apply_discovered_plugins(&all, &mut known, &registry_arc, &scopes);
    assert_eq!(report.new_plugin_ids.len(), 3);
    // p1/p2 各 1 tool；p3 是 pipeline，其 capabilities.tools 不注册 → 共 2。
    assert_eq!(report.tools_registered, 2);
    // 仅 p2 带 http_endpoints（1 条端点），写进 registry 后 catch-all 立即转发。
    assert_eq!(report.http_routes_registered, 1);
    assert!(registry_arc.find_http_route("/ext/p2/foo", "GET").is_some());
    assert_eq!(known.len(), 3);
}

// ── A3：cdylib 集合变更检测 / 自动重启开关 ─────────────────

/// 构造指定 host_type 的 manifest。
fn mk_manifest_host(id: &str, host_type: &str) -> PluginManifest {
    let v = json!({
        "id": id, "name": id, "version": "1.0.0",
        "plugin_type": "tool", "language": "rust",
        "host_type": host_type, "entry": "x",
        "capabilities": {},
    });
    serde_json::from_value(v).expect("valid manifest")
}

/// 首轮只建基线：启动期已存在的 cdylib 插件不误判为新增。
#[test]
fn diff_first_round_establishes_baseline() {
    let all = vec![mk_manifest_host("native_a", "in_process")];
    let mut known: Option<HashSet<String>> = None;
    assert_eq!(diff_cdylib_change(&all, &mut known), None);
    assert_eq!(known, Some(HashSet::from(["native_a".to_string()])));
}

/// 显式基线（with_initial_cdylib_ids 路径）：首轮即可 diff 出新增。
#[tokio::test]
async fn sync_once_with_explicit_baseline_detects_addition() {
    let invoker = MockInvoker::new(vec![mk_manifest_host("native_a", "in_process")]);
    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let scopes = PluginScopeRegistry::new();
    let mut known = HashSet::new();
    let mut known_cdylib = Some(HashSet::new()); // boot 期无 cdylib
    let report = sync_once_with_store(
        &invoker,
        &registry_arc,
        &scopes,
        &mut known,
        &mut known_cdylib,
        None,
        &mut HashMap::new(),
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap();
    assert_eq!(
        report.cdylib_change,
        Some(CdylibChange {
            added: vec!["native_a".to_string()],
            removed: vec![],
        })
    );
}

/// 新增 + 消失都算变更；sidecar 插件进出不算。
#[test]
fn diff_detects_addition_and_removal_ignoring_sidecar() {
    let mut known: Option<HashSet<String>> = None;
    // 基线：native_a + sidecar_x
    let base = vec![
        mk_manifest_host("native_a", "in_process"),
        mk_manifest_host("sidecar_x", "sidecar"),
    ];
    assert_eq!(diff_cdylib_change(&base, &mut known), None);
    // 本轮：native_a 消失、native_b 新增、sidecar_x→sidecar_y（sidecar 不算）
    let next = vec![
        mk_manifest_host("native_b", "in_process"),
        mk_manifest_host("sidecar_y", "sidecar"),
    ];
    assert_eq!(
        diff_cdylib_change(&next, &mut known),
        Some(CdylibChange {
            added: vec!["native_b".to_string()],
            removed: vec!["native_a".to_string()],
        })
    );
    // 集合稳定 → 无变更
    assert_eq!(diff_cdylib_change(&next, &mut known), None);
}

/// 开关解析：未设/非 "0" → 开；仅 "0"（含空白）→ 关。
#[test]
fn auto_restart_env_switch_parsing() {
    assert!(auto_restart_env_enabled(None));
    assert!(auto_restart_env_enabled(Some("1".to_string())));
    assert!(auto_restart_env_enabled(Some("yes".to_string())));
    assert!(!auto_restart_env_enabled(Some("0".to_string())));
    assert!(!auto_restart_env_enabled(Some(" 0 ".to_string())));
}

/// 热发现注册的新插件并入 L1 启用集合（thread_fields / domain_event 面随热发现生效）。
#[tokio::test]
async fn merge_report_extends_enabled_ids() {
    let enabled = Arc::new(tokio::sync::RwLock::new(HashSet::new()));
    let mut report = SyncReport::default();
    merge_report_into_enabled_ids(&report, &enabled).await;
    assert!(enabled.read().await.is_empty(), "空报告不动集合");

    report.new_plugin_ids = vec!["hot_new".to_string()];
    merge_report_into_enabled_ids(&report, &enabled).await;
    assert!(enabled.read().await.contains("hot_new"));
}

/// 触发决策：enabled + hook → 调用；enabled=false / 无 hook → 不调用。
#[test]
fn restart_trigger_respects_switch_and_hook() {
    use std::sync::atomic::AtomicBool;
    let change = CdylibChange {
        added: vec!["native_a".to_string()],
        removed: vec![],
    };
    let fired = Arc::new(AtomicBool::new(false));
    let flag = Arc::clone(&fired);
    let hook: Arc<dyn Fn() + Send + Sync> = Arc::new(move || flag.store(true, Ordering::Relaxed));

    // enabled + hook → 触发
    assert!(trigger_cdylib_restart_if_enabled(
        &change,
        &Some(Arc::clone(&hook)),
        true
    ));
    assert!(fired.load(Ordering::Relaxed));

    // 开关关 → 不触发
    fired.store(false, Ordering::Relaxed);
    assert!(!trigger_cdylib_restart_if_enabled(
        &change,
        &Some(Arc::clone(&hook)),
        false
    ));
    assert!(!fired.load(Ordering::Relaxed));

    // 无 hook → 不触发（诚实降级，只记日志）
    assert!(!trigger_cdylib_restart_if_enabled(&change, &None, true));
}

// ── GAP-6：既有插件 manifest 变更 → 重注册 ──────────────────────

fn hash_map() -> std::collections::HashMap<String, u64> {
    std::collections::HashMap::new()
}

#[tokio::test]
async fn sync_reregisters_plugin_on_manifest_change() {
    use agentos_core::traits::CapabilityRegistry;

    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let scopes = PluginScopeRegistry::new();
    let store: ManifestsStore = std::sync::Arc::new(tokio::sync::RwLock::new(Vec::new()));
    let mut known = HashSet::new();
    let mut hashes = hash_map();

    // 首轮：v1 manifest（工具 t_old）
    let inv1 = MockInvoker::new(vec![mk_manifest("chg", "tool", &["t_old"], false)]);
    sync_once_with_store(
        &inv1,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        Some(&store),
        &mut hashes,
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap();
    assert!(registry_arc.get_tool("t_old").is_some());

    // 次轮：manifest 变更（工具 t_old → t_new）
    let inv2 = MockInvoker::new(vec![mk_manifest("chg", "tool", &["t_new"], false)]);
    let report = sync_once_with_store(
        &inv2,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        Some(&store),
        &mut hashes,
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap();

    assert_eq!(report.changed_plugin_ids, vec!["chg".to_string()]);
    // 新 schema 生效、旧工具摘除（scope revoke → guard drop 真撤销）
    assert!(registry_arc.get_tool("t_new").is_some(), "新工具应注册");
    assert!(
        registry_arc.get_tool("t_old").is_none(),
        "旧工具应随 revoke 摘除"
    );
    // manifests store 更新为新 manifest
    let guard = store.read().await;
    let m = guard
        .iter()
        .find(|x| x.id == "chg")
        .expect("store 应含 chg");
    assert!(m.capabilities.tools.iter().any(|t| t.name == "t_new"));
    drop(guard);

    // 第三轮：同 manifest 再同步 → 无变更（幂等，不重复重注册）
    let report3 = sync_once_with_store(
        &inv2,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        Some(&store),
        &mut hashes,
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap();
    assert!(report3.changed_plugin_ids.is_empty(), "未变更不得重注册");
}

#[tokio::test]
async fn sync_http_endpoints_refreshed_on_change() {
    use agentos_core::traits::CapabilityRegistry;

    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let scopes = PluginScopeRegistry::new();
    let mut known = HashSet::new();
    let mut hashes = hash_map();

    let inv1 = MockInvoker::new(vec![mk_manifest("epc", "tool", &["t"], true)]);
    sync_once_with_store(
        &inv1,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut hashes,
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap();
    let route_before = registry_arc
        .find_http_route("/ext/epc/foo", "GET")
        .expect("首轮应注册 http 路由");

    // 变更后路由描述重建（同 path 不同 handler 也能换）
    let inv2 = MockInvoker::new(vec![mk_manifest("epc", "tool", &["t2"], true)]);
    sync_once_with_store(
        &inv2,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut hashes,
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap();
    let route_after = registry_arc
        .find_http_route("/ext/epc/foo", "GET")
        .expect("变更后路由应仍在");
    let _ = (route_before, route_after);
    assert!(registry_arc.get_tool("t2").is_some());
}

// ── manifest 指纹确定性（omnisearch 假变更回归）───────────────────

/// omnisearch 同款场景：mcp endpoint env 含 8 个键（HashMap 迭代顺序随机源）。
fn mk_manifest_with_env(id: &str) -> PluginManifest {
    let v = serde_json::json!({
        "id": id, "name": id, "version": "1.0.0",
        "plugin_type": "tool", "language": "external",
        "host_type": "sidecar", "entry": "mcp:external",
        "mcp": { "transport": "stdio", "endpoint": {
            "command": "x", "args": [],
            "env": {
                "K1": "v1", "K2": "v2", "K3": "v3", "K4": "v4",
                "K5": "v5", "K6": "v6", "K7": "v7", "K8": "v8",
            }
        } },
        "capabilities": { "tools": [ { "name": "t1", "description": "d" } ] },
    });
    serde_json::from_value(v).expect("valid manifest")
}

#[test]
fn manifest_fingerprint_stable_across_reparse_with_multi_key_env() {
    // HashMap 字段（McpEndpoint.env）每次反序列化迭代顺序随机。若指纹依赖
    // 序列化键序，同一内容解析两次会得到不同指纹 → watcher 每轮误判"变更"
    // 重注册（omnisearch 每 5s 一次假变更就是这个）。多次迭代抵掉哈希顺序
    // 偶然一致的极小概率，保证回归测试必 Red。
    for _ in 0..20 {
        let a: PluginManifest = mk_manifest_with_env("fp_env");
        let b: PluginManifest = mk_manifest_with_env("fp_env");
        assert_eq!(
            manifest_fingerprint(&a),
            manifest_fingerprint(&b),
            "同一内容的 manifest 指纹必须稳定（与解析次数/轮询轮次无关）"
        );
    }
}

#[test]
fn notify_event_relevant_only_fires_for_plugin_json_and_new_dirs() {
    use notify::event::{CreateKind, DataChange, ModifyKind, RemoveKind};
    use notify::EventKind;
    use std::path::PathBuf;

    let p = |s: &str| PathBuf::from(s);
    let modify = EventKind::Modify(ModifyKind::Data(DataChange::Any));
    // plugin.json 的 增/改 是 watcher 主路径（manifest 变更 → 重注册）
    assert!(notify_event_relevant(
        modify,
        &[p("/plugins/tools/bash/plugin.json")]
    ));
    assert!(notify_event_relevant(
        EventKind::Create(CreateKind::File),
        &[p("/plugins/tools/newtool/plugin.json")]
    ));
    // 新目录 = 新插件根（tools/<name>/ 嵌套），触发扫描
    assert!(notify_event_relevant(
        EventKind::Create(CreateKind::Folder),
        &[p("/plugins/tools/newtool")]
    ));
    // 运行时产物不许触发：llm_core/logs/payload_diag 缓存、普通源码、编辑器临时文件
    assert!(!notify_event_relevant(
        modify,
        &[p(
            "/plugins/pipeline/core/llm_core/logs/payload_diag/1786__x.json"
        )]
    ));
    assert!(!notify_event_relevant(
        modify,
        &[p("/plugins/tools/bash/tool.py")]
    ));
    assert!(!notify_event_relevant(
        EventKind::Create(CreateKind::File),
        &[p("/plugins/tools/bash/.bash_tool.swp")]
    ));
    // 无关事件类型（Remove/Rename）不触发
    assert!(!notify_event_relevant(
        EventKind::Remove(RemoveKind::File),
        &[p("/plugins/tools/bash/plugin.json")]
    ));
}

// ── Phase 0：注册闸——G2 公共化 / disabled 过滤 / 依赖拒绝 ──────────────

/// G2 spawn 失败处置开关：默认严格（fail-closed），仅 "0" 置 lenient。
/// enablement 过滤纯函数：disabled 跳过并计数，保留序不变。
#[test]
fn filter_enabled_skips_disabled_and_counts() {
    let all = vec![
        mk_manifest("a", "tool", &["t_a"], false),
        mk_manifest("b", "tool", &["t_b"], false),
        mk_manifest("c", "tool", &["t_c"], false),
    ];
    let (kept, skipped) = filter_enabled_manifests(&all, |id, default| {
        if id == "b" {
            false
        } else {
            default.unwrap_or(true)
        }
    });
    assert_eq!(skipped, 1);
    let ids: Vec<&str> = kept.iter().map(|m| m.id.as_str()).collect();
    assert_eq!(ids, vec!["a", "c"]);
}

#[tokio::test]
async fn g2_verify_consistent_returns_unchanged() {
    let invoker = MockInvoker::new(vec![mk_manifest("p1", "tool", &["t1", "t2"], false)]);
    let m = mk_manifest("p1", "tool", &["t1", "t2"], false);
    let out = g2_verify_and_sanitize(&invoker, m).await;
    assert!(!out.drift && !out.spawn_failed);
    assert!(out.rejected_tools.is_empty());
    assert_eq!(out.manifest.capabilities.tools.len(), 2);
}

#[tokio::test]
async fn g2_verify_light_member_namespaced_names_not_drift() {
    // 合宿成员上报的工具名带 "{plugin_id}." 前缀（宿主按 §4.2 命名空间注册）：
    // G2 对照声明（裸名）前必须归一化前缀，否则 100% 误判 missing 漂移剔光工具
    // （08-31 实测：三 SDK-only 插件入 light 组后 task_manage/memory 全被剔除）。
    let mut invoker = MockInvoker::new(vec![mk_manifest_light(
        "task_manage_tool",
        &["task_manage"],
    )]);
    invoker.list_tools.insert(
        "task_manage_tool".into(),
        json!({ "tools": [{"name": "task_manage_tool.task_manage", "description": "任务管理"}] }),
    );
    let m = mk_manifest_light("task_manage_tool", &["task_manage"]);
    let out = g2_verify_and_sanitize(&invoker, m).await;
    assert!(!out.drift, "带命名空间前缀的上报不应判漂移");
    assert!(
        out.rejected_tools.is_empty(),
        "light 成员前缀归一后无剔除——工具不得被误杀"
    );
    assert_eq!(out.manifest.capabilities.tools.len(), 1);
}

#[tokio::test]
async fn g2_verify_drift_sanitizes_rejected_tool_only() {
    let mut invoker = MockInvoker::new(vec![mk_manifest("p1", "tool", &["t1", "ghost"], false)]);
    invoker.list_tools.insert(
        "p1".into(),
        json!({ "tools": [{"name": "t1", "description": "t1"}] }),
    );
    let m = mk_manifest("p1", "tool", &["t1", "ghost"], false);
    let out = g2_verify_and_sanitize(&invoker, m).await;
    assert!(out.drift);
    assert_eq!(out.rejected_tools, vec!["ghost".to_string()]);
    let names: Vec<String> = out
        .manifest
        .capabilities
        .tools
        .iter()
        .map(|t| t.name.clone())
        .collect();
    assert_eq!(names, vec!["t1".to_string()], "漂移工具被剔除、其余照常");
}

#[tokio::test]
async fn g2_verify_spawn_fail_keeps_declared_tools() {
    // 观测失败≠判定失败：重试后仍 spawn/list 失败 → 保留声明注册
    // （spawn_failed 供账本标记校验未完成），不再净化工具。
    let mut invoker = MockInvoker::new(vec![mk_manifest("p1", "tool", &["t1", "t2"], false)]);
    invoker.list_tools_fail = true;
    let m = mk_manifest("p1", "tool", &["t1", "t2"], false);
    let out = g2_verify_and_sanitize(&invoker, m).await;
    assert!(out.spawn_failed && !out.drift);
    assert_eq!(
        out.manifest.capabilities.tools.len(),
        2,
        "观测失败：声明注册原样保留"
    );
    assert!(out.rejected_tools.is_empty(), "观测失败无权拒工具");
}

#[tokio::test]
async fn g2_verify_observation_fail_retries_then_passes() {
    // 瞬态观测失败（首探失败，重试成功）→ 正常走比对路径，不产生 spawn_failed。
    let mut invoker = MockInvoker::new(vec![mk_manifest("p1", "tool", &["t1"], false)]);
    invoker.list_tools_fail_times = 1; // 首次 Err，重试 Ok
    invoker.list_tools.insert(
        "p1".into(),
        json!({ "tools": [{"name": "t1", "description": "t1"}] }),
    );
    let m = mk_manifest("p1", "tool", &["t1"], false);
    let out = g2_verify_and_sanitize(&invoker, m).await;
    assert!(!out.spawn_failed && !out.drift, "重试即过，不误杀");
    assert_eq!(out.manifest.capabilities.tools.len(), 1);
}

/// services-only（无 tools）插件：不 spawn 校验，原样返回（若被调用会因
/// list_tools_fail 炸——应被跳过）。
#[tokio::test]
async fn g2_verify_skips_services_only_plugin() {
    let mut invoker = MockInvoker::new(vec![mk_manifest("s", "system", &[], false)]);
    invoker.list_tools_fail = true;
    let m = mk_manifest("s", "system", &[], false);
    let out = g2_verify_and_sanitize(&invoker, m).await;
    assert!(!out.spawn_failed && !out.drift);
    assert!(out.manifest.capabilities.tools.is_empty());
}

/// InProcess（cdylib）tool 插件：无 describe 通道，不做 G2（若被调用会因
/// list_tools_fail 炸——应被跳过）。
#[tokio::test]
async fn g2_verify_skips_in_process_plugin() {
    let mut invoker = MockInvoker::new(vec![mk_manifest_host("nativeish", "in_process")]);
    invoker.list_tools_fail = true;
    let m = mk_manifest_host("nativeish", "in_process");
    let out = g2_verify_and_sanitize(&invoker, m).await;
    assert!(!out.spawn_failed && !out.drift);
}

/// 注册闸 L1：disabled 插件在热发现路径不进注册表。
#[tokio::test]
async fn sync_skips_disabled_plugin_in_hot_discovery() {
    use agentos_plugin_loader::{PluginEnablement, PluginProfile, ProfileEntry};
    let mut plugins = std::collections::HashMap::new();
    plugins.insert(
        "b".to_string(),
        ProfileEntry {
            enabled: Some(false),
            activation: None,
        },
    );
    let enablement = PluginEnablement::with_profile(PluginProfile {
        version: 1,
        plugins,
        defaults: Default::default(),
    });
    let invoker = MockInvoker::new(vec![
        mk_manifest("a", "tool", &["t_a"], false),
        mk_manifest("b", "tool", &["t_b"], false),
        mk_manifest("c", "tool", &["t_c"], false),
    ]);
    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let scopes = PluginScopeRegistry::new();
    let mut known = HashSet::new();
    let report = sync_once_with_store(
        &invoker,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut HashMap::new(),
        &mut HashMap::new(),
        None,
        Some(&enablement),
        None,
    )
    .await
    .unwrap();
    assert_eq!(report.skipped_disabled, 1, "b 被 profile 禁用");
    let ids: Vec<String> = registry_arc
        .list_tools()
        .iter()
        .map(|t| t.plugin_id.clone())
        .collect();
    assert!(!ids.contains(&"b".to_string()), "disabled 插件不进注册表");
    assert!(ids.contains(&"a".to_string()) && ids.contains(&"c".to_string()));
}

/// 回归锚：disabled 插件不进注册表，但 manifest 必须进 manifests store——
/// 否则 PUT /plugins/{id}/enabled 查不到 manifest，启用静默不注册。store =
/// "磁盘上已发现"全集，与 boot 全量注入语义对齐（e2e 见
/// tests/e2e_02/test_07_plugin_lifecycle_e2e.py）。
#[tokio::test]
async fn sync_disabled_plugin_manifest_still_enters_store() {
    use agentos_plugin_loader::{PluginEnablement, PluginProfile, ProfileEntry};
    let mut plugins = std::collections::HashMap::new();
    let scopes = PluginScopeRegistry::new();
    plugins.insert(
        "b".to_string(),
        ProfileEntry {
            enabled: Some(false),
            activation: None,
        },
    );
    let enablement = PluginEnablement::with_profile(PluginProfile {
        version: 1,
        plugins,
        defaults: Default::default(),
    });
    let invoker = MockInvoker::new(vec![
        mk_manifest("a", "tool", &["t_a"], false),
        mk_manifest("b", "tool", &["t_b"], false),
    ]);
    let registry_arc = Arc::new(CapabilityRegistryImpl::new());
    let store: ManifestsStore = Arc::new(RwLock::new(Vec::new()));
    let mut known = HashSet::new();
    sync_once_with_store(
        &invoker,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        Some(&store),
        &mut HashMap::new(),
        &mut HashMap::new(),
        None,
        Some(&enablement),
        None,
    )
    .await
    .unwrap();

    let store_ids: Vec<String> = store.read().await.iter().map(|m| m.id.clone()).collect();
    assert!(
        store_ids.contains(&"a".to_string()) && store_ids.contains(&"b".to_string()),
        "disabled 插件 manifest 也须进 store（PUT enabled 的查找源），实际: {store_ids:?}"
    );
    assert!(
        registry_arc.list_tools().iter().all(|t| t.plugin_id != "b"),
        "disabled 插件不得注册进 LLM 面"
    );
}

/// 回归锚：store-only（disabled、从未注册）插件目录消失 → store 条目
/// 同样摘除——否则卸载判定只看 known_ids，disabled 卸载留幽灵条目、
/// 插件列表永远显示已删插件。
#[tokio::test]
async fn sync_uninstalls_store_only_disabled_plugin() {
    use agentos_plugin_loader::{PluginEnablement, PluginProfile, ProfileEntry};
    let mut plugins = std::collections::HashMap::new();
    let scopes = PluginScopeRegistry::new();
    plugins.insert(
        "b".to_string(),
        ProfileEntry {
            enabled: Some(false),
            activation: None,
        },
    );
    let enablement = PluginEnablement::with_profile(PluginProfile {
        version: 1,
        plugins,
        defaults: Default::default(),
    });
    let invoker = MockInvoker::new(vec![mk_manifest("b", "tool", &["t_b"], false)]);
    let registry_arc = Arc::new(CapabilityRegistryImpl::new());
    let store: ManifestsStore = Arc::new(RwLock::new(Vec::new()));
    let mut known = HashSet::new();
    sync_once_with_store(
        &invoker,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        Some(&store),
        &mut HashMap::new(),
        &mut HashMap::new(),
        None,
        Some(&enablement),
        None,
    )
    .await
    .unwrap();
    assert!(known.is_empty(), "disabled 插件不注册，known 应为空");
    assert!(store.read().await.iter().any(|m| m.id == "b"));

    // 目录消失（discover 集不再含 b）→ store 条目须摘除（修复点：
    // pre_registered = known_ids ∪ store）。
    let invoker2 = MockInvoker::new(vec![]);
    let report = sync_once_with_store(
        &invoker2,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        Some(&store),
        &mut HashMap::new(),
        &mut HashMap::new(),
        None,
        Some(&enablement),
        None,
    )
    .await
    .unwrap();
    assert_eq!(report.uninstalled, vec!["b".to_string()]);
    assert!(
        store.read().await.iter().all(|m| m.id != "b"),
        "disabled store-only 插件卸载后不得残留幽灵条目"
    );
}

/// 注册闸服务依赖（服务唯一轴）：新插件 requires_services 无人提供 → 整插件拒绝注册。
#[tokio::test]
async fn sync_rejects_new_plugin_with_missing_required_dep() {
    let mut m = mk_manifest("dep_app", "tool", &["t_a"], false);
    let scopes = PluginScopeRegistry::new();
    m.requires_services = vec!["ghost.read".to_string()];
    let invoker = MockInvoker::new(vec![m]);
    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let mut known = HashSet::new();
    let report = sync_once_with_store(
        &invoker,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut HashMap::new(),
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap();
    assert_eq!(report.dependency_rejected, vec!["dep_app".to_string()]);
    assert!(report.new_plugin_ids.is_empty(), "依赖缺失的插件不注册");
    assert!(registry_arc.list_tools().is_empty());
    assert!(known.is_empty());
}

/// 卸载语义（P1）：目录消失 → 摘下能力；依赖者（requires_services 无人提供）
/// 一并级联摘下；服务提供者回归 → 下轮自动重注册（自愈）。
#[tokio::test]
async fn sync_uninstalls_removed_plugin_and_cascades_dependents() {
    // 提供者 p 提供 x.m；消费者 c 依赖 x.m（round1 双双注册）。
    let scopes = PluginScopeRegistry::new();
    let p: PluginManifest = serde_json::from_value(json!({
        "id": "p", "name": "p", "version": "1.0.0",
        "plugin_type": "tool", "language": "python", "host_type": "sidecar",
        "entry": "python server.py",
        "capabilities": { "services": [{ "name": "x.m" }] },
    }))
    .unwrap();
    let mut c = mk_manifest("c", "tool", &["tc"], false);
    c.requires_services = vec!["x.m".to_string()];

    let r1 = {
        let inv = MockInvoker::new(vec![p.clone(), c.clone()]);
        let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
        let mut known = HashSet::new();
        let report = sync_once_with_store(
            &inv,
            &registry_arc,
            &scopes,
            &mut known,
            &mut None,
            None,
            &mut HashMap::new(),
            &mut HashMap::new(),
            None,
            None,
            None,
        )
        .await
        .unwrap();
        assert_eq!(report.new_plugin_ids.len(), 2, "round1 全注册");
        assert_eq!(registry_arc.list_tools().len(), 1, "c 的工具 tc 注册");
        (registry_arc, known)
    };
    let (registry_arc, mut known) = r1;

    // 次轮：p 目录从磁盘消失（discover 只剩 c）→ p 卸载 + c 依赖级联摘下。
    let inv2 = MockInvoker::new(vec![c.clone()]);
    let r2 = sync_once_with_store(
        &inv2,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut HashMap::new(),
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap();
    assert_eq!(r2.uninstalled, vec!["p".to_string()]);
    assert_eq!(r2.cascade_uninstalled, vec!["c".to_string()]);
    assert!(
        registry_arc.list_tools().is_empty(),
        "依赖者 c 的能力也一并摘下"
    );
    assert!(!known.contains("p") && !known.contains("c"));

    // 提供者回归 → 下轮自动重注册（含消费者，自愈）。
    let inv3 = MockInvoker::new(vec![p.clone(), c.clone()]);
    let r3 = sync_once_with_store(
        &inv3,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut HashMap::new(),
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap();
    assert_eq!(r3.new_plugin_ids.len(), 2, "服务回归自动重注册");
    assert_eq!(registry_arc.list_tools().len(), 1, "c 的工具 tc 重新注册");
}

/// 卸载语义（P1）：无依赖者的插件被卸载 → 只摘自身能力，不波及其它插件。
#[tokio::test]
async fn sync_uninstall_isolated_plugin_leaves_others() {
    let inv1 = MockInvoker::new(vec![
        mk_manifest("a", "tool", &["ta"], false),
        mk_manifest("b", "tool", &["tb"], false),
    ]);
    let scopes = PluginScopeRegistry::new();
    let registry_arc = std::sync::Arc::new(CapabilityRegistryImpl::new());
    let mut known = HashSet::new();
    let r1 = sync_once_with_store(
        &inv1,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut HashMap::new(),
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap();
    assert_eq!(r1.new_plugin_ids.len(), 2);

    // 只删 a 的目录：b 无 requires_services → 不受牵连。
    let inv2 = MockInvoker::new(vec![mk_manifest("b", "tool", &["tb"], false)]);
    let r2 = sync_once_with_store(
        &inv2,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut HashMap::new(),
        &mut HashMap::new(),
        None,
        None,
        None,
    )
    .await
    .unwrap();
    assert_eq!(r2.uninstalled, vec!["a".to_string()]);
    assert!(r2.cascade_uninstalled.is_empty());
    let ids: Vec<String> = registry_arc
        .list_tools()
        .iter()
        .map(|t| t.plugin_id.clone())
        .collect();
    assert_eq!(ids, vec!["b".to_string()], "b 不受牵连");
    assert!(!known.contains("a"));
    assert!(known.contains("b"));
}

// ── Phase 1-C5b：注册闸冒烟样例跑（smoke:true 逐能力放行） ──────────────

fn mk_manifest_smoke(id: &str, tool: &str, smoke: bool) -> PluginManifest {
    let v = serde_json::json!({
        "id": id, "name": id, "version": "1.0.0",
        "plugin_type": "tool", "language": "rust",
        "host_type": "sidecar", "entry": "x",
        "capabilities": { "tools": [
            { "name": tool, "description": tool, "smoke": smoke,
              "input_schema": {"type": "object", "properties": {"a": {"type": "string"}}} }
        ] },
    });
    serde_json::from_value(v).expect("valid manifest")
}

fn assert_input_schema_ok(schema: serde_json::Value) -> serde_json::Value {
    // 与 mk_manifest_smoke 的 input_schema 完全一致，避免 G2 schema 误漂移
    serde_json::json!({
        "tools": [ { "name": schema["tools"][0]["name"], "description": "d",
                     "inputSchema": {"type": "object", "properties": {"a": {"type": "string"}}} } ]
    })
}

#[test]
fn sample_from_schema_fills_typed_properties() {
    let schema = serde_json::json!({
        "type": "object",
        "properties": {
            "n": {"type": "number"},
            "s": {"type": "string"},
            "e": {"type": "string", "enum": ["fast", "slow"]},
            "b": {"type": "boolean"},
            "a": {"type": "array", "items": {"type": "string"}},
            "o": {"type": "object", "properties": {"k": {"type": "integer"}}}
        }
    });
    let sample = sample_value_from_schema(&schema);
    assert_eq!(sample["n"], serde_json::json!(0));
    assert_eq!(sample["s"], serde_json::json!(""));
    assert_eq!(sample["e"], serde_json::json!("fast"), "enum 取首项");
    assert_eq!(sample["b"], serde_json::json!(false));
    assert_eq!(sample["a"], serde_json::json!([]));
    assert_eq!(sample["o"]["k"], serde_json::json!(0), "嵌套对象递归填值");
}

#[tokio::test]
async fn smoke_failure_rejects_the_tool() {
    let mut invoker = MockInvoker::new(vec![mk_manifest_smoke("p1", "t_smoke", true)]);
    invoker.list_tools = std::collections::HashMap::from([(
        "p1".into(),
        assert_input_schema_ok(json!({"tools":[{ "name": "t_smoke" }]})),
    )]);
    invoker.invoke_tool_fail = true;
    let out = g2_verify_and_sanitize(&invoker, mk_manifest_smoke("p1", "t_smoke", true)).await;
    assert!(out.smoke_failed, "冒烟失败须标记");
    assert!(
        out.manifest.capabilities.tools.is_empty(),
        "冒烟失败的工具被拒绝"
    );
    assert!(out.rejected_tools.contains(&"t_smoke".to_string()));
}

#[tokio::test]
async fn smoke_success_keeps_the_tool() {
    let mut invoker = MockInvoker::new(vec![mk_manifest_smoke("p1", "t_smoke", true)]);
    invoker.list_tools = std::collections::HashMap::from([(
        "p1".into(),
        assert_input_schema_ok(json!({"tools":[{ "name": "t_smoke" }]})),
    )]);
    let out = g2_verify_and_sanitize(&invoker, mk_manifest_smoke("p1", "t_smoke", true)).await;
    assert!(!out.smoke_failed);
    assert_eq!(out.manifest.capabilities.tools.len(), 1, "冒烟成功则保留");
    assert_eq!(
        invoker
            .invoke_calls
            .load(std::sync::atomic::Ordering::Relaxed),
        1
    );
}

#[tokio::test]
async fn smoke_not_run_without_optin() {
    let mut invoker = MockInvoker::new(vec![mk_manifest_smoke("p1", "t_no_smoke", false)]);
    invoker.list_tools = std::collections::HashMap::from([(
        "p1".into(),
        assert_input_schema_ok(json!({"tools":[{ "name": "t_no_smoke" }]})),
    )]);
    let _ = g2_verify_and_sanitize(&invoker, mk_manifest_smoke("p1", "t_no_smoke", false)).await;
    assert_eq!(
        invoker
            .invoke_calls
            .load(std::sync::atomic::Ordering::Relaxed),
        0,
        "未声明 smoke:true 的工具不被冒烟（避免注册期副作用）"
    );
}

/// output_schema 声明合法性（声明即校验）：畸形声明 → 注册期拒绝该工具，
/// 不再等到 tool_core 运行时才暴露。
#[tokio::test]
async fn malformed_output_schema_rejected_at_registration() {
    let v = serde_json::json!({
        "id": "p1", "name": "p1", "version": "1.0.0",
        "plugin_type": "tool", "language": "rust",
        "host_type": "sidecar", "entry": "x",
        "capabilities": { "tools": [
            { "name": "t_bad", "description": "d",
              "output_schema": {"type": "object", "properties": "oops"} },
            { "name": "t_ok", "description": "d",
              "output_schema": {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]} }
        ] },
    });
    let m: PluginManifest = serde_json::from_value(v).unwrap();
    let invoker = MockInvoker::new(vec![m.clone()]);
    let out = g2_verify_and_sanitize(&invoker, m).await;
    assert!(
        out.rejected_tools.contains(&"t_bad".to_string()),
        "畸形 output_schema 的工具必须被拒"
    );
    assert_eq!(out.manifest.capabilities.tools.len(), 1, "t_ok 保留");
    assert_eq!(out.manifest.capabilities.tools[0].name, "t_ok");
}

// ── G2 复验闭环：净化不被下轮 sync 复活 / manifest 编辑绕不过 G2 / 代码修复恢复 ──

/// 回归锚（复活）：净化剔除的工具不得在下轮 sync 中复活——修复前的实现以
/// 净化版 manifest 指纹为基线，下轮 raw 声明指纹必与之相异，被误判"变更"
/// 后把被剔工具原样重注册（复活）。基线必须落声明指纹。
#[tokio::test]
async fn sync_next_round_does_not_resurrect_sanitized_tool() {
    // 首轮：声明 [t1, ghost]，实现只报 t1 → ghost 净化剔除
    let mut inv1 = MockInvoker::new(vec![mk_manifest("d1", "tool", &["t1", "ghost"], false)]);
    inv1.list_tools.insert(
        "d1".into(),
        json!({ "tools": [{"name": "t1", "description": "t1"}] }),
    );
    let scopes = PluginScopeRegistry::new();
    let registry_arc = Arc::new(CapabilityRegistryImpl::new());
    let store: ManifestsStore = Arc::new(RwLock::new(Vec::new()));
    let ledger = crate::contract::ContractLedger::new();
    let mut known = HashSet::new();
    let mut hashes = HashMap::new();
    let mut code_hashes = HashMap::new();
    let r1 = sync_once_with_store(
        &inv1,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        Some(&store),
        &mut hashes,
        &mut code_hashes,
        None,
        None,
        Some(&ledger),
    )
    .await
    .unwrap();
    assert_eq!(r1.drifted_plugins, vec!["d1".to_string()]);
    assert!(
        registry_arc.get_tool("ghost").is_none(),
        "首轮 ghost 被净化剔除"
    );

    // 次轮：磁盘无任何变化——不得误判变更、不得复活 ghost
    let r2 = sync_once_with_store(
        &inv1,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        Some(&store),
        &mut hashes,
        &mut code_hashes,
        None,
        None,
        Some(&ledger),
    )
    .await
    .unwrap();
    assert!(r2.changed_plugin_ids.is_empty(), "未变更不得重注册");
    assert!(
        registry_arc.get_tool("ghost").is_none(),
        "被剔工具不得随下轮 sync 复活"
    );
    // store 与账本保持净化真相：store 条目无 ghost；账本 sanitized 证据保留
    let store_tools: Vec<String> = store
        .read()
        .await
        .iter()
        .find(|m| m.id == "d1")
        .expect("store 应含 d1")
        .capabilities
        .tools
        .iter()
        .map(|t| t.name.clone())
        .collect();
    assert_eq!(store_tools, vec!["t1".to_string()]);
    let st = ledger.get("d1").expect("账本应含 d1");
    assert_eq!(st.gates.g2_consistency, "sanitized");

    // 第三轮：manifest 编辑（name 变更 → 声明指纹变化）但仍漂移 → 走 G2 复验，
    // ghost 依旧剔除——manifest 编辑不得绕过 G2 复活被剔工具
    let mut m3 = mk_manifest("d1", "tool", &["t1", "ghost"], false);
    m3.name = "d1-edited".into();
    let mut inv3 = MockInvoker::new(vec![m3]);
    inv3.list_tools.insert(
        "d1".into(),
        json!({ "tools": [{"name": "t1", "description": "t1"}] }),
    );
    let r3 = sync_once_with_store(
        &inv3,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        Some(&store),
        &mut hashes,
        &mut code_hashes,
        None,
        None,
        Some(&ledger),
    )
    .await
    .unwrap();
    assert_eq!(
        r3.changed_plugin_ids,
        vec!["d1".to_string()],
        "声明变更须触发复验重注册"
    );
    assert!(
        registry_arc.get_tool("ghost").is_none(),
        "manifest 编辑不得绕过 G2 复活被剔工具"
    );
    assert_eq!(r3.drifted_plugins, vec!["d1".to_string()], "复验漂移可见");
}

/// 回归锚（修复后恢复）：G2 净化剔除的工具，实现修复（代码指纹变化、manifest
/// 未动）后由 watcher 复验自动恢复——不再要求 manifest 再改或重启内核。
#[tokio::test]
async fn sync_revalidates_on_code_change_and_restores_fixed_tool() {
    let scopes = PluginScopeRegistry::new();
    let registry_arc = Arc::new(CapabilityRegistryImpl::new());
    let mut known = HashSet::new();
    let mut hashes = HashMap::new();
    let mut code_hashes = HashMap::new();
    let ledger = crate::contract::ContractLedger::new();

    // 两个内容不同的目录（文件名不同 → 指纹必异），模拟"修复前/后"的代码；
    // stage 记录当前指向，resolver 模拟 loader 的目录解析。
    let dir_old = tempfile::tempdir().unwrap();
    std::fs::write(dir_old.path().join("impl_old.py"), b"v1").unwrap();
    let dir_new = tempfile::tempdir().unwrap();
    std::fs::write(dir_new.path().join("impl_new.py"), b"v2").unwrap();
    let stage = Arc::new(parking_lot::RwLock::new(dir_old.path().to_path_buf()));
    let resolver: Arc<CodeDirResolver> = {
        let stage = stage.clone();
        Arc::new(move |_id: &str| Some(stage.read().clone()))
    };

    // 首轮：声明 [t1, t2]，实现只报 t1 → t2 净化剔除
    let mut inv1 = MockInvoker::new(vec![mk_manifest("fix1", "tool", &["t1", "t2"], false)]);
    inv1.list_tools.insert(
        "fix1".into(),
        json!({ "tools": [{"name": "t1", "description": "t1"}] }),
    );
    let r1 = sync_once_with_store(
        &inv1,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut hashes,
        &mut code_hashes,
        Some(&resolver),
        None,
        Some(&ledger),
    )
    .await
    .unwrap();
    assert_eq!(r1.drifted_plugins, vec!["fix1".to_string()]);
    assert!(registry_arc.get_tool("t2").is_none(), "t2 首轮被净化剔除");

    // 次轮：实现修复（上报恢复 t1+t2），manifest 未动，仅代码指纹变化
    *stage.write() = dir_new.path().to_path_buf();
    let inv2 = MockInvoker::new(vec![mk_manifest("fix1", "tool", &["t1", "t2"], false)]);
    let r2 = sync_once_with_store(
        &inv2,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut hashes,
        &mut code_hashes,
        Some(&resolver),
        None,
        Some(&ledger),
    )
    .await
    .unwrap();
    assert_eq!(
        r2.changed_plugin_ids,
        vec!["fix1".to_string()],
        "代码指纹变化须触发复验重注册"
    );
    assert!(
        registry_arc.get_tool("t2").is_some(),
        "修复后的工具经复验恢复，无需 manifest 变更或重启"
    );
    let st = ledger.get("fix1").expect("复验后账本应有记录");
    assert_eq!(
        st.gates.g2_consistency, "ok",
        "复验通过即清除净化状态: {:?}",
        st.gates.g2_consistency
    );
    assert!(st.gates.reverified_ts.is_some(), "显式复验通过必留痕");

    // 第三轮：代码/声明都不再变 → 不重验（幂等，不重复 spawn 复核）
    let r3 = sync_once_with_store(
        &inv2,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut hashes,
        &mut code_hashes,
        Some(&resolver),
        None,
        Some(&ledger),
    )
    .await
    .unwrap();
    assert!(r3.changed_plugin_ids.is_empty(), "未变更不得重注册");
}

/// 代码指纹解析缺省臂：无解析器 / 解析不到目录 → 指纹恒 0（复验退化为仅声明
/// 指纹驱动，不误触发）。
#[test]
fn current_code_fp_defaults_to_zero_without_resolver_or_dir() {
    let m = mk_manifest("x1", "tool", &["t"], false);
    assert_eq!(current_code_fp(None, &m), 0, "无解析器 → 0");
    let no_dir: Arc<CodeDirResolver> = Arc::new(|_id: &str| None);
    assert_eq!(current_code_fp(Some(&no_dir), &m), 0, "目录不可得 → 0");
}

/// boot 注册插件锚点：known 预置（boot 已注册）、指纹基线空（watcher 刚启动）
/// → 首轮 sync 只建基线不动作；声明编辑后正常触发复验重注册。
#[tokio::test]
async fn sync_boot_plugin_first_sync_establishes_baseline_without_action() {
    let scopes = PluginScopeRegistry::new();
    let registry_arc = Arc::new(CapabilityRegistryImpl::new());
    let mut known: HashSet<String> = ["boot1".to_string()].into_iter().collect();
    let mut hashes = HashMap::new();
    let mut code_hashes = HashMap::new();

    // 首轮：boot 插件建基线，不重注册
    let inv1 = MockInvoker::new(vec![mk_manifest("boot1", "tool", &["t"], false)]);
    let r1 = sync_once_with_store(
        &inv1,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut hashes,
        &mut code_hashes,
        None,
        None,
        None,
    )
    .await
    .unwrap();
    assert!(
        r1.changed_plugin_ids.is_empty() && r1.new_plugin_ids.is_empty(),
        "boot 插件首轮只建基线"
    );

    // 次轮（同 manifest）：基线生效，不动作
    let r2 = sync_once_with_store(
        &inv1,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut hashes,
        &mut code_hashes,
        None,
        None,
        None,
    )
    .await
    .unwrap();
    assert!(r2.changed_plugin_ids.is_empty());

    // 第三轮（声明编辑）：触发复验重注册
    let inv3 = MockInvoker::new(vec![mk_manifest("boot1", "tool", &["t2"], false)]);
    let r3 = sync_once_with_store(
        &inv3,
        &registry_arc,
        &scopes,
        &mut known,
        &mut None,
        None,
        &mut hashes,
        &mut code_hashes,
        None,
        None,
        None,
    )
    .await
    .unwrap();
    assert_eq!(r3.changed_plugin_ids, vec!["boot1".to_string()]);
    assert!(registry_arc.get_tool("t2").is_some(), "新声明工具注册");
    assert!(registry_arc.get_tool("t").is_none(), "旧声明工具摘除");
}

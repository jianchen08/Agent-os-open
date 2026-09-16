// @feature: FP-0.2.一 插件协议 | @ci: rust-test
// 成员粒度热重载的 watcher 回退路径（独立于 tests.rs 的同步轮次大测试）：
// 复验执行段（sync_reverify_apply）在代码变更时先试 reload_member；
// 成功则不驱逐宿主（其余成员进程不动——驱逐爆炸半径收缩到单成员）；
// 失败（独占/不支持/宿主协议错误/超时）回退 force_unload 整组驱逐，
// 行为与既有语义一致。

use super::*;
use agentos_core::traits::{HookContext, LifecycleHook, PluginLoader};
use agentos_core::types::{PluginContext, PluginResult, ToolExecutionResult};
use async_trait::async_trait;
use serde_json::json;
use std::collections::HashMap;
use std::sync::Mutex;

/// 可编程 Mock：记录 reload_member / force_unload 的调用与各自结果。
struct ReloadMockInvoker {
    /// reload_member 返回 Ok 的 plugin_id 集合；不在集合 = Err（不支持/失败）。
    reload_ok: std::collections::HashSet<String>,
    reload_calls: Mutex<Vec<String>>,
    force_unload_calls: Mutex<Vec<String>>,
}

impl ReloadMockInvoker {
    fn new(reload_ok: &[&str]) -> Self {
        Self {
            reload_ok: reload_ok.iter().map(|s| s.to_string()).collect(),
            reload_calls: Mutex::new(Vec::new()),
            force_unload_calls: Mutex::new(Vec::new()),
        }
    }
}

#[async_trait]
impl PluginInvoker for ReloadMockInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _plugin_id: &str,
        _ctx: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        unimplemented!("复验执行段不走 invoke 路径")
    }
    async fn invoke_tool(
        &self,
        _plugin_id: &str,
        _tool_name: &str,
        _inputs: &serde_json::Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        unimplemented!("复验执行段（g2_applicable=false）不走冒烟调用")
    }
    async fn force_unload(&self, plugin_id: &str) -> Result<(), PluginError> {
        self.force_unload_calls
            .lock()
            .unwrap()
            .push(plugin_id.to_string());
        Ok(())
    }
    async fn send_lifecycle_hook(
        &self,
        _plugin_id: &str,
        _hook: LifecycleHook,
        _context: &HookContext,
    ) -> Result<(), PluginError> {
        unimplemented!("复验执行段不走生命周期钩子")
    }
    async fn reload_member(&self, plugin_id: &str) -> Result<(), PluginError> {
        self.reload_calls
            .lock()
            .unwrap()
            .push(plugin_id.to_string());
        if self.reload_ok.contains(plugin_id) {
            Ok(())
        } else {
            Err(PluginError {
                message: "reload_member not supported".into(),
                code: None,
                source: None,
            })
        }
    }
}

/// 用 JSON 反序列化构造零能力 sidecar manifest（省去手写全部字段）。
fn mk_zero_cap_manifest(id: &str) -> PluginManifest {
    let v = json!({
        "id": id, "name": id, "version": "1.0.0",
        "plugin_type": "tool", "language": "python",
        "host_type": "sidecar", "entry": "python server.py",
        "capabilities": {},
    });
    serde_json::from_value(v).expect("valid manifest")
}

async fn run_apply(invoker: &ReloadMockInvoker, m: &PluginManifest) {
    let registry = Arc::new(CapabilityRegistryImpl::new());
    let scopes = PluginScopeRegistry::new();
    let mut report = SyncReport::default();
    sync_reverify_apply(
        invoker,
        &registry,
        &scopes,
        m,
        &HashMap::new(),
        false, // decl_changed
        true,  // code_changed
        false, // dynamic_import
        false, // g2_applicable（零能力 → 复验段只行使驱逐/重载面）
        999u64,
        None,
        None,
        &mut report,
    )
    .await;
}

#[tokio::test]
async fn reload_success_avoids_force_unload() {
    // 组员热重载成功：不触发整组驱逐（其余成员进程不动——成员粒度的核心收益）。
    let invoker = ReloadMockInvoker::new(&["mon"]);
    let m = mk_zero_cap_manifest("mon");
    run_apply(&invoker, &m).await;
    assert_eq!(
        *invoker.reload_calls.lock().unwrap(),
        vec!["mon".to_string()],
        "代码变更必须先试 reload_member"
    );
    assert!(
        invoker.force_unload_calls.lock().unwrap().is_empty(),
        "reload 成功不得回退整组驱逐"
    );
}

#[tokio::test]
async fn reload_failure_falls_back_to_force_unload() {
    // reload 失败（独占/不支持/宿主应答错误）：回退 force_unload 驱逐宿主
    // ——回退后行为与既有语义一致（下次调用按新码重建）。
    let invoker = ReloadMockInvoker::new(&[]);
    let m = mk_zero_cap_manifest("solo_p");
    run_apply(&invoker, &m).await;
    assert_eq!(
        *invoker.reload_calls.lock().unwrap(),
        vec!["solo_p".to_string()],
        "失败路径同样先经过 reload_member 探测"
    );
    assert_eq!(
        *invoker.force_unload_calls.lock().unwrap(),
        vec!["solo_p".to_string()],
        "reload 失败必须回退 force_unload"
    );
}

// 契约留痕：trait 方法集合的引用面（防止误删 trait 侧接线仍静默编译）。
#[allow(dead_code)]
fn _wiring_witness(_c: &HookContext, _h: &LifecycleHook, _l: &dyn PluginLoader) {}

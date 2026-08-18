//! 闸2·观测：插件契约状态聚合（`GET /api/v1/plugins/contract-status`）。
//!
//! 消费《插件契约校验闸门体系完整方案》（2026-08-18）的观测数据模型——每插件
//! 一条契约状态，任一高等级闸失败即红灯（前端 `contractRedLight` 判定）。
//!
//! 状态**在注册入口收口写入**（boot / 热发现 / reenable / validate-all），端点
//! 只读账本、不在请求时重跑校验（"结果前置复用"，方案 §1.7）。账本未登记的
//! manifest 补 `not_covered` 缺省——诚实标注"未覆盖"，不假装绿。

use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

use parking_lot::RwLock;
use serde::Serialize;

use agentos_core::traits::PluginManifest;

use crate::plugin_watcher::G2VerifyOutcome;

/// 契门级状态（`gates` 嵌套对象，与前端 ContractStatusPanel 的 ContractGateState 对齐）。
#[derive(Debug, Clone, Serialize)]
pub struct ContractGates {
    /// 加载通过 = manifest schema 合法（loader 严格反序列化 fail-closed）。
    pub manifest_schema_valid: bool,
    pub dep_ok: bool,
    /// `ok` | `drift` | `not_covered`（spawn 不可用 = 没验到，不是验出问题）。
    pub g2_consistency: String,
    /// `ok` | `failed` | `skipped` | `not_covered`.
    pub smoke_result: String,
    /// `ok` | `invalid` | `n/a`——内核不持前端渲染器，render/ui 声明结构合法性
    /// 由前端 `validatePluginDeclaration` 复核（计划 §5.3），此处诚实标 `n/a`。
    pub render_decl_valid: String,
    pub runtime_input_violations: u64,
    pub runtime_output_violations: u64,
    pub last_error: Option<String>,
}

/// 每插件一条契约状态（闸2·观测模型，`gates` 与前端 ContractStatusPanel 对齐）。
#[derive(Debug, Clone, Serialize)]
pub struct PluginContractState {
    pub plugin_id: String,
    pub enabled: bool,
    pub gates: ContractGates,
    /// Unix 毫秒时间戳（最后一次写入时刻）。
    #[serde(rename = "last_scan_ts")]
    pub last_scan_ts: u64,
}

impl PluginContractState {
    /// 从单插件校验结果推导契约状态（boot/热发现/reenable 收口通用）。
    pub fn derived(plugin: &PluginManifest, enabled: bool, g2: Option<&G2VerifyOutcome>) -> Self {
        let (g2_consistency, smoke_result, last_error) = match g2 {
            None => ("not_covered".to_string(), "not_covered".to_string(), None),
            Some(o) => {
                let g2_consistency = if o.spawn_failed {
                    "not_covered"
                } else if o.drift {
                    "drift"
                } else {
                    "ok"
                };
                let smoke_result = if o.smoke_failed {
                    "failed"
                } else if plugin
                    .capabilities
                    .tools
                    .iter()
                    .any(|t| t.smoke == Some(true))
                {
                    "ok"
                } else {
                    "skipped"
                };
                let last_error = if o.rejected_tools.is_empty() {
                    None
                } else {
                    Some(format!(
                        "拒绝注册/剔除工具: {}",
                        o.rejected_tools.join(", ")
                    ))
                };
                (
                    g2_consistency.to_string(),
                    smoke_result.to_string(),
                    last_error,
                )
            }
        };
        Self {
            plugin_id: plugin.id.clone(),
            enabled,
            gates: ContractGates {
                manifest_schema_valid: true,
                dep_ok: true,
                g2_consistency,
                smoke_result,
                render_decl_valid: "n/a".to_string(),
                runtime_input_violations: 0,
                runtime_output_violations: 0,
                last_error,
            },
            last_scan_ts: now_ms(),
        }
    }

    /// 未覆盖缺省（非 sidecar / 无 tools+services / 无校验结果的登记）。
    pub fn not_covered(plugin: &PluginManifest, enabled: bool) -> Self {
        Self::derived(plugin, enabled, None)
    }
}

/// 插件契约状态账本（每插件一条；boot/热发现/reenable/validate-all 收口写入）。
#[derive(Default)]
pub struct ContractLedger {
    inner: RwLock<HashMap<String, PluginContractState>>,
}

#[allow(clippy::new_without_default)]
impl ContractLedger {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn upsert(&self, st: PluginContractState) {
        self.inner.write().insert(st.plugin_id.clone(), st);
    }

    pub fn get(&self, plugin_id: &str) -> Option<PluginContractState> {
        self.inner.read().get(plugin_id).cloned()
    }

    pub fn snapshot(&self) -> Vec<PluginContractState> {
        let mut items: Vec<_> = self.inner.read().values().cloned().collect();
        items.sort_by(|a, b| a.plugin_id.cmp(&b.plugin_id));
        items
    }
}

/// 把全量 manifests 收敛为契约状态清单：账本有记录用账本（`enabled` 一律以当前
/// 快照为准，不用账本旧值）；未登记补 `not_covered` 缺省。`dep_ok` 取账本登记值
/// （boot 全量登记=通过；热发现依赖拒绝路径显式写 false；未覆盖缺省=true）——
/// 依赖闸的**注册期结论前置复用**，端点不重跑校验（方案 §1.7）。
pub fn contract_statuses(
    ledger: &ContractLedger,
    manifests: &[PluginManifest],
    enabled_ids: &std::collections::HashSet<String>,
) -> Vec<PluginContractState> {
    let mut items = Vec::with_capacity(manifests.len());
    for m in manifests {
        let mut st = match ledger.get(&m.id) {
            Some(s) => s,
            None => PluginContractState::not_covered(m, enabled_ids.contains(&m.id)),
        };
        st.enabled = enabled_ids.contains(&m.id);
        items.push(st);
    }
    items
}

/// Unix 毫秒时间戳（观测字段 `last_scan_ts` 用）。
pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::traits::PluginManifest;

    fn manifest(id: &str) -> PluginManifest {
        serde_json::from_value(serde_json::json!({
            "id": id, "name": id, "version": "1.0.0",
            "plugin_type": "tool", "language": "python",
            "host_type": "sidecar", "entry": format!("python3 {id}.py"),
            "capabilities": {},
        }))
        .expect("valid manifest")
    }

    fn outcome(drift: bool, spawn_failed: bool) -> G2VerifyOutcome {
        G2VerifyOutcome {
            manifest: manifest("x"),
            rejected_tools: Vec::new(),
            drift,
            spawn_failed,
            smoke_failed: false,
        }
    }

    #[test]
    fn derived_ok_marks_green() {
        let st = PluginContractState::derived(&manifest("a"), true, Some(&outcome(false, false)));
        assert!(st.enabled);
        assert!(st.gates.manifest_schema_valid);
        assert_eq!(st.gates.g2_consistency, "ok");
        assert_eq!(st.gates.smoke_result, "skipped");
        assert!(st.gates.last_error.is_none());
    }

    #[test]
    fn derived_drift_marks_red_with_error() {
        let mut o = outcome(true, false);
        o.rejected_tools = vec!["b".into()];
        let st = PluginContractState::derived(&manifest("a"), true, Some(&o));
        assert_eq!(st.gates.g2_consistency, "drift");
        assert!(st.gates.last_error.unwrap().contains("b"));
    }

    #[test]
    fn spawn_failure_is_not_covered_not_drift() {
        let st = PluginContractState::derived(&manifest("a"), true, Some(&outcome(false, true)));
        assert_eq!(st.gates.g2_consistency, "not_covered");
    }

    #[test]
    fn serializes_nested_gates_for_frontend() {
        let st = PluginContractState::derived(&manifest("a"), true, Some(&outcome(false, false)));
        let v = serde_json::to_value(&st).unwrap();
        let gates = v["gates"].as_object().expect("gates 必须是嵌套对象");
        assert_eq!(gates["g2_consistency"], "ok");
        assert_eq!(v["plugin_id"], "a");
        assert!(v["last_scan_ts"].is_u64());
    }

    #[test]
    fn ledger_upsert_snapshot_sorted() {
        let l = ContractLedger::new();
        l.upsert(PluginContractState::not_covered(&manifest("b"), true));
        l.upsert(PluginContractState::not_covered(&manifest("a"), false));
        let snap = l.snapshot();
        assert_eq!(snap.len(), 2);
        assert_eq!(snap[0].plugin_id, "a");
        assert_eq!(snap[1].plugin_id, "b");
        assert!(!snap[0].enabled);
    }
}

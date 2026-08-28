// @feature: FP-0.2.一 插件协议·契约闸门观测账本 | @ci: rust-test
//! 闸2·观测：插件契约状态聚合（`GET /api/v1/plugins/contract-status`）。
//!
//! 消费《插件契约校验闸门体系完整方案》（2026-08-18）的观测数据模型——每插件
//! 一条契约状态，任一高等级闸失败即红灯（前端 `contractRedLight` 判定）。
//!
//! 状态**在注册入口收口写入**（boot / 热发现 / reenable / validate-all），端点
//! 只读账本、不在请求时重跑校验（"结果前置复用"，方案 §1.7）。账本未登记的
//! manifest 补 `not_covered` 缺省——诚实标注"未覆盖"，不假装绿。
//!
//! upsert 为**合并语义**（ADR 2026-08-28 决策1：粘滞）：弱信号（not_covered/
//! verify_incomplete）不覆盖既有 drift/sanitized 证据，清除仅经显式复验通过
//! （记 `reverified_ts`）；净化写显式 `sanitized` 状态留痕（决策2），注册表↔磁盘
//! 一致性检出经 `record_registry_disk_diffs` 留痕（决策3）。

use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

use parking_lot::RwLock;
use serde::Serialize;

use agentos_core::traits::{PluginManifest, ToolCapability};

use crate::plugin_watcher::G2VerifyOutcome;

/// G2 净化证据（ADR 2026-08-28 决策2）：剔除动作的既成事实留痕，
/// 处置（净化/重注册）不得销毁判定证据——后续观测路径必须能还原"处理前发现了什么"。
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SanitizeEvidence {
    /// 被剔除的工具名。
    pub rejected_tools: Vec<String>,
    /// 净化前声明的工具数。
    pub tools_before: usize,
    /// 净化后保留的工具数。
    pub tools_after: usize,
    /// 剔除原因。
    pub reason: String,
    /// Unix 毫秒时间戳（净化发生时刻）。
    pub sanitized_ts: u64,
}

/// 注册表 manifest ↔ 磁盘 manifest 差异项（ADR 2026-08-28 决策3：一致性检出）。
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct RegistryDiskDiff {
    /// `missing_tool`（注册表缺磁盘声明的工具，净化剔除的主信号）
    /// | `extra_tool`（注册表多出的工具）| `schema_diff`（同名工具 schema 摘要不一致）。
    pub kind: String,
    /// 涉及的工具名。
    pub tool: String,
    /// 差异说明。
    pub detail: String,
}

/// 契门级状态（`gates` 嵌套对象，与前端 ContractStatusPanel 的 ContractGateState 对齐）。
#[derive(Debug, Clone, Serialize)]
pub struct ContractGates {
    /// 加载通过 = manifest schema 合法（loader 严格反序列化 fail-closed）。
    pub manifest_schema_valid: bool,
    pub dep_ok: bool,
    /// `ok` | `drift` | `sanitized`（判定失败已净化重注册）|
    /// `verify_incomplete`（观测通道故障 = 没验到，声明注册保留待复验）| `not_covered`。
    pub g2_consistency: String,
    /// `ok` | `failed` | `skipped` | `not_covered`.
    pub smoke_result: String,
    /// `ok` | `invalid` | `n/a`——内核不持前端渲染器，render/ui 声明结构合法性
    /// 由前端 `validatePluginDeclaration` 复核（计划 §5.3），此处诚实标 `n/a`。
    pub render_decl_valid: String,
    pub runtime_input_violations: u64,
    pub runtime_output_violations: u64,
    pub last_error: Option<String>,
    /// 声明↔实现比对中被拒的工具名（drift/sanitized 状态下非空）。
    pub rejected_tools: Vec<String>,
    /// 净化证据：`g2_consistency == "sanitized"` 时必有（既成事实，账本粘滞保留）。
    pub sanitized: Option<SanitizeEvidence>,
    /// Unix 毫秒时间戳：显式复验通过（`ok` 清除口）的时刻——drift/sanitized
    /// 只经真实复验清除，清除必留痕。
    pub reverified_ts: Option<u64>,
    /// 注册表↔磁盘 manifest 一致性检出（validate-all 写入）：`None` = 未检出
    /// （磁盘不可读/未覆盖），`Some(vec![])` = 已检一致，非空 = 已偏离磁盘声明。
    pub registry_disk_diffs: Option<Vec<RegistryDiskDiff>>,
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
        let (g2_consistency, smoke_result, last_error, rejected_tools, sanitized) = match g2 {
            None => (
                "not_covered".to_string(),
                "not_covered".to_string(),
                None,
                Vec::new(),
                None,
            ),
            Some(o) => {
                // 观测失败 ≠ 判定失败：spawn/list 失败时声明
                // 注册保留——账本标记"校验未完成"（区别于未覆盖/漂移），
                // 前端/契约状态页可见"待复验"。
                let g2_consistency = if o.spawn_failed {
                    "verify_incomplete"
                } else if is_sanitized(plugin, o) {
                    "sanitized"
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
                let last_error = if o.spawn_failed {
                    Some("校验未完成（观测通道故障：spawn/tools-list 重试后仍失败，声明注册保留待复验）".to_string())
                } else if o.rejected_tools.is_empty() {
                    None
                } else {
                    Some(format!(
                        "声明与实现不一致，剔除工具（需修改插件）: {}",
                        o.rejected_tools.join(", ")
                    ))
                };
                // 净化证据：净化是处置既成事实，账本必留痕（ADR 决策2）。
                let sanitized = if is_sanitized(plugin, o) {
                    Some(SanitizeEvidence {
                        rejected_tools: o.rejected_tools.clone(),
                        tools_before: plugin.capabilities.tools.len(),
                        tools_after: o.manifest.capabilities.tools.len(),
                        reason: "G2 声明↔实现一致性复核失败，剔除工具后按净化 manifest 重注册".to_string(),
                        sanitized_ts: now_ms(),
                    })
                } else {
                    None
                };
                (
                    g2_consistency.to_string(),
                    smoke_result.to_string(),
                    last_error,
                    o.rejected_tools.clone(),
                    sanitized,
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
                rejected_tools,
                sanitized,
                reverified_ts: None,
                registry_disk_diffs: None,
            },
            last_scan_ts: now_ms(),
        }
    }

    /// 未覆盖缺省（非 sidecar / 无 tools+services / 无校验结果的登记）。
    pub fn not_covered(plugin: &PluginManifest, enabled: bool) -> Self {
        Self::derived(plugin, enabled, None)
    }

    /// 账本 upsert 合并语义（ADR 2026-08-28 决策1：粘滞）。`self` = 新观测，
    /// `old` = 账本既有记录。
    ///
    /// - `enabled`/`last_scan_ts`/dep/runtime 计数以新观测为准（当前事实）；
    /// - 新观测为弱信号（`not_covered`/`verify_incomplete`）时，旧记录的
    ///   drift/sanitized/verify_incomplete 证据（g2_consistency + last_error +
    ///   rejected_tools + 净化证据）保留——处置与观测失败不得销毁既有判定证据；
    /// - 新观测为 `ok` = 显式复验通过：整体换新并记 `reverified_ts`（唯一清除口）；
    /// - 新观测为 `drift`/`sanitized`（新判定）：g2 判定面换新；旧净化证据在新
    ///   判定不带证据时保留（净化是既成事实）。
    fn merged_over(self, old: PluginContractState) -> PluginContractState {
        let old_disk_diffs = old.gates.registry_disk_diffs.clone();
        if self.gates.g2_consistency == "ok" {
            // 显式复验通过：g2 面唯一清除口，记复验时间戳。注册表↔磁盘检出有
            // 自己的清除口（复检一致），不随 g2 ok 清除。
            let mut cleared = self;
            cleared.gates.reverified_ts = Some(cleared.last_scan_ts);
            if cleared.gates.registry_disk_diffs.is_none() {
                cleared.gates.registry_disk_diffs = old_disk_diffs;
            }
            return cleared;
        }
        let new_is_weak = matches!(
            self.gates.g2_consistency.as_str(),
            "not_covered" | "verify_incomplete"
        );
        let old_has_evidence = matches!(
            old.gates.g2_consistency.as_str(),
            "drift" | "sanitized" | "verify_incomplete"
        );
        if new_is_weak && old_has_evidence {
            let mut kept = old;
            kept.enabled = self.enabled;
            kept.last_scan_ts = self.last_scan_ts;
            kept.gates.manifest_schema_valid = self.gates.manifest_schema_valid;
            kept.gates.dep_ok = self.gates.dep_ok;
            kept.gates.runtime_input_violations = self.gates.runtime_input_violations;
            kept.gates.runtime_output_violations = self.gates.runtime_output_violations;
            if self.gates.registry_disk_diffs.is_some() {
                kept.gates.registry_disk_diffs = self.gates.registry_disk_diffs;
            }
            return kept;
        }
        let mut out = self;
        if out.gates.sanitized.is_none() {
            out.gates.sanitized = old.gates.sanitized;
        }
        if out.gates.registry_disk_diffs.is_none() {
            out.gates.registry_disk_diffs = old_disk_diffs;
        }
        out
    }
}

/// G2 结果是否为"实际净化"（ADR 决策2）：被拒工具非空且 outcome 携带的 manifest
/// 工具数少于声明——manifest 已被替换为净化版。validate-all 巡检（manifest 原样
/// 回传、未处置）不满足，仍标 `drift`。
fn is_sanitized(declared: &PluginManifest, o: &G2VerifyOutcome) -> bool {
    !o.spawn_failed
        && !o.rejected_tools.is_empty()
        && o.manifest.capabilities.tools.len() < declared.capabilities.tools.len()
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

    /// 写入插件契约状态（upsert 合并语义，ADR 2026-08-28 决策1：粘滞）：
    /// 弱信号（not_covered/verify_incomplete）不覆盖既有 drift/sanitized 证据；
    /// 清除仅经显式复验通过（`ok`，记 `reverified_ts`）。见
    /// [`PluginContractState::merged_over`]。
    pub fn upsert(&self, st: PluginContractState) {
        let mut g = self.inner.write();
        match g.remove(&st.plugin_id) {
            Some(old) => {
                let merged = st.merged_over(old);
                g.insert(merged.plugin_id.clone(), merged);
            }
            None => {
                g.insert(st.plugin_id.clone(), st);
            }
        }
    }

    /// 记录注册表↔磁盘 manifest 一致性检出结果（validate-all，ADR 决策3）。
    /// `Some(diffs)` 覆盖旧检出（空 vec = 复检一致，显式清除）；
    /// `None` = 磁盘 manifest 不可读、本轮未检出——不写（不得因读不到盘抹掉
    /// 已有差异证据）。账本无该插件条目时以未覆盖缺省建条。
    pub fn record_registry_disk_diffs(&self, plugin_id: &str, diffs: Vec<RegistryDiskDiff>) {
        let mut g = self.inner.write();
        match g.get_mut(plugin_id) {
            Some(st) => st.gates.registry_disk_diffs = Some(diffs),
            None => {
                g.insert(
                    plugin_id.to_string(),
                    PluginContractState {
                        plugin_id: plugin_id.to_string(),
                        enabled: false,
                        gates: ContractGates {
                            manifest_schema_valid: true,
                            dep_ok: true,
                            g2_consistency: "not_covered".to_string(),
                            smoke_result: "not_covered".to_string(),
                            render_decl_valid: "n/a".to_string(),
                            runtime_input_violations: 0,
                            runtime_output_violations: 0,
                            last_error: None,
                            rejected_tools: Vec::new(),
                            sanitized: None,
                            reverified_ts: None,
                            registry_disk_diffs: Some(diffs),
                        },
                        last_scan_ts: now_ms(),
                    },
                );
            }
        }
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

/// 注册表 manifest vs 磁盘 manifest 差异检出（ADR 2026-08-28 决策3，纯函数）。
///
/// 对比工具名集合 + 同名工具的 input/output schema（`serde_json::Value` 相等性，
/// 与键序无关）。净化、热改导致的注册表静默降级由此机检。空 vec = 完全一致。
pub fn registry_disk_diffs(
    registry: &PluginManifest,
    disk: &PluginManifest,
) -> Vec<RegistryDiskDiff> {
    let reg_tools: HashMap<&str, &ToolCapability> = registry
        .capabilities
        .tools
        .iter()
        .map(|t| (t.name.as_str(), t))
        .collect();
    let disk_tool_names: std::collections::HashSet<&str> = disk
        .capabilities
        .tools
        .iter()
        .map(|t| t.name.as_str())
        .collect();
    let mut diffs = Vec::new();
    for t in &disk.capabilities.tools {
        match reg_tools.get(t.name.as_str()) {
            None => diffs.push(RegistryDiskDiff {
                kind: "missing_tool".to_string(),
                tool: t.name.clone(),
                detail: "注册表 manifest 缺少磁盘声明的工具（净化剔除或热改丢失）".to_string(),
            }),
            Some(rt) => {
                if rt.input_schema != t.input_schema {
                    diffs.push(RegistryDiskDiff {
                        kind: "schema_diff".to_string(),
                        tool: t.name.clone(),
                        detail: "input_schema 与磁盘声明不一致".to_string(),
                    });
                }
                if rt.output_schema != t.output_schema {
                    diffs.push(RegistryDiskDiff {
                        kind: "schema_diff".to_string(),
                        tool: t.name.clone(),
                        detail: "output_schema 与磁盘声明不一致".to_string(),
                    });
                }
            }
        }
    }
    for t in &registry.capabilities.tools {
        if !disk_tool_names.contains(t.name.as_str()) {
            diffs.push(RegistryDiskDiff {
                kind: "extra_tool".to_string(),
                tool: t.name.clone(),
                detail: "注册表 manifest 含磁盘未声明的工具".to_string(),
            });
        }
    }
    diffs
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
    fn spawn_failure_is_verify_incomplete_not_drift() {
        let st = PluginContractState::derived(&manifest("a"), true, Some(&outcome(false, true)));
        assert_eq!(st.gates.g2_consistency, "verify_incomplete");
        assert!(
            st.gates.last_error.is_some(),
            "观测失败可见：校验未完成待复验"
        );
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

    /// 带 tools 的 manifest（净化/一致性检出场景用）。
    fn manifest_with_tools(id: &str, tools: &[&str]) -> PluginManifest {
        let tools_json: Vec<serde_json::Value> = tools
            .iter()
            .map(|t| serde_json::json!({"name": t, "description": t}))
            .collect();
        serde_json::from_value(serde_json::json!({
            "id": id, "name": id, "version": "1.0.0",
            "plugin_type": "tool", "language": "python",
            "host_type": "sidecar", "entry": format!("python3 {id}.py"),
            "capabilities": { "tools": tools_json },
        }))
        .expect("valid manifest")
    }

    /// 净化型 outcome：rejected 非空且 manifest 已替换为净化版（工具变少）。
    fn sanitized_outcome(keep: &[&str], rejected: &[&str]) -> G2VerifyOutcome {
        let mut o = outcome(true, false);
        o.manifest = manifest_with_tools("x", keep);
        o.rejected_tools = rejected.iter().map(|s| s.to_string()).collect();
        o
    }

    #[test]
    fn sanitized_outcome_records_evidence() {
        let declared = manifest_with_tools("a", &["t1", "t2"]);
        let o = sanitized_outcome(&["t1"], &["t2"]);
        let st = PluginContractState::derived(&declared, true, Some(&o));
        assert_eq!(st.gates.g2_consistency, "sanitized");
        let ev = st.gates.sanitized.as_ref().expect("净化必留证据");
        assert_eq!(ev.tools_before, 2);
        assert_eq!(ev.tools_after, 1);
        assert_eq!(ev.rejected_tools, vec!["t2".to_string()]);
        assert!(ev.sanitized_ts > 0, "净化时间戳必填");
        assert_eq!(st.gates.rejected_tools, vec!["t2".to_string()]);
        assert!(st.gates.last_error.unwrap().contains("t2"));
    }

    #[test]
    fn inspection_outcome_without_disposal_stays_drift() {
        // validate-all 巡检：manifest 原样回传（未处置）——漂移但不是净化
        let declared = manifest_with_tools("a", &["t1"]);
        let mut o = sanitized_outcome(&[], &["t1"]);
        o.manifest = declared.clone();
        let st = PluginContractState::derived(&declared, true, Some(&o));
        assert_eq!(st.gates.g2_consistency, "drift");
        assert!(st.gates.sanitized.is_none());
        assert_eq!(st.gates.rejected_tools, vec!["t1".to_string()]);
    }

    #[test]
    fn not_covered_upsert_keeps_drift_evidence() {
        let l = ContractLedger::new();
        let mut o = outcome(true, false);
        o.rejected_tools = vec!["task_manage".into()];
        l.upsert(PluginContractState::derived(&manifest("a"), true, Some(&o)));
        // 事故场景：净化后无工具可验，后续写入路径按"无工具插件"写 not_covered
        l.upsert(PluginContractState::not_covered(&manifest("a"), false));
        let st = l.get("a").expect("账本必须有记录");
        assert_eq!(st.gates.g2_consistency, "drift", "弱信号不得覆盖 drift");
        assert!(st.gates.last_error.unwrap().contains("task_manage"));
        assert!(!st.enabled, "enabled 以新观测为准");
    }

    #[test]
    fn not_covered_upsert_keeps_sanitized_evidence() {
        let l = ContractLedger::new();
        let declared = manifest_with_tools("a", &["t1", "t2"]);
        l.upsert(PluginContractState::derived(
            &declared,
            true,
            Some(&sanitized_outcome(&["t1"], &["t2"])),
        ));
        l.upsert(PluginContractState::not_covered(&declared, true));
        let st = l.get("a").expect("账本必须有记录");
        assert_eq!(st.gates.g2_consistency, "sanitized");
        let ev = st.gates.sanitized.as_ref().expect("净化证据粘滞保留");
        assert_eq!(ev.tools_before, 2);
        assert_eq!(ev.tools_after, 1);
    }

    #[test]
    fn explicit_ok_clears_drift_with_reverified_ts() {
        let l = ContractLedger::new();
        let mut o = outcome(true, false);
        o.rejected_tools = vec!["t1".into()];
        l.upsert(PluginContractState::derived(&manifest("a"), true, Some(&o)));
        // 显式复验通过 = 唯一清除口
        l.upsert(PluginContractState::derived(
            &manifest("a"),
            true,
            Some(&outcome(false, false)),
        ));
        let st = l.get("a").expect("账本必须有记录");
        assert_eq!(st.gates.g2_consistency, "ok");
        assert!(st.gates.reverified_ts.is_some(), "清除必留复验时间戳");
        assert!(st.gates.last_error.is_none());
        assert!(st.gates.rejected_tools.is_empty());
    }

    #[test]
    fn verify_incomplete_does_not_erase_drift() {
        let l = ContractLedger::new();
        let mut o = outcome(true, false);
        o.rejected_tools = vec!["t1".into()];
        l.upsert(PluginContractState::derived(&manifest("a"), true, Some(&o)));
        l.upsert(PluginContractState::derived(
            &manifest("a"),
            true,
            Some(&outcome(false, true)),
        ));
        let st = l.get("a").expect("账本必须有记录");
        assert_eq!(st.gates.g2_consistency, "drift", "观测失败不抹既有判定");
        assert!(st.gates.last_error.unwrap().contains("t1"));
    }

    #[test]
    fn registry_disk_diffs_ledger_semantics() {
        let l = ContractLedger::new();
        l.upsert(PluginContractState::not_covered(&manifest("a"), true));
        l.record_registry_disk_diffs(
            "a",
            vec![RegistryDiskDiff {
                kind: "missing_tool".to_string(),
                tool: "t1".to_string(),
                detail: "d".to_string(),
            }],
        );
        assert_eq!(
            l.get("a")
                .unwrap()
                .gates
                .registry_disk_diffs
                .as_ref()
                .expect("差异必须留痕")
                .len(),
            1
        );
        // g2 复验 ok 不清除（注册表↔磁盘检出的清除口是复检一致）
        l.upsert(PluginContractState::derived(
            &manifest("a"),
            true,
            Some(&outcome(false, false)),
        ));
        assert!(l.get("a").unwrap().gates.registry_disk_diffs.is_some());
        // 复检一致 → 显式清空
        l.record_registry_disk_diffs("a", Vec::new());
        assert!(
            l.get("a")
                .unwrap()
                .gates
                .registry_disk_diffs
                .expect("Some(空) = 已检一致，区别于未检出")
                .is_empty()
        );
    }

    #[test]
    fn registry_disk_diffs_detects_set_and_schema() {
        // 工具集差异：磁盘有注册表无（净化剔除主信号）+ 注册表有磁盘无
        let reg = manifest_with_tools("a", &["t1", "t9"]);
        let disk = manifest_with_tools("a", &["t1", "t2"]);
        let mut d = registry_disk_diffs(&reg, &disk);
        assert!(d.iter().any(|x| x.kind == "missing_tool" && x.tool == "t2"));
        assert!(d.iter().any(|x| x.kind == "extra_tool" && x.tool == "t9"));
        // schema 摘要差异（同名工具）
        let mut reg2 = manifest_with_tools("a", &["t1"]);
        let mut disk2 = manifest_with_tools("a", &["t1"]);
        reg2.capabilities.tools[0].input_schema = Some(serde_json::json!({"type": "object"}));
        disk2.capabilities.tools[0].input_schema = Some(serde_json::json!({"type": "string"}));
        let d2 = registry_disk_diffs(&reg2, &disk2);
        assert_eq!(d2.len(), 1);
        assert_eq!(d2[0].kind, "schema_diff");
        assert_eq!(d2[0].tool, "t1");
        // 完全一致 → 空
        let same = manifest_with_tools("a", &["t1"]);
        assert!(registry_disk_diffs(&same, &same).is_empty());
    }

    #[test]
    fn serializes_sanitized_evidence_for_frontend() {
        let declared = manifest_with_tools("a", &["t1", "t2"]);
        let st = PluginContractState::derived(
            &declared,
            true,
            Some(&sanitized_outcome(&["t1"], &["t2"])),
        );
        let v = serde_json::to_value(&st).unwrap();
        let gates = v["gates"].as_object().expect("gates 必须是嵌套对象");
        assert_eq!(gates["g2_consistency"], "sanitized");
        assert_eq!(gates["sanitized"]["tools_before"], 2);
        assert_eq!(gates["sanitized"]["tools_after"], 1);
        assert_eq!(
            gates["rejected_tools"].as_array().expect("数组").len(),
            1
        );
    }
}

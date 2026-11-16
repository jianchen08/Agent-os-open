//! 插件生命周期管理面（routes 子模块）：宿主合宿状态、盘上差异对账、
//! validate-all、启停与级联禁用、profile 文档读写与冷/热路径。处理函数经
//! 父模块 glob 再导出，既有 `crate::routes::*` 引用路径不变。
use std::sync::Arc;

use agentos_core::traits::{
    CapabilityRegistry, HostBoxesSnapshot, PendingSpawnEntry, PluginManifest,
};
use serde::Deserialize;
use serde_json::json;

use agentos_invoker::verify::{compare_tools, declared_with_services, parse_actual_tools};
use agentos_plugin_loader::CapabilityRegistryImpl;

use super::{config_err_to_api, AppState};
use crate::config_service::{
    resolve_kernel_config_target, seed_user_config_from_factory, ConfigTargetMode,
};
use crate::metrics::plugin_widget_broadcast::remove_plugin_bindings;
use agentos_http::error::ApiError;

/// GET /api/v1/plugins/hosts — 宿主盒子快照（宿主长跑劣化修复方案批次C 观测面）。
///
/// 同步读取 invoker 既有记账锁组装（只读：不 spawn、不驱逐、不 touch），
/// pid/alive/uptime_secs 复用 host_proc_snapshots 采集面，commit_mb 由 api 侧
/// 按 pid 富化（轮询同款跳板下探 + collect_memory_commit；HTTP transport 无
/// pid 恒 null）。pending_spawn 经 api 侧差集补全：invoker 记账面可见条目
/// （携带真实 last_call_at）与「AppState 启用清单（L1 真值源
/// `enabled_plugin_ids`）− 盒子成员全集」的差集合并——从未触达 invoker 的
/// 启用插件也进「待装载」区（metrics_admin 型缺口的观测闭环）；plugin_id
/// 去重、invoker 条目优先、按 plugin_id 排序，字段形状不变。
/// 响应契约字段冻结（前端宿主盒子视图逐字消费）。
/// 鉴权走 `/api/v1/plugins*` 白名单读面（与 memstats 同级同法：GET →
/// admin/viewer）。invoker 未装配（裸测试构造）返回空快照——只读诊断面
/// 诚实表达「无盒子」。
pub async fn plugins_hosts_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<HostBoxesSnapshot> {
    let mut snapshot = match state.invoker.as_ref() {
        Some(invoker) => invoker.host_boxes_snapshot().await,
        None => HostBoxesSnapshot::default(),
    };
    crate::metrics::proc_state::enrich_host_boxes_rss(&mut snapshot.hosts);
    let boxed_ids: std::collections::HashSet<String> = snapshot
        .hosts
        .iter()
        .flat_map(|h| h.members.iter().map(|m| m.plugin_id.clone()))
        .collect();
    let enabled_ids = state.enabled_plugin_ids.read().await.clone();
    snapshot.pending_spawn = merge_pending_spawn(snapshot.pending_spawn, &enabled_ids, &boxed_ids);
    axum::Json(snapshot)
}

/// pending_spawn 差集补全（批次C1 契约接缝补刀）。
///
/// invoker 侧 pending_spawn 只含记账面可见 id——「已启用、从未被调用、
/// 进程表无行」的插件不列，恰漏观测面核心场景（metrics_admin 型事故）。
/// 本函数把 AppState 启用清单对盒子成员全集的差集并入：与 invoker 条目按
/// plugin_id 去重合并（invoker 条目优先——携带真实 last_call_at 与记账
/// enabled 面），补入条目固定 `enabled=true` / `last_call_at=null`。已装载
/// 插件（在任一盒子 members 中）不因启用清单而重复出现。输出按 plugin_id
/// 排序（BTreeMap 收敛），前端契约形状不变。
pub(super) fn merge_pending_spawn(
    invoker_entries: Vec<PendingSpawnEntry>,
    enabled_ids: &std::collections::HashSet<String>,
    boxed_ids: &std::collections::HashSet<String>,
) -> Vec<PendingSpawnEntry> {
    let mut merged: std::collections::BTreeMap<String, PendingSpawnEntry> = invoker_entries
        .into_iter()
        .map(|e| (e.plugin_id.clone(), e))
        .collect();
    for plugin_id in enabled_ids {
        if boxed_ids.contains(plugin_id) {
            continue;
        }
        merged
            .entry(plugin_id.clone())
            .or_insert_with(|| PendingSpawnEntry {
                plugin_id: plugin_id.clone(),
                enabled: true,
                last_call_at: None,
            });
    }
    merged.into_values().collect()
}

/// 读磁盘 plugin.json 并与注册表 manifest 做工具集/schema 差异比对
/// （ADR 2026-08-28 决策3，[`crate::contract::registry_disk_diffs`] 纯函数消费）。
/// 返回（`Some(diffs)` = 已检出（空 vec = 一致），`None` = 磁盘不可读）+
/// 不可读原因。磁盘不可读不是"一致"——不可静默当绿。
fn read_disk_manifest_diffs(
    state: &AppState,
    m: &PluginManifest,
) -> (
    Option<Vec<crate::contract::RegistryDiskDiff>>,
    Option<String>,
) {
    let Some(root) = state.plugin_dir(&m.id) else {
        return (
            None,
            Some("无插件目录映射（loader 路径表未登记），无法读取磁盘 manifest".to_string()),
        );
    };
    let raw = match std::fs::read_to_string(root.join("plugin.json")) {
        Ok(r) => r,
        Err(e) => return (None, Some(format!("磁盘 plugin.json 读取失败: {e}"))),
    };
    let disk: PluginManifest = match serde_json::from_str(&raw) {
        Ok(d) => d,
        Err(e) => return (None, Some(format!("磁盘 plugin.json 解析失败: {e}"))),
    };
    (Some(crate::contract::registry_disk_diffs(m, &disk)), None)
}

/// POST /api/v1/plugins/validate-all — G2 双写一致性全量巡检。
///
/// 对照每个 tool 插件的 manifest 声明（`capabilities.tools`）与 sidecar 实际上报
/// （MCP `tools/list`）——工具名集合 + 参数 schema。漂移分类：
/// `missing`（声明有实际无）/ `undeclared`（实际有声明无）/ `schema_mismatch`。
///
/// 语义：spawn → 校验 → 回收（新 spawn 的连接校验后 kill，不破坏懒加载）；
/// 校验失败（spawn 失败 / host 不支持）不阻断，插件标记 `error` 并继续。
/// 结果报告 + **写闸2·观测账本**（`state.contract_states`，处置=安装路径的
/// plugin_watcher 拒绝注册已做；此处为人工巡检的收口，见契约闸门方案 Phase2）。
/// 另做注册表 manifest ↔ 磁盘 manifest 一致性检出（ADR 2026-08-28 决策3）：
/// 差异为独立 `consistency_reports` 报告项 + 账本留痕。
pub async fn validate_all_plugins_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<serde_json::Value> {
    let Some(invoker) = state.invoker.clone() else {
        return axum::Json(json!({
            "checked": 0, "clean": 0, "drifted": 0, "errors": 1,
            "registry_disk_mismatches": 0,
            "message": "invoker 未接线（validate-all 不可用）",
            "reports": [],
            "consistency_reports": [],
        }));
    };
    let enabled_ids = state.enabled_plugin_ids.read().await.clone();
    let ledger = state.contract_states.clone();
    let mut reports: Vec<serde_json::Value> = Vec::new();
    let mut consistency_reports: Vec<serde_json::Value> = Vec::new();
    let mut clean = 0usize;
    let mut drifted = 0usize;
    let mut errors = 0usize;
    let mut registry_disk_mismatches = 0usize;
    for m in state.manifests.read().await.iter() {
        let enabled = enabled_ids.contains(&m.id);
        // ADR 决策3：注册表 manifest ↔ 磁盘 manifest 一致性检出——净化/热改
        // 导致的注册表静默降级机检，差异为独立报告项 + 账本留痕。
        let (disk_diffs, disk_unreadable) = read_disk_manifest_diffs(&state, m);
        match (&disk_diffs, &disk_unreadable) {
            (Some(diffs), _) if diffs.is_empty() => {
                ledger.record_registry_disk_diffs(&m.id, Vec::new());
            }
            (Some(diffs), _) => {
                registry_disk_mismatches += 1;
                ledger.record_registry_disk_diffs(&m.id, diffs.clone());
                consistency_reports.push(json!({
                    "plugin_id": m.id,
                    "status": "registry_disk_mismatch",
                    "diffs": diffs,
                }));
            }
            (None, Some(reason)) => {
                consistency_reports.push(json!({
                    "plugin_id": m.id,
                    "status": "disk_manifest_unreadable",
                    "reason": reason,
                }));
            }
            (None, None) => {}
        }
        if m.capabilities.tools.is_empty() {
            // 非 tool 插件无工具可对照：登记 not_covered 缺省（诚实标未覆盖）
            ledger.upsert(crate::contract::PluginContractState::not_covered(
                m, enabled,
            ));
            continue;
        }
        if m.host_type != agentos_core::traits::HostType::Sidecar {
            reports.push(json!({
                "plugin_id": m.id,
                "status": "skipped",
                "reason": format!("host_type {:?} 暂无 describe 通道（G2 渐进落地）", m.host_type),
                "mismatches": [],
            }));
            ledger.upsert(crate::contract::PluginContractState::not_covered(
                m, enabled,
            ));
            continue;
        }
        match invoker.list_plugin_tools(&m.id).await {
            Ok(raw) => {
                let (mut actual, malformed) = parse_actual_tools(&raw);
                // 合宿成员观测域化（BUG-51 验证链，与 g2_verify_and_sanitize 同判）：
                // 宿主 tools/list 是全组聚合面，剥本成员前缀 + 剔他组前缀条目——
                // 否则成员的声明工具恒误报 missing（虚假"剔除工具"判定）。
                if agentos_invoker::is_cohost_member(m) {
                    actual = crate::plugin_watcher::member_scoped_actual_tools(actual, &m.id);
                }
                let mismatches = compare_tools(&declared_with_services(m), &actual);
                let items: Vec<serde_json::Value> = mismatches
                    .iter()
                    .map(|mm| match mm {
                        agentos_invoker::verify::VerifyMismatch::Missing { name } => {
                            json!({"kind": "missing", "tool": name})
                        }
                        agentos_invoker::verify::VerifyMismatch::Undeclared { name } => {
                            json!({"kind": "undeclared", "tool": name})
                        }
                        agentos_invoker::verify::VerifyMismatch::SchemaMismatch {
                            name,
                            declared,
                            actual,
                        } => {
                            json!({
                                "kind": "schema_mismatch",
                                "tool": name,
                                "declared_schema": declared,
                                "actual_schema": actual,
                            })
                        }
                    })
                    .collect();
                if mismatches.is_empty() {
                    clean += 1;
                } else {
                    drifted += 1;
                }
                // 写健康度：被拒工具 = missing/schema_mismatch（undeclared 仅记录不拒）
                let rejected: Vec<String> = mismatches
                    .iter()
                    .filter_map(|mm| match mm {
                        agentos_invoker::verify::VerifyMismatch::Missing { name } => {
                            Some(name.clone())
                        }
                        agentos_invoker::verify::VerifyMismatch::SchemaMismatch {
                            name, ..
                        } => Some(name.clone()),
                        agentos_invoker::verify::VerifyMismatch::Undeclared { .. } => None,
                    })
                    .collect();
                let g2o = crate::plugin_watcher::G2VerifyOutcome {
                    manifest: m.clone(),
                    rejected_tools: rejected,
                    drift: !mismatches.is_empty(),
                    spawn_failed: false,
                    smoke_failed: false,
                };
                ledger.upsert(crate::contract::PluginContractState::derived(
                    m,
                    enabled,
                    Some(&g2o),
                ));
                reports.push(json!({
                    "plugin_id": m.id,
                    "status": if mismatches.is_empty() { "clean" } else { "drifted" },
                    "declared_tools": m.capabilities.tools.len(),
                    "actual_tools": actual.len(),
                    "malformed_items": malformed,
                    "mismatches": items,
                }));
            }
            Err(e) => {
                errors += 1;
                // 写健康度：spawn/上报不可用 = not_covered（不是验出漂移），记 reason
                let mut st = crate::contract::PluginContractState::not_covered(m, enabled);
                st.gates.last_error = Some(e.message.clone());
                ledger.upsert(st);
                reports.push(json!({
                    "plugin_id": m.id,
                    "status": "error",
                    "reason": e.message,
                    "mismatches": [],
                }));
            }
        }
    }
    axum::Json(json!({
        "checked": reports.len(),
        "clean": clean,
        "drifted": drifted,
        "errors": errors,
        "registry_disk_mismatches": registry_disk_mismatches,
        "reports": reports,
        "consistency_reports": consistency_reports,
    }))
}

/// GET /api/v1/plugins/contract-status — 闸2·观测：每插件契约状态（只读）。
///
/// 只读账本（boot/热发现/reenable/validate-all 写入，`state.contract_states`）；
/// 请求时不重跑校验（"结果前置复用"，契约闸门方案 §1.7）。未登记插件补
/// `not_covered` 缺省（诚实标注未覆盖，不假装绿）。响应 `{ plugins, ... }`
/// 信封，前端 `parseContractStatus` 兼容。
pub async fn plugins_contract_status_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<serde_json::Value> {
    let enabled_ids = state.enabled_plugin_ids.read().await.clone();
    let manifests = state.manifests.read().await.clone();
    let items =
        crate::contract::contract_statuses(&state.contract_states, &manifests, &enabled_ids);
    axum::Json(json!({
        "plugins": items,
        "count": items.len(),
        "generated_at": crate::contract::now_ms(),
    }))
}

/// GET /api/v1/plugins/source-arbitration — 多源仲裁「用户副本未生效」清单
/// （D4/审查总册 P1-15：BuiltinFirst 装机盲区显式告警面）。
///
/// sync 工作流写坏版本号（如 `v` 前缀 strip 后仍非 semver / 不严格大于内置）
/// 时用户副本输给内置，此前仅裁决日志逐 id 汇总行可查——本端点把该清单
/// 结构化透出（`ineffective_user_copies`，语义见
/// `agentos_plugin_loader::IneffectiveUserCopy`）。清单每轮 discover 全量
/// 重建，用户副本修复版本后收敛为空。loader 未装配（裸测试构造）→ 空清单
/// + 默认策略标签。
pub async fn plugins_source_arbitration_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<serde_json::Value> {
    let (policy, ineffective) = match &state.plugin_loader {
        Some(loader) => {
            let label = match loader.dual_source_policy() {
                agentos_plugin_loader::DualSourcePolicy::BuiltinFirst => "builtin",
                agentos_plugin_loader::DualSourcePolicy::UserFirst => "user",
            };
            (label, loader.ineffective_user_copies())
        }
        None => ("user", Vec::new()),
    };
    axum::Json(json!({
        "policy": policy,
        "ineffective_user_copies": ineffective,
        "count": ineffective.len(),
    }))
}

/// GET /api/v1/plugins — 从 manifests 派生插件状态列表。
pub async fn plugins_status_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> axum::Json<serde_json::Value> {
    let enabled_ids = state.enabled_plugin_ids.read().await;
    // BUG-51 注册门禁：验证未完成/验证失败插件的可见原因（契约账本 last_error
    // 经 error 字段透出，插件管理红框展示）。漂移/sanitized 有专属账本标示面，
    // 不占 error（口径：error = 必败风险提示，判定证据走契约状态页）。
    let verify_errors: std::collections::HashMap<String, String> = state
        .contract_states
        .snapshot()
        .into_iter()
        .filter(|st| {
            matches!(
                st.gates.g2_consistency.as_str(),
                "verify_incomplete" | "verify_failed"
            )
        })
        .filter_map(|st| st.gates.last_error.map(|e| (st.plugin_id, e)))
        .collect();
    let items: Vec<serde_json::Value> = state
        .manifests
        .read()
        .await
        .iter()
        .map(|m| {
            let config_type = match m.plugin_type {
                agentos_core::traits::PluginType::System => "system",
                agentos_core::traits::PluginType::Pipeline => "pipeline",
                agentos_core::traits::PluginType::Tool => "tool",
                agentos_core::traits::PluginType::Composite => "composite",
            };
            let enabled = enabled_ids.contains(&m.id);
            // 运行态：enabled 且是 sidecar → Active（按需 lazy 时实为 Idle，但无法从静态状态区分，
            // 统一标 active）；enabled 非 sidecar → active；disabled → disabled
            let run_status = if enabled { "active" } else { "disabled" };
            let activation = match m.activation {
                Some(agentos_core::traits::ActivationPolicy::Eager) => "eager",
                Some(agentos_core::traits::ActivationPolicy::Manual) => "manual",
                _ => "lazy", // None = 走 default_profile 默认 lazy
            };
            let host_type = match m.host_type {
                agentos_core::traits::HostType::InProcess => "in_process",
                agentos_core::traits::HostType::Sidecar => "sidecar",
            };
            json!({
                "plugin_id": m.id,
                "name": m.name,
                "description": m.description,
                "config_type": config_type,
                "host_type": host_type,
                "version": m.version,
                "enabled": enabled,
                "activation": activation,
                "status": run_status,
                "config_files": m.config_files.iter().filter(|c| c.settings.unwrap_or(true)).map(|c| json!({
                    "id": c.id,
                    "label": c.label,
                    "path": c.path,
                })).collect::<Vec<_>>(),
                "has_contributes": m.contributes.is_some(),
                "has_http_endpoints": !m.http_endpoints.is_empty(),
                // 准入分级（2026-09-25）：被剥除的危险前端能力（host_js/host_css），
                // 非空 = 设置页插件卡显示"受限"（禁静默降级）
                "restricted_capabilities": m.restricted_capabilities,
                "error": verify_errors.get(&m.id),
            })
        })
        .collect();
    axum::Json(json!(items))
}

/// PUT /api/v1/plugins/{id}/enabled — 切换插件启用状态（写 default_profile.yaml）。
///
/// 安装触发模型 L1：改 profile 文件后热更新内存状态；启用需重启内核完全生效
/// （axum 路由树启动期固定），禁用立即生效（摘除 capability registry）。
/// 返回 {success, enabled, restart_required}。
pub async fn plugins_set_enabled_handler(
    axum::extract::Path(plugin_id): axum::extract::Path<String>,
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::Json(body): axum::Json<EnabledBody>,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    let new_enabled = body.enabled;
    let project_root = match &state.project_root {
        Some(p) => p,
        None => {
            return Err(ApiError::Internal {
                message: "project_root not available".to_string(),
            })
        }
    };
    // 落点用户空间优先（ADR 2026-09-13-unified-user-root）：启停开关是用户资产，
    // 改动出仓——覆写 git 跟踪的 factory 文件会让它处于工作区还原抹除风险面内。
    // 读（PluginEnablement::load）与写共用同一解析器，两侧同源。
    let profile_path = resolve_kernel_config_target(
        project_root,
        "kernel/default_profile.yaml",
        ConfigTargetMode::Write,
    )
    .map_err(config_err_to_api)?;

    // 首次接管先播种 factory 整个 profile：否则下面 load_profile_doc 对"用户层
    // 尚无此文件"返回空 Mapping，本次只补一个插件条目就写盘——factory 里其他
    // 插件的启停/激活策略被整份静默丢弃（用户只关了一个插件，却重置了全部）。
    seed_user_config_from_factory(project_root, "kernel/default_profile.yaml")
        .map_err(config_err_to_api)?;

    let mut doc = load_profile_doc(&profile_path)?;
    apply_enabled_patch(&mut doc, &plugin_id, new_enabled);

    // 写回：序列化失败直接报错（K1：不得 unwrap_or_default() 把空串写盘，
    // 物理清空整个 profile——序列化对 Mapping 几乎不会失败，但兜底不得是破坏性写）。
    let new_raw = serde_yaml::to_string(&doc)
        .map_err(ApiError::internal("序列化 default_profile.yaml 失败："))?;
    // A12：写盘失败 → 5xx 统一错误信封（不再 200 + success:false 混装，
    // 前端无法据状态码区分"已生效"与"根本没写进去"）。
    // B4：tmp + rename 原子写（对照同文件写 plugin.json 的范式）——直写被中断
    // （进程退出/断电）会留半截 yaml，后续 load_profile_doc 解析失败连锁拒写。
    //
    // 父目录补齐：写落点在用户空间时 `<USER_ROOT>/config/plugins/` 可能尚不存在
    // （factory 时代该目录随仓库必有），不补会 os error 3。
    if let Some(parent) = profile_path.parent() {
        std::fs::create_dir_all(parent).map_err(ApiError::internal("创建 profile 目录失败"))?;
    }
    let tmp_path = profile_path.with_extension("yaml.tmp");
    std::fs::write(&tmp_path, new_raw).map_err(|e| {
        tracing::error!(
            target: "plugin-enablement",
            plugin_id = %plugin_id,
            error = %e,
            "写入 profile tmp 失败"
        );
        ApiError::Internal {
            message: format!("写入 profile 失败: {e}"),
        }
    })?;
    if let Err(e) = std::fs::rename(&tmp_path, &profile_path) {
        // rename 失败 best-effort 清 tmp（不留 .tmp 残骸）
        let _ = std::fs::remove_file(&tmp_path);
        tracing::error!(
            target: "plugin-enablement",
            plugin_id = %plugin_id,
            error = %e,
            "替换 profile 失败"
        );
        return Err(ApiError::Internal {
            message: format!("写入 profile 失败: {e}"),
        });
    }

    // ── 热加载：立即改内存状态，不用重启 ──
    // 1) 改 enabled_plugin_ids（schema 出口的 contributes/configs 立即生效）
    {
        let mut ids = state.enabled_plugin_ids.write().await;
        if new_enabled {
            ids.insert(plugin_id.clone());
        } else {
            ids.remove(&plugin_id);
        }
    }
    // 2) 注册表对称热更新（G1 enable 对称化 + M1 scope 结构性收回）：
    //    禁用 → scope revoke（全部注册 guard 一次性收回）+ clear_plugin 兜底
    //           + broadcaster 绑定移除（零残留）；
    //    启用 → 立即重注册 tools/route_signals/http_routes（guarded，入新 scope）。
    //    /ext/{*rest} 通配分发是注册表数据驱动（http_dispatcher），路由树无需
    //    重启重建。
    let mut registered = serde_json::Value::Null;
    let mut cascade_disabled: Vec<String> = Vec::new();
    if let Some(registry) = &state.capability_registry {
        if new_enabled {
            registered = reenable_hot_path(&state, registry, &plugin_id).await;
        } else {
            disable_hot_path(&state, registry, &plugin_id).await;
            // §2.4/§2.8：禁用实时连带——依赖被禁提供者的已启用插件同步摘除
            // （fail-closed；不杀 sidecar，提供者回归由 watcher sync 自动重注册）。
            cascade_disabled = cascade_disable_dependents(&state, registry, &plugin_id).await;
        }
    }
    let restart_needed = false; // 双向即时生效（G1）
    tracing::info!(
        target: "plugin-enablement",
        "plugin {} enabled={} (hot-reloaded: contributes + registry updated, restart={})",
        plugin_id, new_enabled, restart_needed
    );
    // 剩余项清仓 D2：schema 变更推送——enable/disable 已改变 schema 聚合
    // （tools/contributes/configs），best-effort 广播 widget_event
    // {schema, changed} 让前端增量重载（前端消费见 resync.ts）。
    // 失败静默（观察层不拖垮主流程：session 未启用/无连接时 broadcast
    // 返回 0，不视为错误）。
    if let Some(session) = &state.session {
        let _ = session
            .broadcast_widget(
                "schema",
                "changed",
                json!({ "plugin_id": plugin_id, "enabled": new_enabled }),
                "kernel",
            )
            .await;
    }
    Ok(axum::Json(json!({
        "success": true,
        "plugin_id": plugin_id,
        "enabled": new_enabled,
        "restart_required": restart_needed,
        "registered": registered,
        "cascade_disabled": cascade_disabled,
        "message": if new_enabled {
            format!("已启用插件 {plugin_id}（立即生效）")
        } else if cascade_disabled.is_empty() {
            format!("已禁用插件 {plugin_id}（立即生效）")
        } else {
            format!(
                "已禁用插件 {}（立即生效）；连带摘除依赖方：{}",
                plugin_id,
                cascade_disabled.join("、")
            )
        },
    })))
}

/// 禁用实时连带（ADR 2026-09-14-config-ownership §2.4/§2.8）：提供者 P 被禁后，
/// 在「已启用面 − P」上重跑服务依赖闸，不满足的依赖方结构性收回能力
/// （scope revoke + clear_plugin + widget bindings；不杀 sidecar，提供者回归后
/// watcher sync 依赖面恢复即自动重注册）。fixpoint 收敛覆盖间接依赖方；
/// 返回连带清单（写入响应 `cascade_disabled`，前端 §2.4 提醒面板消费）。
async fn cascade_disable_dependents(
    state: &AppState,
    registry: &Arc<CapabilityRegistryImpl>,
    provider_id: &str,
) -> Vec<String> {
    use agentos_plugin_loader::ServiceSurface;

    let mut cascaded: Vec<String> = Vec::new();
    loop {
        let enabled = state.enabled_plugin_ids.read().await.clone();
        let manifests = state.manifests.read().await.clone();
        let active: Vec<agentos_core::traits::PluginManifest> = manifests
            .into_iter()
            .filter(|m| m.id != provider_id)
            .filter(|m| !cascaded.contains(&m.id))
            .filter(|m| enabled.contains(&m.id))
            .collect();
        let surface = ServiceSurface::from_manifests(&active);
        let newly: Vec<String> = active
            .iter()
            .filter(|m| !m.requires_services.is_empty())
            .filter(|m| surface.first_error_for(m).is_some())
            .map(|m| m.id.clone())
            .collect();
        if newly.is_empty() {
            break;
        }
        for id in &newly {
            cascaded.push(id.clone());
            state.plugin_scopes.revoke(id);
            if let Some(bindings) = &state.widget_bindings {
                remove_plugin_bindings(bindings, id);
            }
            registry.clear_plugin(id);
            tracing::warn!(
                target: "plugin-enablement",
                plugin = %id,
                provider = %provider_id,
                "禁用连带：依赖的服务提供者已禁用，摘除该插件能力（fail-closed，提供者回归自动重注册）"
            );
        }
    }
    cascaded.sort();
    cascaded
}

/// GET /api/v1/plugins/{id}/dependents — 反向依赖查询（§2.4 卸载/禁用事前提醒
/// 数据源）：谁依赖我的服务。`enabled` 标注当前启停态，前端据此提示
/// 「将影响 N 个已启用插件」。
pub async fn plugins_dependents_handler(
    axum::extract::Path(plugin_id): axum::extract::Path<String>,
    axum::extract::State(state): axum::extract::State<AppState>,
) -> Result<axum::Json<serde_json::Value>, ApiError> {
    let manifests = state.manifests.read().await;
    if !manifests.iter().any(|m| m.id == plugin_id) {
        return Err(ApiError::NotFound {
            message: format!("未知插件: {plugin_id}"),
        });
    }
    let surface = agentos_plugin_loader::ServiceSurface::from_manifests(&manifests);
    let dependents = surface.dependents_of(&manifests, &plugin_id);
    let enabled_ids = state.enabled_plugin_ids.read().await;
    let items: Vec<serde_json::Value> = dependents
        .iter()
        .map(|id| {
            json!({
                "id": id,
                "enabled": enabled_ids.contains(id),
            })
        })
        .collect();
    Ok(axum::Json(json!({
        "plugin_id": plugin_id,
        "dependents": items,
        "total": items.len(),
    })))
}

/// 读 default_profile.yaml 为 serde_yaml 文档。
///
/// 文件缺失/空白 → 全新 Mapping（首次落盘，无存量可破坏）；文件存在但解析
/// 失败/顶层非 Mapping → 422 拒绝写入（K1：不得用硬编码模板顶替并覆写，
/// profile 里其他插件的启停设置会被物理清空；损坏现场必须保留给运维排查，
/// 不得静默重建）。其余读失败（权限等）→ 500，同样不写。
pub(super) fn load_profile_doc(
    profile_path: &std::path::Path,
) -> Result<serde_yaml::Value, ApiError> {
    match std::fs::read_to_string(profile_path) {
        Ok(raw) if raw.trim().is_empty() => {
            Ok(serde_yaml::Value::Mapping(serde_yaml::Mapping::new()))
        }
        Ok(raw) => match serde_yaml::from_str::<serde_yaml::Value>(&raw) {
            Ok(v @ serde_yaml::Value::Mapping(_)) => Ok(v),
            Ok(_) => {
                tracing::error!(
                    path = %profile_path.display(),
                    "default_profile.yaml 顶层非 Mapping，拒绝覆写（profile corrupted）"
                );
                Err(ApiError::UnprocessableEntity {
                    message: "profile corrupted, refusing to overwrite".to_string(),
                })
            }
            Err(e) => {
                tracing::error!(
                    path = %profile_path.display(),
                    error = %e,
                    "default_profile.yaml 解析失败，拒绝覆写（profile corrupted）"
                );
                Err(ApiError::UnprocessableEntity {
                    message: "profile corrupted, refusing to overwrite".to_string(),
                })
            }
        },
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            Ok(serde_yaml::Value::Mapping(serde_yaml::Mapping::new()))
        }
        Err(e) => Err(ApiError::Internal {
            message: format!("读取 default_profile.yaml 失败：{e}"),
        }),
    }
}

/// 在 profile 文档 `plugins.<plugin_id>.enabled` 处打启用补丁。
///
/// plugins 键 / 插件条目不存在则逐级创建（手动操作 serde_yaml Mapping）。
pub(super) fn apply_enabled_patch(doc: &mut serde_yaml::Value, plugin_id: &str, new_enabled: bool) {
    if let serde_yaml::Value::Mapping(ref mut top) = *doc {
        // 确保 plugins 键存在且是 Mapping
        let plugins_key = serde_yaml::Value::String("plugins".into());
        if !top.contains_key(&plugins_key) {
            top.insert(
                plugins_key.clone(),
                serde_yaml::Value::Mapping(serde_yaml::Mapping::new()),
            );
        }
        if let Some(serde_yaml::Value::Mapping(ref mut plugins_map)) = top.get_mut(&plugins_key) {
            let pid_key = serde_yaml::Value::String(plugin_id.to_owned());
            // 确保该插件条目存在
            if !plugins_map.contains_key(&pid_key) {
                plugins_map.insert(
                    pid_key.clone(),
                    serde_yaml::Value::Mapping(serde_yaml::Mapping::new()),
                );
            }
            if let Some(serde_yaml::Value::Mapping(ref mut entry)) = plugins_map.get_mut(&pid_key) {
                entry.insert(
                    serde_yaml::Value::String("enabled".into()),
                    serde_yaml::Value::Bool(new_enabled),
                );
            }
        }
    }
}

/// 启用热路径：注册闸 G2 复核后按（净化后）manifest 重注册 tools/http_routes。
///
/// 判定失败（漂移）→ 用净化后 manifest 注册，禁止把"声明与实现不服"的能力在
/// 启用时带进来；观测失败（重试后仍 spawn/list 失败）≠ 判定失败 → 按声明注册，
/// 账本标记校验未完成。返回 registered 账目 `{tools, http_routes}`
/// （manifest 未找到 → null 并告警，不重注册）。
async fn reenable_hot_path(
    state: &AppState,
    registry: &Arc<CapabilityRegistryImpl>,
    plugin_id: &str,
) -> serde_json::Value {
    let mut registered = serde_json::Value::Null;
    match state
        .manifests
        .read()
        .await
        .iter()
        .find(|m| m.id == plugin_id)
    {
        Some(m) => {
            let mut manifest_for_register = m.clone();
            let mut g2_outcome: Option<crate::plugin_watcher::G2VerifyOutcome> = None;
            // G2 适用闸（与 boot g2_applicable 同判）：无 tools+services 的 manifest
            // 无可验面——不跑复核。净化后 0 工具 manifest 的"复核通过"是假绿，会经
            // 复验清除口销毁 drift/sanitized 证据；此处不写 g2 结果，账本 upsert 走
            // not_covered 弱信号（既有证据粘滞保留，ADR 2026-08-28 决策1）。
            if !m.capabilities.tools.is_empty() || !m.capabilities.services.is_empty() {
                if let Some(invoker) = &state.invoker {
                    let outcome =
                        crate::plugin_watcher::g2_verify_and_sanitize(invoker.as_ref(), m.clone())
                            .await;
                    g2_outcome = Some(outcome.clone());
                    if outcome.drift {
                        tracing::warn!(
                            target: "plugin-enablement",
                            plugin = %plugin_id,
                            rejected = ?outcome.rejected_tools,
                            spawn_failed = outcome.spawn_failed,
                            "注册闸 G2：启用复核判定声明与实现不一致，按净化后能力注册（需修改插件）"
                        );
                        manifest_for_register = outcome.manifest;
                    } else if outcome.spawn_failed {
                        tracing::warn!(
                            target: "plugin-enablement",
                            plugin = %plugin_id,
                            "注册闸 G2：启用复核观测失败——按声明注册，账本标记校验未完成（待复验）"
                        );
                    }
                }
            }
            // 闸2·观测：启用复核结果收口（无 invoker = not_covered 缺省）
            state
                .contract_states
                .upsert(crate::contract::PluginContractState::derived(
                    m,
                    true,
                    g2_outcome.as_ref(),
                ));
            let (tools, http_routes) = crate::plugin_lifecycle::reenable_plugin_capabilities(
                &manifest_for_register,
                registry,
                &state.plugin_scopes,
            );
            tracing::info!(
                target: "plugin-enablement",
                "plugin {} re-enabled: re-registered tools={} http_routes={}",
                plugin_id, tools, http_routes
            );
            registered = serde_json::json!({
                "tools": tools, "http_routes": http_routes
            });
        }
        None => {
            tracing::warn!(
                target: "plugin-enablement",
                "plugin {} enabled but manifest not found; nothing re-registered",
                plugin_id
            );
        }
    }
    registered
}

/// 禁用热路径：M1 scope 收回全部登记（registry 四维 + broadcaster 绑定）+
/// clear_plugin 兜底（scope 无登记的直连注册路径仍被覆盖）+ sidecar 窄口击杀。
///
/// 闸2·观测：禁用收口登记 not_covered（账本旧值作废）。sidecar 走
/// kill_sidecar_if_any（只 kill 进程 + 移除缓存，不走 force_unload 的 OnUnload
/// 广播/loader.unload/指纹清理——"仅禁用"语义下插件仍在 loader 内、热发现不
/// 失效）；sidecar 按调用懒 spawn，reenable 后下次调用自然重生。
async fn disable_hot_path(
    state: &AppState,
    registry: &Arc<CapabilityRegistryImpl>,
    plugin_id: &str,
) {
    use agentos_core::traits::CapabilityRegistry;

    if let Some(m) = state
        .manifests
        .read()
        .await
        .iter()
        .find(|m| m.id == plugin_id)
    {
        state
            .contract_states
            .upsert(crate::contract::PluginContractState::not_covered(m, false));
    }
    state.plugin_scopes.revoke(plugin_id);
    if let Some(bindings) = &state.widget_bindings {
        remove_plugin_bindings(bindings, plugin_id);
    }
    registry.clear_plugin(plugin_id);
    // G3：动态注册随 scope/clear_plugin 结构性收回（动态注册是
    // state 域数据不落内核，re-enable 后插件经 on_load/运行时自行重建）。
    if let Some(invoker) = state.invoker.as_ref() {
        invoker.kill_sidecar_if_any(plugin_id).await;
    }
}

/// PUT /api/v1/plugins/{id}/enabled 请求体。
#[derive(Debug, Deserialize)]
pub struct EnabledBody {
    pub enabled: bool,
}

// ── 监控 M5b：Prometheus 导出端点（监控设计 §十一）──
//
// boot-plugin 第三刀拆分（对齐 db-admin 模式）：查询面（原 GET /api/v1/metrics）
// 已迁 `metrics-admin` capability（metrics/capability.rs 的 query/list method，
// HTTP 面在 plugins/shared/metrics_admin 插件 /ext/metrics_admin/**）；
// /metrics 保留内核——Prometheus 抓取方通常不鉴权且是运维契约，URL 稳定优先。

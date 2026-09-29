// @feature: FP-0.2.可观测性 内核诊断端点(memory-breakdown/task-dump) | @ci: rust-test
//! 堆栈级诊断面——内核活内存按结构分解 + 任务级活动转储。
//!
//! 背景：内核 RSS 双长跑 2h 涨到 302MB；mimalloc purge_delay=0 已排除分配器
//! 囤积，内存审计（docs/working/内核内存持有点审计_20260929.md）识别了全部
//! 持有点——本模块把这些从"报告文字"变成"可查端点"：
//!
//! - [`system_memory_breakdown_handler`]：遍历内核真实持有的内存结构，逐项
//!   报条目数 + 估算字节，每项标注 `[实测]`（锁内直读计数/字节）或 `[估算]`
//!   （条目 × 平均 payload）。
//! - [`system_task_dump_handler`]：枚举全部插桩任务（
//!   `agentos_core::task_activity`）的名字/当前标签/存活时长——任务级
//!   "此刻在干什么"。
//! - memstats 快照差分：POST 记录基线，GET 返回与基线的逐字段差值
//!   （动作前后对比；mimalloc 无增量 delta API，增量由两次快照相减）。
//!
//! 鉴权走 memstats 同款白名单读面（`write_surface_auth`：GET admin/viewer，
//! 写 admin）；本模块无独立业务状态，全部数值现场从各结构读出。

use std::sync::OnceLock;

use axum::extract::State;
use serde::Serialize;

use crate::routes::AppState;

/// 单个内存持有点的分解条目。
#[derive(Debug, Clone, Serialize)]
pub struct MemoryBreakdownItem {
    /// 持有点名（稳定标识，消费方按名取数）。
    pub name: &'static str,
    /// 实测条目数（结构内锁内计数；无条目语义的面为 0）。
    pub entries: usize,
    /// 近似字节（实测和或条目 × 平均 payload 估算，见 `measured`）。
    pub approx_bytes: usize,
    /// true = 字节为结构内实测（逐条 len/序列化求和）；false = 条目 × 平均
    /// payload 估算。
    pub measured: bool,
    /// 口径补充（分类/边界说明；None = 无补充）。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub note: Option<&'static str>,
    /// 分维度明细（如 pipeline_state 的 ended/active 拆分）。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<serde_json::Value>,
}

/// GET /api/v1/system/memory-breakdown — 内核活内存按结构分解。
///
/// 逐持有点报条目数 + 近似字节 + 实测/估算标注，另附分配器/进程面参照值
/// （mimalloc committed 与 OS RSS——结构分解之和 ≪ RSS 的差额即瞬态工作集
/// 与已提交未触页面，见内存审计 §1）。纯只读：只加各结构自有读锁取计数，
/// 不分配长命结构、不触碰任何业务状态。
pub async fn system_memory_breakdown_handler(
    State(state): State<AppState>,
) -> axum::Json<MemoryBreakdown> {
    axum::Json(build_memory_breakdown(&state).await)
}

/// [`system_memory_breakdown_handler`] 响应体。
#[derive(Debug, Clone, Serialize)]
pub struct MemoryBreakdown {
    /// 持有点分解（按 approx_bytes 降序——大头优先）。
    pub items: Vec<MemoryBreakdownItem>,
    /// 各项 approx_bytes 之和（含估算项，量级口径）。
    pub total_estimated_bytes: usize,
    /// 分配器/进程面参照（与结构分解之和对照，判"结构囤积 vs 瞬态/提交面"）。
    pub process: serde_json::Value,
}

/// pipeline state 条目的活跃/终态拆分。
#[derive(Debug, Default, Clone, Serialize)]
struct PipelineStateSplit {
    entries: usize,
    approx_bytes: usize,
}

/// 终态词表（内核 owned run_status + 任务域扁平标量的粗分类）。
const TERMINAL_STATUS_WORDS: [&str; 4] = ["completed", "failed", "cancelled", "done"];

/// 按条目 state 里的 run_status/task.status 标量粗分终态/活跃。
///
/// 任务状态语义归插件（内核零知识），本分类只是诊断粗读：命中终态词表
/// 记 ended，其余（running/suspended/pending/无标量）记活跃——ended 残留
/// 即内存审计观察的"非 suspended 终态注销缺口"信号。
fn split_pipeline_state(state_json: &serde_json::Value) -> bool {
    let status = state_json
        .get("run_status")
        .or_else(|| state_json.get("task.status"))
        .and_then(|v| v.as_str())
        .unwrap_or_default();
    TERMINAL_STATUS_WORDS.iter().any(|w| status.contains(w))
}

/// 组装分解面（handler 纯逻辑层，测试直接消费）。
pub(crate) async fn build_memory_breakdown(state: &AppState) -> MemoryBreakdown {
    let mut items: Vec<MemoryBreakdownItem> = Vec::new();

    // ── pipeline state 内存层（audit §3 首行：PipelineStateRegistry 常驻）──
    {
        let registry = agentos_session::pipeline_state_registry::global_registry();
        let mut active = PipelineStateSplit::default();
        let mut ended = PipelineStateSplit::default();
        for listing in registry.list() {
            let Some(entry) = registry.get(&listing.tenant_id, &listing.pipeline_id) else {
                continue;
            };
            let e = entry.read();
            let bytes = agentos_engine::transient::approx_value_bytes(&e.state);
            let slot = if split_pipeline_state(&e.state) {
                &mut ended
            } else {
                &mut active
            };
            slot.entries += 1;
            slot.approx_bytes += bytes;
        }
        let entries = active.entries + ended.entries;
        let bytes = active.approx_bytes + ended.approx_bytes;
        items.push(MemoryBreakdownItem {
            name: "pipeline_state",
            entries,
            approx_bytes: bytes,
            measured: true,
            note: Some(
                "常驻 final_state（messages 跨轮延续=设计正确）；ended=state 标量命中终态词表的残留条目",
            ),
            detail: Some(serde_json::json!({
                "active": active,
                "ended": ended,
            })),
        });
    }

    // ── 瞬态寄存器（audit §3：LRU + 双字节预算，原子量直读）──
    {
        let s = agentos_engine::transient::global_registry().memory_stats();
        let entries = s.keys + s.binding_messages + s.chunk_accumulators;
        let bytes = s.key_bytes + s.accum_bytes;
        items.push(MemoryBreakdownItem {
            name: "transient_state",
            entries,
            approx_bytes: bytes,
            measured: true,
            note: Some("A 区键值(LRU+64MB/32MB 预算) + B 区消息绑定 + chunk 累积档"),
            detail: Some(serde_json::json!(s)),
        });
    }

    // ── 重放缓冲 + WS 会话表（session 协调器）──
    if let Some(session) = state.session.as_ref() {
        let r = session.replay().memory_stats();
        items.push(MemoryBreakdownItem {
            name: "replay_buffer",
            entries: r.message_frames + r.misc_events,
            approx_bytes: r.approx_payload_bytes,
            measured: true,
            note: Some("per-thread 在飞消息帧组 + 杂项小环（终态整组即弃/1000 条 5min TTL）；条目=存储事件数"),
            detail: Some(serde_json::json!(r)),
        });

        let c = session.registry().memory_stats();
        // 每连接出站缓冲上界 = WS_OUTBOUND_CAPACITY(512) 帧 × 单帧估 4KB。
        const EST_FRAME_BYTES: usize = 4096;
        let bound = c.connections * 512 * EST_FRAME_BYTES;
        items.push(MemoryBreakdownItem {
            name: "ws_sessions",
            entries: c.connections + c.thread_user + c.thread_pipeline + c.thread_agent,
            approx_bytes: c.outbound_queued_frames * EST_FRAME_BYTES + (c.connections * 256),
            measured: false,
            note: Some("连接表 + 三张 thread 映射 [实测条目];出站积压=在飞帧实测×估 4KB/帧,满载上界 512 帧/连接"),
            detail: Some(serde_json::json!({
                "registry": c,
                "outbound_capacity_bound_bytes": bound,
            })),
        });
    }

    // ── 运行链（audit §3：gen 自清理 + 活跃偏好映射）──
    {
        let chains = crate::run_chain::RunChainRegistry::global();
        let (running, waiting) = chains.gate_snapshot();
        let entries = chains.active_chain_count();
        items.push(MemoryBreakdownItem {
            name: "run_chains",
            entries,
            approx_bytes: entries * 256 + chains.active_pipeline_count() * 128,
            measured: false,
            note: Some(
                "链尾 JoinHandle 表 + 活跃管道偏好(以用户数为界) [估算 KB 级];闸门运行/等待为实测",
            ),
            detail: Some(serde_json::json!({
                "gate_running": running,
                "gate_waiting": waiting,
                "active_pipeline_pref": chains.active_pipeline_count(),
            })),
        });
    }

    // ── 审批/交互等待桥（audit §3：oneshot 即弃，内核唯一内存面）──
    items.push(MemoryBreakdownItem {
        name: "outcome_waiters",
        entries: crate::ws_session::outcome_waiters_count(),
        approx_bytes: 0,
        measured: true,
        note: Some("REST 同步等待桥（oneshot 即弃）；pending_inputs 持久层在 DB，无内存账本"),
        detail: None,
    });

    // ── manifests / 能力注册表 / scopes / 契约账本 ──
    {
        let manifests = state.manifests.read().await;
        let bytes: usize = manifests
            .iter()
            .map(|m| serde_json::to_vec(m).map(|v| v.len()).unwrap_or(0))
            .sum();
        items.push(MemoryBreakdownItem {
            name: "manifests",
            entries: manifests.len(),
            approx_bytes: bytes,
            measured: true,
            note: Some("逐 manifest 序列化字节实测（共享存储本体）"),
            detail: None,
        });
        drop(manifests);

        let reg_stats = state.capability_registry.as_ref().map(|r| r.memory_stats());
        let tools = reg_stats.as_ref().map(|s| s.tools).unwrap_or(0);
        items.push(MemoryBreakdownItem {
            name: "capability_registry",
            entries: reg_stats
                .as_ref()
                .map(|s| {
                    s.tools
                        + s.route_signal_plugins
                        + s.http_routes
                        + s.mode_agents
                        + s.mode_pipelines
                })
                .unwrap_or(0),
            approx_bytes: tools * 2048,
            measured: false,
            note: Some("工具/路由信号/HTTP 端点/模式包键 [实测条目];字节按工具 ×2KB 估算"),
            detail: reg_stats.map(|s| serde_json::json!(s)),
        });

        items.push(MemoryBreakdownItem {
            name: "plugin_scopes",
            entries: state.plugin_scopes.len(),
            approx_bytes: 0,
            measured: true,
            note: Some("per-plugin 注册账本（guard 表）"),
            detail: None,
        });

        items.push(MemoryBreakdownItem {
            name: "contract_states",
            entries: state.contract_states.snapshot().len(),
            approx_bytes: 0,
            measured: true,
            note: None,
            detail: None,
        });
    }

    // ── 管道编译产物缓存（D9 版本集只增观测项）──
    {
        let (pipelines, versions) = crate::server::pipeline_compiled_cache_stats();
        items.push(MemoryBreakdownItem {
            name: "pipeline_compiled_cache",
            entries: versions,
            approx_bytes: versions * 8192,
            measured: false,
            note: Some("版本集随配置热更只增（D9 钉住契约保活）[估算 8KB/产物]"),
            detail: Some(serde_json::json!({ "pipelines": pipelines })),
        });
    }

    // ── LLM 上下文装配缓存（{{path:}} 模板内容 LRU）──
    {
        let (entries, bytes) = agentos_engine::template::path_cache_stats();
        items.push(MemoryBreakdownItem {
            name: "llm_context_template_cache",
            entries,
            approx_bytes: bytes,
            measured: true,
            note: Some("{{path:}} 文件内容 LRU（容量 128，mtime 失效）"),
            detail: None,
        });
    }

    // ── 指标聚合 / 生命周期事件广播 ──
    if let Some(metrics) = state.metrics.as_ref() {
        items.push(MemoryBreakdownItem {
            name: "metrics_aggregator",
            entries: metrics.series_count(),
            approx_bytes: 0,
            measured: true,
            note: Some("滚动桶 10min/2h + rollup 降采样；series 条目实测"),
            detail: None,
        });
    }
    if let Some(bus) = agentos_hooks::global() {
        items.push(MemoryBreakdownItem {
            name: "hook_event_bus",
            entries: bus.queued_len(),
            approx_bytes: bus.queued_len() * 1024,
            measured: false,
            note: Some("tokio broadcast 容量 1024；滞留事件 ×1KB 估算，订阅者见 detail"),
            detail: Some(serde_json::json!({ "receivers": bus.receiver_count() })),
        });
    }

    // ── 杂项进程内小表（audit §3 杂项行：低频或有界）──
    {
        let jtis = state.consumed_refresh_jtis.lock().len();
        let logins = state.login_failures.lock().len();
        let registers = state.register_attempts.lock().len();
        let tickets = state.ws_tickets.len();
        let widgets = state
            .widget_bindings
            .as_ref()
            .map(|b| b.read().len())
            .unwrap_or(0);
        let entries = jtis + logins + registers + tickets + widgets;
        items.push(MemoryBreakdownItem {
            name: "misc_inproc_maps",
            entries,
            approx_bytes: entries * 128,
            measured: false,
            note: Some(
                "refresh jti 兜底集/登录限频窗/注册限频窗/WS 票据表/widget 绑定表 [估算 128B/条]",
            ),
            detail: Some(serde_json::json!({
                "consumed_refresh_jtis": jtis,
                "login_failures": logins,
                "register_attempts": registers,
                "ws_tickets": tickets,
                "widget_bindings": widgets,
                "enabled_plugin_ids": state.enabled_plugin_ids.read().await.len(),
                "plugin_dirs": state.plugin_dirs.len(),
            })),
        });
    }

    // ── 审计点名但内核无内存副本的面（显式报 0，防"没查"误读）──
    items.push(MemoryBreakdownItem {
        name: "traces_ledger",
        entries: 0,
        approx_bytes: 0,
        measured: true,
        note: Some("append_trace 直落库即弃；ops_ledger/step_key_journal per-run 即弃，无内存副本（audit §3）"),
        detail: None,
    });
    items.push(MemoryBreakdownItem {
        name: "pipeline_sessions",
        entries: 0,
        approx_bytes: 0,
        measured: true,
        note: Some(
            "会话↔管道映射持久层在 DB；内存面只有连接注册表的 thread 映射（ws_sessions 项）",
        ),
        detail: None,
    });

    items.sort_by(|a, b| b.approx_bytes.cmp(&a.approx_bytes).then(a.name.cmp(b.name)));
    let total = items.iter().map(|i| i.approx_bytes).sum();
    let alloc = crate::allocator::snapshot_stats();
    let process = serde_json::json!({
        "mimalloc_committed_bytes": alloc.committed_bytes,
        "mimalloc_in_use_bytes": alloc.in_use_bytes,
        "process_commit_bytes": alloc.process_commit_bytes,
        "process_rss_bytes": alloc.process_rss_bytes,
    });
    MemoryBreakdown {
        items,
        total_estimated_bytes: total,
        process,
    }
}

// ── 任务活动转储 ──────────────────────────────────────────────

/// GET /api/v1/system/task-dump — 任务级"此刻在干什么"。
///
/// 枚举全部插桩任务（`agentos_core::task_activity` 全局注册表）的任务名、
/// 当前活动标签、存活秒数。标签由各任务在既有等待点 O(1) 原子更新
/// （如 "waiting admission gate" / "dispatching pipeline x" / "idle"）——
/// 卡死点直读（停泊等待显形为对应标签 + 存活时长）。
pub async fn system_task_dump_handler() -> axum::Json<serde_json::Value> {
    let registry = agentos_core::task_activity::global_registry();
    let tasks = registry.snapshot();
    axum::Json(serde_json::json!({
        "tasks": tasks,
        "count": tasks.len(),
    }))
}

// ── mimalloc 快照差分 ────────────────────────────────────────

/// 已记录的差分基线（POST 记录，GET 消费；进程内单基线——后记覆盖前记）。
static SNAPSHOT_BASELINE: OnceLock<parking_lot::Mutex<Option<SnapshotBaseline>>> = OnceLock::new();

/// 基线条目：记录时刻 + 记录时快照。
#[derive(Debug, Clone)]
struct SnapshotBaseline {
    recorded_at: chrono::DateTime<chrono::Utc>,
    stats: crate::allocator::MemStats,
}

fn snapshot_baseline_cell() -> &'static parking_lot::Mutex<Option<SnapshotBaseline>> {
    SNAPSHOT_BASELINE.get_or_init(|| parking_lot::Mutex::new(None))
}

/// POST /api/v1/system/memstats/snapshot — 记录当前分配器计数为差分基线。
///
/// 返回记录的基线（含记录时刻）。再次 POST 覆盖旧基线（进程内单基线）。
pub async fn memstats_snapshot_record_handler() -> axum::Json<serde_json::Value> {
    let baseline = SnapshotBaseline {
        recorded_at: chrono::Utc::now(),
        stats: crate::allocator::snapshot_stats(),
    };
    let recorded = serde_json::json!({
        "recorded_at": baseline.recorded_at.to_rfc3339(),
        "stats": baseline.stats,
    });
    *snapshot_baseline_cell().lock() = Some(baseline);
    axum::Json(recorded)
}

/// GET /api/v1/system/memstats/snapshot — 当前计数 + 与基线的逐字段差值。
///
/// 无基线 → `baseline: null`（差值字段缺省）；有基线 → `delta` 为
/// current − baseline（饱和减法，字段缺任一侧则该项 null）。用于动作前后
/// 对比：POST 记基线 → 做动作 → GET 读增量。
pub async fn memstats_snapshot_diff_handler() -> axum::Json<serde_json::Value> {
    let current = crate::allocator::snapshot_stats();
    let guard = snapshot_baseline_cell().lock();
    let Some(baseline) = guard.as_ref() else {
        return axum::Json(serde_json::json!({
            "baseline": null,
            "current": current,
            "delta": null,
        }));
    };
    let delta = serde_json::json!({
        "in_use_bytes": delta_u64(baseline.stats.in_use_bytes, current.in_use_bytes),
        "requested_bytes": delta_u64(baseline.stats.requested_bytes, current.requested_bytes),
        "total_allocs": delta_u64(baseline.stats.total_allocs, current.total_allocs),
        "freed_bytes": delta_u64(baseline.stats.freed_bytes, current.freed_bytes),
        "committed_bytes": delta_u64(baseline.stats.committed_bytes, current.committed_bytes),
        "reserved_bytes": delta_u64(baseline.stats.reserved_bytes, current.reserved_bytes),
        "purged_bytes": delta_u64(baseline.stats.purged_bytes, current.purged_bytes),
        "abandoned_pages": delta_u64(baseline.stats.abandoned_pages, current.abandoned_pages),
        "process_commit_bytes": delta_u64(baseline.stats.process_commit_bytes, current.process_commit_bytes),
        "process_rss_bytes": delta_u64(baseline.stats.process_rss_bytes, current.process_rss_bytes),
    });
    axum::Json(serde_json::json!({
        "baseline": {
            "recorded_at": baseline.recorded_at.to_rfc3339(),
            "stats": baseline.stats,
        },
        "current": current,
        "delta": delta,
    }))
}

/// 逐字段差值：两侧都有 → current − baseline（饱和）；任一侧缺 → null。
fn delta_u64(baseline: Option<u64>, current: Option<u64>) -> Option<u64> {
    baseline.zip(current).map(|(b, c)| c.saturating_sub(b))
}

#[cfg(test)]
mod tests {
    //! 分解面计数值正确性（小型注入验证）+ 快照差分逻辑 + 白名单鉴权。
    //! 测试构造与 routes::system_memstats_tests 同构（内存库播种 admin →
    //! 真实 login 换 access token）。

    use super::*;
    use agentos_core::traits::StorageBackend;
    use agentos_http::auth::hash_password;
    use axum::body::Body;
    use axum::http::Request;
    use serde_json::json;
    use tower::ServiceExt;

    const TEST_ADMIN_PW: &str = "test-admin-pw-2026";

    async fn app_with_seeded_admin() -> axum::Router {
        let store = std::sync::Arc::new(
            agentos_engine::SqliteStore::open_memory().expect("open_memory 失败"),
        );
        let admin = agentos_core::types::UserRecord {
            user_id: "00000000-0000-0000-0000-000000000001".to_string(),
            username: "admin".to_string(),
            password: hash_password(TEST_ADMIN_PW).unwrap(),
            email: Some("admin@agentos.dev".to_string()),
            role: "admin".to_string(),
            tenant_id: agentos_http::auth::DEFAULT_TENANT_ID.to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            last_login_at: None,
            must_change_password: false,
        };
        let _ = store.create_user(&admin).await;
        let mut state = AppState::new();
        state.store = Some(store);
        crate::server::build_router(state)
    }

    async fn login_token(app: &axum::Router) -> String {
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/login")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({"username": "admin", "password": TEST_ADMIN_PW}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), axum::http::StatusCode::OK);
        let body = axum::body::to_bytes(resp.into_body(), 8192).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        v["access_token"].as_str().unwrap().to_string()
    }

    // ── 终态粗分类（内核 owned 词表 + 任务域扁平标量）──

    #[test]
    fn terminal_word_table_classifies_state_scalar() {
        // 正常组：命中终态词表（run_status 与任务域扁平标量两键位）。
        assert!(split_pipeline_state(&json!({"run_status": "completed"})));
        assert!(split_pipeline_state(&json!({"task.status": "done"})));
        // 边界组：非终态/空标量/缺键 → 活跃。
        assert!(!split_pipeline_state(&json!({"run_status": "running"})));
        assert!(!split_pipeline_state(&json!({"run_status": "suspended"})));
        assert!(!split_pipeline_state(&json!({})));
        // 符号量级相反组：包含子串匹配的形态（"uncompleted" 含 "completed"）。
        assert!(
            split_pipeline_state(&json!({"run_status": "uncompleted"})),
            "contains 粗分类语义如实断言（词表匹配是包含关系，诊断用途可接受）"
        );
    }

    // ── 分解面组装（小型注入验证各结构计数）──

    #[tokio::test]
    async fn breakdown_sorted_flagged_and_zero_copy_faces_present() {
        let state = AppState::new();
        let bd = build_memory_breakdown(&state).await;
        assert!(!bd.items.is_empty());
        // 排序性质：approx_bytes 降序（大头优先）。
        for w in bd.items.windows(2) {
            assert!(
                w[0].approx_bytes >= w[1].approx_bytes,
                "items 必须按 approx_bytes 降序"
            );
        }
        // 标注完备性：每项都有实测/估算标注；名字唯一（消费方按名取数）。
        let mut names: Vec<&str> = bd.items.iter().map(|i| i.name).collect();
        names.sort_unstable();
        let dup = names.len();
        names.dedup();
        assert_eq!(names.len(), dup, "持有点名唯一");
        // 审计点名但无内存副本的面：显式报 0（防"没查"误读）。
        for zero_face in ["traces_ledger", "pipeline_sessions"] {
            let item = bd
                .items
                .iter()
                .find(|i| i.name == zero_face)
                .unwrap_or_else(|| panic!("{zero_face} 必须在列"));
            assert_eq!(item.entries, 0, "{zero_face} 无内存副本");
            assert!(item.measured, "{zero_face} 的 0 是实测（结构确认无驻留）");
        }
        // 总和 = 各项之和。
        let sum: usize = bd.items.iter().map(|i| i.approx_bytes).sum();
        assert_eq!(bd.total_estimated_bytes, sum);
        // 进程参照面：测试进程未装 mimalloc 全局分配器，但统计面可用
        // （OS 口径 commit > 0 与既有 allocator 测试同性质断言）。
        assert!(
            bd.process["process_commit_bytes"].as_u64().unwrap_or(0) > 0,
            "进程参照 commit 应 > 0"
        );
    }

    #[tokio::test]
    async fn breakdown_reports_session_structures_with_injection() {
        let mut state = AppState::new();
        let session = std::sync::Arc::new(agentos_session::SessionCoordinator::new());
        // 注入：1 条杂项重放事件（misc 小环）。
        let recorded = session
            .replay()
            .record(
                "thread-memtest",
                agentos_session::replay::ReplayEvent::new(1, r#"{"type":"run_started"}"#),
            )
            .await;
        assert!(recorded);
        // 注入：1 条 WS 连接（出站队列 0 在飞）。
        let (sink, mut out_rx, _close_rx) = crate::ws_session::WsSink::new();
        let sink_for_reg: std::sync::Arc<dyn agentos_session::EventSink> = sink.clone();
        session.register("user-memtest", sink_for_reg);
        drop(out_rx.recv()); // 保持 rx 存活到断言后（防队列关闭干扰）
        state.session = Some(session);
        let bd = build_memory_breakdown(&state).await;

        let replay_item = bd
            .items
            .iter()
            .find(|i| i.name == "replay_buffer")
            .expect("有 session 装配时 replay_buffer 必须在列");
        assert!(replay_item.measured);
        assert_eq!(replay_item.entries, 1, "1 thread 1 杂项事件");
        assert_eq!(replay_item.approx_bytes, r#"{"type":"run_started"}"#.len());

        let ws_item = bd
            .items
            .iter()
            .find(|i| i.name == "ws_sessions")
            .expect("有 session 装配时 ws_sessions 必须在列");
        assert_eq!(
            ws_item.detail.as_ref().unwrap()["registry"]["connections"],
            1
        );
        assert_eq!(
            ws_item.detail.as_ref().unwrap()["registry"]["outbound_queued_frames"],
            0,
            "空队列在飞帧实测为 0"
        );
    }

    #[tokio::test]
    async fn breakdown_pipeline_state_split_counts_injected_entries() {
        let registry = agentos_session::pipeline_state_registry::global_registry();
        let key_a = format!("memtest-active-{}", std::process::id());
        let key_b = format!("memtest-ended-{}", std::process::id());
        registry.get_or_init(
            "default",
            &key_a,
            "t",
            "a",
            json!({"messages": [], "run_status": "running"}),
        );
        registry.get_or_init(
            "default",
            &key_b,
            "t",
            "a",
            json!({"messages": [], "run_status": "completed"}),
        );
        let state = AppState::new();
        let bd = build_memory_breakdown(&state).await;
        registry.remove("default", &key_a);
        registry.remove("default", &key_b);
        let item = bd
            .items
            .iter()
            .find(|i| i.name == "pipeline_state")
            .expect("pipeline_state 必须在列");
        assert!(item.measured, "pipeline state 字节为逐条实测");
        let active = item.detail.as_ref().unwrap()["active"]["entries"]
            .as_u64()
            .unwrap();
        let ended = item.detail.as_ref().unwrap()["ended"]["entries"]
            .as_u64()
            .unwrap();
        assert!(
            active >= 1 && ended >= 1,
            "注入的活跃/终态条目必须各计 1（实际 active={active} ended={ended}，并行测试可能另有注入）"
        );
        assert_eq!(
            item.entries as u64,
            active + ended,
            "条目总数 = active + ended 拆分"
        );
        assert!(
            item.approx_bytes > 0,
            "常驻 state 字节应 > 0（注入了非空 state）"
        );
    }

    // ── 白名单鉴权（memstats 同语义链）──

    #[tokio::test]
    async fn diagnostics_endpoints_unauthorized_without_token() {
        let app = app_with_seeded_admin().await;
        for (method, uri) in [
            ("GET", "/api/v1/system/memory-breakdown"),
            ("GET", "/api/v1/system/task-dump"),
            ("GET", "/api/v1/system/memstats/snapshot"),
            ("POST", "/api/v1/system/memstats/snapshot"),
        ] {
            let resp = app
                .clone()
                .oneshot(
                    Request::builder()
                        .method(method)
                        .uri(uri)
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(
                resp.status(),
                axum::http::StatusCode::UNAUTHORIZED,
                "{method} {uri} 匿名必须 401"
            );
        }
    }

    #[tokio::test]
    async fn diagnostics_endpoints_ok_with_admin_token() {
        let app = app_with_seeded_admin().await;
        let token = login_token(&app).await;
        for (method, uri) in [
            ("GET", "/api/v1/system/memory-breakdown"),
            ("GET", "/api/v1/system/task-dump"),
            ("GET", "/api/v1/system/memstats/snapshot"),
            ("POST", "/api/v1/system/memstats/snapshot"),
        ] {
            let resp = app
                .clone()
                .oneshot(
                    Request::builder()
                        .method(method)
                        .uri(uri)
                        .header("authorization", format!("Bearer {token}"))
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(
                resp.status(),
                axum::http::StatusCode::OK,
                "{method} {uri} admin 必须 200"
            );
        }
        // 分解面形状：items 数组 + 进程参照面（数值或 null——测试进程构建
        // 开关影响 malloc 计量面）。
        let resp = app
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri("/api/v1/system/memory-breakdown")
                    .header("authorization", format!("Bearer {token}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = axum::body::to_bytes(resp.into_body(), 262_144)
            .await
            .unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert!(v["items"].as_array().expect("items 数组").len() >= 10);
        assert!(
            v["process"]["process_rss_bytes"].is_number()
                || v["process"]["process_rss_bytes"].is_null()
        );
    }

    // ── 快照差分 ──

    #[test]
    fn delta_u64_saturation_and_none_semantics() {
        // 正常组：current > baseline → 差值。
        assert_eq!(delta_u64(Some(100), Some(250)), Some(150));
        // 边界组：current < baseline（计数回绕/口径变化）→ 饱和 0（不回吐负值）。
        assert_eq!(delta_u64(Some(300), Some(100)), Some(0));
        // 缺失组：任一侧 None → null（不猜 0）。
        assert_eq!(delta_u64(None, Some(1)), None);
        assert_eq!(delta_u64(Some(1), None), None);
        assert_eq!(delta_u64(None, None), None);
    }

    #[tokio::test]
    async fn snapshot_record_then_diff_returns_baseline_and_delta() {
        // 单测内串行覆盖全部流程（静态基线是进程级单例，避免并行互踩）：
        // 清基线 → GET 无基线 → POST 记录 → GET 有基线 + 差值 → 再 POST 覆盖。
        *snapshot_baseline_cell().lock() = None;

        let no_baseline = memstats_snapshot_diff_handler().await;
        assert!(no_baseline.0["baseline"].is_null(), "无基线如实报 null");
        assert!(no_baseline.0["delta"].is_null());

        let recorded = memstats_snapshot_record_handler().await;
        assert!(recorded.0["recorded_at"].is_string());
        assert!(
            recorded.0["stats"]["process_commit_bytes"]
                .as_u64()
                .unwrap_or(0)
                > 0,
            "记录的基线含 OS 口径 commit（进程存活即有已提交内存）"
        );

        let diff = memstats_snapshot_diff_handler().await;
        assert!(diff.0["baseline"].is_object(), "POST 后 GET 必须有基线");
        assert!(diff.0["current"].is_object());
        for field in [
            "in_use_bytes",
            "committed_bytes",
            "purged_bytes",
            "process_commit_bytes",
            "process_rss_bytes",
        ] {
            let d = &diff.0["delta"][field];
            assert!(
                d.is_u64() || d.is_null(),
                "delta.{field} 应为数值或 null，实际 {d}"
            );
        }
        // 性质断言：差值 ≤ 当前值（current − baseline 的饱和下界，恒非负）。
        if let (Some(d), Some(c)) = (
            diff.0["delta"]["committed_bytes"].as_u64(),
            diff.0["current"]["committed_bytes"].as_u64(),
        ) {
            assert!(d <= c, "差值不得超过当前值");
        }
        // 再 POST = 覆盖基线（进程内单基线语义）：recorded_at 前移。
        let first_at = diff.0["baseline"]["recorded_at"].clone();
        let _ = memstats_snapshot_record_handler().await;
        let diff2 = memstats_snapshot_diff_handler().await;
        assert!(diff2.0["baseline"].is_object());
        assert_ne!(
            diff2.0["baseline"]["recorded_at"], first_at,
            "重记录必须覆盖旧基线（recorded_at 刷新）"
        );
        *snapshot_baseline_cell().lock() = None; // 清场，不影响其它测试
    }

    // ── 任务活动转储 ──

    #[tokio::test]
    async fn task_dump_lists_registered_slot_label_and_unregisters_on_drop() {
        let name = format!("diag-slot-{}", std::process::id());
        let guard = agentos_core::task_activity::global_registry().register(&name);
        guard.set_label("waiting human_interaction response req=diag-1");
        let dump = system_task_dump_handler().await;
        let entry = dump.0["tasks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|t| t["name"] == name.as_str())
            .unwrap_or_else(|| panic!("已注册任务 {name} 必须在 dump 列表"));
        assert_eq!(
            entry["label"],
            "waiting human_interaction response req=diag-1"
        );
        assert_eq!(entry["age_secs"], 0, "刚注册存活 < 1s");
        assert_eq!(
            dump.0["count"].as_u64().unwrap() as usize,
            dump.0["tasks"].as_array().unwrap().len()
        );

        // 跑完/断开（守卫 drop）= 从 dump 消失。
        drop(guard);
        let dump = system_task_dump_handler().await;
        assert!(
            !dump.0["tasks"]
                .as_array()
                .unwrap()
                .iter()
                .any(|t| t["name"] == name.as_str()),
            "注销后任务不得残留"
        );
    }
}

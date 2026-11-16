// @feature: FP-0.2.〇 管道引擎（三热点性能基准之二） | @ci: rust-bench
//! SQLite 写路径基准（整改方案 §6 拍板⑨）。
//!
//! 测什么：SqliteStore 的单条/批量写路径（生产形态真实落盘，tempfile 临时库）：
//! - `upsert_state_field`：单键 upsert（每条独立 autocommit——引擎分层投影的
//!   逐键写形态）；
//! - `upsert_state_fields_16`：16 键单事务批量 upsert（B6 批量事务版——
//!   record_run_start / 收尾折算的写形态）；
//! - `append_trace`：轨迹追加（trait 面 async：blocking() 池派发 + INSERT 带
//!   MAX(seq) 索引子查询——引擎每 step 每窗口一次的生产写形态）。
//!
//! 确定性可重复：载荷内容逐字节固定（同键同值覆盖写，表尺寸暖机后恒定）；
//! traces 表追加式增长是生产真实形态，(pipeline_id, tenant_id, seq) 索引使
//! MAX(seq) 为索引定位、代价与表尺寸无关；updated_at/created_at/trace_id 的
//! 时钟与 UUID 内容差异不影响控制流与工作量。
//!
//! 运行：cd kernel && cargo bench -p agentos-engine --bench sqlite_write

use std::sync::atomic::{AtomicUsize, Ordering};

use criterion::{criterion_group, criterion_main, Criterion};
use serde_json::{json, Value};

use agentos_core::traits::StorageBackend;
use agentos_core::types::{PatchType, TraceEntry};
use agentos_engine::SqliteStore;

const TENANT: &str = "bench-tenant";
const PIPELINE: &str = "bench-pipeline";

/// 固定 256B 标量载荷（state 字段典型尺寸）。
fn state_payload(i: usize) -> Value {
    json!(format!(
        "bench-value-{i:04}-0123456789abcdef0123456789abcdef0123456789abcdef\
0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    ))
}

/// 固定 ~1KB 轨迹补丁（step 窗口 diff 的典型形态：少量顶层键 + 嵌套对象）。
fn trace_patch_data(i: usize) -> Value {
    json!({
        "plugin_key": format!("k{i:03}"),
        "bench_a": "fixed-window-payload-0123456789",
        "bench_b": {
            "kind": "bench_trace_patch",
            "body": "fixed deterministic step window diff payload for sqlite bench",
            "nested": {"depth": 1, "leaf": [1, 2, 3]}
        },
        "filler": "0123456789abcdef".repeat(48),
    })
}

fn bench_sqlite_write(c: &mut Criterion) {
    let dir = tempfile::tempdir().expect("tempdir");
    let db_path = dir.path().join("bench.sqlite3");
    let store = SqliteStore::open(db_path.to_str().expect("utf-8 path")).expect("open bench db");
    let rt = tokio::runtime::Runtime::new().expect("tokio runtime");

    // ── 单键 upsert：128 固定键轮转覆盖（首圈后表尺寸恒定，测稳态写）──────
    {
        let key_idx = AtomicUsize::new(0);
        c.bench_function("upsert_state_field_single", |b| {
            b.iter(|| {
                let i = key_idx.fetch_add(1, Ordering::Relaxed) % 128;
                store
                    .upsert_state_field(
                        criterion::black_box(PIPELINE),
                        criterion::black_box(TENANT),
                        criterion::black_box(&format!("bench_key_{i:03}")),
                        criterion::black_box(&state_payload(i)),
                    )
                    .expect("upsert ok")
            })
        });
    }

    // ── 批量 upsert：16 固定键单事务（同键同值，事务开销 + 16 条执行）──────
    {
        let fields: serde_json::Map<String, Value> = (0..16)
            .map(|i| (format!("bench_batch_{i:02}"), state_payload(i)))
            .collect();
        c.bench_function("upsert_state_fields_batch16", |b| {
            b.iter(|| {
                store
                    .upsert_state_fields(
                        criterion::black_box(PIPELINE),
                        criterion::black_box(TENANT),
                        criterion::black_box(&fields),
                    )
                    .expect("batch upsert ok")
            })
        });
    }

    // ── 轨迹追加：trait async 面（blocking 池派发 + INSERT + MAX(seq) 子查询）──
    {
        let trace_idx = AtomicUsize::new(0);
        c.bench_function("append_trace", |b| {
            b.iter(|| {
                let i = trace_idx.fetch_add(1, Ordering::Relaxed);
                let entry = TraceEntry {
                    trace_id: format!("t_bench_{i:08}"),
                    pipeline_id: PIPELINE.to_string(),
                    seq: 0,
                    plugin_id: format!("bench_step_{:02}", i % 3),
                    patch_type: PatchType::StateUpdate,
                    patch_data: trace_patch_data(i % 8),
                    created_at: chrono::Utc::now().to_rfc3339(),
                };
                rt.block_on(store.append_trace(criterion::black_box(entry)))
                    .expect("append_trace ok")
            })
        });
    }
}

criterion_group!(benches, bench_sqlite_write);
criterion_main!(benches);

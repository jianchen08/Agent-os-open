// @feature: FP-0.2.八 会话传输内核（三热点性能基准之三） | @ci: rust-bench
//! WS 扇出基准（整改方案 §6 拍板⑨）。
//!
//! 测什么：连接注册表向 N 连接扇出的纯逻辑面——快照连接集（读锁 + Arc 克隆）
//! → 逐 sink await send_text → 死连接注销簿记；以及 FrontendEventBus 的完整
//! 广播事件路径（限流令牌 → sequence 分配 → 信封构造 + serde 序列化 → 扇出）。
//! 不开真 socket：mock sink 只做原子计数（任务口径：纯逻辑面）。
//!
//! 确定性可重复：连接拓扑预构造固定（users × conns 注册一次），载荷逐字节
//! 固定，sink 无时钟/网络依赖；HashMap 迭代序不影响工作量（N 次计数发送）。
//! emit 基准的限流配置取大突发（u32::MAX）——测扇出路径本身，不测令牌桶
//! 耗尽后的丢弃路径。
//!
//! 运行：cd kernel && cargo bench -p agentos-session --bench ws_fanout

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use criterion::{criterion_group, criterion_main, BenchmarkId, Criterion};
use serde_json::json;

use agentos_session::event_bus::{EmitScope, FrontendEventBus, RateLimitConfig};
use agentos_session::{ConnectionRegistry, EventSink};

/// 计数 sink：send_text 原子计数恒返回成功（纯逻辑面，零 IO）。
struct CountingSink {
    sink_id: u64,
    delivered: Arc<AtomicU64>,
}

#[async_trait::async_trait]
impl EventSink for CountingSink {
    async fn send_text(&self, _text: &str) -> bool {
        self.delivered.fetch_add(1, Ordering::Relaxed);
        true
    }
    fn id(&self) -> u64 {
        self.sink_id
    }
}

/// 固定 ~256B 文本帧（widget 事件信封序列化后的典型尺寸）。
const PAYLOAD: &str = "bench-ws-frame-{\"type\":\"widget_event\",\"data\":{\"widget_id\":\
\"bench.widget\",\"event\":\"update\",\"data\":{\"value\":1234567890,\"seq\":42}},\
\"metadata\":{\"source_plugin\":\"bench\",\"ts\":\"2026-10-10T00:00:00Z\"}}";

/// 预构造注册表：users 个用户 × conns_per_user 连接（全局唯一 sink id）。
fn registry_of(
    users: usize,
    conns_per_user: usize,
    counter: &Arc<AtomicU64>,
) -> ConnectionRegistry {
    let registry = ConnectionRegistry::with_max_conns(conns_per_user);
    let mut next_id = 0u64;
    for u in 0..users {
        for _ in 0..conns_per_user {
            let sink = CountingSink {
                sink_id: next_id,
                delivered: Arc::clone(counter),
            };
            next_id += 1;
            registry.register(&format!("bench-user-{u:03}"), Arc::new(sink));
        }
    }
    registry
}

fn bench_ws_fanout(c: &mut Criterion) {
    let rt = tokio::runtime::Runtime::new().expect("tokio runtime");
    let delivered = Arc::new(AtomicU64::new(0));

    // ── broadcast 扇出：16 / 64 / 256 连接（4 conn/user × N user）──────────
    {
        let mut group = c.benchmark_group("broadcast");
        for total in [16usize, 64, 256] {
            // 每场景独立注册表（连接互不混），warmup 期首轮快照即稳定。
            let registry = registry_of(total / 4, 4, &delivered);
            group.bench_with_input(
                BenchmarkId::new("fanout", format!("n={total}")),
                &total,
                |b, _| {
                    b.iter(|| {
                        let n = rt.block_on(registry.broadcast(criterion::black_box(PAYLOAD)));
                        assert_eq!(n, total, "全部 mock sink 投递成功");
                    })
                },
            );
        }
        group.finish();
    }

    // ── 单 user 扇出：4 连接（push_to_user 底层，流式帧常态路径）───────────
    {
        let registry = registry_of(16, 4, &delivered);
        c.bench_function("send_to_user_conns4", |b| {
            b.iter(|| {
                let ok = rt.block_on(registry.send_to_user(
                    criterion::black_box("bench-user-007"),
                    criterion::black_box(PAYLOAD),
                ));
                assert!(ok, "在线用户投递成功");
            })
        });
    }

    // ── FrontendEventBus 完整广播路径：信封构造+序列化+sequence+扇出（64 连接）──
    {
        let registry = Arc::new(registry_of(16, 4, &delivered));
        let bus = FrontendEventBus::with_rate_limit(
            registry,
            RateLimitConfig {
                burst: u32::MAX,
                refill_per_sec: u32::MAX,
            },
        );
        let data = json!({"value": 1234567890, "seq": 42, "note": "bench emit payload"});
        c.bench_function("emit_broadcast_n64", |b| {
            b.iter(|| {
                let n = rt.block_on(bus.emit(
                    criterion::black_box("bench.widget"),
                    criterion::black_box("update"),
                    data.clone(),
                    EmitScope::Broadcast,
                    criterion::black_box("bench_plugin"),
                ));
                assert_eq!(n, 64, "广播触达全部 64 连接");
            })
        });
    }

    // 工作量完整性自检：投递计数必须为正（基准真实走过 send_text 路径）。
    assert!(
        delivered.load(Ordering::Relaxed) > 0,
        "mock sink 必须收到投递"
    );
}

criterion_group!(benches, bench_ws_fanout);
criterion_main!(benches);

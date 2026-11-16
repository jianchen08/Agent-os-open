// @feature: FP-0.2.内核内存治理 高频跨线程分配压测(稳态不涨+释放回落) | @ci: rust-test
//! 高频跨线程分配压测：验证内存闸（稳态不涨 + 释放即回落）。
//!
//! 场景与生产同型：`spawn_blocking` 闭包在 worker 上构造对象树（大量小对象
//! 落在该线程的 mimalloc 堆页里），调用方持有；worker 按短 keep-alive 退休
//! 时页里仍有活块 → 转 abandoned；随后在短命 blocking 线程里释放（生产里
//! 释放者也会退休），后台周期 `collect_now()` 结算残页。
//!
//! 两段指标：
//!   ① 稳态 20 波（每波分配→退休→释放，每 4 波 collect）：commit/abandoned
//!      峰值有界，且后半段峰值不超过前半段 + 64MB（不单调增长）；
//!   ② 持有段（12 波结果不释放）：峰值即"活数据 + 退休钉页"的高水位；释放 +
//!      collect 后回到基线附近。
//!
//! 跑法（重负载，不进常规车道）：
//!   cargo test -p agentos-api --test memory_stress -- --ignored --nocapture

use std::time::Duration;

use agentos_api::allocator::{
    collect_now, install_global_allocator, set_page_reclaim_on_free, snapshot_stats,
};
use serde_json::Value;

const PER_CALL_OBJECTS: usize = 500;
const PER_WAVE: usize = 40;
const RETIRE_WAIT: Duration = Duration::from_millis(800);
const STEADY_WAVES: usize = 20;
const PARKED_WAVES: usize = 12;

#[derive(Debug, Clone, Copy)]
struct Sample {
    commit_mb: u64,
    abandoned_pages: u64,
}

fn sample() -> Sample {
    let s = snapshot_stats();
    Sample {
        commit_mb: s.process_commit_bytes.unwrap_or(0) / (1024 * 1024),
        abandoned_pages: s.abandoned_pages.unwrap_or(0),
    }
}

/// 一波高频调用：worker 内构造对象树，返回给调用方持有。
async fn wave_retained(start: usize) -> Vec<Value> {
    // 并发扇出（不是逐个 await）：实测池上限与并发排队下的内存闸。
    let mut set = tokio::task::JoinSet::new();
    for i in start..start + PER_WAVE {
        set.spawn_blocking(move || {
            let mut map = serde_json::Map::with_capacity(PER_CALL_OBJECTS);
            for j in 0..PER_CALL_OBJECTS {
                map.insert(
                    format!("k{j}"),
                    Value::String(format!("v{i}-{j}-0123456789abcdef")),
                );
            }
            Value::Object(map)
        });
    }
    let mut out = Vec::with_capacity(PER_WAVE);
    while let Some(res) = set.join_next().await {
        out.push(res.expect("blocking task"));
    }
    out
}

/// 在短命 blocking 线程里释放（生产里释放者同样会退休），并等它退潮。
async fn release_on_short_lived_thread(values: Vec<Value>) {
    tokio::task::spawn_blocking(move || drop(values))
        .await
        .expect("drop task");
    tokio::time::sleep(RETIRE_WAIT).await;
}

#[test]
#[ignore = "压力测试：cargo test -p agentos-api --test memory_stress -- --ignored --nocapture"]
fn frequent_cross_thread_allocations_stay_bounded() {
    install_global_allocator();
    // 池上限默认 8；按生产推导值压测时传 MEM_STRESS_THREADS（本机 desktop
    // 24 核 → 96）。keep-alive 固定 200ms：加速"退休潮"以暴露 abandoned。
    let max_threads: usize = std::env::var("MEM_STRESS_THREADS")
        .ok()
        .and_then(|v| v.trim().parse().ok())
        .filter(|n| *n > 0)
        .unwrap_or(8);
    println!("stress pool: max_blocking_threads={max_threads}");
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .max_blocking_threads(max_threads)
        .thread_keep_alive(Duration::from_millis(200))
        .enable_all()
        .build()
        .expect("runtime");
    rt.block_on(async {
        drop(wave_retained(0).await);
        collect_now();
        let baseline = sample();

        // ① 稳态：每波分配→退休→释放；每 4 波 collect（模拟周期清理）。
        let mut steady: Vec<Sample> = Vec::with_capacity(STEADY_WAVES);
        for w in 0..STEADY_WAVES {
            let held = wave_retained(1_000 + w * PER_WAVE).await;
            tokio::time::sleep(RETIRE_WAIT).await;
            release_on_short_lived_thread(held).await;
            if w % 4 == 3 {
                collect_now();
            }
            steady.push(sample());
        }
        let steady_commit = steady.iter().map(|s| s.commit_mb).max().unwrap_or(0);
        let steady_abandoned = steady.iter().map(|s| s.abandoned_pages).max().unwrap_or(0);
        let first_half_peak = steady[..STEADY_WAVES / 2]
            .iter()
            .map(|s| s.commit_mb)
            .max()
            .unwrap_or(0);
        let second_half_peak = steady[STEADY_WAVES / 2..]
            .iter()
            .map(|s| s.commit_mb)
            .max()
            .unwrap_or(0);

        // ② 持有段：12 波结果不释放，观察"活数据 + 退休钉页"高水位，再释放结算。
        let mut parked: Vec<Value> = Vec::new();
        for w in 0..PARKED_WAVES {
            parked.extend(wave_retained(100_000 + w * PER_WAVE).await);
            tokio::time::sleep(RETIRE_WAIT).await;
        }
        let parked_sample = sample();
        release_on_short_lived_thread(parked).await;
        collect_now();
        let released_sample = sample();

        // ③ 观察段：关掉 free 即认领（page_reclaim_on_free=0）跑 4 波再释放，
        // 看"整批释放时空页归还"与选项无关；最后恢复生产默认 1。
        set_page_reclaim_on_free(0);
        let mut parked_off: Vec<Value> = Vec::new();
        for w in 0..4 {
            parked_off.extend(wave_retained(500_000 + w * PER_WAVE).await);
            tokio::time::sleep(RETIRE_WAIT).await;
        }
        let off_peak = sample();
        release_on_short_lived_thread(parked_off).await;
        let off_released = sample();
        set_page_reclaim_on_free(1);

        println!("baseline          = {baseline:?}");
        println!("steady_peak       = commit={steady_commit}MB abandoned={steady_abandoned}");
        println!("steady_first_half = {first_half_peak}MB / second_half = {second_half_peak}MB");
        println!("parked_peak       = {parked_sample:?}");
        println!("parked_released   = {released_sample:?}");
        println!("off_peak          = {off_peak:?}");
        println!("off_released      = {off_released:?}");

        assert!(
            steady_commit < 1024,
            "稳态 commit 应有界，实测峰值 {steady_commit}MB"
        );
        assert!(
            second_half_peak <= first_half_peak + 64,
            "稳态不应单调增长：first_half={first_half_peak}MB second_half={second_half_peak}MB"
        );
        assert!(
            parked_sample.abandoned_pages > baseline.abandoned_pages,
            "持有+退休后应看到退休钉页：baseline={baseline:?} parked={parked_sample:?}"
        );
        assert!(
            released_sample.commit_mb < parked_sample.commit_mb.max(1)
                && released_sample.commit_mb < baseline.commit_mb + 64,
            "释放+collect 后应回落基线附近：baseline={baseline:?} parked={parked_sample:?} released={released_sample:?}"
        );
        assert!(
            off_released.commit_mb < 2048,
            "选项关闭的观察段也不得有界外 commit，实测 {}MB",
            off_released.commit_mb
        );
    });
}

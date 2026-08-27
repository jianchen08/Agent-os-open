//! engine crate 自采 A 类指标——调度层（监控设计 §三 通道1 + §补引擎调度层）。
//!
//! 引擎是调度器，它知道**编排视角**：pipeline 执行次数/耗时、step 命中、
//! LLM/工具调用次数/耗时（invoke 前后差）、迭代轮数。
//!
//! 业务细节（token/cost/error）由执行插件走 record_metric 上报（B 类），不在本处。
//!
//! 线程安全：AtomicU64 计数器，关键路径 inc（纳秒级）。

use std::sync::atomic::{AtomicU64, Ordering};

/// engine crate 的运行态计数器集合（监控设计 §三 通道1 表 engine 行）。
#[derive(Debug, Default)]
pub struct EngineMetrics {
    /// pipeline 执行累计次数（counter）。
    pub pipeline_exec_total: AtomicU64,
    /// pipeline 执行累计耗时（微秒，counter；速率 = delta_time/delta_count）。
    pub pipeline_exec_micros: AtomicU64,
    /// step 命中累计次数（counter）。
    pub step_hits_total: AtomicU64,
    /// LLM 调用累计次数（counter，调度层视角 = invoke llm_core 次数）。
    pub llm_calls_total: AtomicU64,
    /// LLM 调用累计耗时（微秒，counter）。
    pub llm_calls_micros: AtomicU64,
    /// 工具调用累计次数（counter，调度层视角 = invoke tool 次数）。
    pub tool_calls_total: AtomicU64,
    /// 工具调用累计耗时（微秒，counter）。
    pub tool_calls_micros: AtomicU64,
    /// 迭代累计轮数（counter，每个 pipeline run 的迭代数之和）。
    pub iterations_total: AtomicU64,
    /// 持久化落库失败累计次数（counter）。
    ///
    /// persist_run_start/persist_run_end/persist_trace 失败时累加。失败只 warn 不阻断管道，
    /// 此计数器让失败可被观测（health/监控端点暴露），避免静默吞掉。
    pub persist_failures: AtomicU64,
}

impl EngineMetrics {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn inc_pipeline_exec(&self, elapsed_micros: u64) {
        self.pipeline_exec_total.fetch_add(1, Ordering::Relaxed);
        self.pipeline_exec_micros
            .fetch_add(elapsed_micros, Ordering::Relaxed);
    }

    pub fn inc_step_hit(&self) {
        self.step_hits_total.fetch_add(1, Ordering::Relaxed);
    }

    /// 记录一次 LLM 调用（调度层视角，elapsed = invoke 前后差）。
    pub fn inc_llm_call(&self, elapsed_micros: u64) {
        self.llm_calls_total.fetch_add(1, Ordering::Relaxed);
        self.llm_calls_micros
            .fetch_add(elapsed_micros, Ordering::Relaxed);
    }

    /// 记录一次工具调用（调度层视角，elapsed = invoke 前后差）。
    pub fn inc_tool_call(&self, elapsed_micros: u64) {
        self.tool_calls_total.fetch_add(1, Ordering::Relaxed);
        self.tool_calls_micros
            .fetch_add(elapsed_micros, Ordering::Relaxed);
    }

    pub fn inc_iterations(&self, n: u64) {
        self.iterations_total.fetch_add(n, Ordering::Relaxed);
    }

    /// 记录一次持久化落库失败。
    pub fn inc_persist_failure(&self) {
        self.persist_failures.fetch_add(1, Ordering::Relaxed);
    }

    /// 快照所有计数器。
    pub fn snapshot(&self) -> EngineMetricsSnapshot {
        EngineMetricsSnapshot {
            pipeline_exec_total: self.pipeline_exec_total.load(Ordering::Relaxed),
            pipeline_exec_micros: self.pipeline_exec_micros.load(Ordering::Relaxed),
            step_hits_total: self.step_hits_total.load(Ordering::Relaxed),
            llm_calls_total: self.llm_calls_total.load(Ordering::Relaxed),
            llm_calls_micros: self.llm_calls_micros.load(Ordering::Relaxed),
            tool_calls_total: self.tool_calls_total.load(Ordering::Relaxed),
            tool_calls_micros: self.tool_calls_micros.load(Ordering::Relaxed),
            iterations_total: self.iterations_total.load(Ordering::Relaxed),
            persist_failures: self.persist_failures.load(Ordering::Relaxed),
        }
    }
}

/// 一次快照。
#[derive(Debug, Clone, Default)]
pub struct EngineMetricsSnapshot {
    pub pipeline_exec_total: u64,
    pub pipeline_exec_micros: u64,
    pub step_hits_total: u64,
    pub llm_calls_total: u64,
    pub llm_calls_micros: u64,
    pub tool_calls_total: u64,
    pub tool_calls_micros: u64,
    pub iterations_total: u64,
    pub persist_failures: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_engine_metrics_inc() {
        let m = EngineMetrics::new();
        m.inc_pipeline_exec(1000);
        m.inc_pipeline_exec(500);
        m.inc_step_hit();
        m.inc_step_hit();
        m.inc_llm_call(200);
        m.inc_tool_call(50);
        m.inc_iterations(3);
        let s = m.snapshot();
        assert_eq!(s.pipeline_exec_total, 2);
        assert_eq!(s.pipeline_exec_micros, 1500);
        assert_eq!(s.step_hits_total, 2);
        assert_eq!(s.llm_calls_total, 1);
        assert_eq!(s.llm_calls_micros, 200);
        assert_eq!(s.tool_calls_total, 1);
        assert_eq!(s.tool_calls_micros, 50);
        assert_eq!(s.iterations_total, 3);
    }
}

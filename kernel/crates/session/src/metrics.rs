//! session crate 内部计数器（监控设计 §三 通道1 的自采写面）。
//!
//! 仅内部计数：本组计数器只自采不导出（无快照读面、不进聚合器/Prometheus/
//! 广播——读面已随 docs/decisions/2026-09-09-kernel-dead-layer-adjudication.md
//! 裁决移除）。排查时可经日志/调试器直接读原子字段。
//!
//! - connections：活跃连接数
//! - kick_old_total：踢旧次数
//! - event_bus_push_total：event_bus push 次数
//! - event_bus_dropped_total：限流丢弃数
//! - broadcast_total：broadcast 次数
//! - replay_hits_total：replay 命中次数
//! - replay_misses_total：replay 未命中（resync）次数
//!
//! 线程安全：AtomicU64，关键路径 inc（纳秒级）。

use std::sync::atomic::{AtomicU64, Ordering};

/// session crate 的运行态计数器集合。
#[derive(Debug, Default)]
pub struct SessionMetrics {
    /// 活跃连接数（registry 调，反映真实连接表大小）。
    pub connections: AtomicU64,
    /// 踢旧连接累计次数。
    pub kick_old_total: AtomicU64,
    /// event_bus 投递累计次数。
    pub event_bus_push_total: AtomicU64,
    /// event_bus 限流丢弃累计次数。
    pub event_bus_dropped_total: AtomicU64,
    /// broadcast 累计次数。
    pub broadcast_total: AtomicU64,
    /// replay 命中累计次数。
    pub replay_hits_total: AtomicU64,
    /// replay 未命中（resync_required）累计次数。
    pub replay_misses_total: AtomicU64,
}

impl SessionMetrics {
    pub fn new() -> Self {
        Self::default()
    }

    /// 直接设置活跃连接数（registry 调，反映真实连接表大小）。
    pub fn set_connections(&self, n: u64) {
        self.connections.store(n, Ordering::Relaxed);
    }

    pub fn inc_kick_old(&self) {
        self.kick_old_total.fetch_add(1, Ordering::Relaxed);
    }

    pub fn inc_event_bus_push(&self, n: u64) {
        self.event_bus_push_total.fetch_add(n, Ordering::Relaxed);
    }

    pub fn inc_event_bus_dropped(&self) {
        self.event_bus_dropped_total.fetch_add(1, Ordering::Relaxed);
    }

    pub fn inc_broadcast(&self) {
        self.broadcast_total.fetch_add(1, Ordering::Relaxed);
    }

    pub fn inc_replay_hit(&self) {
        self.replay_hits_total.fetch_add(1, Ordering::Relaxed);
    }

    pub fn inc_replay_miss(&self) {
        self.replay_misses_total.fetch_add(1, Ordering::Relaxed);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_inc_counters() {
        let m = SessionMetrics::new();
        m.inc_kick_old();
        m.inc_event_bus_push(2);
        m.inc_event_bus_dropped();
        m.inc_broadcast();
        m.inc_replay_hit();
        m.inc_replay_miss();
        assert_eq!(m.connections.load(Ordering::Relaxed), 0);
        assert_eq!(m.kick_old_total.load(Ordering::Relaxed), 1);
        assert_eq!(m.event_bus_push_total.load(Ordering::Relaxed), 2);
        assert_eq!(m.event_bus_dropped_total.load(Ordering::Relaxed), 1);
        assert_eq!(m.broadcast_total.load(Ordering::Relaxed), 1);
        assert_eq!(m.replay_hits_total.load(Ordering::Relaxed), 1);
        assert_eq!(m.replay_misses_total.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn test_set_connections() {
        let m = SessionMetrics::new();
        m.set_connections(42);
        assert_eq!(m.connections.load(Ordering::Relaxed), 42);
    }
}

//! session crate 自采 A 类指标（监控设计 §三 通道1）。
//!
//! 指标（监控设计 §三 通道1 表）：
//! - session.connections（gauge）：活跃连接数
//! - session.kick_old_total（counter）：踢旧次数
//! - session.event_bus_push_total（counter）：event_bus push 次数
//! - session.event_bus_dropped_total（counter）：限流丢弃数
//! - session.broadcast_total（counter）：broadcast 次数
//! - session.replay_hits_total（counter）：replay 命中次数
//! - session.replay_misses_total（counter）：replay 未命中（resync）次数
//!
//! 线程安全：AtomicU64，关键路径 inc（纳秒级）。
//! 聚合器周期性调 snapshot() 拉快照写入聚合器。

use std::sync::atomic::{AtomicU64, Ordering};

/// session crate 的运行态计数器集合。
#[derive(Debug, Default)]
pub struct SessionMetrics {
    /// 活跃连接数（gauge：set）。
    pub connections: AtomicU64,
    /// 踢旧连接累计次数（counter）。
    pub kick_old_total: AtomicU64,
    /// event_bus 投递累计次数（counter）。
    pub event_bus_push_total: AtomicU64,
    /// event_bus 限流丢弃累计次数（counter）。
    pub event_bus_dropped_total: AtomicU64,
    /// broadcast 累计次数（counter）。
    pub broadcast_total: AtomicU64,
    /// replay 命中累计次数（counter）。
    pub replay_hits_total: AtomicU64,
    /// replay 未命中（resync_required）累计次数（counter）。
    pub replay_misses_total: AtomicU64,
}

impl SessionMetrics {
    pub fn new() -> Self {
        Self::default()
    }

    /// 连接数 +1。
    pub fn inc_connection(&self) {
        self.connections.fetch_add(1, Ordering::Relaxed);
    }

    /// 连接数 -1（不低于 0）。
    pub fn dec_connection(&self) {
        // 用 fetch_sub + saturating，避免下溢
        let _ = self
            .connections
            .fetch_update(SeqCst::Relaxed, SeqCst::Relaxed, |v| Some(v.saturating_sub(1)));
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

    /// 快照所有计数器（聚合器周期性调）。
    pub fn snapshot(&self) -> SessionMetricsSnapshot {
        SessionMetricsSnapshot {
            connections: self.connections.load(Ordering::Relaxed),
            kick_old_total: self.kick_old_total.load(Ordering::Relaxed),
            event_bus_push_total: self.event_bus_push_total.load(Ordering::Relaxed),
            event_bus_dropped_total: self.event_bus_dropped_total.load(Ordering::Relaxed),
            broadcast_total: self.broadcast_total.load(Ordering::Relaxed),
            replay_hits_total: self.replay_hits_total.load(Ordering::Relaxed),
            replay_misses_total: self.replay_misses_total.load(Ordering::Relaxed),
        }
    }
}

/// 计数器用的 Ordering 别名（fetch_update 签名需要）。
type SeqCst = Ordering;

/// 一次快照（值拷贝，便于跨线程）。
#[derive(Debug, Clone, Default)]
pub struct SessionMetricsSnapshot {
    pub connections: u64,
    pub kick_old_total: u64,
    pub event_bus_push_total: u64,
    pub event_bus_dropped_total: u64,
    pub broadcast_total: u64,
    pub replay_hits_total: u64,
    pub replay_misses_total: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_inc_and_snapshot() {
        let m = SessionMetrics::new();
        m.inc_connection();
        m.inc_connection();
        m.inc_kick_old();
        m.inc_event_bus_push(2);
        m.inc_event_bus_dropped();
        m.inc_broadcast();
        m.inc_replay_hit();
        m.inc_replay_miss();
        let s = m.snapshot();
        assert_eq!(s.connections, 2);
        assert_eq!(s.kick_old_total, 1);
        assert_eq!(s.event_bus_push_total, 2);
        assert_eq!(s.event_bus_dropped_total, 1);
        assert_eq!(s.broadcast_total, 1);
        assert_eq!(s.replay_hits_total, 1);
        assert_eq!(s.replay_misses_total, 1);
    }

    #[test]
    fn test_dec_connection_saturating() {
        let m = SessionMetrics::new();
        m.dec_connection(); // 0 → 不下溢
        assert_eq!(m.snapshot().connections, 0);
        m.inc_connection();
        m.inc_connection();
        m.dec_connection();
        assert_eq!(m.snapshot().connections, 1);
    }

    #[test]
    fn test_set_connections() {
        let m = SessionMetrics::new();
        m.set_connections(42);
        assert_eq!(m.snapshot().connections, 42);
    }
}

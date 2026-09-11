//! 断线重放——per-thread 缓冲，按 message 保存（2026-09-09 裁定，ADR §7.2 修订）。
//!
//! 语义：
//! - 消息帧（载荷携带 message_id：stream_chunk/thinking_chunk 等）：按消息分组，
//!   只保存**在飞消息**；消息终态（stream_end/stream_error）到达即整组即弃——
//!   最终形态已落 message_slots（DB 真值），缓冲内不保留已完成消息的任何帧。
//! - 杂项事件（无 message_id：run 状态/生命周期等）：小环缓冲（默认 1000 条或
//!   5 分钟，先到先丢），防杂项洪泛。
//! - widget_event 族：只保留每个 widget_id 的最新一帧（状态快照语义）。
//! - 断点续传：重连握手上报 last_sequence，内核回放 (last_sequence, now]；
//!   缺口含已终态消息（缓冲即弃区间）时返回 resync_required，前端整树刷新
//!   从 DB 真值恢复。
//! - B9：interaction_request 类事件不进重放缓冲（重放过期审批无意义）。

use std::collections::{HashMap, VecDeque};
use std::time::{Duration, Instant};

use parking_lot::Mutex;

/// 事件族（ADR §7.3 三族，决定重放策略）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EventFamily {
    /// 流式族（stream_chunk/tool_*/thinking_*）— 逐条存，溢出 FIFO 丢弃。
    Stream,
    /// widget 事件族（widget_event）— 溢出时只保留每 widget_id 最新一帧。
    Widget,
    /// 交互族（interaction_request/*）— B9 不进重放缓冲。
    Interaction,
}

/// 可重放的事件记录。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplayEvent {
    pub sequence: u64,
    pub family: EventFamily,
    /// widget_id（仅 Widget 族有意义；其余族为空）。
    pub widget_id: String,
    /// 序列化后的事件载荷（重放时原样回发）。
    pub payload: String,
}

impl ReplayEvent {
    /// 构造流式族事件。
    pub fn new(sequence: u64, payload: impl Into<String>) -> Self {
        Self {
            sequence,
            family: EventFamily::Stream,
            widget_id: String::new(),
            payload: payload.into(),
        }
    }

    /// 构造 widget 族事件。
    pub fn widget(sequence: u64, widget_id: impl Into<String>, payload: impl Into<String>) -> Self {
        Self {
            sequence,
            family: EventFamily::Widget,
            widget_id: widget_id.into(),
            payload: payload.into(),
        }
    }

    /// 构造交互族事件（记录时会被拒绝）。
    pub fn interaction(sequence: u64, request_id: impl Into<String>) -> Self {
        Self {
            sequence,
            family: EventFamily::Interaction,
            widget_id: String::new(),
            payload: request_id.into(),
        }
    }
}

/// 环形缓冲配置。
#[derive(Debug, Clone, Copy)]
pub struct ReplayConfig {
    /// 杂项事件（无 message_id：run 状态/生命周期等）每 thread 最大条数（默认 1000）。
    /// 消息帧不适用此上限——其生命周期归消息终态管（终态整组即弃）。
    pub capacity: usize,
    /// 杂项事件单条存活秒数（默认 300 = 5 分钟）。
    pub ttl_secs: u64,
}

impl Default for ReplayConfig {
    fn default() -> Self {
        Self {
            capacity: 1000,
            ttl_secs: 300,
        }
    }
}

/// 单条存储事件（含记录时刻，用于 TTL 判定）。
#[derive(Debug, Clone)]
struct StoredEvent {
    event: ReplayEvent,
    recorded_at: Instant,
}

/// 单个 thread 的缓冲状态（2026-09-09 按 message 保存裁定）。
#[derive(Debug, Default)]
struct ThreadBuffer {
    /// 消息帧分组：message_id → 该消息的 delta/块帧（sequence 递增）。
    /// 消息终态（stream_end/stream_error）到达时整组即弃——最终形态已落
    /// message_slots，缓冲内只保留在飞消息。
    message_frames: HashMap<String, VecDeque<StoredEvent>>,
    /// 杂项事件（无 message_id：run 状态/生命周期等）。唯一无生命周期信号的
    /// 分区，保留 TTL/容量小环防杂项洪泛。
    misc: VecDeque<StoredEvent>,
    /// 已被淘汰/即弃（终态）的最小 sequence 边界：
    /// 所有 sequence <= evicted_below 的条目都已不在缓冲。初始 0。
    /// 注意：这是"曾经存在过的最大已淘汰 sequence"，用于检测请求区间是否含丢失。
    evicted_below: u64,
    /// 该 thread 历史最大 sequence（用于 replay 返回 latest_sequence）。
    max_sequence: u64,
}

/// 重放结果。
#[derive(Debug, Clone)]
pub enum ReplayResult {
    /// 成功回放（含 (last_sequence, now] 区间事件 + 当前最新 sequence）。
    Events {
        events: Vec<ReplayEvent>,
        latest_sequence: u64,
    },
    /// 缓冲溢出（请求区间含已丢失事件），前端需整树刷新。
    ResyncRequired,
}

/// per-thread 环形缓冲集合。
pub struct ReplayBuffer {
    config: ReplayConfig,
    buffers: Mutex<HashMap<String, ThreadBuffer>>,
}

/// 从事件载荷提取 message_id（廉价子串扫描，避免整帧 JSON 解析——流式热路径）。
/// 紧凑 serde JSON 形如 `"message_id":"a_f64fe7e..."`。
fn extract_message_id(payload: &str) -> Option<String> {
    const KEY: &str = "\"message_id\":\"";
    let i = payload.find(KEY)? + KEY.len();
    let rest = &payload[i..];
    let end = rest.find('"')?;
    if end == 0 {
        return None;
    }
    Some(rest[..end].to_string())
}

/// 消息终态判定：stream_end/stream_error（与 capability_router 的
/// clear_chunk 触发点同词表）——该消息最终形态已落 message_slots，
/// 缓冲内的 delta/块帧不再有重放价值。
fn is_message_terminal(payload: &str) -> bool {
    payload.contains("\"stream_end\"") || payload.contains("\"stream_error\"")
}

impl ReplayBuffer {
    /// 用指定配置创建。
    pub fn new(config: ReplayConfig) -> Self {
        Self {
            config,
            buffers: Mutex::new(HashMap::new()),
        }
    }

    /// 记录一个事件到 thread 的缓冲。
    ///
    /// 返回是否被接受：交互族（B9）返回 false（不进缓冲），其余 true。
    ///
    /// 按 message 保存（2026-09-09 裁定）：载荷携带 message_id 的帧入该消息的
    /// 分组；消息终态（stream_end/stream_error）到达时**整组即弃**——最终形态
    /// 已落 message_slots，终态帧本身也不入缓冲，evicted_below 推进到终态
    /// sequence（缺口客户端走 resync 整树刷新，从 DB 真值恢复）。
    ///
    /// 空线程条目回收：无在飞消息且杂项经 evict_misc（TTL/容量）淘汰后为空
    /// 时移除该 thread 条目（连 evicted_below/max_sequence 一起释放），防长
    /// 运行空壳累积——回收对外不可见：该 thread 再来事件走上方 or_default
    /// 重建，replay 对无缓冲 thread 返回空 Events（既有行为）。
    pub async fn record(&self, thread_id: &str, event: ReplayEvent) -> bool {
        // B9：交互族不进重放缓冲
        if event.family == EventFamily::Interaction {
            return false;
        }
        let message_id = extract_message_id(&event.payload);
        let mut buffers = self.buffers.lock();
        let buf = buffers.entry(thread_id.to_string()).or_default();
        buf.max_sequence = buf.max_sequence.max(event.sequence);
        match message_id {
            Some(mid) => {
                if is_message_terminal(&event.payload) {
                    if let Some(group) = buf.message_frames.remove(&mid) {
                        if let Some(last) = group.back() {
                            buf.evicted_below = buf.evicted_below.max(last.event.sequence);
                        }
                    }
                    buf.evicted_below = buf.evicted_below.max(event.sequence);
                    // 终态路径同样跑杂项淘汰——空缓冲判定建立在淘汰后的杂项上
                    Self::evict_misc(buf, self.config);
                } else {
                    buf.message_frames
                        .entry(mid)
                        .or_default()
                        .push_back(StoredEvent {
                            event,
                            recorded_at: Instant::now(),
                        });
                }
            }
            None => {
                buf.misc.push_back(StoredEvent {
                    event,
                    recorded_at: Instant::now(),
                });
                Self::evict_misc(buf, self.config);
            }
        }
        if buf.message_frames.is_empty() && buf.misc.is_empty() {
            buffers.remove(thread_id);
        }
        true
    }

    /// 回放 (last_sequence, now] 区间事件。
    pub async fn replay(&self, thread_id: &str, last_sequence: u64) -> ReplayResult {
        let mut buffers = self.buffers.lock();
        let Some(buf) = buffers.get_mut(thread_id) else {
            // 该 thread 无缓冲：无事件可回放，latest=last_sequence（无新增）
            return ReplayResult::Events {
                events: Vec::new(),
                latest_sequence: last_sequence,
            };
        };
        // 先做杂项过期淘汰，更新 evicted_below
        Self::evict_misc(buf, self.config);

        // 检测请求区间是否含已丢失事件：
        // 若 last_sequence < evicted_below，说明 (last_sequence, evicted_below] 已丢
        //（含已终态消息的帧区间——客户端应整树刷新从 DB 真值恢复）
        if last_sequence < buf.evicted_below {
            return ReplayResult::ResyncRequired;
        }

        // 收集杂项 + 全部在飞消息分组中 sequence > last_sequence 的事件，按序回放
        let mut events: Vec<ReplayEvent> = buf
            .misc
            .iter()
            .chain(buf.message_frames.values().flat_map(|g| g.iter()))
            .filter(|s| s.event.sequence > last_sequence)
            .map(|s| s.event.clone())
            .collect();
        events.sort_by_key(|e| e.sequence);
        ReplayResult::Events {
            events,
            latest_sequence: buf.max_sequence,
        }
    }

    /// 杂项事件淘汰：先删过期，再按容量 FIFO 删最旧（widget 中间帧可丢）。
    ///
    /// 关键：`evicted_below` 只在淘汰**流式族**事件时推进——widget 帧丢失
    /// 是状态快照语义（中间帧可丢，最新帧仍在），不构成触发 resync 的数据丢失。
    /// 消息帧不在此列——其生命周期归消息终态管（record 时整组即弃）。
    fn evict_misc(buf: &mut ThreadBuffer, config: ReplayConfig) {
        let ttl = Duration::from_secs(config.ttl_secs);
        let now = Instant::now();

        // 1. 过期淘汰：从队首删 recorded_at + ttl < now 的杂项条目
        while buf
            .misc
            .front()
            .is_some_and(|front| now.duration_since(front.recorded_at) > ttl)
        {
            if let Some(removed) = buf.misc.pop_front() {
                if removed.event.family == EventFamily::Stream {
                    buf.evicted_below = buf.evicted_below.max(removed.event.sequence);
                }
            }
        }

        // 2. 容量淘汰：超过容量时从队首删（FIFO）。
        //    widget 中间帧可丢，不推进 evicted_below（最新帧仍在缓冲内）。
        while buf.misc.len() > config.capacity {
            if let Some(removed) = buf.misc.pop_front() {
                if removed.event.family == EventFamily::Stream {
                    buf.evicted_below = buf.evicted_below.max(removed.event.sequence);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn replay_event_constructors_set_family() {
        assert_eq!(ReplayEvent::new(1, "x").family, EventFamily::Stream);
        let w = ReplayEvent::widget(1, "wid", "x");
        assert_eq!(w.family, EventFamily::Widget);
        assert_eq!(w.widget_id, "wid");
        assert_eq!(
            ReplayEvent::interaction(1, "r").family,
            EventFamily::Interaction
        );
    }
}

#[cfg(test)]
mod message_lifecycle_tests {
    use super::*;

    /// 构造携带 message_id 的流式帧载荷。
    fn delta(seq: u64, mid: &str, text: &str) -> ReplayEvent {
        ReplayEvent::new(
            seq,
            format!(
                r#"{{"type":"text_delta","data":{{"message_id":"{mid}","text":"{text}"}},"sequence":{seq}}}"#
            ),
        )
    }

    fn terminal(seq: u64, mid: &str) -> ReplayEvent {
        ReplayEvent::new(
            seq,
            format!(r#"{{"type":"stream_end","data":{{"message_id":"{mid}"}},"sequence":{seq}}}"#),
        )
    }

    #[tokio::test]
    async fn in_flight_message_frames_replay_to_reconnecting_client() {
        // 在飞消息 m1 的帧 5..9，客户端 last=7 → 只补 8、9
        let buf = ReplayBuffer::new(ReplayConfig::default());
        for seq in 5..=9 {
            assert!(buf.record("t", delta(seq, "m1", "字")).await);
        }
        match buf.replay("t", 7).await {
            ReplayResult::Events {
                events,
                latest_sequence,
            } => {
                let seqs: Vec<u64> = events.iter().map(|e| e.sequence).collect();
                assert_eq!(seqs, vec![8, 9], "只回放水位之后的帧");
                assert_eq!(latest_sequence, 9);
            }
            other => panic!("期望 Events，实际 {other:?}"),
        }
    }

    #[tokio::test]
    async fn terminal_event_drops_message_group_and_advances_evicted_below() {
        let buf = ReplayBuffer::new(ReplayConfig::default());
        // 一条未过期杂项保住条目不触发空回收（空缓冲回收语义由下方
        // terminal_drop_recycles_* 系列专测）——本测试聚焦终态推进 evicted_below
        assert!(buf.record("t", misc(4)).await);
        for seq in 5..=9 {
            assert!(buf.record("t", delta(seq, "m1", "字")).await);
        }
        // 终态到达：m1 整组即弃，终态帧本身不入缓冲
        assert!(buf.record("t", terminal(10, "m1")).await);

        // 断连期间消息已完成 → 缺口含已终态区间 → resync（前端整树刷新从 DB 恢复）
        match buf.replay("t", 0).await {
            ReplayResult::ResyncRequired => {}
            other => panic!("期望 ResyncRequired，实际 {other:?}"),
        }
        // 水位在终态之后：无事件可补，latest=10
        match buf.replay("t", 10).await {
            ReplayResult::Events {
                events,
                latest_sequence,
            } => {
                assert!(events.is_empty(), "已完成消息零帧残留");
                assert_eq!(latest_sequence, 10);
            }
            other => panic!("期望 Events，实际 {other:?}"),
        }
    }

    #[tokio::test]
    async fn misc_events_without_message_id_keep_small_ring() {
        // 无 message_id 的杂项事件走小环（capacity=3）
        let buf = ReplayBuffer::new(ReplayConfig {
            capacity: 3,
            ttl_secs: 300,
        });
        for seq in 1..=5 {
            assert!(
                buf.record(
                    "t",
                    ReplayEvent::new(
                        seq,
                        format!(r#"{{"type":"run_finished","sequence":{seq}}}"#)
                    )
                )
                .await
            );
        }
        // 水位 2（seq 1、2 已被容量 FIFO 逐出）：回放 (2, now] = 3、4、5
        match buf.replay("t", 2).await {
            ReplayResult::Events {
                events,
                latest_sequence,
            } => {
                let seqs: Vec<u64> = events.iter().map(|e| e.sequence).collect();
                assert_eq!(seqs, vec![3, 4, 5], "杂项环按容量 FIFO");
                assert_eq!(latest_sequence, 5);
            }
            other => panic!("期望 Events，实际 {other:?}"),
        }
    }

    #[test]
    fn extract_message_id_and_terminal_detection() {
        let p = r#"{"type":"text_delta","data":{"message_id":"m_abc","text":"字"},"sequence":1}"#;
        assert_eq!(extract_message_id(p).as_deref(), Some("m_abc"));
        assert!(!is_message_terminal(p));
        let end = r#"{"type":"stream_end","data":{"message_id":"m_abc"},"sequence":2}"#;
        assert_eq!(extract_message_id(end).as_deref(), Some("m_abc"));
        assert!(is_message_terminal(end));
        assert_eq!(extract_message_id(r#"{"type":"run_finished"}"#), None);
    }

    /// 构造无 message_id 的杂项事件（run 状态族）。
    fn misc(seq: u64) -> ReplayEvent {
        ReplayEvent::new(seq, format!(r#"{{"type":"run_started","sequence":{seq}}}"#))
    }

    // ── 空线程条目回收（终态组即弃 + 杂项淘汰后整缓冲空 → 移除条目）──

    #[tokio::test]
    async fn terminal_drop_recycles_thread_entry_when_misc_empty() {
        let buf = ReplayBuffer::new(ReplayConfig::default());
        for seq in 1..=3 {
            assert!(buf.record("t", delta(seq, "m1", "字")).await);
        }
        // 终态组即弃 + 杂项本就为空 → record 触发回收
        assert!(buf.record("t", terminal(4, "m1")).await);
        assert!(
            !buf.buffers.lock().contains_key("t"),
            "整缓冲空的 thread 条目应被回收"
        );
        // 回收对外不可见：replay 对无缓冲 thread 返回空 Events（既有行为）
        match buf.replay("t", 0).await {
            ReplayResult::Events {
                events,
                latest_sequence,
            } => {
                assert!(events.is_empty());
                assert_eq!(latest_sequence, 0, "无缓冲 thread latest=请求水位");
            }
            other => panic!("期望 Events，实际 {other:?}"),
        }
    }

    #[tokio::test]
    async fn terminal_drop_recycles_thread_entry_after_misc_expiry() {
        // 杂项 TTL 1s：空缓冲判定须建立在 evict_misc 淘汰后（淘汰前 misc 非空）
        let buf = ReplayBuffer::new(ReplayConfig {
            capacity: 100,
            ttl_secs: 1,
        });
        assert!(buf.record("t", misc(1)).await);
        assert!(buf.record("t", delta(2, "m1", "字")).await);
        tokio::time::sleep(std::time::Duration::from_millis(1100)).await;
        // 杂项已过期：终态组即弃 + 杂项过期淘汰 → 整缓冲空 → 回收
        assert!(buf.record("t", terminal(3, "m1")).await);
        assert!(
            !buf.buffers.lock().contains_key("t"),
            "杂项过期淘汰后整缓冲空应回收（连 evicted_below/max_sequence 一起释放）"
        );
    }

    #[tokio::test]
    async fn active_thread_with_in_flight_message_is_not_recycled() {
        let buf = ReplayBuffer::new(ReplayConfig::default());
        for seq in 1..=2 {
            assert!(buf.record("t", delta(seq, "m1", "字")).await);
        }
        // m2 仍在飞（帧 3 在 m1 终态前、帧 5 在其后）：m1 终态即弃后 m2 分组
        // 保留 → 条目不回收
        assert!(buf.record("t", delta(3, "m2", "字")).await);
        assert!(buf.record("t", terminal(4, "m1")).await);
        assert!(
            buf.buffers.lock().contains_key("t"),
            "有在飞消息的 thread 不应回收"
        );
        assert!(buf.record("t", delta(5, "m2", "字")).await);
        match buf.replay("t", 4).await {
            ReplayResult::Events {
                events,
                latest_sequence,
            } => {
                let seqs: Vec<u64> = events.iter().map(|e| e.sequence).collect();
                assert_eq!(seqs, vec![5], "在飞消息 m2 的帧应保留可回放");
                assert_eq!(latest_sequence, 5);
            }
            other => panic!("期望 Events，实际 {other:?}"),
        }
    }

    #[tokio::test]
    async fn active_thread_with_fresh_misc_is_not_recycled() {
        let buf = ReplayBuffer::new(ReplayConfig::default());
        assert!(buf.record("t", delta(1, "m1", "字")).await);
        // 杂项未过期：终态组即弃后仍有存活杂项 → 不回收
        assert!(buf.record("t", misc(2)).await);
        assert!(buf.record("t", terminal(3, "m1")).await);
        assert!(
            buf.buffers.lock().contains_key("t"),
            "杂项未过期的 thread 不应回收（空缓冲判定在淘汰后仍非空）"
        );
    }

    #[tokio::test]
    async fn recycled_thread_rebuilds_on_next_record() {
        let buf = ReplayBuffer::new(ReplayConfig::default());
        for seq in 1..=3 {
            assert!(buf.record("t", delta(seq, "m1", "字")).await);
        }
        assert!(buf.record("t", terminal(4, "m1")).await);
        assert!(!buf.buffers.lock().contains_key("t"));
        // 回收后再 record：or_default 重建，行为与从未缓冲过的 thread 一致
        assert!(buf.record("t", delta(10, "m2", "字")).await);
        assert!(buf.buffers.lock().contains_key("t"), "重建后条目应存在");
        match buf.replay("t", 0).await {
            ReplayResult::Events {
                events,
                latest_sequence,
            } => {
                let seqs: Vec<u64> = events.iter().map(|e| e.sequence).collect();
                assert_eq!(seqs, vec![10], "重建后只回放重建后的帧（旧缺口证据已释放）");
                assert_eq!(latest_sequence, 10);
            }
            other => panic!("期望 Events，实际 {other:?}"),
        }
    }
}

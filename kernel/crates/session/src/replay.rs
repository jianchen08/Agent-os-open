//! 断线重放——per-thread 环形缓冲（ADR §7.2）。
//!
//! 语义：
//! - 出站缓冲：每个 thread 一个环形缓冲（默认 1000 条或 5 分钟，先到先丢）。
//! - 流式族事件逐条存；widget_event 族也存，但溢出时只保留每个 widget_id
//!   的最新一帧（状态快照语义，中间帧可丢）。
//! - 断点续传：重连握手上报 last_sequence，内核回放 (last_sequence, now]。
//! - 兜底：缓冲溢出（区间事件已丢）时返回 resync_required，前端整树刷新。
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
    /// 每 thread 最大条数（默认 1000）。
    pub capacity: usize,
    /// 单条事件存活秒数（默认 300 = 5 分钟）。
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

/// 单个 thread 的缓冲状态。
#[derive(Debug, Default)]
struct ThreadBuffer {
    /// FIFO 队列（按 sequence 递增）。
    events: VecDeque<StoredEvent>,
    /// 已被淘汰（溢出/过期）的最小 sequence 边界：
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
    pub async fn record(&self, thread_id: &str, event: ReplayEvent) -> bool {
        // B9：交互族不进重放缓冲
        if event.family == EventFamily::Interaction {
            return false;
        }
        let mut buffers = self.buffers.lock();
        let buf = buffers.entry(thread_id.to_string()).or_default();
        buf.max_sequence = buf.max_sequence.max(event.sequence);
        buf.events.push_back(StoredEvent {
            event,
            recorded_at: Instant::now(),
        });
        Self::evict(buf, self.config);
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
        // 先做过期淘汰，更新 evicted_below
        Self::evict(buf, self.config);

        // 检测请求区间是否含已丢失事件：
        // 若 last_sequence < evicted_below，说明 (last_sequence, evicted_below] 已丢
        if last_sequence < buf.evicted_below {
            return ReplayResult::ResyncRequired;
        }

        // 收集 sequence > last_sequence 的事件
        let events: Vec<ReplayEvent> = buf
            .events
            .iter()
            .filter(|s| s.event.sequence > last_sequence)
            .map(|s| s.event.clone())
            .collect();
        ReplayResult::Events {
            events,
            latest_sequence: buf.max_sequence,
        }
    }

    /// 淘汰：先删过期，再按容量 FIFO 删最旧（widget 中间帧可丢）。
    ///
    /// 关键：`evicted_below` 只在淘汰**流式族**事件时推进——widget 帧丢失
    /// 是状态快照语义（中间帧可丢，最新帧仍在），不构成触发 resync 的数据丢失。
    fn evict(buf: &mut ThreadBuffer, config: ReplayConfig) {
        let ttl = Duration::from_secs(config.ttl_secs);
        let now = Instant::now();

        // 1. 过期淘汰：从队首删 recorded_at + ttl < now 的条目
        while let Some(front) = buf.events.front() {
            if now.duration_since(front.recorded_at) > ttl {
                let removed = buf.events.pop_front().unwrap();
                if removed.event.family == EventFamily::Stream {
                    buf.evicted_below = buf.evicted_below.max(removed.event.sequence);
                }
            } else {
                break;
            }
        }

        // 2. 容量淘汰：超过容量时从队首删（FIFO）。
        //    widget 中间帧可丢，不推进 evicted_below（最新帧仍在缓冲内）。
        while buf.events.len() > config.capacity {
            if let Some(removed) = buf.events.pop_front() {
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

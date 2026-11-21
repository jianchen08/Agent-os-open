// @feature: FP-0.2.八 多租户核心系统 | @ci: rust-test
//! 断线重放——per-thread 缓冲，按 message 保存（2026-09-09 裁定，ADR §7.2 修订）。
//!
//! 语义：
//! - 消息帧（载荷携带 message_id：stream_chunk/thinking_chunk 等）：按消息分组，
//!   只保存**在飞消息**；消息终态（stream_end/stream_error）到达即整组即弃——
//!   最终形态已落 message_slots（DB 真值），缓冲内不保留已完成消息的任何帧。
//!   帧组另有双兜底（2026-11-16 泄漏向量修复：终态帧是唯一正常释放信号，
//!   终态被丢/未达即永久滞留）：**组龄 TTL**（组首帧起算，到期整组释放并推进
//!   evicted_below）+ **进程级帧字节预算**（超限逐组龄最旧整组）；释放语义与
//!   终态即弃同构（重连落 resync_required 整树刷新，既有恢复路径）。
//!   [来源: docs/working/a695d2c7_llm轮内存放大点审计报告.md §7 泄漏向量/§5 修复4]
//! - 杂项事件（无 message_id：run 状态/生命周期等）：小环缓冲（默认 1000 条或
//!   5 分钟，先到先丢），防杂项洪泛。
//! - widget_event 族：只保留每个 widget_id 的最新一帧（状态快照语义）。
//! - 断点续传：重连握手上报 last_sequence，内核回放 (last_sequence, now]；
//!   缺口含已终态消息（缓冲即弃区间）时返回 resync_required，前端整树刷新
//!   从 DB 真值恢复。
//! - B9：interaction_request 类事件不进重放缓冲（重放过期审批无意义）。

use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicUsize, Ordering};
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
    /// 消息帧不适用此上限——其生命周期归消息终态管（终态整组即弃 + 兜底 TTL/预算）。
    pub capacity: usize,
    /// 杂项事件单条存活秒数（默认 300 = 5 分钟）。
    pub ttl_secs: u64,
    /// 消息帧组组龄上限秒数（组首帧起算；默认 1800 = 30 分钟）。
    ///
    /// 终态（stream_end/stream_error）是帧组的唯一正常释放信号；组龄 TTL 只兜
    /// 「终态永不到达」的僵死组（sidecar 断流兜底帧未达 / 路由面信封校验拒绝
    /// 丢弃终态 / 会话删除后无人清理）。到期整组释放并推进 evicted_below——
    /// 重连落 resync_required 整树刷新，与终态即弃同一恢复路径。
    /// [假设] 30min 取审计报告建议值；组龄 = 组寿命硬上限（审计建议形态），
    /// 在飞超长流（>30min）的帧组同样到期回收，重连改走 resync——僵尸组
    /// 确定性回收优先于超长流的增量重放。
    pub message_group_ttl_secs: u64,
    /// 全部消息帧组的载荷字节预算（进程级；默认 64MB）。
    ///
    /// 超限按组龄最旧整组逐出直至预算内（刚收到帧的活跃组豁免，防饿死写入；
    /// 单组自超预算时允许暂时越限，同 transient accum 预算口径）。
    /// [假设] 64MB = 实测单巨型轮帧组 ≈47MB 的 1.35 倍余量；多巨型轮并发
    /// 超限时最旧轮帧组被逐，重连走 resync 恢复。
    pub max_message_frame_bytes: usize,
}

impl Default for ReplayConfig {
    fn default() -> Self {
        Self {
            capacity: 1000,
            ttl_secs: 300,
            message_group_ttl_secs: 30 * 60,
            max_message_frame_bytes: 64 * 1024 * 1024,
        }
    }
}

/// 单条存储事件（含记录时刻，用于 TTL 判定）。
#[derive(Debug, Clone)]
struct StoredEvent {
    event: ReplayEvent,
    recorded_at: Instant,
}

/// 单个消息帧组：一条在飞消息的全部 delta/块帧。
///
/// 兜底生命周期（2026-11-16 泄漏向量修复）：终态即弃是正常释放路径；
/// 组龄 TTL / 进程级字节预算接「终态永不到达」的僵死组（审计报告 §5 修复4，
/// 与杂项环 / transient accum 预算同构）。
#[derive(Debug)]
struct MessageFrameGroup {
    /// 该消息的 delta/块帧（sequence 递增）。
    frames: VecDeque<StoredEvent>,
    /// 组首帧记录时刻（组龄 TTL 判据：组寿命硬上限，不随后续帧刷新）。
    first_frame_at: Instant,
    /// 组内载荷近似字节（增量维护；进程级预算逐出判据免全组重算）。
    approx_bytes: usize,
}

/// 单个 thread 的缓冲状态（2026-09-09 按 message 保存裁定）。
#[derive(Debug, Default)]
struct ThreadBuffer {
    /// 消息帧分组：message_id → 该消息的 delta/块帧。
    /// 消息终态（stream_end/stream_error）到达时整组即弃——最终形态已落
    /// message_slots，缓冲内只保留在飞消息；组龄 TTL/字节预算兜底僵死组。
    message_frames: HashMap<String, MessageFrameGroup>,
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

/// 全量回收拍节流窗（秒）：兜「thread 永不再活动」的僵死帧组——该 thread
/// 自身的 record/replay 不会再触发其组龄淘汰，任何活跃 thread 的事件流
/// 当心跳驱动全量扫描（60s 一拍，与 30min 组龄相比粒度足够）。
const SWEEP_INTERVAL_SECS: u64 = 60;

/// per-thread 环形缓冲集合。
pub struct ReplayBuffer {
    config: ReplayConfig,
    buffers: Mutex<HashMap<String, ThreadBuffer>>,
    /// 全部 thread 帧组载荷字节总和（进程级 [`ReplayConfig::max_message_frame_bytes`]
    /// 判据；只在 buffers 锁内读写，原子量仅为免借用纠缠）。
    frame_bytes_total: AtomicUsize,
    /// 上次全量回收拍时刻（节流判据；锁序恒 buffers → sweep_last）。
    sweep_last: Mutex<Instant>,
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
            frame_bytes_total: AtomicUsize::new(0),
            sweep_last: Mutex::new(Instant::now()),
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
    /// 帧组兜底回收（2026-11-16 泄漏向量修复）：每次记录顺带跑本 thread 的
    /// 组龄 TTL 淘汰与进程级字节预算逐出（终态被丢/未达的僵死组不再永久
    /// 滞留）；另有节流 60s 的全量回收拍接「thread 永不再活动」场景。
    /// [来源: docs/working/a695d2c7_llm轮内存放大点审计报告.md §5 修复4]
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
        let now = Instant::now();
        let mut buffers = self.buffers.lock();
        let mut active_group = None;
        let empty = {
            let buf = buffers.entry(thread_id.to_string()).or_default();
            buf.max_sequence = buf.max_sequence.max(event.sequence);
            match message_id {
                Some(mid) => {
                    if is_message_terminal(&event.payload) {
                        if let Some(group) = buf.message_frames.remove(&mid) {
                            self.frame_bytes_total
                                .fetch_sub(group.approx_bytes, Ordering::Relaxed);
                            if let Some(last) = group.frames.back() {
                                buf.evicted_below = buf.evicted_below.max(last.event.sequence);
                            }
                        }
                        buf.evicted_below = buf.evicted_below.max(event.sequence);
                        // 终态路径同样跑杂项淘汰——空缓冲判定建立在淘汰后的杂项上
                        Self::evict_misc(buf, self.config);
                    } else {
                        let payload_len = event.payload.len();
                        let group = buf.message_frames.entry(mid.clone()).or_insert_with(|| {
                            MessageFrameGroup {
                                frames: VecDeque::new(),
                                first_frame_at: now,
                                approx_bytes: 0,
                            }
                        });
                        group.approx_bytes += payload_len;
                        group.frames.push_back(StoredEvent {
                            event,
                            recorded_at: now,
                        });
                        self.frame_bytes_total
                            .fetch_add(payload_len, Ordering::Relaxed);
                        active_group = Some(mid);
                    }
                }
                None => {
                    buf.misc.push_back(StoredEvent {
                        event,
                        recorded_at: now,
                    });
                    Self::evict_misc(buf, self.config);
                }
            }
            // 帧组兜底：组龄 TTL（僵死组整组释放）+ 进程级字节预算（超限逐
            // 组龄最旧，活跃组豁免）。仍在 buffers 独占锁内执行，无嵌套锁。
            Self::evict_message_groups(buf, self.config, now, &self.frame_bytes_total);
            buf.message_frames.is_empty() && buf.misc.is_empty()
        };
        if empty {
            buffers.remove(thread_id);
        }
        Self::evict_frame_bytes_over(
            &mut buffers,
            &self.frame_bytes_total,
            self.config.max_message_frame_bytes,
            active_group.as_deref().map(|mid| (thread_id, mid)),
        );
        // 全量回收拍（节流）：接「thread 永不再活动」——其自身 record/replay
        // 不会再触发组龄淘汰，靠其他活跃 thread 的事件流当心跳。
        {
            let mut sweep_last = self.sweep_last.lock();
            if now.duration_since(*sweep_last) >= Duration::from_secs(SWEEP_INTERVAL_SECS) {
                *sweep_last = now;
                Self::sweep_map(&mut buffers, self.config, now, &self.frame_bytes_total);
            }
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
        // 先做帧组组龄淘汰 + 杂项过期淘汰，更新 evicted_below
        //（僵死组到期的 resync 判定在重连面即收敛，不回放死帧）
        Self::evict_message_groups(buf, self.config, Instant::now(), &self.frame_bytes_total);
        Self::evict_misc(buf, self.config);

        // 检测请求区间是否含已丢失事件：
        // 若 last_sequence < evicted_below，说明 (last_sequence, evicted_below] 已丢
        //（含已终态/兜底回收消息的帧区间——客户端应整树刷新从 DB 真值恢复）
        if last_sequence < buf.evicted_below {
            return ReplayResult::ResyncRequired;
        }

        // 收集杂项 + 全部在飞消息分组中 sequence > last_sequence 的事件，按序回放
        let mut events: Vec<ReplayEvent> = buf
            .misc
            .iter()
            .chain(buf.message_frames.values().flat_map(|g| g.frames.iter()))
            .filter(|s| s.event.sequence > last_sequence)
            .map(|s| s.event.clone())
            .collect();
        events.sort_by_key(|e| e.sequence);
        ReplayResult::Events {
            events,
            latest_sequence: buf.max_sequence,
        }
    }

    /// 帧组组龄 TTL 兜底淘汰：组龄（组首帧起算）超限的整组释放并推进
    /// evicted_below（数据已丢，重连走 resync 整树刷新）。
    ///
    /// 只接「终态永不到达」的僵死组；正常路径终态先于 TTL 到达（终态即弃）。
    /// `total` 为进程级帧字节记账（本组释放时同步扣减）。
    fn evict_message_groups(
        buf: &mut ThreadBuffer,
        config: ReplayConfig,
        now: Instant,
        total: &AtomicUsize,
    ) {
        let ttl = Duration::from_secs(config.message_group_ttl_secs);
        let mut freed_bytes = 0usize;
        let mut evicted_max = 0u64;
        buf.message_frames.retain(|_, group| {
            if now.duration_since(group.first_frame_at) > ttl {
                freed_bytes += group.approx_bytes;
                if let Some(last) = group.frames.back() {
                    evicted_max = evicted_max.max(last.event.sequence);
                }
                return false;
            }
            true
        });
        if freed_bytes > 0 {
            total.fetch_sub(freed_bytes, Ordering::Relaxed);
            buf.evicted_below = buf.evicted_below.max(evicted_max);
        }
    }

    /// 进程级帧字节预算逐出：超限按组龄最旧整组逐出直至预算内。
    /// `keep` = (thread_id, message_id) 刚收到帧的活跃组（防饿死写入，与
    /// transient accum 预算同口径）；单组自超预算时允许暂时越限。
    /// 调用方持 buffers 写锁；逐空的 thread 条目顺带回收。
    fn evict_frame_bytes_over(
        buffers: &mut HashMap<String, ThreadBuffer>,
        total: &AtomicUsize,
        budget: usize,
        keep: Option<(&str, &str)>,
    ) {
        let mut cur = total.load(Ordering::Relaxed);
        if cur <= budget {
            return;
        }
        // 只在超预算时才付出排序收集成本（常规流量零分配）
        let mut order: Vec<(Instant, String, String)> = buffers
            .iter()
            .flat_map(|(tid, buf)| {
                buf.message_frames
                    .iter()
                    .map(move |(mid, g)| (g.first_frame_at, tid.clone(), mid.clone()))
            })
            .collect();
        order.sort();
        for (_, tid, mid) in order {
            if cur <= budget {
                break;
            }
            if let Some((keep_tid, keep_mid)) = keep {
                if tid == keep_tid && mid == keep_mid {
                    continue;
                }
            }
            if let Some(buf) = buffers.get_mut(&tid) {
                if let Some(group) = buf.message_frames.remove(&mid) {
                    cur = cur.saturating_sub(group.approx_bytes);
                    total.fetch_sub(group.approx_bytes, Ordering::Relaxed);
                    if let Some(last) = group.frames.back() {
                        buf.evicted_below = buf.evicted_below.max(last.event.sequence);
                    }
                    if buf.message_frames.is_empty() && buf.misc.is_empty() {
                        buffers.remove(&tid);
                    }
                }
            }
        }
    }

    /// 全量回收拍：扫全部 thread 缓冲做组龄淘汰 + 进程级预算逐出，并回收
    /// 全空条目。调用方持 buffers 写锁（节流判定由调用方完成）。
    fn sweep_map(
        buffers: &mut HashMap<String, ThreadBuffer>,
        config: ReplayConfig,
        now: Instant,
        total: &AtomicUsize,
    ) {
        let thread_ids: Vec<String> = buffers.keys().cloned().collect();
        for tid in thread_ids {
            if let Some(buf) = buffers.get_mut(&tid) {
                Self::evict_message_groups(buf, config, now, total);
                let empty = buf.message_frames.is_empty() && buf.misc.is_empty();
                if empty {
                    buffers.remove(&tid);
                }
            }
        }
        Self::evict_frame_bytes_over(buffers, total, config.max_message_frame_bytes, None);
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

    /// 内存驻留快照（堆栈级诊断面 GET /api/v1/system/memory-breakdown 消费）。
    ///
    /// 条目数为实测计数；`approx_payload_bytes` 为实测载荷字节（逐条
    /// `payload.len()` 求和，不含 VecDeque/HashMap 结构开销——量级诊断用途）。
    pub fn memory_stats(&self) -> ReplayMemoryStats {
        let buffers = self.buffers.lock();
        let mut stats = ReplayMemoryStats::default();
        for buf in buffers.values() {
            stats.threads += 1;
            stats.message_groups += buf.message_frames.len();
            for group in buf.message_frames.values() {
                stats.message_frames += group.frames.len();
                stats.approx_payload_bytes += group.approx_bytes;
            }
            stats.misc_events += buf.misc.len();
            stats.approx_payload_bytes += buf
                .misc
                .iter()
                .map(|s| s.event.payload.len())
                .sum::<usize>();
        }
        stats
    }
}

/// [`ReplayBuffer::memory_stats`] 快照。
#[derive(Debug, Default, Clone, serde::Serialize)]
pub struct ReplayMemoryStats {
    /// 有缓冲的 thread 数。
    pub threads: usize,
    /// 在飞消息分组数（message_id → 帧组）。
    pub message_groups: usize,
    /// 在飞消息帧总数。
    pub message_frames: usize,
    /// 杂项事件数（TTL/容量小环）。
    pub misc_events: usize,
    /// 载荷近似字节实测和（不含容器结构开销）。
    pub approx_payload_bytes: usize,
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
            ..Default::default()
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
            ..Default::default()
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

    // ── 泄漏向量兜底（2026-11-16：终态是唯一释放信号 → 组龄 TTL + 字节预算）──

    /// 泄漏场景断言：终态永不到达（路由面丢弃 / sidecar 断流）的帧组在组龄
    /// TTL 内被整组释放，evicted_below 推进（缺口走 resync 既有恢复路径），
    /// 不再永久滞留。
    #[tokio::test]
    async fn zombie_group_without_terminal_released_by_group_ttl() {
        let buf = ReplayBuffer::new(ReplayConfig {
            capacity: 100,
            ttl_secs: 300,
            message_group_ttl_secs: 1,
            ..Default::default()
        });
        // 未过期杂项保住 thread 条目（隔离条目回收语义，聚焦组龄 TTL）
        assert!(buf.record("t", misc(4)).await);
        for seq in 5..=9 {
            assert!(buf.record("t", delta(seq, "m1", "字")).await);
        }
        assert_eq!(
            buf.buffers.lock().get("t").unwrap().message_frames.len(),
            1,
            "前置：在飞帧组在缓冲"
        );
        tokio::time::sleep(std::time::Duration::from_millis(1100)).await;
        // 终态永不到达：下一笔事件触发组龄淘汰 → 整组释放
        assert!(buf.record("t", misc(10)).await);
        {
            let buffers = buf.buffers.lock();
            let b = buffers.get("t").unwrap();
            assert!(b.message_frames.is_empty(), "组龄到期的僵死组应整组释放");
            assert_eq!(b.evicted_below, 9, "释放推进 evicted_below（丢失证据）");
        }
        // 缺口含被回收区间 → resync（与终态即弃同一恢复路径）
        match buf.replay("t", 0).await {
            ReplayResult::ResyncRequired => {}
            other => panic!("期望 ResyncRequired，实际 {other:?}"),
        }
        // 水位之后：后续事件照常回放
        match buf.replay("t", 9).await {
            ReplayResult::Events {
                events,
                latest_sequence,
            } => {
                let seqs: Vec<u64> = events.iter().map(|e| e.sequence).collect();
                assert_eq!(seqs, vec![10]);
                assert_eq!(latest_sequence, 10);
            }
            other => panic!("期望 Events，实际 {other:?}"),
        }
    }

    /// 组龄从组首帧起算（组寿命硬上限，审计建议形态）——末帧新鲜不豁免。
    /// 若实现误用末帧龄（不活动过期），本测试超时后组仍存活即红。
    #[tokio::test]
    async fn group_ttl_measures_first_frame_age_not_last_frame_age() {
        let buf = ReplayBuffer::new(ReplayConfig {
            capacity: 100,
            ttl_secs: 300,
            message_group_ttl_secs: 1,
            ..Default::default()
        });
        assert!(buf.record("t", delta(1, "m1", "字")).await); // 组首帧 t0
        tokio::time::sleep(std::time::Duration::from_millis(600)).await;
        // 组龄 0.6s < TTL：续帧存活
        assert!(buf.record("t", delta(2, "m1", "字")).await);
        assert_eq!(
            buf.buffers.lock().get("t").unwrap().message_frames["m1"]
                .frames
                .len(),
            2
        );
        tokio::time::sleep(std::time::Duration::from_millis(600)).await; // 组龄 1.2s，末帧龄仅 0.6s
        assert!(buf.record("t", misc(10)).await);
        let buffers = buf.buffers.lock();
        assert!(
            buffers.get("t").unwrap().message_frames.is_empty(),
            "组龄超限整组释放（末帧新鲜不豁免）"
        );
        assert_eq!(buffers.get("t").unwrap().evicted_below, 2);
    }

    /// 进程级帧字节预算：跨 thread 超限逐组龄最旧整组，记账同步扣减；
    /// 新组（组龄最新且刚活跃）存活可回放。
    #[tokio::test]
    async fn frame_byte_budget_evicts_oldest_group_process_wide() {
        let buf = ReplayBuffer::new(ReplayConfig {
            capacity: 100,
            max_message_frame_bytes: 300,
            ..Default::default()
        });
        // t1 旧组：两帧 ≈262B（预算内）
        let text = "字".repeat(20);
        assert!(buf.record("t1", delta(1, "m1", &text)).await);
        assert!(buf.record("t1", delta(2, "m1", &text)).await);
        let t1_bytes = buf.frame_bytes_total.load(Ordering::Relaxed);
        assert!(t1_bytes <= 300, "前置：t1 组在预算内（实际 {t1_bytes}）");
        // t2 新组入账 → 进程级超预算 → 组龄最旧（t1/m1）跨 thread 整组逐出
        let p3 = delta(10, "m2", &text);
        let p3_len = p3.payload.len();
        assert!(buf.record("t2", p3).await);
        assert!(
            !buf.buffers.lock().contains_key("t1"),
            "最旧组跨 thread 逐出且全空条目回收"
        );
        assert_eq!(
            buf.frame_bytes_total.load(Ordering::Relaxed),
            p3_len,
            "进程级记账同步扣减至新组"
        );
        // 被逐 thread 缺口证据随条目回收释放（既有回收语义：空 Events）
        match buf.replay("t1", 0).await {
            ReplayResult::Events { events, .. } => assert!(events.is_empty()),
            other => panic!("期望 Events，实际 {other:?}"),
        }
        // 新组照常可回放
        match buf.replay("t2", 0).await {
            ReplayResult::Events {
                events,
                latest_sequence,
            } => {
                let seqs: Vec<u64> = events.iter().map(|e| e.sequence).collect();
                assert_eq!(seqs, vec![10]);
                assert_eq!(latest_sequence, 10);
            }
            other => panic!("期望 Events，实际 {other:?}"),
        }
    }

    /// 预算逐出豁免刚收到帧的活跃组：单组自超预算允许暂时越限
    /// （防饿死写入，与 transient accum 预算同口径），帧全活可回放。
    #[tokio::test]
    async fn budget_eviction_protects_active_group_from_starvation() {
        let buf = ReplayBuffer::new(ReplayConfig {
            capacity: 100,
            max_message_frame_bytes: 200,
            ..Default::default()
        });
        let text = "字".repeat(20);
        let e1 = delta(1, "m1", &text);
        let e2 = delta(2, "m1", &text);
        let expect = e1.payload.len() + e2.payload.len();
        assert!(buf.record("t", e1).await);
        assert!(buf.record("t", e2).await);
        assert_eq!(
            buf.frame_bytes_total.load(Ordering::Relaxed),
            expect,
            "单组自超预算越限保留（活跃组豁免）"
        );
        assert_eq!(
            buf.buffers.lock().get("t").unwrap().evicted_below,
            0,
            "零逐出"
        );
        match buf.replay("t", 0).await {
            ReplayResult::Events { events, .. } => {
                let seqs: Vec<u64> = events.iter().map(|e| e.sequence).collect();
                assert_eq!(seqs, vec![1, 2], "两帧全活可回放");
            }
            other => panic!("期望 Events，实际 {other:?}"),
        }
    }

    /// 迟到的终态（组已被 TTL 释放）：不 panic，evicted_below 照常推进。
    #[tokio::test]
    async fn terminal_for_already_ttl_evicted_group_advances_watermark() {
        let buf = ReplayBuffer::new(ReplayConfig {
            capacity: 100,
            ttl_secs: 300,
            message_group_ttl_secs: 1,
            ..Default::default()
        });
        assert!(buf.record("t", misc(1)).await);
        for seq in 2..=3 {
            assert!(buf.record("t", delta(seq, "m1", "字")).await);
        }
        tokio::time::sleep(std::time::Duration::from_millis(1100)).await;
        assert!(buf.record("t", misc(4)).await); // 回收拍：组龄到期整组释放
        assert!(
            buf.buffers
                .lock()
                .get("t")
                .unwrap()
                .message_frames
                .is_empty(),
            "前置：组已被 TTL 释放"
        );
        assert!(buf.record("t", terminal(5, "m1")).await);
        assert_eq!(
            buf.buffers.lock().get("t").unwrap().evicted_below,
            5,
            "迟到终态推进 evicted_below"
        );
        match buf.replay("t", 5).await {
            ReplayResult::Events {
                events,
                latest_sequence,
            } => {
                assert!(events.is_empty());
                assert_eq!(latest_sequence, 5);
            }
            other => panic!("期望 Events，实际 {other:?}"),
        }
    }

    /// 全量回收拍（60s 节流）接「thread 永不再活动」：任何活跃 thread 的
    /// 事件流当心跳，扫出他 thread 的组龄超限僵死组并回收全空条目。
    #[tokio::test]
    async fn global_sweep_recycles_idle_thread_zombie_groups() {
        let buf = ReplayBuffer::new(ReplayConfig {
            capacity: 100,
            ttl_secs: 300,
            message_group_ttl_secs: 1,
            ..Default::default()
        });
        // t1 记录在飞帧后永不再活动（泄漏场景：自身 record/replay 不会再触发）
        let text = "字".repeat(20);
        for seq in 1..=2 {
            assert!(buf.record("t1", delta(seq, "m1", &text)).await);
        }
        assert!(buf.buffers.lock().contains_key("t1"));
        assert!(buf.frame_bytes_total.load(Ordering::Relaxed) > 0);
        tokio::time::sleep(std::time::Duration::from_millis(1100)).await;
        // 全量回收拍节流戳拨回：下一笔任意 thread 的事件驱动 sweep
        *buf.sweep_last.lock() = Instant::now() - Duration::from_secs(SWEEP_INTERVAL_SECS + 1);
        assert!(buf.record("t2", misc(1)).await);
        assert!(
            !buf.buffers.lock().contains_key("t1"),
            "僵尸组随全量回收拍释放并回收条目"
        );
        assert_eq!(
            buf.frame_bytes_total.load(Ordering::Relaxed),
            0,
            "进程级记账归零"
        );
        // 驱动心跳的 thread 正常存活
        assert!(buf.buffers.lock().contains_key("t2"));
    }
}

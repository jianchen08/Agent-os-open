//! SessionCoordinator——会话顶层编排（ADR §7.2 resume）。
//!
//! 整合 ConnectionRegistry + FrontendEventBus + ReplayBuffer，提供：
//! - emit_widget / emit_stream：投递 + 同步记录到 per-thread 重放缓冲；
//! - replay_all_for_user：建连时按 watermark 一次性重放该 user 全部线程（FIX 2026-08-23）。
//!
//! 参考 0.1 `ws_handler.py:204`（_resume_pipeline_for_thread）。

use std::sync::Arc;

use serde_json::{json, Value};

use crate::event_bus::{EmitScope, FrontendEventBus};
use crate::replay::{ReplayBuffer, ReplayConfig, ReplayEvent, ReplayResult};
use crate::ConnectionRegistry;

/// 会话协调器——聚合连接注册表 / 事件总线 / 重放缓冲。
pub struct SessionCoordinator {
    registry: Arc<ConnectionRegistry>,
    bus: FrontendEventBus,
    replay: Arc<ReplayBuffer>,
    /// 监控 M2：session crate 自采计数器（监控设计 §三 通道1）。
    metrics: Arc<crate::metrics::SessionMetrics>,
}

impl SessionCoordinator {
    /// 用默认配置创建（限流 20msg/s 突发 50，重放 1000 条/5min）。
    pub fn new() -> Self {
        let registry = Arc::new(ConnectionRegistry::new());
        let bus = FrontendEventBus::new(registry.clone());
        let replay = Arc::new(ReplayBuffer::new(ReplayConfig::default()));
        let metrics = Arc::new(crate::metrics::SessionMetrics::new());
        Self {
            registry,
            bus,
            replay,
            metrics,
        }
    }

    /// 监控 M2：暴露 session 计数器句柄（聚合器周期性 snapshot）。
    pub fn metrics(&self) -> &Arc<crate::metrics::SessionMetrics> {
        &self.metrics
    }

    /// 暴露连接注册表（ws_handler 注册连接用）。
    pub fn registry(&self) -> &Arc<ConnectionRegistry> {
        &self.registry
    }

    /// 枚举当前内存中的线程列表（thread_id, user_id）。
    pub fn list_threads(&self) -> Vec<(String, String)> {
        self.registry.list_threads()
    }

    /// 注册连接（单连接踢旧）。
    pub fn register(&self, user_id: &str, sink: Arc<dyn crate::EventSink>) -> Option<u64> {
        let kicked = self.registry.register(user_id, sink);
        if let Some(old) = &kicked {
            // 踢旧必须真正关闭旧连接（connection_registry 注释要求的 CLOSE_CODE_KICKED 语义落地），
            // 只换注册表会让旧 socket 变幽灵连接：收不到事件、也永不退出。
            old.shutdown();
            self.metrics.inc_kick_old();
        }
        // 活跃连接数 = 注册表大小（gauge，每次 register 后同步真实值）
        self.metrics
            .set_connections(self.registry.active_count() as u64);
        kicked.map(|k| k.id())
    }

    /// 建立 thread→user 映射。
    pub fn register_thread(&self, thread_id: &str, user_id: &str) {
        self.bus.register_thread(thread_id, user_id);
    }

    /// 发送任意类型事件到 thread scope（绕过 widget_envelope 硬编码）。
    ///
    /// 与已删除的 emit_widget/emit_stream 的区别：那两个方法最终都走 bus.emit_inner，
    /// 而 build_envelope 硬编码 type="widget_event"（见 event_bus.rs），发不出
    /// new_message / stream_start / stream_end 等聊天协议事件。本方法直接构建
    /// payload + registry.send_to_thread，支持任意 type，是**唯一的 thread 单播出口**
    /// （widget 单播/流式 chunk/聊天事件全走它；全局广播走 broadcast_widget）。
    ///
    /// 用于聊天流式闭环：dispatch_user_input 把引擎结果包成 new_message 推回前端。
    /// 不经限流（聊天事件低频，单次推送）。
    pub async fn emit_event(&self, thread_id: &str, event_type: &str, data: Value) -> bool {
        let sequence = self
            .bus
            .next_sequence(&EmitScope::Thread(thread_id.to_string()))
            .await;
        let payload = serde_json::json!({
            "type": event_type,
            "data": data,
            "sequence": sequence,
        });
        let payload_str = serde_json::to_string(&payload).unwrap_or_default();
        let delivered = self.registry.send_to_thread(thread_id, &payload_str).await;
        if delivered {
            self.metrics.inc_event_bus_push(1);
        } else {
            self.metrics.inc_event_bus_dropped();
        }
        // B4：记录到重放缓冲，让断线重连能回放聊天事件（new_message/stream_start 等）。
        // 交互族（interaction_*）走 interaction family，record() 会拒绝（B9：重放过期审批无意义）；
        // 其余走 Stream 族，正常缓冲。与 emit_widget/emit_stream 一致（它们也记录）。
        let ev = if event_type.starts_with("interaction_") {
            ReplayEvent::interaction(sequence, payload_str.clone())
        } else {
            ReplayEvent::new(sequence, payload_str.clone())
        };
        self.replay.record(thread_id, ev).await;
        delivered
    }

    /// 广播一个 widget 事件到**全部活跃连接**（EmitScope::Broadcast，
    /// 监控设计 §六 形态2 statusBar 实时数字）。
    ///
    /// 与 emit_event（thread 单播）的区别：本方法广播给所有连接，
    /// 且不进 thread 级重放缓冲（广播事件不重放）。用于状态栏类全局推送。
    pub async fn broadcast_widget(
        &self,
        widget_id: &str,
        event: &str,
        data: Value,
        plugin_id: &str,
    ) -> usize {
        let (delivered, _seq) = self
            .bus
            .emit_with_sequence(widget_id, event, data, EmitScope::Broadcast, plugin_id)
            .await;
        // 监控 M2：broadcast 计数
        self.metrics.inc_broadcast();
        if delivered > 0 {
            self.metrics.inc_event_bus_push(delivered as u64);
        }
        delivered
    }

    /// B3：仅回放（不重注册连接、不发连接确认）——连接已建立、确认已发，
    /// 在首个 thread 注册时补发断线期间该 thread 缺失的事件。
    /// `last_sequence` 是前端全局 watermark（0.2 sequence 已为全局空间，单 cursor 正确）。
    pub async fn replay_missed(&self, thread_id: &str, _user_id: &str, last_sequence: u64) {
        match self.replay.replay(thread_id, last_sequence).await {
            ReplayResult::Events { events, .. } => {
                for ev in events {
                    // 该 thread 已 register_thread → send_to_thread 能定位到当前连接的 sink
                    self.registry.send_to_thread(thread_id, &ev.payload).await;
                }
                self.metrics.inc_replay_hit();
            }
            ReplayResult::ResyncRequired => {
                let resync = json!({"type": "resync_required", "data": {"thread_id": thread_id}});
                self.registry
                    .send_to_thread(
                        thread_id,
                        &serde_json::to_string(&resync).unwrap_or_default(),
                    )
                    .await;
                self.metrics.inc_replay_miss();
            }
        }
    }

    /// 当前全局 thread 序（重连重放的上界，见 [`EventBus::current_thread_sequence`]）。
    pub async fn current_sequence(&self) -> u64 {
        self.bus.current_thread_sequence().await
    }

    /// 断线重连：按 watermark 一次性重放该 user 名下全部线程的缓冲事件。
    ///
    /// B3（首条入站 thread_id 触发单线程重放）存在触发饥饿——前端重连后不重发
    /// active_thread_changed、心跳的 thread_id 为空，断连期间落缓冲的事件永远
    /// 等不到触发（2026-08-23 真机复现：new_message delivered=false 进缓冲，
    /// 重连后 90s 前端零帧到达，回复不显示直到手动刷新）。本方法在连接建立时
    /// 主动遍历 registry 中该 user 的全部线程补放；`floor` 之后的 sequence 由
    /// 当前活动连接实时送达，重放只发 (last_sequence, floor] 防重复。
    pub async fn replay_all_for_user(
        &self,
        user_id: &str,
        last_sequence: u64,
        floor: u64,
        sink: &Arc<dyn crate::EventSink>,
    ) {
        let threads: Vec<String> = self
            .registry
            .list_threads()
            .into_iter()
            .filter(|(_t, uid)| uid == user_id)
            .map(|(t, _u)| t)
            .collect();
        if threads.is_empty() {
            return;
        }
        let mut replayed_any = false;
        let mut resync_needed = false;
        for thread_id in &threads {
            match self.replay.replay(thread_id, last_sequence).await {
                ReplayResult::Events { events, .. } => {
                    for ev in events {
                        // 同 thread 缓冲按 sequence 升序，越过 floor 即可停
                        if ev.sequence > floor {
                            break;
                        }
                        if !sink.send_text(&ev.payload).await {
                            return;
                        }
                        replayed_any = true;
                    }
                }
                ReplayResult::ResyncRequired => resync_needed = true,
            }
        }
        if replayed_any {
            self.metrics.inc_replay_hit();
        }
        if resync_needed {
            let resync =
                json!({"type": "resync_required", "data": {"reason": "replay_buffer_overflow"}});
            let _ = sink
                .send_text(&serde_json::to_string(&resync).unwrap_or_default())
                .await;
            self.metrics.inc_replay_miss();
        }
    }
}

impl Default for SessionCoordinator {
    fn default() -> Self {
        Self::new()
    }
}

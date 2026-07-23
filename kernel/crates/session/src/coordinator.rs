//! SessionCoordinator——会话顶层编排（ADR §7.2 resume）。
//!
//! 整合 ConnectionRegistry + FrontendEventBus + ReplayBuffer，提供：
//! - emit_widget / emit_stream：投递 + 同步记录到 per-thread 重放缓冲；
//! - handle_reconnect：断线重连时踢旧连接、回放缓冲、发连接确认。
//!
//! 参考 0.1 `ws_handler.py:204`（_resume_pipeline_for_thread）。

use std::sync::Arc;

use serde_json::{json, Value};

use crate::event_bus::{EmitScope, FrontendEventBus};
use crate::replay::{EventFamily, ReplayBuffer, ReplayConfig, ReplayEvent, ReplayResult};
use crate::ConnectionRegistry;

/// 重连处理结果。
#[derive(Debug, Clone)]
pub struct ReconnectOutcome {
    /// 是否成功回放（false = resync_required）。
    pub replayed: bool,
    /// 缓冲溢出，前端需整树刷新。
    pub resync_required: bool,
    /// 被踢出的旧连接 sink id（None = 无旧连接）。
    pub kicked_old_sink_id: Option<u64>,
}

/// 会话协调器——聚合连接注册表 / 事件总线 / 重放缓冲。
pub struct SessionCoordinator {
    registry: Arc<ConnectionRegistry>,
    bus: FrontendEventBus,
    replay: Arc<ReplayBuffer>,
}

impl SessionCoordinator {
    /// 用默认配置创建（限流 20msg/s 突发 50，重放 1000 条/5min）。
    pub fn new() -> Self {
        let registry = Arc::new(ConnectionRegistry::new());
        let bus = FrontendEventBus::new(registry.clone());
        let replay = Arc::new(ReplayBuffer::new(ReplayConfig::default()));
        Self {
            registry,
            bus,
            replay,
        }
    }

    /// 用指定重放容量创建（测试用小容量触发溢出）。
    pub fn with_replay_capacity(capacity: usize) -> Self {
        let registry = Arc::new(ConnectionRegistry::new());
        let bus = FrontendEventBus::new(registry.clone());
        let replay = Arc::new(ReplayBuffer::new(ReplayConfig {
            capacity,
            ttl_secs: 300,
        }));
        Self {
            registry,
            bus,
            replay,
        }
    }

    /// 暴露连接注册表（ws_handler 注册连接用）。
    pub fn registry(&self) -> &Arc<ConnectionRegistry> {
        &self.registry
    }

    /// 注册连接（单连接踢旧）。
    pub fn register(&self, user_id: &str, sink: Arc<dyn crate::EventSink>) -> Option<u64> {
        self.registry
            .register(user_id, sink)
            .map(|kicked| kicked.id())
    }

    /// 建立 thread→user 映射。
    pub fn register_thread(&self, thread_id: &str, user_id: &str) {
        self.bus.register_thread(thread_id, user_id);
    }

    /// emit widget 事件到 thread scope，并记录到重放缓冲。
    pub async fn emit_widget(
        &self,
        thread_id: &str,
        widget_id: &str,
        event: &str,
        data: Value,
        plugin_id: &str,
    ) -> usize {
        let (delivered, seq) = self
            .bus
            .emit_with_sequence(
                widget_id,
                event,
                data.clone(),
                EmitScope::Thread(thread_id.to_string()),
                plugin_id,
            )
            .await;
        // 记录到重放缓冲（widget 族）
        let payload = serde_json::to_string(&widget_envelope(widget_id, event, data, seq, plugin_id))
            .unwrap_or_default();
        self.replay
            .record(thread_id, ReplayEvent::widget(seq, widget_id, payload))
            .await;
        delivered
    }

    /// emit 流式 chunk 到 thread scope，并记录到重放缓冲。
    pub async fn emit_stream(&self, thread_id: &str, chunk: &str) -> usize {
        let (delivered, seq) = self
            .bus
            .emit_with_sequence(
                "",
                "stream_chunk",
                json!({"chunk": chunk}),
                EmitScope::Thread(thread_id.to_string()),
                "stream",
            )
            .await;
        let payload = serde_json::to_string(&stream_envelope(chunk, seq)).unwrap_or_default();
        self.replay
            .record(thread_id, ReplayEvent::new(seq, payload))
            .await;
        delivered
    }

    /// 处理断线重连：踢旧连接 + 回放缓冲 + 发连接确认。
    pub async fn handle_reconnect(
        &self,
        thread_id: &str,
        user_id: &str,
        sink: Arc<dyn crate::EventSink>,
        last_sequence: u64,
    ) -> ReconnectOutcome {
        // 1. 注册新连接（踢旧）
        let kicked_old_sink_id = self.register(user_id, sink.clone());

        // 2. 发连接确认
        let confirmation = json!({
            "type": "connection_confirmation",
            "data": {"status": "connected", "mode": "global", "user_id": user_id},
        });
        let confirmation_str = serde_json::to_string(&confirmation).unwrap_or_default();
        let _ = sink.send_text(&confirmation_str).await;

        // 3. 回放缓冲
        match self.replay.replay(thread_id, last_sequence).await {
            ReplayResult::Events { events, .. } => {
                for ev in events {
                    let _ = sink.send_text(&ev.payload).await;
                }
                ReconnectOutcome {
                    replayed: true,
                    resync_required: false,
                    kicked_old_sink_id,
                }
            }
            ReplayResult::ResyncRequired => {
                // 通知前端整树刷新
                let resync = json!({
                    "type": "resync_required",
                    "data": {"thread_id": thread_id},
                });
                let _ = sink
                    .send_text(&serde_json::to_string(&resync).unwrap_or_default())
                    .await;
                ReconnectOutcome {
                    replayed: false,
                    resync_required: true,
                    kicked_old_sink_id,
                }
            }
        }
    }
}

impl Default for SessionCoordinator {
    fn default() -> Self {
        Self::new()
    }
}

/// widget 信封（与 FrontendEventBus::build_envelope 对齐，供重放记录用）。
fn widget_envelope(
    widget_id: &str,
    event: &str,
    data: Value,
    sequence: u64,
    plugin_id: &str,
) -> Value {
    json!({
        "type": "widget_event",
        "data": {"widget_id": widget_id, "event": event, "data": data},
        "metadata": {"source_plugin": plugin_id},
        "sequence": sequence,
    })
}

/// 流式 chunk 信封。
fn stream_envelope(chunk: &str, sequence: u64) -> Value {
    json!({
        "type": "stream_chunk",
        "data": {"chunk": chunk},
        "sequence": sequence,
    })
}

#[allow(dead_code)]
fn _family_use() -> EventFamily {
    EventFamily::Stream
}

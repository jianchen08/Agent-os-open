//! FrontendEventBus——"插件→前端"运行时总线唯一出口（ADR §3.5）。
//!
//! 职责：
//! 1. 按 scope（thread/user/broadcast）查连接注册表路由；
//! 2. 套信封 `{type:"widget_event", data:{widget_id,event,data}, metadata, sequence}`；
//! 3. per-plugin 令牌桶限流（默认 20 msg/s、突发 50），超限丢弃；
//! 4. sequence 与流式族共享同一空间（同 thread 内严格有序）。
//!
//! 铁律：插件绝不直接 speak WebSocket，只能通过 emit 把事件交给内核路由。

use std::collections::HashMap;
use std::sync::Arc;

use parking_lot::Mutex;
use serde_json::{json, Value};
use tokio::sync::Mutex as AsyncMutex;

use crate::ConnectionRegistry;

/// emit 的投递范围（ADR §3.5 第5条广播 scope）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EmitScope {
    /// 推送到 thread 关联用户（反查 thread→user→连接）。
    Thread(String),
    /// 推送到指定用户。
    User(String),
    /// 广播到全部活跃连接（statusBar 类全局更新；不进 thread 级重放缓冲）。
    Broadcast,
}

/// per-plugin 令牌桶限流配置（ADR §3.5 第6条）。
#[derive(Debug, Clone, Copy)]
pub struct RateLimitConfig {
    /// 突发上限（令牌桶容量）。
    pub burst: u32,
    /// 每秒补充的令牌数（持续速率）。
    pub refill_per_sec: u32,
}

impl Default for RateLimitConfig {
    fn default() -> Self {
        // ADR 默认：20 msg/s、突发 50
        Self {
            burst: 50,
            refill_per_sec: 20,
        }
    }
}

/// 令牌桶状态（per-plugin）。
#[derive(Debug, Clone)]
struct TokenBucket {
    tokens: f64,
    last_refill_secs: f64,
}

impl TokenBucket {
    fn new(burst: u32) -> Self {
        Self {
            tokens: burst as f64,
            last_refill_secs: now_secs(),
        }
    }

    /// 补充令牌并尝试消费 1 个。返回是否消费成功。
    fn try_consume(&mut self, cfg: RateLimitConfig) -> bool {
        let now = now_secs();
        let elapsed = now - self.last_refill_secs;
        // 补充（不超容量）
        self.tokens = (self.tokens + elapsed * cfg.refill_per_sec as f64).min(cfg.burst as f64);
        self.last_refill_secs = now;
        if self.tokens >= 1.0 {
            self.tokens -= 1.0;
            true
        } else {
            false
        }
    }
}

fn now_secs() -> f64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// FrontendEventBus——事件总线唯一出口。
pub struct FrontendEventBus {
    registry: Arc<ConnectionRegistry>,
    rate_limit: RateLimitConfig,
    /// per-plugin 令牌桶。
    buckets: Mutex<HashMap<String, TokenBucket>>,
    /// 全局 sequence 计数器（widget_event 与流式族共享，跨所有 thread 单调递增）。
    /// 全局空间——前端 GlobalWebSocket 只维护一个全局 last_sequence，后端必须用
    /// 同一全局空间才能让单 cursor 正确 watermark；per-thread 计数器会让别
    /// thread 的新事件 seq 低于全局 watermark 而漏回放。
    global_sequence: AsyncMutex<u64>,
    /// 广播 sequence 计数器（广播不进 thread 重放缓冲，但仍带 sequence 排序）。
    broadcast_sequence: AsyncMutex<u64>,
}

impl FrontendEventBus {
    /// 用默认限流（20 msg/s、突发 50）创建。
    pub fn new(registry: Arc<ConnectionRegistry>) -> Self {
        Self::with_rate_limit(registry, RateLimitConfig::default())
    }

    /// 用自定义限流配置创建（测试用小突发便于快速触发丢弃）。
    pub fn with_rate_limit(registry: Arc<ConnectionRegistry>, rate_limit: RateLimitConfig) -> Self {
        Self {
            registry,
            rate_limit,
            buckets: Mutex::new(HashMap::new()),
            global_sequence: AsyncMutex::new(0),
            broadcast_sequence: AsyncMutex::new(0),
        }
    }

    /// 建立 thread_id → user_id 映射（转发给 registry）。
    pub fn register_thread(&self, thread_id: &str, user_id: &str) {
        self.registry.register_thread(thread_id, user_id);
    }

    /// emit 一个 widget 事件到指定 scope（插件→前端总线唯一入口）。
    ///
    /// 返回成功投递的连接数（单播为 0/1，广播为 N）。被限流丢弃时返回 0（不投递）。
    pub async fn emit(
        &self,
        widget_id: &str,
        event: &str,
        data: Value,
        scope: EmitScope,
        plugin_id: &str,
    ) -> usize {
        self.emit_inner(widget_id, event, data, scope, plugin_id)
            .await
            .0
    }

    /// emit 并返回 (投递数, 分配的 sequence)。
    ///
    /// sequence 即便被限流丢弃也会返回（限流前已分配），供 coordinator 记录重放。
    /// coordinator 据此把同一 sequence 的事件写入重放缓冲，保证续传一致。
    pub async fn emit_with_sequence(
        &self,
        widget_id: &str,
        event: &str,
        data: Value,
        scope: EmitScope,
        plugin_id: &str,
    ) -> (usize, u64) {
        self.emit_inner(widget_id, event, data, scope, plugin_id)
            .await
    }

    async fn emit_inner(
        &self,
        widget_id: &str,
        event: &str,
        data: Value,
        scope: EmitScope,
        plugin_id: &str,
    ) -> (usize, u64) {
        // 1. per-plugin 限流
        if !self.acquire_token(plugin_id) {
            tracing::warn!(
                plugin = plugin_id,
                "frontend.emit 被限流丢弃（per-plugin 令牌桶耗尽）"
            );
            // 仍分配 sequence，避免被限流丢弃导致 sequence 空洞影响重放连续性
            let seq = self.next_sequence(&scope).await;
            return (0, seq);
        }

        // 2. 分配 sequence（按 scope）
        let sequence = self.next_sequence(&scope).await;

        // 3. 套信封
        let envelope = build_envelope(widget_id, event, data, sequence, plugin_id);
        let payload = serde_json::to_string(&envelope).unwrap_or_else(|_| "{}".into());

        // 4. 路由到唯一出口 push_to_*
        let delivered = match scope {
            EmitScope::Thread(thread_id) => {
                if self.registry.send_to_thread(&thread_id, &payload).await {
                    1
                } else {
                    0
                }
            }
            EmitScope::User(user_id) => {
                if self.registry.send_to_user(&user_id, &payload).await {
                    1
                } else {
                    0
                }
            }
            EmitScope::Broadcast => self.registry.broadcast(&payload).await,
        };
        (delivered, sequence)
    }

    /// 获取（或创建）plugin 的令牌桶并尝试消费 1 个令牌。
    fn acquire_token(&self, plugin_id: &str) -> bool {
        let mut buckets = self.buckets.lock();
        let bucket = buckets
            .entry(plugin_id.to_string())
            .or_insert_with(|| TokenBucket::new(self.rate_limit.burst));
        bucket.try_consume(self.rate_limit)
    }

    /// 按 scope 分配下一个 sequence。
    pub async fn next_sequence(&self, scope: &EmitScope) -> u64 {
        match scope {
            // Thread scope 用全局计数器：跨所有 thread 单调递增，前端单 last_sequence 可正确 watermark。
            EmitScope::Thread(_) => {
                let mut counter = self.global_sequence.lock().await;
                *counter += 1;
                *counter
            }
            EmitScope::User(_) => {
                // user scope 无 thread 序空间，用 broadcast 计数器（不进 thread 重放）
                let mut counter = self.broadcast_sequence.lock().await;
                *counter += 1;
                *counter
            }
            EmitScope::Broadcast => {
                let mut counter = self.broadcast_sequence.lock().await;
                *counter += 1;
                *counter
            }
        }
    }

    /// 当前全局 thread 序（只读，不递增）——重连重放上界用：register 时刻之后
    /// 分配的 sequence 必然经当前活动连接实时送达，重放只需覆盖
    /// (watermark, floor] 区间，避免与实时投递重复推送同一事件。
    pub async fn current_thread_sequence(&self) -> u64 {
        *self.global_sequence.lock().await
    }
}

/// 构造 widget_event 信封（ADR §3.5 第2条 + §7.3 事件协议）。
fn build_envelope(
    widget_id: &str,
    event: &str,
    data: Value,
    sequence: u64,
    plugin_id: &str,
) -> Value {
    json!({
        "type": "widget_event",
        "data": {
            "widget_id": widget_id,
            "event": event,
            "data": data,
        },
        "metadata": {
            "source_plugin": plugin_id,
            "ts": chrono::Utc::now().to_rfc3339(),
        },
        "sequence": sequence,
    })
}

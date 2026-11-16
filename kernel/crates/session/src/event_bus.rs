//! FrontendEventBus——"插件→前端"运行时总线唯一出口（ADR §3.5）。
//!
//! 职责：
//! 1. 按 scope（thread/user/broadcast）查连接注册表路由；
//! 2. 套信封 `{type:"widget_event", data:{widget_id,event,data}, metadata, sequence}`；
//! 3. per-plugin 令牌桶限流（按设备档取值：桌面/云 20 msg/s·突发 50，手机档
//!    收窄减半；env 可钉值，见 [`resolved_rate_limit_config`]），超限丢弃；
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
        // ADR 默认：20 msg/s、突发 50（桌面/云档现值）
        Self {
            burst: 50,
            refill_per_sec: 20,
        }
    }
}

/// 手机档持续速率（msg/s）：桌面半档（原则 P2 资源参数自适应，低端不过载）。
/// 只压 widget/广播事件（`emit` 族）——流式 chunk/聊天走 `emit_event` 不过桶，
/// 手机档收窄不影响流式体验。
pub const PHONE_REFILL_PER_SEC: u32 = 10;
/// 手机档突发容量（桌面半档）。
pub const PHONE_BURST: u32 = 25;
/// 持续速率钉值环境变量（运维逃生口：未设置/解析失败/非正值按缺席）。
pub const REFILL_PER_SEC_ENV: &str = "AGENTOS_EVENT_BUS_REFILL_PER_SEC";
/// 突发容量钉值环境变量（同上）。
pub const BURST_ENV: &str = "AGENTOS_EVENT_BUS_BURST";

/// 档位 → 令牌桶限流（纯函数；env 钉值外的默认派生）：手机档收窄至桌面半档，
/// 桌面/云维持 ADR 默认。
fn rate_limit_config_for_tier(tier: agentos_core::device_profile::DeviceTier) -> RateLimitConfig {
    match tier {
        agentos_core::device_profile::DeviceTier::Phone => RateLimitConfig {
            burst: PHONE_BURST,
            refill_per_sec: PHONE_REFILL_PER_SEC,
        },
        agentos_core::device_profile::DeviceTier::Desktop
        | agentos_core::device_profile::DeviceTier::Cloud => RateLimitConfig::default(),
    }
}

/// 解析核心（纯函数，测试注入 env/档位）：env 显式钉值 > 档位派生（与 invoker
/// `light_host_max_members` 同构的三级序，本函数承载后两级）。两字段独立解析，
/// 单字段钉值时另一字段仍取档位值。
fn resolved_rate_limit_config_from(
    refill_env: Option<&str>,
    burst_env: Option<&str>,
    tier: agentos_core::device_profile::DeviceTier,
) -> RateLimitConfig {
    let mut cfg = rate_limit_config_for_tier(tier);
    if let Some(v) = refill_env
        .and_then(|v| v.trim().parse::<u32>().ok())
        .filter(|&n| n > 0)
    {
        cfg.refill_per_sec = v;
    }
    if let Some(v) = burst_env
        .and_then(|v| v.trim().parse::<u32>().ok())
        .filter(|&n| n > 0)
    {
        cfg.burst = v;
    }
    cfg
}

/// 生产限流配置解析序：环境变量显式钉值（[`REFILL_PER_SEC_ENV`]/
/// [`BURST_ENV`]，两字段独立）优先 > 设备档位派生（原则 P2）> ADR 默认。
/// 调用面为进程启动装配（[`FrontendEventBus::new`]），非热路径，现读现算。
pub fn resolved_rate_limit_config() -> RateLimitConfig {
    resolved_rate_limit_config_from(
        std::env::var(REFILL_PER_SEC_ENV).ok().as_deref(),
        std::env::var(BURST_ENV).ok().as_deref(),
        agentos_core::device_profile::detect_device_tier(),
    )
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
    /// 用生产限流配置创建：env 钉值 > 设备档位派生（P2）> ADR 默认
    /// （20 msg/s、突发 50），见 [`resolved_rate_limit_config`]。
    pub fn new(registry: Arc<ConnectionRegistry>) -> Self {
        Self::with_rate_limit(registry, resolved_rate_limit_config())
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

#[cfg(test)]
mod tests {
    use super::*;

    /// 档位 → 限流映射区分度（≥2 档）：手机档收窄减半，桌面/云维持 ADR 默认；
    /// 性质：手机档两字段都 ≤ 桌面档（低端不比高端更宽）。
    #[test]
    fn rate_limit_config_distinct_by_tier() {
        use agentos_core::device_profile::DeviceTier;
        let phone = rate_limit_config_for_tier(DeviceTier::Phone);
        let desktop = rate_limit_config_for_tier(DeviceTier::Desktop);
        let cloud = rate_limit_config_for_tier(DeviceTier::Cloud);
        assert_eq!(
            (phone.refill_per_sec, phone.burst),
            (PHONE_REFILL_PER_SEC, PHONE_BURST),
            "手机档取收窄常量"
        );
        assert_eq!(
            (desktop.refill_per_sec, desktop.burst),
            (20, 50),
            "桌面档维持 ADR 默认"
        );
        assert_eq!(
            (cloud.refill_per_sec, cloud.burst),
            (desktop.refill_per_sec, desktop.burst),
            "云档与桌面同值"
        );
        assert_ne!(
            (phone.refill_per_sec, phone.burst),
            (desktop.refill_per_sec, desktop.burst),
            "手机与桌面必须有区分度"
        );
        assert!(phone.refill_per_sec <= desktop.refill_per_sec && phone.burst <= desktop.burst);
    }

    /// env 显式钉值优先于档位派生（host_key::light_host_max_members 同构）；
    /// 两字段独立解析，单字段钉值时另一字段仍取档位值。
    #[test]
    fn env_overrides_beat_tier_derivation() {
        use agentos_core::device_profile::DeviceTier;
        let cfg = resolved_rate_limit_config_from(Some("7"), Some("9"), DeviceTier::Phone);
        assert_eq!(
            (cfg.refill_per_sec, cfg.burst),
            (7, 9),
            "双字段钉值整体压过手机档派生"
        );
        let cfg = resolved_rate_limit_config_from(Some("7"), None, DeviceTier::Phone);
        assert_eq!(
            (cfg.refill_per_sec, cfg.burst),
            (7, PHONE_BURST),
            "仅钉速率时突发仍取档位值"
        );
        // 桌面档（派生为 None 语义的默认档）钉值同样生效。
        let cfg = resolved_rate_limit_config_from(Some(" 7 "), None, DeviceTier::Desktop);
        assert_eq!(
            (cfg.refill_per_sec, cfg.burst),
            (7, 50),
            "桌面档钉值生效且容忍首尾空白"
        );
    }

    /// 非法钉值（非数字/零/空串）逐字段回落档位派生，不整体作废另一字段。
    #[test]
    fn invalid_env_falls_through_to_tier() {
        use agentos_core::device_profile::DeviceTier;
        for bad in ["not-a-number", "0", ""] {
            let cfg = resolved_rate_limit_config_from(Some(bad), Some(bad), DeviceTier::Phone);
            assert_eq!(
                (cfg.refill_per_sec, cfg.burst),
                (PHONE_REFILL_PER_SEC, PHONE_BURST),
                "非法值 {bad:?} 必须回落手机档派生"
            );
            let cfg = resolved_rate_limit_config_from(Some(bad), None, DeviceTier::Desktop);
            assert_eq!(
                (cfg.refill_per_sec, cfg.burst),
                (20, 50),
                "非法值 {bad:?} 必须回落桌面档默认"
            );
        }
    }

    /// 生产解析链（真依赖）：env 未钉值 = 注入式解析同参结果；钉值生效后
    /// 还原。环境变量进程全局态，分支串在一个测试内执行。
    #[test]
    fn resolved_rate_limit_config_reads_env_and_tier() {
        std::env::remove_var(REFILL_PER_SEC_ENV);
        std::env::remove_var(BURST_ENV);
        let tier = agentos_core::device_profile::detect_device_tier();
        assert_eq!(
            {
                let c = resolved_rate_limit_config();
                (c.refill_per_sec, c.burst)
            },
            {
                let c = resolved_rate_limit_config_from(None, None, tier);
                (c.refill_per_sec, c.burst)
            },
            "未钉值 = 按当前档位派生"
        );

        std::env::set_var(REFILL_PER_SEC_ENV, "7");
        std::env::set_var(BURST_ENV, "9");
        let cfg = resolved_rate_limit_config();
        assert_eq!(
            (cfg.refill_per_sec, cfg.burst),
            (7, 9),
            "生产链 env 钉值生效"
        );

        std::env::remove_var(REFILL_PER_SEC_ENV);
        std::env::remove_var(BURST_ENV);
    }
}

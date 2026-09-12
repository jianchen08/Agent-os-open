// @feature: FP-0.2.一 插件协议 | @ci: rust-test
//! WS 一次性握手票据（POST /api/v1/ws-ticket → 200 {"ticket","expires_in"}）。
//!
//! 契约（前端按此消费）：
//! - `POST /api/v1/ws-ticket`（auth user，任意已认证用户）→
//!   `{"ticket": "<43 位 url-safe 随机>", "expires_in": 60}`；
//! - WS 握手 `?ticket=<ticket>` 单次消费：签发后 60s 内可用一次，过期/已用/
//!   未知一律 4001 拒绝；`?token=` 路径并存保留（外部脚本等既有消费方）。
//!
//! 票据存储为进程内 Map（不落库）：单机单进程内核部署形态下等价于全量状态，
//! 重启即失效（客户端重连走重新签发）。升级触发条件=多实例部署（届时需共享
//! 存储或粘性会话）。

use std::collections::HashMap;
use std::time::{Duration, Instant};

use axum::extract::State;
use axum::Json;
use serde::Serialize;

use crate::routes::AppState;
use agentos_http::error::ApiError;

/// 票据有效期（秒），与响应 `expires_in` 同源。
pub const TICKET_TTL_SECS: u64 = 60;

/// 票据字节长度（32 字节 → base64url 无填充 43 字符）。
const TICKET_BYTES: usize = 32;

/// 票据表条目上限：过期清理原先只在消费时做，循环取票不建连即线性增长
/// 内存——签发时顺带清理过期，仍满则逐出任意一条（同 TTL 下近似最旧；
/// 被逐出方的下次建连 4001 → 重新取票自愈）。
const TICKET_MAX_ENTRIES: usize = 4096;

/// 进程内单次消费票据表：ticket → (user_id, username, expires_at)。
pub struct WsTicketStore {
    ttl: Duration,
    tickets: parking_lot::Mutex<HashMap<String, (String, String, Instant)>>,
}

impl WsTicketStore {
    pub fn new() -> Self {
        Self::with_ttl(Duration::from_secs(TICKET_TTL_SECS))
    }

    /// 测试可注入短 TTL（过期路径无需真实等待 60s）。
    pub fn with_ttl(ttl: Duration) -> Self {
        Self {
            ttl,
            tickets: parking_lot::Mutex::new(HashMap::new()),
        }
    }

    /// 签发一张绑定 (user_id, username) 的票据，返回 43 位 url-safe 随机串。
    pub fn issue(&self, user_id: &str, username: &str) -> String {
        let ticket = random_ticket();
        let expires_at = Instant::now() + self.ttl;
        let mut tickets = self.tickets.lock();
        let now = Instant::now();
        tickets.retain(|_, (_, _, exp)| *exp > now);
        while tickets.len() >= TICKET_MAX_ENTRIES {
            let Some(victim) = tickets.keys().next().cloned() else {
                break;
            };
            tickets.remove(&victim);
        }
        tickets.insert(
            ticket.clone(),
            (user_id.to_string(), username.to_string(), expires_at),
        );
        ticket
    }

    /// 单次消费：命中且未过期 → `Some((user_id, username))` 并立即移除；
    /// 未命中 / 已消费 / 已过期 → `None`（过期条目顺带清理）。
    pub fn consume(&self, ticket: &str) -> Option<(String, String)> {
        let mut tickets = self.tickets.lock();
        let now = Instant::now();
        tickets.retain(|_, (_, _, exp)| *exp > now);
        tickets
            .remove(ticket)
            .map(|(user_id, username, _)| (user_id, username))
    }
}

impl Default for WsTicketStore {
    fn default() -> Self {
        Self::new()
    }
}

/// 43 位 url-safe 随机票据：32 随机字节（两个 v4 UUID）→ base64url 无填充。
/// 字符表 `A-Za-z0-9_-`，URL query 直传无转义问题。
fn random_ticket() -> String {
    use base64::Engine;
    let mut bytes = [0u8; TICKET_BYTES];
    let a = uuid::Uuid::new_v4();
    let b = uuid::Uuid::new_v4();
    bytes[..16].copy_from_slice(a.as_bytes());
    bytes[16..].copy_from_slice(b.as_bytes());
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

/// POST /api/v1/ws-ticket 响应体。
#[derive(Debug, Serialize)]
pub struct WsTicketResponse {
    pub ticket: String,
    pub expires_in: u64,
}

/// POST /api/v1/ws-ticket——为当前已认证用户签发一次性 WS 握手票据。
///
/// 鉴权双保险：write_surface_auth 的 ws-ticket 分支先行校验（401 出口统一），
/// handler 内 `resolve_request_user` 再解析出身份（user_id/username 绑进票据）。
pub async fn issue_ticket_handler(
    State(state): State<AppState>,
    headers: axum::http::HeaderMap,
) -> Result<Json<WsTicketResponse>, ApiError> {
    let (user_id, username, _, _) =
        agentos_http::auth::resolve_request_user(state.store.as_ref(), &headers).await?;
    Ok(Json(WsTicketResponse {
        ticket: state.ws_tickets.issue(&user_id, &username),
        expires_in: TICKET_TTL_SECS,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn ticket_is_43_char_url_safe() {
        let store = WsTicketStore::new();
        let t = store.issue("u-1", "alice");
        assert_eq!(t.len(), 43, "32 字节 base64url 无填充必须 43 字符");
        assert!(
            t.chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_'),
            "票据必须 url-safe: {t}"
        );
        // 性质断言：两次签发不重号（随机性）
        assert_ne!(t, store.issue("u-1", "alice"));
    }

    #[test]
    fn consume_is_single_use() {
        let store = WsTicketStore::new();
        let t = store.issue("u-1", "alice");
        assert_eq!(
            store.consume(&t),
            Some(("u-1".to_string(), "alice".to_string()))
        );
        assert_eq!(store.consume(&t), None, "复用必须失败（单次消费）");
        assert_eq!(store.consume("no-such-ticket"), None, "未知票据必须失败");
    }

    #[test]
    fn expired_ticket_is_rejected() {
        let store = WsTicketStore::with_ttl(Duration::from_millis(20));
        let t = store.issue("u-1", "alice");
        std::thread::sleep(Duration::from_millis(60));
        assert_eq!(store.consume(&t), None, "过期票据必须失效");
    }

    #[test]
    fn issue_bounded_and_cleans_expired() {
        // 表级设界（S2）：签发免成本 + 过期清理原先只在消费时做——循环取票
        // 不建连即线性增长内存。签发时顺带清过期 + 超限逐出，条目数钉在上限。
        // 满载逐出（60s TTL，窗口内连发超出上限）：
        let store = WsTicketStore::new();
        for _ in 0..TICKET_MAX_ENTRIES + 8 {
            store.issue("u-1", "alice");
        }
        {
            let tickets = store.tickets.lock();
            assert!(
                tickets.len() <= TICKET_MAX_ENTRIES,
                "签发任意次后条目数必须钉在 {} 以内，实际 {}",
                TICKET_MAX_ENTRIES,
                tickets.len()
            );
        }

        // 过期顺带清理：全部过期后签发一张，表内只剩新条（不因上限拒发）
        let store = WsTicketStore::with_ttl(Duration::from_millis(20));
        for _ in 0..200 {
            store.issue("u-1", "alice");
        }
        std::thread::sleep(Duration::from_millis(60));
        let fresh = store.issue("u-2", "bob");
        {
            let tickets = store.tickets.lock();
            assert_eq!(tickets.len(), 1, "过期条目必须在签发时清空");
        }
        assert_eq!(
            store.consume(&fresh),
            Some(("u-2".to_string(), "bob".to_string()))
        );
    }
}

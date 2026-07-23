//! 会话传输内核——channel/WS 内核化（ADR §7）。
//!
//! 承接 0.1 `src/channels/websocket/` 全部职责，Rust 重写。模块对应：
//! - [`connection_registry`]：user_id/thread_id → 连接，单连接踢旧（B10）
//! - [`auth`]：token 校验，握手拒绝码（4001）
//! - [`event_bus`]：FrontendEventBus，唯一出口 `push_to_*`，背压 + per-plugin 限流
//! - [`replay`]：per-thread 环形缓冲，断线重放 + 溢出 `resync_required`（B9 交互族不进重放）
//! - [`router`]：inbound `user_input`/`interaction_response`/`stop_generation` 路由
//! - [`resume`]：断线重连切活跃 pipeline sink + 离线事件重放
//!
//! [来源: docs/working/重要设计/插件能力统一模型设计.md §7]

pub mod auth;
pub mod connection_registry;
pub mod coordinator;
pub mod event_bus;
pub mod replay;
pub mod router;

pub use connection_registry::ConnectionRegistry;
pub use coordinator::SessionCoordinator;

/// 出站消息投递抽象（ADR §7.2 唯一出口 push_to_* 的底层 sink）。
///
/// session crate 不直接依赖 axum，通过此 trait 抽象 WS 文本帧发送；
/// api crate 提供 axum `WebSocket` 的适配实现。便于测试用 mock sink 验证
/// 路由/重放/限流逻辑，无需真实 WS 连接。
#[async_trait::async_trait]
pub trait EventSink: Send + Sync {
    /// 异步发送一条文本消息，返回是否成功（失败 = 连接已断/发送超时）。
    async fn send_text(&self, text: &str) -> bool;

    /// 返回 sink 的唯一身份标识（用于连接注册表去重/踢旧比较）。
    fn id(&self) -> u64;
}

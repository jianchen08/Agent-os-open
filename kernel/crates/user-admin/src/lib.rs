//! User Admin——用户管理策略面（boot-plugin 第二刀）。
//!
//! §9.6 精确拆分：auth **执行门**（登录验签/JWT 校验/路由准入——
//! `/api/v1/auth/login|logout|me|register|refresh`，前端与 WS 握手在用）
//! **永留内核**（api/src/auth.rs，不动）；本 crate 承载的是**管理性质**的
//! 用户管理策略面（用户列表/改角色/改租户/删用户——内核此前没有这些端点，
//! 本刀以插件形态新建，不再往内核路由树加管理面）。
//!
//! - [`capability`]：`user-admin` namespace 的 CapabilityHandler（4 method，
//!   注册进内核 handler_registry，agentos-kernel.rs 启动期，先于任何 sidecar
//!   spawn）；鉴权在内核侧执行（插件仅转发 Authorization 头）。
//! - HTTP 面：`plugins/shared/user_admin`（Python sidecar 插件）承载
//!   `/ext/user_admin/**`，经内核 `/ext/{*rest}` 通配分发 → 插件 `http.handle`
//!   → 反向调用 `user-admin.<method>` → 本 crate handler。
//!
//! 设计来源：docs/working/重要设计/boot-plugin内核能力插件化立项.md
//! §二/§四（用户管理/登录策略候选——第二刀）+ §五（第一刀实施记录，本刀
//! 复用同一模式）。

pub mod capability;

pub use capability::{UserAdminCapabilityHandler, UserAdminState, NAMESPACE};

//! DB Admin——通用表 CRUD + SQL 执行器（boot-plugin 第一刀：SQL 能力层）。
//!
//! 演进史：从 api crate 拆出（task_kernel_cleanup_and_split 任务 1）后，
//! 原 `/api/v1/db/*` axum 路由层已在 boot-plugin 迁移中摘除——HTTP 面由
//! `plugins/shared/db_admin`（Python sidecar 插件）承载，经内核 `/ext/db_admin/*`
//! 通配分发 → 插件 `http.handle` → 反向调用 `db-admin` capability → 本 crate 的
//! [`capability::DbAdminCapabilityHandler`]（注册进内核 handler_registry）。
//!
//! - [`db_routes`]：纯 SQL 构建与校验逻辑（白名单枚举/参数绑定/租户隔离/BLOB 安全/
//!   SQL 执行器防线——check_dangerous/classify_sql 等全部保留）；
//! - [`capability`]：capability handler（8 method）+ 鉴权（`_authorization` 转发，
//!   内核侧 resolve_request_user 复用 api 管理面同一实现）。
//!
//! 设计来源：docs/working/unified_db_admin_plan.md（表驱动动态枚举 + 安全约束）、
//! docs/working/重要设计/boot-plugin内核能力插件化立项.md §三（第一刀方案）。

pub mod capability;
pub mod db_routes;

pub use capability::{DbAdminCapabilityHandler, DbAdminState, NAMESPACE};

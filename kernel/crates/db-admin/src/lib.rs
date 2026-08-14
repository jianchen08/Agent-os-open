//! DB Admin 独立管理后台——通用表 CRUD + SQL 执行器（`/api/v1/db/*`）。
//!
//! 从 api crate 拆出（task_kernel_cleanup_and_split 任务 1）：api 回归
//! "对外服务层"本职，本 crate 成为独立"管理工具"。前端 DB Admin 后台
//! （`/debug/db`）经 `/api/v1/db/*` 观察/调试数据，7 个端点与路径保持不变，
//! 前端无感知；鉴权（admin/viewer 角色）沿用 api 管理面同一套用户解析
//! （`agentos_http::auth::resolve_request_user`，单一来源）。
//!
//! 设计来源：docs/working/unified_db_admin_plan.md（表驱动动态枚举 + 安全约束）。

pub mod db_routes;

pub use db_routes::{router, DbAdminState};

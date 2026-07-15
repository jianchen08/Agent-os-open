//! # Lingxi Engine — 管道引擎（极简调度器 + SQLite 状态账本）
//!
//! ADR ①极简主义：引擎仅为调度器与状态账本，不含业务逻辑。
//! 只负责：按配置顺序调用插件、维护状态一致性、记录变更日志（Append-Only Patch）。
//!
//! ## 模块组织
//!
//! - `store`: SQLite 四表存储实现——runs/messages/traces/blobs DDL + CRUD
//! - `engine`: AdrEngine 实现——start_run/execute_step/suspend/resume/rollback/end_run
//!
//! [来源: docs/0.2_rust_plugin_solution.md §3.6]
//! [来源: docs/working/adr_engine_design.md]
//! [来源: docs/tasks/task_06_pipeline_engine.md]

pub mod engine;
pub mod store;

pub use engine::AdrEngineImpl;
pub use store::SqliteStore;

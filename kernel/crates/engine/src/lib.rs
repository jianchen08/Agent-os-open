//! # Lingxi Engine — 管道引擎（极简调度器 + SQLite 状态账本）
//!
//! ADR ①极简主义：引擎仅为调度器与状态账本，不含业务逻辑。
//! 只负责：按配置顺序调用插件、维护状态一致性、记录变更日志（Append-Only Patch）。
//!
//! ## 模块组织
//!
//! - `store`: SQLite 四表存储实现——runs/messages/traces/blobs DDL + CRUD
//! - `engine`: AdrEngine 实现——start_run/execute_step/suspend/resume/rollback/end_run
//! - `template`: 配置模板插值器——解析 `{{state.xxx}}` / `{{path:xxx}}` 表达式
//!
//! [来源: docs/0.2_rust_plugin_solution.md §3.6]
//! [来源: docs/working/adr_engine_design.md]
//! [来源: docs/tasks/task_06_pipeline_engine.md]

pub mod condition;
pub mod engine;
pub mod metrics;
pub mod pipeline_loop;
pub mod store;
pub mod template;

pub use engine::AdrEngineImpl;
pub use metrics::{EngineMetrics, EngineMetricsSnapshot};
pub use pipeline_loop::apply_messages_op_update;
pub use pipeline_loop::apply_slot_ops_to_array;
pub use pipeline_loop::PipelineExecutor;
pub use store::SqliteStore;

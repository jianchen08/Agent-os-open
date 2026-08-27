//! # Lingxi Engine — 管道引擎（PipelineExecutor + SQLite 状态账本）
//!
//! 0.2 统一执行器：`PipelineExecutor`（pipeline_loop.rs）——按 YAML 配置调用插件、
//! 维护 state 一致性、记录变更（Append-Only Patch）。chat 与任务执行全部走
//! PipelineExecutor；capability 侧 suspend/resume 直接操作 runs 表
//! （见 kernel/crates/api/src/capability_router.rs）。
//!
//! ## 模块组织
//!
//! - `pipeline_loop`: PipelineExecutor——统一管道执行器（生产路径）
//! - `store`: SQLite 存储实现——runs/message_slots/blobs/traces/sessions 等 DDL + CRUD
//! - `template`: 配置模板插值器——解析 `{{state.xxx}}` / `{{path:xxx}}` 表达式
//!
//! [来源: docs/0.2_rust_plugin_solution.md §3.6]
//! [来源: docs/working/adr_engine_design.md]
//! [来源: docs/tasks/task_06_pipeline_engine.md]

pub mod compiler;
pub mod condition;
pub mod metrics;
pub mod pipeline_loop;
pub mod replay;
pub mod storage_factory;
pub mod store;
pub mod template;
pub mod transient;

pub use metrics::{EngineMetrics, EngineMetricsSnapshot};
pub use pipeline_loop::apply_messages_op_update;
pub use pipeline_loop::apply_slot_ops_to_array;
pub use pipeline_loop::PipelineExecutor;
pub use store::SqliteStore;
pub use store::VOLATILE_RUN_KEYS;
pub use transient::{global_registry, TransientStateRegistry};

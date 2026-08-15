//! # Lingxi Config — 配置系统
//!
//! 0.1 Python ConfigLoader + ConfigCenter 的 Rust 等价实现。
//! 确保现有 YAML 配置文件零修改即可被 Rust 内核正确加载和热重载。
//!
//! ## 模块组织
//!
//! - `loader`: 配置加载器——YAML 解析、环境变量插值 `${VAR}` / `${VAR:-default}`、
//!   外部文件引用 `{{path:filename}}`、组合插件 YAML 解析（ADR ⑥）
//! - `config_center`: 配置中心——基于 notify 的热重载、500ms 防抖 + 内容哈希去重、
//!   读写锁并发安全、加载失败回滚 + 审计日志
//! - `pipeline`: 管道配置承载（P7）——Agent 配置加载（config_id/level/model_tier/
//!   system_prompt/tool_ids/max_iterations）；管道配置（多循环体）由 api crate 的
//!   pipeline_loader 直接解析为引擎模型
//! - `error`: 配置系统错误类型
//!
//! ## 设计决策
//!
//! - YAML 库选用 serde_yaml 0.9（社区维护，兼容 anchor/alias/merge key）
//! - 热重载用 notify（watchfiles 的 Rust 原生，0.1 的 Python watchfiles 底层就是它）
//! - 环境变量正则与 0.1 Python 完全一致：`\$\{([^}:]+)(?::-([^}]*))?\}`
//! - 优先级链：系统环境变量 > .env 文件 > 默认值
//!
//! [来源: docs/0.2_rust_plugin_solution.md §3.3]
//! [来源: docs/tasks/task_04_config_system.md]
//! [来源: docs/working/frontend_01_alignment_plan.md §P7]

pub mod agent_loader;
pub mod config_center;
pub mod error;
pub mod loader;
pub mod pipeline;

pub use agent_loader::load_agent_into_state;
pub use config_center::{AuditEntry, ConfigCenter, ConfigChangeEvent, ConfigEventType};
pub use error::ConfigError;
pub use loader::{CompositePluginYaml, ConfigLoader, StepConfig};
pub use pipeline::{load_agent_config, AgentConfig};

//! # Lingxi Invoker — 插件调用器
//!
//! 按 host_type 透明分发调用，两种模式功能对等（差异只在执行目标）：
//! - InProcess: 经 NativePluginLoader 加载 cdylib，走 C-ABI
//! - Sidecar: 通过 MCP 客户端走 JSON-RPC（进程隔离）
//!
//! 两种路径对管道引擎透明——统一返回 `PluginResult`。
//! （原 Wasm 轨已按两轨终局决策关闭摘除。）
//!
//! ## 模块组织
//!
//! - `invoker`: PluginInvoker 实现——结构体、分发入口、sidecar 调用、空闲 GC
//! - `shared`: 两种 host_type 共用的逻辑（config 注入、PluginInput 构造）
//!
//! [来源: docs/0.2_rust_plugin_solution.md §3.2]
//! [来源: docs/tasks/task_05_plugin_system.md AC-04-5/AC-04-6]

pub mod capability;
pub mod invoker;
pub mod shared;
pub mod verify;

pub use invoker::PluginInvokerImpl;

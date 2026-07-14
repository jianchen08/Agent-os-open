//! MCP 通信层错误类型

use thiserror::Error;

#[derive(Debug, Clone, Error)]
pub enum McpError {
    #[error("connection failed: {message}")]
    ConnectionFailed { message: String },

    #[error("process spawn failed: {command}: {message}")]
    SpawnFailed { command: String, message: String },

    #[error("process crashed: {plugin_id}: {reason}")]
    ProcessCrashed { plugin_id: String, reason: String },

    #[error("MCP protocol error: {message}")]
    Protocol { message: String },

    #[error("timeout waiting for response: {timeout_secs}s")]
    Timeout { timeout_secs: u64 },

    #[error("tool call failed: {tool_name}: {message}")]
    ToolCallFailed { tool_name: String, message: String },

    #[error("transport error: {message}")]
    Transport { message: String },

    #[error("initialize handshake failed: {message}")]
    InitFailed { message: String },
}

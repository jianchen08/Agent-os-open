//! 配置系统错误类型

use thiserror::Error;

/// 配置系统统一错误类型
#[derive(Debug, Clone, Error)]
pub enum ConfigError {
    /// 文件不存在
    #[error("config file not found: {path}")]
    NotFound { path: String },

    /// YAML 解析错误
    #[error("YAML parse error in {path}: {message}")]
    YamlParse { path: String, message: String },

    /// 环境变量不存在且无默认值
    #[error("environment variable not found: {var_name}")]
    EnvVarNotFound { var_name: String },

    /// 外部文件引用解析失败
    #[error("path reference resolution failed: {ref_path} (in {source_path})")]
    PathRefFailed {
        ref_path: String,
        source_path: String,
    },

    /// IO 错误
    #[error("IO error: {message}")]
    Io { message: String },

    /// 组合插件配置解析错误
    #[error("composite plugin config error: {message}")]
    Composite { message: String },
}

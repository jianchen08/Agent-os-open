//! 插件加载器错误类型

use thiserror::Error;

#[derive(Debug, Clone, Error)]
pub enum LoaderError {
    #[error("manifest not found: {path}")]
    ManifestNotFound { path: String },

    #[error("manifest parse error in {path}: {message}")]
    ManifestParse { path: String, message: String },

    #[error("manifest validation failed for '{plugin_id}': {reason}")]
    ManifestValidation { plugin_id: String, reason: String },

    #[error("plugin not found: {plugin_id}")]
    PluginNotFound { plugin_id: String },

    #[error("plugin already loaded: {plugin_id}")]
    AlreadyLoaded { plugin_id: String },

    #[error("plugin load failed: {plugin_id}: {reason}")]
    LoadFailed { plugin_id: String, reason: String },

    #[error("capability not found: {name}")]
    CapabilityNotFound { name: String },

    #[error("dependency error: {message}")]
    Dependency { message: String },

    #[error("IO error: {message}")]
    Io { message: String },
}

impl From<agentos_core::types::PluginError> for LoaderError {
    fn from(e: agentos_core::types::PluginError) -> Self {
        LoaderError::LoadFailed {
            plugin_id: String::new(),
            reason: e.message,
        }
    }
}

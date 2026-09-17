//! 插件加载器错误类型
// @feature: FP-0.2.一 插件协议 | @ci: rust-test

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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn display_renders_every_variant_context() {
        let cases: Vec<(LoaderError, String)> = vec![
            (
                LoaderError::ManifestNotFound {
                    path: "a/plugin.json".into(),
                },
                "manifest not found: a/plugin.json".into(),
            ),
            (
                LoaderError::ManifestParse {
                    path: "a/plugin.json".into(),
                    message: "bad json".into(),
                },
                "manifest parse error in a/plugin.json: bad json".into(),
            ),
            (
                LoaderError::ManifestValidation {
                    plugin_id: "p1".into(),
                    reason: "missing name".into(),
                },
                "manifest validation failed for 'p1': missing name".into(),
            ),
            (
                LoaderError::PluginNotFound {
                    plugin_id: "p1".into(),
                },
                "plugin not found: p1".into(),
            ),
            (
                LoaderError::AlreadyLoaded {
                    plugin_id: "p1".into(),
                },
                "plugin already loaded: p1".into(),
            ),
            (
                LoaderError::LoadFailed {
                    plugin_id: "p1".into(),
                    reason: "boom".into(),
                },
                "plugin load failed: p1: boom".into(),
            ),
            (
                LoaderError::CapabilityNotFound { name: "llm".into() },
                "capability not found: llm".into(),
            ),
            (
                LoaderError::Io {
                    message: "disk full".into(),
                },
                "IO error: disk full".into(),
            ),
        ];
        for (err, expected) in cases {
            assert_eq!(err.to_string(), expected);
        }
    }

    #[test]
    fn from_core_plugin_error_maps_to_load_failed_with_reason() {
        let core_err = agentos_core::types::PluginError {
            message: "spawn died".into(),
            code: Some("E1".into()),
            source: None,
        };
        let err = LoaderError::from(core_err);
        match err {
            LoaderError::LoadFailed { plugin_id, reason } => {
                assert!(plugin_id.is_empty(), "跨插件转换无归属，plugin_id 留空");
                assert_eq!(reason, "spawn died");
            }
            other => panic!("期望 LoadFailed，得到 {other:?}"),
        }
    }
}

//! 插件启用层（L1 Enabled）+ 激活策略（L2 activation）。
//!
//! 安装触发模型（docs/working/重要设计/插件安装与触发模型设计.md）：
//! - L0 Installed：磁盘有包 + discover 扫到（loader 负责）
//! - L1 Enabled：本模块控制，disabled 的插件不进注册表出口
//! - L2 activation：eager/lazy/manual（loader 据此决定 load 时机）
//! - L3 Invoked：运行时调用（引擎/工具/HTTP/钩子）
//!
//! 数据源：`config/plugins/default_profile.yaml`。
//! 优先级：manifest 显式声明 > profile > defaults。

use std::collections::HashMap;
use std::path::Path;

use agentos_core::traits::ActivationPolicy;
use serde::Deserialize;

/// default_profile.yaml 的一个插件条目。
#[derive(Debug, Clone, Deserialize, Default)]
pub struct ProfileEntry {
    #[serde(default)]
    pub enabled: Option<bool>,
    #[serde(default)]
    pub activation: Option<ActivationPolicy>,
}

/// default_profile.yaml 的 defaults 段。
#[derive(Debug, Clone, Deserialize, Default)]
pub struct ProfileDefaults {
    #[serde(default)]
    pub enabled: Option<bool>,
    #[serde(default)]
    pub activation: Option<ActivationPolicy>,
}

/// default_profile.yaml 完整结构。
#[derive(Debug, Clone, Deserialize, Default)]
pub struct PluginProfile {
    #[serde(default)]
    pub version: u32,
    #[serde(default)]
    pub plugins: HashMap<String, ProfileEntry>,
    #[serde(default)]
    pub defaults: ProfileDefaults,
}

/// 插件启用状态查询器（L1 Enabled 层）。
///
/// 合并优先级：manifest > profile.plugins[id] > profile.defaults > 内置默认(enabled=true, lazy)。
/// 在内核启动期读 default_profile.yaml 构造，注册循环 + schema 聚合用它过滤。
#[derive(Debug, Clone, Default)]
pub struct PluginEnablement {
    profile: PluginProfile,
    /// profile 文件解析失败标记（K6）：损坏 = 启停配置不可信 → is_enabled
    /// 一律 false（保守全禁），由启动日志/报告暴露，运维修复后重启恢复。
    corrupted: bool,
}

impl PluginEnablement {
    /// 测试用：直接传入 profile 构造。
    pub fn with_profile(profile: PluginProfile) -> Self {
        Self {
            profile,
            corrupted: false,
        }
    }

    /// profile 是否因解析失败而进入保守全禁态（启动报告用）。
    pub fn is_corrupted(&self) -> bool {
        self.corrupted
    }

    /// 从 config_root 下的 `plugins/default_profile.yaml` 加载（缺失与损坏分化，K6）：
    /// - 文件缺失 → 空 profile（全部走默认：enabled=true, lazy。文档化引导默认，保留）；
    /// - 解析失败 → 空 profile + corrupted 标记：显式禁用过的插件不得借"回退默认
    ///   启用"静默复活——保守全禁（is_enabled 恒 false），warn + 启动报告可见。
    pub fn load(config_root: &Path) -> Self {
        let path = config_root.join("plugins").join("default_profile.yaml");
        match std::fs::read_to_string(&path) {
            Ok(raw) => match serde_yaml::from_str::<PluginProfile>(&raw) {
                Ok(p) => {
                    tracing::info!(
                        target: "plugin-enablement",
                        count = p.plugins.len(),
                        "default_profile.yaml loaded ({} plugins)",
                        p.plugins.len()
                    );
                    Self {
                        profile: p,
                        corrupted: false,
                    }
                }
                Err(e) => {
                    tracing::warn!(
                        target: "plugin-enablement",
                        "failed to parse {}: {}, fail-closed: all plugins disabled until profile is fixed",
                        path.display(),
                        e
                    );
                    Self {
                        profile: PluginProfile::default(),
                        corrupted: true,
                    }
                }
            },
            Err(_) => {
                tracing::debug!(
                    target: "plugin-enablement",
                    "no default_profile.yaml at {}, all plugins default to enabled+lazy",
                    path.display()
                );
                Self::default()
            }
        }
    }

    /// 查询某插件是否启用（L1）。
    ///
    /// 合并优先级：manifest_enabled > profile.plugins[id].enabled > defaults.enabled > true。
    /// manifest 显式 false 则禁用（插件自声明不参与）；显式 true 或缺省则看 profile。
    ///
    /// 例外（K6）：profile 损坏（corrupted）时恒 false——启停配置不可信，manifest
    /// 声明也无法作为放行依据（保守全禁，防显式禁用过的插件复活）。
    pub fn is_enabled(&self, plugin_id: &str, manifest_enabled: Option<bool>) -> bool {
        if self.corrupted {
            return false;
        }
        if let Some(b) = manifest_enabled {
            return b;
        }
        let entry = self.profile.plugins.get(plugin_id);
        entry
            .and_then(|e| e.enabled)
            .or(self.profile.defaults.enabled)
            .unwrap_or(true)
    }

    /// 查询某插件的激活策略（L2）。
    ///
    /// 合并优先级：manifest_activation > profile.plugins[id].activation > defaults.activation > Lazy。
    pub fn activation(
        &self,
        plugin_id: &str,
        manifest_activation: Option<ActivationPolicy>,
    ) -> ActivationPolicy {
        if let Some(a) = manifest_activation {
            return a;
        }
        let entry = self.profile.plugins.get(plugin_id);
        entry
            .and_then(|e| e.activation)
            .or(self.profile.defaults.activation)
            .unwrap_or_default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_manifest_enabled_overrides_profile() {
        let mut en = PluginEnablement::default();
        en.profile.plugins.insert(
            "p1".into(),
            ProfileEntry {
                enabled: Some(false),
                activation: None,
            },
        );
        // manifest 显式 true 覆盖 profile 的 false
        assert!(en.is_enabled("p1", Some(true)));
        // manifest 缺省 → 走 profile 的 false
        assert!(!en.is_enabled("p1", None));
    }

    #[test]
    fn test_defaults_apply_when_unlisted() {
        let mut en = PluginEnablement::default();
        en.profile.defaults = ProfileDefaults {
            enabled: Some(false),
            activation: None,
        };
        // 未在 profile 列出 + manifest 缺省 → 走 defaults false
        assert!(!en.is_enabled("unknown_plugin", None));
    }

    #[test]
    fn test_default_enabled_when_no_profile() {
        let en = PluginEnablement::default(); // 空 profile
        assert!(en.is_enabled("anything", None)); // 默认启用
        assert_eq!(en.activation("anything", None), ActivationPolicy::Lazy);
    }

    #[test]
    fn test_activation_merge() {
        let mut en = PluginEnablement::default();
        en.profile.plugins.insert(
            "p1".into(),
            ProfileEntry {
                enabled: None,
                activation: Some(ActivationPolicy::Eager),
            },
        );
        // manifest 缺省 → profile 的 Eager
        assert_eq!(en.activation("p1", None), ActivationPolicy::Eager);
        // manifest Manual 覆盖 profile
        assert_eq!(
            en.activation("p1", Some(ActivationPolicy::Manual)),
            ActivationPolicy::Manual
        );
    }

    #[test]
    fn test_load_from_yaml_string() {
        let yaml = "
version: 1
plugins:
  eager_plugin:
    enabled: true
    activation: eager
  disabled_plugin:
    enabled: false
defaults:
  enabled: true
  activation: lazy
";
        let profile: PluginProfile = serde_yaml::from_str(yaml).unwrap();
        assert_eq!(profile.plugins.len(), 2);
        assert_eq!(
            profile.plugins["eager_plugin"].activation,
            Some(ActivationPolicy::Eager)
        );
        assert!(!profile.plugins["disabled_plugin"].enabled.unwrap());
    }

    /// K6：profile 文件解析失败 → 保守全禁（is_enabled 恒 false，manifest
    /// 显式 true 也不放行）+ is_corrupted 可查（启动报告）。
    /// 显式禁用过的插件不得借"回退默认启用"静默复活。
    #[test]
    fn test_corrupted_profile_disables_everything() {
        let dir = tempfile::tempdir().unwrap();
        let plugins_cfg = dir.path().join("plugins");
        std::fs::create_dir_all(&plugins_cfg).unwrap();
        std::fs::write(
            plugins_cfg.join("default_profile.yaml"),
            "version: 1\nplugins: [broken\n",
        )
        .unwrap();

        let en = PluginEnablement::load(dir.path());
        assert!(en.is_corrupted(), "解析失败应标记 corrupted");
        assert!(
            !en.is_enabled("any_plugin", None),
            "保守全禁：缺省 manifest"
        );
        assert!(
            !en.is_enabled("any_plugin", Some(true)),
            "保守全禁：manifest 显式 true 也不放行（启停配置不可信）"
        );
        assert!(!en.is_enabled("any_plugin", Some(false)));
    }

    /// K6 对照：文件缺失（非损坏）→ 保留文档化默认（默认启用 + 非 corrupted）。
    #[test]
    fn test_missing_profile_keeps_default_enabled() {
        let dir = tempfile::tempdir().unwrap();
        let en = PluginEnablement::load(dir.path());
        assert!(!en.is_corrupted(), "缺失不是损坏");
        assert!(
            en.is_enabled("any_plugin", None),
            "缺失 → 默认启用（文档化默认）"
        );
    }
}

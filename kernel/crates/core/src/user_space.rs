// @feature: FP-0.2.CFG 内核 core 用户空间 | @ci: rust-test
//! 用户空间（`AGENTOS_USER_ROOT`）——用户可写资产的统一根。
//!
//! 用户可写的东西（插件/配置/数据/密钥）全部住在一个根下面，使之整体位于
//! 仓库**之外**：仓内 `config/`（git 跟踪）与 `data/` 处于工作区还原的抹除
//! 风险面内，而用户空间不受影响。
//!
//! ```text
//! <USER_ROOT>/                 # 默认 dirs::data_dir()/agentos（按 OS 不同）
//! ├── plugins/                 # 用户插件根（覆盖内置根：同 id 用户赢）
//! ├── config/                  # 用户配置层（镜像 factory config/ 相对路径）
//! ├── data/                    # 运行时数据（多租户树 / uploads / DB）
//! └── .env                     # 密钥与环境变量
//! ```
//!
//! 覆盖语义（见 ADR 2026-09-13-unified-user-root）：**文件级整体替换**——用户层
//! 存在某文件时，factory 同路径文件不被读取、不被合并，任一时刻一个路径只有
//! 一份生效文件。这是文件级所有权转移（与插件双根「同 id 用户赢」同构），
//! **不是**被 ADR 2026-09-02 否决的「出厂默认 + 用户覆盖」字段级两层。
//!
//! 本模块只做**解析**（根在哪、某文件该读哪个根），不做读写、不做迁移——
//! 调用方按各自已有语义使用解析结果（读文件/写文件/校验路径）。

use std::path::{Path, PathBuf};

/// 用户空间根环境变量名。
pub const USER_ROOT_ENV: &str = "AGENTOS_USER_ROOT";
/// 用户配置层根环境变量名（分区覆盖）。
pub const USER_CONFIG_DIR_ENV: &str = "AGENTOS_USER_CONFIG_DIR";
/// 用户数据根环境变量名（分区覆盖）。
pub const USER_DATA_DIR_ENV: &str = "AGENTOS_DATA_DIR";
/// 用户插件根环境变量名（分区覆盖）。
pub const USER_PLUGINS_DIR_ENV: &str = "AGENTOS_USER_PLUGINS_DIR";

/// 读一个环境变量，空白值视为未设。
fn env_path(name: &str) -> Option<PathBuf> {
    std::env::var(name)
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
}

/// 用户空间根：`AGENTOS_USER_ROOT` > OS 标准目录（`dirs::data_dir()/agentos`）。
///
/// 用 `data_dir()` 而非 `data_local_dir()`：`%LOCALAPPDATA%` 不随用户漫游，
/// 而配置与密钥应当跟用户走。
pub fn user_root() -> Option<PathBuf> {
    env_path(USER_ROOT_ENV).or_else(|| dirs::data_dir().map(|d| d.join("agentos")))
}

/// 用户配置层根：`AGENTOS_USER_CONFIG_DIR` > `<USER_ROOT>/config`。
pub fn user_config_dir() -> Option<PathBuf> {
    env_path(USER_CONFIG_DIR_ENV).or_else(|| user_root().map(|r| r.join("config")))
}

/// 用户数据根：`AGENTOS_DATA_DIR` > `<USER_ROOT>/data`。
pub fn user_data_dir() -> Option<PathBuf> {
    env_path(USER_DATA_DIR_ENV).or_else(|| user_root().map(|r| r.join("data")))
}

/// 用户插件根：`AGENTOS_USER_PLUGINS_DIR` > `<USER_ROOT>/plugins`。
pub fn user_plugins_dir() -> Option<PathBuf> {
    env_path(USER_PLUGINS_DIR_ENV).or_else(|| user_root().map(|r| r.join("plugins")))
}

/// 解析一个配置相对路径应读/写的落点。
///
/// `rel` 是相对 factory config 根的路径（如 `models/llm.yaml`；允许带
/// `config/` 前缀，会被剥掉）。返回用户层路径**当且仅当该文件已存在**——
/// 不存在则回落到 factory 路径（`factory_root/rel`）。
///
/// 语义要点（ADR 2026-09-13）：
/// - **整体替换**：命中用户层时 factory 文件完全不参与——调用方不得再读它；
/// - **存在性判定用文件而非目录**：用户层目录存在但该文件不存在 = 该键未被接管；
/// - **单一解析器**：读与写必须走同一个本函数，禁止两侧各自拼路径（那正是
///   单真值 ADR 的实证根因：用户值写盘了、插件读的却是另一份）。
///
/// 返回 `None` 仅当用户层不可用（无 OS data dir）且 factory 路径也拼不出来；
/// 生产环境下 `factory_root` 必为绝对路径，故实际总能返回 `Some`。
pub fn resolve_config_path(factory_root: &Path, rel: &str) -> Option<PathBuf> {
    let rel_norm = rel.replace('\\', "/");
    let rel_trimmed = rel_norm.strip_prefix("config/").unwrap_or(&rel_norm);
    let candidate = user_config_dir().map(|u| u.join(rel_trimmed));
    if let Some(ref user_path) = candidate {
        if user_path.is_file() {
            return Some(user_path.clone());
        }
    }
    Some(factory_root.join(rel_trimmed))
}

/// 同上，但返回「落点根」信息：`(路径, 是否用户层)`。
///
/// 写路径需要区分两种情形——用户层文件已存在（直接写）、尚未接管（先播种
/// factory 内容再写）。`is_user` 为真表示该路径在用户空间内（无论文件是否
/// 已存在）；为假表示用户层不可用，只能回落 factory。
pub fn config_write_target(factory_root: &Path, rel: &str) -> (PathBuf, bool) {
    let rel_norm = rel.replace('\\', "/");
    let rel_trimmed = rel_norm.strip_prefix("config/").unwrap_or(&rel_norm);
    if let Some(user_root_dir) = user_config_dir() {
        return (user_root_dir.join(rel_trimmed), true);
    }
    (factory_root.join(rel_trimmed), false)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 环境变量是进程全局态：测试串行并配对清场，防同二进制并行用例互相污染。
    struct EnvGuard {
        key: &'static str,
        original: Option<String>,
    }

    impl EnvGuard {
        fn set(key: &'static str, value: &str) -> Self {
            let original = std::env::var(key).ok();
            std::env::set_var(key, value);
            Self { key, original }
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            match &self.original {
                Some(v) => std::env::set_var(self.key, v),
                None => std::env::remove_var(self.key),
            }
        }
    }

    /// 把四个分区环境变量全部钉到临时目录，返回隔离的 (user_root, factory_root)。
    fn isolate(tmp: &Path) -> (PathBuf, PathBuf) {
        let user = tmp.join("user-root");
        let factory = tmp.join("factory-config");
        std::fs::create_dir_all(&user).unwrap();
        std::fs::create_dir_all(&factory).unwrap();
        std::env::set_var(USER_ROOT_ENV, &user);
        std::env::remove_var(USER_CONFIG_DIR_ENV);
        std::env::remove_var(USER_DATA_DIR_ENV);
        std::env::remove_var(USER_PLUGINS_DIR_ENV);
        (user, factory)
    }

    #[test]
    fn sub_roots_default_under_user_root() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (user, _f) = isolate(tmp.path());

        assert_eq!(user_plugins_dir().unwrap(), user.join("plugins"));
        assert_eq!(user_config_dir().unwrap(), user.join("config"));
        assert_eq!(user_data_dir().unwrap(), user.join("data"));
        assert_eq!(user_root().unwrap(), user);
    }

    #[test]
    fn partition_env_overrides_user_root() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (user, _f) = isolate(tmp.path());
        let _g = EnvGuard::set(
            USER_CONFIG_DIR_ENV,
            tmp.path().join("elsewhere").to_str().unwrap(),
        );

        // 分区覆盖：config 落到别处，其余仍随用户根
        assert_eq!(user_config_dir().unwrap(), tmp.path().join("elsewhere"));
        assert_eq!(user_plugins_dir().unwrap(), user.join("plugins"));
    }

    #[test]
    fn blank_env_is_treated_as_unset() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (user, _f) = isolate(tmp.path());
        let _g = EnvGuard::set(USER_CONFIG_DIR_ENV, "   ");

        assert_eq!(
            user_config_dir().unwrap(),
            user.join("config"),
            "空白值应视为未设（回退用户根推导）"
        );
    }

    #[test]
    fn resolve_prefers_user_file_when_present() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (_user, factory) = isolate(tmp.path());
        // factory 侧有该文件，用户侧也有 → 用户赢（整体替换）
        std::fs::create_dir_all(factory.join("models")).unwrap();
        std::fs::write(factory.join("models/llm.yaml"), "factory: true").unwrap();
        let user_cfg = user_config_dir().unwrap();
        std::fs::create_dir_all(user_cfg.join("models")).unwrap();
        std::fs::write(user_cfg.join("models/llm.yaml"), "user: true").unwrap();

        let resolved = resolve_config_path(&factory, "models/llm.yaml").unwrap();
        assert_eq!(resolved, user_cfg.join("models/llm.yaml"));
        assert_eq!(std::fs::read_to_string(&resolved).unwrap(), "user: true");
    }

    #[test]
    fn resolve_falls_back_to_factory_when_user_absent() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (_user, factory) = isolate(tmp.path());
        std::fs::create_dir_all(factory.join("models")).unwrap();
        std::fs::write(factory.join("models/llm.yaml"), "factory: true").unwrap();

        let resolved = resolve_config_path(&factory, "models/llm.yaml").unwrap();
        assert_eq!(resolved, factory.join("models/llm.yaml"));
    }

    #[test]
    fn resolve_accepts_config_prefixed_rel() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (_user, factory) = isolate(tmp.path());
        std::fs::create_dir_all(factory.join("models")).unwrap();
        std::fs::write(factory.join("models/llm.yaml"), "x: 1").unwrap();

        // manifest 的 config_files[].path 可能带 config/ 前缀——两种写法必须同解
        let bare = resolve_config_path(&factory, "models/llm.yaml").unwrap();
        let prefixed = resolve_config_path(&factory, "config/models/llm.yaml").unwrap();
        assert_eq!(bare, prefixed, "带 config/ 前缀应解析到同一落点");
    }

    #[test]
    fn user_dir_present_but_file_absent_is_not_takeover() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (_user, factory) = isolate(tmp.path());
        // 用户层目录存在（别的文件已接管），但本文件没接管 → 仍读 factory
        std::fs::create_dir_all(user_config_dir().unwrap().join("models")).unwrap();
        std::fs::write(user_config_dir().unwrap().join("models/other.yaml"), "u").unwrap();
        std::fs::create_dir_all(factory.join("models")).unwrap();
        std::fs::write(factory.join("models/llm.yaml"), "factory").unwrap();

        assert_eq!(
            resolve_config_path(&factory, "models/llm.yaml").unwrap(),
            factory.join("models/llm.yaml"),
            "接管判定按文件存在性，不按目录存在性"
        );
    }

    #[test]
    fn write_target_always_points_into_user_space() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let (_user, factory) = isolate(tmp.path());
        let user_cfg = user_config_dir().unwrap();

        // 已接管：直接写用户层文件
        std::fs::create_dir_all(user_cfg.join("models")).unwrap();
        std::fs::write(user_cfg.join("models/llm.yaml"), "u").unwrap();
        let (p, is_user) = config_write_target(&factory, "models/llm.yaml");
        assert!(is_user);
        assert_eq!(p, user_cfg.join("models/llm.yaml"));

        // 未接管：写目标**仍在用户空间**（调用方先播种再写，不覆写 factory）
        let (p2, is_user2) = config_write_target(&factory, "agents/main/agentos.yaml");
        assert!(is_user2);
        assert_eq!(p2, user_cfg.join("agents/main/agentos.yaml"));
        assert!(
            !p2.starts_with(&factory),
            "写目标绝不得落在 factory（否则用户改动进仓内，正是本机制要消灭的）"
        );
    }

    /// 串行锁：env 是进程态，本模块测试互斥执行。
    fn tests_guard() -> std::sync::MutexGuard<'static, ()> {
        static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
        LOCK.lock().unwrap_or_else(|e| e.into_inner())
    }
}

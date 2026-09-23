//! 存储驱动工厂（§9.6 StorageBackend driver 化）。
//!
//! 把存储从"写死 SqliteStore 文件库"改为**按配置选 driver**：
//! 换存储方式 = 改 `config/kernel/storage.yaml` 一行（或环境变量）+ 重启，
//! 上层零改动（runs/messages/traces/blobs/memory/users 全走
//! [`StorageBackend`] trait）。
//!
//! ## 为什么是内核内 driver 接口而不是插件轨（§9.6 已定）
//!
//! 存储是状态账本（审计/调度敏感件）+ 自举必需件（插件加载之前就要用）。
//! 交给 sidecar 插件的后果：账本可被不受结构性关押的进程持有（审计面瓦解），
//! 且加载器自身又要存储（鸡生蛋死循环）。driver 编译进内核：可信、无 IPC、
//! 自举无问题；"换"的体验与换插件一致（改配置即换）。
//!
//! ## driver 清单
//!
//! - `sqlite`（默认）：文件库，db-admin 表驱动接口（with_conn 任意表 SQL）可用；
//! - `memory`：内存 SQLite（测试/临时实例），db-admin 同样可用；
//! - `postgres` 等：**留桩**——返回显式错误。真实引入需加依赖（§八.1
//!   "不轻易引入大依赖"基线），等出现真实需求再落地，接口已预留。
//!
//! ## 配置来源（优先级从高到低）
//!
//! 1. 环境变量 `AGENTOS_STORAGE_DRIVER`（driver 名）+ `AGENTOS_DB_PATH`
//!    （sqlite 路径，`:memory:` 别名向后兼容）；
//! 2. `config/kernel/storage.yaml`：
//!   ```yaml
//!   storage:
//!     driver: sqlite        # sqlite | memory
//!     sqlite:
//!       path: agentos_kernel.db  # 相对项目根；":memory:" = 内存库
//!   ```
//! 3. 默认：sqlite + **用户空间数据根** `<USER_ROOT>/data/agentos_kernel.db`
//!    （ADR 2026-09-13-unified-user-root；用户空间不可得时回落项目根）。
//!
//! 文件不存在 = 未配置（走默认）；文件存在但损坏（读失败/YAML 解析失败）=
//! [`resolve_storage_config`] 返回 `Err` 拒绝启动——数据正确性优先，
//! 不静默落到默认库。
//!

use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::store::SqliteStore;
use agentos_core::traits::StorageBackend;
use agentos_core::types::StorageError;

/// 存储驱动配置（`config/kernel/storage.yaml` + 环境变量归一后的结果）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StorageConfig {
    /// driver 名：`sqlite` | `memory`（未来 `postgres`）。
    pub driver: String,
    /// sqlite driver 的库路径（相对项目根或绝对；`:memory:` = 内存）。
    /// 其它 driver 忽略此字段。
    pub sqlite_path: String,
}

/// 环境变量：driver 名覆盖（最高优先级）。
pub const ENV_STORAGE_DRIVER: &str = "AGENTOS_STORAGE_DRIVER";
/// 环境变量：sqlite 路径覆盖（向后兼容既有用法，含 `:memory:`）。
pub const ENV_DB_PATH: &str = "AGENTOS_DB_PATH";

/// config 文件名（config_root 下）。
const STORAGE_CONFIG_FILE: &str = "kernel/storage.yaml";

/// 默认库文件名。位置由 [`resolve_storage_config`] 决定（用户空间数据根优先）。
pub const DB_FILENAME: &str = "agentos_kernel.db";

/// 解析 storage.yaml 的 storage 节。
///
/// serde 宽松：未知字段忽略（前向兼容）。文件存在与否由调用方在读文件时区分。
#[derive(serde::Deserialize, Default)]
struct StorageFile {
    #[serde(default)]
    storage: Option<StorageSection>,
}

#[derive(serde::Deserialize, Default)]
struct StorageSection {
    #[serde(default)]
    driver: Option<String>,
    #[serde(default)]
    sqlite: Option<SqliteSection>,
}

#[derive(serde::Deserialize, Default)]
struct SqliteSection {
    #[serde(default)]
    path: Option<String>,
}

/// 归一存储配置：环境变量 > config/kernel/storage.yaml > 默认。
///
/// `project_root` 用于相对 path 的基准与默认路径推导（config_root 的父目录）。
/// 相对 sqlite path（env 或 yaml）统一**锚定项目根**解析并绝对化——内核与
/// Python 读侧（plugins/shared/kernel_db.py）同规则，两端 CWD 不同也读写同库；
/// 绝对路径与 `:memory:` 原样保留。
///
/// 文件不存在 = 未配置（走默认 sqlite）；文件存在但读取/YAML 解析失败 = `Err`
/// （数据正确性优先：坏配置可能让读写落到错误 driver/路径，拒绝启动而非静默默认）。
pub fn resolve_storage_config(config_root: &Path) -> Result<StorageConfig, StorageError> {
    // config_root 相对时先按进程 CWD 绝对化——project_root 必须是绝对基准，
    // 相对 db path 锚定后才与 CWD 无关。
    let config_root_abs = if config_root.is_absolute() {
        config_root.to_path_buf()
    } else {
        std::env::current_dir()
            .map(|cwd| cwd.join(config_root))
            .unwrap_or_else(|_| config_root.to_path_buf())
    };
    let project_root: PathBuf = config_root_abs
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."));

    // ① config 文件：NotFound = 未配置；其余读失败/解析失败 = Err
    let config_path = config_root.join(STORAGE_CONFIG_FILE);
    let file: StorageFile = match std::fs::read_to_string(&config_path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => StorageFile::default(),
        Err(e) => {
            return Err(StorageError::Io(format!(
                "读取 {} 失败: {e}",
                config_path.display()
            )))
        }
        Ok(raw) => serde_yaml::from_str(&raw).map_err(|e| {
            StorageError::Io(format!("{} YAML 解析失败: {e}", config_path.display()))
        })?,
    };
    let section = file.storage.unwrap_or_default();
    let sqlite_section = section.sqlite.unwrap_or_default();

    // ② 环境变量覆盖（向后兼容 AGENTOS_DB_PATH=:memory: 的旧用法）
    let env_driver = std::env::var(ENV_STORAGE_DRIVER)
        .ok()
        .filter(|s| !s.is_empty());
    let env_path = std::env::var(ENV_DB_PATH).ok().filter(|s| !s.is_empty());

    let driver = env_driver
        .or(section.driver.filter(|s| !s.is_empty()))
        .unwrap_or_else(|| "sqlite".to_string());

    let sqlite_path = env_path
        .or(sqlite_section.path.filter(|s| !s.is_empty()))
        .map(|p| anchor_db_path(&p, &project_root))
        .unwrap_or_else(|| {
            // 默认库文件位置＝用户空间数据根（ADR 2026-09-13-unified-user-root）：
            // 库存的是用户资产（会话/任务/轨迹），落仓内会处于工作区还原的抹除
            // 风险面内。用户空间不可得时回落项目根（保持旧行为，不在极端环境下
            // 把库开到一个取不到的地方）。
            match agentos_core::user_space::user_data_dir() {
                Some(data_dir) => data_dir.join(DB_FILENAME).to_string_lossy().to_string(),
                None => project_root.join(DB_FILENAME).to_string_lossy().to_string(),
            }
        });

    Ok(StorageConfig {
        driver,
        sqlite_path,
    })
}

/// 相对 db path 锚定项目根解析（与 plugins/shared/kernel_db.py 同规则）：
/// 绝对路径与 `:memory:` 别名原样保留；相对路径 join 项目根后绝对化。
/// 双端（内核开库 / 插件读侧）同规则 ⇒ 两端 CWD 不同也指向同一物理库。
fn anchor_db_path(path: &str, project_root: &Path) -> String {
    let candidate = Path::new(path);
    if path == ":memory:" || candidate.is_absolute() {
        return path.to_string();
    }
    let anchored = project_root.join(candidate);
    if anchored.is_absolute() {
        anchored.to_string_lossy().to_string()
    } else {
        // project_root 本身相对（config_root 相对且 CWD 不可得）：按 CWD 兜底绝对化
        match std::env::current_dir() {
            Ok(cwd) => cwd.join(anchored).to_string_lossy().to_string(),
            Err(_) => anchored.to_string_lossy().to_string(),
        }
    }
}

/// 打开存储。
///
/// 返回 [`StorageHandles`]（业务账本 trait 句柄 + SQLite 专有 db-admin 句柄）：
/// - sqlite/memory driver：两个句柄都可用（SqliteStore 本体）；
/// - 未来非 SQLite driver：db 句柄为 None——db-admin capability 与
///   G8 排空的 SQLite 专有路径诚实降级（"统一数据接口未启用"），
///   业务账本（trait 面）完全可用。
///
/// 工厂产物：业务账本句柄（任何 driver 都有）+ SQLite 专有句柄（仅 sqlite/memory）。
pub type StorageHandles = (Arc<dyn StorageBackend>, Option<Arc<SqliteStore>>);

/// 打开存储（按 [`StorageConfig::driver`] 分派）。
pub fn open_storage(cfg: &StorageConfig) -> Result<StorageHandles, StorageError> {
    match cfg.driver.as_str() {
        "sqlite" => {
            let store = if cfg.sqlite_path == ":memory:" {
                SqliteStore::open_memory()?
            } else {
                SqliteStore::open(&cfg.sqlite_path)?
            };
            let store = Arc::new(store);
            Ok((store.clone(), Some(store)))
        }
        "memory" => {
            let store = Arc::new(SqliteStore::open_memory()?);
            Ok((store.clone(), Some(store)))
        }
        other => Err(StorageError::Io(format!(
            "unknown storage driver '{other}' (known: sqlite, memory; postgres 留桩待真实需求)"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 环境变量互斥锁：同二进制并行测试线程间 set/remove 与读取的竞态防护。
    /// 所有读 `AGENTOS_DB_PATH` 的测试（直接或经 resolve_storage_config）持锁。
    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    /// 默认配置：无文件无环境变量 → sqlite + `agentos_kernel.db`。
    ///
    /// 位置断言依赖用户空间解析（ADR 2026-09-13-unified-user-root），故把
    /// `AGENTOS_USER_ROOT` 钉到临时目录——否则会读宿主机真实用户目录，
    /// 断言既不确定、又可能被真实环境干扰。
    #[test]
    fn resolve_defaults_to_sqlite_file() {
        // 环境变量在测试进程可能被其它用例设置——此处只断言 driver 默认逻辑
        // 在无 env 时的行为（CI 单测进程通常干净；本地有 env 时跳过断言）。
        let _guard = ENV_LOCK.lock().unwrap();
        if std::env::var(ENV_STORAGE_DRIVER).is_ok() || std::env::var(ENV_DB_PATH).is_ok() {
            return;
        }
        let dir = tempfile::tempdir().unwrap();
        let user_root = tempfile::tempdir().unwrap();
        let original = std::env::var(agentos_core::user_space::USER_ROOT_ENV).ok();
        std::env::set_var(agentos_core::user_space::USER_ROOT_ENV, user_root.path());

        let cfg = resolve_storage_config(dir.path()).expect("无文件应走默认");

        match original {
            Some(v) => std::env::set_var(agentos_core::user_space::USER_ROOT_ENV, v),
            None => std::env::remove_var(agentos_core::user_space::USER_ROOT_ENV),
        }

        assert_eq!(cfg.driver, "sqlite");
        assert!(cfg
            .sqlite_path
            .replace('\\', "/")
            .ends_with("agentos_kernel.db"));
        // 默认库落在用户空间数据根（出仓），不再是项目根
        let expected = user_root.path().join("data").join(DB_FILENAME);
        assert_eq!(
            Path::new(&cfg.sqlite_path),
            expected,
            "默认库文件应落用户空间数据根"
        );
    }

    /// 文件不存在（空 config 目录）→ Ok 且默认 sqlite。
    #[test]
    fn resolve_missing_file_is_ok_with_defaults() {
        let _guard = ENV_LOCK.lock().unwrap();
        if std::env::var(ENV_STORAGE_DRIVER).is_ok() || std::env::var(ENV_DB_PATH).is_ok() {
            return;
        }
        let dir = tempfile::tempdir().unwrap();
        assert!(!dir.path().join(STORAGE_CONFIG_FILE).exists());
        let cfg = resolve_storage_config(dir.path()).expect("缺文件 = 未配置，走默认");
        assert_eq!(cfg.driver, "sqlite");
    }

    /// 损坏 YAML（存在但解析失败）→ Err 拒绝启动（不静默走默认）。
    #[test]
    fn resolve_corrupted_yaml_is_error() {
        let dir = tempfile::tempdir().unwrap();
        let cfg_path = dir.path().join(STORAGE_CONFIG_FILE);
        std::fs::create_dir_all(cfg_path.parent().unwrap()).unwrap();
        std::fs::write(cfg_path, "!!!not yaml{{").unwrap();
        let err = match resolve_storage_config(dir.path()) {
            Ok(cfg) => panic!("损坏 YAML 应返回 Err，got: {cfg:?}"),
            Err(e) => e,
        };
        let msg = format!("{err}");
        assert!(msg.contains("解析失败"), "err: {msg}");
        assert!(msg.contains("storage.yaml"), "错误应点名文件：{msg}");
    }

    /// yaml 解析：storage.driver/sqlite.path 节生效。
    #[test]
    fn resolve_reads_yaml_sections() {
        let raw = "
storage:
  driver: memory
  sqlite:
    path: /tmp/x.db
";
        let f: StorageFile = serde_yaml::from_str(raw).unwrap();
        let s = f.storage.unwrap();
        assert_eq!(s.driver.as_deref(), Some("memory"));
        assert_eq!(s.sqlite.unwrap().path.as_deref(), Some("/tmp/x.db"));
    }

    /// 坏 yaml：serde_yaml 层即报错（resolve_storage_config 传播为 Err）。
    #[test]
    fn resolve_tolerates_broken_yaml() {
        let f: Result<StorageFile, _> = serde_yaml::from_str("!!!not yaml{{");
        assert!(f.is_err()); // resolve 侧：文件存在 + 解析失败 → Err 拒绝启动
    }

    /// open_storage：memory driver 双句柄可用且功能等价。
    #[tokio::test]
    async fn open_storage_memory_yields_both_handles() {
        let cfg = StorageConfig {
            driver: "memory".to_string(),
            sqlite_path: String::new(),
        };
        let (backend, db) = open_storage(&cfg).unwrap();
        assert!(db.is_some(), "memory driver 的 db-admin 句柄应可用");
        // trait 面可用性：运行开始簿记（state 运行键）+ 读回投影。
        backend
            .record_run_start("pipe_r1", "default", "r1", "hash")
            .await
            .unwrap();
        let run = backend.get_run("r1").await.unwrap();
        assert_eq!(run.run_id, "r1");
    }

    /// open_storage：sqlite :memory: 别名等价 memory。
    #[tokio::test]
    async fn open_storage_sqlite_memory_alias() {
        let cfg = StorageConfig {
            driver: "sqlite".to_string(),
            sqlite_path: ":memory:".to_string(),
        };
        let (backend, db) = open_storage(&cfg).unwrap();
        assert!(db.is_some());
        backend
            .record_run_start("pipe_r1", "default", "r1", "h")
            .await
            .unwrap();
        assert!(backend.get_run("r1").await.is_ok());
    }

    /// open_storage：未知 driver 显式报错（postgres 留桩）。
    #[test]
    fn open_storage_unknown_driver_errors() {
        let cfg = StorageConfig {
            driver: "postgres".to_string(),
            sqlite_path: String::new(),
        };
        let err = match open_storage(&cfg) {
            Ok(_) => panic!("unknown driver 应报错"),
            Err(e) => e,
        };
        let msg = format!("{}", err);
        assert!(msg.contains("unknown storage driver"), "got: {msg}");
        assert!(msg.contains("postgres"), "留桩 driver 应点名: {msg}");
    }

    /// env 相对路径 → 锚定项目根解析并绝对化（与 Python 读侧 kernel_db.py
    /// 同规则：两端 CWD 不同也指向同一物理库）。
    #[test]
    fn env_relative_db_path_anchors_to_project_root() {
        let _guard = ENV_LOCK.lock().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let config_root = dir.path().join("config");
        std::fs::create_dir_all(&config_root).unwrap();
        std::env::set_var(ENV_DB_PATH, "data/sub/rel.db");
        let cfg = resolve_storage_config(&config_root).expect("resolve ok");
        std::env::remove_var(ENV_DB_PATH);
        assert_eq!(
            Path::new(&cfg.sqlite_path),
            dir.path().join("data/sub/rel.db")
        );
        assert!(Path::new(&cfg.sqlite_path).is_absolute(), "解析后绝对化");
    }

    /// yaml 相对路径 → 同样锚定项目根（env 未设置时）。
    #[test]
    fn yaml_relative_db_path_anchors_to_project_root() {
        let _guard = ENV_LOCK.lock().unwrap();
        std::env::remove_var(ENV_DB_PATH);
        let dir = tempfile::tempdir().unwrap();
        let config_root = dir.path().join("config");
        std::fs::create_dir_all(&config_root).unwrap();
        let cfg_path = config_root.join(STORAGE_CONFIG_FILE);
        std::fs::create_dir_all(cfg_path.parent().unwrap()).unwrap();
        std::fs::write(
            cfg_path,
            "storage:\n  driver: sqlite\n  sqlite:\n    path: custom/rel.db\n",
        )
        .unwrap();
        let cfg = resolve_storage_config(&config_root).expect("resolve ok");
        assert_eq!(cfg.driver, "sqlite");
        assert_eq!(
            Path::new(&cfg.sqlite_path),
            dir.path().join("custom/rel.db")
        );
    }

    /// env 绝对路径原样保留（不重锚定）。
    #[test]
    fn env_absolute_db_path_passthrough() {
        let _guard = ENV_LOCK.lock().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let config_root = dir.path().join("config");
        std::fs::create_dir_all(&config_root).unwrap();
        let abs = dir.path().join("elsewhere.db");
        std::env::set_var(ENV_DB_PATH, &abs);
        let cfg = resolve_storage_config(&config_root).expect("resolve ok");
        std::env::remove_var(ENV_DB_PATH);
        assert_eq!(Path::new(&cfg.sqlite_path), abs);
    }

    /// env `:memory:` 别名原样保留（不参与锚定）。
    #[test]
    fn env_memory_alias_passthrough() {
        let _guard = ENV_LOCK.lock().unwrap();
        let dir = tempfile::tempdir().unwrap();
        std::env::set_var(ENV_DB_PATH, ":memory:");
        let cfg = resolve_storage_config(dir.path()).expect("resolve ok");
        std::env::remove_var(ENV_DB_PATH);
        assert_eq!(cfg.sqlite_path, ":memory:");
    }
}

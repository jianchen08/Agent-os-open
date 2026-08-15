//! 存储驱动工厂（§9.6 StorageBackend driver 化）。
//!
//! 把存储从"写死 SqliteStore 文件库"改为**按配置选 driver**：
//! 换存储方式 = 改 `config/storage.yaml` 一行（或环境变量）+ 重启，
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
//! 2. `config/storage.yaml`：
//!   ```yaml
//!   storage:
//!     driver: sqlite        # sqlite | memory
//!     sqlite:
//!       path: agentos_kernel.db  # 相对项目根；":memory:" = 内存库
//!   ```
//! 3. 默认：sqlite + 项目根 `agentos_kernel.db`（与 driver 化之前行为逐位一致）。
//!
//! [来源: docs/working/重要设计/插件三轨一致性与Cordis机制迁移计划.md §9.6
//!  "换存储后端 → StorageBackend driver 化（内核内 driver 接口，不是插件轨）"]

use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::store::SqliteStore;
use agentos_core::traits::StorageBackend;
use agentos_core::types::StorageError;

/// 存储驱动配置（`config/storage.yaml` + 环境变量归一后的结果）。
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
const STORAGE_CONFIG_FILE: &str = "storage.yaml";

/// 解析 storage.yaml 的 storage 节（容错：缺文件/坏结构 = None，走默认）。
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

/// 归一存储配置：环境变量 > config/storage.yaml > 默认。
///
/// `project_root` 用于相对 path 的基准与默认路径推导（config_root 的父目录，
/// 与 driver 化之前的推导逻辑一致）。
pub fn resolve_storage_config(config_root: &Path) -> StorageConfig {
    let project_root: PathBuf = config_root
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."));

    // ① config 文件（容错缺省）
    let file: StorageFile = std::fs::read_to_string(config_root.join(STORAGE_CONFIG_FILE))
        .ok()
        .and_then(|raw| serde_yaml::from_str(&raw).ok())
        .unwrap_or_default();
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
        .unwrap_or_else(|| {
            project_root
                .join("agentos_kernel.db")
                .to_string_lossy()
                .to_string()
        });

    StorageConfig {
        driver,
        sqlite_path,
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

    /// 默认配置：无文件无环境变量 → sqlite + 项目根 agentos_kernel.db。
    #[test]
    fn resolve_defaults_to_sqlite_file() {
        // 环境变量在测试进程可能被其它用例设置——此处只断言 driver 默认逻辑
        // 在无 env 时的行为（CI 单测进程通常干净；本地有 env 时跳过断言）。
        if std::env::var(ENV_STORAGE_DRIVER).is_ok() || std::env::var(ENV_DB_PATH).is_ok() {
            return;
        }
        let cfg = resolve_storage_config(Path::new("/proj/config"));
        assert_eq!(cfg.driver, "sqlite");
        assert!(cfg
            .sqlite_path
            .replace('\\', "/")
            .ends_with("agentos_kernel.db"));
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

    /// 坏 yaml 容错 → 默认（不 panic）。
    #[test]
    fn resolve_tolerates_broken_yaml() {
        let f: Result<StorageFile, _> = serde_yaml::from_str("!!!not yaml{{");
        assert!(f.is_err()); // 调用方 .ok() 兜底
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
        // trait 面可用性：建 run + 读回。
        backend.create_run("r1", "hash", "default").await.unwrap();
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
        backend.create_run("r1", "h", "default").await.unwrap();
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
}

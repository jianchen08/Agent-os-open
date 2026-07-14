//! SQLite 四表存储实现
//!
//! 实现 StorageBackend trait，使用 rusqlite 作为 SQLite 后端。
//! 四表模型：runs / messages / traces / blobs（ADR ③④）。
//!
//! [来源: docs/working/adr_engine_design.md §4.2]

use std::sync::Arc;

use async_trait::async_trait;
use lingxi_core::traits::StorageBackend;
use lingxi_core::types::{
    BlobRecord, Branch, Message, MessageRecord, PatchType, RunRecord, RunStatus, StorageError,
    TraceEntry,
};
use parking_lot::Mutex;
use rusqlite::Connection;
use sha2::{Digest, Sha256};
use tracing::info;

/// SQLite 四表 DDL（建表脚本）
const DDL: &str = "
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    config_hash    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'running',
    tenant_id      TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    ended_at       TEXT,
    current_branch TEXT NOT NULL,
    current_seq    INTEGER NOT NULL DEFAULT 0,
    metadata       TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    message_id      TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    branch_id       TEXT NOT NULL,
    seq_in_branch   INTEGER NOT NULL,
    role            TEXT NOT NULL,
    blob_id         TEXT,
    content_preview TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(branch_id, seq_in_branch)
);
CREATE TABLE IF NOT EXISTS traces (
    trace_id      TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    branch_id     TEXT NOT NULL,
    seq_in_branch INTEGER NOT NULL,
    plugin_id     TEXT NOT NULL,
    patch_type    TEXT NOT NULL,
    patch_data    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_branch_seq ON traces(branch_id, seq_in_branch);
CREATE TABLE IF NOT EXISTS blobs (
    blob_id    TEXT PRIMARY KEY,
    mime_type  TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    data       BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS branches (
    branch_id     TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    parent_branch TEXT,
    parent_seq    INTEGER,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (branch_id, run_id)
);
";

/// SQLite 四表存储实现。
///
/// 使用 `rusqlite::Connection`（线程安全包装在 `Arc<Mutex>` 中）。
/// 支持 WAL 模式以提高并发读写性能。
pub struct SqliteStore {
    conn: Arc<Mutex<Connection>>,
}

impl SqliteStore {
    /// 在指定路径创建 SQLite 数据库并初始化四表。
    pub fn open(path: &str) -> Result<Self, StorageError> {
        let conn = Connection::open(path).map_err(|e| StorageError::Database(e.to_string()))?;
        Self::init(&conn)?;
        Ok(Self {
            conn: Arc::new(Mutex::new(conn)),
        })
    }

    /// 创建内存数据库（用于测试）。
    pub fn open_memory() -> Result<Self, StorageError> {
        let conn =
            Connection::open_in_memory().map_err(|e| StorageError::Database(e.to_string()))?;
        Self::init(&conn)?;
        Ok(Self {
            conn: Arc::new(Mutex::new(conn)),
        })
    }

    fn init(conn: &Connection) -> Result<(), StorageError> {
        conn.execute_batch("PRAGMA journal_mode=WAL;")
            .map_err(|e| StorageError::Database(e.to_string()))?;
        conn.execute_batch(DDL)
            .map_err(|e| StorageError::Database(e.to_string()))?;
        info!("SQLite four-table store initialized");
        Ok(())
    }

    /// 计算内容 SHA256 哈希（用作 blob_id）。
    fn compute_blob_id(data: &[u8]) -> String {
        let mut hasher = Sha256::new();
        hasher.update(data);
        format!("{:x}", hasher.finalize())
    }

    // ── runs 表操作 ──────────────────────────────────────────

    /// 创建运行实例。
    pub fn create_run(
        &self,
        run_id: &str,
        config_hash: &str,
        tenant_id: &str,
    ) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        let now = chrono::Utc::now().to_rfc3339();
        conn.execute(
            "INSERT INTO runs (run_id, config_hash, status, tenant_id, created_at, current_branch, current_seq) VALUES (?1, ?2, 'running', ?3, ?4, 'main', 0)",
            rusqlite::params![run_id, config_hash, tenant_id, now],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
        // 创建主分支
        conn.execute(
            "INSERT INTO branches (branch_id, run_id, created_at) VALUES ('main', ?1, ?2)",
            rusqlite::params![run_id, now],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(())
    }

    /// 追加消息到 messages 表。
    #[allow(clippy::too_many_arguments)]
    pub fn append_message(
        &self,
        message_id: &str,
        run_id: &str,
        branch_id: &str,
        seq_in_branch: u32,
        role: &str,
        blob_id: Option<&str>,
        content_preview: Option<&str>,
    ) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        let now = chrono::Utc::now().to_rfc3339();
        conn.execute(
            "INSERT INTO messages (message_id, run_id, branch_id, seq_in_branch, role, blob_id, content_preview, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            rusqlite::params![message_id, run_id, branch_id, seq_in_branch, role, blob_id, content_preview, now],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(())
    }

    /// 存储 BLOB 数据（内容寻址去重）。
    pub fn store_blob(&self, data: &[u8], mime_type: &str) -> Result<String, StorageError> {
        let blob_id = Self::compute_blob_id(data);
        let conn = self.conn.lock();
        // 先检查是否已存在（去重）
        let exists: bool = conn
            .query_row(
                "SELECT COUNT(*) > 0 FROM blobs WHERE blob_id = ?1",
                rusqlite::params![blob_id],
                |row| row.get(0),
            )
            .unwrap_or(false);
        if !exists {
            let now = chrono::Utc::now().to_rfc3339();
            conn.execute(
                "INSERT INTO blobs (blob_id, mime_type, size_bytes, data, created_at) VALUES (?1, ?2, ?3, ?4, ?5)",
                rusqlite::params![blob_id, mime_type, data.len() as i64, data, now],
            )
            .map_err(|e| StorageError::Database(e.to_string()))?;
        }
        Ok(blob_id)
    }

    /// 获取指定分支的 traces（正向重放用）。
    pub fn get_traces(
        &self,
        branch_id: &str,
        from_seq: u32,
        to_seq: u32,
    ) -> Result<Vec<TraceEntry>, StorageError> {
        let conn = self.conn.lock();
        let mut stmt = conn
            .prepare(
                "SELECT trace_id, run_id, branch_id, seq_in_branch, plugin_id, patch_type, patch_data, created_at FROM traces WHERE branch_id = ?1 AND seq_in_branch >= ?2 AND seq_in_branch <= ?3 ORDER BY seq_in_branch ASC",
            )
            .map_err(|e| StorageError::Database(e.to_string()))?;

        let traces = stmt
            .query_map(rusqlite::params![branch_id, from_seq, to_seq], |row| {
                let patch_type_str: String = row.get(5)?;
                let patch_data_str: String = row.get(6)?;
                Ok(TraceEntry {
                    trace_id: row.get(0)?,
                    run_id: row.get(1)?,
                    branch_id: row.get(2)?,
                    seq_in_branch: row.get(3)?,
                    plugin_id: row.get(4)?,
                    patch_type: match patch_type_str.as_str() {
                        "state_update" => PatchType::StateUpdate,
                        "route_signal" => PatchType::RouteSignal,
                        "error" => PatchType::Error,
                        "lifecycle" => PatchType::Lifecycle,
                        "rollback" => PatchType::Rollback,
                        _ => PatchType::StateUpdate,
                    },
                    patch_data: serde_json::from_str(&patch_data_str).map_err(|e| {
                        rusqlite::Error::FromSqlConversionFailure(
                            6,
                            rusqlite::types::Type::Text,
                            Box::new(e),
                        )
                    })?,
                    created_at: row.get(7)?,
                })
            })
            .map_err(|e| StorageError::Database(e.to_string()))?;

        traces
            .into_iter()
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| StorageError::Database(e.to_string()))
    }

    /// 获取当前分支最大 seq_in_branch。
    pub fn get_current_seq(&self, run_id: &str) -> Result<u32, StorageError> {
        let conn = self.conn.lock();
        let row: rusqlite::Result<i64> = conn.query_row(
            "SELECT current_seq FROM runs WHERE run_id = ?1",
            rusqlite::params![run_id],
            |row| row.get(0),
        );
        match row {
            Ok(seq) => Ok(seq as u32),
            Err(rusqlite::Error::QueryReturnedNoRows) => {
                Err(StorageError::NotFound(format!("run not found: {}", run_id)))
            }
            Err(e) => Err(StorageError::Database(e.to_string())),
        }
    }

    /// 获取当前分支 ID。
    pub fn get_current_branch(&self, run_id: &str) -> Result<String, StorageError> {
        let conn = self.conn.lock();
        let row: rusqlite::Result<String> = conn.query_row(
            "SELECT current_branch FROM runs WHERE run_id = ?1",
            rusqlite::params![run_id],
            |row| row.get(0),
        );
        match row {
            Ok(branch) => Ok(branch),
            Err(rusqlite::Error::QueryReturnedNoRows) => {
                Err(StorageError::NotFound(format!("run not found: {}", run_id)))
            }
            Err(e) => Err(StorageError::Database(e.to_string())),
        }
    }

    /// 获取所有 blob 记录（用于测试）。
    pub fn get_blob_record(&self, blob_id: &str) -> Result<BlobRecord, StorageError> {
        let conn = self.conn.lock();
        let row = conn.query_row(
            "SELECT blob_id, mime_type, size_bytes, created_at FROM blobs WHERE blob_id = ?1",
            rusqlite::params![blob_id],
            |row| {
                Ok(BlobRecord {
                    blob_id: row.get(0)?,
                    mime_type: row.get(1)?,
                    size_bytes: row.get::<_, i64>(2)? as u64,
                    created_at: row.get(3)?,
                })
            },
        );
        match row {
            Ok(r) => Ok(r),
            Err(rusqlite::Error::QueryReturnedNoRows) => Err(StorageError::NotFound(format!(
                "blob not found: {}",
                blob_id
            ))),
            Err(e) => Err(StorageError::Database(e.to_string())),
        }
    }
}

#[async_trait]
impl StorageBackend for SqliteStore {
    async fn get_run(&self, run_id: &str) -> Result<RunRecord, StorageError> {
        let conn = self.conn.lock();
        let row = conn.query_row(
            "SELECT run_id, config_hash, status, tenant_id, created_at, ended_at, current_branch, current_seq, metadata FROM runs WHERE run_id = ?1",
            rusqlite::params![run_id],
            |row| {
                let status_str: String = row.get(2)?;
                let metadata_str: Option<String> = row.get(8)?;
                Ok(RunRecord {
                    run_id: row.get(0)?,
                    config_hash: row.get(1)?,
                    status: match status_str.as_str() {
                        "running" => RunStatus::Running,
                        "suspended" => RunStatus::Suspended,
                        "completed" => RunStatus::Completed,
                        "failed" => RunStatus::Failed,
                        _ => RunStatus::Running,
                    },
                    tenant_id: row.get(3)?,
                    created_at: row.get(4)?,
                    ended_at: row.get(5)?,
                    current_branch: row.get(6)?,
                    current_seq: row.get::<_, i64>(7)? as u32,
                    metadata: metadata_str
                        .and_then(|s| serde_json::from_str(&s).ok()),
                })
            },
        );
        match row {
            Ok(r) => Ok(r),
            Err(rusqlite::Error::QueryReturnedNoRows) => {
                Err(StorageError::NotFound(format!("run not found: {}", run_id)))
            }
            Err(e) => Err(StorageError::Database(e.to_string())),
        }
    }

    async fn get_messages(
        &self,
        _run_id: &str,
        branch_id: &str,
    ) -> Result<Vec<MessageRecord>, StorageError> {
        let conn = self.conn.lock();
        let mut stmt = conn
            .prepare(
                "SELECT message_id, run_id, branch_id, seq_in_branch, role, blob_id, content_preview, created_at FROM messages WHERE branch_id = ?1 ORDER BY seq_in_branch ASC",
            )
            .map_err(|e| StorageError::Database(e.to_string()))?;

        let msgs = stmt
            .query_map(rusqlite::params![branch_id], |row| {
                Ok(MessageRecord {
                    message_id: row.get(0)?,
                    run_id: row.get(1)?,
                    branch_id: row.get(2)?,
                    seq_in_branch: row.get::<_, i64>(3)? as u32,
                    role: row.get(4)?,
                    blob_id: row.get(5)?,
                    content_preview: row.get(6)?,
                    created_at: row.get(7)?,
                })
            })
            .map_err(|e| StorageError::Database(e.to_string()))?;

        msgs.into_iter()
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| StorageError::Database(e.to_string()))
    }

    async fn get_recent_messages(
        &self,
        run_id: &str,
        branch_id: &str,
        n: usize,
    ) -> Result<Vec<Message>, StorageError> {
        let conn = self.conn.lock();
        let mut stmt = conn
            .prepare(
                "SELECT m.message_id, m.role, m.blob_id, m.content_preview,
                        b.data
                 FROM messages m
                 LEFT JOIN blobs b ON m.blob_id = b.blob_id
                 WHERE m.run_id = ?1 AND m.branch_id = ?2
                 ORDER BY m.seq_in_branch DESC
                 LIMIT ?3",
            )
            .map_err(|e| StorageError::Database(e.to_string()))?;

        let msgs = stmt
            .query_map(rusqlite::params![run_id, branch_id, n as i64], |row| {
                let blob_id: Option<String> = row.get(2)?;
                let content_preview: Option<String> = row.get(3)?;
                let blob_data: Option<Vec<u8>> = row.get(4)?;
                let content = blob_data
                    .as_deref()
                    .and_then(|d| std::str::from_utf8(d).ok())
                    .map(|s| s.to_string())
                    .or(content_preview)
                    .ok_or_else(|| {
                        rusqlite::Error::FromSqlConversionFailure(
                            4,
                            rusqlite::types::Type::Text,
                            Box::new(std::io::Error::new(
                                std::io::ErrorKind::InvalidData,
                                format!("message content missing: blob_id={:?}", blob_id),
                            )),
                        )
                    })?;
                Ok(Message {
                    message_id: row.get(0)?,
                    role: row.get(1)?,
                    content,
                    blob_id,
                })
            })
            .map_err(|e| StorageError::Database(e.to_string()))?;

        let mut messages: Vec<Message> = msgs
            .into_iter()
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| StorageError::Database(e.to_string()))?;
        messages.reverse(); // 恢复时间顺序
        Ok(messages)
    }

    async fn get_blob(&self, blob_id: &str) -> Result<Vec<u8>, StorageError> {
        let conn = self.conn.lock();
        let row = conn.query_row(
            "SELECT data FROM blobs WHERE blob_id = ?1",
            rusqlite::params![blob_id],
            |row| row.get::<_, Vec<u8>>(0),
        );
        match row {
            Ok(data) => Ok(data),
            Err(rusqlite::Error::QueryReturnedNoRows) => Err(StorageError::NotFound(format!(
                "blob not found: {}",
                blob_id
            ))),
            Err(e) => Err(StorageError::Database(e.to_string())),
        }
    }

    async fn append_trace(&self, entry: TraceEntry) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        let patch_type_str = match entry.patch_type {
            PatchType::StateUpdate => "state_update",
            PatchType::RouteSignal => "route_signal",
            PatchType::Error => "error",
            PatchType::Lifecycle => "lifecycle",
            PatchType::Rollback => "rollback",
        };
        let patch_data_str =
            serde_json::to_string(&entry.patch_data).unwrap_or_else(|_| "{}".to_string());
        conn.execute(
            "INSERT INTO traces (trace_id, run_id, branch_id, seq_in_branch, plugin_id, patch_type, patch_data, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            rusqlite::params![
                entry.trace_id,
                entry.run_id,
                entry.branch_id,
                entry.seq_in_branch,
                entry.plugin_id,
                patch_type_str,
                patch_data_str,
                entry.created_at,
            ],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(())
    }

    async fn create_branch(&self, branch: Branch) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        conn.execute(
            "INSERT INTO branches (branch_id, run_id, parent_branch, parent_seq, created_at) VALUES (?1, ?2, ?3, ?4, ?5)",
            rusqlite::params![
                branch.branch_id,
                branch.run_id,
                branch.parent_branch,
                branch.parent_seq.map(|s| s as i64),
                branch.created_at,
            ],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(())
    }

    async fn update_run_status(
        &self,
        run_id: &str,
        status: RunStatus,
        current_branch: Option<&str>,
        current_seq: Option<u32>,
    ) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        let status_str = match status {
            RunStatus::Running => "running",
            RunStatus::Suspended => "suspended",
            RunStatus::Completed => "completed",
            RunStatus::Failed => "failed",
        };
        let now = chrono::Utc::now().to_rfc3339();

        match (current_branch, current_seq) {
            (Some(branch), Some(seq)) => {
                let ended = if status == RunStatus::Completed || status == RunStatus::Failed {
                    Some(now.as_str())
                } else {
                    None
                };
                conn.execute(
                    "UPDATE runs SET status = ?1, current_branch = ?2, current_seq = ?3, ended_at = COALESCE(?4, ended_at) WHERE run_id = ?5",
                    rusqlite::params![status_str, branch, seq as i64, ended, run_id],
                )
                .map_err(|e| StorageError::Database(e.to_string()))?;
            }
            (None, None) => {
                let ended = if status == RunStatus::Completed || status == RunStatus::Failed {
                    Some(now.as_str())
                } else {
                    None
                };
                conn.execute(
                    "UPDATE runs SET status = ?1, ended_at = COALESCE(?2, ended_at) WHERE run_id = ?3",
                    rusqlite::params![status_str, ended, run_id],
                )
                .map_err(|e| StorageError::Database(e.to_string()))?;
            }
            _ => {
                return Err(StorageError::Database(
                    "current_branch and current_seq must both be Some or both be None".to_string(),
                ));
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_open_memory() {
        let store = SqliteStore::open_memory().unwrap();
        // 验证表存在——插入一条 run
        store
            .create_run("test_run_1", "hash_abc", "tenant_1")
            .unwrap();
    }

    #[tokio::test]
    async fn test_create_and_get_run() {
        let store = SqliteStore::open_memory().unwrap();
        store
            .create_run("run_1", "config_hash_1", "tenant_1")
            .unwrap();

        let run = store.get_run("run_1").await.unwrap();
        assert_eq!(run.run_id, "run_1");
        assert_eq!(run.config_hash, "config_hash_1");
        assert_eq!(run.tenant_id, "tenant_1");
        assert_eq!(run.status, RunStatus::Running);
        assert_eq!(run.current_branch, "main");
        assert_eq!(run.current_seq, 0);
    }

    #[tokio::test]
    async fn test_get_run_not_found() {
        let store = SqliteStore::open_memory().unwrap();
        let result = store.get_run("nonexistent").await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_update_run_status() {
        let store = SqliteStore::open_memory().unwrap();
        store.create_run("run_2", "hash", "tenant").unwrap();

        store
            .update_run_status("run_2", RunStatus::Suspended, None, None)
            .await
            .unwrap();
        let run = store.get_run("run_2").await.unwrap();
        assert_eq!(run.status, RunStatus::Suspended);

        store
            .update_run_status("run_2", RunStatus::Completed, None, None)
            .await
            .unwrap();
        let run = store.get_run("run_2").await.unwrap();
        assert_eq!(run.status, RunStatus::Completed);
        assert!(run.ended_at.is_some());
    }

    #[tokio::test]
    async fn test_update_run_status_with_branch() {
        let store = SqliteStore::open_memory().unwrap();
        store.create_run("run_3", "hash", "tenant").unwrap();

        store
            .update_run_status(
                "run_3",
                RunStatus::Running,
                Some("main.rollback.001"),
                Some(5),
            )
            .await
            .unwrap();

        let run = store.get_run("run_3").await.unwrap();
        assert_eq!(run.current_branch, "main.rollback.001");
        assert_eq!(run.current_seq, 5);
    }

    #[tokio::test]
    async fn test_store_and_get_blob() {
        let store = SqliteStore::open_memory().unwrap();
        let data = b"Hello, World!";
        let blob_id = store.store_blob(data, "text/plain").unwrap();

        let record = store.get_blob_record(&blob_id).unwrap();
        assert_eq!(record.mime_type, "text/plain");
        assert_eq!(record.size_bytes, 13);

        let loaded = store.get_blob(&blob_id).await.unwrap();
        assert_eq!(loaded, data);
    }

    #[tokio::test]
    async fn test_blob_dedup() {
        let store = SqliteStore::open_memory().unwrap();
        let data = b"same content";
        let id1 = store.store_blob(data, "text/plain").unwrap();
        let id2 = store.store_blob(data, "text/plain").unwrap();
        assert_eq!(id1, id2); // 相同内容应得到相同 blob_id
    }

    #[tokio::test]
    async fn test_append_message() {
        let store = SqliteStore::open_memory().unwrap();
        store.create_run("run_4", "hash", "tenant").unwrap();
        store
            .append_message("msg_1", "run_4", "main", 0, "user", None, Some("Hello"))
            .unwrap();

        let msgs = store.get_messages("run_4", "main").await.unwrap();
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0].role, "user");
        assert_eq!(msgs[0].content_preview, Some("Hello".to_string()));
    }

    #[tokio::test]
    async fn test_append_trace_and_replay() {
        let store = SqliteStore::open_memory().unwrap();
        store.create_run("run_5", "hash", "tenant").unwrap();

        // 追加 3 条 trace
        for i in 0..3u32 {
            let entry = TraceEntry {
                trace_id: format!("trace_{}", i),
                run_id: "run_5".to_string(),
                branch_id: "main".to_string(),
                seq_in_branch: i,
                plugin_id: format!("plugin_{}", i),
                patch_type: PatchType::StateUpdate,
                patch_data: json!({"key": format!("value_{}", i)}),
                created_at: chrono::Utc::now().to_rfc3339(),
            };
            store.append_trace(entry).await.unwrap();
        }

        // 正向重放 seq 0..2
        let traces = store.get_traces("main", 0, 2).unwrap();
        assert_eq!(traces.len(), 3);
        assert_eq!(traces[0].plugin_id, "plugin_0");
        assert_eq!(traces[2].plugin_id, "plugin_2");
        assert_eq!(traces[1].patch_data["key"], "value_1");
    }

    #[tokio::test]
    async fn test_create_branch() {
        let store = SqliteStore::open_memory().unwrap();
        store.create_run("run_6", "hash", "tenant").unwrap();

        let branch = Branch {
            branch_id: "main.rollback.001".to_string(),
            run_id: "run_6".to_string(),
            parent_branch: Some("main".to_string()),
            parent_seq: Some(1),
            created_at: chrono::Utc::now().to_rfc3339(),
        };
        store.create_branch(branch).await.unwrap();
    }

    #[tokio::test]
    async fn test_get_current_seq_and_branch() {
        let store = SqliteStore::open_memory().unwrap();
        store.create_run("run_7", "hash", "tenant").unwrap();

        assert_eq!(store.get_current_seq("run_7").unwrap(), 0);
        assert_eq!(store.get_current_branch("run_7").unwrap(), "main");

        store
            .update_run_status("run_7", RunStatus::Running, Some("main"), Some(3))
            .await
            .unwrap();
        assert_eq!(store.get_current_seq("run_7").unwrap(), 3);
    }

    #[tokio::test]
    async fn test_get_recent_messages_with_blob() {
        let store = SqliteStore::open_memory().unwrap();
        store.create_run("run_8", "hash", "tenant").unwrap();

        let blob_id = store.store_blob(b"full content", "text/plain").unwrap();
        store
            .append_message(
                "msg_1",
                "run_8",
                "main",
                0,
                "user",
                Some(&blob_id),
                Some("short"),
            )
            .unwrap();

        let msgs = store
            .get_recent_messages("run_8", "main", 10)
            .await
            .unwrap();
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0].content, "full content"); // 从 blob 加载完整内容
        assert_eq!(msgs[0].blob_id, Some(blob_id));
    }

    /// 回归测试：消息同时缺少 blob_id 和 content_preview 时，get_recent_messages 必须返回错误
    /// 而非静默返回空 content（AC-05-2 + §1.4 错误处理铁律）。
    #[tokio::test]
    async fn test_get_recent_messages_missing_content_returns_error() {
        let store = SqliteStore::open_memory().unwrap();
        store.create_run("run_9", "hash", "tenant").unwrap();
        store
            .append_message("msg_2", "run_9", "main", 0, "assistant", None, None)
            .unwrap();

        let result = store.get_recent_messages("run_9", "main", 10).await;
        assert!(
            result.is_err(),
            "expected error when blob_id and content_preview both missing"
        );
    }
}

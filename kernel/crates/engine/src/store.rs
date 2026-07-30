//! SQLite 四表存储实现
//!
//! 实现 StorageBackend trait，使用 rusqlite 作为 SQLite 后端。
//! 四表模型：runs / messages / traces / blobs（ADR ③④）。
//!
//! [来源: docs/working/adr_engine_design.md §4.2]

use std::sync::Arc;

use async_trait::async_trait;
use agentos_core::traits::{MessageQueryOpts, SessionListFilter, StorageBackend};
use agentos_core::types::{
    BlobRecord, Branch, Message, MessageRecord, PatchType, RunRecord, RunStatus, SessionRecord,
    StorageError, TraceEntry,
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
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    -- pipeline_id：消息所属管道（= 其他项目的会话 id）。两域解耦的关键：
    -- 消息层只按 pipeline_id 查询，不加 thread_id（不关心会话归属）。
    -- 对齐 0.1 ExecutionRecordData.pipeline_run_id。可空兼容历史数据。
    pipeline_id     TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(run_id, branch_id, seq_in_branch)
);
-- 按 pipeline_id 查询历史消息的主索引（get_messages_by_pipeline 走此路径）
CREATE INDEX IF NOT EXISTS idx_messages_pipeline_seq ON messages(pipeline_id, tenant_id, seq_in_branch);
CREATE TABLE IF NOT EXISTS traces (
    trace_id      TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    branch_id     TEXT NOT NULL,
    seq_in_branch INTEGER NOT NULL,
    plugin_id     TEXT NOT NULL,
    patch_type    TEXT NOT NULL,
    patch_data    TEXT NOT NULL,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
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
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    created_at    TEXT NOT NULL,
    PRIMARY KEY (branch_id, run_id)
);
-- 域2：session 标签夹层（对齐 0.1 SessionModel）。
-- 两域解耦：sessions 只持 pipeline_ids JSON 引用列表，不反向 join messages。
-- 会话只是聚合管道引用的标签，管道自治（消息/执行由管道自身负责）。
CREATE TABLE IF NOT EXISTS sessions (
    thread_id          TEXT PRIMARY KEY,
    title              TEXT,
    intent             TEXT,
    current_state      TEXT NOT NULL DEFAULT 'active',
    agent_id           TEXT,
    active_pipeline_id TEXT,
    pipeline_ids       TEXT,
    metadata           TEXT,
    tenant_id          TEXT NOT NULL DEFAULT 'default',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    last_active_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON sessions(tenant_id, updated_at DESC);
";

/// 为旧库（建表时无 tenant_id 列）补加 tenant_id 列。
///
/// 仅在列缺失时执行 `ALTER TABLE ... ADD COLUMN`，幂等。blob 表不加（内容寻址，靠上游归属）。
fn migrate_add_tenant_id(conn: &Connection) -> Result<(), StorageError> {
    for table in ["messages", "traces", "branches"] {
        let has_col: bool = conn
            .prepare(&format!("PRAGMA table_info({})", table))
            .map_err(|e| StorageError::Database(e.to_string()))?
            .query_map([], |row| row.get::<_, String>(1))
            .map_err(|e| StorageError::Database(e.to_string()))?
            .filter_map(|r| r.ok())
            .any(|col| col == "tenant_id");
        if !has_col {
            conn.execute(
                &format!(
                    "ALTER TABLE {} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
                    table
                ),
                [],
            )
            .map_err(|e| StorageError::Database(e.to_string()))?;
        }
    }
    Ok(())
}

/// 为旧库（建表时无 pipeline_id 列）补加 messages.pipeline_id 列。
///
/// 仅在列缺失时执行 `ALTER TABLE ... ADD COLUMN`，幂等。可空，兼容历史数据。
/// 对齐 0.1 消息按 pipeline_run_id 分组的语义——pipeline_id 是消息层的查询主键。
fn migrate_add_pipeline_id(conn: &Connection) -> Result<(), StorageError> {
    let has_col: bool = conn
        .prepare("PRAGMA table_info(messages)")
        .map_err(|e| StorageError::Database(e.to_string()))?
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(|e| StorageError::Database(e.to_string()))?
        .filter_map(|r| r.ok())
        .any(|col| col == "pipeline_id");
    if !has_col {
        conn.execute(
            "ALTER TABLE messages ADD COLUMN pipeline_id TEXT",
            [],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
        // 旧库已建表后补加索引（新建库走 DDL 内的 CREATE INDEX）
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_pipeline_seq ON messages(pipeline_id, tenant_id, seq_in_branch)",
            [],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
    }
    Ok(())
}

/// SQLite 四表存储实现。
///
/// 使用 `rusqlite::Connection`（线程安全包装在 `Arc<Mutex>` 中）。
/// 支持 WAL 模式以提高并发读写性能。
/// Clone 语义：浅拷 Arc<Mutex<Connection>>，多个 clone 共享同一连接。
/// spawn_blocking 转发 trait 方法时需要（'self' 借用无法 move 进闭包）。
#[derive(Clone)]
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
        migrate_add_tenant_id(conn)?;
        migrate_add_pipeline_id(conn)?;
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
        // 创建主分支（与 run 同租户）
        conn.execute(
            "INSERT INTO branches (branch_id, run_id, tenant_id, created_at) VALUES ('main', ?1, ?2, ?3)",
            rusqlite::params![run_id, tenant_id, now],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(())
    }

    /// 取管道内下一个 sequence（按 pipeline_id 维度连续递增）。
    ///
    /// sequence 按 pipeline_id 单调递增（跨多轮、跨 run_id），支撑前端
    /// `before_sequence`/`after_sequence` 游标分页。替代旧的"每轮 run_id 重置 0/1"。
    /// 对齐 0.1 ExecutionRecordData.sequence（管道内连续）。
    ///
    /// tenant_id 从 task_local [`agentos_tenant`] 取，无则回退 'default'。
    pub fn next_sequence(&self, pipeline_id: &str) -> Result<u32, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let conn = self.conn.lock();
        let max_seq: i64 = conn
            .query_row(
                "SELECT COALESCE(MAX(seq_in_branch), 0) FROM messages WHERE pipeline_id = ?1 AND tenant_id = ?2",
                rusqlite::params![pipeline_id, tenant_id],
                |row| row.get(0),
            )
            .unwrap_or(0);
        Ok(max_seq as u32 + 1)
    }

    /// 追加消息到 messages 表。
    ///
    /// tenant_id 从 task_local [`agentos_tenant`] 取，无则回退 'default'。
    /// pipeline_id：消息所属管道（消息层查询主键），从 state 读，可为空（兼容旧调用）。
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
        pipeline_id: Option<&str>,
    ) -> Result<(), StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let conn = self.conn.lock();
        let now = chrono::Utc::now().to_rfc3339();
        conn.execute(
            "INSERT INTO messages (message_id, run_id, branch_id, seq_in_branch, role, blob_id, content_preview, tenant_id, created_at, pipeline_id) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
            rusqlite::params![message_id, run_id, branch_id, seq_in_branch, role, blob_id, content_preview, tenant_id, now, pipeline_id],
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

    // ── 域2：session 标签夹内部方法（对齐 0.1 SessionModel）──────────────
    // tenant_id 从 task_local 取。pipeline_ids / metadata 以 JSON 文本存储。

    /// upsert 会话（存在则更新，不存在则插入）。
    fn upsert_session_inner(&self, session: &SessionRecord) -> Result<(), StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let pipeline_ids_json = serde_json::to_string(&session.pipeline_ids)
            .map_err(|e| StorageError::Database(format!("serialize pipeline_ids: {e}")))?;
        let metadata_json = session
            .metadata
            .as_ref()
            .map(|v| serde_json::to_string(v).unwrap_or_else(|_| "null".to_string()));
        let conn = self.conn.lock();
        conn.execute(
            "INSERT INTO sessions (thread_id, title, intent, current_state, agent_id, active_pipeline_id, pipeline_ids, metadata, tenant_id, created_at, updated_at, last_active_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)
             ON CONFLICT(thread_id) DO UPDATE SET
                title = excluded.title,
                intent = excluded.intent,
                current_state = excluded.current_state,
                agent_id = excluded.agent_id,
                active_pipeline_id = excluded.active_pipeline_id,
                pipeline_ids = excluded.pipeline_ids,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at,
                last_active_at = excluded.last_active_at",
            rusqlite::params![
                session.thread_id,
                session.title,
                session.intent,
                session.current_state,
                session.agent_id,
                session.active_pipeline_id,
                pipeline_ids_json,
                metadata_json,
                tenant_id,
                session.created_at,
                session.updated_at,
                session.last_active_at,
            ],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(())
    }

    /// 按 thread_id 取单个会话。
    fn get_session_inner(&self, thread_id: &str) -> Result<Option<SessionRecord>, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let conn = self.conn.lock();
        let row = conn.query_row(
            "SELECT thread_id, title, intent, current_state, agent_id, active_pipeline_id, pipeline_ids, metadata, created_at, updated_at, last_active_at
             FROM sessions WHERE thread_id = ?1 AND tenant_id = ?2",
            rusqlite::params![thread_id, tenant_id],
            Self::row_to_session,
        );
        match row {
            Ok(s) => Ok(Some(s)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(StorageError::Database(e.to_string())),
        }
    }

    /// 列会话（可选按 session_type 过滤，按 updated_at 倒序）。
    fn list_sessions_inner(&self, filter: &SessionListFilter) -> Result<Vec<SessionRecord>, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let conn = self.conn.lock();
        let mut sql = String::from(
            "SELECT thread_id, title, intent, current_state, agent_id, active_pipeline_id, pipeline_ids, metadata, created_at, updated_at, last_active_at
             FROM sessions WHERE tenant_id = ?1",
        );
        // session_type 过滤：JSON metadata 里 session_type 字段匹配
        if filter.session_type.is_some() {
            sql.push_str(" AND json_extract(COALESCE(metadata, '{}'), '$.session_type') = ?2");
        }
        sql.push_str(" ORDER BY updated_at DESC");
        if filter.limit.is_some() {
            sql.push_str(" LIMIT ?3");
        }
        let mut stmt = conn
            .prepare(&sql)
            .map_err(|e| StorageError::Database(e.to_string()))?;
        let mut params: Vec<Box<dyn rusqlite::ToSql>> = vec![Box::new(tenant_id)];
        if let Some(st) = &filter.session_type {
            params.push(Box::new(st.clone()));
        }
        if let Some(lim) = filter.limit {
            params.push(Box::new(lim as i64));
        }
        let param_refs: Vec<&dyn rusqlite::ToSql> = params.iter().map(|p| p.as_ref()).collect();
        let sessions = stmt
            .query_map(param_refs.as_slice(), Self::row_to_session)
            .map_err(|e| StorageError::Database(e.to_string()))?;
        sessions
            .into_iter()
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| StorageError::Database(e.to_string()))
    }

    /// 从查询行构造 SessionRecord（pipeline_ids/metadata 反序列化）。
    fn row_to_session(row: &rusqlite::Row<'_>) -> rusqlite::Result<SessionRecord> {
        let pipeline_ids_str: Option<String> = row.get(6)?;
        let metadata_str: Option<String> = row.get(7)?;
        let pipeline_ids: Vec<String> = pipeline_ids_str
            .as_deref()
            .and_then(|s| serde_json::from_str(s).ok())
            .unwrap_or_default();
        let metadata = metadata_str
            .as_deref()
            .and_then(|s| serde_json::from_str(s).ok());
        Ok(SessionRecord {
            thread_id: row.get(0)?,
            title: row.get(1)?,
            intent: row.get(2)?,
            current_state: row.get(3)?,
            agent_id: row.get(4)?,
            active_pipeline_id: row.get(5)?,
            pipeline_ids,
            metadata,
            created_at: row.get(8)?,
            updated_at: row.get(9)?,
            last_active_at: row.get(10)?,
        })
    }
    pub fn get_traces(
        &self,
        branch_id: &str,
        from_seq: u32,
        to_seq: u32,
    ) -> Result<Vec<TraceEntry>, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let conn = self.conn.lock();
        let mut stmt = conn
            .prepare(
                "SELECT trace_id, run_id, branch_id, seq_in_branch, plugin_id, patch_type, patch_data, created_at FROM traces WHERE branch_id = ?1 AND seq_in_branch >= ?2 AND seq_in_branch <= ?3 AND tenant_id = ?4 ORDER BY seq_in_branch ASC",
            )
            .map_err(|e| StorageError::Database(e.to_string()))?;

        let traces = stmt
            .query_map(rusqlite::params![branch_id, from_seq, to_seq, tenant_id], |row| {
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
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let conn = self.conn.lock();
        let row = conn.query_row(
            "SELECT run_id, config_hash, status, tenant_id, created_at, ended_at, current_branch, current_seq, metadata FROM runs WHERE run_id = ?1 AND tenant_id = ?2",
            rusqlite::params![run_id, tenant_id],
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
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let conn = self.conn.lock();
        let mut stmt = conn
            .prepare(
                "SELECT message_id, run_id, branch_id, seq_in_branch, role, blob_id, content_preview, created_at, pipeline_id FROM messages WHERE branch_id = ?1 AND tenant_id = ?2 ORDER BY seq_in_branch ASC",
            )
            .map_err(|e| StorageError::Database(e.to_string()))?;

        let msgs = stmt
            .query_map(rusqlite::params![branch_id, tenant_id], |row| {
                Ok(MessageRecord {
                    message_id: row.get(0)?,
                    run_id: row.get(1)?,
                    branch_id: row.get(2)?,
                    seq_in_branch: row.get::<_, i64>(3)? as u32,
                    role: row.get(4)?,
                    blob_id: row.get(5)?,
                    content_preview: row.get(6)?,
                    created_at: row.get(7)?,
                    pipeline_id: row.get(8)?,
                })
            })
            .map_err(|e| StorageError::Database(e.to_string()))?;

        msgs.into_iter()
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| StorageError::Database(e.to_string()))
    }

    async fn get_messages_by_pipeline(
        &self,
        pipeline_id: &str,
        opts: MessageQueryOpts,
    ) -> Result<Vec<MessageRecord>, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let conn = self.conn.lock();

        // 动态拼装 WHERE：pipeline_id + tenant_id 必选，游标与 limit 可选
        let mut sql = String::from(
            "SELECT message_id, run_id, branch_id, seq_in_branch, role, blob_id, content_preview, created_at, pipeline_id FROM messages WHERE pipeline_id = ?1 AND tenant_id = ?2",
        );
        let mut idx = 3;
        if opts.before_sequence.is_some() {
            sql.push_str(&format!(" AND seq_in_branch < ?{}", idx));
            idx += 1;
        }
        if opts.after_sequence.is_some() {
            sql.push_str(&format!(" AND seq_in_branch > ?{}", idx));
            idx += 1;
        }
        sql.push_str(" ORDER BY seq_in_branch ASC");
        if opts.limit.is_some() {
            sql.push_str(&format!(" LIMIT ?{}", idx));
        }

        let mut stmt = conn
            .prepare(&sql)
            .map_err(|e| StorageError::Database(e.to_string()))?;

        // 绑定参数顺序须与 SQL 占位符一致
        let mut params: Vec<Box<dyn rusqlite::ToSql>> = vec![
            Box::new(pipeline_id.to_string()),
            Box::new(tenant_id),
        ];
        if let Some(before) = opts.before_sequence {
            params.push(Box::new(before as i64));
        }
        if let Some(after) = opts.after_sequence {
            params.push(Box::new(after as i64));
        }
        if let Some(limit) = opts.limit {
            params.push(Box::new(limit as i64));
        }
        let param_refs: Vec<&dyn rusqlite::ToSql> = params.iter().map(|p| p.as_ref()).collect();

        let msgs = stmt
            .query_map(param_refs.as_slice(), |row| {
                Ok(MessageRecord {
                    message_id: row.get(0)?,
                    run_id: row.get(1)?,
                    branch_id: row.get(2)?,
                    seq_in_branch: row.get::<_, i64>(3)? as u32,
                    role: row.get(4)?,
                    blob_id: row.get(5)?,
                    content_preview: row.get(6)?,
                    created_at: row.get(7)?,
                    pipeline_id: row.get(8)?,
                })
            })
            .map_err(|e| StorageError::Database(e.to_string()))?;

        msgs.into_iter()
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| StorageError::Database(e.to_string()))
    }

    async fn next_sequence(&self, pipeline_id: &str) -> Result<u32, StorageError> {
        let this = self.clone();
        let pipeline_id = pipeline_id.to_string();
        tokio::task::spawn_blocking(move || this.next_sequence(&pipeline_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn get_recent_messages(
        &self,
        run_id: &str,
        branch_id: &str,
        n: usize,
    ) -> Result<Vec<Message>, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let conn = self.conn.lock();
        let mut stmt = conn
            .prepare(
                "SELECT m.message_id, m.role, m.blob_id, m.content_preview,
                        b.data
                 FROM messages m
                 LEFT JOIN blobs b ON m.blob_id = b.blob_id
                 WHERE m.run_id = ?1 AND m.branch_id = ?2 AND m.tenant_id = ?3
                 ORDER BY m.seq_in_branch DESC
                 LIMIT ?4",
            )
            .map_err(|e| StorageError::Database(e.to_string()))?;

        let msgs = stmt
            .query_map(rusqlite::params![run_id, branch_id, tenant_id, n as i64], |row| {
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
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
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
            "INSERT INTO traces (trace_id, run_id, branch_id, seq_in_branch, plugin_id, patch_type, patch_data, tenant_id, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            rusqlite::params![
                entry.trace_id,
                entry.run_id,
                entry.branch_id,
                entry.seq_in_branch,
                entry.plugin_id,
                patch_type_str,
                patch_data_str,
                tenant_id,
                entry.created_at,
            ],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(())
    }

    async fn create_branch(&self, branch: Branch) -> Result<(), StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let conn = self.conn.lock();
        conn.execute(
            "INSERT INTO branches (branch_id, run_id, parent_branch, parent_seq, tenant_id, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            rusqlite::params![
                branch.branch_id,
                branch.run_id,
                branch.parent_branch,
                branch.parent_seq.map(|s| s as i64),
                tenant_id,
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
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
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
                    "UPDATE runs SET status = ?1, current_branch = ?2, current_seq = ?3, ended_at = COALESCE(?4, ended_at) WHERE run_id = ?5 AND tenant_id = ?6",
                    rusqlite::params![status_str, branch, seq as i64, ended, run_id, tenant_id],
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
                    "UPDATE runs SET status = ?1, ended_at = COALESCE(?2, ended_at) WHERE run_id = ?3 AND tenant_id = ?4",
                    rusqlite::params![status_str, ended, run_id, tenant_id],
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

    // 以下三个 trait 方法直接转发到固有 impl（store.rs:150/176/198），
    // 让持 Arc<dyn StorageBackend> 的 PipelineExecutor 能调到写方法。
    async fn create_run(
        &self,
        run_id: &str,
        config_hash: &str,
        tenant_id: &str,
    ) -> Result<(), StorageError> {
        // 在阻塞任务里执行，避免阻塞 async runtime（SqliteStore 用同步 rusqlite）
        let this = self.clone();
        let run_id = run_id.to_string();
        let config_hash = config_hash.to_string();
        let tenant_id = tenant_id.to_string();
        tokio::task::spawn_blocking(move || {
            this.create_run(&run_id, &config_hash, &tenant_id)
        })
        .await
        .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    #[allow(clippy::too_many_arguments)]
    async fn append_message(
        &self,
        message_id: &str,
        run_id: &str,
        branch_id: &str,
        seq_in_branch: u32,
        role: &str,
        blob_id: Option<&str>,
        content_preview: Option<&str>,
        pipeline_id: Option<&str>,
    ) -> Result<(), StorageError> {
        let this = self.clone();
        let message_id = message_id.to_string();
        let run_id = run_id.to_string();
        let branch_id = branch_id.to_string();
        let role = role.to_string();
        let blob_id = blob_id.map(String::from);
        let content_preview = content_preview.map(String::from);
        let pipeline_id = pipeline_id.map(String::from);
        tokio::task::spawn_blocking(move || {
            this.append_message(
                &message_id,
                &run_id,
                &branch_id,
                seq_in_branch,
                &role,
                blob_id.as_deref(),
                content_preview.as_deref(),
                pipeline_id.as_deref(),
            )
        })
        .await
        .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn store_blob(&self, data: &[u8], mime_type: &str) -> Result<String, StorageError> {
        let this = self.clone();
        let data = data.to_vec();
        let mime_type = mime_type.to_string();
        tokio::task::spawn_blocking(move || this.store_blob(&data, &mime_type))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    // ── 域2：session 标签夹 CRUD（对齐 0.1 SessionModel）──────────────
    async fn create_session(&self, session: &SessionRecord) -> Result<(), StorageError> {
        let this = self.clone();
        let session = session.clone();
        tokio::task::spawn_blocking(move || this.upsert_session_inner(&session))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn get_session(&self, thread_id: &str) -> Result<Option<SessionRecord>, StorageError> {
        let this = self.clone();
        let thread_id = thread_id.to_string();
        tokio::task::spawn_blocking(move || this.get_session_inner(&thread_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn list_sessions(
        &self,
        filter: SessionListFilter,
    ) -> Result<Vec<SessionRecord>, StorageError> {
        let this = self.clone();
        tokio::task::spawn_blocking(move || this.list_sessions_inner(&filter))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn update_session(&self, session: &SessionRecord) -> Result<(), StorageError> {
        let this = self.clone();
        let session = session.clone();
        tokio::task::spawn_blocking(move || this.upsert_session_inner(&session))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::types::TenantContext;
    use serde_json::json;

    #[test]
    fn test_open_memory() {
        let store = SqliteStore::open_memory().unwrap();
        // 验证表存在——插入一条 run
        store
            .create_run("test_run_1", "hash_abc", "default")
            .unwrap();
    }

    #[tokio::test]
    async fn test_create_and_get_run() {
        let store = SqliteStore::open_memory().unwrap();
        store
            .create_run("run_1", "config_hash_1", "default")
            .unwrap();

        let run = store.get_run("run_1").await.unwrap();
        assert_eq!(run.run_id, "run_1");
        assert_eq!(run.config_hash, "config_hash_1");
        assert_eq!(run.tenant_id, "default");
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
        store.create_run("run_2", "hash", "default").unwrap();

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
        store.create_run("run_3", "hash", "default").unwrap();

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
        store.create_run("run_4", "hash", "default").unwrap();
        store
            .append_message("msg_1", "run_4", "main", 0, "user", None, Some("Hello"), None)
            .unwrap();

        let msgs = store.get_messages("run_4", "main").await.unwrap();
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0].role, "user");
        assert_eq!(msgs[0].content_preview, Some("Hello".to_string()));
    }

    /// 验证 next_sequence 按 pipeline_id 维度连续递增（跨多轮/跨 run_id）。
    /// 这是修复"每轮硬编码 0/1 导致前端游标分页失效"的核心。
    #[tokio::test]
    async fn test_next_sequence_continuous_across_runs() {
        let store = SqliteStore::open_memory().unwrap();
        let pid = "pipe_continuous";

        // 第一轮（run_A）：user 消息
        store.create_run("run_A", "hash", "default").unwrap();
        let seq1 = store.next_sequence(pid).unwrap();
        assert_eq!(seq1, 1, "空管道首条 seq 应为 1");
        store
            .append_message("m1", "run_A", "main", seq1, "user", None, Some("hi"), Some(pid))
            .unwrap();

        // 同轮（run_A）：assistant 消息
        let seq2 = store.next_sequence(pid).unwrap();
        assert_eq!(seq2, 2);
        store
            .append_message("m2", "run_A", "main", seq2, "assistant", None, Some("hello"), Some(pid))
            .unwrap();

        // 第二轮（新 run_B）：sequence 必须跨 run 连续，不重置为 0
        store.create_run("run_B", "hash", "default").unwrap();
        let seq3 = store.next_sequence(pid).unwrap();
        assert_eq!(seq3, 3, "跨 run_id sequence 必须连续递增");
        store
            .append_message("m3", "run_B", "main", seq3, "user", None, Some("again"), Some(pid))
            .unwrap();
    }

    /// 验证 get_messages_by_pipeline 按 pipeline_id 隔离 + 游标分页。
    /// 这是修复"按 thread_id 查询跨会话混杂"的核心。
    #[tokio::test]
    async fn test_get_messages_by_pipeline_isolation_and_cursor() {
        use agentos_core::traits::MessageQueryOpts;
        let store = SqliteStore::open_memory().unwrap();

        // 管道 A：2 条
        store.create_run("rA", "h", "default").unwrap();
        store.append_message("a1", "rA", "main", 1, "user", None, Some("a-u"), Some("pipeA")).unwrap();
        store.append_message("a2", "rA", "main", 2, "assistant", None, Some("a-ai"), Some("pipeA")).unwrap();
        // 管道 B：2 条（不同 pipeline_id）
        store.create_run("rB", "h", "default").unwrap();
        store.append_message("b1", "rB", "main", 1, "user", None, Some("b-u"), Some("pipeB")).unwrap();
        store.append_message("b2", "rB", "main", 2, "assistant", None, Some("b-ai"), Some("pipeB")).unwrap();

        // 隔离：查 pipeA 只返 A 的 2 条，不含 B
        let msgs_a = store
            .get_messages_by_pipeline("pipeA", MessageQueryOpts::default())
            .await
            .unwrap();
        assert_eq!(msgs_a.len(), 2);
        assert_eq!(msgs_a[0].message_id, "a1");
        assert_eq!(msgs_a[1].message_id, "a2");
        assert!(msgs_a.iter().all(|m| m.pipeline_id.as_deref() == Some("pipeA")));

        // 游标：after_sequence=1 应只返 seq>1 的（即 a2）
        let after = store
            .get_messages_by_pipeline(
                "pipeA",
                MessageQueryOpts { after_sequence: Some(1), ..Default::default() },
            )
            .await
            .unwrap();
        assert_eq!(after.len(), 1);
        assert_eq!(after[0].message_id, "a2");

        // limit：限制 1 条
        let limited = store
            .get_messages_by_pipeline(
                "pipeA",
                MessageQueryOpts { limit: Some(1), ..Default::default() },
            )
            .await
            .unwrap();
        assert_eq!(limited.len(), 1);

        // 历史数据（pipeline_id 为 NULL）不应混入任何管道查询
        store.create_run("rOld", "h", "default").unwrap();
        store.append_message("old1", "rOld", "main", 0, "user", None, Some("legacy"), None).unwrap();
        let still_2 = store
            .get_messages_by_pipeline("pipeA", MessageQueryOpts::default())
            .await
            .unwrap();
        assert_eq!(still_2.len(), 2, "NULL pipeline_id 的历史数据不应被任何管道查到");
    }

    /// 验证迁移幂等性：迁移加列后老查询（不含 pipeline_id）仍正常。
    #[tokio::test]
    async fn test_migration_pipeline_id_column_idempotent() {
        let store = SqliteStore::open_memory().unwrap();
        // init() 内部已执行 migrate_add_pipeline_id，重复调用不应报错
        store.create_run("rM", "h", "default").unwrap();
        store.append_message("m1", "rM", "main", 0, "user", None, Some("ok"), None).unwrap();
        // 老式查询 get_messages 仍可用
        let msgs = store.get_messages("rM", "main").await.unwrap();
        assert_eq!(msgs.len(), 1);
        // MessageRecord 的 pipeline_id 字段存在且为 None
        assert_eq!(msgs[0].pipeline_id, None);
    }

    /// 验证 session CRUD + pipeline_ids JSON 持久化 + upsert 更新。
    /// 对齐 0.1 SessionModel：会话是聚合管道引用的标签夹。
    #[tokio::test]
    async fn test_session_crud_and_pipeline_ids() {
        use agentos_core::traits::SessionListFilter;
        use agentos_core::types::SessionRecord;
        let store = SqliteStore::open_memory().unwrap();
        let now = chrono::Utc::now().to_rfc3339();

        // 创建会话：主管道 pid_main
        let s1 = SessionRecord {
            thread_id: "thread_1".to_string(),
            title: Some("会话一".to_string()),
            intent: Some("测试".to_string()),
            current_state: "active".to_string(),
            agent_id: Some("agentos".to_string()),
            active_pipeline_id: Some("pid_main".to_string()),
            pipeline_ids: vec!["pid_main".to_string()],
            metadata: Some(json!({ "session_type": "main_pipeline", "pinned": true })),
            created_at: now.clone(),
            updated_at: now.clone(),
            last_active_at: Some(now.clone()),
        };
        store.create_session(&s1).await.unwrap();

        // 读取：pipeline_ids JSON 正确反序列化
        let got = store.get_session("thread_1").await.unwrap().unwrap();
        assert_eq!(got.title.as_deref(), Some("会话一"));
        assert_eq!(got.pipeline_ids, vec!["pid_main".to_string()]);
        assert_eq!(got.active_pipeline_id.as_deref(), Some("pid_main"));
        assert_eq!(got.metadata.as_ref().unwrap()["pinned"], true);

        // upsert 更新：追加子管道 pid_sub，但 active 不动（对齐 0.1 set_active=False）
        let mut s1b = got.clone();
        s1b.pipeline_ids.push("pid_sub".to_string());
        s1b.updated_at = chrono::Utc::now().to_rfc3339();
        store.update_session(&s1b).await.unwrap();
        let got2 = store.get_session("thread_1").await.unwrap().unwrap();
        assert_eq!(got2.pipeline_ids, vec!["pid_main", "pid_sub"]);
        assert_eq!(got2.active_pipeline_id.as_deref(), Some("pid_main"), "子管道注册不应覆盖 active");

        // 创建第二个会话（不同 session_type）
        let s2 = SessionRecord {
            thread_id: "thread_2".to_string(),
            title: None,
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: Some("pid2".to_string()),
            pipeline_ids: vec!["pid2".to_string()],
            metadata: Some(json!({ "session_type": "cli" })),
            created_at: now.clone(),
            updated_at: now.clone(),
            last_active_at: None,
        };
        store.create_session(&s2).await.unwrap();

        // list：按 session_type 过滤
        let main_only = store
            .list_sessions(SessionListFilter {
                session_type: Some("main_pipeline".to_string()),
                limit: Some(100),
            })
            .await
            .unwrap();
        assert_eq!(main_only.len(), 1);
        assert_eq!(main_only[0].thread_id, "thread_1");

        // list 全部（不过滤 session_type）
        let all = store.list_sessions(SessionListFilter::default()).await.unwrap();
        assert_eq!(all.len(), 2);

        // get 不存在的会话
        assert!(store.get_session("nonexistent").await.unwrap().is_none());
    }

    #[tokio::test]
    async fn test_append_trace_and_replay() {
        let store = SqliteStore::open_memory().unwrap();
        store.create_run("run_5", "hash", "default").unwrap();

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
        store.create_run("run_6", "hash", "default").unwrap();

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
        store.create_run("run_7", "hash", "default").unwrap();

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
        store.create_run("run_8", "hash", "default").unwrap();

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
                None,
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
        store.create_run("run_9", "hash", "default").unwrap();
        store
            .append_message("msg_2", "run_9", "main", 0, "assistant", None, None, None)
            .unwrap();

        let result = store.get_recent_messages("run_9", "main", 10).await;
        assert!(
            result.is_err(),
            "expected error when blob_id and content_preview both missing"
        );
    }

    /// P0-5 关键验收：跨租户隔离。
    ///
    /// 租户 A 写入数据后，切换到租户 B 的 task_local 作用域：
    /// - get_run / get_messages 必须读不到 A 的数据（隔离生效）
    /// - 切回 A 作用域后仍可读到（数据未丢失）
    ///
    /// tenant_id 通过 task_local 隐式传递，StorageBackend trait 签名不变。
    #[tokio::test]
    async fn test_cross_tenant_isolation() {
        let store = SqliteStore::open_memory().unwrap();

        // 租户 A：创建 run + 追加消息
        let ctx_a = TenantContext::new("tenant_a", "session_a");
        agentos_tenant::scope(ctx_a, async {
            store.create_run("run_a", "hash_a", "tenant_a").unwrap();
            store
                .append_message("msg_a", "run_a", "main", 0, "user", None, Some("hi-a"), None)
                .unwrap();

            // 自身作用域内可读到
            let run = store.get_run("run_a").await.unwrap();
            assert_eq!(run.tenant_id, "tenant_a");
            let msgs = store.get_messages("run_a", "main").await.unwrap();
            assert_eq!(msgs.len(), 1);
        })
        .await;

        // 切换到租户 B 的作用域：必须读不到 A 的数据
        let ctx_b = TenantContext::new("tenant_b", "session_b");
        agentos_tenant::scope(ctx_b, async {
            let run_result = store.get_run("run_a").await;
            assert!(
                run_result.is_err(),
                "tenant B must not see tenant A's run (isolation)"
            );
            let msgs = store.get_messages("run_a", "main").await.unwrap();
            assert!(
                msgs.is_empty(),
                "tenant B must not see tenant A's messages (isolation)"
            );
        })
        .await;

        // 切回 A：数据仍在（未丢失）
        agentos_tenant::scope(TenantContext::new("tenant_a", "session_a"), async {
            let run = store.get_run("run_a").await.unwrap();
            assert_eq!(run.tenant_id, "tenant_a");
        })
        .await;
    }
}

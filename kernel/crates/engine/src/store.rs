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
    BlobRecord, Branch, ExecutionRecord, MemoryRecord, Message, MessageRecord, PipelineRunSummary,
    PatchType, RunRecord, RunStatus, SessionRecord, StorageError, TraceEntry, UserRecord,
};
use parking_lot::Mutex;
use rusqlite::Connection;
use sha2::{Digest, Sha256};
use tracing::{info, warn};

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
    -- reasoning_content：assistant 消息的思考内容（LLM reasoning/chain-of-thought）。
    -- 前端据此渲染思考过程折叠区。仅 role=assistant 且模型输出思考时非空。
    reasoning_content TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(run_id, branch_id, seq_in_branch)
);
-- 工具调用相关列（迁移函数补加，DDL 里同步声明供新建库直接建全）：
-- tool_calls_json：assistant 消息的 tool_calls 数组（OpenAI 结构 JSON）。
-- tool_call_id：role=tool 的结果消息对应的调用 ID，与 tool_calls_json 配对还原调用链。
-- 列允许 NULL，兼容迁移前的扁平历史数据（仅有 user/assistant 文本）。
-- 注意：idx_messages_pipeline_seq 不在 DDL 批里建，改由 migrate_add_pipeline_id 兜底。
-- 原因：旧库 messages 表已存在但缺 pipeline_id 列，CREATE TABLE IF NOT EXISTS 是空操作，
-- 若索引建在 DDL 批里会在 ALTER ADD COLUMN（迁移函数）之前执行，从而因列不存在而整体失败。
-- 把索引创建挪到迁移函数内、确保列存在后再建，对新建库与旧库都安全（CREATE INDEX IF NOT EXISTS 幂等）。
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
-- 域3：execution_records（M1，对齐 0.1 ExecutionRecordData）。
-- 复合主键 (record_id, sequence)：同一 AI 消息多轮迭代共享 record_id，按 sequence 区分。
-- 聚簇/分页键 (pipeline_run_id, sequence) 对齐 0.1 list_by_pipeline 游标分页。
CREATE TABLE IF NOT EXISTS execution_records (
    record_id         TEXT NOT NULL,
    sequence          INTEGER NOT NULL,
    pipeline_run_id   TEXT NOT NULL,
    record_type       TEXT NOT NULL DEFAULT 'ai',
    iteration         INTEGER NOT NULL DEFAULT 0,
    role              TEXT NOT NULL DEFAULT '',
    content           TEXT NOT NULL DEFAULT '',
    name              TEXT,
    tool_call_id      TEXT,
    tool_input        TEXT,
    thinking_content  TEXT,
    tool_calls_json   TEXT,
    attachments_json  TEXT,
    container_task_id TEXT,
    error             TEXT,
    client_message_id TEXT,
    tenant_id         TEXT NOT NULL DEFAULT 'default',
    created_at        TEXT NOT NULL,
    PRIMARY KEY (record_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_exec_pipeline_seq ON execution_records(pipeline_run_id, tenant_id, sequence);
-- 域4：pipeline_run_summaries（M1，对齐 0.1 PipelineRunSummary）。
CREATE TABLE IF NOT EXISTS pipeline_run_summaries (
    run_id           TEXT PRIMARY KEY,
    thread_id        TEXT NOT NULL DEFAULT '',
    total_iterations INTEGER NOT NULL DEFAULT 0,
    total_tokens     TEXT NOT NULL DEFAULT '{}',
    total_seconds    REAL NOT NULL DEFAULT 0,
    total_records    INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT '',
    final_output     TEXT NOT NULL DEFAULT '',
    error            TEXT,
    review_status    TEXT NOT NULL DEFAULT 'pending',
    reviewed_at      TEXT,
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_tenant ON pipeline_run_summaries(tenant_id, created_at DESC);
-- 域5：memory（M1，对齐 0.1 MemoryStore.memories）。0.1 为进程内字典无持久化，下沉内核落 SQLite。
CREATE TABLE IF NOT EXISTS memory (
    id          TEXT PRIMARY KEY,
    content     TEXT NOT NULL,
    memory_type TEXT NOT NULL DEFAULT 'episode',
    tags        TEXT NOT NULL DEFAULT '[]',
    score       REAL NOT NULL DEFAULT 0,
    tenant_id   TEXT NOT NULL DEFAULT 'default',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_tenant ON memory(tenant_id, memory_type);
-- 域6：users（0.5.0 完整用户系统的最小持久化地基）。
-- 一用户一租户：tenant_id = user_id（admin 种子 = 'default'）。username 跨租户全局唯一。
-- 密码明文（DEBT: 0.5.0 替换为哈希）。
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password      TEXT NOT NULL,
    email         TEXT,
    role          TEXT NOT NULL DEFAULT 'user',
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);
-- 域7：pipeline_sessions（会话↔管道映射）。
-- 一个会话下所有管道（主管道 + 子任务管道）都映射到同一 thread_id。
-- 删除会话时按 thread_id 一次查到全部 pipeline_id 级联清理，无需父子关系。
-- 写入时机：persist_run_start（每次管道开跑）+ create/update session（主管道兜底）。
CREATE TABLE IF NOT EXISTS pipeline_sessions (
    pipeline_id TEXT NOT NULL,
    thread_id   TEXT NOT NULL,
    tenant_id   TEXT NOT NULL DEFAULT 'default',
    created_at  TEXT NOT NULL,
    PRIMARY KEY (pipeline_id, tenant_id)
);
CREATE INDEX IF NOT EXISTS idx_ps_thread ON pipeline_sessions(thread_id, tenant_id);
-- 域8：pipeline_state（state 标量字段的实时快照）。
-- 除 messages 外，state 中需要跨轮保留/重建恢复的累计字段（如 track.total_tokens），
-- 每字段一行 upsert。冷启动重建时读出累计值喂回 state，插件自然累加。
-- 用完即弃的传送带字段（raw_tool_calls / tool_results / router.* 等）不进此表。
CREATE TABLE IF NOT EXISTS pipeline_state (
    pipeline_id  TEXT NOT NULL,
    field_key    TEXT NOT NULL,
    field_value  TEXT NOT NULL,
    tenant_id    TEXT NOT NULL DEFAULT 'default',
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (pipeline_id, field_key, tenant_id)
);
-- 域9：pipeline_checkpoints（定期全量 state 快照，留档用）。
-- 每 N 步把当时完整 state 复制一份到此表。冷启动重建优先取最近 checkpoint（O(1) 基线），
-- 再回放其后 traces 增量。checkpoint 存全量（非 diff）——用存储换 O(1) 恢复速度，
-- 与 traces 增量化（省存储）配套。checkpoint 是状态表的留档副本，刻意冗余，N 步才产生一份。
CREATE TABLE IF NOT EXISTS pipeline_checkpoints (
    checkpoint_id  TEXT PRIMARY KEY,
    pipeline_id    TEXT NOT NULL,
    step_no        INTEGER NOT NULL,
    state_json     TEXT NOT NULL,
    tenant_id      TEXT NOT NULL DEFAULT 'default',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cp_pipeline_step ON pipeline_checkpoints(pipeline_id, tenant_id, step_no DESC);
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

/// 为旧库（建表时无 pipeline_id 列）补加 messages.pipeline_id 列，并保证查询索引存在。
///
/// - 列缺失：执行 `ALTER TABLE ... ADD COLUMN`（幂等，可空兼容历史数据）。
/// - 索引：列存在后无条件执行 `CREATE INDEX IF NOT EXISTS`（幂等）。
///   索引之所以放在迁移函数而非 DDL 批里，是为了避免旧库「表已存在但缺列」时索引先于
///   ALTER 执行而失败。对齐 0.1 消息按 pipeline_run_id 分组的语义——pipeline_id 是消息层的查询主键。
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
    }
    // 索引兜底：列已确保存在后创建（CREATE INDEX IF NOT EXISTS 幂等，新建库与旧库都安全）。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_pipeline_seq ON messages(pipeline_id, tenant_id, seq_in_branch)",
        [],
    )
    .map_err(|e| StorageError::Database(e.to_string()))?;
    Ok(())
}

/// 为旧库（建表时无 tool_calls_json / tool_call_id 列）补加这两列。
///
/// 让 messages 表能完整表达多轮工具调用：assistant 的 tool_calls 序列化进
/// tool_calls_json，tool 结果消息的 tool_call_id 进对应列。
/// 仅在列缺失时执行 `ALTER TABLE ... ADD COLUMN`（幂等，可空兼容历史扁平数据）。
fn migrate_add_tool_call_columns(conn: &Connection) -> Result<(), StorageError> {
    let cols: Vec<String> = conn
        .prepare("PRAGMA table_info(messages)")
        .map_err(|e| StorageError::Database(e.to_string()))?
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(|e| StorageError::Database(e.to_string()))?
        .filter_map(|r| r.ok())
        .collect();
    if !cols.iter().any(|c| c == "tool_calls_json") {
        conn.execute("ALTER TABLE messages ADD COLUMN tool_calls_json TEXT", [])
            .map_err(|e| StorageError::Database(e.to_string()))?;
    }
    if !cols.iter().any(|c| c == "tool_call_id") {
        conn.execute("ALTER TABLE messages ADD COLUMN tool_call_id TEXT", [])
            .map_err(|e| StorageError::Database(e.to_string()))?;
    }
    if !cols.iter().any(|c| c == "reasoning_content") {
        conn.execute("ALTER TABLE messages ADD COLUMN reasoning_content TEXT", [])
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

/// 判断 StorageError 是否为 SQLite 损坏类错误（可自愈重建）。
///
/// 覆盖 rusqlite 对坏库的典型报错：
/// - `database disk image is malformed`（SQLITE_CORRUPT=11，宿主 .kernel_02.log 即此报错）
/// - `file is not a database` / `file is encrypted or is not a database`（SQLITE_NOTADB=26）
/// - 其他包含 corrupt 字样的损坏描述
///
/// 权限/IO/磁盘满等非损坏错误**不**在此列，原样传播、不做静默降级。
fn is_corruption_error(e: &StorageError) -> bool {
    match e {
        StorageError::Database(msg) => {
            let lower = msg.to_lowercase();
            lower.contains("malformed")
                || lower.contains("not a database")
                || lower.contains("file is encrypted")
                || lower.contains("corrupt")
        }
        _ => false,
    }
}

/// 从 messages 数组元素提取 content 的字符串表示。
///
/// content 可能是字符串（普通文本/工具结果）或数组（多 part：thinking/text 等）。
/// 数组形式时拼接所有 text part 的 text 字段，保持与前端渲染一致。
fn extract_content_string(msg: &serde_json::Value) -> String {
    match msg.get("content") {
        Some(serde_json::Value::String(s)) => s.clone(),
        Some(serde_json::Value::Array(parts)) => {
            // 多 part：拼接 text/thinking 的内容
            let mut buf = String::new();
            for p in parts {
                if let Some(t) = p.get("type").and_then(|v| v.as_str()) {
                    if t == "text" || t == "thinking" {
                        if let Some(txt) = p.get("text").and_then(|v| v.as_str()) {
                            if !buf.is_empty() {
                                buf.push('\n');
                            }
                            buf.push_str(txt);
                        }
                    }
                }
            }
            buf
        }
        _ => String::new(),
    }
}

/// 备份损坏的 SQLite 文件（含 -wal/-shm 伴生文件）为 `<src>.corrupt-<ts>`，保留现场供排查。
///
/// 注意：WAL 模式下损坏可能落在 -wal/-shm 伴生文件中，因此三个文件一起处理。
/// 备份失败（如文件系统权限异常）不在这里阻断——后续重建 open_inner 若仍失败，
/// 错误会正常传播给调用方，不会假装自愈成功。
fn backup_corrupt_files(path: &str) {
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| format!("{}.{}", d.as_secs(), d.subsec_nanos()))
        .unwrap_or_else(|_| "unknown".to_string());
    for suffix in ["", "-wal", "-shm"] {
        let src = format!("{path}{suffix}");
        let dst = format!("{src}.corrupt-{ts}");
        if std::path::Path::new(&src).exists() {
            match std::fs::rename(&src, &dst) {
                Ok(()) => warn!(
                    from = %src,
                    to = %dst,
                    "损坏的 SQLite 文件已备份保留现场"
                ),
                Err(e) => warn!(
                    from = %src,
                    error = %e,
                    "损坏的 SQLite 文件备份失败（继续尝试重建）"
                ),
            }
        }
    }
}

impl SqliteStore {
    /// 在指定路径创建 SQLite 数据库并初始化四表。
    ///
    /// 损坏自愈：若打开/初始化失败且错误为 SQLite 损坏类（malformed / not a database /
    /// corrupt / file is encrypted，如进程异常退出或磁盘故障留下的坏库），自动将损坏文件
    /// （含 -wal/-shm 伴生文件）备份为 `<path>.corrupt-<ts>` 保留现场，然后重建空库继续
    /// 启动——避免 kernel 因单次库损坏直接崩溃退出（历史 issue：启动后 /health 不响应）。
    /// 其他错误（权限、IO 等）原样传播，不做静默降级。
    pub fn open(path: &str) -> Result<Self, StorageError> {
        match Self::open_inner(path) {
            Ok(store) => Ok(store),
            Err(e) if is_corruption_error(&e) => {
                warn!(
                    path = %path,
                    error = %e,
                    "SQLite 数据库损坏，自动备份并重建新库（原数据保留在 .corrupt-* 备份中）"
                );
                backup_corrupt_files(path);
                Self::open_inner(path)
            }
            Err(e) => Err(e),
        }
    }

    fn open_inner(path: &str) -> Result<Self, StorageError> {
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
        migrate_add_tool_call_columns(conn)?;
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
    /// tenant_id 由调用方显式传入（async trait 层在 spawn_blocking 前从 task_local
    /// 解析——tokio::task_local 不跨 spawn_blocking，若在此处读会恒为 'default'）。
    pub fn next_sequence(&self, pipeline_id: &str, tenant_id: &str) -> Result<u32, StorageError> {
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
    /// tenant_id 由调用方显式传入（async trait 层在 spawn_blocking 前从 task_local
    /// 解析——tokio::task_local 不跨 spawn_blocking，若在此处读会恒为 'default'）。
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
        tenant_id: &str,
    ) -> Result<(), StorageError> {
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

    // ── 域10：分层持久化投影（messages 增量对齐 + 标量快照 + checkpoint）────
    // 设计：引擎 merge state_updates 时，对 messages（系统字段）走 project_messages
    // 增量对齐（索引比对，追加 O(1)）；对插件 manifest 声明的 persistent_fields 走
    // upsert_state_field（标量快照）；传送带字段不投影。checkpoint 每 N 步复制完整 state。

    /// 投影 messages 列表字段到 messages 表（增量对齐，幂等）。
    ///
    /// `new_arr` 是插件执行完返回的完整 messages 数组快照。投影按索引对齐：
    /// - `i < min(N, M)`：逐条比对（role/content/tool_calls/tool_call_id），变了 UPDATE，没变跳过
    ///   （正常对话每轮追加 1-3 条，对齐段全跳过 → O(0)）
    /// - `M > N`：尾部 INSERT 新增（正常 1-2 条 → O(1)）
    /// - `N > M`（压缩场景）：DELETE 索引 ≥ M 的旧行
    ///
    /// message_id 用确定性 `m_{pipeline_id}_{i}`，保证重放幂等不撞主键。
    /// content 存 blob（内容寻址去重），tool_calls 序列化进 tool_calls_json。
    pub fn project_messages(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        new_arr: &[serde_json::Value],
    ) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        let pid = pipeline_id.to_string();
        // 读现有行：index → (message_id, role, blob_id, tool_calls_json, tool_call_id, reasoning_content)
        let mut existing: Vec<(String, String, Option<String>, Option<String>, Option<String>, Option<String>)> = conn
            .prepare(
                "SELECT message_id, role, blob_id, tool_calls_json, tool_call_id, reasoning_content \
                 FROM messages WHERE pipeline_id = ?1 AND tenant_id = ?2 ORDER BY seq_in_branch ASC",
            )
            .map_err(|e| StorageError::Database(e.to_string()))?
            .query_map(rusqlite::params![pid, tenant_id], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, Option<String>>(2)?,
                    row.get::<_, Option<String>>(3)?,
                    row.get::<_, Option<String>>(4)?,
                    row.get::<_, Option<String>>(5)?,
                ))
            })
            .map_err(|e| StorageError::Database(e.to_string()))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| StorageError::Database(e.to_string()))?;
        let n = existing.len();
        let m = new_arr.len();
        let now = chrono::Utc::now().to_rfc3339();

        // ① 比对段：i < min(N, M)，变了 UPDATE
        for (i, new_msg) in new_arr.iter().take(n.min(m)).enumerate() {
            let (msg_id, old_role, old_blob, old_tc_json, old_tc_id, old_reasoning) = &existing[i];
            let role = new_msg
                .get("role")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            // content → blob_id（内容寻址）
            let content = extract_content_string(new_msg);
            let (blob_id, _) = self.ensure_blob_locked(&conn, &content)?;
            let tool_calls_json = new_msg
                .get("tool_calls")
                .map(|tc| serde_json::to_string(tc).unwrap_or_default());
            let tool_call_id = new_msg
                .get("tool_call_id")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string());
            let reasoning_content = new_msg
                .get("reasoning_content")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string());
            // 内容级比对：role / blob_id / tool_calls_json / tool_call_id / reasoning 都一致则跳过
            if role == *old_role
                && blob_id.as_deref() == old_blob.as_deref()
                && tool_calls_json == *old_tc_json
                && tool_call_id == *old_tc_id
                && reasoning_content == *old_reasoning
            {
                continue;
            }
            let content_preview: String = content.chars().take(200).collect();
            conn.execute(
                "UPDATE messages SET role=?2, blob_id=?3, content_preview=?4, tool_calls_json=?5, tool_call_id=?6, reasoning_content=?7 \
                 WHERE message_id=?1",
                rusqlite::params![msg_id, role, blob_id, content_preview, tool_calls_json, tool_call_id, reasoning_content],
            )
            .map_err(|e| StorageError::Database(e.to_string()))?;
        }

        // ② 追加段：M > N，尾部 INSERT
        for i in n.min(m)..m {
            let new_msg = &new_arr[i];
            let msg_id = format!("m_{}_{}", pid, i);
            let role = new_msg
                .get("role")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let content = extract_content_string(new_msg);
            let (blob_id, _) = self.ensure_blob_locked(&conn, &content)?;
            let content_preview: String = content.chars().take(200).collect();
            let tool_calls_json = new_msg
                .get("tool_calls")
                .map(|tc| serde_json::to_string(tc).unwrap_or_default());
            let tool_call_id = new_msg
                .get("tool_call_id")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string());
            let reasoning_content = new_msg
                .get("reasoning_content")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string());
            // branch_id/run_id 用占位（投影语义下 messages 跨 run，按 pipeline 主键）。
            // 取 state 通路里的 run_id 需改签名；此处用 pipeline_id 派生稳定占位即可。
            let run_id = format!("r_{}", pid);
            let branch_id = format!("b_{}", pid);
            conn.execute(
                "INSERT INTO messages (message_id, run_id, branch_id, seq_in_branch, role, blob_id, \
                 content_preview, tenant_id, created_at, pipeline_id, tool_calls_json, tool_call_id, reasoning_content) \
                 VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13)",
                rusqlite::params![
                    msg_id, run_id, branch_id, i as i64, role, blob_id, content_preview, tenant_id,
                    now, pid, tool_calls_json, tool_call_id, reasoning_content
                ],
            )
            .map_err(|e| StorageError::Database(e.to_string()))?;
        }

        // ③ 压缩段：N > M，DELETE 索引 ≥ M 的旧行
        if n > m {
            conn.execute(
                "DELETE FROM messages WHERE pipeline_id=?1 AND tenant_id=?2 AND seq_in_branch >= ?3",
                rusqlite::params![pid, tenant_id, m as i64],
            )
            .map_err(|e| StorageError::Database(e.to_string()))?;
        }
        Ok(())
    }

    /// upsert 一个 state 标量字段到 pipeline_state 表（覆盖最新值，O(1)）。
    ///
    /// 累计语义：投影层无脑覆盖；累加智能在插件里（它读 state 旧值 + 本轮）。
    /// 重建时从 pipeline_state 读出累计值喂回 state，插件自然累加，不归零。
    pub fn upsert_state_field(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        key: &str,
        value: &serde_json::Value,
    ) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        let value_json = serde_json::to_string(value).unwrap_or_else(|_| "null".into());
        let now = chrono::Utc::now().to_rfc3339();
        conn.execute(
            "INSERT INTO pipeline_state (pipeline_id, field_key, field_value, tenant_id, updated_at) \
             VALUES (?1,?2,?3,?4,?5) \
             ON CONFLICT(pipeline_id, field_key, tenant_id) DO UPDATE SET field_value=?3, updated_at=?5",
            rusqlite::params![pipeline_id, key, value_json, tenant_id, now],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(())
    }

    /// 读出某 pipeline 的全部持久化标量字段（冷启动重建用）。
    pub fn load_pipeline_state(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<std::collections::HashMap<String, serde_json::Value>, StorageError> {
        let conn = self.conn.lock();
        let rows = conn
            .prepare("SELECT field_key, field_value FROM pipeline_state WHERE pipeline_id=?1 AND tenant_id=?2")
            .map_err(|e| StorageError::Database(e.to_string()))?
            .query_map(rusqlite::params![pipeline_id, tenant_id], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })
            .map_err(|e| StorageError::Database(e.to_string()))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| StorageError::Database(e.to_string()))?;
        let mut map = std::collections::HashMap::new();
        for (k, v) in rows {
            if let Ok(val) = serde_json::from_str::<serde_json::Value>(&v) {
                map.insert(k, val);
            }
        }
        Ok(map)
    }

    /// 保存一个全量 state checkpoint（每 N 步调用一次，留档用）。
    pub fn save_checkpoint(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        step_no: i64,
        state: &serde_json::Value,
    ) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        let checkpoint_id = format!("cp_{}_{}", pipeline_id, step_no);
        let state_json = serde_json::to_string(state).unwrap_or_else(|_| "{}".into());
        let now = chrono::Utc::now().to_rfc3339();
        // INSERT OR REPLACE：同一 step 重放幂等
        conn.execute(
            "INSERT OR REPLACE INTO pipeline_checkpoints \
             (checkpoint_id, pipeline_id, step_no, state_json, tenant_id, created_at) \
             VALUES (?1,?2,?3,?4,?5,?6)",
            rusqlite::params![checkpoint_id, pipeline_id, step_no, state_json, tenant_id, now],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(())
    }

    /// 取最近一个 checkpoint（step_no 最大），返回 (step_no, state_json)。
    pub fn load_latest_checkpoint(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<Option<(i64, serde_json::Value)>, StorageError> {
        let conn = self.conn.lock();
        let row = conn
            .query_row(
                "SELECT step_no, state_json FROM pipeline_checkpoints \
                 WHERE pipeline_id=?1 AND tenant_id=?2 ORDER BY step_no DESC LIMIT 1",
                rusqlite::params![pipeline_id, tenant_id],
                |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)),
            )
            .ok();
        match row {
            Some((step_no, state_json)) => {
                let state = serde_json::from_str(&state_json).unwrap_or(serde_json::Value::Object(Default::default()));
                Ok(Some((step_no, state)))
            }
            None => Ok(None),
        }
    }

    /// 在已有锁定连接上确保 blob 存在，返回 (blob_id, mime)。
    /// project_messages 复用同锁内的 conn，避免重复加锁死锁。
    fn ensure_blob_locked(
        &self,
        conn: &Connection,
        content: &str,
    ) -> Result<(Option<String>, &'static str), StorageError> {
        if content.is_empty() {
            return Ok((None, "text/plain"));
        }
        let blob_id = Self::compute_blob_id(content.as_bytes());
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
                "INSERT INTO blobs (blob_id, mime_type, size_bytes, data, created_at) VALUES (?1,?2,?3,?4,?5)",
                rusqlite::params![blob_id, "text/plain", content.len() as i64, content.as_bytes(), now],
            )
            .map_err(|e| StorageError::Database(e.to_string()))?;
        }
        Ok((Some(blob_id), "text/plain"))
    }

    // ── 域2：session 标签夹内部方法（对齐 0.1 SessionModel）──────────────
    // tenant_id 从 task_local 取。pipeline_ids / metadata 以 JSON 文本存储。

    /// upsert 会话（存在则更新，不存在则插入）。
    /// tenant_id 由调用方在 spawn_blocking 前解析（task_local 不跨 spawn_blocking）。
    fn upsert_session_inner(
        &self,
        session: &SessionRecord,
        tenant_id: &str,
    ) -> Result<(), StorageError> {
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

    /// 级联删除会话及其全部关联数据（主管道 + 子任务管道的 messages/traces/runs/state）。
    ///
    /// 通过 `pipeline_sessions` 映射表按 thread_id 找到该会话下所有 pipeline_id
    /// （主管道 + 子管道，无需父子关系），再级联清理它们产生的 messages / execution_records /
    /// traces / branches / pipeline_run_summaries / runs，最后删映射表与 sessions 行。
    /// 无记录时同样返回 Ok(())（幂等）。tenant_id 由调用方在 spawn_blocking 前解析。
    /// 单次事务包裹，失败回滚。
    fn delete_session_inner(&self, thread_id: &str, tenant_id: &str) -> Result<(), StorageError> {
        let mut conn = self.conn.lock();
        let tx = conn
            .transaction()
            .map_err(|e| StorageError::Database(format!("begin tx: {e}")))?;

        // 1. 收集该会话全部 pipeline_id：映射表 + sessions.pipeline_ids 兜底（防主管道未写映射）。
        let mut pipeline_ids: Vec<String> = Vec::new();
        {
            let mut stmt = tx
                .prepare(
                    "SELECT pipeline_id FROM pipeline_sessions WHERE thread_id = ?1 AND tenant_id = ?2",
                )
                .map_err(|e| StorageError::Database(e.to_string()))?;
            let rows = stmt
                .query_map(rusqlite::params![thread_id, tenant_id], |row| row.get::<_, String>(0))
                .map_err(|e| StorageError::Database(e.to_string()))?;
            for r in rows {
                let pid = r.map_err(|e| StorageError::Database(e.to_string()))?;
                if !pipeline_ids.contains(&pid) {
                    pipeline_ids.push(pid);
                }
            }
        }
        // 兜底：从 sessions.pipeline_ids (JSON) 补全主管道 id
        let session_row: Option<(Option<String>,)> = tx
            .query_row(
                "SELECT pipeline_ids FROM sessions WHERE thread_id = ?1 AND tenant_id = ?2",
                rusqlite::params![thread_id, tenant_id],
                |row| Ok((row.get::<_, Option<String>>(0)?,)),
            )
            .ok();
        if let Some((Some(json),)) = session_row {
            if let Ok(list) = serde_json::from_str::<Vec<String>>(&json) {
                for pid in list {
                    if !pid.is_empty() && !pipeline_ids.contains(&pid) {
                        pipeline_ids.push(pid);
                    }
                }
            }
        }

        if pipeline_ids.is_empty() {
            // 无任何管道（纯标签会话）：直接删 sessions 行
            tx.execute(
                "DELETE FROM sessions WHERE thread_id = ?1 AND tenant_id = ?2",
                rusqlite::params![thread_id, tenant_id],
            )
            .map_err(|e| StorageError::Database(e.to_string()))?;
            return tx
                .commit()
                .map_err(|e| StorageError::Database(format!("commit: {e}")));
        }

        // 2. execution_records 直接按 pipeline_run_id 删（与 pipeline_id 同义）
        Self::delete_in_clause(
            &tx,
            "DELETE FROM execution_records WHERE pipeline_run_id IN ({placeholders}) AND tenant_id = ?",
            &pipeline_ids,
            tenant_id,
        )?;

        // 3. 收集这些 pipeline_id 产生的 run_id（traces/branches/summaries/runs 按 run_id 删）
        let run_ids: Vec<String> = {
            let placeholders = (0..pipeline_ids.len())
                .map(|i| format!("?{}", i + 1))
                .collect::<Vec<_>>()
                .join(", ");
            let sql = format!(
                "SELECT DISTINCT run_id FROM messages WHERE pipeline_id IN ({placeholders}) AND tenant_id = ?"
            );
            let mut stmt = tx.prepare(&sql).map_err(|e| StorageError::Database(e.to_string()))?;
            let mut params: Vec<&dyn rusqlite::ToSql> =
                pipeline_ids.iter().map(|p| p as &dyn rusqlite::ToSql).collect();
            params.push(&tenant_id);
            let rows = stmt
                .query_map(params.as_slice(), |row| row.get::<_, String>(0))
                .map_err(|e| StorageError::Database(e.to_string()))?;
            let mut out = Vec::new();
            for r in rows {
                out.push(r.map_err(|e| StorageError::Database(e.to_string()))?);
            }
            out
        };

        if !run_ids.is_empty() {
            Self::delete_in_clause(&tx, "DELETE FROM traces WHERE run_id IN ({placeholders})", &run_ids, "")?;
            Self::delete_in_clause(
                &tx,
                "DELETE FROM branches WHERE run_id IN ({placeholders})",
                &run_ids,
                "",
            )?;
            Self::delete_in_clause(
                &tx,
                "DELETE FROM pipeline_run_summaries WHERE run_id IN ({placeholders})",
                &run_ids,
                "",
            )?;
            Self::delete_in_clause(&tx, "DELETE FROM runs WHERE run_id IN ({placeholders})", &run_ids, "")?;
        }

        // 4. messages 按 pipeline_id 删（含主管道 + 子管道）
        Self::delete_in_clause(
            &tx,
            "DELETE FROM messages WHERE pipeline_id IN ({placeholders}) AND tenant_id = ?",
            &pipeline_ids,
            tenant_id,
        )?;

        // 5. 清映射表 + 删 sessions 行
        tx.execute(
            "DELETE FROM pipeline_sessions WHERE thread_id = ?1 AND tenant_id = ?2",
            rusqlite::params![thread_id, tenant_id],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
        tx.execute(
            "DELETE FROM sessions WHERE thread_id = ?1 AND tenant_id = ?2",
            rusqlite::params![thread_id, tenant_id],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;

        tx.commit()
            .map_err(|e| StorageError::Database(format!("commit: {e}")))
    }

    /// 辅助：按 IN (?, ?, ...) 占位符执行 DELETE。
    /// `sql_template` 中用 `{placeholders}` 标记占位符位置；`extra` 为可选的额外参数（如 tenant_id）。
    fn delete_in_clause(
        tx: &rusqlite::Transaction<'_>,
        sql_template: &str,
        values: &[String],
        extra: &str,
    ) -> Result<(), StorageError> {
        let placeholders = (0..values.len())
            .map(|i| format!("?{}", i + 1))
            .collect::<Vec<_>>()
            .join(", ");
        let sql = sql_template.replace("{placeholders}", &placeholders);
        let mut params: Vec<&dyn rusqlite::ToSql> = values.iter().map(|p| p as &dyn rusqlite::ToSql).collect();
        if !extra.is_empty() {
            params.push(&extra);
        }
        tx.execute(&sql, params.as_slice())
            .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(())
    }

    /// 写入 pipeline↔session 映射（幂等：INSERT OR IGNORE）。
    /// 在 persist_run_start 时调用，确保每个管道（主管道/子管道）都记录所属会话。
    /// tenant_id 由调用方在 spawn_blocking 前解析。
    fn link_pipeline_session_inner(
        &self,
        pipeline_id: &str,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<(), StorageError> {
        if pipeline_id.is_empty() || thread_id.is_empty() {
            return Ok(());
        }
        let conn = self.conn.lock();
        let now = chrono::Utc::now().to_rfc3339();
        conn.execute(
            "INSERT OR IGNORE INTO pipeline_sessions (pipeline_id, thread_id, tenant_id, created_at) VALUES (?1, ?2, ?3, ?4)",
            rusqlite::params![pipeline_id, thread_id, tenant_id, now],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(())
    }

    /// 查询某会话下的全部 pipeline_id（主管道 + 子管道）。
    /// tenant_id 由调用方在 spawn_blocking 前解析。
    fn list_pipeline_ids_by_thread_inner(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<String>, StorageError> {
        let conn = self.conn.lock();
        let mut stmt = conn
            .prepare("SELECT pipeline_id FROM pipeline_sessions WHERE thread_id = ?1 AND tenant_id = ?2")
            .map_err(|e| StorageError::Database(e.to_string()))?;
        let rows = stmt
            .query_map(rusqlite::params![thread_id, tenant_id], |row| row.get::<_, String>(0))
            .map_err(|e| StorageError::Database(e.to_string()))?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r.map_err(|e| StorageError::Database(e.to_string()))?);
        }
        Ok(out)
    }

    /// 按 thread_id 取单个会话。tenant_id 由调用方在 spawn_blocking 前解析。
    fn get_session_inner(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Option<SessionRecord>, StorageError> {
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
    /// tenant_id 由调用方在 spawn_blocking 前解析（task_local 不跨 spawn_blocking）。
    fn list_sessions_inner(
        &self,
        filter: &SessionListFilter,
        tenant_id: &str,
    ) -> Result<Vec<SessionRecord>, StorageError> {
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

    // ── 域6：users（0.5.0 完整用户系统的最小持久化地基）───────────────
    // 一用户一租户：tenant_id = user_id。username 跨租户全局唯一。
    // get_user_by_username 不加 tenant 过滤（登录时还没有租户上下文）。

    /// 创建用户（username 全局唯一约束，重复返回 StorageError）。
    /// user.tenant_id 直接入库（一用户一租户，由调用方设 = user_id）。
    fn create_user_inner(&self, user: &UserRecord) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        conn.execute(
            "INSERT INTO users (user_id, username, password, email, role, tenant_id, created_at, last_login_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            rusqlite::params![
                user.user_id,
                user.username,
                user.password,
                user.email,
                user.role,
                user.tenant_id,
                user.created_at,
                user.last_login_at,
            ],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(())
    }

    /// 按 user_id 取用户（跨租户全局查询，token 解析用，不加 tenant 过滤）。
    /// user_id 是全局唯一主键，按 user_id 查天然定位唯一用户。
    /// 注意：token 解析场景（resolve_tenant_id_by_user）尚未确定 tenant，
    /// 故不能按 task_local tenant 过滤（否则查不到 → 回退 default → 隔离失效）。
    fn get_user_by_id_inner(&self, user_id: &str) -> Result<Option<UserRecord>, StorageError> {
        let conn = self.conn.lock();
        let row = conn.query_row(
            "SELECT user_id, username, password, email, role, tenant_id, created_at, last_login_at
             FROM users WHERE user_id = ?1",
            rusqlite::params![user_id],
            Self::row_to_user,
        );
        match row {
            Ok(u) => Ok(Some(u)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(StorageError::Database(e.to_string())),
        }
    }

    /// 按用户名取用户（跨租户全局查询，登录用，不加 tenant 过滤）。
    fn get_user_by_username_inner(
        &self,
        username: &str,
    ) -> Result<Option<UserRecord>, StorageError> {
        let conn = self.conn.lock();
        let row = conn.query_row(
            "SELECT user_id, username, password, email, role, tenant_id, created_at, last_login_at
             FROM users WHERE username = ?1",
            rusqlite::params![username],
            Self::row_to_user,
        );
        match row {
            Ok(u) => Ok(Some(u)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(StorageError::Database(e.to_string())),
        }
    }

    /// 列全部用户（跨租户，管理用）。
    fn list_users_inner(&self) -> Result<Vec<UserRecord>, StorageError> {
        let conn = self.conn.lock();
        let mut stmt = conn
            .prepare(
                "SELECT user_id, username, password, email, role, tenant_id, created_at, last_login_at
                 FROM users ORDER BY created_at ASC",
            )
            .map_err(|e| StorageError::Database(e.to_string()))?;
        let users = stmt
            .query_map([], Self::row_to_user)
            .map_err(|e| StorageError::Database(e.to_string()))?;
        users
            .into_iter()
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| StorageError::Database(e.to_string()))
    }

    /// 更新最近登录时间。
    fn update_last_login_inner(&self, user_id: &str) -> Result<(), StorageError> {
        let now = chrono::Utc::now().to_rfc3339();
        let conn = self.conn.lock();
        conn.execute(
            "UPDATE users SET last_login_at = ?1 WHERE user_id = ?2",
            rusqlite::params![now, user_id],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(())
    }

    /// 删除用户。返回是否删除了行。
    fn delete_user_inner(&self, user_id: &str) -> Result<bool, StorageError> {
        let conn = self.conn.lock();
        let affected = conn
            .execute(
                "DELETE FROM users WHERE user_id = ?1",
                rusqlite::params![user_id],
            )
            .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(affected > 0)
    }

    /// 从查询行构造 UserRecord。
    fn row_to_user(row: &rusqlite::Row<'_>) -> rusqlite::Result<UserRecord> {
        Ok(UserRecord {
            user_id: row.get(0)?,
            username: row.get(1)?,
            password: row.get(2)?,
            email: row.get(3)?,
            role: row.get(4)?,
            tenant_id: row.get(5)?,
            created_at: row.get(6)?,
            last_login_at: row.get(7)?,
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

    /// 查询某会话下的 step 级轨迹（冷启动统一回放用）。
    ///
    /// 经 pipeline_sessions 映射表按 thread_id 找到全部 pipeline_id → 这些 pipeline 产生
    /// 的 run_id → 对应 traces。只返回 step 级轨迹（plugin_id 为配置 step id，不以
    /// `pipeline_` 前缀的旧插件级轨迹被忽略），按 created_at 升序以便按序 merge 回放。
    /// tenant_id 由调用方在 spawn_blocking 前解析。
    fn get_step_traces_by_thread_inner(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<TraceEntry>, StorageError> {
        let conn = self.conn.lock();
        // pipeline_id 集合：映射表 + sessions.pipeline_ids 兜底
        let mut pipeline_ids: Vec<String> = Vec::new();
        {
            let mut stmt = conn
                .prepare("SELECT pipeline_id FROM pipeline_sessions WHERE thread_id = ?1 AND tenant_id = ?2")
                .map_err(|e| StorageError::Database(e.to_string()))?;
            let rows = stmt
                .query_map(rusqlite::params![thread_id, tenant_id], |row| row.get::<_, String>(0))
                .map_err(|e| StorageError::Database(e.to_string()))?;
            for r in rows {
                let pid = r.map_err(|e| StorageError::Database(e.to_string()))?;
                if !pipeline_ids.contains(&pid) {
                    pipeline_ids.push(pid);
                }
            }
        }
        let session_row: Option<(Option<String>,)> = conn
            .query_row(
                "SELECT pipeline_ids FROM sessions WHERE thread_id = ?1 AND tenant_id = ?2",
                rusqlite::params![thread_id, tenant_id],
                |row| Ok((row.get::<_, Option<String>>(0)?,)),
            )
            .ok();
        if let Some((Some(json),)) = session_row {
            if let Ok(list) = serde_json::from_str::<Vec<String>>(&json) {
                for pid in list {
                    if !pid.is_empty() && !pipeline_ids.contains(&pid) {
                        pipeline_ids.push(pid);
                    }
                }
            }
        }
        if pipeline_ids.is_empty() {
            return Ok(vec![]);
        }

        // run_id 集合（经 messages.pipeline_id 反查）
        let placeholders = (0..pipeline_ids.len())
            .map(|_| "?")
            .collect::<Vec<_>>()
            .join(", ");
        let run_sql = format!(
            "SELECT DISTINCT run_id FROM messages WHERE pipeline_id IN ({placeholders}) AND tenant_id = ?"
        );
        let run_ids: Vec<String> = {
            let mut stmt = conn.prepare(&run_sql).map_err(|e| StorageError::Database(e.to_string()))?;
            let mut params: Vec<&dyn rusqlite::ToSql> =
                pipeline_ids.iter().map(|p| p as &dyn rusqlite::ToSql).collect();
            params.push(&tenant_id);
            let rows = stmt
                .query_map(params.as_slice(), |row| row.get::<_, String>(0))
                .map_err(|e| StorageError::Database(e.to_string()))?;
            let mut out = Vec::new();
            for r in rows {
                out.push(r.map_err(|e| StorageError::Database(e.to_string()))?);
            }
            out
        };
        if run_ids.is_empty() {
            return Ok(vec![]);
        }

        // traces：只取 step 级（plugin_id 不以 pipeline_ 开头），按 created_at 升序
        let run_placeholders = (0..run_ids.len())
            .map(|_| "?")
            .collect::<Vec<_>>()
            .join(", ");
        let trace_sql = format!(
            "SELECT trace_id, run_id, branch_id, seq_in_branch, plugin_id, patch_type, patch_data, created_at FROM traces WHERE run_id IN ({run_placeholders}) AND tenant_id = ? AND plugin_id NOT LIKE 'pipeline_%' ORDER BY created_at ASC"
        );
        let mut stmt = conn.prepare(&trace_sql).map_err(|e| StorageError::Database(e.to_string()))?;
        let mut params: Vec<&dyn rusqlite::ToSql> = run_ids.iter().map(|p| p as &dyn rusqlite::ToSql).collect();
        params.push(&tenant_id);
        let traces = stmt
            .query_map(params.as_slice(), |row| {
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

    /// 在锁内执行任意 SQLite 操作（统一数据接口 `/api/v1/db/*` 专用）。
    ///
    /// 暴露只读的 `&Connection` 给上层做表驱动动态访问（`sqlite_master` /
    /// `PRAGMA table_info` / 通用查询/CRUD）。不改变任何持久化语义——
    /// 只是连接访问的受控出口，DDL/迁移逻辑仍由本模块独占。
    ///
    /// 错误类型 `E` 由调用方决定（如 api 层的 `ApiError`），锁获取本身不失败。
    pub fn with_conn<T, E>(
        &self,
        f: impl FnOnce(&Connection) -> Result<T, E>,
    ) -> Result<T, E> {
        let conn = self.conn.lock();
        f(&conn)
    }

    // ── 域3/4/5：execution_records / summaries / memory（M1）──────────

    /// 从查询行构造 ExecutionRecord（JSON 列反序列化）。
    /// 列顺序须与各 SELECT 语句一致：
    /// record_id, sequence, pipeline_run_id, record_type, iteration, role, content,
    /// name, tool_call_id, tool_input, thinking_content, tool_calls_json, attachments_json,
    /// container_task_id, error, client_message_id, created_at
    fn row_to_execution_record(row: &rusqlite::Row<'_>) -> rusqlite::Result<ExecutionRecord> {
        let tool_input = row
            .get::<_, Option<String>>(9)?
            .as_deref()
            .and_then(|s| serde_json::from_str(s).ok());
        Ok(ExecutionRecord {
            record_id: row.get(0)?,
            sequence: row.get::<_, i64>(1)? as u32,
            pipeline_run_id: row.get(2)?,
            record_type: row.get(3)?,
            iteration: row.get::<_, i64>(4)? as u32,
            role: row.get(5)?,
            content: row.get(6)?,
            name: row.get(7)?,
            tool_call_id: row.get(8)?,
            tool_input,
            thinking_content: row.get(10)?,
            tool_calls_json: row.get(11)?,
            attachments_json: row.get(12)?,
            container_task_id: row.get(13)?,
            error: row.get(14)?,
            client_message_id: row.get(15)?,
            created_at: row.get(16)?,
        })
    }

    /// 追加/覆盖一条执行记录（composite key）。
    /// tenant_id 由调用方在 spawn_blocking 前解析（task_local 不跨 spawn_blocking）。
    fn append_execution_record_inner(
        &self,
        record: &ExecutionRecord,
        tenant_id: &str,
    ) -> Result<(), StorageError> {
        let tool_input_json = record
            .tool_input
            .as_ref()
            .map(|v| serde_json::to_string(v).unwrap_or_else(|_| "null".to_string()));
        let conn = self.conn.lock();
        conn.execute(
            "INSERT INTO execution_records (
                record_id, sequence, pipeline_run_id, record_type, iteration, role, content,
                name, tool_call_id, tool_input, thinking_content, tool_calls_json,
                attachments_json, container_task_id, error, client_message_id, tenant_id,
                created_at
             )
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18)
             ON CONFLICT(record_id, sequence) DO UPDATE SET
                pipeline_run_id = excluded.pipeline_run_id,
                record_type = excluded.record_type,
                iteration = excluded.iteration,
                role = excluded.role,
                content = excluded.content,
                name = excluded.name,
                tool_call_id = excluded.tool_call_id,
                tool_input = excluded.tool_input,
                thinking_content = excluded.thinking_content,
                tool_calls_json = excluded.tool_calls_json,
                attachments_json = excluded.attachments_json,
                container_task_id = excluded.container_task_id,
                error = excluded.error,
                client_message_id = excluded.client_message_id,
                created_at = excluded.created_at",
            rusqlite::params![
                record.record_id,
                record.sequence as i64,
                record.pipeline_run_id,
                record.record_type,
                record.iteration as i64,
                record.role,
                record.content,
                record.name,
                record.tool_call_id,
                tool_input_json,
                record.thinking_content,
                record.tool_calls_json,
                record.attachments_json,
                record.container_task_id,
                record.error,
                record.client_message_id,
                tenant_id,
                record.created_at,
            ],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(())
    }

    /// 按 pipeline_run_id 游标分页查询执行记录。
    fn list_execution_records_inner(
        &self,
        pipeline_run_id: &str,
        opts: &MessageQueryOpts,
        tenant_id: &str,
    ) -> Result<Vec<ExecutionRecord>, StorageError> {
        let conn = self.conn.lock();
        let mut sql = String::from(
            "SELECT record_id, sequence, pipeline_run_id, record_type, iteration, role, content,
                    name, tool_call_id, tool_input, thinking_content, tool_calls_json,
                    attachments_json, container_task_id, error, client_message_id, created_at
             FROM execution_records WHERE pipeline_run_id = ? AND tenant_id = ?",
        );
        let mut params: Vec<Box<dyn rusqlite::ToSql>> = vec![
            Box::new(pipeline_run_id.to_string()),
            Box::new(tenant_id),
        ];
        if let Some(s) = opts.before_sequence {
            sql.push_str(" AND sequence < ?");
            params.push(Box::new(s as i64));
        }
        if let Some(s) = opts.after_sequence {
            sql.push_str(" AND sequence > ?");
            params.push(Box::new(s as i64));
        }
        sql.push_str(" ORDER BY sequence ASC");
        if let Some(lim) = opts.limit {
            sql.push_str(" LIMIT ?");
            params.push(Box::new(lim as i64));
        }
        let mut stmt = conn
            .prepare(&sql)
            .map_err(|e| StorageError::Database(e.to_string()))?;
        let param_refs: Vec<&dyn rusqlite::ToSql> = params.iter().map(|p| p.as_ref()).collect();
        let rows = stmt
            .query_map(param_refs.as_slice(), Self::row_to_execution_record)
            .map_err(|e| StorageError::Database(e.to_string()))?;
        rows.into_iter()
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| StorageError::Database(e.to_string()))
    }

    /// 统计某 pipeline_run_id 的执行记录数。
    fn count_execution_records_inner(
        &self,
        pipeline_run_id: &str,
        tenant_id: &str,
    ) -> Result<u64, StorageError> {
        let conn = self.conn.lock();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM execution_records WHERE pipeline_run_id = ?1 AND tenant_id = ?2",
                rusqlite::params![pipeline_run_id, tenant_id],
                |row| row.get(0),
            )
            .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(count as u64)
    }

    /// 删除某 pipeline_run_id 的全部执行记录，返回删除条数。
    fn delete_execution_records_by_session_inner(
        &self,
        pipeline_run_id: &str,
        tenant_id: &str,
    ) -> Result<u64, StorageError> {
        let conn = self.conn.lock();
        let n = conn
            .execute(
                "DELETE FROM execution_records WHERE pipeline_run_id = ?1 AND tenant_id = ?2",
                rusqlite::params![pipeline_run_id, tenant_id],
            )
            .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(n as u64)
    }

    /// 从查询行构造 PipelineRunSummary（total_tokens JSON 反序列化）。
    fn row_to_run_summary(row: &rusqlite::Row<'_>) -> rusqlite::Result<PipelineRunSummary> {
        let total_tokens_str: String = row.get(3)?;
        let total_tokens = serde_json::from_str(&total_tokens_str).unwrap_or_default();
        Ok(PipelineRunSummary {
            run_id: row.get(0)?,
            thread_id: row.get(1)?,
            total_iterations: row.get::<_, i64>(2)? as u32,
            total_tokens,
            total_seconds: row.get(4)?,
            total_records: row.get::<_, i64>(5)? as u32,
            status: row.get(6)?,
            final_output: row.get(7)?,
            error: row.get(8)?,
            review_status: row.get(9)?,
            reviewed_at: row.get(10)?,
            created_at: row.get(12)?,
        })
    }

    /// upsert 管道运行汇总。
    fn save_run_summary_inner(
        &self,
        summary: &PipelineRunSummary,
        tenant_id: &str,
    ) -> Result<(), StorageError> {
        let total_tokens_json = serde_json::to_string(&summary.total_tokens)
            .map_err(|e| StorageError::Database(format!("serialize total_tokens: {e}")))?;
        let conn = self.conn.lock();
        conn.execute(
            "INSERT INTO pipeline_run_summaries (
                run_id, thread_id, total_iterations, total_tokens, total_seconds, total_records,
                status, final_output, error, review_status, reviewed_at, tenant_id, created_at
             )
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)
             ON CONFLICT(run_id) DO UPDATE SET
                thread_id = excluded.thread_id,
                total_iterations = excluded.total_iterations,
                total_tokens = excluded.total_tokens,
                total_seconds = excluded.total_seconds,
                total_records = excluded.total_records,
                status = excluded.status,
                final_output = excluded.final_output,
                error = excluded.error,
                review_status = excluded.review_status,
                reviewed_at = excluded.reviewed_at,
                created_at = excluded.created_at",
            rusqlite::params![
                summary.run_id,
                summary.thread_id,
                summary.total_iterations as i64,
                total_tokens_json,
                summary.total_seconds,
                summary.total_records as i64,
                summary.status,
                summary.final_output,
                summary.error,
                summary.review_status,
                summary.reviewed_at,
                tenant_id,
                summary.created_at,
            ],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(())
    }

    /// 取单个汇总。
    fn get_run_summary_inner(
        &self,
        run_id: &str,
        tenant_id: &str,
    ) -> Result<Option<PipelineRunSummary>, StorageError> {
        let conn = self.conn.lock();
        let row = conn.query_row(
            "SELECT run_id, thread_id, total_iterations, total_tokens, total_seconds,
                    total_records, status, final_output, error, review_status, reviewed_at,
                    tenant_id, created_at
             FROM pipeline_run_summaries WHERE run_id = ?1 AND tenant_id = ?2",
            rusqlite::params![run_id, tenant_id],
            Self::row_to_run_summary,
        );
        match row {
            Ok(s) => Ok(Some(s)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(StorageError::Database(e.to_string())),
        }
    }

    /// 局部更新汇总字段（updates 为对象，键=字段名）。run_id 不存在返回 NotFound。
    fn update_run_summary_inner(
        &self,
        run_id: &str,
        updates: &serde_json::Value,
        tenant_id: &str,
    ) -> Result<(), StorageError> {
        let obj = updates
            .as_object()
            .ok_or_else(|| StorageError::Serialization("updates must be a JSON object".into()))?;
        // 取现有记录（拿 total_tokens 整体重写等），不存在则报错
        let mut existing = self
            .get_run_summary_inner(run_id, tenant_id)?
            .ok_or_else(|| StorageError::NotFound(format!("run summary not found: {run_id}")))?;
        let mut total_tokens = existing.total_tokens.clone();
        let total_tokens_obj = if total_tokens.is_object() {
            total_tokens.as_object().cloned().unwrap_or_default()
        } else {
            serde_json::Map::new()
        };
        for (k, v) in obj {
            match k.as_str() {
                "thread_id" => existing.thread_id = v.as_str().unwrap_or("").to_string(),
                "total_iterations" => existing.total_iterations = v.as_u64().unwrap_or(0) as u32,
                "total_seconds" => existing.total_seconds = v.as_f64().unwrap_or(0.0),
                "total_records" => existing.total_records = v.as_u64().unwrap_or(0) as u32,
                "status" => existing.status = v.as_str().unwrap_or("").to_string(),
                "final_output" => existing.final_output = v.as_str().unwrap_or("").to_string(),
                "error" => existing.error = v.as_str().map(|s| s.to_string()),
                "review_status" => existing.review_status = v.as_str().unwrap_or("").to_string(),
                "reviewed_at" => existing.reviewed_at = v.as_str().map(|s| s.to_string()),
                "total_tokens" => {
                    if let Some(o) = v.as_object() {
                        let mut merged = total_tokens_obj.clone();
                        for (tk, tv) in o {
                            merged.insert(tk.clone(), tv.clone());
                        }
                        total_tokens = serde_json::Value::Object(merged);
                    }
                }
                _ => { /* 未知字段忽略，对齐 0.1 setattr 宽松语义 */ }
            }
        }
        existing.total_tokens = total_tokens;
        self.save_run_summary_inner(&existing, tenant_id)
    }

    /// 列汇总，新创建优先（created_at DESC）。limit=None 取全部。
    fn list_run_summaries_inner(
        &self,
        limit: Option<usize>,
        tenant_id: &str,
    ) -> Result<Vec<PipelineRunSummary>, StorageError> {
        let conn = self.conn.lock();
        let mut sql = String::from(
            "SELECT run_id, thread_id, total_iterations, total_tokens, total_seconds,
                    total_records, status, final_output, error, review_status, reviewed_at,
                    tenant_id, created_at
             FROM pipeline_run_summaries WHERE tenant_id = ? ORDER BY created_at DESC",
        );
        let mut params: Vec<Box<dyn rusqlite::ToSql>> = vec![Box::new(tenant_id)];
        if let Some(lim) = limit {
            sql.push_str(" LIMIT ?");
            params.push(Box::new(lim as i64));
        }
        let mut stmt = conn
            .prepare(&sql)
            .map_err(|e| StorageError::Database(e.to_string()))?;
        let param_refs: Vec<&dyn rusqlite::ToSql> = params.iter().map(|p| p.as_ref()).collect();
        let rows = stmt
            .query_map(param_refs.as_slice(), Self::row_to_run_summary)
            .map_err(|e| StorageError::Database(e.to_string()))?;
        rows.into_iter()
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| StorageError::Database(e.to_string()))
    }

    /// 从查询行构造 MemoryRecord（tags JSON 反序列化）。
    fn row_to_memory(row: &rusqlite::Row<'_>) -> rusqlite::Result<MemoryRecord> {
        let tags_str: String = row.get(3)?;
        let tags = serde_json::from_str(&tags_str).unwrap_or_default();
        Ok(MemoryRecord {
            id: row.get(0)?,
            content: row.get(1)?,
            memory_type: row.get(2)?,
            tags,
            score: row.get::<_, Option<f64>>(4)?.unwrap_or(0.0),
            created_at: row.get(6)?,
        })
    }

    /// 创建记忆条目（id 已存在则替换）。
    fn create_memory_inner(
        &self,
        memory: &MemoryRecord,
        tenant_id: &str,
    ) -> Result<(), StorageError> {
        let tags_json = serde_json::to_string(&memory.tags)
            .map_err(|e| StorageError::Database(format!("serialize tags: {e}")))?;
        let conn = self.conn.lock();
        conn.execute(
            "INSERT INTO memory (id, content, memory_type, tags, score, tenant_id, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
             ON CONFLICT(id) DO UPDATE SET
                content = excluded.content,
                memory_type = excluded.memory_type,
                tags = excluded.tags,
                score = excluded.score,
                created_at = excluded.created_at",
            rusqlite::params![
                memory.id,
                memory.content,
                memory.memory_type,
                tags_json,
                memory.score,
                tenant_id,
                memory.created_at,
            ],
        )
        .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(())
    }

    /// 取单条记忆。
    fn get_memory_inner(
        &self,
        id: &str,
        tenant_id: &str,
    ) -> Result<Option<MemoryRecord>, StorageError> {
        let conn = self.conn.lock();
        let row = conn.query_row(
            "SELECT id, content, memory_type, tags, score, tenant_id, created_at
             FROM memory WHERE id = ?1 AND tenant_id = ?2",
            rusqlite::params![id, tenant_id],
            Self::row_to_memory,
        );
        match row {
            Ok(m) => Ok(Some(m)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(StorageError::Database(e.to_string())),
        }
    }

    /// 列记忆（memory_type 可过滤，limit/offset 分页）。
    fn list_memory_inner(
        &self,
        memory_type: Option<&str>,
        limit: usize,
        offset: usize,
        tenant_id: &str,
    ) -> Result<Vec<MemoryRecord>, StorageError> {
        let conn = self.conn.lock();
        // 用位置无关的参数列表：按出现顺序绑定，LIMIT/OFFSET 用后续占位符
        let mut sql = String::from(
            "SELECT id, content, memory_type, tags, score, tenant_id, created_at
             FROM memory WHERE tenant_id = ?",
        );
        let mut params: Vec<Box<dyn rusqlite::ToSql>> = vec![Box::new(tenant_id)];
        if let Some(mt) = memory_type {
            sql.push_str(" AND memory_type = ?");
            params.push(Box::new(mt.to_string()));
        }
        sql.push_str(" ORDER BY created_at DESC LIMIT ? OFFSET ?");
        params.push(Box::new(limit as i64));
        params.push(Box::new(offset as i64));
        let mut stmt = conn
            .prepare(&sql)
            .map_err(|e| StorageError::Database(e.to_string()))?;
        let param_refs: Vec<&dyn rusqlite::ToSql> = params.iter().map(|p| p.as_ref()).collect();
        let rows = stmt
            .query_map(param_refs.as_slice(), Self::row_to_memory)
            .map_err(|e| StorageError::Database(e.to_string()))?;
        rows.into_iter()
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| StorageError::Database(e.to_string()))
    }

    /// 关键词搜索记忆（评分=匹配次数/内容长度，倒序取 top_k）。
    fn search_memory_inner(
        &self,
        query: &str,
        top_k: usize,
        tenant_id: &str,
    ) -> Result<Vec<MemoryRecord>, StorageError> {
        let all = self.list_memory_inner(None, usize::MAX, 0, tenant_id)?;
        let query_lower = query.to_lowercase();
        let mut scored: Vec<MemoryRecord> = all
            .into_iter()
            .filter_map(|mut m| {
                let content_lower = m.content.to_lowercase();
                if content_lower.contains(&query_lower) {
                    let count = content_lower.matches(&query_lower).count() as f64;
                    m.score = (count / content_lower.len().max(1) as f64 * 10000.0).round() / 10000.0;
                    Some(m)
                } else {
                    None
                }
            })
            .collect();
        scored.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
        scored.truncate(top_k);
        Ok(scored)
    }

    /// 删除记忆，返回是否删除成功。
    fn delete_memory_inner(&self, id: &str, tenant_id: &str) -> Result<bool, StorageError> {
        let conn = self.conn.lock();
        let n = conn
            .execute(
                "DELETE FROM memory WHERE id = ?1 AND tenant_id = ?2",
                rusqlite::params![id, tenant_id],
            )
            .map_err(|e| StorageError::Database(e.to_string()))?;
        Ok(n > 0)
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
                "SELECT message_id, run_id, branch_id, seq_in_branch, role, blob_id, content_preview, created_at, pipeline_id, tool_calls_json, tool_call_id, reasoning_content FROM messages WHERE branch_id = ?1 AND tenant_id = ?2 ORDER BY seq_in_branch ASC",
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
                    tool_calls_json: row.get(9)?,
                    tool_call_id: row.get(10)?,
                    reasoning_content: row.get(11)?,
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
            "SELECT message_id, run_id, branch_id, seq_in_branch, role, blob_id, content_preview, created_at, pipeline_id, tool_calls_json, tool_call_id, reasoning_content FROM messages WHERE pipeline_id = ?1 AND tenant_id = ?2",
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
                    tool_calls_json: row.get(9)?,
                    tool_call_id: row.get(10)?,
                    reasoning_content: row.get(11)?,
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
        // task_local 不跨 spawn_blocking：必须在 blocking 前解析 tenant_id 传入。
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        tokio::task::spawn_blocking(move || this.next_sequence(&pipeline_id, &tenant_id))
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
        // task_local 不跨 spawn_blocking：必须在 blocking 前解析 tenant_id 传入。
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
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
                &tenant_id,
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

    // ── 域10：分层持久化投影（trait async 包装，spawn_blocking + task_local tenant）──

    async fn project_messages(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        new_arr: &[serde_json::Value],
    ) -> Result<(), StorageError> {
        let this = self.clone();
        let pipeline_id = pipeline_id.to_string();
        let tenant_id = tenant_id.to_string();
        let new_arr = new_arr.to_vec();
        tokio::task::spawn_blocking(move || {
            this.project_messages(&pipeline_id, &tenant_id, &new_arr)
        })
        .await
        .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn upsert_state_field(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        key: &str,
        value: &serde_json::Value,
    ) -> Result<(), StorageError> {
        let this = self.clone();
        let pipeline_id = pipeline_id.to_string();
        let tenant_id = tenant_id.to_string();
        let key = key.to_string();
        let value = value.clone();
        tokio::task::spawn_blocking(move || {
            this.upsert_state_field(&pipeline_id, &tenant_id, &key, &value)
        })
        .await
        .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn load_pipeline_state(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<std::collections::HashMap<String, serde_json::Value>, StorageError> {
        let this = self.clone();
        let pipeline_id = pipeline_id.to_string();
        let tenant_id = tenant_id.to_string();
        tokio::task::spawn_blocking(move || this.load_pipeline_state(&pipeline_id, &tenant_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn save_checkpoint(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        step_no: i64,
        state: &serde_json::Value,
    ) -> Result<(), StorageError> {
        let this = self.clone();
        let pipeline_id = pipeline_id.to_string();
        let tenant_id = tenant_id.to_string();
        let state = state.clone();
        tokio::task::spawn_blocking(move || {
            this.save_checkpoint(&pipeline_id, &tenant_id, step_no, &state)
        })
        .await
        .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn load_latest_checkpoint(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<Option<(i64, serde_json::Value)>, StorageError> {
        let this = self.clone();
        let pipeline_id = pipeline_id.to_string();
        let tenant_id = tenant_id.to_string();
        tokio::task::spawn_blocking(move || this.load_latest_checkpoint(&pipeline_id, &tenant_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    // ── 域2：session 标签夹 CRUD（对齐 0.1 SessionModel）──────────────
    // 注意：tenant_id 必须在 spawn_blocking 之前解析——tokio::task_local 不跨 spawn_blocking。
    async fn create_session(&self, session: &SessionRecord) -> Result<(), StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let this = self.clone();
        let session = session.clone();
        tokio::task::spawn_blocking(move || this.upsert_session_inner(&session, &tenant_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn get_session(&self, thread_id: &str) -> Result<Option<SessionRecord>, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let this = self.clone();
        let thread_id = thread_id.to_string();
        tokio::task::spawn_blocking(move || this.get_session_inner(&thread_id, &tenant_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn list_sessions(
        &self,
        filter: SessionListFilter,
    ) -> Result<Vec<SessionRecord>, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let this = self.clone();
        tokio::task::spawn_blocking(move || this.list_sessions_inner(&filter, &tenant_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn update_session(&self, session: &SessionRecord) -> Result<(), StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let this = self.clone();
        let session = session.clone();
        tokio::task::spawn_blocking(move || this.upsert_session_inner(&session, &tenant_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn delete_session(&self, thread_id: &str) -> Result<(), StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let this = self.clone();
        let thread_id = thread_id.to_string();
        tokio::task::spawn_blocking(move || this.delete_session_inner(&thread_id, &tenant_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn link_pipeline_session(
        &self,
        pipeline_id: &str,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<(), StorageError> {
        let this = self.clone();
        let pipeline_id = pipeline_id.to_string();
        let thread_id = thread_id.to_string();
        let tenant_id = tenant_id.to_string();
        tokio::task::spawn_blocking(move || {
            this.link_pipeline_session_inner(&pipeline_id, &thread_id, &tenant_id)
        })
        .await
        .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn list_pipeline_ids_by_thread(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<String>, StorageError> {
        let this = self.clone();
        let thread_id = thread_id.to_string();
        let tenant_id = tenant_id.to_string();
        tokio::task::spawn_blocking(move || {
            this.list_pipeline_ids_by_thread_inner(&thread_id, &tenant_id)
        })
        .await
        .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn get_step_traces_by_thread(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<TraceEntry>, StorageError> {
        let this = self.clone();
        let thread_id = thread_id.to_string();
        let tenant_id = tenant_id.to_string();
        tokio::task::spawn_blocking(move || {
            this.get_step_traces_by_thread_inner(&thread_id, &tenant_id)
        })
        .await
        .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    // ── 域3/4/5：execution_records / summaries / memory（M1）──────────
    // 注意：tenant_id 必须在 spawn_blocking 之前解析——tokio::task_local 不跨 spawn_blocking。
    async fn append_execution_record(
        &self,
        record: &ExecutionRecord,
    ) -> Result<(), StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let this = self.clone();
        let record = record.clone();
        tokio::task::spawn_blocking(move || this.append_execution_record_inner(&record, &tenant_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn list_execution_records(
        &self,
        pipeline_run_id: &str,
        opts: MessageQueryOpts,
    ) -> Result<Vec<ExecutionRecord>, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let this = self.clone();
        let pipeline_run_id = pipeline_run_id.to_string();
        tokio::task::spawn_blocking(move || {
            this.list_execution_records_inner(&pipeline_run_id, &opts, &tenant_id)
        })
        .await
        .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn count_execution_records(
        &self,
        pipeline_run_id: &str,
    ) -> Result<u64, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let this = self.clone();
        let pipeline_run_id = pipeline_run_id.to_string();
        tokio::task::spawn_blocking(move || {
            this.count_execution_records_inner(&pipeline_run_id, &tenant_id)
        })
        .await
        .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn delete_execution_records_by_session(
        &self,
        pipeline_run_id: &str,
    ) -> Result<u64, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let this = self.clone();
        let pipeline_run_id = pipeline_run_id.to_string();
        tokio::task::spawn_blocking(move || {
            this.delete_execution_records_by_session_inner(&pipeline_run_id, &tenant_id)
        })
        .await
        .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn save_run_summary(
        &self,
        summary: &PipelineRunSummary,
    ) -> Result<(), StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let this = self.clone();
        let summary = summary.clone();
        tokio::task::spawn_blocking(move || this.save_run_summary_inner(&summary, &tenant_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn get_run_summary(
        &self,
        run_id: &str,
    ) -> Result<Option<PipelineRunSummary>, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let this = self.clone();
        let run_id = run_id.to_string();
        tokio::task::spawn_blocking(move || this.get_run_summary_inner(&run_id, &tenant_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn update_run_summary(
        &self,
        run_id: &str,
        updates: &serde_json::Value,
    ) -> Result<(), StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let this = self.clone();
        let run_id = run_id.to_string();
        let updates = updates.clone();
        tokio::task::spawn_blocking(move || {
            this.update_run_summary_inner(&run_id, &updates, &tenant_id)
        })
        .await
        .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn list_run_summaries(
        &self,
        limit: Option<usize>,
    ) -> Result<Vec<PipelineRunSummary>, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let this = self.clone();
        tokio::task::spawn_blocking(move || this.list_run_summaries_inner(limit, &tenant_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn create_memory(&self, memory: &MemoryRecord) -> Result<(), StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let this = self.clone();
        let memory = memory.clone();
        tokio::task::spawn_blocking(move || this.create_memory_inner(&memory, &tenant_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn get_memory(&self, id: &str) -> Result<Option<MemoryRecord>, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let this = self.clone();
        let id = id.to_string();
        tokio::task::spawn_blocking(move || this.get_memory_inner(&id, &tenant_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn list_memory(
        &self,
        memory_type: Option<&str>,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<MemoryRecord>, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let this = self.clone();
        let memory_type = memory_type.map(|s| s.to_string());
        tokio::task::spawn_blocking(move || {
            this.list_memory_inner(memory_type.as_deref(), limit, offset, &tenant_id)
        })
        .await
        .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn search_memory(
        &self,
        query: &str,
        top_k: usize,
    ) -> Result<Vec<MemoryRecord>, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let this = self.clone();
        let query = query.to_string();
        tokio::task::spawn_blocking(move || this.search_memory_inner(&query, top_k, &tenant_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn delete_memory(&self, id: &str) -> Result<bool, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let this = self.clone();
        let id = id.to_string();
        tokio::task::spawn_blocking(move || this.delete_memory_inner(&id, &tenant_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    // ── 域6：users async wrapper（0.5.0 最小持久化地基）──────────────
    // 注意：get_user_by_username / list_users 跨租户查询，不解析 task_local tenant。
    // get_user_by_id 按 tenant 隔离（与消息/会话一致，task_local 在 spawn_blocking 前解析）。

    async fn create_user(&self, user: &UserRecord) -> Result<(), StorageError> {
        let this = self.clone();
        let user = user.clone();
        tokio::task::spawn_blocking(move || this.create_user_inner(&user))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn get_user_by_id(&self, user_id: &str) -> Result<Option<UserRecord>, StorageError> {
        // 跨租户查询（token 解析场景，user_id 是全局主键），不依赖 task_local tenant
        let this = self.clone();
        let user_id = user_id.to_string();
        tokio::task::spawn_blocking(move || this.get_user_by_id_inner(&user_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn get_user_by_username(
        &self,
        username: &str,
    ) -> Result<Option<UserRecord>, StorageError> {
        // 跨租户查询（登录时还没有租户上下文），不解析 task_local tenant
        let this = self.clone();
        let username = username.to_string();
        tokio::task::spawn_blocking(move || this.get_user_by_username_inner(&username))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn list_users(&self) -> Result<Vec<UserRecord>, StorageError> {
        let this = self.clone();
        tokio::task::spawn_blocking(move || this.list_users_inner())
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn update_last_login(&self, user_id: &str) -> Result<(), StorageError> {
        let this = self.clone();
        let user_id = user_id.to_string();
        tokio::task::spawn_blocking(move || this.update_last_login_inner(&user_id))
            .await
            .map_err(|e| StorageError::Database(format!("join error: {e}")))?
    }

    async fn delete_user(&self, user_id: &str) -> Result<bool, StorageError> {
        let this = self.clone();
        let user_id = user_id.to_string();
        tokio::task::spawn_blocking(move || this.delete_user_inner(&user_id))
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

    /// 回归测试：损坏的 SQLite 文件不应导致 kernel 启动崩溃（issue: kernel 启动后 /health 不响应）。
    ///
    /// 背景：`.kernel_02.log` 末行 `Error: Database("database disk image is malformed")` ——
    /// 进程异常退出/磁盘故障可能留下损坏的 agentos_kernel.db，`SqliteStore::open()` 直接
    /// 返回 Err，main 传播退出，/health 永远不响应，启动脚本 60s 轮询超时。
    ///
    /// 期望行为：open 检测到损坏类错误时自愈——备份损坏文件保留现场，重建空库返回 Ok，
    /// 新库可正常读写（create_run / get_run）。
    #[tokio::test]
    async fn test_open_corrupt_db_self_heals() {
        // 模拟损坏的 SQLite 文件：文件头不是 "SQLite format 3"（进程崩溃/磁盘异常场景）
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("agentos_kernel.db");
        std::fs::write(&db_path, vec![0xDE_u8; 8192]).unwrap();

        // open 不应报错退出，而应自愈：返回 Ok 且新库可正常读写
        let store = SqliteStore::open(db_path.to_str().unwrap()).unwrap();
        store
            .create_run("run_self_heal", "hash_abc", "default")
            .unwrap();
        let run = store.get_run("run_self_heal").await.unwrap();
        assert_eq!(run.run_id, "run_self_heal");

        // 损坏文件应被备份保留现场（agentos_kernel.db.corrupt-*），便于人工排查
        let backup_names: Vec<String> = std::fs::read_dir(dir.path())
            .unwrap()
            .filter_map(|e| e.ok())
            .map(|e| e.file_name().to_string_lossy().to_string())
            .filter(|n| n.starts_with("agentos_kernel.db.corrupt-"))
            .collect();
        assert!(
            !backup_names.is_empty(),
            "损坏文件应被备份保留现场，实际: {:?}",
            backup_names
        );
    }

    /// 正常库不应被误备份重建（自愈只针对损坏场景，健康库原样打开）。
    #[tokio::test]
    async fn test_open_healthy_db_no_backup() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("healthy.db");
        let store = SqliteStore::open(db_path.to_str().unwrap()).unwrap();
        store
            .create_run("run_healthy", "hash_abc", "default")
            .unwrap();
        let run = store.get_run("run_healthy").await.unwrap();
        assert_eq!(run.run_id, "run_healthy");

        // 不应产生任何 .corrupt-* 备份文件
        let corrupt_files: Vec<String> = std::fs::read_dir(dir.path())
            .unwrap()
            .filter_map(|e| e.ok())
            .map(|e| e.file_name().to_string_lossy().to_string())
            .filter(|n| n.contains(".corrupt-"))
            .collect();
        assert!(corrupt_files.is_empty(), "健康库不应产生备份: {:?}", corrupt_files);
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
            .append_message("msg_1", "run_4", "main", 0, "user", None, Some("Hello"), None, "default")
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
        let seq1 = store.next_sequence(pid, "default").unwrap();
        assert_eq!(seq1, 1, "空管道首条 seq 应为 1");
        store
            .append_message("m1", "run_A", "main", seq1, "user", None, Some("hi"), Some(pid), "default")
            .unwrap();

        // 同轮（run_A）：assistant 消息
        let seq2 = store.next_sequence(pid, "default").unwrap();
        assert_eq!(seq2, 2);
        store
            .append_message("m2", "run_A", "main", seq2, "assistant", None, Some("hello"), Some(pid), "default")
            .unwrap();

        // 第二轮（新 run_B）：sequence 必须跨 run 连续，不重置为 0
        store.create_run("run_B", "hash", "default").unwrap();
        let seq3 = store.next_sequence(pid, "default").unwrap();
        assert_eq!(seq3, 3, "跨 run_id sequence 必须连续递增");
        store
            .append_message("m3", "run_B", "main", seq3, "user", None, Some("again"), Some(pid), "default")
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
        store.append_message("a1", "rA", "main", 1, "user", None, Some("a-u"), Some("pipeA"), "default").unwrap();
        store.append_message("a2", "rA", "main", 2, "assistant", None, Some("a-ai"), Some("pipeA"), "default").unwrap();
        // 管道 B：2 条（不同 pipeline_id）
        store.create_run("rB", "h", "default").unwrap();
        store.append_message("b1", "rB", "main", 1, "user", None, Some("b-u"), Some("pipeB"), "default").unwrap();
        store.append_message("b2", "rB", "main", 2, "assistant", None, Some("b-ai"), Some("pipeB"), "default").unwrap();

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
        store.append_message("old1", "rOld", "main", 0, "user", None, Some("legacy"), None, "default").unwrap();
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
        store.append_message("m1", "rM", "main", 0, "user", None, Some("ok"), None, "default").unwrap();
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

        // 删除会话：仅删标签夹行，幂等（删不存在的也 Ok）
        store.delete_session("thread_1").await.unwrap();
        assert!(store.get_session("thread_1").await.unwrap().is_none(), "删除后应查无记录");
        assert_eq!(
            store.list_sessions(SessionListFilter::default()).await.unwrap().len(),
            1,
            "只删了 thread_1，thread_2 应保留"
        );
        // 幂等：再删一次不报错
        store.delete_session("thread_1").await.unwrap();
    }

    /// 验证删除会话级联：主管道 + 子任务管道的 messages/traces/runs/execution_records 全清，
    /// 映射表同步清理，且不误删其他会话数据。
    #[tokio::test]
    async fn test_delete_session_cascade_includes_sub_pipelines() {
        use agentos_core::types::SessionRecord;
        let store = SqliteStore::open_memory().unwrap();
        let now = chrono::Utc::now().to_rfc3339();

        // 会话 thread_1：主管道 pid_main + 子管道 pid_sub（不同 pipeline_id，同 thread_id）
        store.link_pipeline_session("pid_main", "thread_1", "default").await.unwrap();
        store.link_pipeline_session("pid_sub", "thread_1", "default").await.unwrap();
        let s1 = SessionRecord {
            thread_id: "thread_1".to_string(),
            title: None, intent: None, current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: Some("pid_main".to_string()),
            pipeline_ids: vec!["pid_main".to_string()],
            metadata: None,
            created_at: now.clone(), updated_at: now.clone(), last_active_at: None,
        };
        store.create_session(&s1).await.unwrap();

        // 主管道数据：1 run + 2 messages + 1 trace + 1 execution_record
        store.create_run("run_main", "h", "default").unwrap();
        store.append_message("m1", "run_main", "main", 1, "user", None, Some("u"), Some("pid_main"), "default").unwrap();
        store.append_message("m2", "run_main", "main", 2, "assistant", None, Some("a"), Some("pid_main"), "default").unwrap();
        store.append_trace(TraceEntry {
            trace_id: "t1".into(), run_id: "run_main".into(), branch_id: "main".into(),
            seq_in_branch: 0, plugin_id: "prepare".into(), patch_type: PatchType::StateUpdate,
            patch_data: json!({"k": "v"}), created_at: now.clone(),
        }).await.unwrap();

        // 子管道数据：1 run + 1 message + 1 trace（独立 pipeline_id pid_sub）
        store.create_run("run_sub", "h", "default").unwrap();
        store.append_message("s1", "run_sub", "main", 1, "user", None, Some("su"), Some("pid_sub"), "default").unwrap();
        store.append_trace(TraceEntry {
            trace_id: "t2".into(), run_id: "run_sub".into(), branch_id: "main".into(),
            seq_in_branch: 0, plugin_id: "core".into(), patch_type: PatchType::StateUpdate,
            patch_data: json!({"k2": "v2"}), created_at: now.clone(),
        }).await.unwrap();

        // 另一会话 thread_2：数据应保留
        store.link_pipeline_session("pid_other", "thread_2", "default").await.unwrap();
        let s2 = SessionRecord {
            thread_id: "thread_2".to_string(),
            title: None, intent: None, current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: Some("pid_other".to_string()),
            pipeline_ids: vec!["pid_other".to_string()],
            metadata: None,
            created_at: now.clone(), updated_at: now.clone(), last_active_at: None,
        };
        store.create_session(&s2).await.unwrap();
        store.create_run("run_other", "h", "default").unwrap();
        store.append_message("o1", "run_other", "main", 1, "user", None, Some("ou"), Some("pid_other"), "default").unwrap();

        // 删 thread_1：应级联清掉主管道 + 子管道全部数据
        store.delete_session("thread_1").await.unwrap();

        // thread_1 的数据应全部归零
        let pids = store.list_pipeline_ids_by_thread("thread_1", "default").await.unwrap();
        assert!(pids.is_empty(), "映射表应已清理，无残留 pipeline_id");
        assert!(store.get_session("thread_1").await.unwrap().is_none(), "sessions 行应删除");
        let main_msgs = store.get_messages_by_pipeline("pid_main", agentos_core::traits::MessageQueryOpts::default()).await.unwrap();
        assert!(main_msgs.is_empty(), "主管道 messages 应清空");
        let sub_msgs = store.get_messages_by_pipeline("pid_sub", agentos_core::traits::MessageQueryOpts::default()).await.unwrap();
        assert!(sub_msgs.is_empty(), "子管道 messages 应清空");
        let traces_left = store.get_traces("main", 0, 999).unwrap();
        assert!(traces_left.iter().all(|t| t.run_id != "run_main" && t.run_id != "run_sub"), "traces 应清空");

        // thread_2 数据应保留
        assert!(store.get_session("thread_2").await.unwrap().is_some(), "thread_2 应保留");
        let other_msgs = store.get_messages_by_pipeline("pid_other", agentos_core::traits::MessageQueryOpts::default()).await.unwrap();
        assert_eq!(other_msgs.len(), 1, "thread_2 的 messages 不应被误删");

        // 幂等：再删一次不报错
        store.delete_session("thread_1").await.unwrap();
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
                "default",
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
            .append_message("msg_2", "run_9", "main", 0, "assistant", None, None, None, "default")
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
                .append_message("msg_a", "run_a", "main", 0, "user", None, Some("hi-a"), None, "tenant_a")
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

    // ── M1：execution_records / summaries / memory 测试 ──────────────

    /// 执行记录：复合主键（record_id+sequence）、游标分页、count、删除。
    #[tokio::test]
    async fn test_execution_record_crud_and_pagination() {
        use agentos_core::traits::MessageQueryOpts;
        let store = SqliteStore::open_memory().unwrap();
        let now = chrono::Utc::now().to_rfc3339();

        // 同一 record_id 的两轮迭代（共享 record_id，sequence 不同）——复合主键不丢
        for seq in 0..2u32 {
            let r = ExecutionRecord {
                record_id: "rec_a".to_string(),
                pipeline_run_id: "pipe_1".to_string(),
                record_type: "ai".to_string(),
                sequence: seq,
                iteration: seq,
                role: "assistant".to_string(),
                content: format!("reply iter {seq}"),
                name: None,
                tool_call_id: None,
                tool_input: Some(json!({"name": "search", "args": {"q": "x"}})),
                thinking_content: Some("thinking...".to_string()),
                tool_calls_json: None,
                attachments_json: None,
                container_task_id: Some("task_1".to_string()),
                error: None,
                client_message_id: None,
                created_at: now.clone(),
            };
            store.append_execution_record(&r).await.unwrap();
        }
        // 另一条 tool 记录
        store
            .append_execution_record(&ExecutionRecord {
                record_id: "rec_b".to_string(),
                pipeline_run_id: "pipe_1".to_string(),
                record_type: "tool".to_string(),
                sequence: 2,
                iteration: 1,
                role: "tool".to_string(),
                content: "tool output".to_string(),
                name: Some("search".to_string()),
                tool_call_id: Some("call_1".to_string()),
                tool_input: None,
                thinking_content: None,
                tool_calls_json: None,
                attachments_json: None,
                container_task_id: None,
                error: None,
                client_message_id: None,
                created_at: now.clone(),
            })
            .await
            .unwrap();

        // count
        assert_eq!(store.count_execution_records("pipe_1").await.unwrap(), 3);

        // list 全部（按 sequence 升序）
        let all = store
            .list_execution_records("pipe_1", MessageQueryOpts::default())
            .await
            .unwrap();
        assert_eq!(all.len(), 3);
        // 复合主键：两轮迭代都在（record_id=rec_a 出现两次）
        assert_eq!(all.iter().filter(|r| r.record_id == "rec_a").count(), 2);
        assert_eq!(all[0].sequence, 0);
        assert_eq!(all[2].record_type, "tool");
        assert_eq!(all[2].tool_call_id.as_deref(), Some("call_1"));
        // JSON 字段往返
        assert_eq!(all[0].tool_input.as_ref().unwrap()["name"], "search");

        // 游标分页：before_sequence=2 → seq 0,1
        let before = store
            .list_execution_records(
                "pipe_1",
                MessageQueryOpts {
                    before_sequence: Some(2),
                    after_sequence: None,
                    limit: None,
                },
            )
            .await
            .unwrap();
        assert_eq!(before.len(), 2);
        assert_eq!(before[1].sequence, 1);

        // limit 截断
        let lim = store
            .list_execution_records(
                "pipe_1",
                MessageQueryOpts {
                    before_sequence: None,
                    after_sequence: None,
                    limit: Some(1),
                },
            )
            .await
            .unwrap();
        assert_eq!(lim.len(), 1);

        // 覆盖（同 composite key 覆盖 content）
        let mut dup = all[1].clone();
        dup.content = "updated".to_string();
        store.append_execution_record(&dup).await.unwrap();
        assert_eq!(store.count_execution_records("pipe_1").await.unwrap(), 3, "覆盖不新增");
        let after_dup = store
            .list_execution_records("pipe_1", MessageQueryOpts::default())
            .await
            .unwrap();
        assert_eq!(after_dup[1].content, "updated");

        // 按会话删除
        let n = store.delete_execution_records_by_session("pipe_1").await.unwrap();
        assert_eq!(n, 3);
        assert_eq!(store.count_execution_records("pipe_1").await.unwrap(), 0);
        // 幂等：再删返回 0
        assert_eq!(
            store.delete_execution_records_by_session("pipe_1").await.unwrap(),
            0
        );
    }

    /// 汇总：upsert / get / 局部更新（含 total_tokens 合并）/ list 倒序。
    #[tokio::test]
    async fn test_run_summary_crud_and_update() {
        let store = SqliteStore::open_memory().unwrap();
        let now = chrono::Utc::now().to_rfc3339();

        let s1 = PipelineRunSummary {
            run_id: "run_x".to_string(),
            thread_id: "thread_1".to_string(),
            total_iterations: 3,
            total_tokens: json!({"input_tokens": 100, "output_tokens": 50}),
            total_seconds: 12.5,
            total_records: 3,
            status: "completed".to_string(),
            final_output: "done".to_string(),
            error: None,
            review_status: "pending".to_string(),
            reviewed_at: None,
            created_at: now.clone(),
        };
        store.save_run_summary(&s1).await.unwrap();
        // 第二个（更晚创建，验证 list 倒序）
        let s2 = PipelineRunSummary {
            run_id: "run_y".to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            ..s1.clone()
        };
        store.save_run_summary(&s2).await.unwrap();

        // get
        let got = store.get_run_summary("run_x").await.unwrap().unwrap();
        assert_eq!(got.total_iterations, 3);
        assert_eq!(got.total_tokens["input_tokens"], 100);
        assert_eq!(got.review_status, "pending");

        // 局部更新：status + review_status + total_tokens 合并新增 cached_tokens
        store
            .update_run_summary(
                "run_x",
                &json!({
                    "status": "reviewed",
                    "review_status": "reviewed",
                    "total_tokens": {"cached_tokens": 20}
                }),
            )
            .await
            .unwrap();
        let got2 = store.get_run_summary("run_x").await.unwrap().unwrap();
        assert_eq!(got2.status, "reviewed");
        assert_eq!(got2.review_status, "reviewed");
        // total_tokens 合并：原 input/output 仍在 + 新增 cached
        assert_eq!(got2.total_tokens["input_tokens"], 100);
        assert_eq!(got2.total_tokens["cached_tokens"], 20);

        // update 不存在的 run_id → NotFound
        let err = store.update_run_summary("nope", &json!({"status": "x"})).await;
        assert!(matches!(err, Err(StorageError::NotFound(_))));

        // list：created_at DESC（run_y 更晚在前）
        let listed = store.list_run_summaries(Some(10)).await.unwrap();
        assert_eq!(listed.len(), 2);
        assert_eq!(listed[0].run_id, "run_y");
    }

    /// memory：CRUD + memory_type 过滤 + 关键词搜索评分。
    #[tokio::test]
    async fn test_memory_crud_search() {
        let store = SqliteStore::open_memory().unwrap();
        let now = chrono::Utc::now().to_rfc3339();

        store
            .create_memory(&MemoryRecord {
                id: "mem_1".to_string(),
                content: "the quick brown fox".to_string(),
                memory_type: "episode".to_string(),
                tags: vec!["animal".to_string()],
                score: 0.0,
                created_at: now.clone(),
            })
            .await
            .unwrap();
        store
            .create_memory(&MemoryRecord {
                id: "mem_2".to_string(),
                content: "fox fox fox everywhere".to_string(),
                memory_type: "semantic".to_string(),
                tags: vec![],
                score: 0.0,
                created_at: now.clone(),
            })
            .await
            .unwrap();

        // get
        let got = store.get_memory("mem_1").await.unwrap().unwrap();
        assert_eq!(got.tags, vec!["animal".to_string()]);

        // list 全部
        assert_eq!(
            store.list_memory(None, 100, 0).await.unwrap().len(),
            2
        );
        // list 按 memory_type 过滤
        assert_eq!(
            store.list_memory(Some("semantic"), 100, 0).await.unwrap().len(),
            1
        );

        // 搜索 "fox"：两命中，多次匹配的 mem_2 得分更高排前
        let results = store.search_memory("fox", 5).await.unwrap();
        assert_eq!(results.len(), 2);
        assert_eq!(results[0].id, "mem_2", "多次匹配应得分更高");
        assert!(results[0].score > results[1].score);

        // 删除
        assert!(store.delete_memory("mem_1").await.unwrap());
        assert!(!store.delete_memory("mem_1").await.unwrap(), "幂等返回 false");
        assert!(store.get_memory("mem_1").await.unwrap().is_none());
    }

    /// execution_records / memory 跨租户隔离（核心不变量）。
    #[tokio::test]
    async fn test_m1_cross_tenant_isolation() {
        let store = SqliteStore::open_memory().unwrap();
        let now = chrono::Utc::now().to_rfc3339();

        // 租户 A：写执行记录 + memory
        let ctx_a = TenantContext::new("tenant_a", "session_a");
        agentos_tenant::scope(ctx_a, async {
            store
                .append_execution_record(&ExecutionRecord {
                    record_id: "rec_a".to_string(),
                    pipeline_run_id: "pipe_a".to_string(),
                    record_type: "user".to_string(),
                    sequence: 0,
                    iteration: 0,
                    role: "user".to_string(),
                    content: "hi-a".to_string(),
                    name: None,
                    tool_call_id: None,
                    tool_input: None,
                    thinking_content: None,
                    tool_calls_json: None,
                    attachments_json: None,
                    container_task_id: None,
                    error: None,
                    client_message_id: None,
                    created_at: now.clone(),
                })
                .await
                .unwrap();
            store
                .create_memory(&MemoryRecord {
                    id: "mem_a".to_string(),
                    content: "tenant a memory".to_string(),
                    memory_type: "episode".to_string(),
                    tags: vec![],
                    score: 0.0,
                    created_at: now.clone(),
                })
                .await
                .unwrap();
        })
        .await;

        // 租户 B：读不到 A 的数据
        let ctx_b = TenantContext::new("tenant_b", "session_b");
        agentos_tenant::scope(ctx_b, async {
            assert_eq!(store.count_execution_records("pipe_a").await.unwrap(), 0);
            assert!(
                store
                    .list_execution_records("pipe_a", agentos_core::traits::MessageQueryOpts::default())
                    .await
                    .unwrap()
                    .is_empty()
            );
            assert!(store.get_memory("mem_a").await.unwrap().is_none());
            assert!(store.list_memory(None, 100, 0).await.unwrap().is_empty());
        })
        .await;

        // 切回 A：数据仍在
        agentos_tenant::scope(TenantContext::new("tenant_a", "session_a"), async {
            assert_eq!(store.count_execution_records("pipe_a").await.unwrap(), 1);
            assert!(store.get_memory("mem_a").await.unwrap().is_some());
        })
        .await;
    }

    /// session 域跨租户隔离（M1 遗留隐患修复后的回归测试）。
    ///
    /// 原Bug：session 的 5 个写/读方法（create/get/list/update/delete）在 spawn_blocking
    /// 内解析 tenant_id，而 tokio::task_local 不跨 spawn_blocking → 所有 session 都写入
    /// tenant_id="default"，跨租户隔离失效；delete 甚至无 tenant 过滤（跨租户删）。
    /// 修复：tenant_id 在 async wrapper 解析后传入闭包（同 M1 新方法模式）。
    #[tokio::test]
    async fn test_session_cross_tenant_isolation() {
        use agentos_core::traits::SessionListFilter;
        let store = SqliteStore::open_memory().unwrap();
        let now = chrono::Utc::now().to_rfc3339();

        let mk = |tid: &str| SessionRecord {
            thread_id: tid.to_string(),
            title: Some("t".to_string()),
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: None,
            pipeline_ids: vec![],
            metadata: None,
            created_at: now.clone(),
            updated_at: now.clone(),
            last_active_at: None,
        };

        // 租户 A：建会话 thread_a
        agentos_tenant::scope(TenantContext::new("tenant_a", "s_a"), async {
            store.create_session(&mk("thread_a")).await.unwrap();
            assert!(store.get_session("thread_a").await.unwrap().is_some());
            assert_eq!(store.list_sessions(SessionListFilter::default()).await.unwrap().len(), 1);
        })
        .await;

        // 租户 B：读不到 A 的会话；list 为空；get 返回 None；delete 不影响 A
        agentos_tenant::scope(TenantContext::new("tenant_b", "s_b"), async {
            assert!(
                store.get_session("thread_a").await.unwrap().is_none(),
                "tenant B must not see tenant A's session"
            );
            assert!(
                store.list_sessions(SessionListFilter::default()).await.unwrap().is_empty(),
                "tenant B must not list tenant A's sessions"
            );
            // B 尝试删 A 的会话：tenant 过滤下不应影响 A
            store.delete_session("thread_a").await.unwrap();
        })
        .await;

        // 切回 A：会话仍在（B 的删除被 tenant 过滤挡住）
        agentos_tenant::scope(TenantContext::new("tenant_a", "s_a"), async {
            assert!(
                store.get_session("thread_a").await.unwrap().is_some(),
                "tenant B's delete must not affect tenant A (isolation)"
            );
            // A 自己删自己的，成功
            store.delete_session("thread_a").await.unwrap();
            assert!(store.get_session("thread_a").await.unwrap().is_none());
        })
        .await;
    }

    // ── 分层持久化投影测试 ──────────────────────────────────────

    /// project_messages 增量：追加新消息只 INSERT 尾部，幂等重放不重复。
    #[tokio::test]
    async fn test_project_messages_append_and_idempotent() {
        let store = SqliteStore::open_memory().unwrap();
        let pid = "pipe_test_append";
        // 第一次投影：2 条
        let arr1 = vec![
            json!({"role": "user", "content": "你好"}),
            json!({"role": "assistant", "content": "你好！"}),
        ];
        store.project_messages(pid, "default", &arr1).unwrap();
        let cnt1 = count_messages(&store, pid);
        assert_eq!(cnt1, 2, "首次投影应有 2 条");

        // 第二次投影：追加 1 条 tool 调用结果（共 3 条）
        let arr2 = vec![
            json!({"role": "user", "content": "你好"}),
            json!({"role": "assistant", "content": "你好！", "tool_calls": [{"id":"c1","type":"function","function":{"name":"f"}}]}),
            json!({"role": "tool", "content": "结果", "tool_call_id": "c1"}),
        ];
        store.project_messages(pid, "default", &arr2).unwrap();
        let cnt2 = count_messages(&store, pid);
        assert_eq!(cnt2, 3, "追加后应有 3 条");

        // 第三次：重复投影 arr2（幂等，不应产生新行）
        store.project_messages(pid, "default", &arr2).unwrap();
        let cnt3 = count_messages(&store, pid);
        assert_eq!(cnt3, 3, "幂等重放应仍为 3 条");

        // 验证 tool_calls_json 和 tool_call_id 被正确写入
        let msgs = store.get_messages_by_pipeline(pid, Default::default()).await.unwrap();
        let assistant = msgs.iter().find(|m| m.role == "assistant").unwrap();
        assert!(assistant.tool_calls_json.is_some(), "assistant 应有 tool_calls_json");
        assert!(assistant.tool_calls_json.as_ref().unwrap().contains("c1"));
        let tool = msgs.iter().find(|m| m.role == "tool").unwrap();
        assert_eq!(tool.tool_call_id.as_deref(), Some("c1"), "tool 应有 tool_call_id");
    }

    /// project_messages 压缩：新数组比旧短，DELETE 多余尾部。
    #[test]
    fn test_project_messages_compression() {
        let store = SqliteStore::open_memory().unwrap();
        let pid = "pipe_test_compress";
        let arr_long = vec![
            json!({"role": "user", "content": "m1"}),
            json!({"role": "assistant", "content": "m2"}),
            json!({"role": "user", "content": "m3"}),
            json!({"role": "assistant", "content": "m4"}),
        ];
        store.project_messages(pid, "default", &arr_long).unwrap();
        assert_eq!(count_messages(&store, pid), 4);

        // 压缩：只保留前 2 条
        let arr_short = vec![
            json!({"role": "user", "content": "m1"}),
            json!({"role": "assistant", "content": "m2"}),
        ];
        store.project_messages(pid, "default", &arr_short).unwrap();
        assert_eq!(count_messages(&store, pid), 2, "压缩后应剩 2 条");
    }

    /// upsert_state_field 幂等 + load_pipeline_state 往返。
    #[tokio::test]
    async fn test_upsert_and_load_pipeline_state() {
        let store = SqliteStore::open_memory().unwrap();
        let pid = "pipe_state_test";
        // 首次 upsert
        store.upsert_state_field(pid, "default", "track.total_tokens", &json!(150)).unwrap();
        // 再次 upsert 覆盖（累计语义）
        store.upsert_state_field(pid, "default", "track.total_tokens", &json!(300)).unwrap();
        store.upsert_state_field(pid, "default", "track.llm_usage", &json!({"prompt": 10})).unwrap();

        let loaded = store.load_pipeline_state(pid, "default").unwrap();
        assert_eq!(loaded.get("track.total_tokens"), Some(&json!(300)), "应取最新覆盖值");
        assert_eq!(loaded.get("track.llm_usage"), Some(&json!({"prompt": 10})));
        assert_eq!(loaded.len(), 2, "应有 2 个字段");
    }

    /// save_checkpoint / load_latest_checkpoint 往返 + 取最新。
    #[tokio::test]
    async fn test_save_and_load_checkpoint() {
        let store = SqliteStore::open_memory().unwrap();
        let pid = "pipe_ckpt_test";
        store.save_checkpoint(pid, "default", 5, &json!({"messages": [], "step": 5})).unwrap();
        store.save_checkpoint(pid, "default", 10, &json!({"messages": [{"role":"user","content":"hi"}], "step": 10})).unwrap();

        let latest = store.load_latest_checkpoint(pid, "default").unwrap();
        assert!(latest.is_some());
        let (step_no, state) = latest.unwrap();
        assert_eq!(step_no, 10, "应取 step_no 最大的 checkpoint");
        assert_eq!(state["step"], json!(10));
    }

    /// 辅助：统计某 pipeline 的 messages 行数。
    fn count_messages(store: &SqliteStore, pid: &str) -> usize {
        let conn = store.conn.lock();
        conn.query_row(
            "SELECT COUNT(*) FROM messages WHERE pipeline_id = ?1 AND tenant_id = 'default'",
            rusqlite::params![pid],
            |row| row.get::<_, i64>(0),
        )
        .unwrap_or(0) as usize
    }
}

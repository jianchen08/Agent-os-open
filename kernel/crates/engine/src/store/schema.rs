//! SQLite schema 面（store 子模块）：版本常量、迁移链类型与有序迁移集、
//! 建表 DDL（traces/pipeline_state/四表）与退役保全/补列迁移函数。
//!
//! 由 store.rs `mod schema` 挂载：open/init 迁移驱动在父模块，消费项经其
//! `use` 再导出；`encode_state_value` 等记录面 helper 仍归父模块，经
//! `super::` 现取。
use agentos_core::types::StorageError;
use rusqlite::Connection;
use tracing::{error, info, warn};

/// SQLite schema 版本（PRAGMA user_version）。结构变更新增 MIGRATIONS 条目并递增。
/// v2 = pending 输入行收敛为「信封 + DispatchRequest」（ADR-2026-11-17）。
pub(super) const SCHEMA_VERSION: i64 = 2;

/// 单条迁移函数类型：把 schema 从版本 i 升到 i+1。
type MigrationFn = fn(&Connection) -> Result<(), StorageError>;

/// v0 → v1 基线：0.2 全期存量库由 init 的幂等迁移集（建表 IF NOT EXISTS /
/// 补列存在性守护 / 退役表 DROP）收敛到 v1 形状，本条目无动作——占位保证
/// 迁移链索引与版本号一一对应。
fn migrate_v1_baseline(_conn: &Connection) -> Result<(), StorageError> {
    Ok(())
}

/// 有序迁移链：MIGRATIONS[i] 把 schema 从版本 i 升到 i+1。
///
/// v1 基线 = init() 的 DDL + 幂等迁移集（建表 IF NOT EXISTS / 补列存在性守护 /
/// 退役表 DROP，对 0.2 全期存量库幂等）。后续结构变更：追加 (描述, fn) 条目并
/// 递增 SCHEMA_VERSION；open 时只跑 user_version 之后的条目。数据库版本号高于
/// 本内核认知 = 新版本内核写出，显式拒绝（不猜、不静默降级——fail-closed）。
pub(super) const MIGRATIONS: &[(&str, MigrationFn)] = &[
    ("v1_baseline", migrate_v1_baseline),
    (
        "pending_input_dispatch_request",
        migrate_pending_input_dispatch_request,
    ),
];

/// SQLite 存储表 DDL（建表脚本）。
///
/// ADR 2026-09-18（runs/branches 退役与 state 唯一真值）：runs/branches 表退役，
/// 运行簿记 = pipeline_state 标量键；traces = pipeline 级 op 流（定位 =
/// (pipeline_id, seq)，seq 由存储层写入时分配）；pipeline_state 值域标量化
/// （value + value_kind，禁 JSON 文档——过渡期非标量声明键以 value_kind='json' 留存）。
const TRACES_DDL: &str = "
CREATE TABLE IF NOT EXISTS traces (
    trace_id      TEXT PRIMARY KEY,
    pipeline_id   TEXT NOT NULL DEFAULT '',
    seq           INTEGER NOT NULL,
    plugin_id     TEXT NOT NULL,
    patch_type    TEXT NOT NULL,
    patch_data    TEXT NOT NULL,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_pipeline_seq ON traces(pipeline_id, tenant_id, seq);
-- 工具调用记录页（monitoring tool-calls 明细/统计）按租户取最近 N 条 trace 作
-- 窗口：无此索引时 ORDER BY created_at DESC 全表扫描且逐行跳读 patch_data
-- 溢出页尾列（实测 1.5GB/2 万行窗口子查询 0.6s；旧内容谓词版 8.2-8.5s，端点
-- 必超时——BUG-47）。(tenant_id, created_at DESC) 与窗口子查询
-- `WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?` 形状对齐，倒序索引扫描
-- O(window)；无此索引时窗口子查询仍是无界全表扫描。
CREATE INDEX IF NOT EXISTS idx_traces_tenant_created ON traces(tenant_id, created_at DESC);
-- llm_usage 用量聚合（monitoring token 统计 / cost_control 账本）按 patch_data JSON
-- 路径取值：无此索引时每条消费查询都要全表扫描并逐行解析 patch_data（实测 1.7-2.1s，
-- 且同步阻塞插件 sidecar 事件循环，拖垮同进程其它 /ext 端点）。
-- 收益分两层（2026-09-15 实测 1.695s → 0.0002s）：①部分索引条件把扫描限定在含
-- llm_usage 的行，非用量轨迹里的巨型行整体不进索引——单独这一条即得 142x；
-- ②表达式列与消费方 SQL（traces_usage.py）对齐后查询改走覆盖索引不再回表读
-- patch_data，再得约 60x。SQLite 对表达式做解析树规范化（空白、函数名大小写
-- 不敏感），但 JSON 路径/常量/函数语义必须一致，否则覆盖失效回表。
CREATE INDEX IF NOT EXISTS idx_traces_llm_usage ON traces(
    substr(created_at, 1, 10),
    COALESCE(json_extract(patch_data, '$.llm_usage.model'), ''),
    COALESCE(json_extract(patch_data, '$.llm_usage.provider'), ''),
    json_extract(patch_data, '$.llm_usage.input_tokens'),
    json_extract(patch_data, '$.llm_usage.output_tokens'),
    json_extract(patch_data, '$.llm_usage.total_tokens')
) WHERE json_extract(patch_data, '$.llm_usage') IS NOT NULL;
";

const PIPELINE_STATE_DDL: &str = "
CREATE TABLE IF NOT EXISTS pipeline_state (
    pipeline_id  TEXT NOT NULL,
    field_key    TEXT NOT NULL,
    value        TEXT NOT NULL,
    value_kind   TEXT NOT NULL DEFAULT 'str',
    tenant_id    TEXT NOT NULL DEFAULT 'default',
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (pipeline_id, field_key, tenant_id)
);
-- 标量值复合索引：运行簿记键的热查询全部走它——按状态扫描（reap 清扫 running /
-- G8 排空 / /pipelines/runs 按状态过滤）、suspend_request_id 定位挂起管道、
-- run_id 反查所属管道。runs 表退役后这些查询不再有专属表，全靠此索引承接。
CREATE INDEX IF NOT EXISTS idx_state_key_value ON pipeline_state(tenant_id, field_key, value);
";

/// 其余表的建表脚本（与 TRACES_DDL / PIPELINE_STATE_DDL 拼成完整 init DDL）。
pub(super) const DDL: &str = "
CREATE TABLE IF NOT EXISTS blobs (
    blob_id    TEXT PRIMARY KEY,
    mime_type  TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    data       BLOB NOT NULL,
    created_at TEXT NOT NULL
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
-- 域6：users（0.5.0 完整用户系统的最小持久化地基）。
-- 一用户一租户：tenant_id = user_id（admin 种子 = 'default'）。username 跨租户全局唯一。
-- password 存 argon2id 哈希（PHC 字符串）；存量明文行由内核启动迁移哈希化回写。
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password      TEXT NOT NULL,
    email         TEXT,
    role          TEXT NOT NULL DEFAULT 'user',
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    created_at    TEXT NOT NULL,
    last_login_at TEXT,
    must_change_password INTEGER NOT NULL DEFAULT 0
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
-- 域8：pipeline_state（state 标量字段的实时快照，唯一真值）。
-- 除 messages 外，state 中需要跨轮保留/重建恢复的累计字段（如 track.total_tokens），
-- 每字段一行 upsert。值域标量化：value + value_kind（str/int/real/bool/null/json），
-- 禁 JSON 文档——运行簿记键（run_status/run_started_at/run_ended_at/run_config_hash/
-- run_id/suspend_request_id）的热查询走 idx_state_key_value 复合索引。
-- 用完即弃的传送带字段（raw_tool_calls / tool_results / router.* 等）不进此表。
-- 旧 field_value（JSON 壳标量）由迁移一次性剥壳转标量；非标量声明键以
-- value_kind='json' 过渡留存，按域迁移批次消灭（ADR 2026-09-18）。
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
-- message_slots：op-based 消息槽位表（新模型，详见 docs/message_persistence_design.md）。
-- **纯索引表**（任务 7 收敛后）：行上零内容字段，消息全文（role/content/tool_calls/
-- reasoning_content/tool_result envelope）整体序列化成一个 blob 存 blobs 表（内容寻址去重）。
-- 读路径（get_slot_messages_by_pipeline / load_message_history / get_messages_by_pipeline）
-- join blobs 读时重建完整字段——存储收敛，接口形状不变。
-- 不变量：
--   * 主键 = (tenant_id, pipeline_id, seq)：seq 是稳定逻辑槽位（≠ 数组下标），删除留 gap；
--   * message_id = 整条消息规范化 hash（core::ids，排除 seq/_前缀字段）：内容变→id 变、seq 不变；
--   * 压缩：前段 summary 占最小 seq（id 变）、中段删 gap、后段 seq/id 不变、不顺延。
CREATE TABLE IF NOT EXISTS message_slots (
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    pipeline_id      TEXT NOT NULL,
    seq              INTEGER NOT NULL,
    message_id       TEXT NOT NULL,
    blob_id          TEXT,
    run_id           TEXT,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (tenant_id, pipeline_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_message_slots_pipeline_seq
    ON message_slots(pipeline_id, tenant_id, seq);
-- 域12：message_segments（消息段——替换事件的冻结内容，多代切换/压缩原文存档）。
-- 段 = 消息引用的有序列表（members_blob_id 指向「成员 blob_id JSON 数组」的 blob，
-- 成员本体内容寻址单条一存，不双存全文）；base_seq/base_len 定位冻结区间。
-- visible_to 是非启用段的可消费者标签（空串 = 全可见）；段行按 id 幂等（重复投递
-- 不重复建行）。插入后移时 base_seq 与槽位 seq 同事务同步（见 apply_messages_ops_in_tx
-- 的 insert 分支）；pipeline 删除级联清段行；blobs 孤儿清扫保留段引用面。
CREATE TABLE IF NOT EXISTS message_segments (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    pipeline_id     TEXT NOT NULL,
    base_seq        INTEGER NOT NULL,
    base_len        INTEGER NOT NULL,
    members_blob_id TEXT NOT NULL,
    visible_to      TEXT NOT NULL DEFAULT '',
    preview         TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_message_segments_pipeline_seq
    ON message_segments(tenant_id, pipeline_id, base_seq);
-- 域11：pipeline_pending_inputs（pending 输入队列，ADR-2026-08-26 /
-- ADR-2026-11-17）：队列信封 + 一次派发请求（request 列 = DispatchRequest
-- JSON 单列）。旧 15 列强类型形态退役——业务词汇归 request 内两个不透明 map，
-- 队列面只留查询/去重所需的信封列。
-- 消息在入队→激活之间停留在此表：等待窗口内可修改/删除/清空，
-- 消费任务从表取请求重放执行（内容不被闭包捕获），重启后队列仍在（续跑）。
-- FIFO 序 = (created_at, id) 升序；消费瞬态 = 取出行并物理删除（无 status 列）。
-- pipeline_id 列是 request.pipeline_id 的索引投影（FIFO/管道过滤键），
-- 单一真值仍是 request。
CREATE TABLE IF NOT EXISTS pipeline_pending_inputs (
    id                TEXT PRIMARY KEY,
    pipeline_id       TEXT NOT NULL,
    tenant_id         TEXT NOT NULL,
    client_message_id TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    request           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_pipeline
    ON pipeline_pending_inputs(tenant_id, pipeline_id, created_at, id);
-- 消息幂等契约（ADR 2026-08-21）在队列层的索引：入队按
-- (tenant_id, pipeline_id, client_message_id) 查重，网络重试/前端重发同 cmid
-- 不产生第二条。非唯一索引（历史数据可能已有同 cmid 多条，唯一索引会使
-- 启动迁移失败；去重由 enqueue_pending_input 的查重逻辑保证）。
CREATE INDEX IF NOT EXISTS idx_pending_cmid
    ON pipeline_pending_inputs(tenant_id, pipeline_id, client_message_id);
-- consumed_refresh_jtis：refresh token 单次轮换消费账本（api auth D12）。
-- 跨进程重启持久判已消费（进程内记录重启即清，已消费 token 会复活）；
-- 行保留窗口 = refresh token 有效期上限 7 天（api auth REFRESH_TOKEN_TTL_SECS），
-- 消费写入时顺手清理窗口外旧行。
CREATE TABLE IF NOT EXISTS consumed_refresh_jtis (
    jti         TEXT PRIMARY KEY,
    consumed_at INTEGER NOT NULL
);
";

/// 完整 init DDL（TRACES/PIPELINE_STATE 在前——迁移重建后的新表形状下幂等跳过）。
pub(super) fn ddl_sql() -> String {
    format!("{TRACES_DDL}{PIPELINE_STATE_DDL}{DDL}")
}

/// 退役表迁移处置：存在且有行 → `ALTER TABLE ... RENAME TO <name>_retired_<yyyymmddHHMMSS>`
/// 留档保全并 error 留痕（数据保全优先于自动清理——退役即盲删会把存量数据随升级
/// 静默抹掉，人工确认无用后再清理 _retired_ 表）；不存在或空表 → DROP（db_admin
/// 表清单与后端读写一一对应的既有契约不变）。
pub(super) fn retire_table_preserve_rows(
    conn: &Connection,
    name: &str,
) -> Result<(), StorageError> {
    let exists: bool = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?1",
            [name],
            |row| row.get::<_, i64>(0),
        )
        .map(|n| n > 0)?;
    if !exists {
        return Ok(());
    }
    let rows: i64 = conn.query_row(&format!("SELECT COUNT(*) FROM {name}"), [], |r| r.get(0))?;
    if rows == 0 {
        conn.execute(&format!("DROP TABLE {name}"), [])?;
        return Ok(());
    }
    let ts = chrono::Utc::now().format("%Y%m%d%H%M%S");
    let renamed = format!("{name}_retired_{ts}");
    conn.execute(&format!("ALTER TABLE {name} RENAME TO {renamed}"), [])?;
    error!(
        table = name,
        rows = rows,
        renamed = %renamed,
        "退役表含数据：已改名保全，不自动删除；确认无用后可人工 DROP 该 _retired_ 表"
    );
    Ok(())
}

/// 零兼容：检测 message_slots 旧 schema（含内容列，如 content_preview）并整表撤位重建。
///
/// 任务 7 收敛后 slots 是纯索引表；旧库的宽表（含 role/content_preview/tool_calls_json
/// 等内容列）数据格式不再支持——旧宽表经 [`retire_table_preserve_rows`] 处置（有行改名
/// `_retired_<ts>` 留档、空表 DROP），再按现行 DDL 重建纯索引表。
pub(super) fn migrate_drop_legacy_message_slots(conn: &Connection) -> Result<(), StorageError> {
    // 行级读取失败必须显式留痕：filter_map(.ok()) 会把失败行静默丢掉，
    // 可能让残留旧列逃过检测、误判"无旧列"而跳过重建。
    let mut has_legacy_col = false;
    let mut stmt = conn.prepare("PRAGMA table_info(message_slots)")?;
    let rows = stmt.query_map([], |row| row.get::<_, String>(1))?;
    for r in rows {
        match r {
            Ok(col) => {
                if col == "content_preview" || col == "tool_calls_json" {
                    has_legacy_col = true;
                }
            }
            Err(e) => warn!(error = %e, "message_slots 列信息行读取失败，该行不计入旧 schema 检测"),
        }
    }
    if has_legacy_col {
        retire_table_preserve_rows(conn, "message_slots")?;
        warn!("message_slots 含旧内容列（零兼容），已按保全策略撤位并按现行 DDL 重建");
        conn.execute_batch(&ddl_sql())?;
    }
    Ok(())
}

/// 为旧库（无 must_change_password 列的 users 表）补加该列。幂等：列已存在时跳过。
pub(super) fn migrate_add_users_must_change_password(
    conn: &Connection,
) -> Result<(), StorageError> {
    let has = conn
        .prepare(
            "SELECT COUNT(*) FROM pragma_table_info('users') WHERE name='must_change_password'",
        )?
        .query_row([], |row| row.get::<_, i64>(0))?;
    if has == 0 {
        conn.execute(
            "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0",
            [],
        )?;
    }
    Ok(())
}

/// 为旧库（无 pipeline_config_id 列的 pipeline_pending_inputs 表）补加该列。幂等。
/// 存量行该列为 NULL = autonomous（显式指定才写值，缺省路径不变）。
/// 已收敛到 DispatchRequest 形状（`request` 列在）的表跳过——该字段已在 request 内。
pub(super) fn migrate_add_pending_pipeline_config_id(
    conn: &Connection,
) -> Result<(), StorageError> {
    if !table_has_column(conn, "pipeline_pending_inputs", "pipeline_config_id")?
        && !table_has_column(conn, "pipeline_pending_inputs", "request")?
    {
        conn.execute(
            "ALTER TABLE pipeline_pending_inputs ADD COLUMN pipeline_config_id TEXT",
            [],
        )?;
    }
    Ok(())
}

/// v1 → v2：pending 输入行收敛为「队列信封 + DispatchRequest」（ADR-2026-11-17）。
///
/// 旧 15 列强类型行 → `request` 单列 JSON。旧列多为 NOT NULL 无默认值，留着会让
/// 新写入违反约束，故整体重建：建新表 → 逐行回填 → 换名 → 重建索引（单事务）。
/// 幂等：`request` 列已存在 = 已迁移，直接返回（新库由 DDL 直接建新形状）。
///
/// 存量语义保全：legacy `agent_id` / `thinking_strength` 非空值并入
/// `state_overlay` 的 `agent.id` / `thinking_strength` 键——排队中的用户选择
/// 不因列收窄而丢失（二者本就是 overlay 通道的重复列，见 ADR）。
pub(super) fn migrate_pending_input_dispatch_request(
    conn: &Connection,
) -> Result<(), StorageError> {
    if !table_exists(conn, "pipeline_pending_inputs")?
        || table_has_column(conn, "pipeline_pending_inputs", "request")?
    {
        return Ok(());
    }
    // ① 读旧行（按现存列动态投影：历史库可能缺后加列）
    let rows: Vec<(String, String, String, String, String, String)> = {
        let mut stmt = conn.prepare("SELECT * FROM pipeline_pending_inputs")?;
        let columns: Vec<String> = stmt.column_names().iter().map(|c| c.to_string()).collect();
        let idx = |name: &str| columns.iter().position(|c| c == name);
        // legacy 列宽松读取，类型异常按缺失处理（旧列 NOT NULL，实际风险极低）
        let text = |row: &rusqlite::Row<'_>, name: &str| -> Option<String> {
            idx(name).and_then(|i| row.get::<_, Option<String>>(i).ok().flatten())
        };
        let json = |row: &rusqlite::Row<'_>, name: &str| -> Option<serde_json::Value> {
            text(row, name).and_then(|raw| serde_json::from_str::<serde_json::Value>(&raw).ok())
        };
        let collected = stmt
            .query_map([], |row| {
                let pipeline_id = text(row, "route_id")
                    .filter(|s| !s.is_empty())
                    .or_else(|| text(row, "pipeline_id"))
                    .unwrap_or_default();
                // 旧派对列 → overlay 键（非空且键缺席才落，不覆盖显式 overlay）
                let mut overlay = json(row, "state_overlay")
                    .filter(serde_json::Value::is_object)
                    .unwrap_or_else(|| serde_json::json!({}));
                if let Some(obj) = overlay.as_object_mut() {
                    if let Some(agent_id) = text(row, "agent_id").filter(|s| !s.is_empty()) {
                        obj.entry("agent.id".to_string())
                            .or_insert_with(|| serde_json::json!(agent_id));
                    }
                    if let Some(strength) = text(row, "thinking_strength").filter(|s| !s.is_empty())
                    {
                        obj.entry("thinking_strength".to_string())
                            .or_insert_with(|| serde_json::json!(strength));
                    }
                }
                let overlay = if overlay.as_object().is_some_and(|o| o.is_empty()) {
                    serde_json::Value::Null
                } else {
                    overlay
                };
                let source = text(row, "source")
                    .and_then(|raw| serde_json::from_str::<serde_json::Value>(&raw).ok())
                    .filter(|v| v.is_string())
                    .unwrap_or_else(|| serde_json::json!("user"));
                let client_message_id = text(row, "client_message_id").unwrap_or_default();
                let request = serde_json::json!({
                    "thread_id": text(row, "thread").unwrap_or_default(),
                    "user_id": text(row, "user_id").unwrap_or_default(),
                    "pipeline_id": pipeline_id,
                    "content": text(row, "content").unwrap_or_default(),
                    "client_message_id": client_message_id,
                    "source": source,
                    "execution_context": json(row, "execution_context")
                        .unwrap_or(serde_json::Value::Null),
                    "state_overlay": overlay,
                    "pipeline_config_id": text(row, "pipeline_config_id")
                        .filter(|s| !s.is_empty())
                        .map(serde_json::Value::String)
                        .unwrap_or(serde_json::Value::Null),
                });
                Ok((
                    text(row, "id").unwrap_or_default(),
                    pipeline_id,
                    text(row, "tenant_id").unwrap_or_default(),
                    client_message_id,
                    text(row, "created_at").unwrap_or_default(),
                    serde_json::to_string(&request).unwrap_or_else(|_| "{}".to_string()),
                ))
            })?
            .collect::<Result<Vec<_>, _>>()?;
        collected
    };
    // ② 重建（单事务；旧列无默认值 → 必须换表，不能保留物理列）
    let tx = conn.unchecked_transaction()?;
    tx.execute_batch("DROP TABLE IF EXISTS pipeline_pending_inputs_v2")?;
    tx.execute_batch(
        "CREATE TABLE pipeline_pending_inputs_v2 (\
         id TEXT PRIMARY KEY, pipeline_id TEXT NOT NULL, tenant_id TEXT NOT NULL, \
         client_message_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, \
         request TEXT NOT NULL)",
    )?;
    {
        let mut insert = tx.prepare(
            "INSERT INTO pipeline_pending_inputs_v2 \
             (id, pipeline_id, tenant_id, client_message_id, created_at, request) \
             VALUES (?1,?2,?3,?4,?5,?6)",
        )?;
        for row in &rows {
            insert.execute(rusqlite::params![row.0, row.1, row.2, row.3, row.4, row.5])?;
        }
    }
    tx.execute_batch("DROP TABLE pipeline_pending_inputs")?;
    tx.execute_batch("ALTER TABLE pipeline_pending_inputs_v2 RENAME TO pipeline_pending_inputs")?;
    tx.execute_batch(
        "CREATE INDEX IF NOT EXISTS idx_pending_pipeline \
         ON pipeline_pending_inputs(tenant_id, pipeline_id, created_at, id);\
         CREATE INDEX IF NOT EXISTS idx_pending_cmid \
         ON pipeline_pending_inputs(tenant_id, pipeline_id, client_message_id);",
    )?;
    tx.commit()?;
    info!(
        rows = rows.len(),
        "pipeline_pending_inputs 迁移为 DispatchRequest 行（旧列已收窄）"
    );
    Ok(())
}

/// 表存在性检查。
fn table_exists(conn: &Connection, name: &str) -> Result<bool, StorageError> {
    let exists: i64 = conn.query_row(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?1",
        [name],
        |row| row.get(0),
    )?;
    Ok(exists > 0)
}

/// 列存在性检查（表不存在 = false）。
fn table_has_column(conn: &Connection, table: &str, column: &str) -> Result<bool, StorageError> {
    if !table_exists(conn, table)? {
        return Ok(false);
    }
    let has: i64 = conn.query_row(
        &format!("SELECT COUNT(*) FROM pragma_table_info('{table}') WHERE name = ?1"),
        [column],
        |row| row.get(0),
    )?;
    Ok(has > 0)
}

/// ADR 2026-09-18（runs/branches 退役与 state 唯一真值）结构迁移，幂等：
///
/// ① pipeline_state 值域标量化：旧 `field_value`（JSON 壳）→ `value`/`value_kind`
///    标量列（剥壳转标量；解析失败按原始字符串标量留存，不静默丢行）；
/// ② traces → pipeline 级 op 流：`(run_id, branch_id, seq_in_branch)` →
///    `(pipeline_id, seq)`。pipeline 归属经 runs 表映射（runs 不存在的历史孤儿行
///    pipeline_id 置空串，不可被管道查询命中）；seq 按原行序（rowid）逐 run 重排；
/// ③ runs 簿记收割进 state 标量键（run_id/run_status/run_started_at/run_ended_at/
///    run_config_hash/suspend_request_id）——INSERT OR IGNORE，state 已有真值不覆盖
///    （state 是较新真值，runs 行只是启动期兜底）；
/// ④ runs/branches 表退役：按保全策略处置（有行改名 `_retired_<ts>` 留档、空表 DROP）。
///
/// runs 物理表的全部剩余引用都在本迁移内部（③ 的收割源 + ② 的换轨映射）——
/// 运行时读写零引用（ADR 2026-09-18）；等价 state 读法对旧库不成立（收割前
/// state 尚无簿记键，收割本体就是 state 簿记键的来源），故保留。物理表随本
/// 迁移退役（保全改名/DROP）；`migrate_retire_runs_branches` 函数本体待部署
/// 基线迁移完毕后整体退役（登记 ADR 2026-10-08-runs-retire-completion，本轮
/// 不动避免引入迁移）。
pub(super) fn migrate_retire_runs_branches(conn: &Connection) -> Result<(), StorageError> {
    // ① pipeline_state 值域标量化
    if table_has_column(conn, "pipeline_state", "field_value")? {
        conn.execute(
            "ALTER TABLE pipeline_state RENAME TO pipeline_state_pre_scalar",
            [],
        )?;
        conn.execute_batch(PIPELINE_STATE_DDL)?;
        let mut stmt = conn.prepare(
            "SELECT pipeline_id, field_key, field_value, tenant_id, updated_at \
             FROM pipeline_state_pre_scalar",
        )?;
        let rows = stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
            ))
        })?;
        let rows = rows.collect::<Result<Vec<_>, _>>()?;
        drop(stmt);
        for (pipeline_id, key, field_value, tenant_id, updated_at) in rows {
            let parsed = serde_json::from_str::<serde_json::Value>(&field_value)
                .unwrap_or(serde_json::Value::String(field_value.clone()));
            let (value, kind) = super::encode_state_value(&parsed);
            conn.execute(
                "INSERT INTO pipeline_state (pipeline_id, field_key, value, value_kind, tenant_id, updated_at) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                rusqlite::params![pipeline_id, key, value, kind, tenant_id, updated_at],
            )?;
        }
        conn.execute("DROP TABLE pipeline_state_pre_scalar", [])?;
        info!("pipeline_state 已标量化（field_value JSON 壳 → value/value_kind）");
    }

    // ② traces → pipeline 级 op 流
    if table_has_column(conn, "traces", "run_id")? {
        conn.execute("ALTER TABLE traces RENAME TO traces_pre_oplog", [])?;
        conn.execute_batch(TRACES_DDL)?;
        let has_runs = table_exists(conn, "runs")?;
        let mut stmt = conn.prepare(
            "SELECT trace_id, run_id, plugin_id, patch_type, patch_data, tenant_id, created_at \
             FROM traces_pre_oplog ORDER BY rowid",
        )?;
        let rows = stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, String>(5)?,
                row.get::<_, String>(6)?,
            ))
        })?;
        let rows = rows.collect::<Result<Vec<_>, _>>()?;
        drop(stmt);
        let mut pipeline_cache: std::collections::HashMap<String, String> = Default::default();
        let mut seq_counters: std::collections::HashMap<String, i64> = Default::default();
        for (trace_id, run_id, plugin_id, patch_type, patch_data, tenant_id, created_at) in rows {
            let pipeline_id = if has_runs {
                match pipeline_cache.entry(run_id.clone()) {
                    std::collections::hash_map::Entry::Occupied(e) => e.get().clone(),
                    std::collections::hash_map::Entry::Vacant(e) => {
                        let mapped: String = conn
                            .query_row(
                                "SELECT pipeline_id FROM runs WHERE run_id = ?1",
                                [&run_id],
                                |row| row.get::<_, Option<String>>(0),
                            )
                            .map(|v| v.unwrap_or_default())
                            .unwrap_or_else(|e| {
                                warn!(run_id = %run_id, error = %e, "traces 换轨映射：runs 行查询失败，pipeline_id 置空");
                                String::new()
                            });
                        e.insert(mapped.clone());
                        mapped
                    }
                }
            } else {
                String::new()
            };
            let seq = seq_counters.entry(run_id.clone()).or_insert(0);
            let this_seq = *seq;
            *seq += 1;
            conn.execute(
                "INSERT INTO traces (trace_id, pipeline_id, seq, plugin_id, patch_type, patch_data, tenant_id, created_at) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                rusqlite::params![
                    trace_id,
                    pipeline_id,
                    this_seq,
                    plugin_id,
                    patch_type,
                    patch_data,
                    tenant_id,
                    created_at
                ],
            )?;
        }
        conn.execute("DROP TABLE traces_pre_oplog", [])?;
        info!("traces 已换轨为 pipeline 级 op 流（run_id/branch_id → pipeline_id + seq 重排）");
    }

    // ③+④ runs 簿记收割 → state 标量键；runs/branches 退役
    if table_exists(conn, "runs")? {
        let mut stmt = conn.prepare(
            "SELECT run_id, config_hash, status, tenant_id, pipeline_id, created_at, ended_at, metadata \
             FROM runs WHERE pipeline_id IS NOT NULL AND pipeline_id != ''",
        )?;
        let rows = stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, String>(5)?,
                row.get::<_, Option<String>>(6)?,
                row.get::<_, Option<String>>(7)?,
            ))
        })?;
        let rows = rows.collect::<Result<Vec<_>, _>>()?;
        drop(stmt);
        for (run_id, config_hash, status, tenant_id, pipeline_id, created_at, ended_at, metadata) in
            rows
        {
            // INSERT OR IGNORE：state 已有真值不覆盖（state 是较新真值）
            let put = |key: &str, value: &str| -> Result<(), StorageError> {
                conn.execute(
                    "INSERT OR IGNORE INTO pipeline_state (pipeline_id, field_key, value, value_kind, tenant_id, updated_at) \
                     VALUES (?1, ?2, ?3, 'str', ?4, ?5)",
                    rusqlite::params![pipeline_id, key, value, tenant_id, created_at],
                )?;
                Ok(())
            };
            put("run_id", &run_id)?;
            put("run_status", &status)?;
            put("run_started_at", &created_at)?;
            put("run_config_hash", &config_hash)?;
            if let Some(ended) = &ended_at {
                put("run_ended_at", ended)?;
            }
            if let Some(meta_str) = &metadata {
                if let Ok(meta) = serde_json::from_str::<serde_json::Value>(meta_str) {
                    if let Some(req) = meta
                        .get("pending_interaction_request_id")
                        .and_then(|v| v.as_str())
                    {
                        put("suspend_request_id", req)?;
                    }
                }
            }
        }
        retire_table_preserve_rows(conn, "runs")?;
    }
    retire_table_preserve_rows(conn, "branches")?;
    Ok(())
}

/// 仅在列缺失时执行 `ALTER TABLE ... ADD COLUMN`，幂等。blob 表不加（内容寻址，靠上游归属）。
/// 表不存在时跳过（全新库由 DDL 直接建新形状；本迁移只服务旧库存量表）。
pub(super) fn migrate_add_tenant_id(conn: &Connection) -> Result<(), StorageError> {
    for table in ["traces"] {
        if !table_exists(conn, table)? {
            continue;
        }
        // 行级读取失败必须显式留痕（不吞）：判断结果只取可读行，失败时保守跳过
        // ALTER——列存在性不明时不做可能自撞"duplicate column"的补列。
        let mut has_col = false;
        let mut stmt = conn.prepare(&format!("PRAGMA table_info({table})"))?;
        let rows = stmt.query_map([], |row| row.get::<_, String>(1))?;
        for r in rows {
            match r {
                Ok(col) => {
                    if col == "tenant_id" {
                        has_col = true;
                    }
                }
                Err(e) => {
                    warn!(table = table, error = %e, "列信息行读取失败，该行不计入 tenant_id 检测")
                }
            }
        }
        if !has_col {
            conn.execute(
                &format!(
                    "ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'"
                ),
                [],
            )?;
        }
    }
    Ok(())
}

//! db-read 契约测试——语义移植自对照基线 commit 71dca0fff 的 24 例 Python
//! 契约测试（tests/plugins/shared/test_kernel_db.py，spec §9 清单）：
//! 租户隔离双向 / 收窄不可绕 / 保留名拒 / 未注册面拒 / 常量冒充拒 /
//! 写与 PRAGMA 与跨面 join DENY / global 三态 / role 误传拒 / ro 不建库 /
//! 裸聚合形状纪律文档锚。Rust 侧身份不入参（从身份链构造 DbReadIdentity）。

use super::*;
use serde_json::json;

/// 与生产 config/kernel/read_faces.yaml 同形的四面 YAML（装载路径真跑）。
const FACES_YAML: &str = "\
faces:
  - id: pipeline_state
    table: pipeline_state
    scope: tenant
    tenant_column: tenant_id
  - id: pipeline_state_admin
    table: pipeline_state
    scope: global
    tenant_column: tenant_id
  - id: sessions
    table: sessions
    scope: tenant
    tenant_column: tenant_id
  - id: traces
    table: traces
    scope: tenant
    tenant_column: tenant_id
";

/// 三表最小内核库（sessions/pipeline_state/traces，双租户行）。
///
/// 表形态与内核落库同形（三表均带 tenant_id 列）；双租户行是租户隔离断言的
/// 可断基础，traces 用量行 tenant-a=150 / tenant-b=10。
fn contract_db() -> (tempfile::TempDir, std::path::PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("agentos_kernel.db");
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    conn.execute_batch(
        "CREATE TABLE sessions (thread_id TEXT PRIMARY KEY, title TEXT,
            tenant_id TEXT, updated_at TEXT, last_active_at TEXT);
         CREATE TABLE pipeline_state (pipeline_id TEXT, field_key TEXT,
            value TEXT, value_kind TEXT, tenant_id TEXT, updated_at TEXT);
         CREATE TABLE traces (trace_id TEXT PRIMARY KEY, pipeline_id TEXT,
            plugin_id TEXT, patch_data TEXT, tenant_id TEXT, created_at TEXT);",
    )
    .unwrap();
    conn.execute(
        "INSERT INTO sessions VALUES\
         ('th-a', '甲租户会话', 'tenant-a', '2026-10-01T00:00:00Z', NULL)",
        [],
    )
    .unwrap();
    conn.execute(
        "INSERT INTO sessions VALUES\
         ('th-b', '乙租户会话', 'tenant-b', '2026-10-02T00:00:00Z', NULL)",
        [],
    )
    .unwrap();
    for (pid, key, val, tenant) in [
        ("p-a", "run_status", "completed", "tenant-a"),
        ("p-b", "run_status", "completed", "tenant-b"),
        ("p-a", "run_id", "run-1", "tenant-a"),
        ("p-a", "agent.id", "ag-1", "tenant-a"),
        ("p-a", "agent_level", "main", "tenant-a"),
        ("p-a", "context.agent_name", "alpha", "tenant-a"),
        ("p-a", "session_id", "th-a", "tenant-a"),
    ] {
        conn.execute(
            "INSERT INTO pipeline_state (pipeline_id, field_key, value, value_kind,\
             tenant_id, updated_at)\
             VALUES (?1, ?2, ?3, 'str', ?4, '2026-10-01T00:00:00Z')",
            rusqlite::params![pid, key, val, tenant],
        )
        .unwrap();
    }
    for (tid, pid, tokens, tenant, created) in [
        ("t-a", "p-a", 150, "tenant-a", "2026-01-15T08:00:00Z"),
        ("t-b", "p-b", 10, "tenant-b", "2026-01-15T09:00:00Z"),
    ] {
        conn.execute(
            "INSERT INTO traces (trace_id, pipeline_id, plugin_id, patch_data,\
             tenant_id, created_at)\
             VALUES (?1, ?2, 'core', ?3, ?4, ?5)",
            rusqlite::params![
                tid,
                pid,
                format!(
                    "{{\"llm_usage\": {{\"input_tokens\": 100, \"output_tokens\": 50,
                      \"total_tokens\": {tokens}}}, \"llm_model\": \"m\"}}"
                ),
                tenant,
                created,
            ],
        )
        .unwrap();
    }
    conn.close().unwrap();
    (dir, db_path)
}

/// 注册表夹具（真跑 from_yaml 装载路径）。
fn registry(db_path: &std::path::Path) -> ReadFacesRegistry {
    ReadFacesRegistry::from_yaml(FACES_YAML, db_path.to_str().unwrap()).unwrap()
}

fn tid(tenant: &str) -> DbReadIdentity {
    DbReadIdentity {
        tenant_id: tenant.to_string(),
        role: "tenant".to_string(),
    }
}

fn admin() -> DbReadIdentity {
    DbReadIdentity {
        tenant_id: "default".to_string(),
        role: "admin".to_string(),
    }
}

/// 断言错误码与状态（各用例按 spec §5 同码）。
fn assert_err(result: Result<DbReadResult, DbReadError>, status: u16, code: &str) -> DbReadError {
    let e = result.expect_err("应当拒绝");
    assert_eq!(e.status, status, "status: {e:?}");
    assert_eq!(e.code, code, "code: {e:?}");
    e
}

// ── 面定义了就能用：自由 SELECT（聚合/裁剪/JSON 函数） ────────────────

/// [基线 test_free_select_returns_rows]
#[test]
fn free_select_returns_rows() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    let result = query(
        &reg,
        "traces",
        "SELECT tenant_id, json_extract(patch_data, '$.llm_usage.total_tokens') AS tokens, \
         created_at FROM traces WHERE json_extract(patch_data, '$.llm_usage') IS NOT NULL \
         AND substr(created_at, 1, 7) = '2026-01'",
        &Value::Null,
        &tid("tenant-a"),
        None,
    )
    .unwrap();
    assert_eq!(
        result.rows,
        vec![vec![
            json!("tenant-a"),
            json!(150),
            json!("2026-01-15T08:00:00Z")
        ]]
    );
}

/// [基线 test_tenant_isolation_bidirectional] 三面各验：A 查不到 B，双向隔离。
#[test]
fn tenant_isolation_bidirectional() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    let usage_sql = "SELECT tenant_id, COALESCE(SUM(json_extract(patch_data, \
        '$.llm_usage.total_tokens')), 0) FROM traces \
        WHERE json_extract(patch_data, '$.llm_usage') IS NOT NULL GROUP BY tenant_id";
    let a = query(
        &reg,
        "traces",
        usage_sql,
        &Value::Null,
        &tid("tenant-a"),
        None,
    )
    .unwrap();
    let b = query(
        &reg,
        "traces",
        usage_sql,
        &Value::Null,
        &tid("tenant-b"),
        None,
    )
    .unwrap();
    assert_eq!(a.rows, vec![vec![json!("tenant-a"), json!(150)]]);
    assert_eq!(b.rows, vec![vec![json!("tenant-b"), json!(10)]]);

    let pipes = query(
        &reg,
        "pipeline_state",
        "SELECT tenant_id, pipeline_id FROM pipeline_state WHERE field_key = 'run_status'",
        &Value::Null,
        &tid("tenant-a"),
        None,
    )
    .unwrap();
    assert_eq!(pipes.rows, vec![vec![json!("tenant-a"), json!("p-a")]]);

    let sessions = query(
        &reg,
        "sessions",
        "SELECT tenant_id, thread_id FROM sessions",
        &Value::Null,
        &tid("tenant-a"),
        None,
    )
    .unwrap();
    assert_eq!(sessions.rows, vec![vec![json!("tenant-a"), json!("th-a")]]);
}

/// [基线 test_inner_predicate_cannot_bypass_wrap] 内层谓词绕不过契约包装：
/// 误/恶意指派他租户值只会过度限制；本租户谓词 = 结果完整。
#[test]
fn inner_predicate_cannot_bypass_wrap() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    let sql = "SELECT tenant_id, pipeline_id FROM pipeline_state WHERE tenant_id = :tenant";
    let adversarial = query(
        &reg,
        "pipeline_state",
        sql,
        &json!({ "tenant": "tenant-b" }),
        &tid("tenant-a"),
        None,
    )
    .unwrap();
    assert!(adversarial.rows.is_empty(), "他租户行必须被契约包装滤除");
    let normal = query(
        &reg,
        "pipeline_state",
        sql,
        &json!({ "tenant": "tenant-a" }),
        &tid("tenant-a"),
        None,
    )
    .unwrap();
    // 基线断言形态是 set（同形行去重）：本租户谓词 = 只含本租户行（p-a 的
    // 6 个标量键行全部命中且无他租户行）。
    assert_eq!(normal.rows.len(), 6, "p-a 全部 6 行命中: {:?}", normal.rows);
    assert!(
        normal
            .rows
            .iter()
            .all(|r| r == &vec![json!("tenant-a"), json!("p-a")]),
        "只含 (tenant-a, p-a): {:?}",
        normal.rows
    );
}

// ── 权限分档与身份 ──────────────────────────────────────────────────

/// [基线 test_tenant_face_requires_tenant_id] 缺租户身份 → 403（fail-closed
/// 不退化全租户）；tenant 档传 tenant 参数 → 403（收窄值不可指派）。
#[test]
fn tenant_face_requires_tenant_identity() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    let anonymous = DbReadIdentity {
        tenant_id: String::new(),
        role: "tenant".to_string(),
    };
    assert_err(
        query(
            &reg,
            "traces",
            "SELECT tenant_id FROM traces",
            &Value::Null,
            &anonymous,
            None,
        ),
        403,
        ERR_ROLE_DENIED,
    );
    assert_err(
        query(
            &reg,
            "traces",
            "SELECT tenant_id FROM traces",
            &Value::Null,
            &tid("tenant-a"),
            Some("tenant-b"),
        ),
        403,
        ERR_ROLE_DENIED,
    );
}

/// [基线 test_tenant_face_rejects_other_roles] role 误传 → 拒。Rust 侧身份
/// 从内核复权链取，调用方传 role/tenant_id 形参在入口层即拒（403）。
#[test]
fn identity_params_rejected_at_entry() {
    for key in ["role", "tenant_id"] {
        let err = DbReadCapabilityHandler::reject_identity_params(&json!({ key: "admin" }))
            .expect_err("身份形参必须拒");
        assert_eq!(err.status, 403);
        assert_eq!(err.code, ERR_ROLE_DENIED);
    }
    // 面外普通参数不受影响。
    DbReadCapabilityHandler::reject_identity_params(&json!({ "face": "traces" })).unwrap();
}

/// [基线 test_global_face_requires_admin] 全局面无 admin → 403。
#[test]
fn global_face_requires_admin() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    let viewer = DbReadIdentity {
        tenant_id: "default".to_string(),
        role: "viewer".to_string(),
    };
    assert_err(
        query(
            &reg,
            "pipeline_state_admin",
            "SELECT tenant_id, pipeline_id FROM pipeline_state WHERE field_key = 'run_status'",
            &Value::Null,
            &viewer,
            None,
        ),
        403,
        ERR_ROLE_DENIED,
    );
}

/// [基线 test_global_admin_full_then_narrowed] global 三态：无 admin 拒 /
/// admin 全局 / admin+tenant 收窄单租户。
#[test]
fn global_admin_full_then_narrowed() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    let sql = "SELECT tenant_id, pipeline_id FROM pipeline_state \
               WHERE field_key = 'run_status' ORDER BY pipeline_id";
    let full = query(
        &reg,
        "pipeline_state_admin",
        sql,
        &Value::Null,
        &admin(),
        None,
    )
    .unwrap();
    assert_eq!(
        full.rows,
        vec![
            vec![json!("tenant-a"), json!("p-a")],
            vec![json!("tenant-b"), json!("p-b")],
        ]
    );
    let narrowed = query(
        &reg,
        "pipeline_state_admin",
        sql,
        &Value::Null,
        &admin(),
        Some("tenant-b"),
    )
    .unwrap();
    assert_eq!(narrowed.rows, vec![vec![json!("tenant-b"), json!("p-b")]]);
}

/// [基线 test_global_face_select_aggregate_and_self_join] 全局面自由 SELECT：
/// 聚合 + 同表子查询（收割透视形状）可用。
#[test]
fn global_face_select_aggregate_and_self_join() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    let rows = query(
        &reg,
        "pipeline_state_admin",
        "SELECT s.tenant_id, COUNT(*) FROM pipeline_state s WHERE s.pipeline_id IN \
         (SELECT pipeline_id FROM pipeline_state WHERE field_key = 'run_id') \
         GROUP BY s.tenant_id ORDER BY s.tenant_id",
        &Value::Null,
        &admin(),
        None,
    )
    .unwrap();
    assert_eq!(rows.rows, vec![vec![json!("tenant-a"), json!(6)]]);
}

/// [基线 test_positional_params_allowed_without_narrowing] 全局面不带 tenant：
/// 调用方可自选位置参数（无契约注入占位共存问题）。
#[test]
fn positional_params_allowed_without_narrowing() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    let rows = query(
        &reg,
        "pipeline_state_admin",
        "SELECT value FROM pipeline_state WHERE pipeline_id = ? AND field_key = ?",
        &json!(["p-a", "run_id"]),
        &admin(),
        None,
    )
    .unwrap();
    assert_eq!(rows.rows, vec![vec![json!("run-1")]]);
}

// ── 语句级与面限制 ──────────────────────────────────────────────────

/// [基线 test_unregistered_face_rejected] 面外查询一律拒绝（物理表名未注册
/// 面/近似名/空名），404 同码。
#[test]
fn unregistered_face_rejected() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    for bad in [
        "sqlite_master",
        "pipeline_state_adminn",
        "traces_global",
        "",
    ] {
        let e = assert_err(
            query(&reg, bad, "SELECT 1", &Value::Null, &tid("tenant-a"), None),
            404,
            ERR_FACE_NOT_FOUND,
        );
        assert!(e.message.contains("未登记数据面"), "{e:?}");
    }
}

/// [基线 test_reserved_tenant_param_rejected] 参数占用保留名 __tenant__ → 422。
#[test]
fn reserved_tenant_param_rejected() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    let e = assert_err(
        query(
            &reg,
            "traces",
            "SELECT tenant_id FROM traces WHERE created_at = :__tenant__",
            &json!({ "__tenant__": "tenant-b" }),
            &tid("tenant-a"),
            None,
        ),
        422,
        ERR_RESERVED_PARAM,
    );
    assert!(e.message.contains(TENANT_PARAM), "{e:?}");
}

/// [基线 test_wrapped_query_requires_named_params] 收窄查询只接受命名参数。
#[test]
fn wrapped_query_requires_named_params() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    assert_err(
        query(
            &reg,
            "traces",
            "SELECT tenant_id FROM traces LIMIT ?",
            &json!([10]),
            &tid("tenant-a"),
            None,
        ),
        422,
        ERR_SQL_REJECTED,
    );
}

/// [基线 test_global_narrowed_requires_named_params] 全局面带 tenant 收窄时
/// 同样只接受命名参数。
#[test]
fn global_narrowed_requires_named_params() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    assert_err(
        query(
            &reg,
            "pipeline_state_admin",
            "SELECT tenant_id FROM pipeline_state LIMIT ?",
            &json!([10]),
            &admin(),
            Some("tenant-a"),
        ),
        422,
        ERR_SQL_REJECTED,
    );
}

/// [基线 test_must_project_real_tenant_column] 收窄查询未投影真实租户列：
/// 缺失列 → 422 专码；常量改名冒充 → 422 专码（authorizer READ 列记录校验）。
#[test]
fn must_project_real_tenant_column() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    let e = assert_err(
        query(
            &reg,
            "traces",
            "SELECT plugin_id FROM traces",
            &Value::Null,
            &tid("tenant-a"),
            None,
        ),
        422,
        ERR_TENANT_COLUMN_REQUIRED,
    );
    assert!(e.message.contains("tenant_id"), "{e:?}");
    // 常量改名冒充租户列：包装谓词恒真即全租户泄露——契约层按 READ 列记录拒绝。
    assert_err(
        query(
            &reg,
            "traces",
            "SELECT 'tenant-a' AS tenant_id, plugin_id FROM traces",
            &Value::Null,
            &tid("tenant-a"),
            None,
        ),
        422,
        ERR_TENANT_COLUMN_REQUIRED,
    );
}

/// [基线 test_only_single_select_statement] 非 SELECT 文本与多语句拒绝。
#[test]
fn only_single_select_statement() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    assert_err(
        query(
            &reg,
            "traces",
            "INSERT INTO traces VALUES ('x', '', '', '{}', 'tenant-a', '')",
            &Value::Null,
            &tid("tenant-a"),
            None,
        ),
        422,
        ERR_SQL_REJECTED,
    );
    assert_err(
        query(
            &reg,
            "traces",
            "SELECT tenant_id FROM traces; SELECT tenant_id FROM sessions",
            &Value::Null,
            &tid("tenant-a"),
            None,
        ),
        422,
        ERR_SQL_REJECTED,
    );
}

/// [基线 test_writes_rejected] 写语句文本（INSERT/UPDATE/DELETE/ATTACH/CTE
/// 开头）一律拒绝，不触库。
#[test]
fn writes_rejected() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    for write_sql in [
        "INSERT INTO traces VALUES ('x', '', '', '{}', 'tenant-a', '')",
        "UPDATE traces SET tenant_id = 'tenant-b'",
        "DELETE FROM traces",
        "ATTACH DATABASE 'x.db' AS x",
        "WITH dropped AS (SELECT 1) SELECT * FROM dropped",
    ] {
        assert_err(
            query(
                &reg,
                "traces",
                write_sql,
                &Value::Null,
                &tid("tenant-a"),
                None,
            ),
            422,
            ERR_SQL_REJECTED,
        );
    }
}

/// [基线 test_pragma_and_cross_face_read_rejected] PRAGMA（语句级文本拒绝）
/// 与跨面读他表（authorizer DENY，报文注明被拒对象）均不可达。
#[test]
fn pragma_and_cross_face_read_rejected() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    assert_err(
        query(
            &reg,
            "traces",
            "PRAGMA table_info(traces)",
            &Value::Null,
            &tid("tenant-a"),
            None,
        ),
        422,
        ERR_SQL_REJECTED,
    );
    let e = assert_err(
        query(
            &reg,
            "traces",
            "SELECT t.tenant_id, s.thread_id FROM traces t \
             JOIN sessions s ON s.thread_id = t.pipeline_id",
            &Value::Null,
            &tid("tenant-a"),
            None,
        ),
        422,
        ERR_SQL_REJECTED,
    );
    assert!(
        e.message.contains("DENY") && e.message.to_lowercase().contains("sessions"),
        "报文应注明 DENY 对象: {e:?}"
    );
}

/// [基线 test_ro_mode_never_creates_missing_db] 只读性：库缺失 → 503 且不
/// 创建库文件（读写连接会静默建空库的副作用被只读打开关死）。
#[test]
fn ro_mode_never_creates_missing_db() {
    let dir = tempfile::tempdir().unwrap();
    let absent = dir.path().join("nope.db");
    let reg = ReadFacesRegistry::from_yaml(FACES_YAML, absent.to_str().unwrap()).unwrap();
    let e = assert_err(
        query(
            &reg,
            "traces",
            "SELECT tenant_id FROM traces",
            &Value::Null,
            &tid("tenant-a"),
            None,
        ),
        503,
        ERR_STORAGE,
    );
    assert!(e.message.contains("只读打开失败"), "{e:?}");
    assert!(!absent.exists(), "只读连接不得创建库文件");
}

/// [基线 test_approval_anchor_shape_on_tenant_face] 迁移等价锚：approval
/// run_id 反查形状（租户内命中，他租户不可达）。
#[test]
fn approval_anchor_shape_on_tenant_face() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    let sql = "SELECT pipeline_id, tenant_id FROM pipeline_state \
               WHERE field_key = 'run_id' AND value = :run_id";
    let a = query(
        &reg,
        "pipeline_state",
        sql,
        &json!({ "run_id": "run-1" }),
        &tid("tenant-a"),
        None,
    )
    .unwrap();
    assert_eq!(a.rows, vec![vec![json!("p-a"), json!("tenant-a")]]);
    let b = query(
        &reg,
        "pipeline_state",
        sql,
        &json!({ "run_id": "run-1" }),
        &tid("tenant-b"),
        None,
    )
    .unwrap();
    assert!(b.rows.is_empty());
}

/// 裸聚合形状纪律文档锚（spec §3.4 诚实降级）：机制层不做 SQL 形状分析——
/// 无 GROUP BY 的裸聚合把全租户聚成一行，租户列成任意值，包装无法正确收窄；
/// 调用方纪律 = 聚合必须 `GROUP BY <租户列>` 后交包装收窄。本测试锁定：
/// 纪律形态结果正确；机制对其余形状不报错也不修复（global 面裸聚合 = 全租户
/// 合计，属 admin 权限档的既定语义）。
#[test]
fn bare_aggregate_discipline_anchor() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    // 纪律形态：GROUP BY tenant_id 后包装收窄 → 本租户口径正确。
    let disciplined = query(
        &reg,
        "traces",
        "SELECT tenant_id, SUM(json_extract(patch_data, '$.llm_usage.total_tokens')) \
         FROM traces GROUP BY tenant_id",
        &Value::Null,
        &tid("tenant-a"),
        None,
    )
    .unwrap();
    assert_eq!(disciplined.rows, vec![vec![json!("tenant-a"), json!(150)]]);
    // 机制不分析形状：裸聚合不报错（包装按冒充/任意值过滤或整行返回），但
    // 全局面裸聚合 = 跨租户合计（admin 档既定语义），文档锚定诚实边界。
    let bare = query(
        &reg,
        "pipeline_state_admin",
        "SELECT COUNT(*) FROM pipeline_state",
        &Value::Null,
        &admin(),
        None,
    )
    .unwrap();
    assert_eq!(bare.rows, vec![vec![json!(7)]], "全局面裸聚合=全租户行数");
}

// ── 注册表装载（fail-closed） ───────────────────────────────────────

/// 装载期校验：scope 词表 / 面 id 重复 / 标识符白名单，任一违反即 Err。
#[test]
fn loader_rejects_invalid_definitions() {
    let bad_scope = "\
faces:
  - id: t1
    table: traces
    scope: readonly
    tenant_column: tenant_id
";
    assert!(ReadFacesRegistry::from_yaml(bad_scope, "x.db").is_err());
    let dup = "\
faces:
  - id: same
    table: traces
    scope: tenant
    tenant_column: tenant_id
  - id: same
    table: sessions
    scope: tenant
    tenant_column: tenant_id
";
    let err = ReadFacesRegistry::from_yaml(dup, "x.db").unwrap_err();
    assert!(err.contains("重复"), "{err}");
    let bad_ident = "\
faces:
  - id: t1
    table: traces; DROP TABLE users
    scope: tenant
    tenant_column: tenant_id
";
    assert!(ReadFacesRegistry::from_yaml(bad_ident, "x.db").is_err());
    assert!(
        ReadFacesRegistry::from_yaml("faces: []", "x.db").is_ok(),
        "空注册表合法"
    );
}

/// 文件缺失 = 未配置（空注册表不拒绝启动，全部查询按面未注册拒绝——对齐
/// storage.yaml 缺失宽松先例）；文件损坏 = fail-closed。
#[test]
fn load_missing_file_is_empty_registry_and_corrupt_fails() {
    let dir = tempfile::tempdir().unwrap();
    let reg = ReadFacesRegistry::load(dir.path(), "k.db").unwrap();
    assert!(reg.list().is_empty(), "缺文件 = 空注册表");
    assert_eq!(reg.db_path(), "k.db");
    std::fs::create_dir_all(dir.path().join("kernel")).unwrap();
    std::fs::write(
        dir.path().join("kernel").join("read_faces.yaml"),
        "!!!not yaml{{",
    )
    .unwrap();
    assert!(
        ReadFacesRegistry::load(dir.path(), "k.db").is_err(),
        "坏 YAML 拒绝启动"
    );
}

/// 注册表快照（faces 自省面）：按 id 排序、四字段齐全。
#[test]
fn registry_list_snapshot_is_sorted() {
    let (_dir, db) = contract_db();
    let reg = registry(&db);
    let ids: Vec<&str> = reg.list().iter().map(|f| f.id.as_str()).collect();
    assert_eq!(
        ids,
        [
            "pipeline_state",
            "pipeline_state_admin",
            "sessions",
            "traces"
        ]
    );
    let admin_face = reg.face("pipeline_state_admin").unwrap();
    assert_eq!(admin_face.scope, FaceScope::Global);
    assert_eq!(admin_face.tenant_column, "tenant_id");
}

// ── capability 信封端到端（身份链真跑：token → 复权 → 执法） ──────────

/// 内置 admin token（与 api 登录签发同格式；无 store 时 token 校验走内置
/// admin 回退——db-admin capability 测试同款夹具）。
fn admin_bearer() -> String {
    let admin = agentos_http::auth::default_users()
        .into_iter()
        .next()
        .unwrap();
    format!(
        "Bearer {}",
        agentos_http::auth::encode_token(agentos_http::auth::TokenType::Access, &admin, 3600)
    )
}

fn handler(reg: Arc<ReadFacesRegistry>) -> DbReadCapabilityHandler {
    DbReadCapabilityHandler::new(None, reg)
}

/// query 端到端：_authorization 原头 → 内核复权 → 执法 → {status, body} 信封。
#[tokio::test]
async fn capability_query_roundtrip_with_admin_envelope() {
    let (_dir, db) = contract_db();
    let h = handler(Arc::new(registry(&db)));
    let envelope = h
        .call(
            "query",
            json!({
                "_authorization": admin_bearer(),
                "face": "pipeline_state_admin",
                "sql": "SELECT tenant_id, pipeline_id FROM pipeline_state \
                        WHERE field_key = 'run_status' ORDER BY pipeline_id",
            }),
        )
        .await
        .unwrap();
    assert_eq!(envelope["status"], 200, "{envelope}");
    assert_eq!(
        envelope["body"]["rows"],
        json!([["tenant-a", "p-a"], ["tenant-b", "p-b"]])
    );
}

/// 无凭证 → 401 信封（DB_READ_NOT_AUTHENTICATED）；业务拒绝同码透出
/// （face 未注册 → 404 DB_READ_FACE_NOT_FOUND）。
#[tokio::test]
async fn capability_envelopes_auth_and_contract_errors() {
    let (_dir, db) = contract_db();
    let h = handler(Arc::new(registry(&db)));
    let envelope = h
        .call("query", json!({ "face": "traces", "sql": "SELECT 1" }))
        .await
        .unwrap();
    assert_eq!(envelope["status"], 401, "无凭证应 401: {envelope}");
    assert_eq!(envelope["error"]["code"], ERR_NOT_AUTHENTICATED);

    let envelope = h
        .call(
            "query",
            json!({
                "_authorization": admin_bearer(),
                "face": "sqlite_master",
                "sql": "SELECT 1",
            }),
        )
        .await
        .unwrap();
    assert_eq!(envelope["status"], 404, "{envelope}");
    assert_eq!(envelope["error"]["code"], ERR_FACE_NOT_FOUND);
}

/// 身份形参（role/tenant_id）与 tenant 档传 tenant 参数 → 403 信封（参数即
/// 权限，插件不可伪造身份）。
#[tokio::test]
async fn capability_rejects_identity_spoofing_params() {
    let (_dir, db) = contract_db();
    let h = handler(Arc::new(registry(&db)));
    let base = json!({
        "_authorization": admin_bearer(),
        "face": "traces",
        "sql": "SELECT tenant_id FROM traces",
    });
    for key in ["role", "tenant_id", "tenant"] {
        let mut params = base.clone();
        params[key] = json!("tenant-a");
        let envelope = h.call("query", params).await.unwrap();
        assert_eq!(envelope["status"], 403, "{key} 形参应拒: {envelope}");
        assert_eq!(envelope["error"]["code"], ERR_ROLE_DENIED);
    }
}

/// faces 自省面：返回当前注册表（需有效身份）。
#[tokio::test]
async fn capability_faces_lists_registry() {
    let (_dir, db) = contract_db();
    let h = handler(Arc::new(registry(&db)));
    let envelope = h
        .call("faces", json!({ "_authorization": admin_bearer() }))
        .await
        .unwrap();
    assert_eq!(envelope["status"], 200, "{envelope}");
    let faces = envelope["body"]["faces"].as_array().unwrap();
    assert_eq!(faces.len(), 4);
    assert_eq!(faces[0]["id"], "pipeline_state");
    assert_eq!(faces[0]["scope"], "tenant");
}

/// 未知 method → McpError（列出已知清单）；缺 face/sql 参数 → 422 信封。
#[tokio::test]
async fn capability_unknown_method_and_missing_params() {
    let (_dir, db) = contract_db();
    let h = handler(Arc::new(registry(&db)));
    let err = h.call("execute", json!({})).await.unwrap_err();
    assert!(format!("{err}").contains("not implemented"));
    let envelope = h
        .call("query", json!({ "_authorization": admin_bearer() }))
        .await
        .unwrap();
    assert_eq!(envelope["status"], 422, "{envelope}");
}

// ── HTTP 面共享路径（db_read_http_inner 直测，全局单例不经手） ────────

/// HTTP 面：同一段执法核心 + Bearer 身份链（无凭证 401；admin 全局面 200）。
#[tokio::test]
async fn http_inner_identity_and_scope() {
    let (_dir, db) = contract_db();
    let service = Arc::new(DbReadHttpService {
        registry: Arc::new(registry(&db)),
        store: None,
    });
    let mut headers = HeaderMap::new();
    headers.insert(
        AUTHORIZATION,
        HeaderValue::from_str(&admin_bearer()).unwrap(),
    );
    // 全局面：admin 放行，跨租户全量
    let body = db_read_http_inner(
        &service,
        &headers,
        &json!({ "face": "pipeline_state_admin",
                 "sql": "SELECT tenant_id FROM pipeline_state WHERE field_key = 'run_status'" }),
    )
    .await
    .unwrap();
    assert_eq!(body["rows"], json!([["tenant-a"], ["tenant-b"]]));
    // tenant 档面：admin 身份只收窄到自己的租户（default，夹具无该租户行）
    // ——"看单个租户的按租户收窄"对任何身份成立，不退化为全租户。
    let body = db_read_http_inner(
        &service,
        &headers,
        &json!({ "face": "traces", "sql": "SELECT tenant_id FROM traces GROUP BY tenant_id" }),
    )
    .await
    .unwrap();
    assert_eq!(body["rows"], json!([]));
    // 无凭证：401
    let err = db_read_http_inner(
        &service,
        &HeaderMap::new(),
        &json!({ "face": "traces", "sql": "SELECT 1" }),
    )
    .await
    .unwrap_err();
    assert_eq!(err.status, 401);
    assert_eq!(err.code, ERR_NOT_AUTHENTICATED);
}

// ── busy_timeout 防护（性能监测补强 T4）──

/// 只读连接 busy_timeout 查回值与引擎连接同口径（5000，同一常量单一真值）：
/// 多连接并发下遇 WAL checkpoint 等短暂持锁等待而非立即 SQLITE_BUSY。
#[test]
fn open_read_only_conn_sets_busy_timeout() {
    let (_dir, db_path) = contract_db();
    let conn = open_read_only_conn(db_path.to_str().unwrap()).unwrap();
    let v: i64 = conn
        .query_row("PRAGMA busy_timeout", [], |r| r.get(0))
        .unwrap();
    assert_eq!(v, SQLITE_BUSY_TIMEOUT_MS);
    assert_eq!(v, 5000);
}

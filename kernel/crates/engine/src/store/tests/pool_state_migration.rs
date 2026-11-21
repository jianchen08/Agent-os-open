use super::*;

use super::super::schema::DDL;

// ── DedicatedDbPool：固定长命 DB 线程池行为契约 ──

/// 并发 50 任务全部完成且结果正确：池不丢任务、不串结果。
/// 载荷两组有区分度（短文本 / 4KB+ 长文本），性质断言 = 50 个互异内容
/// 恰好得到 50 个互异 blob_id 且逐一回读一致。
#[tokio::test]
async fn db_pool_concurrent_50_tasks_all_complete_and_correct() {
    let store = Arc::new(SqliteStore::open_memory().unwrap());
    let mut handles = Vec::new();
    for i in 0..50u32 {
        let store = Arc::clone(&store);
        handles.push(tokio::spawn(async move {
            let (data, mime) = if i % 2 == 0 {
                (format!("payload-{i}").into_bytes(), "text/plain")
            } else {
                (vec![b'x'; 4096 + i as usize], "application/octet-stream")
            };
            // 全限定调用：固有同名同步 store_blob 会遮蔽 trait 的 async 版本
            let blob_id = <SqliteStore as StorageBackend>::store_blob(&store, &data, mime).await?;
            Ok::<_, StorageError>((blob_id, data))
        }));
    }
    let mut seen = std::collections::HashSet::new();
    for h in handles {
        let (blob_id, data) = h.await.unwrap().unwrap();
        let roundtrip = store.get_blob(&blob_id).await.unwrap();
        assert_eq!(roundtrip, data, "回读内容必须与写入一致");
        assert!(seen.insert(blob_id), "blob_id 不得重复（任务串结果）");
    }
    assert_eq!(seen.len(), 50, "50 个互异内容必须全部落库，无任务丢失");
}

/// 关停语义：池 Drop（发送端关闭）后，已入队任务必须先排空再让线程退出
/// ——关停不丢任务（8 任务 > 4 线程，必然存在入队未执行的任务）。
#[test]
fn db_pool_shutdown_drains_queued_jobs_without_loss() {
    let pool = DedicatedDbPool::spawn().unwrap();
    let (done_tx, done_rx) = std::sync::mpsc::channel::<usize>();
    for i in 0..8usize {
        let done_tx = done_tx.clone();
        pool.dispatch(Box::new(move || {
            let _ = done_tx.send(i);
        }))
        .unwrap();
    }
    drop(done_tx);
    drop(pool);
    // iter() 在通道关闭且排空后结束：每个任务闭包持有 done_tx 的一个 clone，
    // 全部执行完（clone 随闭包释放）iter 才收尾——collect 完成即"全部执行完"。
    let mut got: Vec<usize> = done_rx.iter().collect();
    got.sort_unstable();
    assert_eq!(got, (0..8).collect::<Vec<_>>(), "关停不得丢任务");
}

/// 关停后不再接受：通道断连（池 Drop 后发送端消亡的等价状态）时 dispatch
/// 返回错误而非静默入队丢失。
#[test]
fn db_pool_dispatch_rejected_after_shutdown() {
    let (tx, rx) = std::sync::mpsc::sync_channel::<DbJob>(1);
    drop(rx);
    let pool = DedicatedDbPool {
        tx,
        blocked_dispatches: std::sync::atomic::AtomicU64::new(0),
    };
    let err = pool.dispatch(Box::new(|| {})).unwrap_err();
    assert!(matches!(err, StorageError::Database(_)));
    assert!(
        err.to_string().contains("shut down"),
        "拒绝信息须可诊断: {err}"
    );
}

/// 背压契约：队列打满后 dispatch 必须阻塞等待（不再无界积压），消费
/// 恢复后阻塞的 dispatch 返回、任务全部执行。单 worker + 容量 1 注入，
/// gate 门控 worker 占位，使"队列满"成为确定性状态（非 sleep 竞速）。
#[test]
fn db_pool_dispatch_backpressures_when_full_then_recovers() {
    let pool = std::sync::Arc::new(DedicatedDbPool::spawn_with(1, 1).unwrap());
    let executed = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));

    // job1：占住唯一 worker，等 gate 放行——期间队列无法被消费。
    let (gate_tx, gate_rx) = std::sync::mpsc::channel::<()>();
    let gate_rx = std::sync::Arc::new(parking_lot::Mutex::new(gate_rx));
    let (started_tx, started_rx) = std::sync::mpsc::channel::<()>();
    let executed1 = std::sync::Arc::clone(&executed);
    pool.dispatch(Box::new(move || {
        let _ = started_tx.send(());
        let _ = gate_rx.lock().recv();
        executed1.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    }))
    .unwrap();
    started_rx
        .recv()
        .expect("worker 应开始执行 job1 并停在 gate");

    // job2：占满唯一队列槽位。
    let executed2 = std::sync::Arc::clone(&executed);
    pool.dispatch(Box::new(move || {
        executed2.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    }))
    .unwrap();

    // job3：队列满 → dispatch 必须阻塞，在独立线程派发。
    let pool3 = std::sync::Arc::clone(&pool);
    let executed3 = std::sync::Arc::clone(&executed);
    let dispatcher = std::thread::spawn(move || {
        pool3
            .dispatch(Box::new(move || {
                executed3.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            }))
            .expect("恢复消费后阻塞的 dispatch 必须成功入队");
    });

    std::thread::sleep(std::time::Duration::from_millis(100));
    assert!(
        !dispatcher.is_finished(),
        "队列满时 dispatch 必须阻塞（背压），不得立即返回"
    );
    assert_eq!(
        executed.load(std::sync::atomic::Ordering::SeqCst),
        0,
        "背压未解除前不得有任何任务执行"
    );

    // 放行 job1 → worker 依次消费 job2、job3，阻塞的 dispatch 返回。
    drop(gate_tx);
    dispatcher.join().unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while executed.load(std::sync::atomic::Ordering::SeqCst) < 3 {
        assert!(
            std::time::Instant::now() < deadline,
            "放行后任务须全部执行完（恢复消费），实际: {}",
            executed.load(std::sync::atomic::Ordering::SeqCst)
        );
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    assert_eq!(
        pool.blocked_dispatches
            .load(std::sync::atomic::Ordering::SeqCst),
        1,
        "一次满载派发必须累计一次背压计数（可观测不静默）"
    );
}

/// 任务 panic 必须映射为错误（不悬挂调用方），且长命线程不被打死——
/// 后续任务仍正常完成。
#[tokio::test]
async fn db_pool_task_panic_maps_to_error_and_pool_survives() {
    let store = SqliteStore::open_memory().unwrap();
    let r: Result<(), StorageError> = store
        .blocking(|_this| -> Result<(), StorageError> { panic!("boom") })
        .await;
    assert!(
        matches!(r, Err(StorageError::Database(_))),
        "panic 必须映射为 StorageError，实际: {r:?}"
    );
    let ok: Result<u32, StorageError> = store.blocking(|_this| Ok(7)).await;
    assert_eq!(ok.unwrap(), 7, "panic 后池必须仍然可用");
}

// ── B6：pipeline-state 批量事务 upsert（all-or-nothing）─────────────

#[test]
fn upsert_state_fields_batch_is_all_or_nothing() {
    let store = SqliteStore::open_memory().unwrap();
    // 中键注入失败：触发器对 field_key = 'b' 的插入 RAISE(ABORT)
    store
            .with_conn::<(), String>(|conn| {
                conn.execute(
                    "CREATE TRIGGER fail_b BEFORE INSERT ON pipeline_state                      WHEN NEW.field_key = 'b' BEGIN SELECT RAISE(ABORT, 'injected mid-key failure'); END;",
                    [],
                )
                .map(|_| ())
                .map_err(|e| e.to_string())
            })
            .unwrap();
    let mut fields = serde_json::Map::new();
    fields.insert("a".to_string(), serde_json::json!(1));
    fields.insert("b".to_string(), serde_json::json!(2));
    fields.insert("c".to_string(), serde_json::json!(3));
    let r = store.upsert_state_fields("pipe_tx", "default", &fields);
    assert!(
        matches!(r, Err(StorageError::Database(ref m)) if m.contains("injected")),
        "中键失败必须整体报错，实际 {r:?}"
    );
    let loaded = store
        .load_pipeline_state("pipe_tx", "default")
        .expect("读回成功");
    assert!(
        loaded.is_empty(),
        "all-or-nothing：中键失败后不得有部分写入，实际 {loaded:?}"
    );

    // 对照组（第二组输入）：无触发器管道批量 3 键全落
    let mut ok_fields = serde_json::Map::new();
    ok_fields.insert("x".to_string(), serde_json::json!("1"));
    ok_fields.insert("y".to_string(), serde_json::json!("2"));
    store
        .upsert_state_fields("pipe_ok", "default", &ok_fields)
        .expect("无故障批量写应成功");
    let loaded = store
        .load_pipeline_state("pipe_ok", "default")
        .expect("读回成功");
    assert_eq!(loaded.len(), 2, "批量成功路径键数完整: {loaded:?}");
    assert_eq!(loaded.get("x"), Some(&serde_json::json!("1")));
}

/// llm_usage 用量聚合：索引入索引 + 聚合值正确（数据驱动，含大载荷非用量行）。
///
/// 该索引的收益分两层：部分索引条件剪掉大体积非用量轨迹（收益主体），表达式列
/// 免回表（增量）。故断言三件事——索引是部分索引、生产查询形状走索引、聚合值
/// 只统计含 llm_usage 的行。消费方 SQL 原文见 plugins/shared/traces_usage.py。
#[test]
fn test_llm_usage_aggregation_uses_partial_covering_index() {
    let store = SqliteStore::open_memory().unwrap();
    let conn = store.conn.lock();

    let ddl: String = conn
            .query_row(
                "SELECT COALESCE(sql, '') FROM sqlite_master WHERE type='index' AND name='idx_traces_llm_usage'",
                [],
                |r| r.get(0),
            )
            .unwrap();
    assert!(
        ddl.contains("WHERE") && ddl.contains("llm_usage"),
        "必须是限定 llm_usage 的部分索引（扫描剪枝的根据），实际定义: {ddl}"
    );

    // 两条用量行（同模型）+ 一条大载荷非用量行（不得进索引、不得参与聚合）
    for (i, (tid, model, total)) in [("u1", "m-a", 10), ("u2", "m-a", 25), ("u3", "m-b", 7)]
        .into_iter()
        .enumerate()
    {
        let payload = format!(
            "{{\"llm_usage\":{{\"model\":\"{model}\",\"provider\":\"p\",\
                 \"input_tokens\":1,\"output_tokens\":2,\"total_tokens\":{total}}}}}"
        );
        conn.execute(
                "INSERT INTO traces (trace_id, pipeline_id, seq, plugin_id, patch_type, patch_data, tenant_id, created_at) \
                 VALUES (?1, 'pipe_llm_usage', ?3, 'llm_core', 'state_update', ?2, 'default', '2026-09-15T00:00:00Z')",
                rusqlite::params![tid, payload, i as i64],
            )
            .unwrap();
    }
    let big = format!(
        "{{\"messages\":[{{\"role\":\"user\",\"content\":\"{}\"}}]}}",
        "x".repeat(200_000)
    );
    conn.execute(
            "INSERT INTO traces (trace_id, pipeline_id, seq, plugin_id, patch_type, patch_data, tenant_id, created_at) \
             VALUES ('big', 'pipe_llm_usage', 9, 'p', 'state_update', ?1, 'default', '2026-09-15T00:00:00Z')",
            rusqlite::params![big],
        )
        .unwrap();

    // 生产查询原文（traces_usage.aggregate_usage_by_model）
    let by_model = "SELECT COALESCE(json_extract(patch_data, '$.llm_usage.model'), ''),\
             \n       COALESCE(json_extract(patch_data, '$.llm_usage.provider'), ''),\
             \n       SUM(json_extract(patch_data, '$.llm_usage.input_tokens')),\
             \n       SUM(json_extract(patch_data, '$.llm_usage.output_tokens')), \
             \n       SUM(json_extract(patch_data, '$.llm_usage.total_tokens')), \
             \n       COUNT(*)\
             \n       FROM traces WHERE json_extract(patch_data, '$.llm_usage') IS NOT NULL\
             \n       GROUP BY 1, 2 ORDER BY 5 DESC";
    let plans: Vec<String> = conn
        .prepare(&format!("EXPLAIN QUERY PLAN {by_model}"))
        .unwrap()
        .query_map([], |r| r.get::<_, String>(3))
        .unwrap()
        .map(|r| r.unwrap())
        .collect();
    assert!(
        plans.iter().any(|p| p.contains("idx_traces_llm_usage")),
        "按模型聚合必须命中 llm_usage 索引，实际计划 {plans:?}"
    );

    let mut stmt = conn.prepare(by_model).unwrap();
    let rows: Vec<(String, f64, i64)> = stmt
        .query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, f64>(4)?,
                r.get::<_, i64>(5)?,
            ))
        })
        .unwrap()
        .map(|r| r.unwrap())
        .collect();
    let mut got = rows.clone();
    got.sort_by(|a, b| a.0.cmp(&b.0));
    assert_eq!(
        got,
        vec![("m-a".to_string(), 35.0, 2), ("m-b".to_string(), 7.0, 1)],
        "按模型聚合计数值：大载荷非用量行不得计入"
    );
}

// ── 迁移/退役辅助函数（init 内的表保全与补列）──

/// 环境变量驱动配置读取的契约：未设回落默认、可解析值生效、
/// 非法值与超大值各自归位（钳制上限保护 cutoff 计算）。
#[test]
fn trace_retention_days_env_contract() {
    const ENV: &str = "AGENTOS_TRACE_RETENTION_DAYS";
    let saved = std::env::var(ENV).ok();
    struct Restore(Option<String>);
    impl Drop for Restore {
        fn drop(&mut self) {
            match &self.0 {
                Some(v) => std::env::set_var(ENV, v),
                None => std::env::remove_var(ENV),
            }
        }
    }
    let _restore = Restore(saved);

    std::env::remove_var(ENV);
    assert_eq!(
        trace_retention_days(),
        TRACE_RETENTION_DAYS_DEFAULT,
        "未设置回落默认"
    );
    std::env::set_var(ENV, "7");
    assert_eq!(trace_retention_days(), 7, "合法值生效");
    std::env::set_var(ENV, "0");
    assert_eq!(trace_retention_days(), 0, "0 = 禁用清扫，不得被默认值顶替");
    std::env::set_var(ENV, "not-a-number");
    assert_eq!(
        trace_retention_days(),
        TRACE_RETENTION_DAYS_DEFAULT,
        "非法值回落默认（家政不阻断启动）"
    );
    std::env::set_var(ENV, "999999999");
    assert_eq!(
        trace_retention_days(),
        TRACE_RETENTION_DAYS_MAX,
        "超大值钳制到上限（cutoff 不溢出）"
    );
}

/// 退役表处置三态：表不存在 = 无操作；空表 = DROP；有行 = 改名留档
/// （数据保全优先于自动清理）。
#[test]
fn retire_table_preserve_rows_three_states() {
    let conn = Connection::open_in_memory().unwrap();

    // ① 表不存在：静默成功，不做任何事
    retire_table_preserve_rows(&conn, "absent_table").unwrap();
    let n: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'absent_table%'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(n, 0);

    // ② 空表：DROP（表清单与后端读写一一对应）
    conn.execute_batch("CREATE TABLE empty_retired (a TEXT);")
        .unwrap();
    retire_table_preserve_rows(&conn, "empty_retired").unwrap();
    let n: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'empty_retired'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(n, 0, "空退役表应被 DROP");

    // ③ 有行：改名 `_retired_<ts>` 保全，原表名不再存在，行数不变
    conn.execute_batch(
        "CREATE TABLE data_retired (a TEXT); INSERT INTO data_retired VALUES ('v');",
    )
    .unwrap();
    retire_table_preserve_rows(&conn, "data_retired").unwrap();
    let renamed: String = conn
            .query_row(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'data_retired_retired_%'",
                [],
                |r| r.get(0),
            )
            .expect("有行退役表必须改名留档而非删除");
    let rows: i64 = conn
        .query_row(&format!("SELECT COUNT(*) FROM {renamed}"), [], |r| r.get(0))
        .unwrap();
    assert_eq!(rows, 1, "留档表必须保留原始行");
    let gone: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'data_retired'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(gone, 0, "原表名应撤位");
}

/// 旧 schema 的 message_slots（含内容列）整表撤位重建：重建后新表只含
/// 现行纯索引列，旧行经 `_retired_` 留档；无旧列的现行表保持原样。
#[test]
fn migrate_drop_legacy_message_slots_rebuilds_only_on_legacy_columns() {
    // ① 旧宽表：含 content_preview 内容列 + 一行数据
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(
        "CREATE TABLE message_slots (tenant_id TEXT, pipeline_id TEXT, seq INTEGER, \
             content_preview TEXT); INSERT INTO message_slots VALUES ('t','p',0,'old');",
    )
    .unwrap();
    migrate_drop_legacy_message_slots(&conn).unwrap();
    let cols: Vec<String> = {
        let mut stmt = conn.prepare("PRAGMA table_info(message_slots)").unwrap();
        stmt.query_map([], |r| r.get::<_, String>(1))
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
    };
    assert!(
        !cols.iter().any(|c| c == "content_preview"),
        "重建后不得再含旧内容列，实际: {cols:?}"
    );
    assert!(
        cols.iter().any(|c| c == "message_id") && cols.iter().any(|c| c == "blob_id"),
        "重建后应为现行纯索引列，实际: {cols:?}"
    );
    let archived: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'message_slots_retired_%'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(archived, 1, "旧宽表有行 → 改名留档（数据保全优先于清理）");
    let archived_name: String = conn
        .query_row(
            "SELECT name FROM sqlite_master WHERE name LIKE 'message_slots_retired_%'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let kept_rows: i64 = conn
        .query_row(
            &format!("SELECT COUNT(*) FROM {archived_name} WHERE content_preview = 'old'"),
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(kept_rows, 1, "留档表必须保留旧行原文（升级不静默抹数据）");

    // ② 现行纯索引表：不触发重建（列集合保持不变）
    let conn2 = Connection::open_in_memory().unwrap();
    conn2.execute_batch(DDL).unwrap();
    let before: Vec<String> = {
        let mut stmt = conn2.prepare("PRAGMA table_info(message_slots)").unwrap();
        stmt.query_map([], |r| r.get::<_, String>(1))
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
    };
    migrate_drop_legacy_message_slots(&conn2).unwrap();
    let after: Vec<String> = {
        let mut stmt = conn2.prepare("PRAGMA table_info(message_slots)").unwrap();
        stmt.query_map([], |r| r.get::<_, String>(1))
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
    };
    assert_eq!(before, after, "无旧列的现行表不得被重建");
}

/// 护栏（ADR-2026-11-17 §⑤）：`pipeline_pending_inputs` 列集合 = 6 列信封。
/// 旧列复活（新增派发字段直上表）必红——新增字段应进 request JSON 单列，
/// 不是加列（加列 = DB 迁移 + core 类型 + trait 三处同步，正是本 ADR 消灭的成本）。
#[test]
fn pending_inputs_table_columns_are_six_envelope_only() {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(DDL).unwrap();
    let mut cols: Vec<String> = {
        let mut stmt = conn
            .prepare("PRAGMA table_info(pipeline_pending_inputs)")
            .unwrap();
        stmt.query_map([], |r| r.get::<_, String>(1))
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
    };
    cols.sort_unstable();
    assert_eq!(
        cols,
        vec![
            "client_message_id",
            "created_at",
            "id",
            "pipeline_id",
            "request",
            "tenant_id",
        ],
        "队列表列集合必须收敛为 6 列信封（防旧列复活；新增字段应进 request JSON）"
    );
}

/// 护栏（ADR-2026-11-17 §④⑤）：旧 15 列行 → request 回填迁移的语义保全。
/// 断言四件事：①迁移后表收敛为 6 列；②`route_id` 优先落 `pipeline_id`
/// （入队时解析后的路由键，空则回退 pipeline_id 列）；③legacy
/// `agent_id` / `thinking_strength` 非空值并入 `state_overlay` 对应键
/// （排队中的用户选择不丢，且不覆盖显式 overlay 键）；④迁移幂等。
#[test]
fn migrate_pending_input_dispatch_request_backfills_legacy_rows() {
    use super::super::schema::migrate_pending_input_dispatch_request;

    let conn = Connection::open_in_memory().unwrap();
    // 旧 15 列形态按 HEAD 前旧 DDL 原样重建；source 列存 serde 序列化串
    // （`"task"` 带引号）。行 1 = 全量字段非空；行 2 = legacy 身份/强度/overlay
    // 全空（回归默认形态）。
    conn.execute_batch(
        r#"CREATE TABLE pipeline_pending_inputs (
             id TEXT PRIMARY KEY, pipeline_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
             user_id TEXT NOT NULL, content TEXT NOT NULL, thread TEXT NOT NULL,
             source TEXT NOT NULL, agent_id TEXT NOT NULL DEFAULT 'agentos',
             route_id TEXT NOT NULL DEFAULT '', thinking_strength TEXT NOT NULL DEFAULT '',
             client_message_id TEXT NOT NULL DEFAULT '', execution_context TEXT,
             state_overlay TEXT, pipeline_config_id TEXT, created_at TEXT NOT NULL);
           INSERT INTO pipeline_pending_inputs VALUES (
             'in_lg1', 'P-thread-key', 'default', 'u1', '排队中', 'T-lg', '"task"',
             'general_agent', 'P-route', '{"depth":"high"}', 'cmid-9',
             '{"mode":"isolated"}', '{"task.goal":"done"}', 'custom_pipe',
             '2026-11-16T01:00:00Z');
           INSERT INTO pipeline_pending_inputs VALUES (
             'in_lg2', 'P2', 'default', 'u2', '无身份', 'T2', '"user"',
             '', '', '', '', NULL, NULL, NULL, '2026-11-16T02:00:00Z');"#,
    )
    .unwrap();

    migrate_pending_input_dispatch_request(&conn).unwrap();

    // ① 列集合收敛为 6 列信封（旧列物理消失）
    let mut cols: Vec<String> = {
        let mut stmt = conn
            .prepare("PRAGMA table_info(pipeline_pending_inputs)")
            .unwrap();
        stmt.query_map([], |r| r.get::<_, String>(1))
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
    };
    cols.sort_unstable();
    assert_eq!(
        cols,
        vec![
            "client_message_id",
            "created_at",
            "id",
            "pipeline_id",
            "request",
            "tenant_id",
        ],
        "迁移后必须收敛为 6 列信封，实际: {cols:?}"
    );

    // ②③ 行 1：全量回填语义
    let (pipeline_col, request): (String, String) = conn
        .query_row(
            "SELECT pipeline_id, request FROM pipeline_pending_inputs WHERE id='in_lg1'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(
        pipeline_col, "P-route",
        "索引投影取 route_id（入队时解析后的路由键），非 thread 侧 pipeline_id 列"
    );
    let req: serde_json::Value = serde_json::from_str(&request).unwrap();
    assert_eq!(req["thread_id"], "T-lg");
    assert_eq!(req["user_id"], "u1");
    assert_eq!(req["pipeline_id"], "P-route");
    assert_eq!(req["content"], "排队中");
    assert_eq!(req["client_message_id"], "cmid-9");
    assert_eq!(req["source"], "task", "source 剥 serde 引号还原字符串值");
    assert_eq!(
        req["execution_context"],
        serde_json::json!({"mode": "isolated"})
    );
    assert_eq!(req["pipeline_config_id"], "custom_pipe");
    assert_eq!(
        req["state_overlay"],
        serde_json::json!({
            "task.goal": "done",
            "agent.id": "general_agent",
            "thinking_strength": "{\"depth\":\"high\"}",
        }),
        "legacy agent_id/thinking_strength 非空并入 overlay，且不覆盖显式 overlay 键"
    );

    // 行 2：legacy 身份/强度/overlay 全空 → 不伪造 overlay 键（null）
    let request2: String = conn
        .query_row(
            "SELECT request FROM pipeline_pending_inputs WHERE id='in_lg2'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let req2: serde_json::Value = serde_json::from_str(&request2).unwrap();
    assert_eq!(
        req2["state_overlay"],
        serde_json::Value::Null,
        "空 overlay + 空 legacy 值不得伪造 overlay 键"
    );
    assert_eq!(req2["pipeline_id"], "P2", "route_id 空回退 pipeline_id 列");
    assert_eq!(req2["source"], "user");
    assert_eq!(
        req2["pipeline_config_id"],
        serde_json::Value::Null,
        "空 config id 回填 null（= autonomous 缺省）"
    );

    // ④ 幂等：request 列已在 → 再跑无动作（不丢行不重复）
    migrate_pending_input_dispatch_request(&conn).unwrap();
    let n: i64 = conn
        .query_row("SELECT COUNT(*) FROM pipeline_pending_inputs", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(n, 2, "重复迁移不得丢行/重复行");
}

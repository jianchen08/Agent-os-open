//! StorageBackend 存储后端抽象（state 唯一真值 + op 流四表模型）。

use super::*;

/// run 起手 sweep 收口摘要（ADR 2026-11-06-round-entry-slot-placeholder 决策6）。
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct StreamingSlotSweep {
    /// 三空占位行删除数（崩溃残留，无用户可见产出）。
    pub deleted_empty: usize,
    /// 有内容占位行改写 `status=interrupted` 数（半截产出留账）。
    pub marked_interrupted: usize,
}

/// 存储后端抽象（ADR ③④）。
///
/// SQLite 四表模型的 trait 抽象，供 ContentLoader 和 AdrEngine 使用。
/// 具体实现为 SQLite，但 trait 层不绑定具体数据库——便于测试时 mock。
///
/// **存储模型**（ADR 2026-09-18：runs/branches 退役，state 唯一真值）：
/// - `pipeline_state`：state 标量字段的唯一真值（含运行簿记键 run_status/
///   run_started_at/run_ended_at/run_config_hash/run_id/suspend_request_id）
/// - `message_slots` + `blobs`：消息 op 流（槽位 + 内容寻址）
/// - `traces`：pipeline 级 op 日志（定位 = (pipeline_id, seq)，审计/DR 回放）
/// - `pipeline_checkpoints`：纯备份（仅 state 丢失/损坏时作 DR 基线）
///
/// [来源: docs/decisions/（ADR 2026-09-18 runs 退役与 state 唯一真值）]
#[async_trait]
pub trait StorageBackend: Send + Sync {
    /// 获取运行记录（由 state 运行簿记键合成；按 run_id 反查所属管道）。
    async fn get_run(&self, run_id: &str) -> Result<RunRecord, StorageError>;

    /// 运行开始簿记：写 state 运行键（run_id/run_status='running'/run_started_at/
    /// run_config_hash）。`started_at` 由调用方（引擎 run_init 单写者）显式传入，
    /// 本方法只持久化、不自产时间戳——双写自产第二时间戳会让插件层字符串相等
    /// renewal 判定误判新 run（刀5 根因）。默认 no-op（mock/null store），
    /// SqliteStore 覆盖。
    async fn record_run_start(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _run_id: &str,
        _config_hash: &str,
        _started_at: &str,
    ) -> Result<(), StorageError> {
        Ok(())
    }

    /// 消费管道级待取消标记（首 token 前取消收口）：检出 `pending_cancel=true`
    /// 即清除并返回 true（test-and-clear，一次落库）；无标记返回 false。
    /// 默认无标记（mock/null store），SqliteStore 覆盖。
    async fn consume_pending_cancel(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
    ) -> Result<bool, StorageError> {
        Ok(false)
    }

    /// 列出某管道的运行投影（ADR 2026-09-18：每管道至多一条——当前运行）。
    /// 默认空（mock/null store），SqliteStore 覆盖。
    async fn list_runs_by_pipeline(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<RunRecord>, StorageError> {
        Ok(Vec::new())
    }

    /// 管道运行快照列表（runs × message_slots × pipeline_sessions × pipeline_run_summaries
    /// 联结，按 started_at 倒序）。调试中心「会话/执行记录」的会话维度数据源，
    /// 与 `GET /api/v1/pipelines/runs` 同查询。默认空（mock/null store），SqliteStore 覆盖。
    async fn list_pipelines(
        &self,
        _tenant_id: &str,
        _status: Option<&str>,
        _limit: u32,
    ) -> Result<Vec<PipelineRunInfo>, StorageError> {
        Ok(Vec::new())
    }

    /// 按 pipeline_id 查询历史消息（消息层自治查询主键）。
    ///
    /// 两域解耦：消息层只按 pipeline_id 查询，不关心会话（thread）归属。
    /// 对齐 0.1 `ExecutionRecordStorage.list_by_pipeline(pipeline_run_id)`。
    /// opts 支持游标分页（before_sequence/after_sequence）与 limit。
    async fn get_messages_by_pipeline(
        &self,
        pipeline_id: &str,
        opts: MessageQueryOpts,
    ) -> Result<Vec<MessageRecord>, StorageError>;

    /// 获取指定 blob_id 的原始数据。
    async fn get_blob(&self, blob_id: &str) -> Result<Vec<u8>, StorageError>;

    /// 追加一条状态变更日志到 traces 表（Append-Only，pipeline 级 op 流）。
    /// seq 由存储层写入时分配（MAX(seq)+1），entry.seq 字段值被忽略。
    async fn append_trace(&self, entry: TraceEntry) -> Result<(), StorageError>;

    /// 存储不可变原始数据到 blobs 表（内容寻址去重，blob_id = SHA256）。
    /// 返回 blob_id 供 message_slots 行引用。
    async fn store_blob(&self, data: &[u8], mime_type: &str) -> Result<String, StorageError>;

    // ── 域10：分层持久化投影（messages 增量对齐 + 标量快照 + checkpoint）────

    /// 应用身份/seq 感知的 messages ops 到 message_slots 表（op-based 新模型单写入器）。
    /// 引擎把插件 emit 的 `set/insert` op 落表（与 `apply_slot_ops_to_array` 落内存是同一组 op）。
    /// 默认 no-op（mock/null store 用），SqliteStore 覆盖为真实实现。详见 docs/message_persistence_design.md。
    async fn apply_messages_ops_to_table(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _ops: &[serde_json::Value],
    ) -> Result<(), StorageError> {
        Ok(())
    }

    /// run 起手 sweep 前代流式占位行（ADR 2026-11-06-round-entry-slot-placeholder
    /// 决策6）：扫描本管道 blob `status=streaming` 且 `run_id ≠ current_run_id` 的
    /// 槽位行，逐条收口——三空（文本/思考/保留 tool_calls 全空，与 llm_core
    /// 三路判据同词汇）删行；有内容改写 blob `status=interrupted`（内容保全，
    /// 崩溃残留的半截产出按 2026-11-04 消息出口权留账）。单事务原子。
    /// 默认 no-op（mock/null store），SqliteStore 覆盖。
    async fn sweep_stale_streaming_slots(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _current_run_id: &str,
    ) -> Result<StreamingSlotSweep, StorageError> {
        Ok(StreamingSlotSweep::default())
    }

    /// 删除流式占位行（ADR 2026-11-06 决策4：轮无 assistant 的收口删行）。
    /// seq + record_id（占位行的 `a_` 轮次 id）双条件命中才删——同槽位已被真实
    /// 消息覆写（record_id 不再是占位 id）时不命中返回 false，防误删真消息。
    /// 默认 no-op（mock/null store），SqliteStore 覆盖。
    async fn delete_streaming_placeholder(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _seq: i64,
        _message_id: &str,
    ) -> Result<bool, StorageError> {
        Ok(false)
    }

    /// upsert 一个 state 标量字段到 pipeline_state 表（覆盖最新值）。
    /// 仅对插件 manifest 声明的 persistent_fields 调用；累计语义由插件保证。
    async fn upsert_state_field(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _key: &str,
        _value: &serde_json::Value,
    ) -> Result<(), StorageError> {
        Ok(())
    }

    /// upsert 一批 state 标量字段到 pipeline_state 表（B6 批量接口）。
    ///
    /// 默认实现退化为逐键调用 [`Self::upsert_state_field`]（只覆盖单键的
    /// 实现者零改动）；SqliteStore 覆盖为单事务全量 upsert——任一失败整体
    /// 回滚（全成才 commit），保证调用方「DB 批量成功 → 再写内存」写序下
    /// 无「DB 半套」部分写入面。既有单键接口保持不动（其他调用点不改）。
    async fn upsert_state_fields(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        fields: &serde_json::Map<String, serde_json::Value>,
    ) -> Result<(), StorageError> {
        for (key, value) in fields {
            self.upsert_state_field(pipeline_id, tenant_id, key, value)
                .await?;
        }
        Ok(())
    }

    /// 读出某 pipeline 全部持久化标量字段（冷启动重建喂回 state 用）。
    async fn load_pipeline_state(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
    ) -> Result<std::collections::HashMap<String, serde_json::Value>, StorageError> {
        Ok(std::collections::HashMap::new())
    }

    /// 批量真删 pipeline_state 行（update 的 delete_keys 落点）。
    ///
    /// 与 upsert_state_fields 对称的批量接口：SqliteStore 覆盖为逐键
    /// `DELETE ... WHERE pipeline_id=? AND tenant_id=? AND field_key=?`
    /// （主键精确删）；默认实现 no-op（Ok 空列表）——只覆盖 upsert 的实现者
    /// 不被强制支持删除语义。幂等：不存在的键跳过、不报错；返回实际删除的
    /// 键列表（调用方据返回值区分 deleted / not_found）。
    async fn delete_state_fields(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _keys: &[String],
    ) -> Result<Vec<String>, StorageError> {
        Ok(Vec::new())
    }

    /// 注入插件声明的 per-run 易变键并集（P1-4 声明化）：checkpoint 落档剥离集 =
    /// 内核自有键（store 内置）∪ 本集。生产侧由 api 从全部插件 manifest
    /// `state.volatile_keys` 声明收集并集后注入，与恢复合并侧消费同一并集。
    /// 缺省空集（不剥任何声明键）；后写覆盖（manifest 热重载后随下一次派发刷新）。
    fn set_declared_volatile_keys(&self, _keys: &[String]) {}

    /// 保存全量 state checkpoint（每 N 步留档，O(1) 重建基线）。
    async fn save_checkpoint(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _step_no: i64,
        _state: &serde_json::Value,
    ) -> Result<(), StorageError> {
        Ok(())
    }

    /// 取最近 checkpoint（step_no 最大），返回 (step_no, state)。
    async fn load_latest_checkpoint(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
    ) -> Result<Option<(i64, serde_json::Value)>, StorageError> {
        Ok(None)
    }

    /// 取目标消息序号之前最近的一份 checkpoint（step_no 最大且
    /// `ckpt_max_seq < seq_exclusive`），返回 (step_no, state)。
    ///
    /// 重发状态回退用（2026-11-09 裁定）：ckpt_max_seq 是 checkpoint 拍点时的
    /// 消息最大序号——以目标 user 消息 seq 为界，取它所属轮次开始前的管道
    /// 状态基线；没有满足条件的快照（无 checkpoint / 旧格式无水位）→ None，
    /// 调用方按无基线处理（只截消息，不回退状态）。
    async fn load_checkpoint_before_seq(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _seq_exclusive: i64,
    ) -> Result<Option<(i64, serde_json::Value)>, StorageError> {
        Ok(None)
    }

    /// 用给定字段集替换某 pipeline 的全部持久化标量（单事务先清后写）。
    ///
    /// 重发状态回退写面：baseline 里缺席的键 = 目标 step 之后才产生的键，
    /// 必须删掉（只 upsert 不删会让「未来」状态残留）；默认 no-op
    /// （mock/null store），SqliteStore 覆盖。
    async fn replace_pipeline_state(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
        _fields: &serde_json::Map<String, serde_json::Value>,
    ) -> Result<(), StorageError> {
        Ok(())
    }

    /// 枚举租户内带持久化 state（pipeline_state 标量 / checkpoint 二者有其一）的管道
    /// `(pipeline_id, thread_id)`。冷读路径（pipeline-state.list / /pipelines/state 的
    /// DB 兜底）据此列 registry 未覆盖的管道——thread_id 以 pipeline_sessions 为准，
    /// 缺省回退 pipeline_id（任务管道 thread_id 恒等于自身 pipeline_id）。
    async fn list_state_pipeline_ids(
        &self,
        _tenant_id: &str,
    ) -> Result<Vec<(String, String)>, StorageError> {
        Ok(vec![])
    }

    /// 冷启动历史读路径：从 message_slots（消息队列持久真值）join blobs 重建完整
    /// 消息对象数组（元素自带稳定 seq），零回放。
    async fn load_message_history(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<serde_json::Value>, StorageError> {
        Ok(vec![])
    }

    // ── 域12：消息段（替换事件的冻结内容，多代切换/压缩原文存档）─────────

    /// 列出某管道的消息段（不含成员、含 preview，按 base_seq, created_at 升序）。
    /// 默认空（mock/null store），SqliteStore 覆盖。
    async fn list_message_segments(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<SegmentRecord>, StorageError> {
        Ok(Vec::new())
    }

    /// 取单个消息段（含成员全文解析：members_blob 引用列表 → 逐成员 blob 重建）。
    /// 不存在 → None。默认 None（mock/null store），SqliteStore 覆盖。
    async fn get_message_segment(
        &self,
        _segment_id: &str,
        _tenant_id: &str,
    ) -> Result<Option<SegmentRecord>, StorageError> {
        Ok(None)
    }

    // ── 域11：pending 输入队列（ADR-2026-08-26）─────────────────────
    // 消息在"入队→激活"之间停留在表中，等待窗口内可修改/删除/清空；
    // 消费任务从表取参数执行（内容不被闭包捕获）。默认 no-op（mock/null store），
    // SqliteStore 覆盖为真实实现。

    /// 入队一条 pending 输入（created_at 即 FIFO 序）。幂等：同 id 重复入队忽略；
    /// 同一 client_message_id 重复入队忽略（网络重试/前端重发不产生第二条）。
    /// 管道键取自 `input.request.pipeline_id`（单一真值，无冗余形参）。
    async fn enqueue_pending_input(
        &self,
        _tenant_id: &str,
        _input: &PendingInputRecord,
    ) -> Result<(), StorageError> {
        Ok(())
    }

    /// 将队列目标条目置为队首（FIFO 键 created_at 置最早）。
    /// 返回 false = 目标不在队列（已被消费/删除）。
    async fn promote_pending_input(
        &self,
        _tenant_id: &str,
        _pipeline_id: &str,
        _input_id: &str,
    ) -> Result<bool, StorageError> {
        Ok(false)
    }

    /// 取队首（FIFO：created_at, id 升序）第一条 pending 输入。None = 队列空。
    async fn pop_pending_input(
        &self,
        _tenant_id: &str,
        _pipeline_id: &str,
    ) -> Result<Option<PendingInputRecord>, StorageError> {
        Ok(None)
    }

    /// 列出某管道全部 pending 条目（按 FIFO 序）。
    async fn list_pending_inputs(
        &self,
        _tenant_id: &str,
        _pipeline_id: &str,
    ) -> Result<Vec<PendingInputRecord>, StorageError> {
        Ok(Vec::new())
    }

    /// 修改 pending 条目 content（不存在 → Ok(false)）。
    async fn update_pending_input_content(
        &self,
        _tenant_id: &str,
        _pipeline_id: &str,
        _input_id: &str,
        _new_content: &str,
    ) -> Result<bool, StorageError> {
        Ok(false)
    }

    /// 删除单条 pending 输入（不存在 → Ok(false)）。
    async fn delete_pending_input(
        &self,
        _tenant_id: &str,
        _pipeline_id: &str,
        _input_id: &str,
    ) -> Result<bool, StorageError> {
        Ok(false)
    }

    /// 清空管道全部 pending 输入，返回删除条数。
    async fn clear_pending_inputs(
        &self,
        _tenant_id: &str,
        _pipeline_id: &str,
    ) -> Result<usize, StorageError> {
        Ok(0)
    }

    // 域2：session 标签夹（对齐 0.1 SessionModel）─────────────────────
    // 解耦：session 只持 pipeline_ids 引用列表，不反向 join messages。

    /// 创建会话（对齐 0.1 SessionModel + MemoryStore.set_session）。
    async fn create_session(&self, session: &SessionRecord) -> Result<(), StorageError>;

    /// 按 thread_id 取单个会话（含 pipeline_ids）。
    async fn get_session(&self, thread_id: &str) -> Result<Option<SessionRecord>, StorageError>;

    /// 列会话（按 filter 过滤，对齐 0.1 list_threads）。
    async fn list_sessions(
        &self,
        filter: SessionListFilter,
    ) -> Result<Vec<SessionRecord>, StorageError>;

    /// 更新会话（upsert：存在则更新，不存在则插入）。
    /// 用于 create_thread 后追加 pipeline_id、更新 title/agent 等。
    async fn update_session(&self, session: &SessionRecord) -> Result<(), StorageError>;

    /// 删除会话并级联清理其全部关联数据（主管道 + 子任务管道的 messages/traces/runs/state）。
    /// 通过 pipeline_sessions 映射表按 thread_id 定位全部 pipeline_id，再沿血缘
    /// lineage.parent_pipeline_id 收集后代任务管道（子任务出生落自环绑定
    /// thread=自身 id，不在会话清单里），单次事务级联删除。
    /// 返回实际删除的 pipeline_id 清单（根 + 血缘后代）——调用方据此逐出内存
    /// state 注册表（DB 已净而注册表残留会让 /pipelines/state 继续出口幽灵行）。
    /// 无记录时返回空清单（幂等，对齐 REST 删除语义）。
    async fn delete_session(&self, thread_id: &str) -> Result<Vec<String>, StorageError>;

    /// 按 pipeline_id 删除单条管道的全部执行数据（任务删除语义，2026-08-24）。
    /// 0.2 任务 = 管道：删除任务即删除其管道数据（runs/traces/branches/
    /// message_slots/pipeline_state/pipeline_checkpoints/pipeline_sessions），
    /// 并沿血缘 lineage.parent_pipeline_id 级联其派生的子任务链。
    /// 返回实际删除的 pipeline_id 清单（含血缘后代，供调用方逐出内存注册表）。
    /// 无记录时返回空清单（幂等）。
    async fn delete_pipeline(&self, _pipeline_id: &str) -> Result<Vec<String>, StorageError> {
        Ok(Vec::new())
    }

    /// 会话是否存在（thread_id 是 `sessions` 主键，全局唯一；不按租户过滤）。
    ///
    /// 派生管道创建闸门：归属会话已被删除时拒绝出生——否则在途 run 用已删
    /// 会话 id 派生的子任务管道会以"孤儿"形态留在库里（2026-11-10 装机实证）。
    /// 默认 `Ok(true)`（mock/null store 不破），SqliteStore 覆盖为真实查询。
    async fn session_exists(&self, _thread_id: &str) -> Result<bool, StorageError> {
        Ok(true)
    }

    /// 删除前挂起（会话级联）：把会话名下全部管道当前运行置 `run_status=suspended`，
    /// 返回其 run_id 清单——调用方逐条发 RunSignalHub 停止信号（llm 流式
    /// 取消感知的事件驱动入口）。
    ///
    /// 删除不会让在途 run 立刻停手；先挂起把"删后仍跑"的窗口收窄，数据面兜底
    /// 由 store 的进程级删除墓碑（写屏障）承担。
    /// 默认空清单（mock/null store 不破），SqliteStore 覆盖为真实实现。
    async fn suspend_cascade_runs(&self, _thread_id: &str) -> Result<Vec<String>, StorageError> {
        Ok(Vec::new())
    }

    /// 删除前挂起（单管道语义，含血缘后代），返回 run_id 清单。
    /// 默认空清单（mock/null store 不破），SqliteStore 覆盖为真实实现。
    async fn suspend_pipeline_runs(&self, _pipeline_id: &str) -> Result<Vec<String>, StorageError> {
        Ok(Vec::new())
    }

    /// 写入 pipeline↔session 映射（幂等）。每次管道开跑（persist_run_start）时记录，
    /// 含子任务管道。删除会话时据此按 thread_id 找到全部 pipeline_id 级联清理。
    async fn link_pipeline_session(
        &self,
        pipeline_id: &str,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<(), StorageError>;

    /// 查询某会话下的全部 pipeline_id（主管道 + 子任务管道）。
    async fn list_pipeline_ids_by_thread(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<String>, StorageError>;

    /// 按管道唯一坐标反查所属会话 thread_id（注入分支坐标解析）。
    /// pipeline_id 全局唯一（uuid），单查无歧义，不按租户过滤。
    /// 只查 pipeline_sessions 映射表（唯一真值源），未命中即 None——调用方
    /// （chat.send_message 注入分支）据此报协议错误，不做 sessions 回退。
    /// 默认 `Ok(None)`（mock/null store 不破），SqliteStore 覆盖为真实查询。
    async fn get_thread_id_by_pipeline(
        &self,
        _pipeline_id: &str,
    ) -> Result<Option<String>, StorageError> {
        Ok(None)
    }

    /// 查询某会话下的 step 级轨迹（冷启动统一回放用）。
    /// 经 pipeline_sessions 映射 → run_id → traces，只取 step 级（plugin_id 为配置
    /// step id），按 created_at 升序以便按序 merge 回放重建完整 state（含 messages）。
    ///
    /// 会话是组织集合：本方法只供会话面聚合读（执行记录页/复盘骨架），
    /// 管道执行态恢复禁止用它（会串其它管道的控制态）——用
    /// [`Self::get_step_traces_by_pipeline`]。
    async fn get_step_traces_by_thread(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<TraceEntry>, StorageError>;

    /// 查询单个管道自己的 step 级轨迹（管道执行态冷恢复专用）。
    /// pipeline_id 是执行态唯一坐标：state 回放只允许读本管道产生的轨迹，
    /// 同会话其它管道（父/兄弟子任务）的轨迹一律不可见。
    /// 默认 `Ok(vec![])`（mock/null store 不破），SqliteStore 覆盖为真实查询。
    async fn get_step_traces_by_pipeline(
        &self,
        _pipeline_id: &str,
        _tenant_id: &str,
    ) -> Result<Vec<TraceEntry>, StorageError> {
        Ok(vec![])
    }

    // ── 域6：users（0.5.0 完整用户系统的最小持久化地基）───────────────
    // register 真实建用户、login/me/refresh/WS 查 DB。RBAC/JWT/bcrypt 留给 0.5.0。
    // 一用户一租户：tenant_id = user_id（admin 种子 = "default"）。

    /// 创建用户（username 全局唯一约束，重复返回 StorageError）。
    async fn create_user(&self, user: &UserRecord) -> Result<(), StorageError>;

    /// 按 user_id 取用户（WS 握手 / token 解析用，tenant-unscoped 主键查询：
    /// user_id 全局唯一，租户解析正是求真租户，task_local 租户不参与——
    /// K8 修订 ADR 2026-10-10 的载重不变量）。
    async fn get_user_by_id(&self, user_id: &str) -> Result<Option<UserRecord>, StorageError>;

    /// 按用户名取用户（登录用，跨租户全局查询，不加 tenant 过滤）。
    async fn get_user_by_username(
        &self,
        username: &str,
    ) -> Result<Option<UserRecord>, StorageError>;

    /// 列全部用户（管理用，跨租户）。
    async fn list_users(&self) -> Result<Vec<UserRecord>, StorageError>;

    /// 更新最近登录时间（登录成功后调）。
    async fn update_last_login(&self, user_id: &str) -> Result<(), StorageError>;

    /// 更新口令哈希与首登改密标记（D1：改密端点/启动明文迁移共用）。
    /// `password_hash` 必须已是 argon2id 哈希（调用方负责）；不存在返回 false。
    async fn update_user_password(
        &self,
        user_id: &str,
        password_hash: &str,
        must_change_password: bool,
    ) -> Result<bool, StorageError>;

    /// 删除用户。不存在返回 false。
    async fn delete_user(&self, user_id: &str) -> Result<bool, StorageError>;
}

/// `list_sessions` 的过滤条件。
///
/// tenant_id 走 task_local（与消息查询一致）。limit 控制返回条数。
/// session_type 用于按 metadata.session_type 过滤（对齐 0.1 list_threads 的 query）。
#[derive(Debug, Clone, Default)]
pub struct SessionListFilter {
    /// 按 metadata.session_type 过滤（如 "main_pipeline"）
    pub session_type: Option<String>,
    /// 最多返回条数
    pub limit: Option<usize>,
}

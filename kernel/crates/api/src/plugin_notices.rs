// @feature: FP-0.2.可观测性 插件不可用系统通知 | @ci: rust-test
//! 插件不可用/恢复系统通知（卡3 通知面，装机 2026-10-17 事故）。
//!
//! 判定来源三路（invoker spawn 熔断跳闸 / host.py `[cohost][member-offline]`
//! 结构性行 / G2 复验超限落 verify_failed），内核经既有 WS `system_notification`
//! 事件直发用户——错误透传的终点是用户，不是日志文件。纯后端：事件形状与
//! 前端既有 `handleSystemNotification` 契约对齐（record_id/pipeline_id/content），
//! 不引前端契约变更。
//!
//! 投递面双路（装机 2026-10-18 补全局跳）：活跃管道（pipeline_state_registry）
//! 逐 thread 发系统消息气泡——用户正在看的会话内联可见；**同时**经
//! `broadcast_event` 广播到全部活跃 WS 连接——前端 `handlePluginUnavailableNotice`
//! 直订消费（record_id 前缀 `n_plugin_unavailable_` 即健康类识别标记，toast +
//! 降级横幅），无活跃管道也必达（组全灭时用户往往无相关活跃会话——单靠内联
//! 路由 = 零感知，1018 装机事故 50 分钟零通知实证）。同一次判定去重闸只过
//! 一次（`NoticeDeduper`），两路共享该决策——双路不会双倍刷屏。
//!
//! 恢复沿：kind=`restored`（熔断宿主 spawn 成功收束时由 invoker 产出）走同
//! 通道同去重窗口，正文与级别随 kind 分派（success/info toast）。
//!
//! 去重：同类事件（kind+members）10 分钟窗口内只发一次——fail-soft 组每次
//! respawn 都会重打 member-offline 行，无窗口会随 respawn 刷屏。

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use agentos_session::SessionCoordinator;

/// 同类事件去重窗口。
pub const NOTICE_DEDUP_WINDOW: Duration = Duration::from_secs(10 * 60);

/// 恢复通知 kind（熔断宿主 spawn 成功收束；同通道同去重窗口）。
pub const RESTORED_KIND: &str = "restored";

/// 内存压力告警 kind（ADR 2026-10-19 D5 第一刀：WARN 阈值触发态**进入沿**，
/// 内核采样循环边沿触发；同通道同去重窗口）。
pub const MEM_PRESSURE_KIND: &str = "mem_pressure";

/// 内存压力解除 kind（**退出沿**，commit/abandoned 回落 WARN 阈值下；
/// 级别随恢复语义走 success，与 [`RESTORED_KIND`] 同档）。
pub const MEM_PRESSURE_CLEARED_KIND: &str = "mem_pressure_cleared";

/// 一条「插件（组）当前不可用」判定通知。
#[derive(Debug, Clone)]
pub struct PluginUnavailableNotice {
    /// 判定种类：`spawn_breaker` / `cohost_member_offline` / `verify_failed` /
    /// `restored`（恢复沿）。
    pub kind: String,
    /// 合宿组名（非组事件为 None）。
    pub group: Option<String>,
    /// 下线成员（通知正文逐个点名）。
    pub members: Vec<String>,
    /// 根因（透传，截断见 [`truncated_reason`]）。
    pub reason: String,
    /// 下线工具数（组 = 成员声明工具数合计）。
    pub tools_down: usize,
}

/// 不可用通知回调（watcher / main 装配注入面）。
pub type UnavailableNotifier = Arc<dyn Fn(&PluginUnavailableNotice) + Send + Sync>;

/// 根因截断（通知正文防堆栈淹没，280 字符对齐 no_assistant_reply_content 口径）。
pub fn truncated_reason(reason: &str) -> String {
    if reason.chars().count() > 280 {
        format!("{}…", reason.chars().take(280).collect::<String>())
    } else {
        reason.to_string()
    }
}

/// 内存压力判定通知构造（ADR 2026-10-19 D5 第一刀：告警进通知中心，跟随
/// spawn 熔断通知同一契约面——payload/level/dedup 全复用，正文与级别按
/// kind 分派，restored 先例）。`cleared=false` 进入沿（`mem_pressure`，
/// error），`true` 退出沿（`mem_pressure_cleared`，success）；`detail` 携带
/// 观测数字（commit/abandoned 对阈值），经 truncated_reason 截断保护。
pub fn mem_pressure_notice(cleared: bool, detail: String) -> PluginUnavailableNotice {
    PluginUnavailableNotice {
        kind: if cleared {
            MEM_PRESSURE_CLEARED_KIND
        } else {
            MEM_PRESSURE_KIND
        }
        .to_string(),
        group: None,
        members: Vec::new(),
        reason: detail,
        tools_down: 0,
    }
}

impl PluginUnavailableNotice {
    /// 通知正文：组名/成员/根因/下线工具数（台账卡3 验收四要素）；恢复沿
    /// （kind=`restored`）正文改为「已恢复，工具重新可用」（reason/tools_down
    /// 恢复语义下无意义，不进正文）。
    ///
    /// 内存压力族（D5 第一刀）：正文 = 判定头 + 观测数字（reason 承载
    /// commit/abandoned 对阈值），不走插件不可用的成员/工具措辞——
    /// members/tools_down 对内存事件无意义（构造时恒空/0，不编造）。
    pub fn content(&self) -> String {
        if self.kind == MEM_PRESSURE_KIND || self.kind == MEM_PRESSURE_CLEARED_KIND {
            let head = if self.kind == MEM_PRESSURE_KIND {
                "内核内存压力告警"
            } else {
                "内核内存压力解除"
            };
            return format!("{head}：{}", truncated_reason(&self.reason));
        }
        let subject = match (&self.group, self.members.as_slice()) {
            (Some(g), []) => format!("插件组 {g}"),
            (Some(g), [only]) => format!("插件组 {g} 成员 {only}"),
            (Some(g), members) => format!("插件组 {g} 成员 {}", members.join("、")),
            (None, [only]) => format!("插件 {only}"),
            (None, members) => format!("插件 {}", members.join("、")),
        };
        if self.kind == RESTORED_KIND {
            return format!("{subject}已恢复，工具重新可用。");
        }
        let tools = if self.tools_down > 0 {
            format!("，{} 个工具已下线并从工具面遮挡", self.tools_down)
        } else {
            String::new()
        };
        format!(
            "{subject}当前不可用（{}）{tools}；修复插件后将自动恢复，详见插件管理页。",
            truncated_reason(&self.reason)
        )
    }

    /// 事件级别：不可用族恒 `error`；恢复沿 `success`（前端 toast 归 info 档，
    /// 与「好消息」语义对齐——见 handlePluginUnavailableNotice 的 severity 映射）。
    /// 内存压力**退出沿**随恢复语义同走 `success`；进入沿与不可用族一致
    /// `error`（走下方兜底，无需特判）。
    pub fn level(&self) -> &'static str {
        if self.kind == RESTORED_KIND || self.kind == MEM_PRESSURE_CLEARED_KIND {
            "success"
        } else {
            "error"
        }
    }

    /// 去重键（同类同成员事件共享窗口）。
    pub fn dedup_key(&self) -> String {
        format!(
            "{}|{}|{}",
            self.kind,
            self.group.clone().unwrap_or_default(),
            self.members.join(",")
        )
    }

    /// `system_notification` 事件载荷（record_id 为唯一 id 来源，前端据它
    /// 去重；缺 record_id 前端拒绝渲染，见 handleSystemNotification 契约）。
    pub fn payload(
        &self,
        pipeline_id: &str,
        thread_id: &str,
        record_id: &str,
    ) -> serde_json::Value {
        serde_json::json!({
            "pipeline_id": pipeline_id,
            "thread_id": thread_id,
            "record_id": record_id,
            "notification_id": self.kind,
            "notificationType": self.kind,
            "level": self.level(),
            "content": self.content(),
            "sequence": null,
        })
    }
}

/// 同类事件去重器（进程级共享；`should_emit` 恰在窗口外返回 true）。
#[derive(Default)]
pub struct NoticeDeduper {
    last_emitted: parking_lot::Mutex<HashMap<String, Instant>>,
}

impl NoticeDeduper {
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    /// 窗口内重复 = false（不投递）；窗口外/首见 = true 并记账。
    pub fn should_emit(&self, key: &str, now: Instant, window: Duration) -> bool {
        let mut map = self.last_emitted.lock();
        match map.get(key) {
            Some(t) if now.duration_since(*t) < window => false,
            _ => {
                map.insert(key.to_string(), now);
                true
            }
        }
    }
}

/// 通知记录 id（每条通知实例唯一；前端按它建消息/去重）。
pub fn record_id(kind: &str, ts_ms: u64) -> String {
    format!("n_plugin_unavailable_{kind}_{ts_ms}")
}

/// 一轮投递的结论（日志消费面）。
pub struct NoticeDelivery {
    /// 是否通过去重闸（false = 窗口内重复，两路都未投）。
    pub passed: bool,
    /// 会话内联路由投递成功 thread 数（0 = 无活跃管道）。
    pub threads: usize,
    /// 全局广播路由投递成功连接数（0 = 无活跃连接）。
    pub broadcast: usize,
}

/// 把判定通知投递给用户（去重闸恰好过一次，两路共享该决策）：
/// ①活跃管道逐 thread 系统消息气泡（内联可见）；②`broadcast_event` 广播到
/// 全部活跃 WS 连接（前端 `handlePluginUnavailableNotice` 直订——toast+降级
/// 横幅，无活跃管道也必达，1018 装机事故缺口）。恢复沿（kind=restored）同
/// 通道同去重。返回 [`NoticeDelivery`] 供调用方日志留痕。
pub async fn emit_notice(
    session: &SessionCoordinator,
    notice: &PluginUnavailableNotice,
    deduper: &NoticeDeduper,
) -> NoticeDelivery {
    if !deduper.should_emit(&notice.dedup_key(), Instant::now(), NOTICE_DEDUP_WINDOW) {
        return NoticeDelivery {
            passed: false,
            threads: 0,
            broadcast: 0,
        };
    }
    let rid = record_id(&notice.kind, now_ms());
    // ①内联路由：活跃管道逐 thread。
    let listings = agentos_session::pipeline_state_registry::global_registry().list();
    let mut threads = 0usize;
    let mut seen_threads = std::collections::HashSet::new();
    for l in listings {
        if l.thread_id.is_empty() || !seen_threads.insert(l.thread_id.clone()) {
            continue;
        }
        if session
            .emit_event(
                &l.thread_id,
                "system_notification",
                notice.payload(&l.pipeline_id, &l.thread_id, &rid),
            )
            .await
        {
            threads += 1;
        }
    }
    // ②全局路由：全连接广播（pipeline_id/thread_id 空串——无管道锚；前端
    // 健康面消费 record_id/content/level/notificationType，不读管道字段）。
    let broadcast = session
        .broadcast_event("system_notification", notice.payload("", "", &rid))
        .await;
    NoticeDelivery {
        passed: true,
        threads,
        broadcast,
    }
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn notice(
        kind: &str,
        group: Option<&str>,
        members: &[&str],
        tools: usize,
    ) -> PluginUnavailableNotice {
        PluginUnavailableNotice {
            kind: kind.to_string(),
            group: group.map(String::from),
            members: members.iter().map(|s| s.to_string()).collect(),
            reason: "ImportError: cannot import name 'WORKSPACE_PREPARE_PHASE'".to_string(),
            tools_down: tools,
        }
    }

    #[test]
    fn content_names_group_members_tools_and_reason() {
        // 组事件四要素（台账卡3：组名/成员/根因/下线工具数）
        let group_case = notice("cohost_member_offline", Some("light"), &["task_submit"], 3);
        let text = group_case.content();
        assert!(text.contains("插件组 light"), "{text}");
        assert!(text.contains("task_submit"), "{text}");
        assert!(text.contains("WORKSPACE_PREPARE_PHASE"), "{text}");
        assert!(text.contains("3 个工具已下线"), "{text}");
        // 独占插件不带组名前缀
        let solo = notice("spawn_breaker", None, &["fs_tools"], 5);
        let text = solo.content();
        assert!(text.starts_with("插件 fs_tools"), "{text}");
        assert!(!text.contains("插件组"), "{text}");
        // 多成员逐个点名
        let multi = notice("spawn_breaker", None, &["a", "b"], 0);
        assert!(multi.content().contains("插件 a、b"), "{}", multi.content());
        // 零工具不提工具数（不编造）
        assert!(
            !multi.content().contains("工具已下线"),
            "{}",
            multi.content()
        );
    }

    #[test]
    fn reason_truncated_at_280_chars() {
        let long = "x".repeat(500);
        let mut n = notice("verify_failed", None, &["p"], 1);
        n.reason = long;
        let text = n.content();
        // 280 + 省略号：正文不再包含 500 连 x
        assert!(text.chars().count() < 500, "根因必须截断");
        assert!(text.contains('…'), "截断须有省略号标记");
        assert_eq!(
            truncated_reason(&"y".repeat(10)),
            "y".repeat(10),
            "短根因原样"
        );
    }

    #[test]
    fn payload_carries_frontend_contract_fields() {
        let n = notice("spawn_breaker", None, &["p"], 2);
        let payload = n.payload("pipe-1", "thread-1", "n_x_1");
        assert_eq!(payload["record_id"], "n_x_1");
        assert_eq!(payload["pipeline_id"], "pipe-1");
        assert_eq!(payload["thread_id"], "thread-1");
        assert_eq!(payload["notificationType"], "spawn_breaker");
        assert_eq!(payload["level"], "error");
        assert!(payload["content"].as_str().unwrap().contains("p"));
    }

    // ── 恢复沿（kind=restored，装机 2026-10-18 补）──

    fn restored(group: Option<&str>, members: &[&str]) -> PluginUnavailableNotice {
        PluginUnavailableNotice {
            kind: RESTORED_KIND.to_string(),
            group: group.map(String::from),
            members: members.iter().map(|s| s.to_string()).collect(),
            reason: String::new(),
            tools_down: 0,
        }
    }

    #[test]
    fn restored_content_announces_recovery_without_reason() {
        let n = restored(Some("light"), &["task_submit", "approval"]);
        let text = n.content();
        assert!(text.contains("插件组 light"), "{text}");
        assert!(text.contains("task_submit"), "{text}");
        assert!(text.contains("已恢复"), "{text}");
        assert!(
            !text.contains("不可用") && !text.contains("根因"),
            "恢复正文不得携带故障措辞: {text}"
        );
        // 独占插件恢复不带组名前缀
        assert!(restored(None, &["fs_tools"])
            .content()
            .starts_with("插件 fs_tools"));
    }

    #[test]
    fn restored_level_is_success_and_unavailable_stays_error() {
        assert_eq!(restored(Some("light"), &["p"]).level(), "success");
        assert_eq!(notice("spawn_breaker", None, &["p"], 1).level(), "error");
        assert_eq!(
            restored(Some("light"), &["p"]).payload("pipe", "t", "n_r_1")["level"],
            "success",
            "payload level 随 kind 分派（前端 toast 归 info 档）"
        );
    }

    #[test]
    fn restored_dedup_key_is_disjoint_from_breaker() {
        // 同组同成员：不可用与恢复是两类事件，去重窗口互不压制
        let breaker = notice("spawn_breaker", Some("light"), &["p"], 1);
        let back = restored(Some("light"), &["p"]);
        assert_ne!(breaker.dedup_key(), back.dedup_key());
    }

    // ── 双路投递（内联 + 全局广播；装机 2026-10-18 缺口的主修复面）──

    use std::sync::atomic::{AtomicU64, Ordering};

    struct RecordingSink {
        id: u64,
        received: std::sync::Arc<std::sync::Mutex<Vec<serde_json::Value>>>,
    }

    #[async_trait::async_trait]
    impl agentos_session::EventSink for RecordingSink {
        async fn send_text(&self, text: &str) -> bool {
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(text) {
                self.received.lock().unwrap().push(v);
            }
            true
        }
        fn id(&self) -> u64 {
            self.id
        }
    }

    fn sink() -> (
        Arc<RecordingSink>,
        std::sync::Arc<std::sync::Mutex<Vec<serde_json::Value>>>,
    ) {
        static NEXT: AtomicU64 = AtomicU64::new(1);
        let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        (
            Arc::new(RecordingSink {
                id: NEXT.fetch_add(1, Ordering::SeqCst),
                received: received.clone(),
            }),
            received,
        )
    }

    /// 无活跃管道（用户无相关会话——1018 事故形态）：内联路由 0 投递，
    /// 全局广播仍必达（前端直订 system_notification → toast+降级横幅）。
    #[tokio::test]
    async fn emit_notice_broadcasts_without_active_pipelines() {
        let session = Arc::new(agentos_session::SessionCoordinator::new());
        let (sink, received) = sink();
        session.register("u-global", sink);
        let deduper = NoticeDeduper::new();

        let delivery = emit_notice(
            &session,
            &notice("spawn_breaker", Some("light"), &["task_submit"], 3),
            &deduper,
        )
        .await;
        assert!(delivery.passed);
        assert_eq!(delivery.threads, 0, "无活跃管道 → 内联 0");
        assert_eq!(delivery.broadcast, 1, "全局广播必达（缺口修复面）");

        let frames = received.lock().unwrap();
        assert_eq!(frames.len(), 1);
        assert_eq!(frames[0]["type"], "system_notification");
        assert_eq!(frames[0]["data"]["level"], "error");
        assert!(
            frames[0]["data"]["record_id"]
                .as_str()
                .unwrap()
                .starts_with("n_plugin_unavailable_spawn_breaker_"),
            "record_id 前缀 = 前端健康类识别标记"
        );
    }

    /// 有活跃管道：内联与广播双路都投（同一次判定、同一去重决策——不会双闸）。
    #[tokio::test]
    async fn emit_notice_delivers_both_routes_with_active_pipeline() {
        let session = Arc::new(agentos_session::SessionCoordinator::new());
        let (sink, received) = sink();
        session.register("u-both", sink);
        session.register_thread("thread-both", "u-both");
        agentos_session::pipeline_state_registry::global_registry().get_or_init(
            "default",
            "pipe-both",
            "thread-both",
            "agent-both",
            serde_json::json!({}),
        );
        let deduper = NoticeDeduper::new();

        let delivery = emit_notice(
            &session,
            &notice("spawn_breaker", None, &["fs_tools"], 2),
            &deduper,
        )
        .await;
        assert!(delivery.passed);
        assert_eq!(delivery.threads, 1, "内联 1 thread");
        assert_eq!(delivery.broadcast, 1, "全局 1 连接");

        {
            let frames = received.lock().unwrap();
            assert_eq!(frames.len(), 2, "同连接收内联帧+广播帧各一份");
            // 内联帧带管道锚；广播帧管道字段空串（无锚不伪造）
            let with_pipe = frames
                .iter()
                .find(|f| f["data"]["pipeline_id"] == "pipe-both")
                .expect("内联帧必须带 pipeline_id");
            assert_eq!(with_pipe["data"]["thread_id"], "thread-both");
        }

        // 去重窗口内第二次判定：两路都静默（双路不会双倍刷屏）
        let second = emit_notice(
            &session,
            &notice("spawn_breaker", None, &["fs_tools"], 2),
            &deduper,
        )
        .await;
        assert!(!second.passed);
        assert_eq!(second.threads + second.broadcast, 0);
    }

    #[test]
    fn deduper_suppresses_within_window_only() {
        let d = NoticeDeduper::default();
        let t0 = Instant::now();
        assert!(d.should_emit("k", t0, Duration::from_secs(60)));
        assert!(!d.should_emit("k", t0 + Duration::from_secs(1), Duration::from_secs(60)));
        assert!(!d.should_emit("k", t0 + Duration::from_secs(59), Duration::from_secs(60)));
        assert!(
            d.should_emit("k", t0 + Duration::from_secs(60), Duration::from_secs(60)),
            "窗口滑出后放行"
        );
        // 键隔离：不同类事件互不压制
        assert!(d.should_emit("other", t0, Duration::from_secs(60)));
    }

    #[test]
    fn record_id_embeds_kind_and_is_ts_parameterized() {
        let a = record_id("spawn_breaker", 1);
        let b = record_id("spawn_breaker", 2);
        assert_ne!(a, b, "不同实例 id 不同（前端按 id 去重）");
        assert!(a.starts_with("n_plugin_unavailable_spawn_breaker_"));
    }

    // ── 内存压力族（D5 第一刀：告警进通知中心）──

    #[test]
    fn mem_pressure_content_level_and_payload_contract() {
        // 进入沿：告警头 + 观测数字，error 级
        let enter = mem_pressure_notice(false, "commit 9000MB 超 8192MB".to_string());
        assert_eq!(enter.kind, MEM_PRESSURE_KIND);
        let text = enter.content();
        assert!(text.contains("内核内存压力告警"), "{text}");
        assert!(text.contains("9000MB"), "观测数字进正文: {text}");
        assert_eq!(enter.level(), "error");
        // 退出沿：解除头，success 级（恢复语义同档）
        let cleared = mem_pressure_notice(true, "commit 500MB 回落".to_string());
        assert_eq!(cleared.kind, MEM_PRESSURE_CLEARED_KIND);
        assert!(cleared.content().contains("内核内存压力解除"));
        assert_eq!(cleared.level(), "success");
        // 边沿两类事件去重键互不压制（restored/breaker 同判）
        assert_ne!(enter.dedup_key(), cleared.dedup_key());
        // payload 契约字段照常（前端 handleSystemNotification 通用消费）
        let payload = enter.payload("pipe", "thread", "n_m_1");
        assert_eq!(payload["level"], "error");
        assert_eq!(payload["notificationType"], MEM_PRESSURE_KIND);
        assert_eq!(payload["record_id"], "n_m_1");
        assert!(payload["content"].as_str().unwrap().contains("commit"));
    }
}

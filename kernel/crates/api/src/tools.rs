//! 工具执行失败告警器（2026-08-23）。
//!
//! 真机教训：omnisearch `universal_search` 因 manifest 缺 input_schema 被注册成
//! 零参数工具，LLM 每轮调用都被服务端 pydantic 校验拒绝（`mode Field required`），
//! 日志里逐条 pydantic 错误淹没在流水里，没有闸门把「同一工具连续 N 次失败」
//! 汇总成一条可操作的告警——调研 agent 空转 45 万 token、42 分钟无人察觉。
//!
//! 本器挂在 tool-executor.invoke 结果归一化处：工具返回 `success=false`（参数
//! 校验失败/执行错误）时计数，同工具连续失败达到阈值输出一条 `tracing::error`
//! （含失败片段与持续秒数）；成功一次即清零（工具恢复不重复轰炸）。并发安全，
//! 进程内单例语义。

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// 连续失败告警阈值：同工具连续失败达到该次数即告警。
pub const FAILURE_ALERT_THRESHOLD: u32 = 5;

/// 告警冷却窗口：告警后同工具再次达标，至少间隔该时长才再告警
/// （防 LLM 反复选择同一坏工具时日志刷屏）。
const ALERT_COOLDOWN: Duration = Duration::from_secs(300);

/// 单工具的连续失败状态。
#[derive(Debug)]
struct FailureState {
    consecutive: u32,
    first_failure_at: Instant,
    last_alert_at: Option<Instant>,
    last_error_sample: String,
}

/// 告警载荷（人读摘要）。
#[derive(Debug, Clone)]
pub struct ToolFailureAlert {
    pub tool_name: String,
    pub consecutive: u32,
    pub error_sample: String,
    /// 自首次失败以来的秒数。
    pub since_secs: u64,
}

/// 工具失败追踪器 trait（可 mock，测试注入）。
pub trait ToolFailureTracker: Send + Sync {
    /// 记录一次调用结果。`success=false` 计为失败；true 清零。
    /// 达到阈值且出冷却窗口时返回告警载荷（否则 None）。
    fn record(
        &self,
        tool_name: &str,
        success: bool,
        error_sample: &str,
    ) -> Option<ToolFailureAlert>;
}

/// 默认实现：进程内 HashMap 计数。
#[derive(Default)]
pub struct ConsecutiveFailureTracker {
    inner: Mutex<HashMap<String, FailureState>>,
}

impl ToolFailureTracker for ConsecutiveFailureTracker {
    fn record(
        &self,
        tool_name: &str,
        success: bool,
        error_sample: &str,
    ) -> Option<ToolFailureAlert> {
        let mut map = self.inner.lock().unwrap();
        if success {
            map.remove(tool_name);
            return None;
        }
        let now = Instant::now();
        let sample = if error_sample.is_empty() {
            "(无错误详情)".to_string()
        } else {
            error_sample.chars().take(160).collect()
        };
        let entry = map
            .entry(tool_name.to_string())
            .or_insert_with(|| FailureState {
                consecutive: 0,
                first_failure_at: now,
                last_alert_at: None,
                last_error_sample: sample.clone(),
            });
        entry.consecutive += 1;
        entry.last_error_sample = sample;
        if entry.consecutive < FAILURE_ALERT_THRESHOLD {
            return None;
        }
        if let Some(last) = entry.last_alert_at {
            if now.duration_since(last) < ALERT_COOLDOWN {
                return None;
            }
        }
        entry.last_alert_at = Some(now);
        Some(ToolFailureAlert {
            tool_name: tool_name.to_string(),
            consecutive: entry.consecutive,
            error_sample: entry.last_error_sample.clone(),
            since_secs: now.duration_since(entry.first_failure_at).as_secs(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn consecutive_failures_alert_at_threshold() {
        let t = ConsecutiveFailureTracker::default();
        for i in 1..FAILURE_ALERT_THRESHOLD {
            assert!(
                t.record("universal_search", false, "mode Field required")
                    .is_none(),
                "阈值前不告警（第 {i} 次）"
            );
        }
        let alert = t
            .record("universal_search", false, "mode Field required")
            .expect("达到阈值应告警");
        assert_eq!(alert.tool_name, "universal_search");
        assert_eq!(alert.consecutive, FAILURE_ALERT_THRESHOLD);
        assert!(alert.error_sample.contains("mode"));
    }

    #[test]
    fn success_resets_consecutive_count() {
        let t = ConsecutiveFailureTracker::default();
        for _ in 0..3 {
            t.record("bash_execute", false, "boom");
        }
        t.record("bash_execute", true, "");
        // 清零后重新计数：再来 4 次失败也不到阈值（4 < 5）
        for _ in 0..4 {
            assert!(t.record("bash_execute", false, "boom").is_none());
        }
        let alert = t
            .record("bash_execute", false, "boom")
            .expect("清零后重新累计达标");
        assert_eq!(alert.consecutive, 5);
    }

    #[test]
    fn alert_cooldown_prevents_spam() {
        let t = ConsecutiveFailureTracker::default();
        for _ in 0..4 {
            t.record("bad_tool", false, "x");
        }
        assert!(
            t.record("bad_tool", false, "x").is_some(),
            "第 5 次连续失败应告警"
        );
        // 冷却窗口内再失败不重复告警
        for _ in 0..5 {
            assert!(
                t.record("bad_tool", false, "x").is_none(),
                "冷却期内不重复告警"
            );
        }
    }

    #[test]
    fn distinct_tools_count_independently() {
        let t = ConsecutiveFailureTracker::default();
        for _ in 0..4 {
            t.record("tool_a", false, "a");
            t.record("tool_b", false, "b");
        }
        let a = t.record("tool_a", false, "a");
        let b = t.record("tool_b", false, "b");
        assert_eq!(a.as_ref().map(|x| x.tool_name.as_str()), Some("tool_a"));
        assert_eq!(b.as_ref().map(|x| x.tool_name.as_str()), Some("tool_b"));
    }
}

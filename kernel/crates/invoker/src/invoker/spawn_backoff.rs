// @feature: FP-0.2.一 spawn 失败指数退避+按宿主键熔断账本 | @ci: rust-test
//! spawn 失败指数退避 + 熔断账本（按宿主键记账）。
//!
//! 坏插件宿主此前每次调用/每轮复验都无条件重试 spawn，失败无限堆积（装机
//! 2026-10-17 实证：合宿组 ImportError 循环 535 次）。本账本补上升级路径：
//!
//! - **指数退避**：连续失败后 1s 起步倍增（60s 封顶），窗口内到达的调用
//!   fail-fast（`HOST_SPAWN_BACKOFF`），不再每调用一个新进程；
//! - **熔断**：连续失败达阈值（默认 5，`AGENTOS_SPAWN_BREAKER_THRESHOLD`
//!   可配）即停自动 spawn（`HOST_SPAWN_BREAKER_OPEN`），错误携带末次根因
//!   与恢复指引；跨线时返回 `true` 恰好一次（通知面挂点）；
//! - **恢复通道不设限**：代码/声明指纹较上次失败时变化（用户动手修）即
//!   自动重置放行；spawn 成功或宿主显式卸载同样清账。熔断的是"对未修复
//!   插件的自动重试"，不是修复本身。
//!
//! [来源: 装机插件组事故台账 20261017 卡2 / K8s CrashLoopBackOff 同构语义]

use std::collections::HashMap;
use std::time::{Duration, Instant};

use parking_lot::Mutex;

/// 退避延迟：`1s * 2^(n-1)`，60s 封顶（n = 连续失败次数；n=0 不应出现）。
pub(super) fn backoff_delay(consecutive: u32) -> Duration {
    let shift = consecutive.saturating_sub(1).min(6);
    Duration::from_secs(1u64.checked_shl(shift).unwrap_or(u64::MAX).min(60))
}

/// 熔断阈值解析（纯函数，单测面）：非法/缺省/<1 → 默认 5。
fn parse_breaker_threshold(raw: Option<&str>) -> u32 {
    raw.and_then(|s| s.trim().parse::<u32>().ok())
        .filter(|&v| v >= 1)
        .unwrap_or(5)
}

/// 熔断阈值（进程级一次解析）：`AGENTOS_SPAWN_BREAKER_THRESHOLD`，默认 5。
pub(super) fn spawn_breaker_threshold() -> u32 {
    static THRESHOLD: std::sync::OnceLock<u32> = std::sync::OnceLock::new();
    *THRESHOLD.get_or_init(|| {
        parse_breaker_threshold(
            std::env::var("AGENTOS_SPAWN_BREAKER_THRESHOLD")
                .ok()
                .as_deref(),
        )
    })
}

/// 单宿主的失败连击记录。
#[derive(Debug, Clone)]
struct Entry {
    consecutive: u32,
    last_fail: Instant,
    /// 末次失败时的插件代码/声明指纹——变化即视为用户已动手修，重置放行。
    last_fail_fp: u64,
    /// 末次失败的错误摘要（熔断报错携带，fail-fast 不丢根因）。
    last_error: String,
}

/// spawn 前闸门判定结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum GateDecision {
    /// 放行（无记录 / 退避窗已过 / 指纹变化已重置）。
    Allow,
    /// 退避窗口内：距可重试的剩余等待。
    Backoff { retry_after: Duration },
    /// 熔断已开：停止自动 spawn。
    BreakerOpen,
}

#[derive(Debug, Default)]
pub(super) struct SpawnBackoffLedger {
    entries: Mutex<HashMap<String, Entry>>,
    /// 熔断阈值（new() 读 env 定型；测试用 with_threshold 注入短值）。
    threshold: u32,
}

impl SpawnBackoffLedger {
    pub(super) fn new() -> Self {
        Self {
            entries: Mutex::new(HashMap::new()),
            threshold: spawn_breaker_threshold(),
        }
    }

    #[cfg(test)]
    pub(super) fn with_threshold(threshold: u32) -> Self {
        Self {
            entries: Mutex::new(HashMap::new()),
            threshold,
        }
    }

    /// spawn 前闸门（持 spawn 锁后调用）：退避/熔断判定 + 指纹变化自动重置。
    pub(super) fn gate(&self, host_key: &str, current_fp: u64) -> GateDecision {
        let mut entries = self.entries.lock();
        let Some(e) = entries.get(host_key) else {
            return GateDecision::Allow;
        };
        if e.last_fail_fp != current_fp {
            // 指纹变化 = 用户动手修（或插件被同步/重装）：清账放行，恢复不设限。
            entries.remove(host_key);
            return GateDecision::Allow;
        }
        if e.consecutive >= self.threshold {
            return GateDecision::BreakerOpen;
        }
        let wait = backoff_delay(e.consecutive);
        match wait.checked_sub(e.last_fail.elapsed()) {
            Some(remaining) => GateDecision::Backoff {
                retry_after: remaining,
            },
            None => GateDecision::Allow,
        }
    }

    /// 记一次 spawn 失败。返回是否恰好跨线熔断（true 仅一次，通知面挂点）。
    pub(super) fn record_failure(&self, host_key: &str, fp: u64, error: &str) -> bool {
        let mut entries = self.entries.lock();
        let e = entries
            .entry(host_key.to_string())
            .or_insert_with(|| Entry {
                consecutive: 0,
                last_fail: Instant::now(),
                last_fail_fp: fp,
                last_error: String::new(),
            });
        e.consecutive += 1;
        e.last_fail = Instant::now();
        e.last_fail_fp = fp;
        e.last_error = error.to_string();
        e.consecutive == self.threshold
    }

    /// spawn 成功：清账（下次失败从 1 重计）。返回本次成功是否收束了一次
    /// **已熔断**的宿主（true 恰好一次——恢复通知面挂点，卡3 恢复沿）。
    pub(super) fn record_success(&self, host_key: &str) -> bool {
        self.entries
            .lock()
            .remove(host_key)
            .is_some_and(|e| e.consecutive >= self.threshold)
    }

    /// 宿主显式卸载/驱逐：清账（操作者介入通道——重启用即重开预算）。
    pub(super) fn reset(&self, host_key: &str) {
        self.entries.lock().remove(host_key);
    }

    /// 熔断态宿主的末次失败摘要（fail-fast 报错携带，不丢根因）。
    pub(super) fn last_error(&self, host_key: &str) -> Option<String> {
        self.entries
            .lock()
            .get(host_key)
            .map(|e| e.last_error.clone())
    }

    /// 当前连击数（跳闸通知携带；无记录 = 0）。
    pub(super) fn consecutive(&self, host_key: &str) -> u32 {
        self.entries
            .lock()
            .get(host_key)
            .map(|e| e.consecutive)
            .unwrap_or(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_delay_doubles_and_caps_at_60s() {
        assert_eq!(backoff_delay(1), Duration::from_secs(1));
        assert_eq!(backoff_delay(2), Duration::from_secs(2));
        assert_eq!(backoff_delay(3), Duration::from_secs(4));
        assert_eq!(backoff_delay(4), Duration::from_secs(8));
        // 单调不减 + 封顶（性质断言：任意大 n 不超 60s）
        let mut prev = Duration::ZERO;
        for n in 1..=20u32 {
            let d = backoff_delay(n);
            assert!(d >= prev, "退避序列须单调不减: n={n}");
            assert!(d <= Duration::from_secs(60), "退避封顶 60s: n={n}");
            prev = d;
        }
        assert_eq!(backoff_delay(20), Duration::from_secs(60));
    }

    #[test]
    fn breaker_threshold_parses_default_override_invalid_and_zero() {
        assert_eq!(parse_breaker_threshold(None), 5);
        assert_eq!(parse_breaker_threshold(Some("")), 5);
        assert_eq!(parse_breaker_threshold(Some("3")), 3);
        assert_eq!(parse_breaker_threshold(Some(" 9 ")), 9);
        assert_eq!(parse_breaker_threshold(Some("abc")), 5);
        assert_eq!(parse_breaker_threshold(Some("0")), 5);
        assert_eq!(parse_breaker_threshold(Some("-2")), 5);
    }

    #[test]
    fn first_failure_allows_but_second_within_window_backs_off() {
        let ledger = SpawnBackoffLedger::with_threshold(5);
        assert_eq!(ledger.gate("h", 7), GateDecision::Allow, "无记录放行");
        assert!(!ledger.record_failure("h", 7, "boom-1"));
        // 首败后立即重试：1s 退避窗内 → fail-fast
        match ledger.gate("h", 7) {
            GateDecision::Backoff { retry_after } => {
                assert!(retry_after <= Duration::from_secs(1));
            }
            other => panic!("退避窗内应 Backoff，实际 {other:?}"),
        }
    }

    #[test]
    fn breaker_opens_at_threshold_exactly_once_and_carries_last_error() {
        let ledger = SpawnBackoffLedger::with_threshold(3);
        assert!(!ledger.record_failure("h", 7, "e1"));
        assert!(!ledger.record_failure("h", 7, "e2"));
        assert!(
            ledger.record_failure("h", 7, "e3-root-cause"),
            "跨线那次必须返回 true（通知面恰好一次）"
        );
        assert_eq!(ledger.gate("h", 7), GateDecision::BreakerOpen);
        assert_eq!(
            ledger.gate("h", 7),
            GateDecision::BreakerOpen,
            "熔断态稳定，不随重复判定抖动"
        );
        assert_eq!(
            ledger.last_error("h").as_deref(),
            Some("e3-root-cause"),
            "熔断报错携带末次根因"
        );
    }

    #[test]
    fn fingerprint_change_resets_ledger_and_allows_recovery() {
        let ledger = SpawnBackoffLedger::with_threshold(3);
        assert!(!ledger.record_failure("h", 100, "broken"));
        assert!(!ledger.record_failure("h", 100, "broken"));
        assert!(
            ledger.record_failure("h", 100, "broken"),
            "阈值 3：第三败跨线"
        );
        assert_eq!(ledger.gate("h", 100), GateDecision::BreakerOpen);
        // 指纹变化（用户修复）→ 清账放行，且清账生效（同指纹再判仍 Allow）
        assert_eq!(ledger.gate("h", 200), GateDecision::Allow);
        assert_eq!(ledger.gate("h", 200), GateDecision::Allow);
        // 修复后的新失败从 1 重计，不继承旧连击
        assert!(!ledger.record_failure("h", 200, "still broken"));
        match ledger.gate("h", 200) {
            GateDecision::Backoff { retry_after } => {
                assert!(retry_after <= Duration::from_secs(1))
            }
            other => panic!("修复后失败应从 1 重计（1s 退避），实际 {other:?}"),
        }
    }

    #[test]
    fn success_and_reset_clear_entries() {
        let ledger = SpawnBackoffLedger::with_threshold(3);
        assert!(!ledger.record_failure("h1", 7, "x"));
        ledger.record_success("h1");
        assert_eq!(ledger.gate("h1", 7), GateDecision::Allow, "成功清账");

        assert!(!ledger.record_failure("h2", 7, "x"));
        assert!(!ledger.record_failure("h2", 7, "x"));
        assert!(
            ledger.record_failure("h2", 7, "x"),
            "阈值 3：第三败跨线熔断"
        );
        assert_eq!(ledger.gate("h2", 7), GateDecision::BreakerOpen);
        ledger.reset("h2");
        assert_eq!(ledger.gate("h2", 7), GateDecision::Allow, "显式重置清账");
    }

    #[test]
    fn hosts_are_isolated() {
        let ledger = SpawnBackoffLedger::with_threshold(2);
        assert!(!ledger.record_failure("host_a", 7, "a"));
        assert!(
            ledger.record_failure("host_a", 7, "a"),
            "阈值 2：A 宿主第二败跨线"
        );
        assert_eq!(
            ledger.gate("host_b", 7),
            GateDecision::Allow,
            "宿主键隔离：A 熔断不连坐 B"
        );
    }
}
// 覆盖补丁（本模块 tests 已在上文 cfg(test) mod 内，此测试并入其断言域——
// 独立 mod 以避免触碰既有测试块布局）：
#[cfg(test)]
mod recovery_tests {
    use super::*;

    /// 卡3 恢复沿：record_success 返回「本次成功是否收束了一次已熔断宿主」——
    /// true 恰好一次（恢复通知挂点）；未熔断的普通清账与无记录成功均 false
    /// （不通知＝无降级可恢复）。
    #[test]
    fn success_after_breaker_reports_recovery_exactly_once() {
        let ledger = SpawnBackoffLedger::with_threshold(3);
        // 未熔断的成功：false（无故障即无恢复通知）
        assert!(!ledger.record_failure("h_ok", 7, "x"));
        assert!(!ledger.record_success("h_ok"), "普通清账不是恢复");
        // 熔断后成功：恰好一次 true
        ledger.record_failure("h_trip", 7, "a");
        ledger.record_failure("h_trip", 7, "b");
        assert!(ledger.record_failure("h_trip", 7, "c"), "跨线熔断");
        assert!(
            ledger.record_success("h_trip"),
            "熔断宿主的成功 = 恢复沿（true）"
        );
        assert!(
            !ledger.record_success("h_trip"),
            "账已清，再次成功不再是恢复沿"
        );
        // 无记录宿主的成功：false
        assert!(!ledger.record_success("h_ghost"));
    }
}

// @feature: FP-0.2.可观测性 合宿成员下线 stderr 结构行解析(卡3 通知面) | @ci: rust-test
//! 合宿宿主 stderr 结构性行解析（卡3 通知面）。
//!
//! host.py 成员加载 fail-soft（装机 2026-10-17 事故卡1）对单成员失败打
//! `[cohost][member-offline] group=.. slot=.. plugin=.. stage=.. error=..`
//! 结构性行——内核 stderr 转发处解析本行即得"哪个组哪个成员因什么下线"，
//! 直走系统通知面（错误透传的终点是用户，不是日志文件）。
//!
//! 解析契约：`error=` 为末键，取**行剩余全文**（根因含空格/等号不截断）；
//! 前四键按空格分隔的 `key=value` 匹配。非本族行返回 None。

/// 一条成员下线记录（host.py main() 的 fail-soft 产出）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MemberOfflineNotice {
    pub group: String,
    pub slot: String,
    pub plugin: String,
    pub stage: String,
    pub error: String,
}

/// 行前缀（host.py 输出契约）。
pub const MEMBER_OFFLINE_PREFIX: &str = "[cohost][member-offline] ";

/// 解析 `[cohost][member-offline]` 结构性行；非本族行 / 缺键返回 None。
pub fn parse_member_offline_line(line: &str) -> Option<MemberOfflineNotice> {
    let rest = line.trim().strip_prefix(MEMBER_OFFLINE_PREFIX)?;
    // error 为末键：先定位它，其余键在前段按 `key=value` 逐个取。
    let (head, error) = match rest.split_once("error=") {
        Some((head, error)) => (head, error),
        None => return None,
    };
    let mut group = None;
    let mut slot = None;
    let mut plugin = None;
    let mut stage = None;
    for token in head.split_whitespace() {
        if let Some(v) = token.strip_prefix("group=") {
            group = Some(v.to_string());
        } else if let Some(v) = token.strip_prefix("slot=") {
            slot = Some(v.to_string());
        } else if let Some(v) = token.strip_prefix("plugin=") {
            plugin = Some(v.to_string());
        } else if let Some(v) = token.strip_prefix("stage=") {
            stage = Some(v.to_string());
        }
    }
    // 空串键视同缺失（fail-soft 行的四键都非空才是有效记录）。
    let nonempty = |v: Option<String>| v.filter(|s| !s.is_empty());
    Some(MemberOfflineNotice {
        group: nonempty(group)?,
        slot: nonempty(slot)?,
        plugin: nonempty(plugin)?,
        stage: nonempty(stage)?,
        error: error.trim().to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_wellformed_line_with_spaces_and_equals_in_error() {
        let n = parse_member_offline_line(
            "[cohost][member-offline] group=light slot=0 plugin=task_submit \
             stage=load error=ImportError: cannot import name 'WORKSPACE_PREPARE_PHASE' \
             from '_root_api' (x=1, y=2)",
        )
        .expect("标准行必须可解析");
        assert_eq!(n.group, "light");
        assert_eq!(n.slot, "0");
        assert_eq!(n.plugin, "task_submit");
        assert_eq!(n.stage, "load");
        assert!(
            n.error.contains("WORKSPACE_PREPARE_PHASE"),
            "error 段保留全文（含空格与等号）: {}",
            n.error
        );
        assert!(n.error.contains("x=1, y=2"));
    }

    #[test]
    fn rejects_non_family_and_malformed_lines() {
        // 非本族行（host.py 心跳/普通日志/空行）
        assert!(parse_member_offline_line("[cohost] group=light members=3 offline=0").is_none());
        assert!(parse_member_offline_line("#HB 1730000000000").is_none());
        assert!(parse_member_offline_line("").is_none());
        // 本族但缺 error 键
        assert!(parse_member_offline_line(
            "[cohost][member-offline] group=g slot=0 plugin=p stage=s"
        )
        .is_none());
        // 缺业务键
        assert!(parse_member_offline_line("[cohost][member-offline] error=boom").is_none());
    }

    #[test]
    fn empty_key_values_are_treated_as_missing() {
        assert!(
            parse_member_offline_line(
                "[cohost][member-offline] group= slot=0 plugin=p stage=s error=boom"
            )
            .is_none(),
            "空 group 视同缺失（无效记录不进通知面）"
        );
    }

    #[test]
    fn parser_is_total_never_panics_on_hostile_input() {
        // 性质断言：任意输入不 panic，解析结果 error 段与原文末段一致
        for line in [
            "[cohost][member-offline] error=",
            "[cohost][member-offline] group=g error=e",
        ] {
            if let Some(n) = parse_member_offline_line(line) {
                assert!(line.ends_with(&n.error) || n.error.is_empty());
            }
        }
    }
}

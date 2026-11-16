// @feature: FP-0.2.可观测性 WS 事件名单单源 | @ci: rust-test
//! WS 事件名单单源（config/kernel/ws_events.yaml，ADR 2026-10-10）。
//!
//! 单源契约：内核消费面（流式路由族 + 域事件镜像名单）与前端
//! WS_SERVER_EVENTS 同读该文件（前端经 Vite ?raw 内嵌，双侧各自解析同一
//! 物理文件——真单源，无生成物投影）。include_str! 编译期内嵌：文件缺失
//! = 编译错误；解析失败 = 首次访问 expect panic（fail-closed，禁止静默
//! 回落空名单）。名单改动 = 改该文件 + 重编，双侧契约测试各自解析对账。

use std::sync::OnceLock;

/// 名单文件原文（编译期内嵌：与前端共享的唯一真值载体，文件缺失即编译失败）。
const WS_EVENTS_YAML: &str = include_str!("../../../../config/kernel/ws_events.yaml");

/// 名单文档。serde 只消费内核需要的两节；server_events 节归前端（未知字段忽略）。
#[derive(serde::Deserialize, Debug)]
struct WsEventsDoc {
    ws_events: WsEvents,
}

/// 内核消费的两节名单。
#[derive(serde::Deserialize, Debug)]
struct WsEvents {
    /// 流式路由族（capability_router try_route_stream_family 白名单）。
    stream_family: Vec<String>,
    /// 域事件镜像（forward_unmatched_event 分流进域事件总线的名单）。
    domain_mirrors: Vec<String>,
}

/// 解析后的名单（进程级缓存；serde 失败启动即炸，不回落空名单）。
fn doc() -> &'static WsEvents {
    static DOC: OnceLock<WsEvents> = OnceLock::new();
    DOC.get_or_init(|| {
        serde_yaml::from_str::<WsEventsDoc>(WS_EVENTS_YAML)
            .expect("config/kernel/ws_events.yaml 解析失败（WS 事件名单单源 fail-closed）")
            .ws_events
    })
}

/// 事件是否属流式路由族。
pub fn is_stream_family(event_name: &str) -> bool {
    doc().stream_family.iter().any(|e| e == event_name)
}

/// 事件是否属域事件镜像名单（分流进域事件总线）。
pub fn is_domain_mirror(event_name: &str) -> bool {
    doc().domain_mirrors.iter().any(|e| e == event_name)
}

/// 域事件镜像名单（只读；消费面契约测试用——证明路由行为跟随单源名单）。
pub fn domain_mirrors() -> &'static [String] {
    &doc().domain_mirrors
}

#[cfg(test)]
mod tests {
    //! 单源机械闸：内嵌解析 == 磁盘解析（防有人把 include_str 换成过期字面量
    //! 或内联副本）+ 名单性质（非空/无重复/非空串）+ 谓词行为（正例取自名单
    //! 本身——名单改名测试不红；负例用字面量证明不误收）。

    use super::*;

    /// 真实仓库名单文件（kernel/crates/api → 上溯三级 = 仓库根）。
    fn repo_ws_events_path() -> std::path::PathBuf {
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../config/kernel/ws_events.yaml")
    }

    fn parse(text: &str) -> WsEvents {
        serde_yaml::from_str::<WsEventsDoc>(text)
            .expect("测试解析失败")
            .ws_events
    }

    #[test]
    fn embedded_parse_equals_disk_file() {
        let disk = std::fs::read_to_string(repo_ws_events_path())
            .expect("config/kernel/ws_events.yaml 应存在于仓库");
        let from_disk = parse(&disk);
        assert_eq!(doc().stream_family, from_disk.stream_family);
        assert_eq!(doc().domain_mirrors, from_disk.domain_mirrors);
    }

    #[test]
    fn lists_are_nonempty_unique_nonblank() {
        for name in doc()
            .stream_family
            .iter()
            .chain(doc().domain_mirrors.iter())
        {
            assert!(!name.is_empty(), "名单条目不得为空串：{name:?}");
        }
        let mut fam = doc().stream_family.clone();
        fam.sort();
        fam.dedup();
        assert_eq!(
            fam.len(),
            doc().stream_family.len(),
            "stream_family 不得重复"
        );
        let mut mir = doc().domain_mirrors.clone();
        mir.sort();
        mir.dedup();
        assert_eq!(
            mir.len(),
            doc().domain_mirrors.len(),
            "domain_mirrors 不得重复"
        );
    }

    #[test]
    fn membership_predicates_follow_the_list() {
        // 正例全部来自名单本身（单源驱动：改名后本测试依旧成立）
        for name in &doc().stream_family {
            assert!(is_stream_family(name), "stream_family 成员应命中: {name}");
        }
        for name in &doc().domain_mirrors {
            assert!(is_domain_mirror(name), "domain_mirrors 成员应命中: {name}");
        }
        // 负例：非名单词与空串不得误收
        for name in ["widget_feedback", "stream_start", "approval.taken", ""] {
            assert!(!is_stream_family(name), "非流式族事件误收: {name}");
            assert!(!is_domain_mirror(name), "非镜像事件误收: {name}");
        }
        // 交叉性质：两节交集不得出现（流式族不走域镜像分流）
        for name in &doc().stream_family {
            assert!(
                !doc().domain_mirrors.contains(name),
                "stream_family 与 domain_mirrors 不得交叉: {name}"
            );
        }
    }

    #[test]
    fn malformed_yaml_fails_closed() {
        // 性质断言：坏 yaml 不产生空名单妥协，而是显式报错
        let bad = serde_yaml::from_str::<WsEventsDoc>("ws_events: [not, an, object]");
        assert!(bad.is_err(), "结构错误必须显式失败（fail-closed）");
    }
}

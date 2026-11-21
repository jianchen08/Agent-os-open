// @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: rust-test
//! 已删除管道墓碑（进程级内存集合）：会话/任务删除提交后登记，写路径据此
//! 丢弃迟到写入。
//!
//! ## 为什么需要
//!
//! 删除是"数据面清空"，但管道 run 是内存里的异步执行——删会话不会让在途
//! run 立刻停手。实测形态（2026-11-10 装机）：会话删除后，仍在运行的任务
//! 管道把整份 state 重新 upsert 回库、并用已删会话 id 派生了新的子任务管道，
//! 管理页于是出现"删了还在"的孤儿。
//!
//! 墓碑 = 删除事务提交后的写屏障：命中墓碑的 pipeline_id 不再接受 state /
//! trace / checkpoint / 消息 / 映射 / pending 写入，从数据面保证删除是终态。
//!
//! ## 为什么放内存
//!
//! 墓碑只需要覆盖"与删除同进程的在途 run"。进程重启后所有在途 run 随之
//! 消失，不存在还能回写的写者；持久化墓碑反而会让日后同 id 数据永远写不进。
//! 因此不建表、不落库，随进程生命周期存在。
//!
//! ## 语义
//!
//! - 写入被丢弃**不报错**（返回 Ok）：管道已不存在，写者无需感知/重试；
//!   报错会让在途 run 以插件失败收场并写补偿标记，反而制造噪音。
//! - 读面不做墓碑过滤：删除事务已把存量数据删净，无需在每条读查询上再查
//!   一次墓碑（且冷读面可能来自另一进程，内存墓碑不可见）。

use std::collections::HashSet;
use std::sync::OnceLock;

use parking_lot::RwLock;

static DELETED: OnceLock<RwLock<HashSet<String>>> = OnceLock::new();

fn registry() -> &'static RwLock<HashSet<String>> {
    DELETED.get_or_init(|| RwLock::new(HashSet::new()))
}

/// 登记已删除管道（删除事务提交后调用；空 id 忽略）。
pub fn mark_deleted(pipeline_id: &str) {
    if !pipeline_id.is_empty() {
        registry().write().insert(pipeline_id.to_string());
    }
}

/// 撤销登记（删除事务失败回滚时调用，避免把仍在的管道误判为已删）。
pub fn unmark_deleted(pipeline_id: &str) {
    if !pipeline_id.is_empty() {
        registry().write().remove(pipeline_id);
    }
}

/// 该管道是否已删除（写路径屏障判定）。
pub fn is_deleted(pipeline_id: &str) -> bool {
    !pipeline_id.is_empty() && registry().read().contains(pipeline_id)
}

/// 当前墓碑数量（测试/诊断用）。
pub fn len() -> usize {
    registry().read().len()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 墓碑是进程级单例：测试用互不相同的 id，避免共享状态互相污染；
    /// 本测试只断言自己登记/撤销的 id。
    #[test]
    fn test_mark_and_unmark_isolated_by_id() {
        let id = "test-deleted-pipeline-mark";
        assert!(!is_deleted(id));
        mark_deleted(id);
        assert!(is_deleted(id), "登记后应命中墓碑");
        unmark_deleted(id);
        assert!(!is_deleted(id), "撤销后不应再命中墓碑");
    }

    #[test]
    fn test_empty_id_never_deleted() {
        mark_deleted("");
        assert!(!is_deleted(""), "空 id 不构成墓碑维度");
    }

    #[test]
    fn test_len_tracks_registry() {
        let before = len();
        mark_deleted("test-deleted-pipeline-len");
        assert!(len() > before);
        unmark_deleted("test-deleted-pipeline-len");
    }
}

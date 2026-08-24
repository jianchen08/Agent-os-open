//! 集成测试空壳 crate——仅承载 `tests/` 下的跨模块集成测试，不提供任何复用代码。
//! （原 NoopInvoker 已删除：benches 目录为空、tests/ 无引用；本 lib 无导出符号。）
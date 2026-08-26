# Native 插件开发（Rust cdylib）

> 返回 [开发指南索引](README.md)。前置阅读：[插件开发总览](plugin-development.md)。

## 1. 何时选 native

进程内零 IPC（延迟 < 1μs 量级），适合每轮必执行的高频管道步骤。代价：无热加载（cdylib 集合变更 = 内核排空 + 自重启，G8）、编译期耦合（内核与插件必须同 rustc 版本，`rust-toolchain.toml` 锁定 1.85）、panic=abort（panic 会终止进程）。常规路径：先写 sidecar，基准证明开销显著再晋升（`plugins/shared/pipeline/output/sensitive_checker/` 即从 Python 迁移的样板）。

## 2. 工程骨架与构建

Cargo.toml（参考 `plugins/shared/pipeline/core/tool_core/Cargo.toml`）：

```toml
[package]
name = "pipeline-sensitive-checker-native"
edition = "2021"

[lib]
crate-type = ["cdylib"]        # 产出 .dll/.so/.dylib 供 NativePluginLoader 加载

[dependencies]
agentos-native-sdk = { path = "../../../../../../kernel/crates/native-sdk" }
serde_json = "1"

[profile.release]
panic = "abort"                # cdylib 跨 FFI 标准做法，避免 unwind 跨边界 UB
```

manifest（参考 `plugins/shared/pipeline/output/sensitive_checker/plugin.json`）：

```jsonc
{
  "id": "pipeline_sensitive_checker",
  "plugin_type": "pipeline",
  "pipeline_role": "output",
  "language": "rust",
  "host_type": "in_process",
  "entry": "pipeline_sensitive_checker_native.dll",     // 产物文件名（相对插件目录）
  "invoke_entry": "plugin_execute",                     // 管道入口名（命名治理用）
  "native": { "artifact": "pipeline_sensitive_checker_native" },  // 无扩展名，加载器跨平台重映射 .dll/.so/.dylib
  "lifecycle": { "idle_timeout_secs": 0 }               // native 常驻，不做空闲卸载
}
```

构建与落位：插件目录内 `cargo build --release`，把产物从 `target/release/` 复制到插件目录根（与 `entry` 文件名一致）。产物缺失在**加载期**即报错（契约闸门），不会等到运行。

## 3. ABI 契约（`kernel/crates/native-sdk/src/lib.rs`）

- 唯一导出符号：`agentos_plugin_create`，返回裸指针（双重 Box 跨 C-ABI 传递 fat trait 对象）。
- 实现 `PipelinePlugin` trait：`fn execute(&self, ectx: &ExecContext) -> Result<String, String>`，**返回 state_updates 的 JSON 字符串**。
- `ExecContext`：`ctx`（`state_json` / `config_json` / `tenant_id` / `session_id` / `task_id` / `pipeline_id` / `tool_call_json`——`tool_call_json` 含 `{"name": ...}` 即工具调用语义，返回 `{success, data}` 信封）+ `host`（`HostServices`，`call_capability(capability, method, params_json)` 反调内核，与 sidecar 走同一 router）。
- 不用 abi_stable：靠 rustc 版本锁定保证 vtable 一致——**不要用与内核不同的工具链版本编译**。

## 4. 完整示例（`plugins/shared/pipeline/output/sensitive_checker/src/lib.rs` 节选）

```rust
use agentos_native_sdk::{plugin_into_raw, ExecContext, PipelinePlugin};
use serde_json::{Map, Value};

pub struct SensitiveChecker;

impl PipelinePlugin for SensitiveChecker {
    fn execute(&self, ectx: &ExecContext) -> Result<String, String> {
        let state = ectx.ctx.state_value();
        let config = ectx.ctx.config_value();

        let enabled = config.get("enabled").and_then(|v| v.as_bool()).unwrap_or(true);
        if !enabled {
            return serde_json::to_string(&serde_json::json!({})).map_err(|e| e.to_string());
        }
        let mask = config.get("mask").and_then(|v| v.as_str()).unwrap_or("***");

        // 扫描 state["tool_results"] 中的敏感模式并脱敏 ...
        let mut updates = serde_json::Map::new();
        // updates.insert("tool_results".into(), ...);
        // updates.insert("sensitive_detected".into(), Value::Bool(true));

        serde_json::to_string(&updates).map_err(|e| e.to_string())
    }
}

/// 构造函数（extern "C"）：内核 dlopen 后调它拿 trait 对象裸指针。
#[no_mangle]
pub extern "C" fn agentos_plugin_create() -> *mut () {
    plugin_into_raw(SensitiveChecker)
}
```

业务代码零 unsafe——FFI 全部由 native-sdk 的 `plugin_into_raw` 封装。源文件尾部带 `#[cfg(test)]` 行为契约测试（脱敏正则、state 写回条件、disabled 空转），`cargo test` 直接跑。

## 5. 生命周期契约

- **永不卸载**：库句柄以 `ManuallyDrop` 持有，逻辑 unload 只从表移除，物理释放随进程退出（Windows 上 `FreeLibrary` 带静态的 cdylib 会访问违例）。
- 内核对 native 的 execute 做 `catch_unwind` 包裹，但 `panic=abort` 下 panic 即进程终止——**native 插件内不要 panic**。
- G8：cdylib 集合变更 = 内核排空在跑任务 + 自重启 + 前端 resync（替换/升级 native 插件前确保无关键任务在跑）。

# WASM Hello World 插件样例（task_11 N10）

验证内核 `WasmRuntime` 的 host↔guest JSON 经线性内存契约，并实测 WASM 产物体积。

## 编译

```bash
# 需先安装 target：rustup target add wasm32-unknown-unknown
cargo build --release --target wasm32-unknown-unknown
# 产物：target/wasm32-unknown-unknown/release/agentos_wasm_hello_plugin.wasm
```

仓库内已附预编译产物 `wasm_hello.wasm`（384 字节），无需 wasm32 工具链即可被内核加载。

## 体积实测（计划文档 §八 #3）

| 编译目标 | 依赖 | 产物体积 |
|---|---|---|
| `wasm32-unknown-unknown` (no_std) | 无 | **384 字节** |
| `wasm32-wasip2` (std + serde_json，估算) | serde_json + WASI runtime | ~50KB–2MB（待 wasip2 工具链 PoC 实测） |

- 最小 `no_std` 插件仅 384 字节——WASM 二进制天然紧凑。
- 完整 std + serde_json 的"现实"插件体积显著增大（Rust monomorphization + WASI runtime），
  这是采用完整 WIT/组件模型前需权衡的点（§八 #1/#3）。
- 当前降级方案（JSON 经线性内存）无需 serde_json 即可工作，样例刻意保持 no_std 最小体积。

## ABI 契约

与内核 `kernel/crates/plugin-loader/src/wasm_loader.rs` 共享：

```text
memory              —— 线性内存（guest 拥有）
allocate(len) -> ptr
deallocate(ptr, len)
execute(in_ptr, in_len) -> packed(out_ptr | out_len << 32)
```

输入/输出都是 JSON 字符串（`PluginInput` / `PluginResult`，与原生插件一致）。

## 行为

`execute` 返回固定的成功 PluginResult：

```json
{"state_updates":{"processed_by":"wasm_hello"}}
```

内核测试 `kernel/crates/plugin-loader/src/wasm_loader.rs::hello_world_wasm_loads_and_executes`
加载本样例并验证返回值。

## manifest

见 `plugin.json`（`host_type: "wasm"`，`wasm.artifact: "wasm_hello.wasm"`）。

# WASM 沙箱化计算器插件

取代 Python `plugins/shared/tools/simple/calc_tools.py` 的 `eval()` 方案。
**第二个端到端范例**：验证内核 `WasmRuntime` 能加载执行"现实"复杂度的 WASM 插件
（std + serde_json，137 KB），并演示 WASM 的核心价值——**沙箱化用户不可信输入**。

## 为什么 WASM 化

Python 版用 `eval(expr, {"__builtins__": {}}, {"math", "abs", "round"})` 计算用户
输入的算式。两个问题：

1. **`eval` 不是真正沙箱**：`{"__builtins__": {}}` 不能阻止构造恶意字面量逃逸
   （Python 沙箱逃逸是经典攻击面）。
2. **不可信输入**：算式来自用户/LLM 输出，属于不可信代码执行场景。

本插件用两种手段彻底消除逃逸面：

- **WASM 沙箱**：`wasm32-unknown-unknown` core wasm，无文件系统/网络/进程/宿主内存访问，
  只能调 host 授予的 import（本插件 `granted_capabilities: []`——零 host 能力）。
- **手写递归下降解析器**：取代 `eval`，只接受严格文法（数字、常量、数学函数、四则运算、
  幂运算），任何越界输入（属性访问、字符串字面量、列表推导等）直接报错。

## 编译

```bash
# 需先安装 target：rustup target add wasm32-unknown-unknown
cd plugins/shared/wasm_calc
cargo build --release --target wasm32-unknown-unknown
# 产物：target/wasm32-unknown-unknown/release/agentos_wasm_calc_plugin.wasm（137 KB）
```

仓库内已附预编译产物 `wasm_calc.wasm`，无需 wasm32 工具链即可被内核加载。

## 体积实测

| 编译目标 | 依赖 | 产物体积 |
|---|---|---|
| `wasm32-unknown-unknown` (no_std) | 无（wasm_hello） | 384 字节 |
| `wasm32-unknown-unknown` (std + serde_json) | serde + serde_json | **137 KB** |

`std + serde_json` 是"现实"插件的典型体积——解析 JSON 输入、序列化 JSON 输出
是任何非平凡插件的刚需。137 KB 相比 no_std 的 384 字节大两个量级，但仍是小而紧凑的
二进制（远小于一个 Python sidecar 进程）。这是采用 WIT/组件模型前需权衡的点
（见 `kernel/samples/wasm-hello-plugin/README.md`）。

## ABI 契约

与内核 `kernel/crates/plugin-loader/src/wasm_loader.rs` 共享：

```text
memory              —— 线性内存（guest 拥有）
allocate(len) -> ptr
deallocate(ptr, len)
execute(in_ptr, in_len) -> packed(out_ptr | out_len << 32)
```

输入（WASM tool 调用约定，state 即工具参数）：
```json
{"state": {"operation": "calculate", "expression": "1+2*3"}, "config": {}}
```

输出（PluginResult，state_updates 即工具返回数据）：
```json
{"state_updates": {"expression": "1+2*3", "result": 7}}
```

错误（对齐 Python calc_tools 的 `{"error": ...}` 返回形态）：
```json
{"state_updates": {"error": "除数不能为零"}}
```

## 支持的运算

对齐 Python `calc_tools.py` 的 `_OPERATIONS` / `_CONSTANTS` / `_safe_eval.safe_funcs`：

- **常量**：`pi`、`e`、`tau`、`inf`
- **三角（度数输入/输出）**：`sin cos tan asin acos atan sinh cosh tanh`
- **对数**：`ln log10 log2`、`log(x, base)`、`log(x, 0)` → 自然对数
- **幂**：`pow(x, y)`、`sqrt`、`cbrt`、`exp`
- **取整**：`ceil floor abs`
- **角度转换**：`degrees radians`
- **整数**：`factorial`、`gcd`
- **运算符**：`+ - * / % ^`（幂，右结合）、括号、一元正负

## 文法（手写递归下降解析器）

```
expr    := term (('+' | '-') term)*
term    := factor (('*' | '/' | '%') factor)*
factor  := power
power   := unary ('^' power)?              // 右结合
unary   := ('+' | '-') unary | atom
atom    := number | constant | func '(' expr (',' expr)* ')' | '(' expr ')'
```

## 验证

```bash
# 1. 单元 + ABI 自检（本 crate 内）
cd plugins/shared/wasm_calc
cargo build --release --target wasm32-unknown-unknown

# 2. 端到端（内核 WasmRuntime 加载真实产物）
cd ../../../kernel
cargo test --release -p agentos-integration-tests --test wasm_calc_plugin
# 9 个测试：优先级、右结合幂、括号、常量、函数调用、错误处理、一元负号、模运算
```

`kernel/crates/integration-tests/tests/wasm_calc_plugin.rs` 用生产 `WasmRuntime` 加载
本插件产物，验证 JSON 经线性内存契约的双向传递 + 数学正确性 + 沙箱错误处理。

## manifest

见 `plugin.json`（`host_type: "wasm"`，`wasm.artifact: "wasm_calc.wasm"`，
`wasm.granted_capabilities: []`——零 host 能力，纯计算）。

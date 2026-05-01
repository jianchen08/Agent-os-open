# Bug 调试方法论

> 来源：2026-05-01 一次多轮调试实战的复盘，结合学术界 confirmation bias 研究和业界分布式追踪实践。

## 总原则

**先确认"谁在执行"，再优化"怎么执行"。**

绝大多数深层 bug 不在报错代码本身，而在对象生命周期或编排层。直接在报错位置反复修改是最大的时间浪费。

---

## 规则 1：自顶向下，不要自底向上

**错误做法**：看到 `AuthenticationError` → 立刻去检查 API key 格式、provider 前缀、litellm 参数。
**正确做法**：先确认"这个错误是从哪个对象/实例抛出的"，再往下追。

操作步骤：
1. 在报错的最顶层入口加标记（print/logger），确认是哪个对象实例在执行
2. 在所有可能的分支/实现都加标记，而不是只在你"认为"正确的那个加
3. 标记内容至少包含：`type(self).__name__` 和关键参数

```python
# 正确示例：在所有适配器的 _do_completion 都加标记
class LiteLLMAdapter:
    async def _do_completion(self, **kwargs):
        print(f"[DIAG] adapter={type(self).__name__} model={kwargs.get('model')}")
        ...

class AdaptiveRouterAdapter:
    async def _do_completion(self, **kwargs):
        print(f"[DIAG] adapter={type(self).__name__} model={kwargs.get('model')}")
        ...
```

**为什么**：自底向上调试容易困在底层细节，忽略高层的对象替换、工厂重建、注册表 fork 等编排问题。

---

## 规则 2：隔离测试通过 ≠ 系统正确

**教训**：连续写了 6 级隔离测试，全部通过。但实际系统仍然失败。

原因：隔离测试只验证"这段代码在给定输入下能正常工作"。但如果实际运行时这段代码根本没被调用（被另一个实例/实现替代了），测试通过反而是误导。

**规则**：
- 隔离测试只用于验证"这个函数/类的逻辑是对的"
- 隔离测试通过后，如果 bug 仍在，**立刻停止在隔离层继续修改**，转向系统集成层
- 隔离测试的通过不能作为"bug 已修复"的判断依据

**判断阈值**：如果隔离测试通过但完整系统仍失败，**最多再做 1 轮隔离测试确认**，然后必须切换到全链路追踪模式。

---

## 规则 3：追踪对象生命周期，而不是代码路径

本次 bug 的本质：

```
build_plugin_registry → LLMCore(adapter=AdaptiveRouterAdapter)  ✓
    ↓ registry.fork()
PipelineEngine.__init__ → LLMCore(config=...) → LiteLLMAdapter  ✗  ← adapter 丢失！
    ↓ _apply_agent_model_override
engine.run → 把 LiteLLMAdapter 当作 existing_adapter 传递      ✗
```

**bug 不在 LLM 调用层，而在插件注册表的 fork 机制。** 从始至终 LLM 相关代码都是对的。

**规则**：对于"功能 A 在测试中正常但在系统中失败"的 bug：
1. 在对象的**构造函数**加调用栈追踪，确认对象被创建了几次、由谁创建
2. 检查所有可能的工厂/fork/clone/rebuild 路径
3. 确认运行时实际使用的实例和你修改的是同一个

```python
# 在构造函数追踪对象创建
class LLMCore:
    def __init__(self, **kwargs):
        import traceback
        traceback.print_stack(limit=4)  # 只打印最近 4 层
        print(f"[DIAG] LLMCore created with adapter={type(kwargs.get('adapter')).__name__}")
```

---

## 规则 4：诊断要加在分叉点，不是执行点

**错误做法**：在 `_direct_call` 内部加了 20 行日志，详细记录每个参数。
**正确做法**：在 `_do_completion`（多态分叉点）和 `__init__`（创建点）各加 1 行标记。

分叉点是指：多个实现可能被执行的地方（多态/策略模式/插件注册表）。
执行点是指：具体实现内部。

**原因**：如果分叉点选错了实现，执行点内的所有日志都是在错误的代码上打的，完全没有参考价值。

---

## 规则 5：警惕 confirmation bias — 主动证伪，不要只验证

学术界对软件工程中 confirmation bias 的研究 ([IEEE/ACM](https://www.computer.org/csdl/journal/ts/2020/12/08506423/14DL8SnwZk4), [Simula](https://web-backend.simula.no/sites/default/files/publications/files/confbiasinse_15_feb_to_submit.pdf)) 表明：

> 调试时，开发者倾向寻找支持自己假设的证据，而非尝试推翻它。

**本次表现**：
- 假设"bug 在 _direct_call 的参数合并" → 反复修改参数合并逻辑
- 每次修改后跑隔离测试，通过 → 觉得"快修好了" → 继续在这个方向深挖
- 从未质疑过"也许 _direct_call 根本没被调用"

**反制措施**：
- 每当修改后 bug 仍在，问自己："如果我的假设是错的，最可能的替代解释是什么？"
- 设立"放弃阈值"：同一方向尝试 3 次无效后，**强制切换假设**
- 用 print 在错误对象上打标记，而不是在"应该是正确"的对象上打标记

---

## 规则 6：插件/工厂系统的常见陷阱清单

当 bug 表现为"功能在简单场景正常但在完整流程失败"时，按此清单检查：

| 检查项 | 常见问题 |
|--------|---------|
| **fork/clone** | 是否只复制了 config 而丢失了 adapter/router 等运行时依赖？ |
| **工厂重建** | `type(obj)(config=...)` 是否漏传了构造函数参数？ |
| **单例 vs 多实例** | 以为是同一个对象，实际被重建了？ |
| **依赖注入** | 依赖是通过构造函数注入还是运行时获取？注入的是引用还是值？ |
| **缓存/全局状态** | `__pycache__`、模块级单例是否过期？ |
| **注册表同步** | 替换插件后，core_plugins 和 plugins 两个映射是否都更新了？ |

---

## 调试工作流（推荐）

```
阶段 1：复现与定位（最多 2 轮）
  ├─ 用最小输入复现 bug
  ├─ 在报错的所有可能分支加 print 标记
  └─ 确认"是哪个对象/实例在执行"

阶段 2：对象生命周期追踪（最多 2 轮）
  ├─ 在构造函数加 traceback
  ├─ 确认对象被创建了几次、由谁创建
  └─ 确认运行时实例与预期一致

阶段 3：根因修复
  ├─ 修复真正的根因（不是症状）
  └─ 在完整系统中验证（不是隔离测试）

阶段 4：回归保护
  └─ 添加测试防止同类问题复发
```

**核心原则**：阶段 1 和 2 是纯诊断，不改任何代码。只加 print。确认了"是谁"和"在哪里"之后，才进入阶段 3 修改代码。

# Agent 工具组件

## 需求
### 职责
提供 Agent 执行过程中的实用工具类，包括重复工具调用检测、连续失败检测等功能。

### 对外接口
- 输入：工具调用历史记录
- 输出：检测结果（重复信息、失败统计）

### 依赖
- 依赖库：标准库（json、logging）

## 逻辑
### 流程设计
**DuplicateCallDetector**：
1. 分析工具调用历史记录
2. 检测相同参数的重复调用
3. 检测连续失败调用
4. 返回检测结果供路由决策使用

### 数据流向
```
工具调用历史 → DuplicateCallDetector → 检测结果 → 路由决策
```

### 配置设计
#### 组件配置
| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| DEFAULT_MAX_CONSECUTIVE_FAILURES | 最大连续失败次数 | 2 |
| DEFAULT_MAX_SAME_PARAM_CALLS | 相同参数最大调用次数 | 3 |

## 结构
### 文件清单（代码文件 - 具体接口）
#### duplicate_call_detector.py
职责：重复工具调用检测器
暴露接口：
- `DuplicateCallDetector`：检测器类
  - `_hash_inputs(inputs: dict[str, Any] | None) -> str`：将输入参数转换为哈希字符串
  - `check_same_param_calls(tool_calls_history: list[dict[str, Any]], max_calls: int) -> dict[str, Any] | None`：检查最近 N 次调用是否使用相同参数
  - `check_duplicate(tool_calls_history: list[dict[str, Any]], min_consecutive_calls: int) -> dict[str, Any] | None`：检查是否有重复的工具调用
  - `get_consecutive_failures(tool_calls_history: list[dict[str, Any]]) -> dict[str, int]`：获取每个工具的连续失败次数
  - `has_excessive_failures(tool_calls_history: list[dict[str, Any]], max_consecutive_failures: int) -> dict[str, Any] | None`：检查是否有工具连续失败超过限制
  - `check_all(tool_calls_history: list[dict[str, Any]], max_consecutive_failures: int, max_same_param_calls: int) -> dict[str, Any] | None`：执行所有检测

#### __init__.py
职责：模块导出
暴露接口：
- `DuplicateCallDetector`

### 测试策略
#### 组件测试
- 单元测试：各检测方法的逻辑
- 边界测试：空历史、单次调用、大量调用
- 场景测试：各种重复模式、失败模式

## 实现
→ 见代码文件

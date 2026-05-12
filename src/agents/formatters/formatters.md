# 格式化器组件

## 需求
### 职责
提供消息格式化功能，负责构建 LLM 版本和纯净版本的工具执行结果消息。

### 对外接口
- 输入：工具名称、执行结果/错误信息
- 输出：格式化后的消息元组 (llm_content, pure_result)

### 依赖
- 依赖模块：`src.core.tokenizer`（Token 计数器）

## 逻辑
### 流程设计
1. 接收工具执行结果
2. 根据结果类型（成功/失败/异常）选择格式化方法
3. 生成两种版本的消息：
   - LLM 版本：包含提示词包装，适合 LLM 理解
   - 纯净版本：只包含原始结果，适合前端/数据库
4. 对超长内容进行截断

### 数据流向
```
工具执行结果 → 格式化器 → (LLM消息, 纯净消息)
```

### 配置设计
#### 组件配置
| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| MAX_TOKENS | 最大 Token 数 | 100000 |
| MAX_OUTPUT_LENGTH | 最大输出长度 | 100000 |

## 结构
### 文件清单（代码文件 - 具体接口）
#### tool_message_formatter.py
职责：工具消息格式化器
暴露接口：
- `ToolMessageFormatter`：格式化器类
  - `format_success_message(tool_name: str, output: Any, record_id: str | None) -> tuple[str, str]`：格式化成功消息
  - `format_error_message(tool_name: str, error: str | None) -> tuple[str, str]`：格式化错误消息
  - `format_exception_message(tool_name: str, exception: Exception) -> tuple[str, str]`：格式化异常消息
  - `truncate_output(output: Any) -> str`：截断输出内容

#### __init__.py
职责：模块导出
暴露接口：
- `ToolMessageFormatter`

### 测试策略
#### 组件测试
- 单元测试：各格式化方法、截断逻辑
- 边界测试：空结果、超长结果、特殊字符

## 实现
→ 见代码文件

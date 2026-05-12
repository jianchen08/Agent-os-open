# 推理工具组件

## 一、需求

### 1.1 组件职责

推理工具组件为高风险工具执行提供推理验证机制：
- 检测是否需要推理
- 提取推理内容
- 验证推理完整性
- 拦截无推理的高风险操作

### 1.2 对外接口

- `ReasoningMiddleware`：推理中间件（装饰器模式）
- `ReasoningInterceptor`：推理拦截器
- `ReasoningExtractor`：推理提取器
- `ReasoningValidator`：推理验证器

### 1.3 依赖

- `tools.executor`：工具执行器
- `core.logging`：日志模块
- `core.config`：配置模块

---

## 二、逻辑

### 2.1 流程设计

#### 推理中间件流程

```
工具调用 → ReasoningMiddleware
              ↓
         ReasoningInterceptor.check()
              ↓
    ┌─────────┼─────────┐
    ↓                   ↓
  需要推理          不需要推理
    ↓                   ↓
  检查推理内容      直接执行
    ↓
    ┌─────────┼─────────┐
    ↓                   ↓
  有推理            无推理
    ↓                   ↓
  验证推理          拒绝执行
    ↓
    ┌─────────┼─────────┐
    ↓                   ↓
  验证通过        验证失败
    ↓                   ↓
  执行工具        返回错误
```

#### 推理提取流程

```
消息内容 → ReasoningExtractor
              ↓
         解析消息结构
              ↓
    ┌─────────┼─────────┐
    ↓         ↓         ↓
  思考标签  推理段落  隐式推理
    ↓         ↓         ↓
    └─────────┼─────────┘
              ↓
         提取推理文本
              ↓
         返回推理内容
```

#### 推理验证流程

```
推理内容 → ReasoningValidator
              ↓
         检查推理完整性
              ↓
    ┌─────────┼─────────┐
    ↓         ↓         ↓
  长度检查  结构检查  逻辑检查
    ↓         ↓         ↓
    └─────────┼─────────┘
              ↓
         返回验证结果
```

### 2.2 数据流向

```
ToolExecutor → ReasoningMiddleware
                   ↓
            ReasoningInterceptor
                   ↓
         ┌─────────┴─────────┐
         ↓                   ↓
    需要推理            不需要推理
         ↓                   ↓
  ReasoningExtractor      直接执行
         ↓
  ReasoningValidator
         ↓
    ┌────┴────┐
    ↓         ↓
  通过      失败
    ↓         ↓
  执行      拒绝
```

### 2.3 配置设计

| 配置项 | 类型 | 说明 |
|--------|------|------|
| high_risk_tools | List[str] | 高风险工具列表 |
| min_reasoning_length | int | 最小推理长度 |
| require_explicit_reasoning | bool | 是否要求显式推理 |

### 2.4 错误处理

- 无推理拦截：返回推理缺失错误
- 推理验证失败：返回验证失败详情
- 提取失败：返回提取错误

---

## 三、结构

### 3.1 子组件清单

| 子组件 | 职责 |
|--------|------|
| ReasoningMiddleware | 推理中间件（装饰器） |
| ReasoningInterceptor | 推理拦截检测 |
| ReasoningExtractor | 推理内容提取 |
| ReasoningValidator | 推理完整性验证 |

### 3.2 文件清单

| 文件 | 职责 |
|------|------|
| `__init__.py` | 模块导出 |
| `middleware.py` | 推理中间件 |
| `interceptor.py` | 推理拦截器 |
| `extractor.py` | 推理提取器 |
| `validator.py` | 推理验证器 |
| `templates.py` | 推理提示模板 |

### 3.3 测试策略

- 单元测试：各组件方法独立测试
- 集成测试：中间件与执行器协作测试
- 覆盖率要求：核心逻辑 ≥85%

---

## 四、实现

### 4.1 middleware.py

```
ReasoningMiddleware:
  wrap(executor: ToolExecutor) -> ToolExecutor: 包装执行器
  before_execute(tool: str, params: dict) -> Optional[ReasoningCheck]: 执行前检查
  after_execute(result: ToolResult) -> None: 执行后处理
```

### 4.2 interceptor.py

```
ReasoningInterceptor:
  check(tool: str, context: dict) -> ReasoningRequirement: 检查是否需要推理
  is_high_risk(tool: str) -> bool: 判断是否高风险工具
  should_intercept(tool: str, params: dict) -> bool: 判断是否拦截
```

### 4.3 extractor.py

```
ReasoningExtractor:
  extract(message: str) -> ReasoningContent: 提取推理内容
  extract_from_tags(text: str) -> str: 从标签提取
  extract_from_paragraphs(text: str) -> str: 从段落提取
```

### 4.4 validator.py

```
ReasoningValidator:
  validate(reasoning: ReasoningContent) -> ValidationResult: 验证推理完整性
  check_length(reasoning: str, min_length: int) -> bool: 检查长度
  check_structure(reasoning: str) -> bool: 检查结构
  check_logic(reasoning: str) -> bool: 检查逻辑
```

### 4.5 templates.py

```
REASONING_PROMPT_TEMPLATE: str: 推理提示模板
```

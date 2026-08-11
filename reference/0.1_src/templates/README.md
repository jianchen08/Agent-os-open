# 模板系统（templates）

## 需求

Agent OS 的产出物需要遵循统一的格式规范。模板本质是**上下文注入**——把模板文件内容读到管道 state 中，由 `prompt_build` 组装到系统提示词里，Agent 按模板格式输出。

支持：
- 从 Markdown 文件加载模板并解析元数据（HTML 注释块）
- 替换 `{xxx}` 占位符进行模板渲染
- 按类型、标签查找模板
- 识别章节标注（必填/可选/按需/条件必填）和评估维度

## 逻辑

### 模板类型

- **A 型（消费型）**：产出物被下游 Agent 解析执行，不需要评估指南
- **B 型（报告型）**：产出物为最终交付物，必须有评估指南

### 三段式结构

1. **头部说明**：HTML 注释块，包含模板是什么、作用、使用方法、场景、关系
2. **正文章节**：`## 章节名称 [标注]`，占位符 `{xxx}`
3. **评估指南**：HTML 注释块，包含检查维度和通过标准（B 型必须有）

### 注入方式

模板通过 Agent YAML 的 `static_vars.items`（`type: "path"`）注入：
1. `ContextBuilder` 读取模板文件内容
2. `PromptBuildPlugin` 将内容组装到系统提示词
3. Agent 按照模板格式输出

这是模板驱动的核心——模板 = 提示词的一部分，不需要复杂的桥接/验证/编排逻辑。

### 加载流程

1. 读取 Markdown 文件
2. 解析头部 HTML 注释块提取元数据
3. 提取 `{xxx}` 占位符（去重）
4. 识别章节标记
5. 解析评估维度表格
6. 有评估维度 → B 型，无 → A 型

### 渲染流程

1. 分离 HTML 注释块和正文
2. 只替换正文部分的占位符
3. 保留注释块中的元数据
4. 严格模式下缺失变量抛出 KeyError，否则保留原始占位符

## 结构

### 文件清单

| 文件 | 用途 |
|------|------|
| `types.py` | 数据类型定义（TemplateType, TemplateSection, EvaluationDimension, TemplateSpec） |
| `loader.py` | TemplateLoader：Markdown 模板加载与解析 |
| `renderer.py` | TemplateRenderer：占位符替换渲染 |
| `registry.py` | TemplateRegistry：注册/查找/筛选/批量加载 |
| `__init__.py` | 公共 API 导出 |
| `README.md` | 本文档 |

### 数据流

```
Markdown 文件 → TemplateLoader.load_from_markdown() → TemplateSpec
TemplateSpec + variables → TemplateRenderer.render() → 渲染后内容
TemplateSpec → TemplateRegistry.register() → 注册表
目录 → TemplateRegistry.load_directory() → 批量注册

Agent YAML static_vars.items (type: "path") → ContextBuilder → state → prompt_build → 系统提示词
```

### 依赖

- 仅使用 Python 标准库（re, pathlib）
- 不依赖其他模块

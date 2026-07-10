# Multimodal 模块文档

## 需求

多模态处理模块，为 Agent OS 提供多模态消息（图片、音频、视频、文档）的处理能力：

1. **统一适配**：屏蔽不同 LLM 提供商的多模态消息格式差异
2. **能力注册**：集中管理各模型的多模态能力（支持的媒体类型）
3. **格式转换**：将附件信息转换为模型特定的消息格式
4. **文件存储**：多模态文件的存储与检索

## 逻辑

### 适配器架构

```
附件内容 + AttachmentInfo
  → ModelCapabilityRegistry.get_adapter(provider)
  → MultimodalAdapter.convert(content, attachments)
  → 模型特定格式消息列表
```

### 工具多模态回流（MM-3 + MM-5 + MM-4b）

工具产生的多模态结果（图片等）回流到 LLM 的完整链路：

```
工具执行（image_generate / playwright_test 截图等）
  → 返回 ToolExecutionResult，metadata 含 multimodal_content 字段
  → multimodal_content 格式：[{type: "image_url", image_url: {url: "data:mime;base64,..."}}]
  → ToolCore.execute() 从 metadata.multimodal_content 提取图片
  → 根据 ModelCapabilityRegistry.is_multimodal_supported(model) 判断：
    - 视觉模型 → 注入多模态 user 消息（content 为 content_blocks 列表）
    - 非视觉模型 → 注入文本提示（引导调用 MCP 分析工具）
  → 同时通过 on_chunk 发射 tool_multimedia_result WS 事件（MM-4b）
  → BridgeEvents._handle_chunk 格式化为前端事件推送
  → 下一轮 LLMCore._build_messages() 读取 messages → LLM "看到" 图片
```

**slim 序列化保护**：`ExecutionResult.to_dict(slim=True)` 排除 `multimodal_content` 字段，
防止 base64 数据污染发给 LLM 的纯文本上下文。

### 适配器类型

| 适配器 | 提供商 | 支持格式 |
|--------|--------|---------|
| OpenAIVisionAdapter | OpenAI | 图片（base64 / URL） |
| ClaudeVisionAdapter | Anthropic | 图片（base64 / URL） |
| DefaultAdapter | 其他 | 文本描述降级 |

### 媒体类型

| 类型 | 说明 | 支持格式 |
|------|------|---------|
| IMAGE | 图片 | jpeg, png, gif, webp |
| AUDIO | 音频 | mp3, wav, m4a |
| VIDEO | 视频 | mp4, mov, avi |
| DOCUMENT | 文档 | pdf, doc, docx |

### 能力注册表

```
ModelCapabilityRegistry
  → CAPABILITIES: 预定义模型能力字典
    - gpt-4-vision-preview: 图片
    - gpt-4o: 图片
    - claude-3-opus/sonnet: 图片
    - 等其他模型
  → ADAPTER_MAPPING: provider → Adapter 类映射
  → PROVIDER_MODEL_MAPPING: model_name → provider 映射

查询接口：
  get_capability(model_name) → ModelCapability
  get_adapter(provider) → MultimodalAdapter
  is_multimodal_supported(model_name) → bool
```

### 数据模型

```
AttachmentInfo (Pydantic BaseModel)
  ├── file_id: str        — 文件唯一标识
  ├── filename: str       — 原始文件名
  ├── mime_type: str      — MIME 类型
  ├── size: int           — 文件大小（字节）
  ├── media_type: MediaType — 媒体类型枚举
  ├── base64_data: str    — Base64 编码内容（可选）
  └── url: str            — 外部 URL（可选）

ModelCapability (Pydantic BaseModel)
  ├── model_name: str              — 模型名称
  ├── supports_image: bool         — 是否支持图片
  ├── supports_audio: bool         — 是否支持音频
  ├── supports_video: bool         — 是否支持视频
  └── supported_image_types: list  — 支持的图片 MIME 类型

MultimodalContent (Pydantic BaseModel)
  → 多模态内容封装
```

## 结构

### 文件清单

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `types.py` | MediaType, AttachmentInfo, ModelCapability, MultimodalContent | 数据类型定义 |
| `adapter.py` | MultimodalAdapter, OpenAIVisionAdapter, ClaudeVisionAdapter, DefaultAdapter | 多模态适配器（ABC + 3 种实现） |
| `capabilities.py` | ModelCapabilityRegistry | 模型能力注册表 |
| `storage.py` | — | 多模态文件存储 |

### 依赖

- `pydantic` — 数据模型验证
- `litellm` — LLM 调用（通过 adapter 间接使用）
- Python 标准库：abc, enum

### 使用方式

```python
from multimodal import ModelCapabilityRegistry, AttachmentInfo, MediaType

# 检查模型是否支持多模态
if ModelCapabilityRegistry.is_multimodal_supported("gpt-4o"):
    adapter = ModelCapabilityRegistry.get_adapter_for_model("gpt-4o")
    messages = adapter.convert("描述这张图片", [attachment])
```

# ChatContainer

## 需求说明

### 功能概述

聊天容器组件，作为完整聊天界面的顶层编排组件，整合消息列表和输入区域。核心职责包括：

1. **消息展示**：渲染经过过滤后的消息列表，支持搜索高亮
2. **消息搜索**：提供搜索栏，按关键词过滤消息内容和工具调用名称
3. **消息输入**：承载聊天输入区域，支持发送、停止生成、思考模式切换
4. **交互面板**：展示人类交互卡片（由 InteractionPanel 管理）
5. **加载状态**：在初始加载时展示全局 Loading 指示器
6. **历史消息加载**：支持向上滚动加载更多历史消息
7. **错误边界**：InteractionPanel 被 ErrorBoundary 包裹，防止交互面板异常影响整体聊天

### 用户故事

- 作为用户，我希望在聊天界面中搜索历史消息，快速找到特定内容或工具调用
- 作为用户，我希望在加载会话时看到 Loading 指示器，了解系统正在工作
- 作为用户，我希望在 AI 生成回复时能停止生成，避免等待过长
- 作为用户，我希望对已有消息进行重新生成、编辑、删除操作
- 作为用户，我希望切换思考模式以获得不同深度的回复
- 作为用户，我希望滚动到顶部时自动加载更多历史消息
- 作为用户，我希望看到当前 Token 使用量和最大限制

### 验收标准

- [AC1] 组件渲染为垂直弹性布局（flex-col），占满父容器高度
- [AC2] 搜索栏位于顶部，包含搜索输入框、清除按钮和匹配计数显示
- [AC3] 搜索关键词同时匹配消息内容（content）和工具调用名称（toolCalls[].tool_name）
- [AC4] 搜索过滤不区分大小写
- [AC5] 搜索栏下方展示过滤后的消息列表，无搜索时展示全部消息
- [AC6] 消息列表下方嵌入 InteractionPanel（被 ErrorBoundary 包裹）
- [AC7] 底部为 ChatInput 输入区域，集成发送、停止生成、思考模式功能
- [AC8] isLoading 为 true 时，组件整体替换为 Loading 指示器（旋转图标 + "加载中..." 文案）
- [AC9] 组件根元素标注 data-testid="chat-container"，Loading 状态标注 data-testid="chat-container-loading"
- [AC10] 组件根元素标注 data-session-id 属性，值为传入的 sessionId
- [AC11] ChatInput 的 placeholder 固定为 "输入消息，按 Enter 发送..."
- [AC12] ChatInput 始终启用思考模式开关（enableThinkingMode=true）

## 逻辑说明

### 数据流

```
外部调用方
  │
  ├─ sessionId ──→ data-session-id 属性 + MessageList + InteractionPanel
  ├─ messages[] ──→ 搜索过滤 ──→ filteredMessages[] ──→ MessageList
  ├─ isLoading ──→ 全局 Loading 状态判断
  ├─ isGenerating ──→ MessageList + ChatInput
  ├─ onSendMessage ──→ ChatInput
  ├─ onStopGenerate ──→ ChatInput
  ├─ onRegenerate ──→ MessageList
  ├─ onEdit ──→ MessageList
  ├─ onDelete ──→ MessageList
  ├─ currentTokenUsage ──→ ChatInput
  ├─ maxTokens ──→ ChatInput
  ├─ modelName ──→ MessageList + ChatInput
  ├─ thinkingMode ──→ ChatInput
  ├─ toggleThinkingMode ──→ ChatInput
  ├─ hasMoreMessages ──→ MessageList
  ├─ isLoadingMoreMessages ──→ MessageList
  └─ onLoadMoreMessages ──→ MessageList
```

```
内部状态
  │
  └─ searchQuery (string)
       │
       ├─ 非空时 → filteredMessages = messages.filter(匹配 content 或 toolCalls)
       └─ 空时   → filteredMessages = messages（原始列表）
                    │
                    ├─→ MessageList（含 searchQuery 用于高亮）
                    └─→ 搜索计数显示 "找到 N 条消息"
```

### 状态流转

**加载状态（外部控制）：**
```
isLoading: false → true → false
  true 时: 整个组件替换为 Loading 指示器
  false 时: 渲染完整聊天界面（搜索栏 + 消息列表 + 交互面板 + 输入区域）
```

**搜索过滤（内部状态）：**
```
searchQuery: "" → "关键词" → ""
  输入时: 实时过滤消息列表，更新匹配计数
  清空时: 恢复展示全部消息
  清除方式: 点击搜索框右侧 X 按钮
```

**消息生成状态（外部控制）：**
```
isGenerating: false → true → false
  true 时: 消息列表显示生成中状态，输入区域显示停止按钮
  false 时: 正常展示，输入区域显示发送按钮
```

### 核心处理逻辑

1. **搜索过滤**：使用 `useMemo` 缓存过滤结果，依赖 `[messages, searchQuery]`。过滤逻辑将搜索词转为小写，分别匹配 `message.content` 和 `message.toolCalls[].tool_name`，任一匹配则保留该消息
2. **条件渲染**：`isLoading` 为 true 时提前返回 Loading 视图，阻止后续渲染
3. **搜索栏交互**：输入框实时更新 searchQuery；非空时显示清除按钮（X 图标）和匹配计数文案
4. **子组件数据透传**：将过滤后的消息透传给 MessageList（含 searchQuery 用于高亮），将输入相关 props 透传给 ChatInput
5. **错误隔离**：InteractionPanel 被 ErrorBoundary 包裹，确保交互面板的运行时错误不会导致整个聊天界面崩溃

## 结构说明

### Props 接口

| 属性名 | 类型 | 必填 | 默认值 | 说明 |
|--------|------|------|--------|------|
| sessionId | `string` | 是 | — | 当前会话唯一标识 |
| messages | `Message[]` | 是 | — | 消息列表，来自 `@/types/models` |
| isLoading | `boolean` | 否 | `false` | 是否正在初始加载会话数据 |
| isGenerating | `boolean` | 否 | `false` | AI 是否正在生成回复 |
| onSendMessage | `(params: SendMessageParams) => Promise<void>` | 是 | — | 发送消息回调，接收消息内容和附件 |
| onStopGenerate | `() => void` | 否 | — | 停止生成回调 |
| onRegenerate | `(messageId: string) => Promise<void>` | 否 | — | 重新生成指定消息回调 |
| onEdit | `(messageId: string, newContent: string) => Promise<void>` | 否 | — | 编辑消息内容回调 |
| onDelete | `(messageId: string) => Promise<void>` | 否 | — | 删除消息回调 |
| currentTokenUsage | `number` | 否 | `0` | 当前 Token 使用量 |
| maxTokens | `number` | 否 | `128000` | 最大 Token 限制 |
| modelName | `string` | 否 | `'glm-5.1'` | 当前使用的模型名称 |
| thinkingMode | `ThinkingModeState` | 否 | — | 思考模式状态 |
| toggleThinkingMode | `(enabled: boolean) => Promise<void>` | 否 | — | 切换思考模式回调 |
| className | `string` | 否 | `''` | 自定义 CSS 类名 |
| hasMoreMessages | `boolean` | 否 | `false` | 是否还有更多历史消息可加载 |
| isLoadingMoreMessages | `boolean` | 否 | `false` | 是否正在加载更多历史消息 |
| onLoadMoreMessages | `() => void` | 否 | — | 加载更多历史消息回调 |

### 状态（State）

| 状态名 | 类型 | 初始值 | 说明 |
|--------|------|--------|------|
| searchQuery | `string` | `''` | 搜索关键词，用于实时过滤消息列表 |

**派生状态：**

| 派生状态 | 计算方式 | 说明 |
|----------|----------|------|
| filteredMessages | `useMemo` 基于 messages 和 searchQuery 计算 | 过滤后的消息列表，搜索为空时等于原始列表 |

### 主题变量依赖

| Tailwind 语义化 Class | 使用位置 | 说明 |
|------------------------|----------|------|
| `bg-background` | 搜索栏外层容器 | 搜索栏背景色 |
| `text-muted-foreground` | 搜索图标、加载文案、搜索匹配计数 | 辅助/弱化文字色 |
| `text-primary` | Loading 旋转图标 | 主色调强调色 |
| `border-border` | 搜索栏底部分割线 | 统一边框色 |

### 子组件依赖

| 子组件 | 路径 | 说明 |
|--------|------|------|
| MessageList | `./MessageList` | 消息列表组件，接收过滤后的消息，支持滚动加载历史 |
| ChatInput | `./ChatInput` | 聊天输入组件，处理消息发送、停止生成、思考模式 |
| InteractionPanel | `./InteractionPanel` | 人类交互面板，展示需要用户输入的交互卡片 |
| ErrorBoundary | `@/components/ErrorBoundary` | 错误边界组件，包裹 InteractionPanel 防止异常扩散 |
| Button | `@/components/ui/button` | 基础 UI 按钮，用于搜索清除按钮 |
| Input | `@/components/ui/input` | 基础 UI 输入框，用于搜索输入 |

### 对外接口

| 接口 | 类型 | 说明 |
|------|------|------|
| `ChatContainer` | 命名导出组件 | 聊天容器组件，使用 `export const` 导出 |
| `ChatContainerProps` | TypeScript Interface | 组件 Props 类型，定义在 `./types.ts` 中 |

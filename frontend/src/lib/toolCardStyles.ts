/**
 * 工具内容展示统一样式常量
 *
 * 覆盖「工具卡片任意层级展开的超长内容」的统一滚动 + 语义换行样式：
 * - max-h-40 + overflow-y-auto：超长内容固定高度 + 纵向滚动条（任何层级展开都生效）
 * - break-words（overflow-wrap: break-word）：中文按语义换行，仅超长单词/URL 断词
 *   （禁止 break-all——word-break: break-all 会强制每字符断行，中文/长文本每字一行）
 *
 * 各组件引用此常量 + 各自的基础类（bg-muted/rounded/p-2/font-mono 等），
 * 避免每处手写遗漏，一处调整全站生效。
 */
export const TOOL_CONTENT_SCROLL_CLASS =
  'max-h-40 overflow-y-auto break-words whitespace-pre-wrap'

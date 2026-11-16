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

/**
 * 工具卡展开详情面板的限高 + 内部滚动样式
 *
 * 约束对象是「展开后的整个详情面板容器」（区别于 TOOL_CONTENT_SCROLL_CLASS
 * 约束的容器内单个内容块）：面板内各块自身已带滚动，块数多且每块都不足以触发
 * 单块上限时，总高会累加并撑满会话视口。此处给面板设视口相对上限，超出部分
 * 在面板内部纵向滚动，外层对话流高度不再被内容撑开；内容不足时按内容自适应收缩。
 *
 * - max-h-[60vh]：相对会话视口自适应，与项目既有卡片档位（InteractionCard
 *   40~60vh、NotificationItem 60vh）一致
 * - overflow-y-auto：超出部分在面板内部滚动
 * - overscroll-contain：滚动链隔离——面板滚到底不外溢带动外层会话滚动
 */
export const TOOL_DETAIL_PANEL_SCROLL_CLASS =
  'max-h-[60vh] overflow-y-auto overscroll-contain'

/**
 * 顶带统一按钮款式（设计系统单源）。
 *
 * 用户裁定：顶带所有按钮——窗口控制、区域开关、对话/工作区标签、以及未来的
 * 任何顶带按钮——共用同一基础款式：同高度、同位置、同圆角、同间隔；
 * 宽度由内容（文字长度 + 图标宽度）自然决定，不写死。
 * 个性化（圆角方角、配色）一律通过主题令牌变化（rounded-md = var(--radius-md)）。
 *
 * 使用规则：
 * - 任何顶带新按钮一律引用 BAND_BUTTON_CLASS（+ 按语义追加 ACTIVE 变体），
 *   禁止在组件里自写高度/圆角/内边距；
 * - 图标统一 BAND_BUTTON_ICON_CLASS；
 * - 拖拽面规则：按钮 app-no-drag，按钮以外的顶带区域由拖拽容器承载。
 */

/** 顶带统一按钮：高度 28px 居中于 40px 顶带；宽度 = 内容 + px-2；主题圆角 */
export const BAND_BUTTON_CLASS =
  'app-no-drag flex h-7 items-center justify-center gap-1 rounded-md px-2 text-sm transition-colors'

/** 激活态变体（当前所在区域/当前标签） */
export const BAND_BUTTON_ACTIVE_CLASS = 'bg-primary/15 text-primary font-medium'

/** 非激活变体 */
export const BAND_BUTTON_IDLE_CLASS = 'text-muted-foreground hover:bg-accent hover:text-foreground'

/** 顶带统一图标尺寸 */
export const BAND_BUTTON_ICON_CLASS = 'h-3.5 w-3.5 shrink-0'

/** 图标独占按钮（仅图标：宽度 = 图标 + 对称内边距） */
export const BAND_ICON_BUTTON_CLASS = `${BAND_BUTTON_CLASS} w-7 shrink-0 px-0`

/** 顶带统一间隔：任何承载顶带按钮/标签的 flex 容器一律引用本值，
    禁止在组件里另写 ml-/mr-（按钮间距只有一处可改） */
export const BAND_GAP_CLASS = 'gap-1'

/** 顶带两端留白（窗口边缘不贴按钮） */
export const BAND_EDGE_PADDING_CLASS = 'px-2'

/** 标签可压缩区间：内容超宽时先压到下限再横向滚动（下限保证仍可点选/拖拽） */
export const BAND_TAB_WIDTH_CLASS = 'shrink'
export const BAND_TAB_MIN_WIDTH_CLASS = 'min-w-[64px]'
export const BAND_TAB_MAX_WIDTH_CLASS = 'max-w-[200px]'


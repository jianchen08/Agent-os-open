/**
 * 文本选区 / 拖拽判定的 DOM 工具
 *
 * 背景：用鼠标在内容区拖选文字时，浏览器在 mouseup 之后**仍会派发 click**
 * （目标是 mousedown 与 mouseup 目标的最近公共祖先）。若不甄别，拖选会被
 * 当成「点击条目」→ 触发条目导航并关闭通知面板（表现为「一复制文字通知栏
 * 就关」）。
 */

/** 拖拽判定阈值（px）：指针位移超过该值视为拖拽选字而非点击 */
export const DRAG_THRESHOLD_PX = 5

/** 内容区内自带交互语义的元素选择器（点击它们由元素自身处理） */
const INTERACTIVE_SELECTOR =
  'a, button, input, textarea, select, [role="button"], [contenteditable="true"]'

/**
 * 是否存在落在 root 内的非折叠文本选区。
 *
 * @param root 选区判定根节点；省略时只判「是否存在非折叠选区」
 */
export function hasTextSelectionWithin(root?: Node | null): boolean {
  if (typeof window === 'undefined' || typeof window.getSelection !== 'function') {
    return false
  }
  const selection = window.getSelection()
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) {
    return false
  }
  if (!root) return true
  const { anchorNode, focusNode } = selection
  return (
    (anchorNode != null && root.contains(anchorNode)) ||
    (focusNode != null && root.contains(focusNode))
  )
}

/**
 * 指针位移是否超过拖拽阈值（mousedown 起点 → click 终点）。
 *
 * @param origin mousedown 时记录的坐标；无记录时视为未拖拽
 */
export function hasDraggedBeyond(
  origin: { x: number; y: number } | null,
  point: { clientX: number; clientY: number },
  threshold: number = DRAG_THRESHOLD_PX,
): boolean {
  if (!origin) return false
  const dx = point.clientX - origin.x
  const dy = point.clientY - origin.y
  return Math.hypot(dx, dy) > threshold
}

/**
 * 事件目标是否落在内容区自带交互语义的元素上（按钮 / 链接 / 表单控件）。
 *
 * 这类元素自己处理点击，不应再冒泡成条目级动作。
 */
export function isInteractiveTarget(target: EventTarget | null): boolean {
  if (!target || typeof (target as Element).closest !== 'function') return false
  return (target as Element).closest(INTERACTIVE_SELECTOR) !== null
}

/**
 * 键盘事件目标是否落在可编辑元素内（2026-11-05 R4 件②共通判定，禁复制）。
 *
 * 用途：document 级 Escape 等全局快捷键在输入法候选激活/取消时也会收到
 * keydown（落点=正在输入的可编辑元素），须按此守卫跳过退出类语义
 * （VS Code zen mode 同口径）。消费点：FiveSpaceLayout 全屏退出、引用浮条
 * Esc 关闭。
 */

/** 元素是否可编辑：textarea/input/contenteditable */
function isEditableElement(el: Element): boolean {
  if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) return true
  if (el instanceof HTMLElement && el.isContentEditable) return true
  return false
}

/**
 * 事件目标（及 composedPath 兜底）是否落在可编辑元素内。
 * 非元素目标（document/body 判定兜底）恒 false——body 上的 Escape 仍是退出语义。
 */
export function isEditableEventTarget(e: Event): boolean {
  const nodes: (EventTarget | null)[] = [e.target]
  if (typeof e.composedPath === 'function') {
    nodes.push(...e.composedPath().slice(0, 4))
  }
  for (const node of nodes) {
    if (node instanceof Element && isEditableElement(node)) return true
  }
  return false
}

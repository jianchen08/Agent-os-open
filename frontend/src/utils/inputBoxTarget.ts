/**
 * 输入框区域事件判定（2026-11-06 R7 Bug① 统一语义共通判定）。
 *
 * 统一语义（用户实测反馈）：引用浮条（CitationFloatBar）活跃期间，点击输入框
 * 区域 → 选区高亮保持、浮条保持，输入框同时获得焦点可打字；点击输入框外的
 * 其余区域 → 既有「点击别处关闭浮条」行为不变。
 *
 * 覆盖三个宿主容器：主输入框容器 chat-input-box、全屏浮窗容器 fullscreen-composer
 * 及其正文输入容器 fullscreen-composer-input-box（testid 契约真源在
 * ChatInput / FullscreenComposer）。
 *
 * 消费点：
 *  - CitationFloatBar：document 级 mousedown capture 保护区（preventDefault +
 *    手动聚焦，挡浏览器默认的「清除文档选区+原生聚焦」）；
 *  - useBubbleCitationBar：点击别处关浮条守卫（输入框区域豁免不关）。
 */

/** 输入框宿主容器选择器（三个 testid 容器） */
export const INPUT_BOX_SELECTOR =
  '[data-testid="chat-input-box"],[data-testid="fullscreen-composer"],[data-testid="fullscreen-composer-input-box"]'

/**
 * 事件目标（及 composedPath 兜底，同 utils/editableTarget 口径）落在输入框
 * 容器内 → 返回该容器元素；否则 null。
 */
export function closestInputBoxFromEvent(e: Event): HTMLElement | null {
  const nodes: (EventTarget | null)[] = [e.target]
  if (typeof e.composedPath === 'function') {
    nodes.push(...e.composedPath().slice(0, 4))
  }
  for (const node of nodes) {
    if (node instanceof Element) {
      const box = node.closest(INPUT_BOX_SELECTOR)
      if (box instanceof HTMLElement) return box
    }
  }
  return null
}

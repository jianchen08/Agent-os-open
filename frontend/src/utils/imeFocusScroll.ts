/**
 * 移动端 IME 对焦滚动：任何输入框聚焦时不得被软键盘遮挡
 *
 * 背景：移动壳在键盘（ime）弹出时把 WebView 底部垫高到键盘上沿
 * （MainActivity ime insets padding + adjustResize），布局视口随之变矮。
 * 主聊天输入框钉在视口底部天然保持可见；其余页面（设置表单/抽屉/弹层/登录）
 * 的输入框多位于嵌套滚动容器内，WebView 原生对焦滚动只处理根滚动层，
 * 不会滚嵌套容器，焦点元素会留在视口（= 键盘上沿）以下不可见。
 *
 * 契约：移动视口下，可编辑元素获得焦点或视口尺寸变化（键盘弹出/收起、旋转）
 * 时，把当前聚焦的可编辑元素滚入可视区（scrollIntoView block=nearest，
 * 逐层滚动祖先容器；已可见时零位移）。桌面视口全程不动作，行为零变化。
 */

import { isMobileViewportNow } from '@/hooks/useIsMobileViewport'

/** 不会唤起软键盘的 input 类型（点击聚焦不产生文本输入，无需对焦滚动） */
const NON_TEXT_INPUT_TYPES = new Set([
  'button',
  'submit',
  'reset',
  'image',
  'file',
  'checkbox',
  'radio',
  'range',
  'color',
])

/** 元素是否为会唤起软键盘的可编辑元素（只读/禁用态不唤起键盘，不参与） */
function isKeyboardEditable(el: Element | null): boolean {
  if (el instanceof HTMLTextAreaElement) return !el.readOnly && !el.disabled
  if (el instanceof HTMLInputElement) {
    if (el.readOnly || el.disabled) return false
    return !NON_TEXT_INPUT_TYPES.has(el.type)
  }
  if (el instanceof HTMLSelectElement) return !el.disabled
  return el instanceof HTMLElement && el.isContentEditable
}

/**
 * 安装全局 IME 对焦滚动监听（document focusin + window resize）。
 *
 * 滚动时机统一收敛到 rAF：焦点变化先于键盘弹出，键盘动画期间视口逐帧变矮、
 * resize 逐帧到达，同帧多次触发合并为一次滚动；真正滚动时读
 * document.activeElement（键盘弹完后聚焦元素可能已被重渲染替换）。
 *
 * @returns 卸载函数（移除监听并取消未决 rAF）
 */
export function installImeFocusScroll(): () => void {
  let rafId: number | null = null

  const scrollFocusedIntoView = () => {
    rafId = null
    const el = document.activeElement
    if (el !== null && isKeyboardEditable(el)) {
      el.scrollIntoView({ block: 'nearest' })
    }
  }

  const schedule = () => {
    if (!isMobileViewportNow()) return
    if (rafId === null) rafId = requestAnimationFrame(scrollFocusedIntoView)
  }

  const onFocusIn = (event: FocusEvent) => {
    if (!isKeyboardEditable(event.target as Element | null)) return
    schedule()
  }
  const onResize = () => schedule()

  document.addEventListener('focusin', onFocusIn)
  window.addEventListener('resize', onResize)
  return () => {
    document.removeEventListener('focusin', onFocusIn)
    window.removeEventListener('resize', onResize)
    if (rafId !== null) cancelAnimationFrame(rafId)
  }
}

/**
 * 移动端 IME 键盘遮挡治理：对焦滚动 + 视觉视口（visualViewport）缺口补偿
 *
 * 背景（两套键盘模型）：
 * - iOS Safari：键盘弹出不缩布局视口（window.innerHeight 不变），只缩视觉视口
 *   （visualViewport.height），页面被键盘遮挡的底部在布局坐标里仍算「可见」，
 *   钉底输入条会留在键盘之下——scrollIntoView 也救不了（元素按布局视口判可见）。
 * - Android Chrome/WebView：viewport meta 带 interactive-widget=resizes-content 时
 *   布局视口随键盘变矮；壳侧（MainActivity ime insets padding）健康时 WebView
 *   底部被垫高，布局视口同样变矮，钉底输入条天然可见。
 *
 * 契约：
 * 1. 对焦滚动——移动视口下可编辑元素获得焦点或视口尺寸变化时，把它滚入可视区
 *    （scrollIntoView block=nearest，逐层滚祖先容器；已可见时零位移），覆盖嵌套
 *    滚动容器内的输入框（WebView 原生对焦滚动只处理根滚动层）。
 * 2. 缺口补偿——gap = innerHeight − visualViewport.height − visualViewport.offsetTop，
 *    ≥ 阈值视为软键盘占位，写入根节点 CSS 变量 --ime-keyboard-gap；钉底输入条
 *    消费该变量上移到键盘上沿。布局视口已随键盘变矮的环境 gap≈0，变量恒 0px，
 *    与原生垫高零叠加。捏合缩放（vv.scale > 1）不是键盘，归零不补偿。
 * 3. 桌面视口全程不动作，行为零变化。
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
 * 键盘判定阈值：布局视口与视觉视口高度差 ≥ 此值视为软键盘占位。
 * 更小的差值归 iOS Safari 地址栏伸缩等浏览器 UI 变化，不补偿。
 */
const KEYBOARD_GAP_THRESHOLD_PX = 100

/** 视觉视口缩放比超出此值视为捏合缩放（键盘弹出不改变 scale），不作键盘判定 */
const PINCH_ZOOM_SCALE_LIMIT = 1.05

/** 键盘缺口 CSS 变量（写入 documentElement；钉底输入条消费：布局高度减去该值即上移到键盘上沿） */
export const IME_KEYBOARD_GAP_VAR = '--ime-keyboard-gap'

/**
 * 计算键盘在布局坐标中占用的高度（px）：布局视口 − 视觉视口顶部偏移 − 视觉视口高度。
 * 捏合缩放、低于阈值、负值一律归 0（不补偿）。
 */
function readKeyboardGapPx(vv: VisualViewport): number {
  const scale = typeof vv.scale === 'number' ? vv.scale : 1
  if (scale > PINCH_ZOOM_SCALE_LIMIT) return 0
  const gap = window.innerHeight - vv.offsetTop - vv.height
  return gap >= KEYBOARD_GAP_THRESHOLD_PX ? Math.round(gap) : 0
}

/**
 * 安装全局 IME 治理监听（document focusin + window resize + visualViewport resize/scroll）。
 *
 * 缺口变量同步写（visualViewport 事件本身按帧派发，无需再并帧）；对焦滚动统一收敛到
 * rAF：焦点变化先于键盘弹出，键盘动画期间视口逐帧变矮、事件逐帧到达，同帧多次触发
 * 合并为一次滚动；真正滚动时读 document.activeElement（键盘弹完后聚焦元素可能已被
 * 重渲染替换）。
 *
 * @returns 卸载函数（移除监听、取消未决 rAF；不再写入变量，保留卸载前最后的值）
 */
export function installImeFocusScroll(): () => void {
  let rafId: number | null = null
  // 老浏览器/纯净 jsdom 下该属性可能是 undefined 而非 null，判空必须用真值判定
  const vv: VisualViewport | undefined =
    typeof window !== 'undefined' ? (window.visualViewport ?? undefined) : undefined

  /** 把当前键盘缺口写入根节点变量；桌面视口/无 visualViewport 环境不写（消费端 fallback 0px） */
  const syncKeyboardGap = () => {
    if (!vv || !isMobileViewportNow()) return
    document.documentElement.style.setProperty(IME_KEYBOARD_GAP_VAR, `${readKeyboardGapPx(vv)}px`)
  }

  const scrollFocusedIntoView = () => {
    rafId = null
    const el = document.activeElement
    if (el !== null && isKeyboardEditable(el)) {
      el.scrollIntoView({ block: 'nearest' })
    }
  }

  const scheduleScroll = () => {
    if (!isMobileViewportNow()) return
    if (rafId === null) rafId = requestAnimationFrame(scrollFocusedIntoView)
  }

  const onFocusIn = (event: FocusEvent) => {
    if (!isKeyboardEditable(event.target as Element | null)) return
    scheduleScroll()
  }
  const onViewportChange = () => {
    syncKeyboardGap()
    scheduleScroll()
  }
  const onWindowResize = () => {
    syncKeyboardGap()
    scheduleScroll()
  }

  document.addEventListener('focusin', onFocusIn)
  window.addEventListener('resize', onWindowResize)
  vv?.addEventListener('resize', onViewportChange)
  vv?.addEventListener('scroll', onViewportChange)
  // 初始写入：页面加载/重挂载时键盘可能已在屏（冷启动直接落输入页）
  syncKeyboardGap()

  return () => {
    document.removeEventListener('focusin', onFocusIn)
    window.removeEventListener('resize', onWindowResize)
    vv?.removeEventListener('resize', onViewportChange)
    vv?.removeEventListener('scroll', onViewportChange)
    if (rafId !== null) cancelAnimationFrame(rafId)
  }
}

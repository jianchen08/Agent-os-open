/**
 * Android back 键语义桥（P1-8 移动端视觉优化：MuMu 实测浮窗/popover 打开时
 * 按 back 直接回退/退出应用，应先关浮层）。
 *
 * 契约：壳侧 MainActivity.handleOnBackPressed 在回退/退出之前 evaluateJavascript
 * 派发 `lingxi-back-press` CustomEvent（detail.consumed 共享引用），本模块是
 * 页面侧唯一消费者：
 * 1. 注册表 LIFO：浮层组件（FloatingWindowManager 底部 sheet、FormWidget
 *    紧凑选择器等）经 registerAndroidBackCloser 注册关闭器，back 事件按后注册
 *    先消费逐个尝试，任一关闭器返回 true = 本按消费（壳不再回退/退出）；
 * 2. 兜底腿：无注册关闭器消费时，探测页面上在场的 radix/antd 浮层
 *    （data-state=open 的 menu/dialog/listbox/tooltip、antd 下拉），派发合成
 *    Escape 交给既有 Escape 关闭语义（GlobalInteractionOverlay / radix
 *    DismissableLayer 均原生响应），派发后仍在场再补一次活动元素 blur
 *    （antd 下拉走 blur 关闭）；仅当浮层确实关闭才标记消费——关不掉不吞
 *    back，退回壳侧既有语义（历史回退 → 无历史退出）。
 *
 * fail-soft：无壳事件（桌面/浏览器/老壳）监听器空挂无副作用；关闭器抛错不
 * 阻断其余关闭器；页面未装本模块时壳侧 detail.consumed 恒 false 回退原语义。
 */

type BackCloser = () => boolean

/** 关闭器注册表（LIFO：后注册先消费——栈顶浮层先关） */
const closers: BackCloser[] = []

/** 壳事件名（壳侧 MainActivity 派发，命名空间 lingxi-*） */
const BACK_EVENT = 'lingxi-back-press'

/** 在场浮层探测选择器：radix 系（data-state=open 的角色化内容）+ antd 系（未隐藏下拉） */
const OVERLAY_OPEN_SELECTOR = [
  '[data-state="open"][role="menu"]',
  '[data-state="open"][role="dialog"]',
  '[data-state="open"][role="listbox"]',
  '[data-state="open"][role="tooltip"]',
  '.ant-select-dropdown:not(.ant-select-dropdown-hidden)',
].join(',')

function hasOpenOverlay(): boolean {
  if (typeof document === 'undefined') return false
  return !!document.querySelector(OVERLAY_OPEN_SELECTOR)
}

/** 兜底腿：合成 Escape +（仍未关时）活动元素 blur；返回浮层是否确实关闭 */
function dismissOverlaysViaEscape(): boolean {
  document.dispatchEvent(
    new KeyboardEvent('keydown', {
      key: 'Escape',
      code: 'Escape',
      keyCode: 27,
      which: 27,
      bubbles: true,
      cancelable: true,
    }),
  )
  if (!hasOpenOverlay()) return true
  const active = document.activeElement
  if (active instanceof HTMLElement && active !== document.body) active.blur()
  return !hasOpenOverlay()
}

let installed = false
/** 壳事件监听句柄（测试 reset 摘除用；生产生命周期=页面级不摘） */
let listener: EventListener | null = null

/** 安装壳事件监听（首次注册时惰性执行一次；重复调用幂等） */
function ensureInstalled() {
  if (installed || typeof window === 'undefined') return
  installed = true
  listener = ((event: CustomEvent<{ consumed: boolean }>) => {
    // 1) 注册表 LIFO：栈顶浮层关闭器先试
    for (let i = closers.length - 1; i >= 0; i--) {
      let consumed = false
      try {
        consumed = closers[i]()
      } catch {
        // 单个关闭器异常不阻断其余（fail-soft，下一关闭器继续）
      }
      if (consumed) {
        event.detail.consumed = true
        return
      }
    }
    // 2) 兜底腿：页面在场的 radix/antd 浮层交给既有 Escape 语义
    if (hasOpenOverlay() && dismissOverlaysViaEscape()) {
      event.detail.consumed = true
    }
  }) as EventListener
  window.addEventListener(BACK_EVENT, listener)
}

/**
 * 注册 back 关闭器。返回注销函数（组件卸载调用，防泄漏）。
 * closer 返回 true = 本按已被消费（壳侧不再回退/退出）。
 */
export function registerAndroidBackCloser(closer: BackCloser): () => void {
  closers.push(closer)
  return () => {
    const index = closers.indexOf(closer)
    if (index >= 0) closers.splice(index, 1)
  }
}

// 模块导入即安装（非惰性）：兜底腿（Escape 关在场浮层）必须在零注册关闭器
// 时也生效——本模块仅被聊天页常驻组件导入，导入即装语义等价「页面在壳内运行」
ensureInstalled()

/** 测试辅助：摘除监听、清空注册表并按「导入即装」语义重装（生产零调用） */
export function __resetAndroidBackBridgeForTests() {
  if (listener && typeof window !== 'undefined') {
    window.removeEventListener(BACK_EVENT, listener)
  }
  listener = null
  closers.length = 0
  installed = false
  ensureInstalled()
}

/**
 * onboarding「直达 llm_setup 向导」焦点通道（自动引导与面板之间的窄接口）。
 *
 * 直达向导闩 + 事件双通道：新挂载的面板消费闩（事件先于挂载发出的场景），
 * 已挂载的面板收事件（面板已开、不重挂载的场景）。
 *
 * 独立成模块的原因：面板侧（OnboardingPanel）只依赖这条窄通道，不该静态
 * 拉进 autoOpen 的判定链（autoOpen 由 GrowthLoop 动态导入，静态消费方会让
 * 该拆分意图失效）。
 */

/** 自动打开直达请求事件（OnboardingPanel 订阅：已挂载面板收到即展开向导） */
export const LLM_WIZARD_FOCUS_EVENT = 'lingxi:onboarding:llm-wizard-focus'

/** 直达闩（模块级：事件先于面板挂载发出时由面板挂载消费） */
let pendingFocus = false

/** 自动打开时请求面板直达 llm_setup 向导第 1 步 */
export function requestLlmWizardFocus(): void {
  pendingFocus = true
  window.dispatchEvent(new CustomEvent(LLM_WIZARD_FOCUS_EVENT))
}

/** 面板挂载时取走直达闩（取走即清，防后续手动进入误触直达） */
export function consumeLlmWizardFocus(): boolean {
  const v = pendingFocus
  pendingFocus = false
  return v
}

/** 订阅直达请求事件（面板已挂载场景；返回取消函数） */
export function subscribeLlmWizardFocus(handler: () => void): () => void {
  window.addEventListener(LLM_WIZARD_FOCUS_EVENT, handler)
  return () => window.removeEventListener(LLM_WIZARD_FOCUS_EVENT, handler)
}

/** @feature FP-0.2.移动端适配 净装态 onboarding | @ci: frontend-test */
/**
 * onboarding 直达 llm_setup 向导焦点通道（services/onboarding/llmWizardFocus.ts）：
 * - 事件通道：订阅方收到 requestLlmWizardFocus 的派发；退订后不再收到；
 * - 闩通道：request 后 consume 取走即清（二次 consume 为 false），防后续手动
 *   进入误触直达。
 */
import { describe, expect, it, vi } from 'vitest'
import {
  consumeLlmWizardFocus,
  requestLlmWizardFocus,
  subscribeLlmWizardFocus,
} from '../llmWizardFocus'

describe('直达向导焦点通道', () => {
  it('requestLlmWizardFocus：事件订阅方收到通知；闩取走即清', () => {
    const handler = vi.fn()
    const off = subscribeLlmWizardFocus(handler)
    requestLlmWizardFocus()
    off()
    expect(handler).toHaveBeenCalledTimes(1)
    expect(consumeLlmWizardFocus()).toBe(true)
    expect(consumeLlmWizardFocus()).toBe(false)
  })

  it('退订后不再收事件（监听器按订阅生命周期挂摘）', () => {
    const handler = vi.fn()
    const off = subscribeLlmWizardFocus(handler)
    off()
    requestLlmWizardFocus()
    expect(handler).not.toHaveBeenCalled()
  })
})

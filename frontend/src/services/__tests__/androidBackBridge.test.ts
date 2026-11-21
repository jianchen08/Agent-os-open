// @feature FP-0.2.mobile_visual_p1 @ci: frontend-test
/**
 * Android back 键语义桥（P1-8）：
 * - 壳事件 detail.consumed 共享置位：注册关闭器 LIFO 消费；任一 true 即消费；
 * - 关闭器抛错不阻断其余（fail-soft）；
 * - 兜底腿：在场 radix 浮层（data-state=open role=dialog/menu）经合成 Escape
 *   语义关闭后标记消费；关不掉（仍开场）不消费——退回壳侧回退语义不吞键；
 * - 注销后不再消费（浮层关闭即撤注册）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  __resetAndroidBackBridgeForTests,
  registerAndroidBackCloser,
} from '@/services/androidBackBridge'

/** 模拟壳侧派发（MainActivity evaluateJavascript 同构：同步 dispatch 后读 consumed） */
function dispatchShellBack(): boolean {
  const detail = { consumed: false }
  window.dispatchEvent(new CustomEvent('lingxi-back-press', { detail }))
  return detail.consumed
}

function mountOverlay(role: 'dialog' | 'menu' = 'dialog') {
  const el = document.createElement('div')
  el.setAttribute('data-state', 'open')
  el.setAttribute('role', role)
  document.body.appendChild(el)
  return el
}

beforeEach(() => {
  __resetAndroidBackBridgeForTests()
})

afterEach(() => {
  document.querySelectorAll('[data-state="open"][role]').forEach((el) => el.remove())
})

describe('androidBackBridge — back 消费契约（P1-8）', () => {
  it('注册关闭器返回 true → 消费（壳不回退）；false → 不消费', () => {
    const unregister = registerAndroidBackCloser(() => true)
    expect(dispatchShellBack()).toBe(true)
    unregister()
    expect(dispatchShellBack()).toBe(false)
  })

  it('LIFO：栈顶（后注册）先消费；栈顶 false 不阻断次顶（继续向下找）', () => {
    const order: string[] = []
    const uBottom = registerAndroidBackCloser(() => {
      order.push('bottom')
      return true
    })
    const uTop = registerAndroidBackCloser(() => {
      order.push('top')
      return false // 栈顶放弃消费（如浮层已自关）
    })
    expect(dispatchShellBack()).toBe(true)
    expect(order).toEqual(['top', 'bottom'])
    uBottom()
    uTop()
  })

  it('关闭器抛错不阻断其余（fail-soft）', () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    registerAndroidBackCloser(() => {
      throw new Error('boom')
    })
    const u2 = registerAndroidBackCloser(() => true)
    expect(dispatchShellBack()).toBe(true)
    u2()
    errSpy.mockRestore()
  })

  it('兜底腿：在场 radix 浮层被合成 Escape 关闭（document 级监听器消费 Escape 后撤场）→ 消费', () => {
    const overlay = mountOverlay('dialog')
    // radix DismissableLayer 同款：document 级 keydown 监听，Escape 即关层
    const onKeydown = (e: Event) => {
      if ((e as KeyboardEvent).key === 'Escape') overlay.remove()
    }
    document.addEventListener('keydown', onKeydown)
    try {
      expect(dispatchShellBack()).toBe(true)
      expect(document.body.contains(overlay)).toBe(false)
    } finally {
      document.removeEventListener('keydown', onKeydown)
    }
  })

  it('关不掉的浮层不消费（不吞 back，壳走既有回退语义）', () => {
    mountOverlay('menu') // 无监听器：Escape 关不掉
    expect(dispatchShellBack()).toBe(false)
  })

  it('无浮层无关闭器：不消费（浮窗语义下 back 行为不变的前提）', () => {
    expect(dispatchShellBack()).toBe(false)
  })
})

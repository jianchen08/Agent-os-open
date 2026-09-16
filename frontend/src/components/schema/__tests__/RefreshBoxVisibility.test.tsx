// @feature: FP-0.2.四 前端Schema驱动 | @ci: frontend-test
/**
 * RefreshBox 离屏暂停契约测试
 *
 * - 不可见（面板 tab display:none / 窗口最小化）时冻结轮询：时间推进 reloadKey 不前进
 * - 恢复可见立即补拉一次（不等下一周期），轮询随之恢复
 * - 无轮询声明（intervalSeconds=0）不参与暂停/补拉，恢复可见不产生多余重挂载
 *
 * jsdom 无 IntersectionObserver，用可手动触发回调的替身驱动可见性。
 * 断言 reloadKey 终值：假定时器批量推进时 React 合并多次 setReloadKey，
 * 逐帧 render 序列不可观测（批量合并后跳变），终值单调前进是稳定契约。
 */
import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RefreshBox } from '../RefreshBox'

let ioCb: IntersectionObserverCallback | null = null
let ioTargets: Element[] = []

function stubIntersectionObserver() {
  vi.stubGlobal(
    'IntersectionObserver',
    class {
      constructor(cb: IntersectionObserverCallback) {
        ioCb = cb
      }
      observe(target: Element) {
        ioTargets.push(target)
      }
      unobserve() {}
      disconnect() {}
    },
  )
}

function fireVisible(visible: boolean) {
  act(() => {
    ioCb?.(
      ioTargets.map((target) => ({ target, isIntersecting: visible }) as unknown as IntersectionObserverEntry),
      {} as IntersectionObserver,
    )
  })
}

/** 记录 render 收到的最新 reloadKey（见头注：断言终值而非逐帧序列） */
function makeKeyTracker() {
  let current: number | null = null
  return {
    record: (key: number) => {
      current = key
    },
    current: () => current,
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  ioCb = null
  ioTargets = []
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('RefreshBox 离屏暂停', () => {
  it('不可见时冻结轮询：时间推进 reloadKey 不前进', () => {
    stubIntersectionObserver()
    const t = makeKeyTracker()
    render(
      <RefreshBox refresh={{ type: 'poll', intervalSeconds: 1 }}>
        {t.record}
      </RefreshBox>,
    )
    fireVisible(false)

    act(() => {
      vi.advanceTimersByTime(3500)
    })
    expect(t.current()).toBe(0)
  })

  it('恢复可见立即补拉一次并恢复轮询节奏', () => {
    stubIntersectionObserver()
    const t = makeKeyTracker()
    render(
      <RefreshBox refresh={{ type: 'poll', intervalSeconds: 1 }}>
        {t.record}
      </RefreshBox>,
    )
    fireVisible(false)
    act(() => {
      vi.advanceTimersByTime(2500)
    })
    expect(t.current()).toBe(0)

    fireVisible(true)
    // 补拉一次
    expect(t.current()).toBe(1)
    // 轮询恢复：两个周期推进两拍
    act(() => {
      vi.advanceTimersByTime(2000)
    })
    expect(t.current()).toBe(3)
  })

  it('可见性未 stub（默认 fail-open 可见）时轮询照常', () => {
    const t = makeKeyTracker()
    render(
      <RefreshBox refresh={{ type: 'poll', intervalSeconds: 1 }}>
        {t.record}
      </RefreshBox>,
    )
    act(() => {
      vi.advanceTimersByTime(2100)
    })
    expect(t.current()).toBe(2)
  })

  it('intervalSeconds=0：恢复可见不补拉（无轮询声明无新鲜度语义）', () => {
    stubIntersectionObserver()
    const t = makeKeyTracker()
    render(
      <RefreshBox refresh={{ type: 'poll', intervalSeconds: 0 }}>
        {t.record}
      </RefreshBox>,
    )
    fireVisible(false)
    fireVisible(true)
    fireVisible(false)
    fireVisible(true)
    act(() => {
      vi.advanceTimersByTime(3000)
    })
    expect(t.current()).toBe(0)
  })
})

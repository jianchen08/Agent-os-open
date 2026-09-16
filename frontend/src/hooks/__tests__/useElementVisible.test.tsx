// @feature: FP-T12 前端连接层/渲染链路 | @ci: frontend-test
/**
 * useElementVisible 契约测试
 *
 * 可见 = IO 相交 且 document 未隐藏；无 IO 环境 fail-open（保持可见）。
 * jsdom 无 IntersectionObserver，用可手动触发回调的替身驱动状态迁移。
 */
import { act, render, renderHook, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useElementVisible } from '../useElementVisible'

/** 可手动触发回调的 IntersectionObserver 替身（观察目标注册表供触发用） */
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

/** 以指定相交状态触发回调 */
function fireIntersecting(isIntersecting: boolean) {
  act(() => {
    ioCb?.(
      ioTargets.map((target) => ({ target, isIntersecting }) as unknown as IntersectionObserverEntry),
      {} as IntersectionObserver,
    )
  })
}

function setDocumentVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
  act(() => {
    document.dispatchEvent(new Event('visibilitychange'))
  })
}

describe('useElementVisible', () => {
  beforeEach(() => {
    ioCb = null
    ioTargets = []
    setDocumentVisibility('visible')
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => 'visible',
    })
  })

  it('无 IntersectionObserver 环境（jsdom 原生）：fail-open 保持可见', () => {
    const { result } = renderHook(() => useElementVisible<HTMLDivElement>())
    expect(result.current.visible).toBe(true)
  })

  it('IO 报告不相交 → 不可见；恢复相交 → 可见', () => {
    stubIntersectionObserver()
    function Probe() {
      const { ref, visible } = useElementVisible<HTMLDivElement>()
      return <div ref={ref} data-testid="probe" data-visible={String(visible)} />
    }
    render(<Probe />)
    expect(screen.getByTestId('probe').dataset.visible).toBe('true')

    fireIntersecting(false)
    expect(screen.getByTestId('probe').dataset.visible).toBe('false')

    fireIntersecting(true)
    expect(screen.getByTestId('probe').dataset.visible).toBe('true')
  })

  it('ref 未绑定元素（早期返回分支）：保持可见', () => {
    // 不 stub IO、不渲染带 ref 的元素——等价 el==null 分支
    const { result } = renderHook(() => useElementVisible<HTMLDivElement>())
    expect(result.current.visible).toBe(true)
  })

  it('document 隐藏（最小化/遮挡）→ 不可见；恢复 → 可见', () => {
    stubIntersectionObserver()
    function Probe() {
      const { ref, visible } = useElementVisible<HTMLDivElement>()
      return <div ref={ref} data-testid="probe" data-visible={String(visible)} />
    }
    render(<Probe />)
    expect(screen.getByTestId('probe').dataset.visible).toBe('true')

    setDocumentVisibility('hidden')
    expect(screen.getByTestId('probe').dataset.visible).toBe('false')

    setDocumentVisibility('visible')
    expect(screen.getByTestId('probe').dataset.visible).toBe('true')
  })

  it('IO 不相交 + document 隐藏同时成立，任一恢复仍需另一条成立（合取语义）', () => {
    stubIntersectionObserver()
    function Probe() {
      const { ref, visible } = useElementVisible<HTMLDivElement>()
      return <div ref={ref} data-testid="probe" data-visible={String(visible)} />
    }
    render(<Probe />)

    fireIntersecting(false)
    setDocumentVisibility('hidden')
    expect(screen.getByTestId('probe').dataset.visible).toBe('false')

    fireIntersecting(true)
    // document 仍隐藏 → 仍不可见
    expect(screen.getByTestId('probe').dataset.visible).toBe('false')

    setDocumentVisibility('visible')
    expect(screen.getByTestId('probe').dataset.visible).toBe('true')
  })

  it('ref 绑定到渲染元素后 IO 观察', () => {
    stubIntersectionObserver()
    function Probe() {
      const { ref, visible } = useElementVisible<HTMLDivElement>()
      return (
        <div ref={ref} data-testid="probe" data-visible={String(visible)} />
      )
    }
    render(<Probe />)
    expect(ioTargets).toHaveLength(1)
    expect(ioTargets[0]).toBe(screen.getByTestId('probe'))
    fireIntersecting(false)
    expect(screen.getByTestId('probe').dataset.visible).toBe('false')
  })
})

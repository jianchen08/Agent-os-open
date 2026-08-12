/**
 * WindowManager 服务测试（阶段5 detachable P1）
 *
 * 覆盖：
 * - openPopout(page) 后 floatingWindows 含该 window
 * - window 的 size 来自 page.detachable.defaultSize
 * - page.detachable.popout === false 时 openPopout 不加 window（返回空）
 * - openChildWindow / openDesktopWidget 在 Web 版降级到 popout（也加 floatingWindow）
 * - close(windowId) 移除 window
 * - openPopout 返回稳定的 windowId（含 page.id）
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { windowManager, WebWindowManager } from '@/services/window/WindowManager'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'

function makePage(overrides: Partial<PageDeclaration> = {}): PageDeclaration {
  return {
    type: 'pages',
    id: 'test-page',
    title: '测试页面',
    space: 'workspace',
    ...overrides,
  } as PageDeclaration
}

describe('WindowManager — openPopout', () => {
  beforeEach(() => {
    // 重置浮窗状态，确保用例间隔离
    useLayoutModeStore.setState({ floatingWindows: [] })
  })

  it('openPopout 后 floatingWindows 含该 window', () => {
    const page = makePage({ detachable: { popout: true } })
    const id = windowManager.openPopout(page)

    expect(id).toBeTruthy()
    const wins = useLayoutModeStore.getState().floatingWindows
    expect(wins).toHaveLength(1)
    expect(wins[0].id).toBe(id)
    expect(wins[0].title).toBe('测试页面')
  })

  it('返回的 windowId 含 page.id 与 popout 标记', () => {
    const page = makePage({ id: 'my-page', detachable: { popout: true } })
    const id = windowManager.openPopout(page)
    expect(id).toContain('my-page')
    expect(id).toContain('popout')
  })

  it('window 的 size 来自 page.detachable.defaultSize', () => {
    const page = makePage({
      detachable: { popout: true, defaultSize: { w: 400, h: 600 } },
    })
    windowManager.openPopout(page)

    const win = useLayoutModeStore.getState().floatingWindows[0]
    expect(win.size.width).toBe(400)
    expect(win.size.height).toBe(600)
  })

  it('opts.size 覆盖 detachable.defaultSize', () => {
    const page = makePage({
      detachable: { popout: true, defaultSize: { w: 400, h: 600 } },
    })
    windowManager.openPopout(page, { size: { w: 200, h: 200 } })

    const win = useLayoutModeStore.getState().floatingWindows[0]
    expect(win.size.width).toBe(200)
    expect(win.size.height).toBe(200)
  })

  it('无 defaultSize 时使用内置默认尺寸', () => {
    const page = makePage({ detachable: { popout: true } })
    windowManager.openPopout(page)
    const win = useLayoutModeStore.getState().floatingWindows[0]
    expect(win.size.width).toBeGreaterThan(0)
    expect(win.size.height).toBeGreaterThan(0)
  })

  it('opts.position 透传到 window.position', () => {
    const page = makePage({ detachable: { popout: true } })
    windowManager.openPopout(page, { position: { x: 10, y: 20 } })
    const win = useLayoutModeStore.getState().floatingWindows[0]
    expect(win.position).toEqual({ x: 10, y: 20 })
  })

  it('page.detachable.popout === false 时不加 window 并返回空串', () => {
    const page = makePage({ detachable: { popout: false } })
    const id = windowManager.openPopout(page)
    expect(id).toBe('')
    expect(useLayoutModeStore.getState().floatingWindows).toHaveLength(0)
  })

  it('新窗口 zIndex 高于已有窗口（置顶）', () => {
    const page = makePage({ detachable: { popout: true } })
    const id1 = windowManager.openPopout(page)
    const id2 = windowManager.openPopout(page)
    const wins = useLayoutModeStore.getState().floatingWindows
    const z1 = wins.find((w) => w.id === id1)!.zIndex
    const z2 = wins.find((w) => w.id === id2)!.zIndex
    expect(z2).toBeGreaterThan(z1)
  })
})

describe('WindowManager — Web 版降级', () => {
  beforeEach(() => {
    useLayoutModeStore.setState({ floatingWindows: [] })
  })

  it('openChildWindow 降级到 popout（也加 floatingWindow）', () => {
    const infoSpy = vi.spyOn(console, 'info').mockImplementation(() => {})
    const page = makePage({ detachable: { childWindow: true } })
    const id = windowManager.openChildWindow(page)

    expect(id).toBeTruthy()
    expect(useLayoutModeStore.getState().floatingWindows).toHaveLength(1)
    expect(infoSpy).toHaveBeenCalled()
    infoSpy.mockRestore()
  })

  it('openDesktopWidget 降级到 popout（也加 floatingWindow）', () => {
    const infoSpy = vi.spyOn(console, 'info').mockImplementation(() => {})
    const page = makePage({ detachable: { desktopWidget: true } })
    const id = windowManager.openDesktopWidget(page)

    expect(id).toBeTruthy()
    expect(useLayoutModeStore.getState().floatingWindows).toHaveLength(1)
    expect(infoSpy).toHaveBeenCalled()
    infoSpy.mockRestore()
  })

  it('childWindow 降级时即便 detachable.popout 未声明也能弹出', () => {
    // popout 未声明（undefined），childWindow:true —— 降级路径不应被 popout 门控阻断
    const page = makePage({ detachable: { childWindow: true } })
    const id = windowManager.openChildWindow(page)
    expect(id).toBeTruthy()
    expect(useLayoutModeStore.getState().floatingWindows).toHaveLength(1)
  })
})

describe('WindowManager — close / focus', () => {
  beforeEach(() => {
    useLayoutModeStore.setState({ floatingWindows: [] })
  })

  it('close(windowId) 移除对应 window', () => {
    const page = makePage({ detachable: { popout: true } })
    const id = windowManager.openPopout(page)
    expect(useLayoutModeStore.getState().floatingWindows).toHaveLength(1)

    windowManager.close(id)
    expect(useLayoutModeStore.getState().floatingWindows).toHaveLength(0)
  })

  it('close 未知 id 不抛错（幂等）', () => {
    expect(() => windowManager.close('nonexistent')).not.toThrow()
  })

  it('focus(windowId) 提升该窗口 zIndex 至顶层', () => {
    const page = makePage({ detachable: { popout: true } })
    const id1 = windowManager.openPopout(page)
    const id2 = windowManager.openPopout(page)
    const wins = () => useLayoutModeStore.getState().floatingWindows

    const z1Before = wins().find((w) => w.id === id1)!.zIndex
    const z2Before = wins().find((w) => w.id === id2)!.zIndex
    expect(z2Before).toBeGreaterThan(z1Before)

    windowManager.focus(id1)
    const z1After = wins().find((w) => w.id === id1)!.zIndex
    expect(z1After).toBeGreaterThan(z2Before)
  })
})

describe('WindowManager — 实例化', () => {
  it('导出的 windowManager 是 WebWindowManager 实例', () => {
    expect(windowManager).toBeInstanceOf(WebWindowManager)
  })

  it('可独立 new WebWindowManager（无单例污染）', () => {
    const mgr = new WebWindowManager()
    const page = makePage({ id: 'solo', detachable: { popout: true } })
    const id = mgr.openPopout(page)
    expect(id).toContain('solo')
  })
})

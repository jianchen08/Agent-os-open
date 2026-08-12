/**
 * ElectronWindowManager 测试（P2/P3 多窗口基础设施）
 *
 * 覆盖：
 * - 检测 window.electronAPI 存在时使用 Electron 实现（调用 electronAPI.window.open 真桌面窗口）
 * - openChildWindow 调 electronAPI.window.open,url 含 '/#/p/<pageId>'
 * - openDesktopWidget 透传 alwaysOnTop / skipTaskbar / frame=false
 * - close / focus / openPopout（popout 在 Electron 下也走原生窗口）
 * - electronAPI 不存在时降级到 Web popout（fallback 路径）
 * - detachable 门控：openChildWindow 在 childWindow===false 时仍允许（基础设施宽松门控，
 *   真正的禁止由页面级配置在调用前过滤）
 *
 * 通过 mock window.electronAPI 模拟 Electron 环境；用 useLayoutModeStore 状态变化
 * 验证 Web 降级路径。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import type { ElectronWindowAPI, ElectronOpenWindowOptions } from '@/types/electron'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'

// 动态导入避免在 mock 设置前触发模块级单例选择（windowManager 的实现选择在模块加载时执行）
async function importManager() {
  return await import('@/services/window/WindowManager')
}

function makePage(overrides: Partial<PageDeclaration> = {}): PageDeclaration {
  return {
    type: 'pages',
    id: 'test-page',
    title: '测试页面',
    space: 'workspace',
    ...overrides,
  } as PageDeclaration
}

/** 构造一个 spy 化的 ElectronWindowAPI */
function makeElectronWindowSpy() {
  return {
    open: vi.fn<(ElectronWindowAPI['open'] extends (o: infer O) => unknown ? O : never)>().mockResolvedValue({ id: 'stub', success: true }),
    close: vi.fn().mockResolvedValue(undefined),
    focus: vi.fn().mockResolvedValue(undefined),
    move: vi.fn().mockResolvedValue(undefined),
    resize: vi.fn().mockResolvedValue(undefined),
  } as unknown as ElectronWindowAPI & {
    open: vi.Mock
    close: vi.Mock
    focus: vi.Mock
    move: vi.Mock
    resize: vi.Mock
  }
}

describe('ElectronWindowManager — Electron 环境', () => {
  let spy: ReturnType<typeof makeElectronWindowSpy>

  beforeEach(() => {
    useLayoutModeStore.setState({ floatingWindows: [] })
    spy = makeElectronWindowSpy()
    ;(window as unknown as { electronAPI: unknown }).electronAPI = { window: spy }
  })

  afterEach(() => {
    delete (window as unknown as { electronAPI?: unknown }).electronAPI
    vi.restoreAllMocks()
  })

  it('openChildWindow 调 electronAPI.window.open,url 含 /#/p/<pageId>', async () => {
    const { ElectronWindowManager } = await importManager()
    const mgr = new ElectronWindowManager()
    const page = makePage({ id: 'my-page', detachable: { childWindow: true } })

    const id = mgr.openChildWindow(page)

    // ElectronWindowManager.openChildWindow 返回同步 id（基于 page.id + 计数器）
    expect(id).toContain('my-page')
    expect(spy.open).toHaveBeenCalledTimes(1)
    const opts = spy.open.mock.calls[0][0] as ElectronOpenWindowOptions
    expect(opts.url).toContain('/#/p/my-page')
    expect(opts.id).toBe(id)
  })

  it('openChildWindow 透传 detachable.defaultSize 到 width/height', async () => {
    const { ElectronWindowManager } = await importManager()
    const mgr = new ElectronWindowManager()
    const page = makePage({
      id: 'size-page',
      detachable: { childWindow: true, defaultSize: { w: 400, h: 600 } },
    })

    mgr.openChildWindow(page)

    const opts = spy.open.mock.calls[0][0] as ElectronOpenWindowOptions
    expect(opts.width).toBe(400)
    expect(opts.height).toBe(600)
  })

  it('openChildWindow 透传 opts.position / opts.size', async () => {
    const { ElectronWindowManager } = await importManager()
    const mgr = new ElectronWindowManager()
    const page = makePage({ id: 'pos-page', detachable: { childWindow: true } })

    mgr.openChildWindow(page, {
      position: { x: 10, y: 20 },
      size: { w: 250, h: 350 },
    })

    const opts = spy.open.mock.calls[0][0] as ElectronOpenWindowOptions
    expect(opts.x).toBe(10)
    expect(opts.y).toBe(20)
    expect(opts.width).toBe(250)
    expect(opts.height).toBe(350)
  })

  it('openDesktopWidget 默认 alwaysOnTop / skipTaskbar / frame=false', async () => {
    const { ElectronWindowManager } = await importManager()
    const mgr = new ElectronWindowManager()
    const page = makePage({ id: 'widget-page', detachable: { desktopWidget: true } })

    mgr.openDesktopWidget(page)

    const opts = spy.open.mock.calls[0][0] as ElectronOpenWindowOptions
    expect(opts.alwaysOnTop).toBe(true)
    expect(opts.skipTaskbar).toBe(true)
    expect(opts.frame).toBe(false)
    expect(opts.url).toContain('/#/p/widget-page')
  })

  it('openPopout 在 Electron 下也走原生窗口（不再写 floatingWindows）', async () => {
    const { ElectronWindowManager } = await importManager()
    const mgr = new ElectronWindowManager()
    const page = makePage({ id: 'popout-page', detachable: { popout: true } })

    mgr.openPopout(page)

    expect(spy.open).toHaveBeenCalledTimes(1)
    expect(useLayoutModeStore.getState().floatingWindows).toHaveLength(0)
  })

  it('close(id) 调 electronAPI.window.close', async () => {
    const { ElectronWindowManager } = await importManager()
    const mgr = new ElectronWindowManager()
    mgr.close('some-id')
    expect(spy.close).toHaveBeenCalledWith('some-id')
  })

  it('focus(id) 调 electronAPI.window.focus', async () => {
    const { ElectronWindowManager } = await importManager()
    const mgr = new ElectronWindowManager()
    mgr.focus('some-id')
    expect(spy.focus).toHaveBeenCalledWith('some-id')
  })

  it('detachable.alwaysOnTop 透传到 electronAPI.window.open', async () => {
    const { ElectronWindowManager } = await importManager()
    const mgr = new ElectronWindowManager()
    const page = makePage({
      id: 'top-page',
      detachable: { childWindow: true, alwaysOnTop: true },
    })

    mgr.openChildWindow(page)

    const opts = spy.open.mock.calls[0][0] as ElectronOpenWindowOptions
    expect(opts.alwaysOnTop).toBe(true)
  })
})

describe('ElectronWindowManager — 检测与降级', () => {
  beforeEach(() => {
    useLayoutModeStore.setState({ floatingWindows: [] })
  })

  it('isElectronWindowAvailable() 在 electronAPI.window 存在时返回 true', async () => {
    const { isElectronWindowAvailable } = await importManager()
    ;(window as unknown as { electronAPI: unknown }).electronAPI = { window: makeElectronWindowSpy() }
    expect(isElectronWindowAvailable()).toBe(true)
    delete (window as unknown as { electronAPI?: unknown }).electronAPI
  })

  it('isElectronWindowAvailable() 在无 electronAPI 时返回 false', async () => {
    const { isElectronWindowAvailable } = await importManager()
    delete (window as unknown as { electronAPI?: unknown }).electronAPI
    expect(isElectronWindowAvailable()).toBe(false)
  })

  it('无 electronAPI 时,独立 new ElectronWindowManager 降级到 Web popout（写 floatingWindows）', async () => {
    const { ElectronWindowManager } = await importManager()
    delete (window as unknown as { electronAPI?: unknown }).electronAPI
    const mgr = new ElectronWindowManager()
    const infoSpy = vi.spyOn(console, 'info').mockImplementation(() => {})

    const page = makePage({ id: 'fallback-page', detachable: { childWindow: true } })
    const id = mgr.openChildWindow(page)

    expect(id).toBeTruthy()
    expect(useLayoutModeStore.getState().floatingWindows).toHaveLength(1)
    expect(infoSpy).toHaveBeenCalled()
    infoSpy.mockRestore()
  })
})

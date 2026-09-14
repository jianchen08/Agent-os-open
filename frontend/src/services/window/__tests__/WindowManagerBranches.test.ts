/**
 * WindowManager 分支补测：补齐既有三份测试未触达的分支——
 * Web 版「无 window 环境」的视口兜底、zIndex 无浮窗时的基数、
 * Electron 版 open 失败的 error 日志（fire-and-forget catch）、
 * electronAPI 运行期消失时的三条降级路径（openPopout/close/focus）。
 *
 * 断言可观察行为：store 中的浮窗状态变化、返回的 windowId、console 输出；
 * electronAPI 作为外部依赖（宿主进程）用 stub 替换。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { WebWindowManager, ElectronWindowManager, isElectronWindowAvailable } from '../WindowManager'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'

const page = (extra: Partial<PageDeclaration> = {}): PageDeclaration =>
  ({ id: 'pg-1', title: '页面一', ...extra }) as PageDeclaration

/** 安装 electronAPI stub（window.open/close/focus 返回可控 Promise） */
function stubElectron(overrides: Record<string, unknown> = {}) {
  const api = {
    window: {
      open: vi.fn().mockResolvedValue({ id: 'ewin-1', success: true }),
      close: vi.fn().mockResolvedValue({ success: true }),
      focus: vi.fn().mockResolvedValue({ success: true }),
      ...overrides,
    },
  }
  ;(window as unknown as { electronAPI: unknown }).electronAPI = api
  return api
}

function removeElectron() {
  delete (window as unknown as { electronAPI?: unknown }).electronAPI
}

beforeEach(() => {
  removeElectron()
  useLayoutModeStore.setState({ floatingWindows: [] })
  vi.clearAllMocks()
})

afterEach(() => {
  removeElectron()
  useLayoutModeStore.setState({ floatingWindows: [] })
  vi.restoreAllMocks()
})

describe('WebWindowManager — 视口兜底与定位边界', () => {
  it('无 window 全局时视口兜底 1024x768（SSR/jsdom 分支）', () => {
    const realWindow = globalThis.window
    try {
      // 删除全局 window → resolvePosition 走 FALLBACK_VIEWPORT 分支
      delete (globalThis as { window?: unknown }).window
      const wm = new WebWindowManager()
      const id = wm.openPopout(page({ detachable: { popout: true } as never }))
      expect(id).not.toBe('')
      const win = useLayoutModeStore.getState().floatingWindows[0]
      expect(win.position).toEqual({
        x: Math.floor((1024 - 320) / 2),
        y: Math.floor((768 - 480) / 2),
      })
    } finally {
      ;(globalThis as unknown as { window: Window }).window = realWindow
    }
  })

  it('窗口大于视口时位置夹到 0（不为负）', () => {
    const wm = new WebWindowManager()
    wm.openPopout(page({ detachable: { popout: true } as never }), {
      size: { w: 4000, h: 3000 },
    })
    const win = useLayoutModeStore.getState().floatingWindows[0]
    expect(win.position.x).toBe(0)
    expect(win.position.y).toBe(0)
  })

  it('无任何浮窗时新窗 zIndex 为基数+1；已有浮窗后取最大 zIndex+1', () => {
    const wm = new WebWindowManager()
    wm.openPopout(page({ id: 'a', detachable: { popout: true } as never }))
    const first = useLayoutModeStore.getState().floatingWindows[0].zIndex

    // 手工把已有窗口 zIndex 抬高，验证新窗取 max+1 而非固定值
    useLayoutModeStore.getState().updateFloatingWindow(
      useLayoutModeStore.getState().floatingWindows[0].id,
      { zIndex: 5000 },
    )
    const secondId = wm.openPopout(page({ id: 'b', detachable: { popout: true } as never }))
    const wins = useLayoutModeStore.getState().floatingWindows
    const second = wins.find((w) => w.id === secondId)!.zIndex

    expect(first).toBeGreaterThan(0)
    expect(second).toBe(5001)
  })

  it('同一毫秒连续弹同一 page 时 windowId 仍唯一（计数器去碰撞）', () => {
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(1_700_000_000_000)
    try {
      const wm = new WebWindowManager()
      const id1 = wm.openPopout(page({ detachable: { popout: true } as never }))
      const id2 = wm.openPopout(page({ detachable: { popout: true } as never }))
      expect(id1).not.toBe(id2)
      expect(useLayoutModeStore.getState().floatingWindows).toHaveLength(2)
    } finally {
      nowSpy.mockRestore()
    }
  })

  it('title 缺省时回退 page.id；component 缺省同样回退 id', () => {
    const wm = new WebWindowManager()
    wm.openPopout(page({ id: 'no-title', title: undefined }))
    const win = useLayoutModeStore.getState().floatingWindows[0]
    expect(win.title).toBe('no-title')
    expect(win.component).toBe('no-title')
  })

  it('page.widget / props / datasourceUri 透传到浮窗实例', () => {
    const wm = new WebWindowManager()
    wm.openPopout(
      page({
        widget: 'chart',
        props: { series: [1, 2] },
        datasourceUri: '/api/data',
      }),
    )
    const win = useLayoutModeStore.getState().floatingWindows[0]
    expect(win.component).toBe('chart')
    expect(win.props).toEqual({ series: [1, 2], pageId: 'pg-1' })
    expect(win.dataSource).toBe('/api/data')
  })

  it('props 已含 pageId 时被规范化值覆盖（不出现两个来源）', () => {
    const wm = new WebWindowManager()
    wm.openPopout(page({ props: { pageId: '伪造', other: 1 } }))
    expect(useLayoutModeStore.getState().floatingWindows[0].props).toEqual({
      pageId: 'pg-1',
      other: 1,
    })
  })

  it.each([
    ['popout 未声明', undefined],
    ['popout 显式 true', { popout: true }],
  ])('%s 时允许弹出（宽松门控）', (_label, popout) => {
    const wm = new WebWindowManager()
    const id = wm.openPopout(page({ detachable: popout ? ({ popout } as never) : undefined }))
    expect(id).not.toBe('')
    expect(useLayoutModeStore.getState().floatingWindows).toHaveLength(1)
  })

  it('childWindow 降级也受 popout=false 门控（返回空串不加窗）', () => {
    const infoSpy = vi.spyOn(console, 'info').mockImplementation(() => {})
    const wm = new WebWindowManager()
    const id = wm.openChildWindow(page({ detachable: { popout: false } as never }))
    expect(id).toBe('')
    expect(useLayoutModeStore.getState().floatingWindows).toEqual([])
    infoSpy.mockRestore()
  })

  it('desktopWidget 降级也受 popout=false 门控', () => {
    const infoSpy = vi.spyOn(console, 'info').mockImplementation(() => {})
    const wm = new WebWindowManager()
    const id = wm.openDesktopWidget(page({ detachable: { popout: false } as never }))
    expect(id).toBe('')
    expect(useLayoutModeStore.getState().floatingWindows).toEqual([])
    infoSpy.mockRestore()
  })

  it('focus 未知 id 不抛错且不新增浮窗（幂等）', () => {
    const wm = new WebWindowManager()
    expect(() => wm.focus('ghost')).not.toThrow()
    expect(useLayoutModeStore.getState().floatingWindows).toEqual([])
  })

  it('close 后 focus 不影响其它窗口的 zIndex', () => {
    const wm = new WebWindowManager()
    wm.openPopout(page({ id: 'keep' }))
    const before = useLayoutModeStore.getState().floatingWindows[0].zIndex
    wm.focus('ghost-id')
    expect(useLayoutModeStore.getState().floatingWindows[0].zIndex).toBe(before)
  })
})

describe('isElectronWindowAvailable — 环境判定', () => {
  it('Web 环境（无 electronAPI）为 false', () => {
    expect(isElectronWindowAvailable()).toBe(false)
  })

  it('electronAPI 存在但无 window 命名空间时为 false', () => {
    ;(window as unknown as { electronAPI: unknown }).electronAPI = {}
    expect(isElectronWindowAvailable()).toBe(false)
  })

  it('electronAPI.window 存在时为 true', () => {
    stubElectron()
    expect(isElectronWindowAvailable()).toBe(true)
  })
})

describe('ElectronWindowManager — 原生窗口路径', () => {
  it('openPopout 缺省 opts 也走原生 open（frame=false 默认）', () => {
    const api = stubElectron()
    const wm = new ElectronWindowManager()
    const id = wm.openPopout(page({ title: '原生页' }))

    expect(id).toContain('-ewin-')
    expect(api.window.open).toHaveBeenCalledTimes(1)
    const req = api.window.open.mock.calls[0][0] as Record<string, unknown>
    expect(req).toMatchObject({ title: '原生页', frame: false, width: 320, height: 480 })
    expect(req.url).toContain('/#/p/pg-1')
  })

  it('openPopout 不写 layoutModeStore.floatingWindows（原生窗口独立）', () => {
    stubElectron()
    const wm = new ElectronWindowManager()
    wm.openPopout(page())
    expect(useLayoutModeStore.getState().floatingWindows).toEqual([])
  })

  it('openChildWindow 缺省 alwaysOnTop/skipTaskbar 为 false', () => {
    const api = stubElectron()
    new ElectronWindowManager().openChildWindow(page())
    const req = api.window.open.mock.calls[0][0] as Record<string, unknown>
    expect(req).toMatchObject({ alwaysOnTop: false, skipTaskbar: false })
  })

  it('openDesktopWidget 缺省强制置顶/隐藏任务栏', () => {
    const api = stubElectron()
    new ElectronWindowManager().openDesktopWidget(page())
    const req = api.window.open.mock.calls[0][0] as Record<string, unknown>
    expect(req).toMatchObject({ alwaysOnTop: true, skipTaskbar: true, frame: false })
  })

  it('detachable.alwaysOnTop=false 覆盖 desktopWidget 默认置顶', () => {
    const api = stubElectron()
    new ElectronWindowManager().openDesktopWidget(
      page({ detachable: { alwaysOnTop: false } as never }),
    )
    const req = api.window.open.mock.calls[0][0] as Record<string, unknown>
    expect(req.alwaysOnTop).toBe(false)
  })

  it('open 失败时打 error 日志但不抛出（fire-and-forget）', async () => {
    const openErr = new Error('main process refused')
    stubElectron({ open: vi.fn().mockRejectedValue(openErr) })
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

    const wm = new ElectronWindowManager()
    expect(() => wm.openChildWindow(page())).not.toThrow()
    // 等 catch 回调落定
    await Promise.resolve()
    await Promise.resolve()

    const joined = errSpy.mock.calls.map((c) => String(c[0])).join('\n')
    expect(joined).toContain('electronAPI.window.open failed')
    errSpy.mockRestore()
  })

  it.each(['openPopout', 'openChildWindow', 'openDesktopWidget'] as const)(
    '%s 的原生 open 失败同样只记日志（三个入口的 catch 分支）',
    async (method) => {
      stubElectron({ open: vi.fn().mockRejectedValue(new Error('refused')) })
      const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

      const wm = new ElectronWindowManager()
      expect(() => wm[method](page())).not.toThrow()
      await Promise.resolve()
      await Promise.resolve()

      expect(errSpy.mock.calls.map((c) => String(c[0])).join('\n')).toContain(
        'electronAPI.window.open failed',
      )
      errSpy.mockRestore()
    },
  )

  it('close 失败时打 error 日志但不抛出', async () => {
    stubElectron({ close: vi.fn().mockRejectedValue(new Error('no such window')) })
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

    const wm = new ElectronWindowManager()
    expect(() => wm.close('ewin-x')).not.toThrow()
    await Promise.resolve()
    await Promise.resolve()

    expect(errSpy.mock.calls.map((c) => String(c[0])).join('\n')).toContain(
      'electronAPI.window.close failed',
    )
    errSpy.mockRestore()
  })

  it('focus 失败时打 error 日志但不抛出', async () => {
    stubElectron({ focus: vi.fn().mockRejectedValue(new Error('no such window')) })
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

    const wm = new ElectronWindowManager()
    expect(() => wm.focus('ewin-x')).not.toThrow()
    await Promise.resolve()
    await Promise.resolve()

    expect(errSpy.mock.calls.map((c) => String(c[0])).join('\n')).toContain(
      'electronAPI.window.focus failed',
    )
    errSpy.mockRestore()
  })
})

describe('ElectronWindowManager — electronAPI 运行期消失的降级', () => {
  it('openPopout 降级到 Web popout 并打 info 提示', () => {
    const infoSpy = vi.spyOn(console, 'info').mockImplementation(() => {})
    const wm = new ElectronWindowManager() // 无 electronAPI → 降级
    const id = wm.openPopout(page())

    expect(id).toContain('-popout-')
    expect(useLayoutModeStore.getState().floatingWindows).toHaveLength(1)
    expect(infoSpy.mock.calls.map((c) => String(c[0])).join('\n')).toContain(
      'degrading openPopout to web popout',
    )
    infoSpy.mockRestore()
  })

  it('openChildWindow 降级到 Web popout 并打 info 提示', () => {
    const infoSpy = vi.spyOn(console, 'info').mockImplementation(() => {})
    const wm = new ElectronWindowManager()
    const id = wm.openChildWindow(page())

    expect(id).toContain('-popout-')
    expect(infoSpy.mock.calls.map((c) => String(c[0])).join('\n')).toContain(
      'degrading openChildWindow to web popout',
    )
    infoSpy.mockRestore()
  })

  it('openDesktopWidget 降级到 Web popout 并打 info 提示', () => {
    const infoSpy = vi.spyOn(console, 'info').mockImplementation(() => {})
    const wm = new ElectronWindowManager()
    const id = wm.openDesktopWidget(page())

    expect(id).toContain('-popout-')
    expect(infoSpy.mock.calls.map((c) => String(c[0])).join('\n')).toContain(
      'degrading openDesktopWidget to web popout',
    )
    infoSpy.mockRestore()
  })

  it('close 降级到 Web 实现（移除 store 中浮窗）', () => {
    // 先用 Web 路径放一个窗（经降级的 Electron 管理器）
    const wm = new ElectronWindowManager()
    const id = wm.openPopout(page({ id: 'to-close' }))
    expect(useLayoutModeStore.getState().floatingWindows).toHaveLength(1)

    wm.close(id)
    expect(useLayoutModeStore.getState().floatingWindows).toEqual([])
  })

  it('focus 降级到 Web 实现（提升 store 中浮窗 zIndex）', () => {
    const wm = new ElectronWindowManager()
    const id = wm.openPopout(page({ id: 'to-focus' }))
    const before = useLayoutModeStore.getState().floatingWindows[0].zIndex

    wm.focus(id)

    const after = useLayoutModeStore.getState().floatingWindows[0].zIndex
    expect(after).toBeGreaterThan(before)
  })

  it('实例化后 electronAPI 被移除：下一次调用即降级（不缓存可用性）', () => {
    const api = stubElectron()
    const wm = new ElectronWindowManager()
    wm.openPopout(page({ id: 'native-1' }))
    expect(api.window.open).toHaveBeenCalledTimes(1)
    expect(useLayoutModeStore.getState().floatingWindows).toEqual([])

    // 宿主进程消失（极端场景）
    removeElectron()
    const infoSpy = vi.spyOn(console, 'info').mockImplementation(() => {})
    const id = wm.openPopout(page({ id: 'fallback-1' }))

    expect(id).toContain('-popout-')
    expect(useLayoutModeStore.getState().floatingWindows).toHaveLength(1)
    infoSpy.mockRestore()
  })
})

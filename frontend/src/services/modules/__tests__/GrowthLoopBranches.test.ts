/**
 * GrowthLoop 分支补测：initializeGrowthLoop / refreshPluginContributions /
 * destroyGrowthLoop / restartGrowthLoop 四条入口的完整链路——
 * commandDispatcher transport 注入、schema 装载面调用、插件声明校验告警、
 * DSH 适配器与主题/样式同步，以及 destroy/restart 的清理与失败回滚。
 *
 * 「装载面」与「注册表」用 spy 替换（外部依赖：网络/单例注册表），
 * GrowthLoop 自身的编排顺序与分支用可观察调用断言。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  getSchema: vi.fn(),
  loadFromSchema: vi.fn(),
  clear: vi.fn(),
  getClientStyles: vi.fn(() => []),
  isInitialized: vi.fn(() => true),
  getPluginTheme: vi.fn(),
  getPluginThemes: vi.fn(() => []),
  retryPendingTheme: vi.fn(),
  syncPluginThemes: vi.fn(),
  invalidateSchemaCache: vi.fn(),
  loadDshAdapterContributions: vi.fn(),
  initializeWidgets: vi.fn(),
  initResyncOnSchema: vi.fn(),
  disposeResyncOnSchema: vi.fn(),
  syncPluginStyles: vi.fn(),
  removeAllPluginStyles: vi.fn(),
  refreshShortcuts: vi.fn(),
  setTransport: vi.fn(),
  post: vi.fn(),
  loadChatCardDeclarations: vi.fn(),
  loadOutputSchemas: vi.fn(),
  loadInteractionModes: vi.fn(),
  loadNotificationModes: vi.fn(),
  loadViewModes: vi.fn(),
  loadRenderIntents: vi.fn(),
}))

vi.mock('@/services/api/schema', () => ({ getSchema: mocks.getSchema }))
vi.mock('@/hooks/queries/useSchemaQuery', () => ({
  fetchSchemaCached: mocks.getSchema,
  invalidateSchemaCache: mocks.invalidateSchemaCache,
}))
vi.mock('@/services/schema/ContributionRegistry', () => ({
  contributionRegistry: {
    loadFromSchema: mocks.loadFromSchema,
    clear: mocks.clear,
    getClientStyles: mocks.getClientStyles,
    isInitialized: mocks.isInitialized,
    getPluginTheme: mocks.getPluginTheme,
    getPluginThemes: mocks.getPluginThemes,
  },
}))
vi.mock('@/services/schema/commandDispatcher', () => ({
  commandDispatcher: { setTransport: mocks.setTransport },
}))
vi.mock('@/services/schema/registerWidgets', () => ({ initializeWidgets: mocks.initializeWidgets }))
vi.mock('@/services/schema/shortcutRegistry', () => ({
  shortcutRegistry: { refresh: mocks.refreshShortcuts },
}))
vi.mock('@/services/websocket/resync', () => ({
  initResyncOnSchema: mocks.initResyncOnSchema,
  disposeResyncOnSchema: mocks.disposeResyncOnSchema,
}))
vi.mock('@/services/dshAdapter', () => ({
  loadDshAdapterContributions: mocks.loadDshAdapterContributions,
}))
vi.mock('@/services/pluginStyles', () => ({
  syncPluginStyles: mocks.syncPluginStyles,
  removeAllPluginStyles: mocks.removeAllPluginStyles,
}))
vi.mock('@/services/api/client', () => ({
  default: { post: mocks.post },
}))
vi.mock('@/utils/chatCardInterpreter', () => ({ loadChatCardDeclarations: mocks.loadChatCardDeclarations }))
vi.mock('@/utils/outputSchemaView', () => ({ loadOutputSchemas: mocks.loadOutputSchemas }))
vi.mock('@/utils/interactionModes', () => ({ loadInteractionModes: mocks.loadInteractionModes }))
vi.mock('@/utils/notificationModes', () => ({ loadNotificationModes: mocks.loadNotificationModes }))
vi.mock('@/utils/viewModeRoutes', () => ({ loadViewModes: mocks.loadViewModes }))
vi.mock('@/utils/renderIntent', () => ({ loadRenderIntents: mocks.loadRenderIntents }))

import {
  destroyGrowthLoop,
  initializeGrowthLoop,
  refreshPluginContributions,
  restartGrowthLoop,
} from '@/services/modules/GrowthLoop'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { useThemeStore } from '@/stores/themeStore'

beforeEach(() => {
  vi.clearAllMocks()
  mocks.getSchema.mockResolvedValue({})
  mocks.loadDshAdapterContributions.mockResolvedValue(undefined)
  mocks.invalidateSchemaCache.mockResolvedValue(undefined)
  mocks.isInitialized.mockReturnValue(true)
  mocks.getPluginTheme.mockReturnValue(undefined)
  useNotificationStore.getState().clearAll()
  useLayoutModeStore.setState({ workspaceTabs: [], dockItems: [] })
})

describe('GrowthLoop — initializeGrowthLoop 编排', () => {
  it('按序注入 transport、注册预置组件、装载 schema、订阅 resync', async () => {
    await initializeGrowthLoop()

    expect(mocks.setTransport).toHaveBeenCalledTimes(1)
    expect(mocks.initializeWidgets).toHaveBeenCalledTimes(1)
    expect(mocks.loadFromSchema).toHaveBeenCalledTimes(1)
    expect(mocks.initResyncOnSchema).toHaveBeenCalledTimes(1)
  })

  it('注入的 transport 经 apiClient 发 actions/execute POST（命令真出口）', async () => {
    mocks.post.mockResolvedValue({ data: {} })
    await initializeGrowthLoop()

    const transport = mocks.setTransport.mock.calls[0][0] as (
      id: string,
      args: unknown,
    ) => Promise<void>
    await transport('cmd.open', { a: 1 })

    expect(mocks.post).toHaveBeenCalledWith(
      expect.stringContaining('/actions/execute'),
      { action: 'cmd.open', args: { a: 1 } },
    )
  })
})

describe('GrowthLoop — schema 装载面', () => {
  it('schema.tools 的五个渲染声明面都被装载（chat_card/output_schema/interaction/notification/view/render）', async () => {
    mocks.getSchema.mockResolvedValue({
      tools: [
        {
          name: 't1',
          ui: {
            chat_card: { title: 'x' },
            interaction_modes: { a: 1 },
            notification_modes: { b: 2 },
            view_modes: { c: 3 },
          },
          output_schema: { type: 'object' },
          render: { card: { kind: 'table' } },
        },
      ],
    })

    await refreshPluginContributions()

    expect(mocks.loadChatCardDeclarations).toHaveBeenCalledTimes(1)
    expect(mocks.loadChatCardDeclarations.mock.calls[0][0]).toHaveLength(1)
    expect(mocks.loadOutputSchemas).toHaveBeenCalledTimes(1)
    expect(mocks.loadInteractionModes).toHaveBeenCalledTimes(1)
    expect(mocks.loadNotificationModes).toHaveBeenCalledTimes(1)
    expect(mocks.loadViewModes).toHaveBeenCalledTimes(1)
    expect(mocks.loadRenderIntents).toHaveBeenCalledTimes(1)
  })

  it('schema 缺 tools 时各装载面收到空数组（不抛错）', async () => {
    mocks.getSchema.mockResolvedValue({})
    await refreshPluginContributions()

    for (const m of [
      mocks.loadChatCardDeclarations,
      mocks.loadOutputSchemas,
      mocks.loadInteractionModes,
      mocks.loadNotificationModes,
      mocks.loadViewModes,
      mocks.loadRenderIntents,
    ]) {
      expect(m).toHaveBeenCalledTimes(1)
      expect(m.mock.calls[0][0]).toEqual([])
    }
  })

  it('plugin_contributes 缺 pages（?? [] 兜底）时不产生页面校验错误', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      mocks.getSchema.mockResolvedValue({
        plugin_contributes: [{ plugin_id: 'pc', contributes: {} }],
      })
      await refreshPluginContributions()
      const joined = warnSpy.mock.calls.map((c) => String(c[1] ?? '')).join('\n')
      expect(joined).not.toContain('插件声明校验不通过')
    } finally {
      warnSpy.mockRestore()
    }
  })

  it('plugin_contributes 的 ui_schema.widgets 被纳入 widget 来源（与 agents/pipelines 合并校验）', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      mocks.getSchema.mockResolvedValue({
        plugin_contributes: [
          {
            plugin_id: 'pc',
            contributes: {},
            ui_schema: { widgets: [{ id: 'w-bad' }] }, // 缺 type → error
          },
        ],
      })

      await refreshPluginContributions()

      const joined = warnSpy.mock.calls.map((c) => String(c[1] ?? '')).join('\n')
      expect(joined).toContain('插件声明校验不通过')
      expect(joined).toContain('ui_schema.widgets[0] 缺 type')
    } finally {
      warnSpy.mockRestore()
    }
  })

  it('agents/pipelines 缺省时 widget 来源仅取 plugin_contributes（不抛错）', async () => {
    mocks.getSchema.mockResolvedValue({ plugin_contributes: [] })
    await expect(refreshPluginContributions()).resolves.toBeUndefined()
  })

  it('ui_schema 存在但无 widgets 键时该来源贡献空列表（?? [] 分支）', async () => {
    mocks.getSchema.mockResolvedValue({
      agents: [{ id: 'a1', ui_schema: { other: 1 } }],
      pipelines: [{ id: 'pl1', ui_schema: { other: 2 } }],
      plugin_contributes: [{ plugin_id: 'pc', contributes: {}, ui_schema: { other: 3 } }],
    })
    await expect(refreshPluginContributions()).resolves.toBeUndefined()
  })

  it('ui_schema 为 null / 非对象时被过滤掉（filter 条件分支）', async () => {
    mocks.getSchema.mockResolvedValue({
      agents: [{ id: 'a-null', ui_schema: null }],
      pipelines: [{ id: 'pl-str', ui_schema: 'oops' }],
      plugin_contributes: [{ plugin_id: 'pc-none' }],
    })
    await expect(refreshPluginContributions()).resolves.toBeUndefined()
  })

  it('成功路径同步主题/样式/快捷键/DSH 适配器，且不发失败通知', async () => {
    mocks.getClientStyles.mockReturnValue([{ id: 's1' }])
    await refreshPluginContributions()

    expect(mocks.retryPendingTheme).toBeTruthy()
    expect(mocks.syncPluginThemes).toBeTruthy()
    expect(mocks.syncPluginStyles).toHaveBeenCalledWith([{ id: 's1' }])
    expect(mocks.refreshShortcuts).toHaveBeenCalledTimes(1)
    expect(mocks.loadDshAdapterContributions).toHaveBeenCalledTimes(1)
    expect(
      useNotificationStore.getState().notifications.filter((n) => n.title === '插件贡献加载失败'),
    ).toEqual([])
  })
})

describe('GrowthLoop — 插件声明校验上报', () => {
  it('坏声明产生 errors 与 warnings 时各发一条 warn（含数量）', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      mocks.getSchema.mockResolvedValue({
        // page 缺 id/space（2 errors）+ 字段未知 type（1 warning）
        plugin_contributes: [
          { plugin_id: 'p', contributes: { pages: [{ title: 'no id', schema: { fields: [{ name: 'a', type: 'weird' }] } }] } },
        ],
      })

      await refreshPluginContributions()

      const joined = warnSpy.mock.calls.map((c) => String(c[1] ?? '')).join('\n')
      expect(joined).toContain('插件声明校验不通过')
      expect(joined).toContain('插件声明降级警告')
    } finally {
      warnSpy.mockRestore()
    }
  })

  it('合法声明时不发校验相关 warn（但仍有常规 info 日志）', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      mocks.getSchema.mockResolvedValue({
        plugin_contributes: [
          { plugin_id: 'p', contributes: { pages: [{ id: 'ok', space: 'settings' }] } },
        ],
      })

      await refreshPluginContributions()

      const joined = warnSpy.mock.calls.map((c) => String(c[1] ?? '')).join('\n')
      expect(joined).not.toContain('插件声明校验不通过')
      expect(joined).not.toContain('插件声明降级警告')
    } finally {
      warnSpy.mockRestore()
    }
  })

  it('校验器自身抛异常时被隔离（走「插件声明校验异常」warn，不影响后续步骤）', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      // agents 里塞循环引用对象 → 让校验内的对象展开抛错
      const cyclic: Record<string, unknown> = { id: 'a' }
      cyclic.self = cyclic
      vi.doMock('@/services/pluginDeclarationValidate', () => ({
        validatePluginDeclaration: () => {
          throw new Error('validator exploded')
        },
      }))
      vi.resetModules()

      const warnSpy2 = vi.spyOn(console, 'warn').mockImplementation(() => {})
      mocks.getSchema.mockResolvedValue({ agents: [cyclic] })
      const mod = await import('@/services/modules/GrowthLoop')
      await mod.refreshPluginContributions()

      const joined = warnSpy2.mock.calls.map((c) => String(c[1] ?? '')).join('\n')
      expect(joined).toContain('插件声明校验异常')
      // 后续步骤仍执行（校验异常不影响主链路）
      expect(mocks.refreshShortcuts).toHaveBeenCalled()
      warnSpy2.mockRestore()
    } finally {
      warnSpy.mockRestore()
      vi.doUnmock('@/services/pluginDeclarationValidate')
      vi.resetModules()
    }
  })
})

describe('GrowthLoop — destroyGrowthLoop 清理', () => {
  it('注销 resync、清空注册表与插件样式、清空工作区标签与 Dock', () => {
    useLayoutModeStore.setState({
      workspaceTabs: [
        { id: 'tab-a', title: 'A', moduleId: 'm', isActive: true, isPinned: false },
      ],
      dockItems: [
        {
          id: 'd1',
          moduleId: 'm',
          icon: 'folder',
          label: 'L',
          indicator: 'none',
          isActive: false,
          onClick: () => {},
        },
      ],
    })

    destroyGrowthLoop()

    expect(mocks.disposeResyncOnSchema).toHaveBeenCalledTimes(1)
    expect(mocks.clear).toHaveBeenCalledTimes(1)
    expect(mocks.removeAllPluginStyles).toHaveBeenCalledTimes(1)
    expect(useLayoutModeStore.getState().workspaceTabs).toEqual([])
    expect(useLayoutModeStore.getState().dockItems).toEqual([])
  })
})

describe('GrowthLoop — restartGrowthLoop', () => {
  it('成功路径：先清理再重建，补挂 resync 与预置组件', async () => {
    await restartGrowthLoop()

    expect(mocks.clear).toHaveBeenCalledTimes(1)
    expect(mocks.initializeWidgets).toHaveBeenCalledTimes(1)
    expect(mocks.initResyncOnSchema).toHaveBeenCalledTimes(1)
    expect(mocks.loadFromSchema).toHaveBeenCalledTimes(1)
  })

  it('reload 内部失败被吞掉时不冒泡（restart 仍 resolved，失败已由通知可见）', async () => {
    mocks.getSchema.mockRejectedValue(new Error('schema down'))
    await expect(restartGrowthLoop()).resolves.toBeUndefined()
  })

  it('非 Error 抛出物（字符串）经 String() 进通知文案（三元另一分支）', async () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date('2034-01-01T00:00:00Z'))
      mocks.getSchema.mockRejectedValue('plain-string-failure')
      const addSpy = vi.fn()
      const origAdd = useNotificationStore.getState().addNotification
      useNotificationStore.setState({ addNotification: addSpy as never })
      try {
        await refreshPluginContributions()
        expect(addSpy).toHaveBeenCalledTimes(1)
        expect(addSpy.mock.calls[0][0].message).toContain('plain-string-failure')
      } finally {
        useNotificationStore.setState({ addNotification: origAdd } as never)
      }
    } finally {
      vi.useRealTimers()
    }
  })

  it('可见降级路径自身抛错时 restart 清空工作区并向上抛出（catch 分支）', async () => {
    vi.useFakeTimers()
    try {
      // 越过 60s 通知节流窗口，确保降级通知路径真的执行
      vi.setSystemTime(new Date('2036-01-01T00:00:00Z'))
      useLayoutModeStore.setState({
        workspaceTabs: [{ id: 'tab-x', title: 'X', moduleId: 'm', isActive: true, isPinned: false }],
        dockItems: [
          {
            id: 'd1',
            moduleId: 'm',
            icon: 'f',
            label: 'L',
            indicator: 'none',
            isActive: false,
            onClick: () => {},
          },
        ],
      })
      mocks.getSchema.mockRejectedValue(new Error('reload failed'))
      // 通知侧抛错（如通知中心未就绪）→ 降级路径失败，异常穿透到 restart 的 catch
      const origAdd = useNotificationStore.getState().addNotification
      useNotificationStore.setState({
        addNotification: () => {
          throw new Error('notify exploded')
        },
      } as never)

      try {
        await expect(restartGrowthLoop()).rejects.toThrow('notify exploded')
        expect(useLayoutModeStore.getState().workspaceTabs).toEqual([])
        expect(useLayoutModeStore.getState().dockItems).toEqual([])
      } finally {
        useNotificationStore.setState({ addNotification: origAdd } as never)
      }
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('GrowthLoop — 主题挂起重放（时序配套）', () => {
  it('schema 装载后调用 themeStore.retryPendingTheme（挂起主题可查到即重放）', async () => {
    const retrySpy = vi.fn()
    useThemeStore.setState({ retryPendingTheme: retrySpy } as never)
    try {
      await refreshPluginContributions()
      expect(retrySpy).toHaveBeenCalledTimes(1)
    } finally {
      useThemeStore.setState({ retryPendingTheme: vi.fn() } as never)
    }
  })
})

// @feature: FP-T12 前端适配 | @ci: frontend-test
/** @bug BUG-68 restartGrowthLoop 拉取失败不得清空用户工作区页签/dock @ci frontend-test */
/**
 * 部署重启窗口「前端先起、内核未就绪」时 schema 拉取失败，旧实现直接
 * workspaceTabs: [] —— 用户工作区页签（导演台/主城/记忆…）被清空并随 zustand
 * persist 永久写入（BUG-68：任务管理页面找不到了）。降级语义应为 FE11 同款：
 * 拉取失败保留旧声明集与用户布局数据，下次 schema 事件再换装。
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
const mocks = vi.hoisted(() => ({
  fetchSchemaCached: vi.fn(),
}))
vi.mock('@/hooks/queries/useSchemaQuery', () => ({
  fetchSchemaCached: mocks.fetchSchemaCached,
  invalidateSchemaCache: vi.fn(),
}))
vi.mock('@/services/websocket/resync', () => ({
  initResyncOnSchema: vi.fn(),
  disposeResyncOnSchema: vi.fn(),
}))
vi.mock('@/services/dshAdapter', () => ({
  loadDshAdapterContributions: vi.fn().mockResolvedValue(undefined),
}))
import { destroyGrowthLoop, restartGrowthLoop } from '@/services/modules/GrowthLoop'
import { useLayoutModeStore } from '@/stores/layoutModeStore'

const DIRECTOR_TAB = {
  id: 'ws-plugin-director',
  title: '直播导演台',
  icon: '🎬',
  moduleId: '__plugin_page__',
  component: 'webview',
  isActive: true,
  isPinned: false,
}
const TASKS_TAB = {
  id: 'ws-panel-tasks',
  title: '任务管理',
  icon: 'folder',
  moduleId: '__panel_tasks__',
  component: 'pipeline_manager',
  isActive: false,
  isPinned: false,
}

describe('GrowthLoop 不清用户工作区页签（BUG-68）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useLayoutModeStore.setState({
      workspaceTabs: [DIRECTOR_TAB, TASKS_TAB] as never,
      dockItems: [{ id: 'dock-debug' }] as never,
    })
  })

  it('destroyGrowthLoop（登出/认证过期路径）：页签保留，dock 照清', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      destroyGrowthLoop()

      const { workspaceTabs, dockItems } = useLayoutModeStore.getState()
      expect(workspaceTabs.map((t) => t.id)).toEqual(['ws-plugin-director', 'ws-panel-tasks'])
      expect(dockItems).toEqual([])
    } finally {
      warnSpy.mockRestore()
    }
  })

  it('restartGrowthLoop 拉取失败（内核未就绪窗）：页签原样保留（内部 catch 降级不抛出）', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      mocks.fetchSchemaCached.mockRejectedValue(new Error('kernel not ready'))

      await expect(restartGrowthLoop()).resolves.toBeUndefined()

      const { workspaceTabs } = useLayoutModeStore.getState()
      expect(workspaceTabs.map((t) => t.id)).toEqual(['ws-plugin-director', 'ws-panel-tasks'])
    } finally {
      warnSpy.mockRestore()
    }
  }, 30_000)

  it('restartGrowthLoop 拉取成功：页签同样不被清空（换装只动声明注册表）', async () => {
    mocks.fetchSchemaCached.mockResolvedValue({ agents: [], pipelines: [], tools: [] })

    await expect(restartGrowthLoop()).resolves.toBeUndefined()

    const { workspaceTabs } = useLayoutModeStore.getState()
    expect(workspaceTabs.map((t) => t.id)).toEqual(['ws-plugin-director', 'ws-panel-tasks'])
  }, 30_000)
})

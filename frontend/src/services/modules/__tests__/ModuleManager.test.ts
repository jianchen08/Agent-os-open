/**
 * ModuleManager 单元测试
 *
 * 验证模块同步与工作区 tab 创建逻辑，重点覆盖
 * BUG-FIX-fix_20260621_workspace_empty_no_retry（工作区为空 — 模块激活后自动出现）：
 * - 正常流程：拉取 schema 后创建 workspace tab
 * - 初始化失败重试：网络失败时按指数退避重试，重试成功后 tab 创建
 * - 兜底同步：拉取失败但注册表已有模块时，ensureWorkspaceSynced 仍创建 tab
 * - WS 重连同步：syncOnReconnect 在 tab 缺失时重新拉取
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'

// ── Mock 外部依赖 ──
/** 模拟后端 /api/v1/modules/ui 返回的 task-manager schema */
const TASK_MANAGER_SCHEMA = {
  identity: {
    id: 'task-manager',
    name: '任务管理',
    version: '1.0.0',
    category: 'builtin',
    description: '任务树管理模块',
    icon: '📋',
  },
  actions: [
    {
      id: 'get_task_tree',
      name: '获取任务树',
      type: 'query',
      api: '/api/v1/projects/tree',
      label: '任务树',
    },
  ],
  rendering: {
    chat: [],
    spaces: [
      {
        space: 'workspace',
        widget: 'tree',
        props: { title: '任务管理', showStatus: true },
        dataSource: 'task-manager://tree',
      },
    ],
    dock: { icon: '📋', label: '任务', indicator: 'dot' },
  },
  clients: {
    requiredSpaces: [],
    requiredWidgets: [],
    minClientVersion: '1.0.0',
  },
}

const mockGetModuleUISchemas = vi.fn()
vi.mock('@/services/api/modules', () => ({
  getModuleUISchemas: () => mockGetModuleUISchemas(),
  registerClientCapabilities: vi.fn().mockResolvedValue({}),
}))

vi.mock('@/utils/logger', () => ({
  loggers: {
    websocket: {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    },
  },
}))

vi.mock('@/constants/storage', () => ({
  STORAGE_KEYS: { ACCESS_TOKEN: 'access_token' },
}))

describe('ModuleManager 工作区 tab 同步', () => {
  let layoutStoreState: {
    workspaceTabs: any[]
    dockItems: any[]
  }

  beforeEach(() => {
    vi.resetModules()
    mockGetModuleUISchemas.mockReset()
    layoutStoreState = { workspaceTabs: [], dockItems: [] }
    localStorage.setItem('access_token', 'fake-token')

    vi.doMock('@/stores/layoutModeStore', () => ({
      useLayoutModeStore: {
        getState: () => ({
          ...layoutStoreState,
          setDockItems: (items: any[]) => {
            layoutStoreState.dockItems = items
          },
        }),
        setState: (partial: any) => {
          if (typeof partial === 'function') {
            Object.assign(layoutStoreState, partial(layoutStoreState))
          } else {
            Object.assign(layoutStoreState, partial)
          }
        },
      },
    }))
  })

  it('正常流程：拉取 schema 后应创建 workspace tab', async () => {
    mockGetModuleUISchemas.mockResolvedValue({
      items: [TASK_MANAGER_SCHEMA],
      total: 1,
    })

    const { moduleManager } = await import('../ModuleManager')
    ;(moduleManager as any).initialized = false

    await moduleManager.fetchAndRegister()

    expect(layoutStoreState.workspaceTabs.length).toBe(1)
    expect(layoutStoreState.workspaceTabs[0].moduleId).toBe('task-manager')
  })

  it('初始化失败重试：前两次失败、第三次成功后应创建 tab', async () => {
    mockGetModuleUISchemas
      .mockRejectedValueOnce(new Error('network error 1'))
      .mockRejectedValueOnce(new Error('network error 2'))
      .mockResolvedValueOnce({ items: [TASK_MANAGER_SCHEMA], total: 1 })

    const { moduleManager } = await import('../ModuleManager')
    ;(moduleManager as any).initialized = false

    await moduleManager.initialize()

    expect(mockGetModuleUISchemas).toHaveBeenCalledTimes(3)
    expect(layoutStoreState.workspaceTabs.length).toBe(1)
    expect((moduleManager as any).initialized).toBe(true)
  })

  it('兜底同步：初始化全部失败但注册表已有模块时，仍应创建 tab', async () => {
    // 先让注册表有模块（模拟前次成功留下的缓存）
    mockGetModuleUISchemas.mockResolvedValue({ items: [TASK_MANAGER_SCHEMA], total: 1 })
    const { moduleManager, schemaRegistry } = await import('../ModuleManager')
    ;(moduleManager as any).initialized = false
    // 先成功注册一次到 registry
    await moduleManager.fetchAndRegister()
    expect(layoutStoreState.workspaceTabs.length).toBe(1)

    // 模拟后续全部拉取失败，且 tab 被清空（如 destroyGrowthLoop 后）
    mockGetModuleUISchemas.mockRejectedValue(new Error('persistent failure'))
    layoutStoreState.workspaceTabs = []

    await moduleManager.initialize()

    // 拉取失败但兜底同步应从 registry 重建 tab
    expect(layoutStoreState.workspaceTabs.length).toBe(1)
    expect(layoutStoreState.workspaceTabs[0].moduleId).toBe('task-manager')
  })

  it('WS 重连同步：syncOnReconnect 在 tab 缺失时重新拉取并创建 tab', async () => {
    mockGetModuleUISchemas.mockResolvedValue({
      items: [TASK_MANAGER_SCHEMA],
      total: 1,
    })

    const { moduleManager } = await import('../ModuleManager')
    // 初始无 tab
    expect(layoutStoreState.workspaceTabs.length).toBe(0)

    await moduleManager.syncOnReconnect()

    expect(mockGetModuleUISchemas).toHaveBeenCalledTimes(1)
    expect(layoutStoreState.workspaceTabs.length).toBe(1)
  })

  it('WS 重连同步：tab 已存在时不再重复拉取', async () => {
    mockGetModuleUISchemas.mockResolvedValue({
      items: [TASK_MANAGER_SCHEMA],
      total: 1,
    })

    const { moduleManager } = await import('../ModuleManager')
    // 预置一个 workspace tab
    layoutStoreState.workspaceTabs = [
      { id: 'ws-task-manager-tree', moduleId: 'task-manager', isActive: true },
    ]

    await moduleManager.syncOnReconnect()

    expect(mockGetModuleUISchemas).not.toHaveBeenCalled()
  })
})

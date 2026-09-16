/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * uiStore 分支补测：初始化读取分支（含读取异常回退）+ 全部 UI 切换动作
 * 及其 localStorage write-through 副作用。
 *
 * 初始化分支用「预置 localStorage → vi.resetModules() → 动态 import」验证；
 * 异常分支用 spy 让 getter 抛错（模拟存储被禁用）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ApprovalRequest } from '@/types/models'

/** 复位模块缓存，让 store 重新执行初始化读取逻辑 */
async function loadFreshUIStore() {
  vi.resetModules()
  return (await import('../uiStore')).useUIStore
}

beforeEach(() => {
  localStorage.clear()
})

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('uiStore — 初始化读取 localStorage', () => {
  it('无持久化数据时全部走默认值（侧边栏/面板展开、比例 null）', async () => {
    const store = await loadFreshUIStore()
    const s = store.getState()
    expect(s.sidebarCollapsed).toBe(false)
    expect(s.taskPanelCollapsed).toBe(false)
    expect(s.workspaceCollapsed).toBe(false)
    expect(s.workspacePanelRatio).toBeNull()
    expect(s.sidebarRatio).toBeNull()
    expect(s.messageSearchQuery).toBe('')
    expect(s.messageJump).toBeNull()
  })

  it('持久化的折叠状态与比例被读出（含 true 与合法比例）', async () => {
    localStorage.setItem('sidebar_collapsed', 'true')
    localStorage.setItem('workspace_collapsed', 'true')
    localStorage.setItem('workspace_panel_ratio', '0.4')
    localStorage.setItem('sidebar_ratio', '0.25')
    const store = await loadFreshUIStore()
    const s = store.getState()
    expect(s.sidebarCollapsed).toBe(true)
    expect(s.workspaceCollapsed).toBe(true)
    expect(s.workspacePanelRatio).toBeCloseTo(0.4)
    expect(s.sidebarRatio).toBeCloseTo(0.25)
  })

  it('越界比例（>=1 或 <=0）落回 null，不把非法值读进状态', async () => {
    localStorage.setItem('workspace_panel_ratio', '1.5')
    localStorage.setItem('sidebar_ratio', '-0.2')
    const store = await loadFreshUIStore()
    expect(store.getState().workspacePanelRatio).toBeNull()
    expect(store.getState().sidebarRatio).toBeNull()
  })

  it('getter 抛错（存储不可用）时回退默认值而非崩溃', async () => {
    vi.resetModules()
    const storageMod = await import('@/utils/storage')
    vi.spyOn(storageMod.uiStorage, 'getSidebarCollapsed').mockImplementation(() => {
      throw new Error('storage disabled')
    })
    vi.spyOn(storageMod.uiStorage, 'getTaskPanelCollapsed').mockImplementation(() => {
      throw new Error('storage disabled')
    })
    vi.spyOn(storageMod.uiStorage, 'getWorkspaceCollapsed').mockImplementation(() => {
      throw new Error('storage disabled')
    })
    vi.spyOn(storageMod.uiStorage, 'getWorkspacePanelRatio').mockImplementation(() => {
      throw new Error('storage disabled')
    })
    vi.spyOn(storageMod.uiStorage, 'getSidebarRatio').mockImplementation(() => {
      throw new Error('storage disabled')
    })
    const store = (await import('../uiStore')).useUIStore
    const s = store.getState()
    expect(s.sidebarCollapsed).toBe(false)
    expect(s.taskPanelCollapsed).toBe(false)
    expect(s.workspaceCollapsed).toBe(false)
    expect(s.workspacePanelRatio).toBeNull()
    expect(s.sidebarRatio).toBeNull()
  })
})

describe('uiStore — 折叠切换动作', () => {
  it('toggleSidebar 翻转状态并 write-through 到 localStorage', async () => {
    const store = await loadFreshUIStore()
    store.getState().toggleSidebar()
    expect(store.getState().sidebarCollapsed).toBe(true)
    expect(localStorage.getItem('sidebar_collapsed')).toBe('true')
    store.getState().toggleSidebar()
    expect(store.getState().sidebarCollapsed).toBe(false)
    expect(localStorage.getItem('sidebar_collapsed')).toBe('false')
  })

  it('setSidebarCollapsed 直接写入指定值（幂等：同值两次结果一致）', async () => {
    const store = await loadFreshUIStore()
    store.getState().setSidebarCollapsed(true)
    store.getState().setSidebarCollapsed(true)
    expect(store.getState().sidebarCollapsed).toBe(true)
    expect(localStorage.getItem('sidebar_collapsed')).toBe('true')
  })

  it('toggleTaskPanel 翻转并持久化', async () => {
    const store = await loadFreshUIStore()
    store.getState().toggleTaskPanel()
    expect(store.getState().taskPanelCollapsed).toBe(true)
    expect(localStorage.getItem('task_panel_collapsed')).toBe('true')
  })

  it('setTaskPanelCollapsed(false) 可从折叠态展开', async () => {
    const store = await loadFreshUIStore()
    store.getState().setTaskPanelCollapsed(true)
    store.getState().setTaskPanelCollapsed(false)
    expect(store.getState().taskPanelCollapsed).toBe(false)
    expect(localStorage.getItem('task_panel_collapsed')).toBe('false')
  })

  it('toggleWorkspace 翻转并持久化', async () => {
    const store = await loadFreshUIStore()
    store.getState().toggleWorkspace()
    expect(store.getState().workspaceCollapsed).toBe(true)
    expect(localStorage.getItem('workspace_collapsed')).toBe('true')
  })

  it('setWorkspaceCollapsed 指定值写入', async () => {
    const store = await loadFreshUIStore()
    store.getState().setWorkspaceCollapsed(true)
    expect(store.getState().workspaceCollapsed).toBe(true)
    expect(localStorage.getItem('workspace_collapsed')).toBe('true')
  })
})

describe('uiStore — 面板比例', () => {
  it('setWorkspacePanelRatio 非 null 时写入比例并持久化', async () => {
    const store = await loadFreshUIStore()
    store.getState().setWorkspacePanelRatio(0.6)
    expect(store.getState().workspacePanelRatio).toBeCloseTo(0.6)
    expect(JSON.parse(localStorage.getItem('workspace_panel_ratio')!)).toBeCloseTo(0.6)
  })

  it('setWorkspacePanelRatio(null) 清空记录（回退默认比例）且状态归 null', async () => {
    const store = await loadFreshUIStore()
    store.getState().setWorkspacePanelRatio(0.6)
    store.getState().setWorkspacePanelRatio(null)
    expect(store.getState().workspacePanelRatio).toBeNull()
    // undefined 落盘语义 = 清除 key（storage.setItem 对 undefined 走 removeItem）
    expect(localStorage.getItem('workspace_panel_ratio')).toBeNull()
  })

  it('setSidebarRatio 非 null 写入、null 清空（两条分支）', async () => {
    const store = await loadFreshUIStore()
    store.getState().setSidebarRatio(0.3)
    expect(store.getState().sidebarRatio).toBeCloseTo(0.3)
    expect(JSON.parse(localStorage.getItem('sidebar_ratio')!)).toBeCloseTo(0.3)

    store.getState().setSidebarRatio(null)
    expect(store.getState().sidebarRatio).toBeNull()
    expect(localStorage.getItem('sidebar_ratio')).toBeNull()
  })
})

describe('uiStore — 审批对话框', () => {
  const approval = {
    id: 'ap-1',
    title: '危险操作',
    description: '确认执行',
  } as unknown as ApprovalRequest

  it('showApprovalDialog 设置、hideApprovalDialog 清空', async () => {
    const store = await loadFreshUIStore()
    store.getState().showApprovalDialog(approval)
    expect(store.getState().approvalDialog).toBe(approval)
    store.getState().hideApprovalDialog()
    expect(store.getState().approvalDialog).toBeNull()
  })

  it('showApprovalDialog 两次以最后一次为准', async () => {
    const store = await loadFreshUIStore()
    const second = { id: 'ap-2' } as unknown as ApprovalRequest
    store.getState().showApprovalDialog(approval)
    store.getState().showApprovalDialog(second)
    expect(store.getState().approvalDialog).toBe(second)
  })
})

describe('uiStore — 消息搜索与跳转目标', () => {
  it('setMessageSearchQuery 设置与清空', async () => {
    const store = await loadFreshUIStore()
    store.getState().setMessageSearchQuery('关键字')
    expect(store.getState().messageSearchQuery).toBe('关键字')
    store.getState().setMessageSearchQuery('')
    expect(store.getState().messageSearchQuery).toBe('')
  })

  it('setMessageJump 写入目标与消费后清空', async () => {
    const store = await loadFreshUIStore()
    store.getState().setMessageJump({ pipelineId: 'p-1', sequence: 7 })
    expect(store.getState().messageJump).toEqual({ pipelineId: 'p-1', sequence: 7 })
    store.getState().setMessageJump(null)
    expect(store.getState().messageJump).toBeNull()
  })
})

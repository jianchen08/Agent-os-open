/**
 * layoutModeStore 分支补测：五空间布局状态机的全部动作 + persist merge 迁移清洗。
 *
 * 断言可观察行为（store 状态变化 / localStorage 副作用），不 mock store 本体；
 * merge 分支通过「预置 localStorage → vi.resetModules() → 动态 import」走真实
 * persist 反序列化路径。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { FloatingWindowInstance, WorkspaceTab, DockItem } from '@/types/layout'
import { useLayoutModeStore } from '../layoutModeStore'

const makeWindow = (id: string, extra: Partial<FloatingWindowInstance> = {}): FloatingWindowInstance => ({
  id,
  title: `w-${id}`,
  component: 'demo',
  position: { x: 0, y: 0 },
  size: { width: 320, height: 240 },
  zIndex: 1,
  isMinimized: false,
  isMaximized: false,
  ...extra,
})

const makeTab = (id: string, extra: Partial<WorkspaceTab> = {}): WorkspaceTab => ({
  id,
  title: `t-${id}`,
  moduleId: `m-${id}`,
  isActive: false,
  isPinned: false,
  ...extra,
})

const makeDock = (id: string, extra: Partial<DockItem> = {}): DockItem => ({
  id,
  moduleId: `m-${id}`,
  icon: 'folder',
  label: `l-${id}`,
  indicator: 'none',
  isActive: false,
  onClick: () => {},
  ...extra,
})

/** 复位到 store 初始态（保留动作函数） */
function resetStore() {
  useLayoutModeStore.setState({
    mode: 'five-space',
    floatingWindows: [],
    workspaceTabs: [],
    dockItems: [],
    fullscreenActive: false,
    fullscreenTitle: null,
    fullscreenContent: null,
    activeExecutions: [],
    pendingInteractions: [],
    connectionStatus: {
      state: 'connecting',
      latencyMs: null,
      reconnectAttempt: 0,
      lastConnectedAt: null,
      queuedMessages: 0,
    },
    workspaceDataVersion: 0,
    visitedTabIds: [],
  })
}

beforeEach(() => {
  localStorage.clear()
  resetStore()
})

describe('layoutModeStore — 模式切换', () => {
  it('toggleMode 在 classic / five-space 之间往返（幂等两跳回原点）', () => {
    expect(useLayoutModeStore.getState().mode).toBe('five-space')
    useLayoutModeStore.getState().toggleMode()
    expect(useLayoutModeStore.getState().mode).toBe('classic')
    useLayoutModeStore.getState().toggleMode()
    expect(useLayoutModeStore.getState().mode).toBe('five-space')
  })

  it('setMode 指定模式生效（两种取值）', () => {
    useLayoutModeStore.getState().setMode('classic')
    expect(useLayoutModeStore.getState().mode).toBe('classic')
    useLayoutModeStore.getState().setMode('five-space')
    expect(useLayoutModeStore.getState().mode).toBe('five-space')
  })
})

describe('layoutModeStore — 浮动窗口', () => {
  it('addFloatingWindow 追加且不覆盖已有条目', () => {
    useLayoutModeStore.getState().addFloatingWindow(makeWindow('a'))
    useLayoutModeStore.getState().addFloatingWindow(makeWindow('b'))
    expect(useLayoutModeStore.getState().floatingWindows.map((w) => w.id)).toEqual(['a', 'b'])
  })

  it('updateFloatingWindow 只改目标，非目标保持原值', () => {
    useLayoutModeStore.getState().addFloatingWindow(makeWindow('a'))
    useLayoutModeStore.getState().addFloatingWindow(makeWindow('b'))
    useLayoutModeStore.getState().updateFloatingWindow('a', { title: 'renamed', zIndex: 9 })
    const [a, b] = useLayoutModeStore.getState().floatingWindows
    expect(a).toMatchObject({ id: 'a', title: 'renamed', zIndex: 9 })
    expect(b.title).toBe('w-b')
  })

  it('closeFloatingWindow 按 id 剔除', () => {
    useLayoutModeStore.getState().addFloatingWindow(makeWindow('a'))
    useLayoutModeStore.getState().addFloatingWindow(makeWindow('b'))
    useLayoutModeStore.getState().closeFloatingWindow('a')
    expect(useLayoutModeStore.getState().floatingWindows.map((w) => w.id)).toEqual(['b'])
  })

  it('minimize/restoreFloatingWindow 只翻转 isMinimized 且互为逆操作', () => {
    useLayoutModeStore.getState().addFloatingWindow(makeWindow('a'))
    useLayoutModeStore.getState().minimizeFloatingWindow('a')
    expect(useLayoutModeStore.getState().floatingWindows[0].isMinimized).toBe(true)
    useLayoutModeStore.getState().restoreFloatingWindow('a')
    expect(useLayoutModeStore.getState().floatingWindows[0].isMinimized).toBe(false)
  })

  it('minimize/restoreFloatingWindow 不影响非目标窗口', () => {
    useLayoutModeStore.getState().addFloatingWindow(makeWindow('a'))
    useLayoutModeStore.getState().addFloatingWindow(makeWindow('b', { isMinimized: true }))
    useLayoutModeStore.getState().minimizeFloatingWindow('a')
    expect(useLayoutModeStore.getState().floatingWindows.map((w) => [w.id, w.isMinimized])).toEqual([
      ['a', true],
      ['b', true],
    ])
    useLayoutModeStore.getState().restoreFloatingWindow('a')
    expect(useLayoutModeStore.getState().floatingWindows.map((w) => [w.id, w.isMinimized])).toEqual([
      ['a', false],
      ['b', true],
    ])
  })
})

describe('layoutModeStore — 工作区 Tab', () => {
  it('addWorkspaceTab 激活新 tab 时把旧 tab 全部置为非激活', () => {
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('a', { isActive: true }))
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('b', { isActive: true }))
    const state = useLayoutModeStore.getState()
    expect(state.workspaceTabs.map((t) => [t.id, t.isActive])).toEqual([
      ['a', false],
      ['b', true],
    ])
  })

  it('addWorkspaceTab 非激活 tab 不影响既有激活态', () => {
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('a', { isActive: true }))
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('b', { isActive: false }))
    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.isActive)).toEqual([true, false])
  })

  it('addWorkspaceTab 非激活 tab 不写入 visitedTabIds（懒挂载语义）', () => {
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('a', { isActive: false }))
    expect(useLayoutModeStore.getState().visitedTabIds).toEqual([])
  })

  it('addWorkspaceTab 同 id 激活两次时 visitedTabIds 去重', () => {
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('a', { isActive: true }))
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('a', { isActive: true }))
    expect(useLayoutModeStore.getState().visitedTabIds).toEqual(['a'])
  })

  it('setActiveTab 只激活目标且并入 visitedTabIds', () => {
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('a'))
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('b'))
    useLayoutModeStore.getState().setActiveTab('b')
    const state = useLayoutModeStore.getState()
    expect(state.workspaceTabs.map((t) => [t.id, t.isActive])).toEqual([
      ['a', false],
      ['b', true],
    ])
    expect(state.visitedTabIds).toEqual(['b'])
  })

  it('setActiveTab 重复激活已访问 tab 不产生重复项（性质：visited 长度不随重复调用增长）', () => {
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('a'))
    useLayoutModeStore.getState().setActiveTab('a')
    const after1 = useLayoutModeStore.getState().visitedTabIds.length
    useLayoutModeStore.getState().setActiveTab('a')
    expect(useLayoutModeStore.getState().visitedTabIds.length).toBe(after1)
  })

  it('closeWorkspaceTab 同时清理 tab 与 visited 记录', () => {
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('a', { isActive: true }))
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('b', { isActive: true }))
    useLayoutModeStore.getState().closeWorkspaceTab('a')
    const state = useLayoutModeStore.getState()
    expect(state.workspaceTabs.map((t) => t.id)).toEqual(['b'])
    expect(state.visitedTabIds).toEqual(['b'])
  })

  it('closeOtherWorkspaceTabs 保留目标与钉住项、丢弃其余', () => {
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('a', { isActive: true }))
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('b'))
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('pin', { isPinned: true }))
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('c'))
    useLayoutModeStore.getState().setActiveTab('b')
    useLayoutModeStore.getState().closeOtherWorkspaceTabs('b')
    const state = useLayoutModeStore.getState()
    expect(state.workspaceTabs.map((t) => t.id).sort()).toEqual(['b', 'pin'])
    expect(state.visitedTabIds).toEqual(['b'])
  })

  it('closeOtherWorkspaceTabs 目标被关闭且剩余无激活项时激活第一个剩余 tab', () => {
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('a'))
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('pin1', { isPinned: true }))
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('pin2', { isPinned: true }))
    // 目标 'a' 未激活；剩余仅钉住项且均非激活 → 需自动激活第一个
    useLayoutModeStore.getState().closeOtherWorkspaceTabs('a')
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs[0].id).toBe('a')
    expect(tabs.filter((t) => t.isActive).map((t) => t.id)).toEqual(['a'])
  })

  it('closeOtherWorkspaceTabs 保留项中已有激活项时不再补激活', () => {
    // 钉住项 'a' 处于激活态 → 保留集合中已有激活项，目标 'b' 不应被强行激活
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('a', { isActive: true, isPinned: true }))
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('b'))
    useLayoutModeStore.getState().closeOtherWorkspaceTabs('b')
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.map((t) => [t.id, t.isActive])).toEqual([
      ['a', true],
      ['b', false],
    ])
  })

  it('closeAllWorkspaceTabs 仅保留钉住项并同步裁剪 visited', () => {
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('a', { isActive: true }))
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('pin', { isPinned: true }))
    useLayoutModeStore.getState().closeAllWorkspaceTabs()
    const state = useLayoutModeStore.getState()
    expect(state.workspaceTabs.map((t) => t.id)).toEqual(['pin'])
    expect(state.visitedTabIds).toEqual([])
  })

  it('closeAllWorkspaceTabs 无钉住项时清空全部', () => {
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('a'))
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('b'))
    useLayoutModeStore.getState().closeAllWorkspaceTabs()
    expect(useLayoutModeStore.getState().workspaceTabs).toEqual([])
  })

  it('updateWorkspaceTab 只改目标 tab 的指定字段', () => {
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('a'))
    useLayoutModeStore.getState().addWorkspaceTab(makeTab('b'))
    useLayoutModeStore.getState().updateWorkspaceTab('a', { title: 'renamed' })
    const [a, b] = useLayoutModeStore.getState().workspaceTabs
    expect(a.title).toBe('renamed')
    expect(b.title).toBe('t-b')
  })
})

describe('layoutModeStore — Dock 项', () => {
  it('setDockItems 整体替换', () => {
    useLayoutModeStore.getState().setDockItems([makeDock('a'), makeDock('b')])
    useLayoutModeStore.getState().setDockItems([makeDock('c')])
    expect(useLayoutModeStore.getState().dockItems.map((d) => d.id)).toEqual(['c'])
  })

  it('updateDockItem 合并部分字段且保留 onClick 引用', () => {
    const onClick = () => {}
    useLayoutModeStore.getState().setDockItems([makeDock('a', { onClick }), makeDock('b')])
    useLayoutModeStore.getState().updateDockItem('a', { indicator: 'badge', badgeCount: 3 })
    const [a, b] = useLayoutModeStore.getState().dockItems
    expect(a).toMatchObject({ indicator: 'badge', badgeCount: 3 })
    expect(a.onClick).toBe(onClick)
    expect(b.indicator).toBe('none')
  })
})

describe('layoutModeStore — 全屏遮罩', () => {
  it('enterFullscreen 写入标题与内容，exitFullscreen 清空三者', () => {
    useLayoutModeStore.getState().enterFullscreen('标题', 'content-node')
    const entered = useLayoutModeStore.getState()
    expect(entered.fullscreenActive).toBe(true)
    expect(entered.fullscreenTitle).toBe('标题')
    expect(entered.fullscreenContent).toBe('content-node')

    useLayoutModeStore.getState().exitFullscreen()
    const exited = useLayoutModeStore.getState()
    expect([exited.fullscreenActive, exited.fullscreenTitle, exited.fullscreenContent]).toEqual([
      false,
      null,
      null,
    ])
  })
})

describe('layoutModeStore — 执行事件', () => {
  const evt = (id: string, status: 'running' | 'completed' | 'failed' = 'running') => ({
    id,
    type: 'tool' as const,
    name: `e-${id}`,
    status,
    progress: 0,
    startedAt: '2026-09-14T00:00:00.000Z',
  })

  it('addOrUpdateExecution 新 id 追加、同 id 原位替换（不重复且顺序不变）', () => {
    useLayoutModeStore.getState().addOrUpdateExecution(evt('a'))
    useLayoutModeStore.getState().addOrUpdateExecution(evt('b'))
    useLayoutModeStore.getState().addOrUpdateExecution({ ...evt('a'), progress: 80 })

    const list = useLayoutModeStore.getState().activeExecutions
    expect(list.map((e) => e.id)).toEqual(['a', 'b'])
    expect(list[0].progress).toBe(80)
  })

  it('removeExecution 按 id 剔除', () => {
    useLayoutModeStore.getState().addOrUpdateExecution(evt('a'))
    useLayoutModeStore.getState().addOrUpdateExecution(evt('b'))
    useLayoutModeStore.getState().removeExecution('a')
    expect(useLayoutModeStore.getState().activeExecutions.map((e) => e.id)).toEqual(['b'])
  })

  it('clearCompletedExecutions 只保留 running（性质：结果全为 running）', () => {
    useLayoutModeStore.getState().addOrUpdateExecution(evt('a', 'running'))
    useLayoutModeStore.getState().addOrUpdateExecution(evt('b', 'completed'))
    useLayoutModeStore.getState().addOrUpdateExecution(evt('c', 'failed'))
    useLayoutModeStore.getState().clearCompletedExecutions()
    const rest = useLayoutModeStore.getState().activeExecutions
    expect(rest.map((e) => e.id)).toEqual(['a'])
    expect(rest.every((e) => e.status === 'running')).toBe(true)
  })
})

describe('layoutModeStore — 交互请求与连接状态', () => {
  it('addInteraction 追加、removeInteraction 按 id 剔除', () => {
    useLayoutModeStore.getState().addInteraction({
      id: 'r1',
      executionId: 'e1',
      prompt: 'approve?',
      timestamp: '2026-09-14T00:00:00.000Z',
    })
    useLayoutModeStore.getState().addInteraction({
      id: 'r2',
      executionId: 'e2',
      prompt: 'deny?',
      timestamp: '2026-09-14T00:00:01.000Z',
    })
    useLayoutModeStore.getState().removeInteraction('r1')
    expect(useLayoutModeStore.getState().pendingInteractions.map((r) => r.id)).toEqual(['r2'])
  })

  it('updateConnectionStatus 部分合并，未提供字段保持原值', () => {
    useLayoutModeStore.getState().updateConnectionStatus({ state: 'connected', latencyMs: 12 })
    const s1 = useLayoutModeStore.getState().connectionStatus
    expect(s1).toMatchObject({ state: 'connected', latencyMs: 12, queuedMessages: 0 })

    useLayoutModeStore.getState().updateConnectionStatus({ queuedMessages: 5 })
    const s2 = useLayoutModeStore.getState().connectionStatus
    expect(s2).toMatchObject({ state: 'connected', latencyMs: 12, queuedMessages: 5 })
  })
})

describe('layoutModeStore — 工作区数据版本号', () => {
  it('bumpWorkspaceDataVersion 单调递增', () => {
    const v0 = useLayoutModeStore.getState().workspaceDataVersion
    useLayoutModeStore.getState().bumpWorkspaceDataVersion()
    useLayoutModeStore.getState().bumpWorkspaceDataVersion()
    const v2 = useLayoutModeStore.getState().workspaceDataVersion
    expect(v2).toBe(v0 + 2)
    expect(v2).toBeGreaterThan(v0)
  })
})

describe('layoutModeStore — persist merge 迁移清洗', () => {
  /** 预置 localStorage 后重新加载模块，让 persist 走真实 rehydrate → merge */
  async function loadWithPersisted(state: Record<string, unknown>) {
    vi.resetModules()
    localStorage.setItem('layout-mode', JSON.stringify({ state, version: 0 }))
    const mod = await import('../layoutModeStore')
    return mod.useLayoutModeStore
  }

  it('清洗退役的 ws-panel-workspace / ws-panel-plugins 页签，保留合法页签', async () => {
    const store = await loadWithPersisted({
      mode: 'classic',
      workspaceTabs: [
        { id: 'ws-panel-workspace', title: '工作区', moduleId: 'x', isActive: false, isPinned: false },
        { id: 'ws-panel-plugins', title: '插件管理', moduleId: 'x', isActive: false, isPinned: false },
        { id: 'keep-me', title: '保留', moduleId: 'x', isActive: true, isPinned: false },
      ],
    })
    const tabs = store.getState().workspaceTabs
    expect(tabs.map((t) => t.id)).toEqual(['keep-me'])
    expect(store.getState().mode).toBe('classic')
  })

  it('清洗后为空 → 补齐默认「任务管理」页签并激活', async () => {
    const store = await loadWithPersisted({
      mode: 'dark' as never,
      workspaceTabs: [
        { id: 'ws-panel-workspace', title: '工作区', moduleId: 'x', isActive: true, isPinned: false },
      ],
    })
    const tabs = store.getState().workspaceTabs
    expect(tabs).toHaveLength(1)
    expect(tabs[0]).toMatchObject({
      id: 'ws-panel-tasks',
      component: 'pipeline_manager',
      isActive: true,
    })
  })

  it('workspaceTabs 非数组时落回当前空列表并补默认页签', async () => {
    const store = await loadWithPersisted({ mode: 'five-space', workspaceTabs: 'not-an-array' })
    expect(store.getState().workspaceTabs.map((t) => t.id)).toEqual(['ws-panel-tasks'])
  })

  it('merge 强制重置运行时状态（浮窗/Dock/全屏/执行/交互/版本号）', async () => {
    const store = await loadWithPersisted({
      mode: 'five-space',
      workspaceTabs: [
        { id: 'keep', title: 'k', moduleId: 'm', isActive: true, isPinned: false },
      ],
      floatingWindows: [{ id: 'fw' }],
      dockItems: [{ id: 'dk' }],
      fullscreenActive: true,
      fullscreenTitle: 'T',
      fullscreenContent: 'C',
      activeExecutions: [{ id: 'e' }],
      pendingInteractions: [{ id: 'r' }],
      workspaceDataVersion: 42,
    })
    const s = store.getState()
    expect(s.floatingWindows).toEqual([])
    expect(s.dockItems).toEqual([])
    expect(s.fullscreenActive).toBe(false)
    expect(s.fullscreenTitle).toBeNull()
    expect(s.fullscreenContent).toBeNull()
    expect(s.activeExecutions).toEqual([])
    expect(s.pendingInteractions).toEqual([])
    expect(s.workspaceDataVersion).toBe(0)
    // connectionStatus 走「当前值」分支（仍为初始 connecting）
    expect(s.connectionStatus.state).toBe('connecting')
  })
})

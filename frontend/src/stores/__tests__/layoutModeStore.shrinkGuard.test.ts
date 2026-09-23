// @feature: FP-T12 前端适配 | @ci: frontend-test
/** @bug BUG-79 workspaceTabs 二现丢失（14→2） @ci frontend-test */
/**
 * layout-mode persist 在每次 set 时整体覆写 workspaceTabs——任何把页签集合写小
 * 的路径都会被持久化放大成不可逆的用户数据丢失（BUG-79：12 个用户签被覆盖写没）。
 *
 * 缩容闸契约（persist 写入侧）：
 * - 页签集合缩小的持久化写入仅允许两类来源：用户关签动作（close*，事置意图旗标）
 *   与 merge 迁移清洗（事置迁移旗标）；其余一律拒写并告警；
 * - 拒写后 localStorage 保留上次完好集合，重新加载经 merge 原样恢复（自愈）；
 * - 用户主动关签（含单个关闭与关闭全部）仍按原语义持久化，闸门不误伤。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { WorkspaceTab } from '@/types/layout'

/** R241 恢复集同形的 5 签基线（任务管理/导航/直播导演台/主城/记忆） */
const BASE_TAB_IDS = [
  'ws-panel-tasks',
  'ws-panel-workspace-nav',
  'ws-plugin-director',
  'ws-plugin-maincity',
  'ws-plugin-memory',
]

const makeTab = (id: string, extra: Partial<WorkspaceTab> = {}): WorkspaceTab => ({
  id,
  title: `t-${id}`,
  moduleId: `m-${id}`,
  isActive: false,
  isPinned: false,
  ...extra,
})

/** 预置 localStorage 后重新加载模块，让 persist 走真实 rehydrate → merge → 写回 */
async function loadWithPersisted(tabIds: string[]) {
  vi.resetModules()
  localStorage.setItem(
    'layout-mode',
    JSON.stringify({
      state: {
        mode: 'five-space',
        workspaceTabs: tabIds.map((id) => makeTab(id)),
      },
      version: 0,
    }),
  )
  const mod = await import('../layoutModeStore')
  return mod.useLayoutModeStore
}

function persistedTabIds(): string[] {
  const raw = localStorage.getItem('layout-mode')
  expect(raw).toBeTruthy()
  const tabs = JSON.parse(raw!).state?.workspaceTabs
  return Array.isArray(tabs) ? tabs.map((t: WorkspaceTab) => t.id) : []
}

beforeEach(() => {
  localStorage.clear()
})

describe('layoutModeStore persist 缩容闸（BUG-79）', () => {
  it('非关签写路径把页签集写小时拒写：localStorage 保留完好集合，后续良性写也带不进缩容集', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const store = await loadWithPersisted(BASE_TAB_IDS)

      // 模拟「未知路径整体覆盖」：不经过任何关签动作，把 5 签替换成 BUG-79 观测残集
      useShrunkTabs(store)

      // 缩容后的任何良性写（setMode 每次都会触发 persist 整体覆写）不得把缩容集落盘
      store.getState().setMode('classic')

      expect(persistedTabIds()).toEqual(BASE_TAB_IDS)
      expect(warnSpy).toHaveBeenCalled()
    } finally {
      warnSpy.mockRestore()
    }
  })

  it('拒写自愈：缩容写被拒后重新加载，页签集原样恢复（persist 侧恢复语义）', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const store = await loadWithPersisted(BASE_TAB_IDS)
      useShrunkTabs(store)
      store.getState().setMode('classic')
      expect(persistedTabIds()).toEqual(BASE_TAB_IDS)

      // 重新加载 = 用户刷新：从保留的完好集合恢复
      const reloaded = await loadWithPersisted(persistedTabIds())
      expect(reloaded.getState().workspaceTabs.map((t) => t.id)).toEqual(BASE_TAB_IDS)
    } finally {
      warnSpy.mockRestore()
    }
  })

  it('用户主动关签仍可持久化缩容（不误伤）：单个关闭精确落盘，关闭全部合法', async () => {
    const store = await loadWithPersisted(BASE_TAB_IDS)

    store.getState().closeWorkspaceTab('ws-plugin-director')
    const afterSingle = persistedTabIds()
    expect(afterSingle).toEqual(BASE_TAB_IDS.filter((id) => id !== 'ws-plugin-director'))
    // 性质：单次关闭恰好缩 1 签
    expect(BASE_TAB_IDS.length - afterSingle.length).toBe(1)

    store.getState().closeAllWorkspaceTabs()
    // 用户主动全清（无钉住项）→ 落盘为空集（下次启动由 merge 默认页签兜底）
    expect(persistedTabIds()).toEqual([])
  })

  it('增长与中性写不误伤：加签、切换激活、连接状态更新照常落盘', async () => {
    const store = await loadWithPersisted(BASE_TAB_IDS)

    store.getState().setActiveTab('ws-plugin-director')
    expect(persistedTabIds()).toEqual(BASE_TAB_IDS)
    const stored = JSON.parse(localStorage.getItem('layout-mode')!).state.workspaceTabs
    expect(stored.find((t: WorkspaceTab) => t.id === 'ws-plugin-director').isActive).toBe(true)

    store.getState().addWorkspaceTab(makeTab('ws-plugin-triggers', { isActive: true }))
    const afterAdd = persistedTabIds()
    expect(afterAdd).toHaveLength(BASE_TAB_IDS.length + 1)
    expect(afterAdd).toContain('ws-plugin-triggers')

    store.getState().updateConnectionStatus({ state: 'connected' })
    expect(persistedTabIds()).toEqual(afterAdd)
  })

  it('merge 迁移清洗仍工作：退役页签经清洗授权放行落盘', async () => {
    const store = await loadWithPersisted([
      ...BASE_TAB_IDS,
      'ws-panel-workspace', // 退役：旧固定「工作区」签
      'ws-plugin-tasks', // 退役：任务管理归前端前的插件签
    ])

    // merge 清洗后（内存不含退役签）的首次写回：丢失集 ⊆ 清洗移除集 → 放行
    expect(store.getState().workspaceTabs.map((t) => t.id)).toEqual(BASE_TAB_IDS)
    store.getState().updateConnectionStatus({ state: 'connected' })
    expect(persistedTabIds()).toEqual(BASE_TAB_IDS)
    expect(persistedTabIds()).not.toContain('ws-panel-workspace')
    expect(persistedTabIds()).not.toContain('ws-plugin-tasks')
  })
})

describe('layoutModeStore 换位与存储包装层', () => {
  it('reorderWorkspaceTabs 换位只改顺序：页签集合不变照常落盘（缩容闸不误伤）', async () => {
    const store = await loadWithPersisted(BASE_TAB_IDS)

    store.getState().reorderWorkspaceTabs('ws-panel-tasks', 'ws-plugin-memory')

    const ids = store.getState().workspaceTabs.map((t) => t.id)
    // 拖拽源插到目标当前位置（移除自身后的索引）→ 首签移到末尾，其余保序
    expect(ids).toEqual([...BASE_TAB_IDS.slice(1), BASE_TAB_IDS[0]])
    expect([...ids].sort()).toEqual([...BASE_TAB_IDS].sort())
    expect(persistedTabIds()).toEqual(ids)
  })

  it('reorderWorkspaceTabs 守卫：同签与未知签保持原序', async () => {
    const store = await loadWithPersisted(BASE_TAB_IDS)
    const before = store.getState().workspaceTabs.map((t) => t.id)

    store.getState().reorderWorkspaceTabs('ws-panel-tasks', 'ws-panel-tasks')
    store.getState().reorderWorkspaceTabs('ghost', 'ws-panel-tasks')
    store.getState().reorderWorkspaceTabs('ws-panel-tasks', 'ghost')

    expect(store.getState().workspaceTabs.map((t) => t.id)).toEqual(before)
    expect(persistedTabIds()).toEqual(before)
  })

  it('clearStorage 经包装层 removeItem 清空持久化（透传内层 storage）', async () => {
    const store = await loadWithPersisted(BASE_TAB_IDS)
    expect(localStorage.getItem('layout-mode')).not.toBeNull()

    store.persist.clearStorage()

    expect(localStorage.getItem('layout-mode')).toBeNull()
  })
})

/** 绕过关签动作直接整体替换页签集（模型化 BUG-79 观测到的非关签覆盖路径） */
function useShrunkTabs(store: { setState: (s: Record<string, unknown>) => void }) {
  store.setState({
    workspaceTabs: [makeTab('ws-panel-workspace-nav', { isActive: true }), makeTab('ws-plugin-triggers')],
  })
}

/** fileEditorRegistry 与 layoutModeStore 工作区标签持久化的最小冒烟测试 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

describe('workspaceTabsPersist', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    localStorage.clear()
  })

  describe('fileEditorRegistry', () => {
    it('registerFileEditor 应当 write-through 到 localStorage', async () => {
      const mod = await import('../fileEditorRegistry')
      mod.registerFileEditor('tab-1', {
        filePath: 'src/main.py',
        fileName: 'main.py',
        content: 'print("hi")',
        size: 11,
        containerTaskId: '_local',
      })

      const raw = localStorage.getItem('file-editor-registry')
      expect(raw).toBeTruthy()
      const parsed = JSON.parse(raw!)
      expect(parsed['tab-1']).toBeDefined()
      expect(parsed['tab-1'].filePath).toBe('src/main.py')
      expect(parsed['tab-1'].content).toBe('print("hi")')
      // 运行时字段不应被持久化
      expect(parsed['tab-1'].loading).toBeUndefined()
    })

    it('updateFileEditorData 后 localStorage 应反映最新内容', async () => {
      const mod = await import('../fileEditorRegistry')
      mod.registerFileEditor('tab-2', {
        filePath: 'a.txt',
        fileName: 'a.txt',
        content: 'old',
        containerTaskId: '_local',
      })
      mod.updateFileEditorData('tab-2', { content: 'new' })

      const parsed = JSON.parse(localStorage.getItem('file-editor-registry')!)
      expect(parsed['tab-2'].content).toBe('new')
    })

    it('removeFileEditorData 应当从 localStorage 同步删除', async () => {
      const mod = await import('../fileEditorRegistry')
      mod.registerFileEditor('tab-3', {
        filePath: 'a.txt',
        fileName: 'a.txt',
        content: '',
        containerTaskId: '_local',
      })
      mod.removeFileEditorData('tab-3')

      const parsed = JSON.parse(localStorage.getItem('file-editor-registry')!)
      expect(parsed['tab-3']).toBeUndefined()
    })
  })

  describe('layoutModeStore', () => {
    it('partialize 应包含 workspaceTabs', async () => {
      const { useLayoutModeStore } = await import('../layoutModeStore')
      useLayoutModeStore.getState().addWorkspaceTab({
        id: 'file-tab-x',
        title: 'x.ts',
        moduleId: '__file_editor__',
        isActive: true,
        isPinned: false,
      })

      // 触发持久化
      const raw = localStorage.getItem('layout-mode')
      expect(raw).toBeTruthy()
      const parsed = JSON.parse(raw!)
      const tabs = parsed.state?.workspaceTabs ?? []
      expect(Array.isArray(tabs)).toBe(true)
      expect(tabs.some((t: any) => t.id === 'file-tab-x')).toBe(true)
    })

    /** 构造初始 tabs：1 个 pinned + 2 个普通（其中 t1 激活） */
    async function seedTabs() {
      const { useLayoutModeStore } = await import('../layoutModeStore')
      // 清空，避免与其它用例残留状态串扰
      useLayoutModeStore.setState({
        workspaceTabs: [],
        visitedTabIds: [],
      })
      const add = (id: string, isPinned: boolean, isActive: boolean) =>
        useLayoutModeStore.getState().addWorkspaceTab({
          id,
          title: id,
          moduleId: '__file_editor__',
          isActive,
          isPinned,
        })
      add('pinned', true, false)
      add('t1', false, true)
      add('t2', false, false)
      return useLayoutModeStore
    }

    it('closeOtherWorkspaceTabs 应保留 keepTabId 与 pinned，并使其成为唯一激活', async () => {
      const store = await seedTabs()
      store.getState().closeOtherWorkspaceTabs('t1')

      const tabs = store.getState().workspaceTabs
      const ids = tabs.map((t) => t.id).sort()
      // t2 被关闭；pinned 与 t1 保留
      expect(ids).toEqual(['pinned', 't1'])
      // 仅 keepTabId 激活；pinned 不应被错误激活
      const activeIds = tabs.filter((t) => t.isActive).map((t) => t.id)
      expect(activeIds).toEqual(['t1'])
      // visited 中 t2 被清理
      expect(store.getState().visitedTabIds).not.toContain('t2')
    })

    it('closeAllWorkspaceTabs 应保留 pinned、移除其余、激活首个 pinned', async () => {
      const store = await seedTabs()
      store.getState().closeAllWorkspaceTabs()

      const tabs = store.getState().workspaceTabs
      // 仅 pinned 保留
      expect(tabs.map((t) => t.id)).toEqual(['pinned'])
      // pinned 成为激活（首个剩余 tab）
      expect(tabs[0].isActive).toBe(true)
      // 可关 tab 的 visited 记录被清理
      expect(store.getState().visitedTabIds).not.toContain('t1')
      expect(store.getState().visitedTabIds).not.toContain('t2')
    })

    it('closeAllWorkspaceTabs 在无 pinned 时应清空至零', async () => {
      const { useLayoutModeStore } = await import('../layoutModeStore')
      useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
      useLayoutModeStore.getState().addWorkspaceTab({
        id: 'only',
        title: 'only',
        moduleId: '__file_editor__',
        isActive: true,
        isPinned: false,
      })
      useLayoutModeStore.getState().closeAllWorkspaceTabs()

      expect(useLayoutModeStore.getState().workspaceTabs).toEqual([])
      expect(useLayoutModeStore.getState().visitedTabIds).toEqual([])
    })
  })
})

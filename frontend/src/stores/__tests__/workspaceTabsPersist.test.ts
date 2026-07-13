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
  })
})

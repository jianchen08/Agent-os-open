/**
 * 附件预览打开服务测试
 *
 * 验证 openAttachment：
 * - 已打开的附件去重（激活现有 Tab 而非新建）
 * - 图片/PDF 不 fetch 内容
 * - 文本类 fetch 内容
 * - fetch 失败时兜底不报错
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { openAttachment } from '@/services/attachmentOpener'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { getFileEditorData } from '@/stores/fileEditorRegistry'

// Mock fetch
const fetchMock = vi.fn()
global.fetch = fetchMock as unknown as typeof fetch

describe('openAttachment', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    // 重置 layout store
    useLayoutModeStore.setState({ workspaceTabs: [], activeTabId: null, visitedTabIds: [] })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('文本附件 fetch 内容并注册编辑器数据', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      text: async () => '文件内容',
    })

    await openAttachment({ id: 'f1', name: 'notes.txt', url: '/uploads/notes.txt' })

    expect(fetchMock).toHaveBeenCalledWith('/uploads/notes.txt')
    const data = getFileEditorData('attach-f1')
    expect(data?.content).toBe('文件内容')
    expect(data?.url).toBe('/uploads/notes.txt')
  })

  it('图片附件不 fetch 内容（靠 url 渲染）', async () => {
    await openAttachment({ id: 'f2', name: 'pic.png', url: '/uploads/pic.png' })

    expect(fetchMock).not.toHaveBeenCalled()
    const data = getFileEditorData('attach-f2')
    expect(data?.content).toBe('')
    expect(data?.url).toBe('/uploads/pic.png')
  })

  it('PDF 附件不 fetch 内容', async () => {
    await openAttachment({ id: 'f3', name: 'doc.pdf', url: '/uploads/doc.pdf' })

    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('已打开的附件去重：激活现有 Tab，不重复注册', async () => {
    await openAttachment({ id: 'f4', name: 'a.txt', url: '/uploads/a.txt' })
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(1)

    // 第二次打开同一附件
    fetchMock.mockClear()
    await openAttachment({ id: 'f4', name: 'a.txt', url: '/uploads/a.txt' })

    // 不新增 Tab，不重新 fetch
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(1)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('fetch 失败时不抛异常，content 留空', async () => {
    fetchMock.mockRejectedValueOnce(new Error('network'))

    await expect(
      openAttachment({ id: 'f5', name: 'b.txt', url: '/uploads/b.txt' }),
    ).resolves.not.toThrow()

    const data = getFileEditorData('attach-f5')
    expect(data?.content).toBe('')
  })

  it('Tab 使用 __file_editor__ moduleId 并激活', async () => {
    await openAttachment({ id: 'f6', name: 'c.md', url: '/uploads/c.md' })

    const tab = useLayoutModeStore.getState().workspaceTabs[0]
    expect(tab.moduleId).toBe('__file_editor__')
    expect(tab.isActive).toBe(true)
    expect(tab.title).toBe('c.md')
  })

  it('无 id 时用 url 作为去重 key', async () => {
    await openAttachment({ name: 'd.txt', url: '/uploads/d.txt' })
    const data = getFileEditorData('attach-/uploads/d.txt')
    expect(data).toBeDefined()
  })
})

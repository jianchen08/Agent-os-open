/**
 * 附件预览打开服务测试
 *
 * 验证 openAttachment：
 * - 已打开的附件去重（激活现有 Tab 而非新建）
 * - 图片/PDF 不拉取内容
 * - 文本类经 apiClient 拉取内容（origin 相对直链 + text 响应 + silent 防双吐司）
 * - 拉取失败时兜底不报错（本地 toast 告知）
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { openAttachment } from '@/services/attachmentOpener'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { getFileEditorData } from '@/stores/fileEditorRegistry'

// mock sonner（外部 toast 库）：断言失败对用户可感知（错误 toast 弹出）
const toastErrorMock = vi.fn()
vi.mock('sonner', () => ({
  toast: { error: (...args: unknown[]) => toastErrorMock(...args) },
}))

// mock apiClient（外部 HTTP 依赖）：文本内容拉取统一走适配层
const getMock = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: { get: (...args: unknown[]) => getMock(...args) },
}))

describe('openAttachment', () => {
  beforeEach(() => {
    getMock.mockReset()
    toastErrorMock.mockClear()
    // 重置 layout store
    useLayoutModeStore.setState({ workspaceTabs: [], activeTabId: null, visitedTabIds: [] })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('文本附件经 apiClient 拉取内容并注册编辑器数据', async () => {
    getMock.mockResolvedValueOnce({ data: '文件内容' })

    await openAttachment({ id: 'f1', name: 'notes.txt', url: '/uploads/notes.txt' })

    expect(getMock).toHaveBeenCalledWith('/uploads/notes.txt', {
      baseURL: '',
      responseType: 'text',
      silent: true,
    })
    const data = getFileEditorData('attach-f1')
    expect(data?.content).toBe('文件内容')
    expect(data?.url).toBe('/uploads/notes.txt')
  })

  it('图片附件不拉取内容（靠 url 渲染）', async () => {
    await openAttachment({ id: 'f2', name: 'pic.png', url: '/uploads/pic.png' })

    expect(getMock).not.toHaveBeenCalled()
    const data = getFileEditorData('attach-f2')
    expect(data?.content).toBe('')
    expect(data?.url).toBe('/uploads/pic.png')
  })

  it('PDF 附件不拉取内容', async () => {
    await openAttachment({ id: 'f3', name: 'doc.pdf', url: '/uploads/doc.pdf' })

    expect(getMock).not.toHaveBeenCalled()
  })

  it('已打开的附件去重：激活现有 Tab，不重复注册', async () => {
    await openAttachment({ id: 'f4', name: 'a.txt', url: '/uploads/a.txt' })
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(1)

    // 第二次打开同一附件
    getMock.mockClear()
    await openAttachment({ id: 'f4', name: 'a.txt', url: '/uploads/a.txt' })

    // 不新增 Tab，不重新拉取
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(1)
    expect(getMock).not.toHaveBeenCalled()
  })

  it('拉取网络失败：不抛异常、content 留空，且用户收到错误 toast', async () => {
    getMock.mockRejectedValueOnce(new Error('network'))

    await expect(
      openAttachment({ id: 'f5', name: 'b.txt', url: '/uploads/b.txt' }),
    ).resolves.not.toThrow()

    const data = getFileEditorData('attach-f5')
    expect(data?.content).toBe('')
    expect(toastErrorMock).toHaveBeenCalledTimes(1)
    expect(String(toastErrorMock.mock.calls[0][0])).toContain('b.txt')
  })

  it('拉取返回非 2xx：用户收到错误 toast，content 留空', async () => {
    getMock.mockRejectedValueOnce(
      Object.assign(new Error('404'), { response: { status: 404 } }),
    )

    await openAttachment({ id: 'f5b', name: 'missing.txt', url: '/uploads/missing.txt' })

    expect(toastErrorMock).toHaveBeenCalledTimes(1)
    expect(String(toastErrorMock.mock.calls[0][0])).toContain('404')
    expect(getFileEditorData('attach-f5b')?.content).toBe('')
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

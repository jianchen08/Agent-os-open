/**
 * fileOpener（工具卡片"打开文件"链路）单元测试
 *
 * 工具卡片（read 卡/写卡 open_file action）点击后经全局回调进入 openFile：
 * - 按 containerTaskId（缺省 _local）拉取文件内容 → 注册编辑器数据 + 建工作区 Tab
 * - 按需加载（用户裁定 2026-10-24）：打开只拉头部 64KB 窗口（offset/limit），
 *   has_more=false 时标记全量（loadedBytes=undefined 可编辑）
 * - 已打开的文件去重（激活现有 Tab，不重复请求）
 * - 任务工作空间未命中时回退 _local 重试
 * - 失败如实返回 success=false + message（供上层 reportError 通知中心弹出，
 *   禁止静默——用户裁定 2026-10-24）
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  FILE_OPEN_CHUNK_BYTES,
  loadMoreFileContent,
  openFile,
} from '@/services/fileOpener'
import { loadFileViewers } from '@/services/fileViewerRegistry'
import { apiClient } from '@/services/api/client'
import { WORKSPACE_SERVICE_ENDPOINTS } from '@/services/api/endpoints.generated'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useWidgetEventStore } from '@/stores/widgetEventStore'
import { getFileEditorData } from '@/stores/fileEditorRegistry'

vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn() },
}))

const getMock = vi.mocked(apiClient.get)

function fileContentResp(content: string, opts?: { hasMore?: boolean; loaded?: number }) {
  const loaded = opts?.loaded ?? content.length
  return {
    data: {
      success: true,
      content,
      size: opts?.hasMore ? 100000 : loaded,
      loaded,
      has_more: opts?.hasMore ?? false,
    },
  }
}

const CONTENT_URL = (id: string) =>
  WORKSPACE_SERVICE_ENDPOINTS.workspaces_file_content_get.replace('{container_task_id}', id)

describe('fileOpener.openFile（工具卡片打开文件链路）', () => {
  beforeEach(() => {
    getMock.mockReset()
    useLayoutModeStore.setState({ workspaceTabs: [], activeTabId: null, visitedTabIds: [] })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('点击后拉取头部窗口并建工作区 Tab：offset/limit 按需参数 + 注册编辑器数据', async () => {
    getMock.mockResolvedValueOnce(fileContentResp('print(1)') as never)

    const res = await openFile('src/main.py', { containerTaskId: 'task-42' })

    expect(res.success).toBe(true)
    // 按需加载：打开只拉头部 64KB 窗口（offset=0&limit=64K）
    expect(getMock).toHaveBeenCalledWith(CONTENT_URL('task-42'), {
      params: { path: 'src/main.py', offset: 0, limit: FILE_OPEN_CHUNK_BYTES },
    })

    const data = getFileEditorData('file-local-src_main.py')
    expect(data?.content).toBe('print(1)')
    expect(data?.containerTaskId).toBe('task-42')
    expect(data?.fileName).toBe('main.py')

    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs).toHaveLength(1)
    expect(tabs[0].moduleId).toBe('__file_editor__')
    expect(tabs[0].isActive).toBe(true)
    expect(tabs[0].title).toBe('main.py')
  })

  it('has_more=false（全量小文件）→ loadedBytes 不落（全量标记，可编辑）', async () => {
    getMock.mockResolvedValueOnce(fileContentResp('tiny') as never)

    await openFile('small.txt')

    expect(getFileEditorData('file-local-small.txt')?.loadedBytes).toBeUndefined()
  })

  it('has_more=true（大文件部分加载）→ loadedBytes 记录窗口末位（partial 禁编辑依据）', async () => {
    getMock.mockResolvedValueOnce(
      fileContentResp('a'.repeat(FILE_OPEN_CHUNK_BYTES), { hasMore: true, loaded: FILE_OPEN_CHUNK_BYTES }) as never,
    )

    await openFile('big.log', { containerTaskId: 'task-42' })

    expect(getFileEditorData('file-local-big.log')?.loadedBytes).toBe(FILE_OPEN_CHUNK_BYTES)
  })

  it('同一文件重复打开去重：激活现有 Tab，不重复请求', async () => {
    getMock.mockResolvedValueOnce(fileContentResp('a') as never)
    await openFile('docs/a.txt', { containerTaskId: 'task-1' })
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(1)

    getMock.mockClear()
    await openFile('docs/a.txt', { containerTaskId: 'task-1' })

    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(1)
    expect(getMock).not.toHaveBeenCalled()
    // 激活表达在 workspaceTabs[].isActive + visitedTabIds（懒挂载渲染来源）
    const { workspaceTabs, visitedTabIds } = useLayoutModeStore.getState()
    expect(workspaceTabs.find((t) => t.id === 'file-local-docs_a.txt')?.isActive).toBe(true)
    expect(visitedTabIds).toContain('file-local-docs_a.txt')
  })

  it('任务工作空间未命中（success=false）时回退 _local 重试（两侧同带分页参数）', async () => {
    getMock
      .mockResolvedValueOnce({ data: { success: false, message: 'not found' } } as never)
      .mockResolvedValueOnce(fileContentResp('root content') as never)

    await openFile('config/app.yaml', { containerTaskId: 'task-x' })

    expect(getMock).toHaveBeenCalledTimes(2)
    expect(getMock).toHaveBeenNthCalledWith(1, CONTENT_URL('task-x'), {
      params: { path: 'config/app.yaml', offset: 0, limit: FILE_OPEN_CHUNK_BYTES },
    })
    expect(getMock).toHaveBeenNthCalledWith(2, CONTENT_URL('_local'), {
      params: { path: 'config/app.yaml', offset: 0, limit: FILE_OPEN_CHUNK_BYTES },
    })

    const data = getFileEditorData('file-local-config_app.yaml')
    expect(data?.content).toBe('root content')
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(1)
  })

  it('未给 containerTaskId 时直接走 _local（项目根）', async () => {
    getMock.mockResolvedValueOnce(fileContentResp('local') as never)

    await openFile('README.md')

    expect(getMock).toHaveBeenCalledWith(CONTENT_URL('_local'), {
      params: { path: 'README.md', offset: 0, limit: FILE_OPEN_CHUNK_BYTES },
    })
    // tabId 仅替换路径分隔符（点号保留）
    expect(getFileEditorData('file-local-README.md')?.content).toBe('local')
  })

  it('双失败（任务空间与 _local 均 success=false）→ 如实返回 success=false + 后端 message（禁静默）', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    getMock
      .mockResolvedValueOnce({ data: { success: false, message: 'not found' } } as never)
      .mockResolvedValueOnce({ data: { success: false, message: 'root miss' } } as never)

    const res = await openFile('missing.txt', { containerTaskId: 'task-2' })

    expect(res.success).toBe(false)
    expect(res.message).toBe('root miss')
    expect(getMock).toHaveBeenCalledTimes(2)
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(0)
    expect(getFileEditorData('file-local-missing.txt')).toBeUndefined()
    warnSpy.mockRestore()
  })

  it('请求异常 → 如实返回 success=false + 错误信息（不建 Tab、不抛异常）', async () => {
    getMock.mockRejectedValueOnce(new Error('network down') as never)

    const res = await openFile('gone.txt', { containerTaskId: 'task-9' })

    expect(res.success).toBe(false)
    expect(res.message).toBe('network down')
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(0)
    expect(getFileEditorData('file-local-gone.txt')).toBeUndefined()
  })
})

describe('fileOpener.openFile — 插件查看器声明路由（ADR 2026-10-24）', () => {
  beforeEach(() => {
    getMock.mockReset()
    useLayoutModeStore.setState({ workspaceTabs: [], activeTabId: null, visitedTabIds: [] })
    useWidgetEventStore.setState({ latest: {} })
    loadFileViewers([
      {
        plugin_id: 'pptx_viewer',
        contributes: {
          file_viewers: [
            {
              id: 'pptx_viewer',
              label: 'PPT 预览',
              extensions: ['.pptx'],
              render: {
                widget: 'webview',
                props: { pluginId: 'pptx_viewer', htmlPath: '/page/pptx', widgetId: 'pptx-live' },
              },
            },
          ],
        },
      },
    ])
  })

  afterEach(() => {
    loadFileViewers([])
    vi.restoreAllMocks()
  })

  it('插件声明命中 → 建 __file_viewer__ webview 页签（声明 props + widgetId），不发内容请求', async () => {
    const res = await openFile('slides/演示.pptx', { containerTaskId: 'task-ppt' })

    expect(res.success).toBe(true)
    expect(getMock).not.toHaveBeenCalled()
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs).toHaveLength(1)
    expect(tabs[0].moduleId).toBe('__file_viewer__')
    expect(tabs[0].component).toBe('webview')
    expect(tabs[0].props).toMatchObject({
      pluginId: 'pptx_viewer',
      htmlPath: '/page/pptx',
      widgetId: 'pptx-live',
    })
    expect(tabs[0].title).toBe('演示.pptx')
  })

  it('file.open 下行事件注入文件坐标（立即推 + 延迟重推）', async () => {
    vi.useFakeTimers()
    try {
      await openFile('slides/deck.pptx', { containerTaskId: 'task-ppt' })
      expect(useWidgetEventStore.getState().latest['pptx-live']).toMatchObject({
        widget_id: 'pptx-live',
        event: 'file.open',
        data: { path: 'slides/deck.pptx', container_task_id: 'task-ppt' },
      })
      vi.advanceTimersByTime(1600)
      // 延迟重推幂等（同坐标第二发，覆盖页面加载窗口）
      expect(useWidgetEventStore.getState().latest['pptx-live'].data).toMatchObject({
        path: 'slides/deck.pptx',
      })
    } finally {
      vi.useRealTimers()
    }
  })

  it('同文件重复打开去重：激活现有页签，不重复建签', async () => {
    await openFile('slides/deck.pptx')
    await openFile('slides/deck.pptx')
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(1)
  })

  it('未命中插件声明（含 .pptx 之外）→ 走内置文本链路', async () => {
    getMock.mockResolvedValueOnce(fileContentResp('notes') as never)
    const res = await openFile('slides/notes.txt')

    expect(res.success).toBe(true)
    expect(getMock).toHaveBeenCalledTimes(1)
    expect(useLayoutModeStore.getState().workspaceTabs[0].moduleId).toBe('__file_editor__')
  })
})

describe('fileOpener.loadMoreFileContent（按需加载分页推进）', () => {
  beforeEach(() => {
    getMock.mockReset()
    useLayoutModeStore.setState({ workspaceTabs: [], activeTabId: null, visitedTabIds: [] })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  async function openPartialFile(): Promise<string> {
    getMock.mockResolvedValueOnce(
      fileContentResp('head-', { hasMore: true, loaded: 5 }) as never,
    )
    await openFile('big.log', { containerTaskId: 'task-7' })
    getMock.mockClear()
    return 'file-local-big.log'
  }

  it('加载更多：自 loadedBytes 续拉下一窗口并追加内容', async () => {
    const tabId = await openPartialFile()
    getMock.mockResolvedValueOnce(
      fileContentResp('tail', { hasMore: false, loaded: 9 }) as never,
    )

    const hasMore = await loadMoreFileContent(tabId)

    expect(hasMore).toBe(false)
    // 续拉从已加载字节位开始、带 limit 窗口
    expect(getMock).toHaveBeenCalledWith(CONTENT_URL('task-7'), {
      params: { path: 'big.log', offset: 5, limit: FILE_OPEN_CHUNK_BYTES },
    })
    const data = getFileEditorData(tabId)
    expect(data?.content).toBe('head-tail')
    // 读完全量 → loadedBytes 置回 undefined（全量标记，恢复可编辑）
    expect(data?.loadedBytes).toBeUndefined()
  })

  it('加载全部：不带 limit（自 offset 读到文件尾）', async () => {
    const tabId = await openPartialFile()
    getMock.mockResolvedValueOnce(
      fileContentResp('all-the-rest', { hasMore: false, loaded: 18 }) as never,
    )

    await loadMoreFileContent(tabId, { loadAll: true })

    expect(getMock).toHaveBeenCalledWith(CONTENT_URL('task-7'), {
      params: { path: 'big.log', offset: 5 },
    })
    expect(getFileEditorData(tabId)?.content).toBe('head-all-the-rest')
  })

  it('仍有未加载内容 → 返回 true，loadedBytes 推进', async () => {
    const tabId = await openPartialFile()
    getMock.mockResolvedValueOnce(
      fileContentResp('mid-', { hasMore: true, loaded: 9 }) as never,
    )

    const hasMore = await loadMoreFileContent(tabId)

    expect(hasMore).toBe(true)
    expect(getFileEditorData(tabId)?.loadedBytes).toBe(9)
  })
})

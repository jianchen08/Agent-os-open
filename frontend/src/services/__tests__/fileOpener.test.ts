/**
 * fileOpener（工具卡片"打开文件"链路）单元测试
 *
 * 工具卡片（read 卡/写卡 open_file action）点击后经全局回调进入 openFile：
 * - 按 containerTaskId（缺省 _local）拉取文件内容 → 注册编辑器数据 + 建工作区 Tab
 * - 已打开的文件去重（激活现有 Tab，不重复请求）
 * - 任务工作空间未命中时回退 _local 重试
 * - 双失败静默（不抛异常、不建 Tab）
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { openFile } from '@/services/fileOpener'
import { apiClient } from '@/services/api/client'
import { WORKSPACE_SERVICE_ENDPOINTS } from '@/services/api/endpoints.generated'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { getFileEditorData } from '@/stores/fileEditorRegistry'

vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn() },
}))

const getMock = vi.mocked(apiClient.get)

function fileContentResp(content: string, success = true) {
  return { data: { success, content, size: content.length } }
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

  it('点击后拉取内容并建工作区 Tab：注册编辑器数据 + Tab 激活 + 标题为文件名', async () => {
    getMock.mockResolvedValueOnce(fileContentResp('print(1)') as never)

    const res = await openFile('src/main.py', { containerTaskId: 'task-42' })

    expect(res.success).toBe(true)
    // 请求打到任务容器工作空间
    expect(getMock).toHaveBeenCalledWith(CONTENT_URL('task-42'), { params: { path: 'src/main.py' } })

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

  it('任务工作空间未命中（success=false）时回退 _local 重试', async () => {
    getMock
      .mockResolvedValueOnce({ data: { success: false, message: 'not found' } } as never)
      .mockResolvedValueOnce(fileContentResp('root content') as never)

    await openFile('config/app.yaml', { containerTaskId: 'task-x' })

    expect(getMock).toHaveBeenCalledTimes(2)
    expect(getMock).toHaveBeenNthCalledWith(1, CONTENT_URL('task-x'), { params: { path: 'config/app.yaml' } })
    expect(getMock).toHaveBeenNthCalledWith(2, CONTENT_URL('_local'), { params: { path: 'config/app.yaml' } })

    const data = getFileEditorData('file-local-config_app.yaml')
    expect(data?.content).toBe('root content')
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(1)
  })

  it('未给 containerTaskId 时直接走 _local（项目根）', async () => {
    getMock.mockResolvedValueOnce(fileContentResp('local') as never)

    await openFile('README.md')

    expect(getMock).toHaveBeenCalledWith(CONTENT_URL('_local'), { params: { path: 'README.md' } })
    // tabId 仅替换路径分隔符（点号保留）
    expect(getFileEditorData('file-local-README.md')?.content).toBe('local')
  })

  it('请求异常时静默：不建 Tab、不抛异常', async () => {
    getMock.mockRejectedValueOnce(new Error('network down') as never)

    await expect(openFile('gone.txt', { containerTaskId: 'task-9' })).resolves.not.toThrow()

    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(0)
    expect(getFileEditorData('file-local-gone.txt')).toBeUndefined()
  })
})

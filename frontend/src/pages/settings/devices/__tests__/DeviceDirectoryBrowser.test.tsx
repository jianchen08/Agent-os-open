/** @feature FP-0.2.多设备目录树 设备中心目录浏览 | @ci: frontend-test */
/** @feature 设备中心多设备目录树浏览 | @ci frontend-test */
/**
 * DeviceDirectoryBrowser（设备页目录浏览区）契约：
 * - 折叠默认零请求；「浏览设备目录」展开后以 fsbrowse:// 索引拉根层
 * - 点文件 → open-file 端点（宿主默认程序打开）+ 成功提示；失败 toast 不静默
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
const mockGet = vi.fn()
const mockPost = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: { get: (...args: unknown[]) => mockGet(...args), post: (...args: unknown[]) => mockPost(...args) },
}))
const toastMock = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }))
vi.mock('sonner', () => ({ toast: toastMock }))
vi.mock('@/services/api/tasks', () => ({
  pauseTask: vi.fn(),
  resumeTask: vi.fn(),
}))
vi.mock('@/services/schema/parser', () => ({
  parseDataSourceRef: (ref: string) => ({ endpoint: ref, params: {} }),
  resolveDataSource: (ref: { endpoint: string }) => ({ endpoint: ref.endpoint, params: {} }),
}))
vi.mock('@/stores/layoutModeStore', () => ({
  useLayoutModeStore: { getState: () => ({ workspaceTabs: [], setActiveTab: vi.fn(), addWorkspaceTab: vi.fn() }), setState: vi.fn() },
}))
vi.mock('@/components/schema/widgets/CreateTaskFormModal', () => ({
  CreateTaskFormModal: () => null,
}))
vi.mock('@/components/schema/widgets/FileTreeContextMenu', () => ({
  FileTreeContextMenu: () => null,
}))
import { DeviceDirectoryBrowser } from '../DeviceDirectoryBrowser'

describe('DeviceDirectoryBrowser — 设备中心目录树', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockGet.mockResolvedValue({
      data: {
        entries: [
          { name: 'D:\\ws', type: 'directory', path: 'D:\\ws' },
          { name: 'readme.md', type: 'file', path: 'D:\\readme.md' },
        ],
        truncated: false,
      },
    })
    mockPost.mockResolvedValue({ data: { success: true, message: '已打开文件' } })
  })

  it('折叠默认零请求；展开后拉 fsbrowse 根层', async () => {
    render(<DeviceDirectoryBrowser />)
    expect(mockGet).not.toHaveBeenCalled()

    fireEvent.click(screen.getByTestId('device-tree-toggle'))
    expect(await screen.findByText('D:\\ws')).toBeInTheDocument()
    expect(mockGet).toHaveBeenCalledWith(
      'fsbrowse://roots',
      expect.objectContaining({ params: expect.anything() }),
    )
  })

  it('点文件调 open-file 端点并 toast 成功', async () => {
    render(<DeviceDirectoryBrowser />)
    fireEvent.click(screen.getByTestId('device-tree-toggle'))
    expect(await screen.findByText('readme.md')).toBeInTheDocument()

    fireEvent.click(screen.getByText('readme.md'))
    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith(
        '/ext/workspace_service/workspaces/open-file',
        { file_path: 'D:\\readme.md' },
      ),
    )
    await waitFor(() => expect(toastMock.success).toHaveBeenCalled())
  })

  it('open-file 失败 toast 报错不静默', async () => {
    mockPost.mockRejectedValue(new Error('host open failed'))
    render(<DeviceDirectoryBrowser />)
    fireEvent.click(screen.getByTestId('device-tree-toggle'))
    expect(await screen.findByText('readme.md')).toBeInTheDocument()

    fireEvent.click(screen.getByText('readme.md'))
    await waitFor(() => expect(toastMock.error).toHaveBeenCalled())
  })
})

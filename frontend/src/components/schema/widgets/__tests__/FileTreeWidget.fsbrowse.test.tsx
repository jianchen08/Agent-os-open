/** @feature FP-0.2.多设备目录树 fsbrowse 懒加载树数据源 | @ci: frontend-test */
/** @feature 多端目录树 fsbrowse:// 数据源 + 目录点击回调 | @ci frontend-test */
/**
 * FileTreeWidget 消费 fsbrowse:// 协议（多端目录树索引）的复用契约：
 * - 懒加载行为与 workspace:// 完全同构（根层单层 → 按 path 拉一层 → 已拉取不重拉）
 * - 目录/链接行点击触发 onDirectoryClick 回调（选定/追踪当前目录的接缝），
 *   文件行不走该回调
 * - 根层业务错误信封（workspace_status）→ 诚实错误态（与 workspace 同契约）
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
const mockGet = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: { get: (...args: unknown[]) => mockGet(...args) },
}))
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
vi.mock('../CreateTaskFormModal', () => ({
  CreateTaskFormModal: () => null,
}))
vi.mock('../FileTreeContextMenu', () => ({
  FileTreeContextMenu: () => null,
}))
import { FileTreeWidget } from '../FileTreeWidget'

const FS_ENDPOINT = 'fsbrowse://roots'

describe('FileTreeWidget fsbrowse 多端目录树', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('根层=盘符；展开目录按绝对 path 拉一层，已拉取层不重复请求', async () => {
    mockGet
      .mockResolvedValueOnce({
        data: {
          entries: [
            { name: 'C:\\', type: 'directory', path: 'C:\\' },
            { name: 'VM-0-16-ubuntu', type: 'link', path: 'C:\\VM-0-16-ubuntu' },
          ],
          truncated: false,
        },
      })
      .mockResolvedValueOnce({
        data: {
          entries: [{ name: 'projects', type: 'directory', path: 'C:\\projects' }],
          truncated: false,
        },
      })

    render(<FileTreeWidget dataSource={FS_ENDPOINT} nodeTitleField="name" expandLevel={0} />)

    expect(await screen.findByText('C:\\')).toBeInTheDocument()
    expect(screen.getByText('VM-0-16-ubuntu')).toBeInTheDocument()
    expect(screen.queryByText('projects')).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('C:\\'))
    // 满套件并行下fetch+渲染可超默认1s：显式放宽等待窗（性质断言不变）
    await waitFor(() => expect(screen.getByText('projects')).toBeInTheDocument(), { timeout: 5000 })
    expect(mockGet).toHaveBeenNthCalledWith(2, FS_ENDPOINT, { params: { path: 'C:\\' } })

    // 收起再展开不重拉（每目录至多一次请求的性质断言）
    fireEvent.click(screen.getByText('C:\\'))
    fireEvent.click(screen.getByText('C:\\'))
    await waitFor(() => expect(screen.getByText('projects')).toBeInTheDocument())
    expect(mockGet).toHaveBeenCalledTimes(2)
  })

  it('目录行点击触发 onDirectoryClick（携带节点）；文件行不触发', async () => {
    mockGet.mockResolvedValueOnce({
      data: {
        entries: [
          { name: 'dirA', type: 'directory', path: 'C:\\dirA' },
          { name: 'f.txt', type: 'file', path: 'C:\\f.txt' },
        ],
        truncated: false,
      },
    })
    const onDirectoryClick = vi.fn()
    render(
      <FileTreeWidget
        dataSource={FS_ENDPOINT}
        nodeTitleField="name"
        expandLevel={0}
        onDirectoryClick={onDirectoryClick}
      />,
    )

    expect(await screen.findByText('dirA')).toBeInTheDocument()
    fireEvent.click(screen.getByText('dirA'))
    await waitFor(() =>
      expect(onDirectoryClick).toHaveBeenCalledWith(
        expect.objectContaining({ path: 'C:\\dirA', type: 'directory' }),
      ),
    )

    onDirectoryClick.mockClear()
    fireEvent.click(screen.getByText('f.txt'))
    expect(onDirectoryClick).not.toHaveBeenCalled()
  })

  it('根层业务错误信封（workspace_status）渲染诚实错误态而非伪装空树', async () => {
    mockGet.mockResolvedValueOnce({
      data: { entries: [], truncated: false, workspace_status: 'fs_error', error: '目录不存在: X:\\' },
    })

    render(<FileTreeWidget dataSource={FS_ENDPOINT} nodeTitleField="name" expandLevel={0} />)

    expect(await screen.findByTestId('file-tree-error')).toBeInTheDocument()
    expect(screen.getByText(/目录不存在/)).toBeInTheDocument()
  })
})

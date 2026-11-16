/** @feature FP-0.2.四/五 懒加载文件树（workspace 单层列举契约） @ci frontend-test */
/**
 * FileTreeWidget workspace:// 懒加载契约（对齐后端 file-tree 单层语义）：
 * - 根层只渲染一层；目录/链接节点可展开，展开时按 path 发一次单层请求
 * - 已拉取层（含空层）不重复请求（性质：每目录至多一次请求）
 * - 根层截断标记渲染截断提示；子层失败在该目录节点如实呈现错误
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

const ROOT = [
  { name: 'a.txt', type: 'file', path: 'a.txt' },
  { name: 'sub', type: 'directory', path: 'sub' },
  { name: 'node_modules', type: 'link', path: 'node_modules' },
]

describe('FileTreeWidget workspace 懒加载', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('根层单层渲染不递归；展开目录按 path 拉一层，已拉取层不重复请求', async () => {
    mockGet
      .mockResolvedValueOnce({ data: { entries: ROOT, truncated: false } })
      .mockResolvedValueOnce({
        data: { entries: [{ name: 'b.md', type: 'file', path: 'sub/b.md' }], truncated: false },
      })

    render(<FileTreeWidget dataSource="workspace://t1" nodeTitleField="name" expandLevel={0} />)

    expect(await screen.findByText('a.txt')).toBeInTheDocument()
    expect(screen.getByText('sub')).toBeInTheDocument()
    expect(screen.getByText('node_modules')).toBeInTheDocument()
    // 根层不递归：子层内容不出现
    expect(screen.queryByText('b.md')).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('sub'))
    await waitFor(() => expect(screen.getByText('b.md')).toBeInTheDocument())
    expect(mockGet).toHaveBeenNthCalledWith(2, 'workspace://t1', { params: { path: 'sub' } })

    // 收起再展开：已拉取层不重复请求（每目录至多一次请求的性质断言）
    fireEvent.click(screen.getByText('sub'))
    fireEvent.click(screen.getByText('sub'))
    await waitFor(() => expect(screen.getByText('b.md')).toBeInTheDocument())
    expect(mockGet).toHaveBeenCalledTimes(2)
  })

  it('链接节点可展开并按 path 列目标第一层', async () => {
    mockGet
      .mockResolvedValueOnce({
        data: { entries: [ROOT[2]], truncated: false },
      })
      .mockResolvedValueOnce({
        data: {
          entries: [{ name: 'react', type: 'directory', path: 'node_modules/react' }],
          truncated: false,
        },
      })

    render(<FileTreeWidget dataSource="workspace://t1" nodeTitleField="name" expandLevel={0} />)

    expect(await screen.findByText('node_modules')).toBeInTheDocument()
    fireEvent.click(screen.getByText('node_modules'))
    await waitFor(() => expect(screen.getByText('react')).toBeInTheDocument())
    expect(mockGet).toHaveBeenNthCalledWith(2, 'workspace://t1', { params: { path: 'node_modules' } })
  })

  it('根层截断渲染提示；子层失败在该目录节点呈现错误而非伪装空目录', async () => {
    mockGet
      .mockResolvedValueOnce({ data: { entries: ROOT, truncated: true } })
      .mockRejectedValueOnce(new Error('boom layer'))

    render(<FileTreeWidget dataSource="workspace://t1" nodeTitleField="name" expandLevel={0} />)

    expect(await screen.findByText('目录过大，仅显示部分条目')).toBeInTheDocument()

    fireEvent.click(screen.getByText('sub'))
    await waitFor(() => expect(screen.getByText(/boom layer/)).toBeInTheDocument())
  })
})

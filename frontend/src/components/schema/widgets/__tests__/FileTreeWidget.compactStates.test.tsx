/** @feature FP-0.2.四/五 fallback-audit FE项 FileTreeWidget 紧凑态 @vision V6 可即用 @ci frontend-test */
/**
 * FileTreeWidget `compactStates` 契约（嵌在辅助位时三种状态各占一行）。
 *
 * 现场面板产物区把文件树当辅助区块：加载中/无内容/失败都不该占版面，
 * 有内容才按内容展开。本套件固定这两面：
 * - compact → 加载/空/失败各一行（弱化样式，不出现 p-8 居中大态）
 * - compact 且**有数据** → 仍渲染完整树节点（不得被折叠成一行）
 * - 默认（不带 compactStates）→ 保持原居中态，供整页树场景（回归面）
 */
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

const mockGet = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: { get: (...args: unknown[]) => mockGet(...args) },
}))
vi.mock('@/services/api/tasks', () => ({ pauseTask: vi.fn(), resumeTask: vi.fn() }))
vi.mock('@/services/schema/parser', () => ({
  parseDataSourceRef: (ref: string) => ({ endpoint: ref, params: {} }),
  resolveDataSource: (ref: { endpoint: string }) => ({ endpoint: ref.endpoint, params: {} }),
}))
vi.mock('@/stores/layoutModeStore', () => ({
  useLayoutModeStore: {
    getState: () => ({ workspaceTabs: [], setActiveTab: vi.fn(), addWorkspaceTab: vi.fn() }),
    setState: vi.fn(),
  },
}))
vi.mock('../CreateTaskFormModal', () => ({ CreateTaskFormModal: () => null }))
vi.mock('../FileTreeContextMenu', () => ({ FileTreeContextMenu: () => null }))

import { FileTreeWidget } from '../FileTreeWidget'

const TREE = [{ id: 'n1', title: '任务A', status: 'running', children: [] }]

/** 一行的判据：容器高度受限（不再是 p-8 居中块），且带虚线弱化边框 */
function expectSingleRow(testId: string): void {
  const row = screen.getByTestId(testId)
  expect(row.className).toContain('border-dashed')
  expect(row.className).not.toContain('p-8')
}

describe('FileTreeWidget compactStates：辅助位三态各一行', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('compact + 加载中 → 一行「加载中…」（不是居中大转圈）', async () => {
    mockGet.mockReturnValueOnce(new Promise(() => {}))
    render(<FileTreeWidget dataSource="workspace://t1" showStatus={false} compactStates />)
    await waitFor(() => expect(screen.getByTestId('file-tree-loading')).toBeInTheDocument())
    expectSingleRow('file-tree-loading')
    expect(screen.getByText('加载中…')).toBeInTheDocument()
  })

  it('compact + 无内容 → 一行「无文件」', async () => {
    mockGet.mockResolvedValueOnce({ data: { entries: [], items: [] } })
    render(<FileTreeWidget dataSource="workspace://t2" showStatus={false} compactStates />)
    await waitFor(() => expect(screen.getByTestId('file-tree-empty')).toBeInTheDocument())
    expectSingleRow('file-tree-empty')
    expect(screen.getByText('无文件')).toBeInTheDocument()
  })

  it('compact + 有数据 → 正常渲染树节点，不得折叠成一行（区分度输入：单个目录项）', async () => {
    mockGet.mockResolvedValueOnce({ data: { children: TREE } })
    render(<FileTreeWidget dataSource="task://tree-a" showStatus={false} compactStates />)
    await waitFor(() => expect(screen.getByText('任务A')).toBeInTheDocument())
    // 有内容时三种折叠态一律不出现
    expect(screen.queryByTestId('file-tree-empty')).not.toBeInTheDocument()
    expect(screen.queryByTestId('file-tree-loading')).not.toBeInTheDocument()
    expect(screen.queryByTestId('file-tree-error')).not.toBeInTheDocument()
  })

  it('compact + 有数据（两组不同名输入）→ 各自渲染，证明未硬编码单一样本', async () => {
    mockGet.mockResolvedValueOnce({
      data: { children: [{ id: 'x', title: 'alpha.txt', status: 'running', children: [] }] },
    })
    const first = render(<FileTreeWidget dataSource="task://tree-b" showStatus={false} compactStates />)
    await waitFor(() => expect(screen.getByText('alpha.txt')).toBeInTheDocument())
    first.unmount()

    mockGet.mockResolvedValueOnce({
      data: { children: [{ id: 'y', title: 'beta.md', status: 'running', children: [] }] },
    })
    render(<FileTreeWidget dataSource="task://tree-c" showStatus={false} compactStates />)
    await waitFor(() => expect(screen.getByText('beta.md')).toBeInTheDocument())
  })

  it('默认（无 compactStates）→ 空态仍是居中大态（整页树场景回归面）', async () => {
    mockGet.mockResolvedValueOnce({ data: { entries: [], items: [] } })
    render(<FileTreeWidget dataSource="workspace://t3" showStatus={false} />)
    await waitFor(() => expect(screen.getByText('暂无树形数据')).toBeInTheDocument())
    expect(screen.queryByTestId('file-tree-empty')).not.toBeInTheDocument()
  })
})

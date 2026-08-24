/** @feature FP-0.2.四/五 fallback-audit FE项 FileTreeWidget 错误态 @ci frontend-test */
/**
 * FileTreeWidget 远程加载失败不得伪装成空树：
 * - 失败 → 显式错误态 + 重试按钮（对齐 FormWidget setDsError 先例）
 * - 重试成功 → 恢复树渲染
 *
 * 宽域回退（会话空 → 全局树）为产品决策，源码内已有注释与 console.debug，
 * 其行为由本测试第二例一并覆盖（空会话结果触发第二次宽域请求）。
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

const TREE = [
  { id: 'n1', name: '任务A', status: 'running', children: [] },
]

describe('FileTreeWidget 远程加载失败错误态（FE8）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('加载失败渲染错误态 + 重试；重试成功恢复树', async () => {
    mockGet.mockRejectedValueOnce(new Error('api down'))
      .mockResolvedValueOnce({ data: { children: TREE } })

    render(<FileTreeWidget dataSource="task://tree" />)
    expect(await screen.findByTestId('file-tree-error')).toBeInTheDocument()
    expect(screen.getByText('api down')).toBeInTheDocument()
    // 失败不得伪装成"暂无树形数据"空态
    expect(screen.queryByText('暂无树形数据')).not.toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('重试加载任务树'))
    await waitFor(() => {
      expect(screen.queryByTestId('file-tree-error')).not.toBeInTheDocument()
    })
    expect(mockGet).toHaveBeenCalledTimes(2)
  })

  it('会话域空结果回退宽域请求（产品决策）并渲染全局树', async () => {
    const debugSpy = vi.spyOn(console, 'debug').mockImplementation(() => {})
    try {
      mockGet.mockResolvedValueOnce({ data: { children: [] } })
        .mockResolvedValueOnce({ data: { children: TREE } })

      render(<FileTreeWidget dataSource="task://tree" sessionId="sess-1" nodeTitleField="name" />)
      await waitFor(() => {
        expect(screen.getByText('任务A')).toBeInTheDocument()
      })
      // 宽域回退发生时 debug 留痕一次
      expect(debugSpy).toHaveBeenCalledWith(
        expect.stringContaining('回退全局树'),
        'sess-1',
      )
      // 第二次请求不带 session_id（宽域）
      expect(mockGet).toHaveBeenNthCalledWith(2, 'task://tree', { params: {} })
    } finally {
      debugSpy.mockRestore()
    }
  })

  it('工作区目录缺失（200 + workspace_status）渲染错误态而非空树（批次A）', async () => {
    mockGet.mockResolvedValueOnce({
      data: {
        tree: [],
        workspace_status: 'dir_missing',
        error: '工作区目录不存在: D:/ws/gone',
      },
    })

    render(<FileTreeWidget dataSource="workspace://taskX" nodeTitleField="name" />)
    expect(await screen.findByTestId('file-tree-error')).toBeInTheDocument()
    expect(screen.getByText('工作区目录不存在: D:/ws/gone')).toBeInTheDocument()
    // 后端声明业务错误 → 不触发会话宽域回退、不伪装空态
    expect(mockGet).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('暂无树形数据')).not.toBeInTheDocument()
  })

  it('无工作区坐标（no_workspace）同样进错误态', async () => {
    mockGet.mockResolvedValueOnce({
      data: {
        tree: [],
        workspace_status: 'no_workspace',
        error: 'taskX 无工作区坐标（任务未分配工作区，或为主会话管道）',
      },
    })

    render(<FileTreeWidget dataSource="workspace://taskX" nodeTitleField="name" />)
    expect(await screen.findByTestId('file-tree-error')).toBeInTheDocument()
    expect(screen.getByText(/无工作区坐标/)).toBeInTheDocument()
  })
})

/** @feature FP-T12 前端适配 | @ci: frontend-test */
/** @ci: frontend-test */
/**
 * 工作区文件树（任务面板「打开工作空间」tab）主链补测。
 *
 * 与既有 FileTreeWidget 测试的差异：不 mock schema parser（workspace:// 走
 * 真实解析 → /ext/workspace_service 端点），mock 响应使用后端 file-tree 端点
 * 的真实形状（顶层 tree 键 + name/type/path 字段），props 与 FiveSpaceLayout
 * component-based tab 渲染路径逐一对齐——回归 BUG-4：该场景树恒空。
 *
 * mock 仅限外部依赖（api client / tasks api / 宿主 store / jsdom 不可行宿主）。
 */
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
const mockGet = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: { get: (...args: unknown[]) => mockGet(...args) },
}))
vi.mock('@/services/api/tasks', () => ({
  pauseTask: vi.fn(),
  resumeTask: vi.fn(),
}))
const layoutState = vi.hoisted(() => ({
  workspaceTabs: [] as Array<{ id: string }>,
  setActiveTab: vi.fn(),
  addWorkspaceTab: vi.fn(),
}))
vi.mock('@/stores/layoutModeStore', () => ({
  useLayoutModeStore: { getState: () => layoutState, setState: vi.fn() },
}))
vi.mock('../CreateTaskFormModal', () => ({
  CreateTaskFormModal: () => null,
}))
vi.mock('../FileTreeContextMenu', () => ({
  FileTreeContextMenu: () => null,
}))
import { FileTreeWidget } from '../FileTreeWidget'

/** FiveSpaceLayout component-based tab 渲染 file_tree 时传入的 props（逐一对齐） */
const WORKSPACE_TAB_PROPS = {
  dataSource: 'workspace://0dda5106a229',
  sessionId: 'sess-under-test',
  showStatus: false,
  showProgress: false,
  showSearch: true,
  expandLevel: 0,
  nodeTitleField: 'name',
  nodeChildrenField: 'children',
} as const

/** 后端 GET /ext/workspace_service/workspaces/{id}/file-tree 的真实返回形状 */
const FILE_TREE_RESPONSE = {
  data: {
    tree: [{ name: 'eval_answer.txt', type: 'file', path: 'eval_answer.txt' }],
  },
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  layoutState.workspaceTabs = []
})

describe('工作区文件树（workspace:// tab 真实链路）', () => {
  it('file-tree 端点 tree 键形状 → 文件节点必须渲染', async () => {
    mockGet.mockResolvedValue(FILE_TREE_RESPONSE)

    render(<FileTreeWidget {...WORKSPACE_TAB_PROPS} />)

    expect(await screen.findByText('eval_answer.txt')).toBeInTheDocument()
  })

  it('请求命中真实解析端点并携带会话参数', async () => {
    mockGet.mockResolvedValue(FILE_TREE_RESPONSE)

    render(<FileTreeWidget {...WORKSPACE_TAB_PROPS} />)
    await screen.findByText('eval_answer.txt')

    expect(mockGet).toHaveBeenCalledWith(
      '/ext/workspace_service/workspaces/0dda5106a229/file-tree',
      expect.objectContaining({
        params: expect.objectContaining({ session_id: 'sess-under-test' }),
      }),
    )
  })

  it('多层目录树 → 根目录可见，子节点按 expandLevel 折叠', async () => {
    mockGet.mockResolvedValue({
      data: {
        tree: [
          {
            name: 'reports',
            type: 'directory',
            path: 'reports',
            children: [{ name: 'a.log', type: 'file', path: 'reports/a.log' }],
          },
        ],
      },
    })

    render(<FileTreeWidget {...WORKSPACE_TAB_PROPS} />)

    expect(await screen.findByText('reports')).toBeInTheDocument()
    expect(screen.queryByText('a.log')).not.toBeInTheDocument()
  })

  it('加载完成前显示加载态，不显示「未找到匹配的节点」', async () => {
    let resolveApi: ((v: typeof FILE_TREE_RESPONSE) => void) | undefined
    mockGet.mockImplementation(
      () => new Promise((r) => { resolveApi = r }),
    )

    render(<FileTreeWidget {...WORKSPACE_TAB_PROPS} />)

    // 请求仍 pending：显示显式加载态（可区分的 testid），绝不伪装成"无匹配"空态
    expect(await screen.findByTestId('file-tree-loading')).toBeInTheDocument()
    expect(screen.queryByText('未找到匹配的节点')).not.toBeInTheDocument()
    expect(screen.queryByText('暂无树形数据')).not.toBeInTheDocument()

    // 数据落定后必须渲染树（加载态退场）
    resolveApi!(FILE_TREE_RESPONSE)
    expect(await screen.findByText('eval_answer.txt')).toBeInTheDocument()
    expect(screen.queryByTestId('file-tree-loading')).not.toBeInTheDocument()
  })

  it('空结果会话回退（两串行请求均空）→ 空态而非「未找到匹配的节点」', async () => {
    mockGet.mockResolvedValue({ data: { tree: [] } })

    render(<FileTreeWidget {...WORKSPACE_TAB_PROPS} />)

    // sessionId 非空：首次请求空 → 宽域回退第二请求 → 仍空 → 暂无树形数据
    expect(await screen.findByText('暂无树形数据')).toBeInTheDocument()
    expect(screen.queryByText('未找到匹配的节点')).not.toBeInTheDocument()
    expect(mockGet).toHaveBeenCalledTimes(2)
  })

  it('HTTP 404 + 业务信封 → 显示后端 error 文本而非 axios 概况消息（BUG-24）', async () => {
    const backendError = '目标任务归属元数据缺失（task.submitted_by），拒绝访问；存量数据需回填归属后可达'
    mockGet.mockRejectedValue(
      Object.assign(new Error('Request failed with status code 404'), {
        response: { status: 404, data: { error: backendError } },
      }),
    )

    render(<FileTreeWidget {...WORKSPACE_TAB_PROPS} />)

    expect(await screen.findByTestId('file-tree-error')).toBeInTheDocument()
    expect(screen.getByText(backendError)).toBeInTheDocument()
    expect(screen.queryByText('Request failed with status code 404')).not.toBeInTheDocument()
  })

  it('HTTP 错误带 detail 形状（无 error 键）→ 显示 detail 文本', async () => {
    mockGet.mockRejectedValue(
      Object.assign(new Error('Request failed with status code 500'), {
        response: { status: 500, data: { detail: 'workspace service error: boom' } },
      }),
    )

    render(<FileTreeWidget {...WORKSPACE_TAB_PROPS} />)

    expect(await screen.findByTestId('file-tree-error')).toBeInTheDocument()
    expect(screen.getByText('workspace service error: boom')).toBeInTheDocument()
  })
})

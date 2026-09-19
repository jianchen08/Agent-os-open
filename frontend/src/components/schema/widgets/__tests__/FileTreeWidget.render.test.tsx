// @feature FP-T12 前端组件补测
/** @ci: frontend-test */
/**
 * FileTreeWidget 渲染主链补测：内联数据渲染 / 状态筛选 / 搜索过滤 /
 * 展开折叠 / 空态。
 *
 * mock 集合与 FileTreeWidget.errorState.test.tsx 同构（静态 data 路径不触网）。
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
import '../taskFileTreeActions' // 任务域绑定副作用（启停/状态词表经注册缝注入）

// 渲染主链用：全 running（默认状态筛选只保留活跃节点）
const TREE = [
  {
    id: 'task-1',
    title: '构建索引',
    status: 'running',
    children: [
      { id: 'task-1-1', title: '下载语料', status: 'running' },
    ],
  },
  { id: 'task-2', title: '训练模型', status: 'running' },
]

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
})

describe('内联数据渲染', () => {
  it('静态 data 直接渲染树；无数据 → 空态提示', () => {
    const { unmount } = render(<FileTreeWidget data={TREE} />)
    expect(screen.getByText('构建索引')).toBeInTheDocument()
    expect(screen.getByText('训练模型')).toBeInTheDocument()
    unmount()

    render(<FileTreeWidget data={[]} />)
    expect(screen.getByText('暂无树形数据')).toBeInTheDocument()
  })

  it('expandLevel 默认全展开；点击节点可折叠/再展开', async () => {
    render(<FileTreeWidget data={TREE} />)
    // 初始化 effect 按 expandLevel=-1 自动展开全部
    expect(await screen.findByText('下载语料')).toBeInTheDocument()

    // 点击折叠
    fireEvent.click(screen.getByText('构建索引'))
    expect(screen.queryByText('下载语料')).not.toBeInTheDocument()

    // 再点击展开
    fireEvent.click(screen.getByText('构建索引'))
    expect(screen.getByText('下载语料')).toBeInTheDocument()
  })

  it('叶子节点（path 字段）点击触发 onFileClick', () => {
    const onFileClick = vi.fn()
    const files = [
      { id: 'f1', title: 'main.py', status: 'running', path: 'src/main.py' },
    ]
    render(<FileTreeWidget data={files} onFileClick={onFileClick} />)

    fireEvent.click(screen.getByText('main.py'))

    expect(onFileClick).toHaveBeenCalledWith('src/main.py', 'main.py')
  })
})

describe('状态筛选器', () => {
  it('默认按活跃状态过滤：completed 节点隐藏，点「全部」恢复', () => {
    const mixed = [
      { id: 't1', title: '已完成任务', status: 'completed' },
      { id: 't2', title: '运行中任务', status: 'running' },
    ]
    render(<FileTreeWidget data={mixed} />)

    expect(screen.queryByText('已完成任务')).not.toBeInTheDocument()
    expect(screen.getByText('运行中任务')).toBeInTheDocument()

    fireEvent.click(screen.getByText('全部'))
    expect(screen.getByText('已完成任务')).toBeInTheDocument()
    expect(screen.getByText('运行中任务')).toBeInTheDocument()
  })

  it('筛选后无匹配节点 → 未找到提示', () => {
    render(<FileTreeWidget data={[{ id: 'a', title: '已完成任务', status: 'completed' }]} />)

    expect(screen.getByText('未找到匹配的节点')).toBeInTheDocument()
  })
})

describe('搜索框', () => {
  it('关键词过滤节点；无匹配显示提示', () => {
    render(<FileTreeWidget data={TREE} showSearch showStatusFilter={false} />)

    fireEvent.change(screen.getByPlaceholderText('搜索节点...'), {
      target: { value: '训练' },
    })

    expect(screen.getByText('训练模型')).toBeInTheDocument()
    expect(screen.queryByText('构建索引')).not.toBeInTheDocument()
  })

  it('清空关键词恢复全量', () => {
    render(<FileTreeWidget data={TREE} showSearch showStatusFilter={false} />)

    const input = screen.getByPlaceholderText('搜索节点...')
    fireEvent.change(input, { target: { value: '不存在词' } })
    expect(screen.getByText('未找到匹配的节点')).toBeInTheDocument()

    fireEvent.change(input, { target: { value: '' } })
    expect(screen.getByText('构建索引')).toBeInTheDocument()
    expect(screen.getByText('训练模型')).toBeInTheDocument()
  })
})

describe('远程加载成功路径', () => {
  it('远程数据加载渲染；加载中不闪烁空态', async () => {
    let resolveApi: ((v: { data: { children: typeof TREE } }) => void) | undefined
    mockGet.mockImplementation(
      () => new Promise((r) => { resolveApi = r }),
    )

    render(<FileTreeWidget dataSource="task://tree" sessionId="s1" />)
    // 首次加载防闪烁：pending 时不显示空态
    await waitFor(() => expect(mockGet).toHaveBeenCalled())
    resolveApi!({ data: { children: TREE } })

    expect(await screen.findByText('构建索引')).toBeInTheDocument()
  })

  it('远程 items 平铺形态作为树数据兜底', async () => {
    mockGet.mockResolvedValue({
      data: { items: [{ id: 'f1', title: '平铺项', status: 'running' }] },
    })

    render(<FileTreeWidget dataSource="task://tree" sessionId="s2" />)

    expect(await screen.findByText('平铺项')).toBeInTheDocument()
  })
})

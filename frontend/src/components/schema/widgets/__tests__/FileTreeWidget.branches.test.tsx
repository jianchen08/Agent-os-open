/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * FileTreeWidget 分支补测（与 FileTreeWidget.render.test.tsx / .errorState.test.tsx
 * 互补，已有场景不重复）：字段映射与数据形态 / 展开策略与 localStorage 持久化 /
 * 数据更新时的增量展开 / 排序 / 进度与元信息 / 操作按钮 / 级联启停开关 /
 * 右键菜单 / 状态图标配置 / 新建任务入口。
 *
 * mock 集合与既有 FileTreeWidget 测试同构：仅外部服务（api client / tasks api /
 * schema parser）与 jsdom 不可行的宿主（layoutModeStore / 模态与右键菜单用探针替代）。
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
const mockGet = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: { get: (...args: unknown[]) => mockGet(...args) },
}))
vi.mock('@/services/api/tasks', () => ({
  pauseTask: vi.fn().mockResolvedValue(undefined),
  resumeTask: vi.fn().mockResolvedValue(undefined),
}))
vi.mock('@/services/schema/parser', () => ({
  parseDataSourceRef: (ref: string) => ({ endpoint: ref, params: {} }),
  resolveDataSource: (ref: { endpoint: string }) => ({ endpoint: ref.endpoint, params: {} }),
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
  CreateTaskFormModal: (props: {
    isOpen: boolean
    sessionId: string
    onClose: () => void
  }): ReactElement | null =>
    props.isOpen ? (
      <div data-testid="create-task-modal-probe" data-session-id={props.sessionId}>
        <button data-testid="close-modal-probe" onClick={props.onClose}>
          close
        </button>
      </div>
    ) : null,
}))
vi.mock('../FileTreeContextMenu', () => ({
  FileTreeContextMenu: (props: {
    x: number
    y: number
    context: ContextMenuContext
    onClose: () => void
  }): ReactElement => (
    <div
      data-testid="context-menu-probe"
      data-x={String(props.x)}
      data-y={String(props.y)}
      data-container-task-id={props.context.containerTaskId}
      data-target-path={props.context.targetPath ?? ''}
      data-target-name={props.context.targetName ?? ''}
      data-is-directory={String(props.context.isDirectory)}
      data-parent-dir={props.context.parentDir}
    >
      <button data-testid="close-context-menu-probe" onClick={props.onClose}>
        close
      </button>
    </div>
  ),
}))
import { pauseTask, resumeTask } from '@/services/api/tasks'
import { FileTreeWidget } from '../FileTreeWidget'
import type { ContextMenuContext } from '../FileTreeContextMenu'
import type { ReactElement } from 'react'

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  layoutState.workspaceTabs = []
})

/** 三层树：根目录 → 子目录 → 叶子（展开层级用） */
const DEEP_TREE = [
  {
    id: 'r',
    title: '根目录',
    status: 'running',
    children: [
      {
        id: 'm',
        title: '子目录',
        status: 'running',
        children: [{ id: 'l', title: '叶子', status: 'running' }],
      },
    ],
  },
]

/** 按 textContent 出现顺序返回实际可见顺序（排序断言用） */
function visibleOrder(container: HTMLElement, titles: string[]): string[] {
  const text = container.textContent ?? ''
  return [...titles].sort((a, b) => text.indexOf(a) - text.indexOf(b))
}

describe('数据形态与字段映射', () => {
  it('自定义字段名（nodeTitleField/nodeChildrenField/nodeStatusField）渲染嵌套树', () => {
    render(
      <FileTreeWidget
        data={[
          {
            id: 'x',
            name: '根任务',
            state: 'running',
            sub: [{ id: 'x1', name: '子任务', state: 'running' }],
          },
        ]}
        nodeTitleField="name"
        nodeChildrenField="sub"
        nodeStatusField="state"
      />,
    )
    expect(screen.getByText('根任务')).toBeInTheDocument()
    expect(screen.getByText('子任务')).toBeInTheDocument()
  })

  it('props 嵌套形态：配置与数据都从嵌套对象提取', () => {
    render(
      <FileTreeWidget
        props={{
          showSearch: true,
          data: [{ id: 'n1', title: '嵌套节点', status: 'running' }],
        }}
      />,
    )
    expect(screen.getByPlaceholderText('搜索节点...')).toBeInTheDocument()
    expect(screen.getByText('嵌套节点')).toBeInTheDocument()
  })

  it('顶层 items 平铺形态兜底（无 data 字段时）', () => {
    render(<FileTreeWidget items={[{ id: 'i1', title: '平铺节点', status: 'running' }]} />)
    expect(screen.getByText('平铺节点')).toBeInTheDocument()
  })
})

describe('展开策略', () => {
  it('expandLevel=0 初始全折叠，点击展开', () => {
    render(<FileTreeWidget data={DEEP_TREE} expandLevel={0} />)
    expect(screen.queryByText('子目录')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('根目录'))
    expect(screen.getByText('子目录')).toBeInTheDocument()
  })

  it('expandLevel=1 仅展开到第一层，孙级保持折叠', () => {
    render(<FileTreeWidget data={DEEP_TREE} expandLevel={1} />)
    expect(screen.getByText('子目录')).toBeInTheDocument()
    expect(screen.queryByText('叶子')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('子目录'))
    expect(screen.getByText('叶子')).toBeInTheDocument()
  })

  it('点击行内折叠箭头仅切换展开态（行内末位按钮）', () => {
    const tree = [
      {
        id: 'root',
        title: '根目录',
        status: 'running',
        children: [{ id: 'c', title: '子文档', status: 'running' }],
      },
    ]
    render(<FileTreeWidget data={tree} />)
    expect(screen.getByText('子文档')).toBeInTheDocument()

    // 行根节点（含按钮区）：标题 span 上溯到 .group 行容器
    const row = screen.getByText('根目录').closest('.group')
    expect(row).not.toBeNull()
    const buttons = within(row as HTMLElement).getAllByRole('button')
    fireEvent.click(buttons[buttons.length - 1]!)
    expect(screen.queryByText('子文档')).not.toBeInTheDocument()

    fireEvent.click(buttons[buttons.length - 1]!)
    expect(screen.getByText('子文档')).toBeInTheDocument()
  })
})

describe('展开状态持久化', () => {
  it('折叠后写入 localStorage，重挂载恢复折叠态（优先于默认全展开）', () => {
    const tree = [
      {
        id: 'root',
        title: '项目根',
        status: 'running',
        children: [{ id: 'doc', title: 'README.md', status: 'running' }],
      },
    ]
    const first = render(<FileTreeWidget data={tree} title="持久化树" />)
    expect(screen.getByText('README.md')).toBeInTheDocument()

    fireEvent.click(screen.getByText('项目根'))
    expect(screen.queryByText('README.md')).not.toBeInTheDocument()
    expect(localStorage.getItem('tree_expanded_default_持久化树')).toBe('[]')

    first.unmount()
    render(<FileTreeWidget data={tree} title="持久化树" />)
    expect(screen.queryByText('README.md')).not.toBeInTheDocument()
  })

  it('预置的空展开记录优先于默认全展开（恢复用户折叠偏好）', () => {
    localStorage.setItem('tree_expanded_default_untitled', '[]')
    render(
      <FileTreeWidget
        data={[
          {
            id: 'root',
            title: '根目录',
            status: 'running',
            children: [{ id: 'c', title: '子文档', status: 'running' }],
          },
        ]}
      />,
    )
    expect(screen.queryByText('子文档')).not.toBeInTheDocument()
  })

  it('treeKey 变更（标题变化）时重置展开状态，旧 key 持久化不被覆盖', () => {
    const tree = [
      {
        id: 'root',
        title: '根目录',
        status: 'running',
        children: [{ id: 'c', title: '子文档', status: 'running' }],
      },
    ]
    const { rerender } = render(<FileTreeWidget data={tree} title="树一" />)
    expect(screen.getByText('子文档')).toBeInTheDocument()
    fireEvent.click(screen.getByText('根目录'))
    expect(screen.queryByText('子文档')).not.toBeInTheDocument()
    expect(localStorage.getItem('tree_expanded_default_树一')).toBe('[]')

    rerender(<FileTreeWidget data={tree} title="树二" />)
    // treeKey 变更即重置展开状态（等待新 key 的数据重载后再按默认策略展开）；
    // 旧 key 的用户偏好保留，新 key 在数据未变前不写入
    expect(screen.queryByText('子文档')).not.toBeInTheDocument()
    expect(localStorage.getItem('tree_expanded_default_树一')).toBe('[]')
    expect(localStorage.getItem('tree_expanded_default_树二')).toBeNull()
  })
})

describe('数据更新', () => {
  it('刷新后新增目录自动展开，已见节点的用户折叠态不被覆盖', () => {
    const v1 = [
      {
        id: 'a',
        title: '目录A',
        status: 'running',
        children: [{ id: 'a1', title: '旧文档', status: 'running' }],
      },
    ]
    const { rerender } = render(<FileTreeWidget data={v1} />)
    expect(screen.getByText('旧文档')).toBeInTheDocument()
    fireEvent.click(screen.getByText('目录A'))
    expect(screen.queryByText('旧文档')).not.toBeInTheDocument()

    const v2 = [
      {
        id: 'a',
        title: '目录A',
        status: 'running',
        children: [
          { id: 'a1', title: '旧文档', status: 'running' },
          { id: 'a2', title: '新文档', status: 'running' },
        ],
      },
      {
        id: 'b',
        title: '目录B',
        status: 'running',
        children: [{ id: 'b1', title: '新目录文档', status: 'running' }],
      },
    ]
    rerender(<FileTreeWidget data={v2} />)
    // 新目录 B 按默认策略自动展开
    expect(screen.getByText('新目录文档')).toBeInTheDocument()
    // 已见目录 A 的折叠态保留（新旧子文档都不出现）
    expect(screen.queryByText('旧文档')).not.toBeInTheDocument()
    expect(screen.queryByText('新文档')).not.toBeInTheDocument()
  })
})

describe('排序', () => {
  const FILES = [
    { id: 'f2', title: 'bbb.txt', status: 'running' },
    { id: 'f1', title: 'aaa.txt', status: 'running' },
  ]
  const MIXED = [
    ...FILES,
    { id: 'd', title: 'zzz目录', status: 'running', children: [] },
  ]
  const ALL_TITLES = ['zzz目录', 'aaa.txt', 'bbb.txt']

  it('第一档：名称 A→Z（文件升序）', () => {
    const { container } = render(<FileTreeWidget data={MIXED} showSearch />)
    fireEvent.click(screen.getByTitle('默认排序'))
    expect(screen.getByTitle('名称 A→Z')).toBeInTheDocument()
    expect(visibleOrder(container, ALL_TITLES)).toEqual(['zzz目录', 'aaa.txt', 'bbb.txt'])
  })

  it('第二档：名称 Z→A（文件降序）', () => {
    const { container } = render(<FileTreeWidget data={MIXED} showSearch />)
    fireEvent.click(screen.getByTitle('默认排序'))
    fireEvent.click(screen.getByTitle('名称 A→Z'))
    expect(screen.getByTitle('名称 Z→A')).toBeInTheDocument()
    expect(visibleOrder(container, ALL_TITLES)).toEqual(['zzz目录', 'bbb.txt', 'aaa.txt'])
  })

  it('第三档：文件夹优先（目录始终在文件前，文件名升序），第四档回到默认', () => {
    const { container } = render(<FileTreeWidget data={MIXED} showSearch />)
    fireEvent.click(screen.getByTitle('默认排序'))
    fireEvent.click(screen.getByTitle('名称 A→Z'))
    fireEvent.click(screen.getByTitle('名称 Z→A'))
    expect(screen.getByTitle('文件夹优先')).toBeInTheDocument()
    expect(visibleOrder(container, ALL_TITLES)).toEqual(['zzz目录', 'aaa.txt', 'bbb.txt'])

    fireEvent.click(screen.getByTitle('文件夹优先'))
    expect(screen.getByTitle('默认排序')).toBeInTheDocument()
  })
})

describe('节点元信息', () => {
  it.each([
    { progress: 50, shown: '50%' },
    { progress: 120, shown: '100%' },
    { progress: -5, shown: '0%' },
  ])('进度条按 $progress 渲染并夹取到 0-100（显示 $shown）', ({ progress, shown }) => {
    render(
      <FileTreeWidget
        showProgress
        data={[{ id: 'p', title: '进度节点', status: 'running', progress }]}
      />,
    )
    const label = screen.getByText(shown)
    expect(label).toBeInTheDocument()
    // 性质断言：进度宽度始终落在 [0, 100] 区间
    const fill = label.previousElementSibling?.firstElementChild
    expect(fill).not.toBeNull()
    const widthPct = Number.parseFloat((fill as HTMLElement).style.width)
    expect(widthPct).toBeGreaterThanOrEqual(0)
    expect(widthPct).toBeLessThanOrEqual(100)
  })

  it('无 progress 字段不渲染进度百分比', () => {
    render(
      <FileTreeWidget showProgress data={[{ id: 'p', title: '无进度节点', status: 'running' }]} />,
    )
    expect(screen.queryByText(/\d+%/)).not.toBeInTheDocument()
  })

  it.each([
    { priority: 'critical', label: '紧急', visible: true },
    { priority: 'high', label: '高', visible: true },
    { priority: 'low', label: '低', visible: true },
    { priority: 'normal', label: '普通', visible: false },
  ])('优先级 $priority 标签展示为「$label」（normal 不展示）', ({ priority, label, visible }) => {
    render(
      <FileTreeWidget
        showStatusFilter={false}
        data={[
          {
            id: 'pr',
            title: '优先级节点',
            status: 'running',
            priority,
            error: '元信息行随错误信息展示',
          },
        ]}
      />,
    )
    if (visible) {
      expect(screen.getByText(label)).toBeInTheDocument()
    } else {
      expect(screen.queryByText(label)).not.toBeInTheDocument()
    }
  })

  it('错误信息与创建时间渲染在元信息行；非法时间不显示', () => {
    const { unmount } = render(
      <FileTreeWidget
        showStatusFilter={false}
        data={[
          {
            id: 'e1',
            title: '出错节点',
            status: 'running',
            error: '磁盘已满',
            created_at: new Date(2026, 8, 13, 10, 30).toISOString(),
          },
        ]}
      />,
    )
    expect(screen.getByText('⚠ 磁盘已满')).toBeInTheDocument()
    expect(screen.getByText(/\d{2}-\d{2} \d{2}:\d{2}/)).toBeInTheDocument()
    unmount()

    render(
      <FileTreeWidget
        showStatusFilter={false}
        data={[{ id: 'e2', title: '坏时间节点', status: 'running', error: 'x', created_at: 'not-a-date' }]}
      />,
    )
    expect(screen.queryByText(/\d{2}-\d{2} \d{2}:\d{2}/)).not.toBeInTheDocument()
  })

  it('agent_name 徽标与子节点计数展示；空白 agent 名不渲染徽标', () => {
    const { unmount } = render(
      <FileTreeWidget
        data={[
          {
            id: 'g1',
            title: '带徽标节点',
            status: 'running',
            agent_name: 'coder',
            children: [
              { id: 'g1a', title: '子一', status: 'running' },
              { id: 'g1b', title: '子二', status: 'running' },
            ],
          },
        ]}
      />,
    )
    expect(screen.getByText('coder')).toBeInTheDocument()
    expect(screen.getByText('[2]')).toBeInTheDocument()
    unmount()

    const { container } = render(
      <FileTreeWidget
        data={[{ id: 'g2', title: '无徽标节点', status: 'running', agent_name: '   ' }]}
      />,
    )
    expect(screen.getByText('无徽标节点').textContent).toBe('无徽标节点')
    expect(container.textContent).not.toContain('coder')
  })
})

describe('操作按钮', () => {
  it.each([
    { desc: '普通任务', extra: {}, hasConversation: true },
    { desc: '容器任务（task_scope=container）', extra: { task_scope: 'container' }, hasConversation: false },
  ])('$desc：对话按钮可见性与回调', ({ extra, hasConversation }) => {
    const onNodeClick = vi.fn()
    render(
      <FileTreeWidget
        data={[{ id: 't1', title: '任务节点', status: 'running', pipeline_run_id: 'pr-1', ...extra }]}
        onNodeClick={onNodeClick}
      />,
    )
    if (hasConversation) {
      fireEvent.click(screen.getByTitle('打开对话'))
      expect(onNodeClick).toHaveBeenCalledTimes(1)
      expect(onNodeClick).toHaveBeenCalledWith(expect.objectContaining({ id: 't1' }))
    } else {
      expect(screen.queryByTitle('打开对话')).not.toBeInTheDocument()
    }
  })

  it('独立工作空间节点：点击注册工作区页签', () => {
    render(
      <FileTreeWidget
        data={[
          { id: 'w1', title: '工作区任务', status: 'running', ws_mode: 'isolated', ws_path: '/ws/w1' },
        ]}
      />,
    )
    fireEvent.click(screen.getByTitle('打开工作空间: /ws/w1'))
    expect(layoutState.addWorkspaceTab).toHaveBeenCalledWith(
      expect.objectContaining({
        id: 'ws-tree-w1',
        dataSource: 'workspace://w1',
        component: 'file_tree',
      }),
    )
    expect(layoutState.setActiveTab).not.toHaveBeenCalled()
  })

  it('工作区页签已存在时只激活，不重复注册', () => {
    layoutState.workspaceTabs = [{ id: 'ws-tree-w1' }]
    render(
      <FileTreeWidget
        data={[
          { id: 'w1', title: '工作区任务', status: 'running', ws_mode: 'isolated', ws_path: '/ws/w1' },
        ]}
      />,
    )
    fireEvent.click(screen.getByTitle('打开工作空间: /ws/w1'))
    expect(layoutState.setActiveTab).toHaveBeenCalledWith('ws-tree-w1')
    expect(layoutState.addWorkspaceTab).not.toHaveBeenCalled()
  })
})

describe('启用/禁用级联开关', () => {
  const CASCADE_TREE = (status: string) => [
    {
      id: 'p1',
      title: '根任务',
      status,
      children: [
        { id: 'c1', title: '子任务一', status },
        {
          id: 'c2',
          title: '子任务二',
          status,
          children: [{ id: 'c3', title: '孙任务', status }],
        },
      ],
    },
  ]

  it.each([
    {
      status: 'running',
      buttonTitle: '点击禁用（将级联禁用所有子任务）',
      api: 'pause' as const,
    },
    { status: 'completed', buttonTitle: '点击启用', api: 'resume' as const },
  ])('开关级联 $api：父节点切换联动全部后代', async ({ status, buttonTitle, api }) => {
    render(<FileTreeWidget showStatusFilter={false} data={CASCADE_TREE(status)} />)
    fireEvent.click(screen.getAllByTitle(buttonTitle)[0]!)
    const apiFn = api === 'pause' ? pauseTask : resumeTask
    const siblingFn = api === 'pause' ? resumeTask : pauseTask
    await waitFor(() => {
      for (const id of ['p1', 'c1', 'c2', 'c3']) {
        expect(apiFn).toHaveBeenCalledWith(id)
      }
    })
    expect(siblingFn).not.toHaveBeenCalled()
  })

  it('子节点开关级联仅作用于其后代，不动父级与兄弟子树', async () => {
    render(<FileTreeWidget showStatusFilter={false} data={CASCADE_TREE('running')} />)
    // 定位 c2 所在行的开关（行根 = 标题 span 上溯到 .group 行容器）
    const row = screen.getByText('子任务二').closest('.group')
    expect(row).not.toBeNull()
    fireEvent.click(within(row as HTMLElement).getByTitle('点击禁用（将级联禁用所有子任务）'))
    await waitFor(() => {
      expect(pauseTask).toHaveBeenCalledWith('c2')
      expect(pauseTask).toHaveBeenCalledWith('c3')
    })
    expect(pauseTask).not.toHaveBeenCalledWith('p1')
    expect(pauseTask).not.toHaveBeenCalledWith('c1')
  })

  it('级联切换 API 失败：仍触发刷新兜底（失败不静默卡死）', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      mockGet.mockResolvedValue({
        data: { children: CASCADE_TREE('running') },
      })
      render(<FileTreeWidget dataSource="task://tree" />)
      expect(await screen.findByText('根任务')).toBeInTheDocument()
      pauseTask.mockRejectedValueOnce(new Error('api down'))

      fireEvent.click(screen.getAllByTitle('点击禁用（将级联禁用所有子任务）')[0]!)
      // 失败后 triggerRefresh 兜底 → 500ms 防抖后重发加载请求
      await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2), { timeout: 3000 })
      // 重载成功后树照常渲染
      expect(await screen.findByText('根任务')).toBeInTheDocument()
    } finally {
      errorSpy.mockRestore()
    }
  })

  it('workspace:// 数据源默认不渲染启停开关', async () => {
    mockGet.mockResolvedValue({
      data: { children: [{ id: 'wf', title: '工作区文件', status: 'running' }] },
    })
    render(<FileTreeWidget dataSource="workspace://t9" />)
    expect(await screen.findByText('工作区文件')).toBeInTheDocument()
    expect(screen.queryByTitle('点击禁用（将级联禁用所有子任务）')).not.toBeInTheDocument()
    expect(screen.queryByTitle('点击启用')).not.toBeInTheDocument()
  })
})

describe('远程加载形态补充', () => {
  it('workspace_status 业务错误缺 error 字段时展示兜底文案', async () => {
    mockGet.mockResolvedValue({ data: { tree: [], workspace_status: 'no_workspace' } })
    render(<FileTreeWidget dataSource="workspace://taskX" />)
    expect(await screen.findByTestId('file-tree-error')).toBeInTheDocument()
    expect(screen.getByText('工作区不可用（无坐标或目录不存在）')).toBeInTheDocument()
  })

  it('数据源返回空且无 sessionId：不触发宽域回退，直接进空态', async () => {
    mockGet.mockResolvedValue({ data: { children: [] } })
    render(<FileTreeWidget dataSource="task://tree" />)
    expect(await screen.findByText('暂无树形数据')).toBeInTheDocument()
    expect(mockGet).toHaveBeenCalledTimes(1)
  })
})

describe('右键菜单', () => {
  interface ContextMenuCase {
    desc: string
    node: {
      id: string
      title: string
      status: string
      path: string
      children?: Array<{ id: string; title: string; status: string; path: string }>
    }
    isDirectory: 'true' | 'false'
    parentDir: string
  }

  it.each([
    {
      desc: '目录节点',
      node: {
        id: 'd1',
        title: 'docs',
        status: 'running',
        path: 'docs',
        children: [{ id: 'd1f', title: 'a.md', status: 'running', path: 'docs/a.md' }],
      },
      isDirectory: 'true',
      parentDir: 'docs',
    },
    {
      desc: '文件节点',
      node: { id: 'f1', title: 'readme.md', status: 'running', path: 'a/readme.md' },
      isDirectory: 'false',
      parentDir: 'a',
    },
  ] as Array<ContextMenuCase>)('$desc 右键：菜单携带路径上下文与坐标', async ({ node, isDirectory, parentDir }) => {
    mockGet.mockResolvedValue({ data: { children: [node] } })
    render(<FileTreeWidget dataSource="workspace://taskX" />)
    fireEvent.contextMenu(await screen.findByText(node.title), { clientX: 101, clientY: 202 })
    const probe = screen.getByTestId('context-menu-probe')
    expect(probe.getAttribute('data-container-task-id')).toBe('taskX')
    expect(probe.getAttribute('data-target-path')).toBe(node.path)
    expect(probe.getAttribute('data-is-directory')).toBe(isDirectory)
    expect(probe.getAttribute('data-parent-dir')).toBe(parentDir)
    expect(probe.getAttribute('data-x')).toBe('101')
    expect(probe.getAttribute('data-y')).toBe('202')

    // 关闭回调生效：菜单消失
    fireEvent.click(screen.getByTestId('close-context-menu-probe'))
    expect(screen.queryByTestId('context-menu-probe')).not.toBeInTheDocument()
  })

  it('空白区域右键（有 containerTaskId）：菜单为空白上下文', async () => {
    mockGet.mockResolvedValue({
      data: { children: [{ id: 'x', title: '过滤后无关节点', status: 'running' }] },
    })
    render(<FileTreeWidget dataSource="workspace://t8" showSearch />)
    expect(await screen.findByText('过滤后无关节点')).toBeInTheDocument()
    fireEvent.change(screen.getByPlaceholderText('搜索节点...'), {
      target: { value: '不存在的词' },
    })
    fireEvent.contextMenu(screen.getByText('未找到匹配的节点'))
    const probe = screen.getByTestId('context-menu-probe')
    expect(probe.getAttribute('data-target-path')).toBe('')
    expect(probe.getAttribute('data-container-task-id')).toBe('t8')
  })

  it('无数据源（containerTaskId 为空）时空白右键不弹菜单', () => {
    render(<FileTreeWidget showSearch data={[{ id: 'y', title: '本地节点', status: 'running' }]} />)
    fireEvent.change(screen.getByPlaceholderText('搜索节点...'), {
      target: { value: '不存在的词' },
    })
    fireEvent.contextMenu(screen.getByText('未找到匹配的节点'))
    expect(screen.queryByTestId('context-menu-probe')).not.toBeInTheDocument()
  })
})

describe('状态图标与配置', () => {
  it('未知状态回退默认图标，标签展示原值', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    render(
      <FileTreeWidget
        showStatusFilter={false}
        data={[{ id: 'u1', title: '神秘节点', status: 'mystery' }]}
      />,
    )
    expect(screen.getByText('mystery')).toBeInTheDocument()
    warnSpy.mockRestore()
  })

  it('statusConfig 自定义项覆盖默认：标签用自定义文案，未知图标名回退默认图标', () => {
    render(
      <FileTreeWidget
        data={[{ id: 's1', title: '自定义状态节点', status: 'running' }]}
        statusConfig={{ running: { icon: 'no-such-icon', color: 'text-status-info', label: '跑着' } }}
      />,
    )
    expect(screen.getByText('跑着')).toBeInTheDocument()
  })

  it('showStatus=false：状态图标标签与状态筛选栏都不渲染', () => {
    render(
      <FileTreeWidget
        showStatus={false}
        data={[{ id: 'n', title: '无状态节点', status: 'running' }]}
      />,
    )
    expect(screen.queryByText('全部')).not.toBeInTheDocument()
    expect(screen.queryByText('运行中')).not.toBeInTheDocument()
    expect(screen.getByText('无状态节点')).toBeInTheDocument()
  })
})

describe('新建任务入口', () => {
  it.each([
    { label: '有 sessionId', sessionId: 'sess-9' as string | undefined, expected: 'sess-9' },
    { label: '无 sessionId', sessionId: undefined, expected: '' },
  ])('$label：打开新建模态并透传（关闭回调生效）', ({ sessionId, expected }) => {
    render(
      <FileTreeWidget
        sessionId={sessionId}
        data={[{ id: 'm1', title: '模态树节点', status: 'running' }]}
      />,
    )
    expect(screen.queryByTestId('create-task-modal-probe')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTitle('新建根任务'))
    const probe = screen.getByTestId('create-task-modal-probe')
    expect(probe.getAttribute('data-session-id')).toBe(expected)

    fireEvent.click(screen.getByTestId('close-modal-probe'))
    expect(screen.queryByTestId('create-task-modal-probe')).not.toBeInTheDocument()
  })
})

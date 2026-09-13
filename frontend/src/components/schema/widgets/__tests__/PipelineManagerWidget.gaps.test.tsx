// @feature FP-T12 前端适配 | @ci frontend-test
/**
 * PipelineManagerWidget 覆盖缺口补充测试（与既有 PipelineManagerWidget.test 互补，不重复）：
 * - 工具栏：视图切换 / 类型筛选（全部·任务·会话）/ 状态筛选（含 paused→suspended 别名）
 * - 树视图：执行中/最近完成分组折叠、会话主管道（pipelineIds）子级缩进、
 *   项目挂靠任务缩进、组头计数
 * - 行内操作（树+列表双视图）：复制 ID（clipboard 成功/拒绝/缺失）、暂停/恢复/取消、
 *   操作失败 console 告警、打开工作空间（含既有 Tab 仅激活）、打开对话按钮
 * - 打开对话分流：归属会话直跳、导航失败回退 setActiveSession、回退再失败通知、
 *   既有同管道 Tab 仅切换、孤儿条目建子标签的 6 态状态映射
 * - 详情面板：结束/错误/已结束=否/Token 汇总/Token 实时/进度条（含 >100 clamp）
 * - 列表视图空态、项目行点击不开对话、删除弹窗 Esc/取消关闭
 * - 任务管道 ID 双取（metadata.pipelineRunId）、实时 token 优先、total_tokens 三形态
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { renderWithProviders } from '@/test/renderWithProviders'
import type { AgentTab } from '@/types/task'

/** 测试播种数据（可变单例：beforeEach 清空，用例自行填充） */
const seed = vi.hoisted(() => {
  const runs: Record<string, Record<string, unknown>> = {}
  const states: Record<string, Record<string, unknown>> = {}
  const tasks: Record<string, unknown>[] = []
  const sessions: Record<string, unknown>[] = []
  const projects: Record<string, unknown>[] = []
  const usage: { usageByPipeline: Record<string, Record<string, unknown>> } = {
    usageByPipeline: {},
  }
  return {
    runs,
    states,
    tasks,
    sessions,
    projects,
    usage,
    mockPause: vi.fn(() => Promise.resolve()),
    mockResume: vi.fn(() => Promise.resolve()),
    mockCancel: vi.fn(() => Promise.resolve()),
    mockDeleteProject: vi.fn(() =>
      Promise.resolve({
        message: '项目已删除',
        id: 'proj-x',
        suspended_children: 0,
        deleted_children: 0,
        folder_removed: false,
      }),
    ),
    mockFetchProjects: vi.fn(() => Promise.resolve({ items: [] as unknown[] })),
    mockWorkspaceOpen: vi.fn(() => Promise.resolve({ data: { success: true } })),
    mockNavigate: vi.fn(() => Promise.resolve(true)),
    mockSetActiveSession: vi.fn(() => Promise.resolve()),
    mockInvalidate: vi.fn(),
    mockReadSessions: vi.fn(() => [] as Record<string, unknown>[]),
    mockEnsureSessions: vi.fn(() => Promise.resolve([] as unknown[])),
    mockUseRuns: vi.fn(() => ({ data: runs })),
    mockUseStates: vi.fn(() => ({ data: states })),
    mockUseTasks: vi.fn(() => ({ data: tasks })),
    mockUseSessions: vi.fn(() => ({ data: sessions })),
  }
})

vi.mock('@/services/api/tasks', () => ({
  pauseTask: seed.mockPause,
  resumeTask: seed.mockResume,
  cancelTask: seed.mockCancel,
  deleteProject: seed.mockDeleteProject,
  fetchProjects: seed.mockFetchProjects,
}))
vi.mock('@/services/api/client', () => ({
  default: { post: seed.mockWorkspaceOpen },
}))
vi.mock('@/services/pipelineNavigator', () => ({
  navigateToPipeline: seed.mockNavigate,
}))
vi.mock('@/hooks/queries/usePipelineRunsQuery', () => ({
  usePipelineRunsQuery: seed.mockUseRuns,
  usePipelineStatesQuery: seed.mockUseStates,
}))
vi.mock('@/hooks/queries/useAllTasksQuery', () => ({
  useAllTasksQuery: seed.mockUseTasks,
}))
vi.mock('@/hooks/queries/useLongTermTasksQuery', () => ({
  invalidateLongTermTasks: seed.mockInvalidate,
}))
vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  useSessionsQuery: seed.mockUseSessions,
  readSessions: seed.mockReadSessions,
  ensureSessionsLoaded: seed.mockEnsureSessions,
}))
vi.mock('@/stores/contextUsageStore', () => ({
  useContextUsageStore: (sel: (s: unknown) => unknown) =>
    sel({ usageByPipeline: seed.usage.usageByPipeline }),
}))
vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: (sel: (s: unknown) => unknown) =>
    sel({ sessions: [], activeSessionId: null }),
}))
vi.mock('@/stores/sessionListStore', () => ({
  useSessionListStore: {
    getState: () => ({ setActiveSession: seed.mockSetActiveSession }),
  },
}))

// ────────────────────────── 公共基建 ──────────────────────────

function resetSeed() {
  for (const k of Object.keys(seed.runs)) delete seed.runs[k]
  for (const k of Object.keys(seed.states)) delete seed.states[k]
  seed.tasks.length = 0
  seed.sessions.length = 0
  seed.projects.length = 0
  seed.usage.usageByPipeline = {}
}

/** 标准播种：2 任务 + 1 挂起任务 + 1 metadata 双取任务 + 2 会话管道 + 1 孤儿管道 */
function seedStandard() {
  seed.runs.r1 = {
    pipeline_id: 'pipe-running', run_id: 'run-1', thread_id: 'th-1',
    status: 'running', started_at: '2026-09-01T00:00:00Z',
  }
  seed.runs.r2 = {
    pipeline_id: 'pipe-done', run_id: 'run-2', thread_id: 'th-1', status: 'completed',
    started_at: '2026-09-01T00:01:00Z', ended_at: '2026-09-01T00:02:00Z',
    total_tokens: { input: 10, output: 20, total: 51234 },
  }
  seed.runs.r3 = {
    pipeline_id: 'pipe-orph', run_id: 'run-3', status: 'suspended',
    started_at: '2026-09-01T00:03:00Z',
  }
  seed.tasks.push(
    { id: 't-run', title: '运行任务', status: 'running', pipeline_run_id: 'pipe-running', agent_name: 'general_agent' },
    { id: 't-susp', title: '挂起任务', status: 'suspended', pipeline_run_id: 'pipe-susp', agent_name: 'general_agent', timestamps: { startedAt: '2026-09-01T00:04:00Z' } },
    { id: 't-meta', title: 'meta任务', status: 'completed', metadata: { pipelineRunId: 'pipe-meta' }, timestamps: { startedAt: '2026-09-01T00:05:00Z' } },
  )
}

function resetStores() {
  useAgentTabStore.setState({ tabs: [], activeTabId: null })
  useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
  useNotificationStore.setState({ notifications: [] })
}

function setClipboard(value: { writeText: (t: string) => Promise<void> } | undefined) {
  Object.defineProperty(window.navigator, 'clipboard', { value, configurable: true })
}

/** 定位含指定条目名的行内按钮（title 或 aria-label 精确匹配；树行=div，列表行=tr） */
function rowButton(entryName: string, selector: string): HTMLElement {
  const el = screen.getAllByText(entryName)[0]
  const row = (el.closest('tr') ?? el.closest('div')) as HTMLElement
  const btn = row.querySelector(selector) as HTMLElement | null
  expect(btn, `row button ${selector} for ${entryName}`).toBeTruthy()
  return btn as HTMLElement
}

/** 树视图默认收起：先展开父行（首个 chevron）再取子行元素 */
async function expandTreeChild(parentName: string, childName: string) {
  const parentRow = ((await screen.findAllByText(parentName))[0]).closest('div') as HTMLElement
  fireEvent.click(parentRow.querySelector('button') as HTMLElement)
  return (await screen.findAllByText(childName))[0]
}

function makeTab(partial: Partial<AgentTab> & { id: string }): AgentTab {
  return {
    agentId: partial.id,
    agentName: partial.id,
    agentLevel: 2,
    path: [],
    status: 'running',
    hasUnread: false,
    canClose: true,
    ...partial,
  }
}

beforeEach(() => {
  resetSeed()
  resetStores()
  setClipboard(undefined)
  seed.mockNavigate.mockReset().mockResolvedValue(true)
  seed.mockReadSessions.mockReset().mockReturnValue([])
  seed.mockEnsureSessions.mockReset().mockResolvedValue([])
  seed.mockSetActiveSession.mockReset().mockResolvedValue(undefined)
  seed.mockPause.mockReset().mockResolvedValue(undefined)
  seed.mockResume.mockReset().mockResolvedValue(undefined)
  seed.mockCancel.mockReset().mockResolvedValue(undefined)
  seed.mockWorkspaceOpen.mockReset().mockResolvedValue({ data: { success: true } })
  seed.mockFetchProjects.mockReset().mockResolvedValue({ items: seed.projects })
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
})

// ────────────────────────── 工具栏筛选 ──────────────────────────

describe('PipelineManagerWidget 工具栏筛选', () => {
  it('类型筛选：任务/会话/全部 三态互斥切换（列表视图断言）', async () => {
    seedStandard()
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')
    fireEvent.click(screen.getByTitle('列表视图'))

    // 任务：会话条目全部隐藏
    fireEvent.click(screen.getByRole('button', { name: '任务' }))
    expect(screen.queryByText('会话 th-1')).toBeNull()
    expect(screen.queryByText('pipe-orph')).toBeNull()
    expect(screen.getByText('运行任务')).toBeInTheDocument()
    expect(screen.getByText('挂起任务')).toBeInTheDocument()
    expect(screen.getByText('meta任务')).toBeInTheDocument()

    // 会话：只留会话条目
    fireEvent.click(screen.getByRole('button', { name: '会话' }))
    expect(screen.queryByText('运行任务')).toBeNull()
    expect(screen.getByText('会话 th-1')).toBeInTheDocument()
    expect(screen.getByText('pipe-orph')).toBeInTheDocument()

    // 全部：恢复（工具栏有两个「全部」：类型筛选在前、状态筛选在后）
    fireEvent.click(screen.getAllByRole('button', { name: '全部' })[0])
    expect(screen.getByText('运行任务')).toBeInTheDocument()
    expect(screen.getByText('会话 th-1')).toBeInTheDocument()

    // 树 ⇄ 列表双向切换：回到树视图后分组表头回归
    fireEvent.click(screen.getByTitle('树视图（会话分组）'))
    expect(await screen.findByText(/执行中的管道/)).toBeInTheDocument()
  })

  it('状态筛选：已完成按运行态过滤；已暂停别名命中 suspended', async () => {
    seedStandard()
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')
    fireEvent.click(screen.getByTitle('列表视图'))

    fireEvent.click(screen.getByRole('button', { name: '已完成' }))
    expect(screen.getByText('meta任务')).toBeInTheDocument()
    expect(screen.getByText('会话 th-1')).toBeInTheDocument()
    expect(screen.queryByText('运行任务')).toBeNull()
    expect(screen.queryByText('挂起任务')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '已暂停' }))
    expect(screen.getByText('挂起任务')).toBeInTheDocument()
    expect(screen.getByText('pipe-orph')).toBeInTheDocument()
    expect(screen.queryByText('meta任务')).toBeNull()
  })
})

// ────────────────────────── 树视图分组与层级 ──────────────────────────

describe('PipelineManagerWidget 树视图', () => {
  it('执行中/最近完成分组渲染，组头折叠收起条目再展开恢复', async () => {
    seedStandard()
    renderWithProviders(<PipelineManagerWidget />)

    expect(await screen.findByText(/执行中的管道/)).toBeInTheDocument()
    expect(screen.getByText(/最近完成/)).toBeInTheDocument()
    expect(screen.getByText('运行任务')).toBeInTheDocument()
    expect(screen.getByText('meta任务')).toBeInTheDocument()

    // 折叠执行中分组：其下条目收起
    fireEvent.click(screen.getByText(/执行中的管道/))
    expect(screen.queryByText('运行任务')).toBeNull()
    expect(screen.queryByText('pipe-orph')).toBeNull()
    // 最近完成分组不受影响
    expect(screen.getByText('meta任务')).toBeInTheDocument()

    // 再展开恢复
    fireEvent.click(screen.getByText(/执行中的管道/))
    expect(screen.getByText('运行任务')).toBeInTheDocument()
  })

  it('会话主管道（session.pipelineIds）优先：其余管道缩进挂其下', async () => {
    seedStandard()
    seed.sessions.push({ id: 'th-1', title: '主会话', pipelineIds: ['pipe-running'] })
    renderWithProviders(<PipelineManagerWidget />)

    // 主管道条目行（任务一对一绑定 → 行名 = 任务名）展开子级
    fireEvent.click(
      (await screen.findAllByText('运行任务'))[0].closest('div')?.querySelector('button') as HTMLElement,
    )
    const childRow = (await screen.findAllByText('主会话'))[0].closest('div')
    expect(childRow?.getAttribute('style') ?? '').toMatch(/padding-left:\s*2[48]px/)
  })

  it('项目挂靠任务（metadata.parent_project_id）挂项目分组节点下', async () => {
    seed.projects.push({ id: 'proj-9', goal: '挂靠项目', timestamps: { createdAt: '2026-09-01T00:00:00Z' } })
    seed.tasks.push({
      id: 't-p', title: '挂靠任务', status: 'running', pipeline_run_id: 'pipe-p',
      metadata: { parent_project_id: 'proj-9' },
    })
    renderWithProviders(<PipelineManagerWidget />)

    fireEvent.click(
      (await screen.findAllByText('挂靠项目'))[0].closest('div')?.querySelector('button') as HTMLElement,
    )
    const taskRow = (await screen.findAllByText('挂靠任务'))[0].closest('div')
    expect(taskRow?.getAttribute('style') ?? '').toMatch(/padding-left:\s*2[48]px/)
  })
})

// ────────────────────────── 行内操作 ──────────────────────────

describe('PipelineManagerWidget 行内操作', () => {
  it('复制管道 ID：clipboard 写入 pipeline_id；拒绝与缺失均静默', async () => {
    seedStandard()
    const writeText = vi.fn<(t: string) => Promise<void>>().mockResolvedValue(undefined)
    setClipboard({ writeText })
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    fireEvent.click(rowButton('运行任务', 'button[title="复制管道 ID"]'))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('pipe-running'))

    // clipboard 拒绝 → 静默
    writeText.mockRejectedValueOnce(new Error('denied'))
    fireEvent.click(rowButton('运行任务', 'button[title="复制管道 ID"]'))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(2))
    expect(console.error).not.toHaveBeenCalled()

    // clipboard 缺失 → 静默
    setClipboard(undefined)
    fireEvent.click(rowButton('运行任务', 'button[title="复制管道 ID"]'))
    await waitFor(() => expect(console.error).not.toHaveBeenCalled())
  })

  it('暂停/取消运行中任务 → 对应端点 + 任务列表失效刷新；失败只告警不崩', async () => {
    seedStandard()
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    fireEvent.click(rowButton('运行任务', 'button[title="暂停任务"]'))
    await waitFor(() => expect(seed.mockPause).toHaveBeenCalledWith('t-run'))
    expect(seed.mockInvalidate).toHaveBeenCalled()

    fireEvent.click(rowButton('运行任务', 'button[title="取消任务"]'))
    await waitFor(() => expect(seed.mockCancel).toHaveBeenCalledWith('t-run'))
    expect(seed.mockInvalidate).toHaveBeenCalled()

    // 操作失败 → console.error 兜底，不崩
    seed.mockPause.mockRejectedValueOnce(new Error('pause boom'))
    fireEvent.click(rowButton('运行任务', 'button[title="暂停任务"]'))
    await waitFor(() => expect(console.error).toHaveBeenCalled())
  })

  it('恢复挂起任务 → resume 端点', async () => {
    seedStandard()
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findAllByText('挂起任务')

    fireEvent.click(rowButton('挂起任务', 'button[title="恢复任务"]'))
    await waitFor(() => expect(seed.mockResume).toHaveBeenCalledWith('t-susp'))
    expect(seed.mockInvalidate).toHaveBeenCalled()
  })

  it('打开工作空间：任务条目用 taskId 开 ws-tree Tab；已有 Tab 仅激活', async () => {
    seedStandard()
    seed.tasks[2] = {
      ...seed.tasks[2],
      metadata: { pipelineRunId: 'pipe-meta', ws_meta: { path: 'D:/ws/meta' } },
    }
    useLayoutModeStore.setState({
      workspaceTabs: [
        { id: 'ws-tree-t-meta', title: 'meta任务', moduleId: '__dynamic__', component: 'file_tree', dataSource: 'workspace://t-meta', isActive: false, isPinned: false },
      ],
    })
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findAllByText('meta任务')

    fireEvent.click(rowButton('meta任务', 'button[title="打开工作空间: D:/ws/meta"]'))

    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs).toHaveLength(1)
    expect(tabs[0]).toMatchObject({ id: 'ws-tree-t-meta', isActive: true })
  })

  it('打开对话按钮（树视图）：归属会话条目走导航器', async () => {
    seedStandard()
    seed.mockReadSessions.mockReturnValue([{ id: 'th-1', title: '会话A' }])
    renderWithProviders(<PipelineManagerWidget />)
    // 树默认收起：pipe-done 挂 pipe-running 下，先展开父行
    await expandTreeChild('运行任务', '会话 th-1')

    fireEvent.click(rowButton('会话 th-1', 'button[aria-label="打开对话"]'))

    await waitFor(() =>
      expect(seed.mockNavigate).toHaveBeenCalledWith(
        'pipe-done',
        expect.objectContaining({ taskId: undefined, agentLevel: 2 }),
      ),
    )
    // 导航成功 → 不回退切换会话
    expect(seed.mockSetActiveSession).not.toHaveBeenCalled()
  })

  it('导航返回 false → 回退切换归属会话；回退失败 → 用户可见通知', async () => {
    seedStandard()
    seed.mockReadSessions.mockReturnValue([{ id: 'th-1', title: '会话A' }])
    seed.mockNavigate.mockReset().mockResolvedValue(false)
    const addNotification = vi
      .spyOn(useNotificationStore.getState(), 'addNotification')
      .mockImplementation(() => {})
    renderWithProviders(<PipelineManagerWidget />)
    await expandTreeChild('运行任务', '会话 th-1')

    // 回退失败版本：先验证通知分支
    seed.mockSetActiveSession.mockReset().mockRejectedValue(new Error('switch boom'))
    fireEvent.click(rowButton('会话 th-1', 'button[aria-label="打开对话"]'))
    await waitFor(() =>
      expect(addNotification).toHaveBeenCalledWith(
        expect.objectContaining({
          title: '无法打开对话',
          message: expect.stringContaining('pipe-done'),
        }),
      ),
    )

    // 回退成功版本：setActiveSession 收到归属会话 id
    seed.mockSetActiveSession.mockReset().mockResolvedValue(undefined)
    fireEvent.click(rowButton('会话 th-1', 'button[aria-label="打开对话"]'))
    await waitFor(() => expect(seed.mockSetActiveSession).toHaveBeenCalledWith('th-1'))
    addNotification.mockRestore()
  })

  it('导航器拒绝（throw）→ 落回退切换会话', async () => {
    seedStandard()
    seed.mockReadSessions.mockReturnValue([{ id: 'th-1', title: '会话A' }])
    seed.mockNavigate.mockReset().mockRejectedValue(new Error('nav down'))
    renderWithProviders(<PipelineManagerWidget />)
    await expandTreeChild('运行任务', '会话 th-1')

    fireEvent.click(rowButton('会话 th-1', 'button[aria-label="打开对话"]'))

    await waitFor(() => expect(seed.mockSetActiveSession).toHaveBeenCalledWith('th-1'))
  })

  it('已有同管道 Tab 的孤儿条目 → 仅切换既有 Tab，不新建', async () => {
    seedStandard()
    useAgentTabStore.setState({
      tabs: [makeTab({ id: 'tab-x', pipelineRunId: 'pipe-running' })],
      activeTabId: null,
    })
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    fireEvent.click(screen.getAllByText('运行任务')[0])

    await waitFor(() => expect(useAgentTabStore.getState().activeTabId).toBe('tab-x'))
    expect(useAgentTabStore.getState().tabs).toHaveLength(1)
  })

  it('孤儿条目点击按运行状态映射建子标签（6 态）', async () => {
    const cases = [
      ['running', 'running'],
      ['completed', 'completed'],
      ['failed', 'failed'],
      ['suspended', 'waiting_input'],
      ['cancelled', 'waiting_input'],
      ['unknown', 'unknown'],
    ] as const
    for (const [runStatus, tabStatus] of cases) {
      resetSeed()
      resetStores()
      const pid = `st-${runStatus}`
      seed.runs.a = {
        pipeline_id: pid, run_id: `run-${pid}`, status: runStatus,
        started_at: '2026-09-01T00:00:00Z',
      }
      renderWithProviders(<PipelineManagerWidget />)
      fireEvent.click((await screen.findAllByText(pid))[0])

      await waitFor(() =>
        expect(
          useAgentTabStore.getState().tabs.some(
            (t) => t.pipelineRunId === pid && t.status === tabStatus,
          ),
        ).toBe(true),
      )
      resetStores()
    }
  })
})

// ────────────────────────── 详情面板 ──────────────────────────

describe('PipelineManagerWidget 详情面板', () => {
  it('结束时间 / Token 汇总 / Token 实时 行完整呈现', async () => {
    seedStandard()
    renderWithProviders(<PipelineManagerWidget />)
    await expandTreeChild('运行任务', '会话 th-1')
    fireEvent.click(rowButton('会话 th-1', 'button[aria-label="切换详细信息"]'))

    expect(await screen.findByText('结束')).toBeInTheDocument()
    expect(screen.getByText('input=10 · output=20 · total=51234')).toBeInTheDocument()
    // token 值两处呈现：条目行 token 列 + 详情「Token 实时」行
    expect(screen.getByText('Token 实时')).toBeInTheDocument()
    expect(screen.getAllByText('51,234').length).toBeGreaterThanOrEqual(2)

    // 再点一次详细信息 → 收起详情（toggle 对称：树操作不动详情，详情自身可关）
    fireEvent.click(rowButton('会话 th-1', 'button[aria-label="切换详细信息"]'))
    await waitFor(() => expect(screen.queryByText('Token 实时')).toBeNull())
  })

  it('state 真值行：错误、已结束=是', async () => {
    seedStandard()
    seed.states.s1 = {
      pipeline_id: 'pipe-running',
      state: { raw_error: 'boom 消息', ended: true },
    }
    renderWithProviders(<PipelineManagerWidget />)
    fireEvent.click(rowButton('运行任务', 'button[aria-label="切换详细信息"]'))

    expect(await screen.findByText('错误')).toBeInTheDocument()
    expect(screen.getByText('boom 消息')).toBeInTheDocument()
    expect(screen.getByText('已结束')).toBeInTheDocument()
    expect(screen.getByText('是')).toBeInTheDocument()
  })

  it('任务进度条：正常值按百分比渲染，超界值 clamp 到 100%', async () => {
    seedStandard()
    seed.tasks[0] = { ...seed.tasks[0], progress: { progressPercent: 42 } }
    seed.tasks[2] = { ...seed.tasks[2], progress: { progressPercent: 150 } }
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    fireEvent.click(rowButton('运行任务', 'button[aria-label="切换详细信息"]'))
    expect(await screen.findByText('42%')).toBeInTheDocument()
    // 进度条 = 百分比文案的兄弟容器内的满高条
    const barWrap = screen.getByText('42%').previousElementSibling as HTMLElement
    expect((barWrap.firstElementChild as HTMLElement).style.width).toBe('42%')

    fireEvent.click(rowButton('meta任务', 'button[aria-label="切换详细信息"]'))
    expect(await screen.findByText('150%')).toBeInTheDocument()
    const wrap150 = screen.getByText('150%').previousElementSibling as HTMLElement
    expect((wrap150.firstElementChild as HTMLElement).style.width).toBe('100%')
  })

  it.each([
    [{ total: 63456 }, '63,456'],
    [{ total_tokens: 74567 }, '74,567'],
    [{ output: 85678 }, '85,678'],
  ])('total_tokens 形态 %s → token 列展示 %s', async (tokens, expected) => {
    seedStandard()
    seed.runs.r2.total_tokens = tokens
    renderWithProviders(<PipelineManagerWidget />)
    await expandTreeChild('运行任务', '会话 th-1')
    expect((await screen.findAllByText(expected)).length).toBeGreaterThanOrEqual(1)
  })

  it('实时 usage 优先于快照 total_tokens', async () => {
    seedStandard()
    seed.usage.usageByPipeline['pipe-running'] = {
      promptTokens: 1, completionTokens: 2, totalTokens: 999999,
    }
    renderWithProviders(<PipelineManagerWidget />)
    expect((await screen.findAllByText('999,999')).length).toBeGreaterThanOrEqual(1)
  })
})

// ────────────────────────── 列表视图 ──────────────────────────

describe('PipelineManagerWidget 列表视图', () => {
  it('空数据：树/列表双视图空态占位', async () => {
    renderWithProviders(<PipelineManagerWidget />)
    expect((await screen.findAllByText('暂无管道运行记录')).length).toBeGreaterThanOrEqual(1)

    fireEvent.click(screen.getByTitle('列表视图'))
    expect(screen.getByText('暂无管道运行记录')).toBeInTheDocument()
  })

  it('行内操作齐备：打开对话/暂停/取消/恢复/复制/工作空间', async () => {
    seedStandard()
    seed.tasks[2] = {
      ...seed.tasks[2],
      metadata: { pipelineRunId: 'pipe-meta', ws_meta: { path: 'D:/ws/meta' } },
    }
    seed.mockReadSessions.mockReturnValue([{ id: 'th-1', title: '会话A' }])
    const writeText = vi.fn<(t: string) => Promise<void>>().mockResolvedValue(undefined)
    setClipboard({ writeText })
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')
    fireEvent.click(screen.getByTitle('列表视图'))

    fireEvent.click(rowButton('会话 th-1', 'button[aria-label="打开对话"]'))
    await waitFor(() => expect(seed.mockNavigate).toHaveBeenCalledWith('pipe-done', expect.anything()))

    fireEvent.click(rowButton('运行任务', 'button[aria-label="复制管道 ID"]'))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('pipe-running'))

    fireEvent.click(rowButton('运行任务', 'button[title="暂停任务"]'))
    await waitFor(() => expect(seed.mockPause).toHaveBeenCalledWith('t-run'))

    fireEvent.click(rowButton('运行任务', 'button[title="取消任务"]'))
    await waitFor(() => expect(seed.mockCancel).toHaveBeenCalledWith('t-run'))

    fireEvent.click(rowButton('挂起任务', 'button[title="恢复任务"]'))
    await waitFor(() => expect(seed.mockResume).toHaveBeenCalledWith('t-susp'))

    fireEvent.click(rowButton('meta任务', 'button[title="打开工作空间: D:/ws/meta"]'))
    await waitFor(() =>
      expect(
        useLayoutModeStore.getState().workspaceTabs.some((t) => t.dataSource === 'workspace://t-meta'),
      ).toBe(true),
    )
  })

  it('项目行点击不开对话（对照：普通条目行点击会打开对话）', async () => {
    seedStandard()
    seed.projects.push({ id: 'proj-l', goal: '列表项目', timestamps: { createdAt: '2026-09-01T00:00:00Z' } })
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findByText('列表项目')
    fireEvent.click(screen.getByTitle('列表视图'))

    // 项目行：点击无动作
    fireEvent.click(screen.getAllByText('列表项目')[0])
    await waitFor(() => expect(useAgentTabStore.getState().tabs).toHaveLength(0))

    // 对照组：普通条目行点击 → 打开对话（孤儿直建子标签）
    fireEvent.click(screen.getAllByText('pipe-orph')[0])
    await waitFor(() =>
      expect(
        useAgentTabStore.getState().tabs.some((t) => t.pipelineRunId === 'pipe-orph'),
      ).toBe(true),
    )
  })
})

// ────────────────────────── 删除弹窗关闭 ──────────────────────────

describe('PipelineManagerWidget 项目删除弹窗关闭', () => {
  async function openDialog() {
    seed.projects.push({ id: 'proj-d', goal: '待删项目', timestamps: { createdAt: '2026-09-01T00:00:00Z' } })
    renderWithProviders(<PipelineManagerWidget />)
    fireEvent.click((await screen.findByText('待删项目')).closest('div')?.querySelector('button[aria-label="删除项目"]') as HTMLElement)
    expect(await screen.findByText(/请选择名下子任务的处置方式/)).toBeTruthy()
  }

  it('取消按钮关闭弹窗且不调删除端点（口径可来回切换）', async () => {
    await openDialog()
    // 口选级联再切回默认口径：radio onChange 双向生效
    fireEvent.click(screen.getByLabelText('连同子任务一起删除（不可恢复）'))
    fireEvent.click(screen.getByLabelText('仅删除项目——名下子任务挂起并保留'))
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    await waitFor(() =>
      expect(screen.queryByText(/请选择名下子任务的处置方式/)).toBeNull(),
    )
    expect(seed.mockDeleteProject).not.toHaveBeenCalled()
  })

  it('Esc 关闭弹窗', async () => {
    await openDialog()
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    await waitFor(() =>
      expect(screen.queryByText(/请选择名下子任务的处置方式/)).toBeNull(),
    )
  })
})

// ────────────────────────── 秒级 ticker ──────────────────────────

describe('PipelineManagerWidget 秒级 ticker', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('有运行中条目时每秒刷新耗时列（fake clock 驱动）', async () => {
    vi.useFakeTimers()
    const startedAt = new Date(Date.now() - 61_000).toISOString()
    seed.runs.a = {
      pipeline_id: 'tick-p', run_id: 'run-tick', status: 'running', started_at: startedAt,
    }
    // query hook 全 mock：数据同步就绪，无需异步等待（fake timers 下 findBy 会挂起）
    renderWithProviders(<PipelineManagerWidget />)
    expect(screen.getByText('1m 1s')).toBeInTheDocument()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(screen.getByText('2m 1s')).toBeInTheDocument()
  })
})

// ────────────────────────── 任务管道 ID 双取 ──────────────────────────

describe('PipelineManagerWidget 任务解析边缘', () => {
  it('仅 metadata.pipelineRunId 的任务照常成行并可打开对话（agentId 落任务 id）', async () => {
    seedStandard()
    renderWithProviders(<PipelineManagerWidget />)
    expect(await screen.findAllByText('meta任务').then((els) => els.length)).toBeGreaterThanOrEqual(1)

    fireEvent.click(screen.getAllByText('meta任务')[0])
    await waitFor(() =>
      expect(
        useAgentTabStore.getState().tabs.some(
          (t) => t.pipelineRunId === 'pipe-meta' && t.agentId === 't-meta',
        ),
      ).toBe(true),
    )
  })
})

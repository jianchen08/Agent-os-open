/**
 * PipelineManagerWidget 组件测试（2026-08-19 调试中心批次；批次 4 query 化适配）
 *
 * 验证任务管理面板（管道总览）三个行为修复：
 * - 未知状态的任务不再被丢弃（原 taskStatusToPipelineStatus 返回 null 即 continue）；
 * - 条目行显示任务原始状态（evaluating 等细态不被 4 态映射吞掉）；
 * - 展开详情含 state 真值行（任务状态/State 状态/已结束/当前阶段/消息条数）。
 *
 * 批次 4 适配：runs/states/全量任务已迁 query（usePipelineRunsQuery /
 * usePipelineStatesQuery / useAllTasksQuery）——mock 三个 query hook 直接返回
 * 播种数据，本地 30s 轮询/注册表自动刷新已退役。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { renderWithProviders } from '@/test/renderWithProviders'

vi.mock('@/services/api/tasks', () => ({
  pauseTask: vi.fn(),
  resumeTask: vi.fn(),
  cancelTask: vi.fn(),
}))
vi.mock('@/services/pipelineNavigator', () => ({
  navigateToPipeline: vi.fn(),
}))

/** 测试播种数据（vi.hoisted：mock 工厂提升执行期前需就绪） */
const seed = vi.hoisted(() => {
  const FAKE_RUNS: Record<string, unknown> = {
    mainRun: {
      pipeline_id: 'mainPipe',
      run_id: 'run-main',
      thread_id: 'th-1',
      status: 'completed',
      started_at: '2026-08-22T00:00:00Z',
      ended_at: '2026-08-22T00:01:00Z',
    },
    subRun: {
      pipeline_id: 'subPipe',
      run_id: 'run-sub',
      thread_id: 'th-1',
      status: 'completed',
      started_at: '2026-08-22T00:02:00Z',
      ended_at: '2026-08-22T00:03:00Z',
    },
  }
  const FAKE_STATES: Record<string, unknown> = {
    pipeA: {
      pipeline_id: 'pipeA',
      thread_id: 'th-a',
      source: 'memory',
      state: {
        current_phase: 'exit',
        ended: true,
        status: 'active',
        'task.status': 'completed',
        message_count: 12,
        raw_error: null,
      },
    },
  }
  /** 全量任务列表（query 化后由 useAllTasksQuery 提供） */
  const FAKE_ALL_TASKS: Record<string, unknown>[] = [
    {
      id: 'task-eval',
      title: '评估中任务',
      status: 'evaluating', // 4 态映射 → running
      pipeline_run_id: 'pipeA',
      agent_name: 'general_agent',
    },
    {
      id: 'task-weird',
      title: '未知状态任务',
      status: 'pending_review', // 4 态映射 → null（原实现直接丢弃）
      pipeline_run_id: 'pipeB',
      agent_name: 'general_agent',
    },
  ]
  /** 主管道提交子任务场景：子任务 parent_task_id = 主管道 id（非任务节点） */
  const SUBTASK_TASKS: Record<string, unknown>[] = [
    {
      id: 'mainPipe',
      title: '主管道',
      status: 'completed',
      pipeline_run_id: 'mainPipe',
      agent_name: 'general_agent',
    },
    {
      id: 'subPipe',
      title: '子任务',
      status: 'completed',
      pipeline_run_id: 'subPipe',
      agent_name: 'general_agent',
      parent_task_id: 'mainPipe',
    },
  ]
  /** 子任务带工作空间场景（0.1 对齐：任务节点"打开工作空间"按钮数据链） */
  const WS_TASKS: Record<string, unknown>[] = [
    {
      id: 'mainPipe',
      title: '主管道',
      status: 'completed',
      pipeline_run_id: 'mainPipe',
      agent_name: 'general_agent',
    },
    {
      id: 'subPipe',
      title: '带空间的子任务',
      status: 'running',
      pipeline_run_id: 'subPipe',
      agent_name: 'general_agent',
      parent_task_id: 'mainPipe',
      metadata: { ws_meta: { path: 'D:/ws/copy_1', mode: 'worktree' } },
    },
  ]
  /** 会话主管道条目场景：主管道有 runs 快照但任务列表无对应任务（kind=session 条目） */
  const SESSION_MAIN_RUNS: Record<string, unknown> = {
    mainRun: {
      pipeline_id: 'mainpipe111',
      run_id: 'run-main',
      thread_id: 'th-1',
      status: 'running',
      started_at: '2026-08-22T00:00:00Z',
    },
    subRun: {
      pipeline_id: 'subpipe222',
      run_id: 'run-sub',
      thread_id: 'th-1',
      status: 'running',
      started_at: '2026-08-22T00:01:00Z',
    },
  }
  /** 子任务：parent_task_id = 主管道全 id（主管道是会话条目，非任务节点——"父=管道"分支） */
  const SESSION_MAIN_TASKS: Record<string, unknown>[] = [
    {
      id: 'task-sub1',
      title: '子任务A',
      status: 'running',
      pipeline_run_id: 'subpipe222',
      parent_task_id: 'mainpipe111',
      agent_name: 'general_agent',
    },
  ]
  const mockUseAllTasksQuery = vi.fn(() => ({ data: FAKE_ALL_TASKS }))
  const mockUsePipelineRunsQuery = vi.fn(() => ({ data: FAKE_RUNS }))
  return {
    FAKE_RUNS,
    FAKE_STATES,
    FAKE_ALL_TASKS,
    SUBTASK_TASKS,
    WS_TASKS,
    SESSION_MAIN_RUNS,
    SESSION_MAIN_TASKS,
    mockUseAllTasksQuery,
    mockUsePipelineRunsQuery,
  }
})

vi.mock('@/hooks/queries/usePipelineRunsQuery', () => ({
  usePipelineRunsQuery: seed.mockUsePipelineRunsQuery,
  usePipelineStatesQuery: () => ({ data: seed.FAKE_STATES }),
}))
vi.mock('@/hooks/queries/useAllTasksQuery', () => ({
  useAllTasksQuery: seed.mockUseAllTasksQuery,
}))
vi.mock('@/hooks/queries/useLongTermTasksQuery', () => ({
  invalidateLongTermTasks: vi.fn(),
}))
// 会话列表走真实 query 会打真实 HTTP——mock 为空列表（widget 仅用标题映射）
vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  useSessionsQuery: () => ({ data: [] }),
  readSessions: () => [],
  ensureSessionsLoaded: () => Promise.resolve([]),
}))

vi.mock('@/stores/contextUsageStore', () => ({
  useContextUsageStore: (sel: (s: unknown) => unknown) => sel({ usageByPipeline: {} }),
}))
vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: (sel: (s: unknown) => unknown) => sel({ sessions: [] }),
}))

describe('PipelineManagerWidget', () => {
  beforeEach(() => {
    // 默认播种全量任务；子任务用例覆盖为 SUBTASK_TASKS
    seed.mockUseAllTasksQuery.mockReturnValue({ data: seed.FAKE_ALL_TASKS })
    seed.mockUsePipelineRunsQuery.mockReturnValue({ data: seed.FAKE_RUNS })
  })

  it('未知状态任务保留且显示原始状态', async () => {
    renderWithProviders(<PipelineManagerWidget />)
    // 两个任务都出现（未知状态不再被吞）；树视图+列表视图双渲染 → 用 getAll
    expect((await screen.findAllByText('评估中任务')).length).toBeGreaterThanOrEqual(1)
    expect((await screen.findAllByText('未知状态任务')).length).toBeGreaterThanOrEqual(1)
    // 原始状态文本可见（细态不被 4 态文案吞掉）
    expect((await screen.findAllByText('evaluating')).length).toBeGreaterThanOrEqual(1)
    expect((await screen.findAllByText('pending_review')).length).toBeGreaterThanOrEqual(1)
  })

  it('展开详情含 state 真值行', async () => {
    renderWithProviders(<PipelineManagerWidget />)
    // 展开任务节点（TaskRow 点击=toggle），再点其下条目行首的 chevron（行主体点击是"打开对话"）
    fireEvent.click((await screen.findAllByText('评估中任务'))[0])
    const afterNode = await screen.findAllByText('评估中任务')
    for (const el of afterNode.slice(1)) {
      const row = el.closest('div')
      const chevron = row?.querySelector('button')
      if (chevron) fireEvent.click(chevron)
    }
    await waitFor(() => expect(screen.getAllByText('State 状态').length).toBeGreaterThanOrEqual(1))
    expect(screen.getAllByText('已结束').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('当前阶段').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('消息条数').length).toBeGreaterThanOrEqual(1)
    // state['task.status'] = completed 的真值出现在详情中
    expect(screen.getAllByText('completed').length).toBeGreaterThanOrEqual(1)
  })

  it('子任务按父管道 id 挂到主管道节点下（树形而非顶层平铺）', async () => {
    seed.mockUseAllTasksQuery.mockReturnValue({ data: seed.SUBTASK_TASKS })
    renderWithProviders(<PipelineManagerWidget />)
    const subtaskRows = await screen.findAllByText('子任务')
    // 任务节点行（TaskRow）至少一个
    const taskRow = subtaskRows.find((el) => el.closest('div')?.className.includes('hover:bg-accent'))
    expect(taskRow).toBeDefined()
    // 子任务行带缩进（depth>0 的 paddingLeft），且出现在主管道行的同一子树容器内
    const padding = taskRow?.closest('div')?.getAttribute('style') ?? ''
    expect(padding).toMatch(/padding-left:\s*2[48]px/)
  })

  it('子任务（parent_task_id=会话主管道条目）渲染为该条目的子节点', async () => {
    seed.mockUsePipelineRunsQuery.mockReturnValue({ data: seed.SESSION_MAIN_RUNS })
    seed.mockUseAllTasksQuery.mockReturnValue({ data: seed.SESSION_MAIN_TASKS })
    renderWithProviders(<PipelineManagerWidget />)
    // 主管道条目（kind=session）+ 子任务条目行都渲染（此前子任务整体丢失）
    expect((await screen.findAllByText('子任务A')).length).toBeGreaterThanOrEqual(1)
    // 子任务条目行带缩进（depth>0，挂主管道条目下而非顶层平铺）
    const subtaskRow = (await screen.findAllByText('子任务A')).find((el) =>
      el.closest('div')?.className.includes('hover:bg-accent'),
    )
    expect(subtaskRow).toBeDefined()
    const padding = subtaskRow?.closest('div')?.getAttribute('style') ?? ''
    expect(padding).toMatch(/padding-left:\s*2[48]px/)
  })

  it('挂会话主管道下的非任务子管道仍渲染（防回归）', async () => {
    seed.mockUsePipelineRunsQuery.mockReturnValue({ data: seed.SESSION_MAIN_RUNS })
    renderWithProviders(<PipelineManagerWidget />)
    // 会话列表 mock 为空 → 条目名回退"会话 th-1"（主管道 + 子管道同名）
    const rows = await screen.findAllByText('会话 th-1')
    expect(rows.length).toBeGreaterThanOrEqual(1)
    // 至少一行带缩进（depth>0 = 挂主管道条目下的直接子管道；主管道本身 depth=0 无缩进）
    const indented = rows.some(
      (el) => el.closest('div')?.getAttribute('style')?.match(/padding-left:\s*2[48]px/),
    )
    expect(indented).toBe(true)
  })

  it('任务节点渲染打开工作空间按钮并开 workspace 文件树标签（0.1 对齐）', async () => {
    seed.mockUseAllTasksQuery.mockReturnValue({ data: seed.WS_TASKS })
    renderWithProviders(<PipelineManagerWidget />)
    // workspacePath 取自 metadata.ws_meta.path；按钮 title 带完整路径
    const btns = await screen.findAllByTitle('打开工作空间: D:/ws/copy_1')
    expect(btns.length).toBeGreaterThanOrEqual(1)
    fireEvent.click(btns[0])
    const tabs = useLayoutModeStore.getState().workspaceTabs
    const tab = tabs.find((t) => t.dataSource === 'workspace://subPipe')
    expect(tab).toBeDefined()
    expect(tab?.component).toBe('file_tree')
    expect(tab?.title).toBe('带空间的子任务')
  })
})

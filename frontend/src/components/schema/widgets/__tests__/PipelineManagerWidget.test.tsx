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
import { renderWithProviders } from '@/test/renderWithProviders'
import { describe, expect, it, vi, beforeEach } from 'vitest'

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
  const mockUseAllTasksQuery = vi.fn(() => ({ data: FAKE_ALL_TASKS }))
  return { FAKE_RUNS, FAKE_STATES, FAKE_ALL_TASKS, SUBTASK_TASKS, mockUseAllTasksQuery }
})

vi.mock('@/hooks/queries/usePipelineRunsQuery', () => ({
  usePipelineRunsQuery: () => ({ data: seed.FAKE_RUNS }),
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

import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'

describe('PipelineManagerWidget', () => {
  beforeEach(() => {
    // 默认播种全量任务；子任务用例覆盖为 SUBTASK_TASKS
    seed.mockUseAllTasksQuery.mockReturnValue({ data: seed.FAKE_ALL_TASKS })
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
})

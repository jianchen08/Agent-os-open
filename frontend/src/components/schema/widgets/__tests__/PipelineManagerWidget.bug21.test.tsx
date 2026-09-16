// @feature FP-T12 前端适配 | @ci frontend-test
/**
 * PipelineManagerWidget BUG-21 回归测试（面板幽灵「执行中」+ 全面板假 0ms）：
 *
 * R47 实证（2026-09-15 22:12，dev + kernel DB）：runs 表零 running，面板仍显示
 * 13 条「任务:待执行 | 运行中 | 0ms」；最近完成条目时长全为假 0ms。根因形态：
 * - 未起跑任务管道（task_submit 出生后从未进消息循环）无 message_slots、无
 *   checkpoint → 内核 /pipelines/state 冷兜底不出口该行 → 第 2 源合并拿不到
 *   任何运行证据，陈旧 task.status=pending 被映射成 running 归入执行中；
 * - 任务端点（state 聚合）无时间戳真值，前端以渲染时刻兜底 startedAt →
 *   所有任务派生条目耗时 ≈ 0ms。
 *
 * 契约（runs 权威对账）：runs/state 双面均无行的管道，任务域未决态不猜
 * running（落 unknown 视图态）；无时间真值不产假 0ms（耗时列 '--'）。
 */

import { screen } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'

/** 测试播种数据（可变单例：beforeEach 清空，用例自行填充） */
const seed = vi.hoisted(() => {
  const runs: Record<string, Record<string, unknown>> = {}
  const states: Record<string, Record<string, unknown>> = {}
  const tasks: Record<string, unknown>[] = []
  return {
    runs,
    states,
    tasks,
    mockFetchProjects: vi.fn(() => Promise.resolve({ items: [] as unknown[] })),
    mockWorkspaceOpen: vi.fn(() => Promise.resolve({ data: { success: true } })),
    mockNavigate: vi.fn(() => Promise.resolve(true)),
    mockInvalidate: vi.fn(),
    mockReadSessions: vi.fn(() => [] as Record<string, unknown>[]),
    mockEnsureSessions: vi.fn(() => Promise.resolve([] as unknown[])),
    mockUseRuns: vi.fn(() => ({ data: runs })),
    mockUseStates: vi.fn(() => ({ data: states })),
    mockUseTasks: vi.fn(() => ({ data: tasks })),
    mockUseSessions: vi.fn(() => ({ data: [] })),
  }
})

vi.mock('@/services/api/tasks', () => ({
  pauseTask: vi.fn(),
  resumeTask: vi.fn(),
  cancelTask: vi.fn(),
  deleteProject: vi.fn(),
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
  useContextUsageStore: (sel: (s: unknown) => unknown) => sel({ usageByPipeline: {} }),
}))
vi.mock('@/stores/sessionListStore', () => ({
  useSessionListStore: {
    getState: () => ({
      setActiveSession: vi.fn(() => Promise.resolve()),
    }),
  },
}))

import { renderWithProviders } from '@/test/renderWithProviders'

/** 播种一条「runs 窗口外 + state 行缺席」的任务（BUG-21 幽灵形态；任务载荷
 *  对齐 task_service 实际响应：无 timestamps 对象，created_at/updated_at 顶层
 *  字段——聚合行当前恒为空串） */
function seedUncoveredTask(
  taskStatus: string,
  extra: Record<string, unknown> = {},
) {
  seed.tasks.push({
    id: 'stalePipe',
    title: '陈旧待执行任务',
    status: taskStatus,
    pipeline_run_id: 'stalePipe',
    agent_name: 'general_agent',
    created_at: '',
    updated_at: '',
    ...extra,
  })
}

describe('PipelineManagerWidget BUG-21 无运行证据不猜 running', () => {
  beforeEach(() => {
    for (const k of Object.keys(seed.runs)) delete seed.runs[k]
    for (const k of Object.keys(seed.states)) delete seed.states[k]
    seed.tasks.length = 0
  })

  it('待执行任务 runs/state 双面无行：不归入执行中分组，状态落未知', async () => {
    seedUncoveredTask('pending')
    renderWithProviders(<PipelineManagerWidget />)
    expect(await screen.findByText('陈旧待执行任务')).toBeInTheDocument()
    expect(screen.queryByText('执行中的管道')).toBeNull()
    expect(screen.getByText('最近完成')).toBeInTheDocument()
    expect(screen.getAllByTitle(/运行状态：未知/).length).toBeGreaterThanOrEqual(1)
  })

  it('陈旧 running 任务同款：无任何运行证据不按任务投影归执行中', async () => {
    seedUncoveredTask('running')
    renderWithProviders(<PipelineManagerWidget />)
    expect(await screen.findByText('陈旧待执行任务')).toBeInTheDocument()
    expect(screen.queryByText('执行中的管道')).toBeNull()
  })

  it('对照：state 行在场 run_status=running（真实在飞）仍归执行中分组', async () => {
    seedUncoveredTask('running')
    seed.states.stalePipe = {
      pipeline_id: 'stalePipe',
      thread_id: 'th-stale',
      source: 'memory',
      state: { run_status: 'running' },
    }
    renderWithProviders(<PipelineManagerWidget />)
    expect(await screen.findByText(/执行中的管道/)).toBeInTheDocument()
    expect(screen.getByText('陈旧待执行任务')).toBeInTheDocument()
  })
})

describe('PipelineManagerWidget BUG-21 无时间真值不产假 0ms', () => {
  beforeEach(() => {
    for (const k of Object.keys(seed.runs)) delete seed.runs[k]
    for (const k of Object.keys(seed.states)) delete seed.states[k]
    seed.tasks.length = 0
  })

  it('任务载荷无时间戳真值（聚合行空串）：耗时列不显示假 0ms', async () => {
    seedUncoveredTask('completed')
    renderWithProviders(<PipelineManagerWidget />)
    expect(await screen.findByText('陈旧待执行任务')).toBeInTheDocument()
    expect(screen.queryByText('0ms')).toBeNull()
    // 无真值 = 耗时缺失态 '--'（token 列同为 '--'，取至少一处即可）
    expect(screen.getAllByText('--').length).toBeGreaterThanOrEqual(1)
  })

  it('载荷带 created_at/updated_at 真值（终态任务）：显示真实区间非 0ms', async () => {
    seedUncoveredTask('completed', {
      created_at: '2026-09-14T07:50:00Z',
      updated_at: '2026-09-14T07:53:05Z',
    })
    renderWithProviders(<PipelineManagerWidget />)
    expect(await screen.findByText('陈旧待执行任务')).toBeInTheDocument()
    expect(screen.getByText('3m 5s')).toBeInTheDocument()
    expect(screen.queryByText('0ms')).toBeNull()
  })
})

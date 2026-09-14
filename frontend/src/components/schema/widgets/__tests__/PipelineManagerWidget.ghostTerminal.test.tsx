// @feature FP-T12 前端适配 | @ci frontend-test
/**
 * PipelineManagerWidget 合并第 2 源终态防御测试（BUG-2b 面板幽灵残留，独立
 * 文件与既有 PipelineManagerWidget.test / .gaps.test 互补不重复）：
 *
 * 任务派生条目（runs 快照窗口外、仅任务列表可见的管道）合并时，state 视图
 * 已持 runs 权威终态（冷行 run_status overlay / reap 投影）的行不再按陈旧
 * task.status 投影归入「执行中」分组——终态优先，崩溃遗留不再显示为幽灵执行中。
 */

import { render, screen } from '@testing-library/react'
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

/** 播种一条「runs 窗口外 + state 行在场」的崩溃残留任务（BUG-2b 幽灵形态） */
function seedGhostTask(taskStatus: string, runStatus: string | undefined) {
  seed.tasks.push({
    id: 'ghostPipe',
    title: '崩溃残留任务',
    status: taskStatus,
    pipeline_run_id: 'ghostPipe',
    agent_name: 'general_agent',
  })
  seed.states.ghostPipe = {
    pipeline_id: 'ghostPipe',
    thread_id: 'th-ghost',
    source: 'checkpoint',
    state: runStatus === undefined ? { 'task.status': taskStatus } : { run_status: runStatus },
  }
}

describe('PipelineManagerWidget 合并第 2 源终态防御（BUG-2b）', () => {
  beforeEach(() => {
    for (const k of Object.keys(seed.runs)) delete seed.runs[k]
    for (const k of Object.keys(seed.states)) delete seed.states[k]
    seed.tasks.length = 0
  })

  it('state 视图持权威 completed 而任务投影残留 running：不归入执行中分组', async () => {
    seedGhostTask('running', 'completed')
    renderWithProviders(<PipelineManagerWidget />)
    // 条目仍在（落到最近完成组），但「执行中的管道」分组不再为幽灵渲染
    expect(await screen.findByText('崩溃残留任务')).toBeInTheDocument()
    expect(screen.queryByText('执行中的管道')).toBeNull()
    expect(screen.getByText('最近完成')).toBeInTheDocument()
  })

  it('state 视图持权威 cancelled 而任务投影残留 running：同款终态防御', async () => {
    seedGhostTask('running', 'cancelled')
    renderWithProviders(<PipelineManagerWidget />)
    expect(await screen.findByText('崩溃残留任务')).toBeInTheDocument()
    expect(screen.queryByText('执行中的管道')).toBeNull()
    expect(screen.getByText('最近完成')).toBeInTheDocument()
  })

  it('state 视图持权威 failed 而任务投影残留 pending：同款终态防御', async () => {
    seedGhostTask('pending', 'failed')
    renderWithProviders(<PipelineManagerWidget />)
    expect(await screen.findByText('崩溃残留任务')).toBeInTheDocument()
    expect(screen.queryByText('执行中的管道')).toBeNull()
    expect(screen.getByText('最近完成')).toBeInTheDocument()
  })

  it('对照：state 视图 run_status=running（真实在飞）仍归执行中分组', async () => {
    seedGhostTask('running', 'running')
    renderWithProviders(<PipelineManagerWidget />)
    expect(await screen.findByText(/执行中的管道/)).toBeInTheDocument()
    expect(screen.getByText('崩溃残留任务')).toBeInTheDocument()
  })

  it('对照：无 state 行（runs 窗口外纯任务源）行为不变，仍按任务投影归执行中', async () => {
    seedGhostTask('running', undefined)
    delete seed.states.ghostPipe
    renderWithProviders(<PipelineManagerWidget />)
    expect(await screen.findByText(/执行中的管道/)).toBeInTheDocument()
    expect(screen.getByText('崩溃残留任务')).toBeInTheDocument()
  })
})

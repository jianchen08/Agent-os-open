// @feature: FP-T12 前端适配 | @ci: frontend-test
/**
 * 管道状态传播组件测试：下级运行中 → 上级显示「下辖运行中」，不因上级自身挂起误判为未运行。
 *
 * 锁三层链（顶层→中间层→叶子）：叶子运行中，顶层与中间层自身 stopped（挂起）。
 * 断言口径（面板真实可观察面）：
 * - 中间层与顶层行均渲染「下辖运行中」徽标（data-testid=pipeline-entry-subtask-running）；
 * - 两行行首状态图标 data-status-kind ≠ 'paused'（挂起但链在跑 ≠ 已暂停图标）；
 * - 叶子行自身显示「运行中」且不挂「下辖运行中」徽标（自身在跑与下级在跑二义分离）。
 *
 * 真实界面核验（自造三层链入 dev 库 → 浏览器实测三行徽标 + 截图）见
 * docs/working/bb7297d792b4_树双渲染与状态传播修复报告.md。
 * 本用例把同一行为固化为 CI 可回归的组件级断言。
 */
import { fireEvent, screen } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
// 顺序约束：先于被测组件 import——vi.mock 工厂体在被测组件初始化时执行，
// 届时本模块必须已求值（见 pmTestUtils 头注）。import-x/order 要求 '@/…'
// 组先于 './…' 组，与本约束相反，故在被测组件那一行禁用排序（禁用排序
// 自动修防回归：自动改序会破坏 vi.mock 工厂求值时序）。
import { pmMod, pmSeed, renderPmAllStatuses, resetPmContainers } from './pmTestUtils'
// eslint-disable-next-line import-x/order -- pmTestUtils 须先于被测组件求值（vi.mock 工厂时序约束，见 pmTestUtils 头注；禁用排序自动修防回归）
import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'

vi.mock('@/services/api/tasks', () => pmMod.tasksApi())
vi.mock('@/services/api/client', () => pmMod.apiClient())
vi.mock('@/services/pipelineNavigator', () => pmMod.pipelineNavigator())
vi.mock('@/hooks/queries/usePipelineRunsQuery', () => pmMod.pipelineRunsQuery())
vi.mock('@/hooks/queries/useAllTasksQuery', () => pmMod.allTasksQuery())
vi.mock('@/hooks/queries/useLongTermTasksQuery', () => pmMod.longTermTasksQuery())
vi.mock('@/hooks/queries/useSessionsQuery', () => pmMod.sessionsQuery())
vi.mock('@/hooks/queries/useAgentsQuery', () => pmMod.agentsQuery())
vi.mock('@/stores/contextUsageStore', () => pmMod.contextUsageStore())
vi.mock('@/stores/sessionListStore', () => pmMod.sessionListStore())

/** 播种三层链：顶层(挂起) → 中间层(挂起) → 叶子(运行中) */
function seedThreeLevelChain() {
  pmSeed.runs.rTop = {
    pipeline_id: 'pipe-top', run_id: 'run-top', title: '传播链·顶层',
    status: 'suspended', started_at: '2026-11-20T10:00:00Z',
  }
  pmSeed.runs.rMid = {
    pipeline_id: 'pipe-mid', run_id: 'run-mid', title: '传播链·中间层',
    status: 'suspended', started_at: '2026-11-20T10:01:00Z',
  }
  pmSeed.runs.rLeaf = {
    pipeline_id: 'pipe-leaf', run_id: 'run-leaf', title: '传播链·叶子',
    status: 'running', started_at: '2026-11-20T10:02:00Z',
  }
  pmSeed.tasks.push(
    { id: 't-top', title: '传播链·顶层', status: 'stopped', pipeline_run_id: 'pipe-top' },
    {
      id: 't-mid', title: '传播链·中间层', status: 'stopped',
      pipeline_run_id: 'pipe-mid', parent_task_id: 't-top',
    },
    {
      id: 't-leaf', title: '传播链·叶子', status: 'running',
      pipeline_run_id: 'pipe-leaf', parent_task_id: 't-mid',
    },
  )
}

/** 条目行（data-entry-key=管道 ID）：按管道 ID 精确定位，不受同名文本歧义影响 */
function entryRow(pipelineId: string): HTMLElement {
  const row = document.querySelector(`div[data-entry-key="${pipelineId}"]`) as HTMLElement | null
  expect(row, `entry row for ${pipelineId}`).toBeTruthy()
  return row as HTMLElement
}

/** 该行「下辖运行中」徽标文本（无徽标返回 null） */
function subtaskRunningBadge(pipelineId: string): string | null {
  const badge = entryRow(pipelineId).querySelector('[data-testid="pipeline-entry-subtask-running"]')
  return badge?.textContent?.trim() ?? null
}

/** 该行行首状态图标种类（data-status-kind） */
function statusIconKind(pipelineId: string): string | null {
  const icon = entryRow(pipelineId).querySelector('[data-testid="pipeline-entry-status-icon"]')
  return icon?.getAttribute('data-status-kind') ?? null
}

describe('管道状态传播：下级运行中 → 上级显示「下辖运行中」', () => {
  beforeEach(() => {
    resetPmContainers()
    pmSeed.fetchProjects.mockResolvedValue({ items: [] })
  })

  it('中间层与顶层均显示「下辖运行中」且状态图标不是「已暂停」；叶子自身显示运行中', async () => {
    seedThreeLevelChain()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('传播链·顶层')
    // 树默认收起：子层行不进 DOM。关键词命中时面板整体展开（同一真实用户路径）——
    // 「传播链」三行同名前缀全部命中，展开后三行可断言。
    fireEvent.change(screen.getByLabelText('搜索管道'), { target: { value: '传播链' } })
    await screen.findAllByText('传播链·叶子')

    // 上级自身挂起但链内有运行中子孙 → 徽标补位（不因自身挂起误判为未运行）
    expect(subtaskRunningBadge('pipe-mid')).toBe('下辖运行中')
    expect(subtaskRunningBadge('pipe-top')).toBe('下辖运行中')

    // 挂起 ≠ 已暂停图标：等待子任务（活跃等待）归运行中视觉
    expect(statusIconKind('pipe-mid')).not.toBe('paused')
    expect(statusIconKind('pipe-top')).not.toBe('paused')
    expect(statusIconKind('pipe-mid')).toBe('running')
    expect(statusIconKind('pipe-top')).toBe('running')

    // 自身在跑的叶子显示运行中且不挂「下辖运行中」徽标（两义分离）
    expect(subtaskRunningBadge('pipe-leaf')).toBeNull()
    expect(statusIconKind('pipe-leaf')).toBe('running')
  })
})

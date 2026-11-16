// @feature FP-T12 前端适配 | @ci: frontend-test
/**
 * 任务失败归因透出整链测试（P-21）
 *
 * 数据链：state 摘要（task.failure_*）→ 适配层视图模型 → 面板条目 → 详情卡
 * 「失败归因」行。锁两面：
 * - 归因在场：分类（人话（原词））+ 全文（后端已译文本原样）单行渲染；
 * - 字段缺失回退现状：只渲染既有「停止原因」行，不伪造归因行。
 */

import { fireEvent, screen } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { pmMod, pmSeed, renderPmAllStatuses, resetPmContainers } from './pmTestUtils'
// eslint-disable-next-line import-x/order -- mock 装配时序约束：pmTestUtils 求值序承载 vi.mock 工厂依赖（工厂体在被测组件初始化期执行，届时 pmSeed 必须已在场），排序偏好让位于 mock 正确性
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
vi.mock('@/services/api/modes', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useModesRegistry: () => pmSeed.modesRegistry,
}))

/** 播种一条 failed 任务（runs + state 摘要 + 任务行三方对齐） */
function seedFailedTask(state: Record<string, unknown>) {
  pmSeed.runs.r1 = {
    pipeline_id: 'pipe-f',
    run_id: 'run-f',
    thread_id: 'th-f',
    title: '失败任务',
    status: 'failed',
    started_at: '2026-10-29T00:00:00Z',
  }
  pmSeed.states['pipe-f'] = {
    pipeline_id: 'pipe-f',
    thread_id: 'th-f',
    source: 'memory',
    state,
  }
  pmSeed.tasks.push({
    id: 't-f',
    title: '失败任务',
    status: 'failed',
    pipeline_run_id: 'pipe-f',
  })
}

/** 渲染并展开首行详情 */
async function openDetail() {
  renderPmAllStatuses(<PipelineManagerWidget />)
  const title = (await screen.findAllByText('失败任务'))[0]
  fireEvent.click(title)
  await screen.findByText('管道 ID')
}

beforeEach(() => {
  resetPmContainers()
})

describe('任务失败归因详情行（P-21 整链）', () => {
  it('归因在场：分类（人话（原词））+ 全文（后端已译文本）单行渲染', async () => {
    seedFailedTask({
      run_status: 'failed',
      run_stop_reason: 'llm_fail_loop',
      'task.failure_class': 'run_stopped',
      'task.failure_reason': '运行终止（LLM 连续同类失败循环）',
    })
    await openDetail()

    const row = screen.getByText('失败归因').closest('div')!
    expect(row.textContent).toBe(
      '失败归因运行终止（run_stopped）：运行终止（LLM 连续同类失败循环）',
    )
  })

  it('仅分类在场（无全文）：只渲染分类文案', async () => {
    seedFailedTask({
      run_status: 'failed',
      'task.failure_class': 'run_aborted',
    })
    await openDetail()

    const row = screen.getByText('失败归因').closest('div')!
    expect(row.textContent).toBe('失败归因原因未记录（run_aborted）')
  })

  it('字段缺失回退现状：只渲染停止原因行，不渲染失败归因行', async () => {
    seedFailedTask({ run_status: 'failed', run_stop_reason: 'llm_fail_loop' })
    await openDetail()

    expect(screen.queryByText('失败归因')).toBeNull()
    expect(screen.getByText('停止原因')).toBeInTheDocument()
  })
})

describe('挂起归因透传整链（P-23 片2 补刀：state → mapper → 行构建 → 渲染）', () => {
  /** 播种一条 suspended 任务（runs + state 摘要 + 任务行三方对齐） */
  function seedSuspendedTask(state: Record<string, unknown>) {
    pmSeed.runs['r-s'] = {
      pipeline_id: 'pipe-s',
      run_id: 'run-s',
      thread_id: 'th-s',
      title: '挂起任务',
      status: 'suspended',
      started_at: '2026-11-04T00:00:00Z',
    }
    pmSeed.states['pipe-s'] = {
      pipeline_id: 'pipe-s',
      thread_id: 'th-s',
      source: 'memory',
      state,
    }
    pmSeed.tasks.push({
      id: 't-s',
      title: '挂起任务',
      status: 'stopped',
      pipeline_run_id: 'pipe-s',
    })
  }

  /** 渲染并展开挂起任务行详情（共享 openDetail 硬编码「失败任务」标题） */
  async function openSuspendedDetail() {
    renderPmAllStatuses(<PipelineManagerWidget />)
    const title = (await screen.findAllByText('挂起任务'))[0]
    fireEvent.click(title)
    await screen.findByText('管道 ID')
  }

  it('suspended_reason 在场：详情「挂起原因」行 = 人话（原词）', async () => {
    seedSuspendedTask({
      run_status: 'suspended',
      suspended_reason: 'memory watermark self-heal: private bytes 持续超阈值',
    })
    await openSuspendedDetail()

    const row = screen.getByText('挂起原因').closest('div')
    expect(row?.textContent).toBe(
      '挂起原因内存水位自愈（memory watermark self-heal: private bytes 持续超阈值）',
    )
  })

  it('suspended_reason 缺席：不渲染挂起原因行（回退现状不伪造）', async () => {
    seedSuspendedTask({ run_status: 'suspended' })
    await openSuspendedDetail()

    expect(screen.queryByText('挂起原因')).toBeNull()
  })
})

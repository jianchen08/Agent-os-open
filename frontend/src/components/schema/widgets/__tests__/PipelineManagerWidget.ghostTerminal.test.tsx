// @feature FP-T12 前端适配 | @ci frontend-test
/**
 * PipelineManagerWidget 合并第 2 源终态防御测试（BUG-2b 面板幽灵残留，独立
 * 文件与既有 PipelineManagerWidget.test / .gaps.test 互补不重复）：
 *
 * 任务派生条目（runs 快照窗口外、仅任务列表可见的管道）合并时，state 视图
 * 已持 runs 权威终态（冷行 run_status overlay / reap 投影）的行不再按陈旧
 * task.status 投影归入「执行中」分组——终态优先，崩溃遗留不再显示为幽灵执行中。
 */

import { screen } from '@testing-library/react'
import { describe, expect, it, beforeEach } from 'vitest'
// 顺序约束：先于被测组件 import——vi.mock 工厂体在被测组件初始化时执行，
// 届时本模块必须已求值（见 pmTestUtils 头注）
import { pmMod, pmSeed, renderPmAllStatuses, resetPmContainers } from './pmTestUtils'
import { renderWithProviders } from '@/test/renderWithProviders'
import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'

// 渲染专用文件只触 query 读面：任务域端点经 useProjectsQuery 只走 fetchProjects，
// 暂停/取消/导航/工作空间/实时 usage 均不进入渲染路径（真实模块惰性加载等价）。
vi.mock('@/services/api/tasks', () => pmMod.tasksApi())
vi.mock('@/hooks/queries/usePipelineRunsQuery', () => pmMod.pipelineRunsQuery())
vi.mock('@/hooks/queries/useAllTasksQuery', () => pmMod.allTasksQuery())
vi.mock('@/hooks/queries/useSessionsQuery', () => pmMod.sessionsQuery())

/** 播种一条「runs 窗口外 + state 行在场」的崩溃残留任务（BUG-2b 幽灵形态） */
function seedGhostTask(taskStatus: string, runStatus: string | undefined) {
  pmSeed.tasks.push({
    id: 'ghostPipe',
    title: '崩溃残留任务',
    status: taskStatus,
    pipeline_run_id: 'ghostPipe',
    agent_name: 'general_agent',
  })
  pmSeed.states.ghostPipe = {
    pipeline_id: 'ghostPipe',
    thread_id: 'th-ghost',
    source: 'checkpoint',
    state: runStatus === undefined ? { 'task.status': taskStatus } : { run_status: runStatus },
  }
}

/** 终态防御公共序列：播种幽灵任务 → 全量视野渲染 → 条目落「最近完成」组
 *  且「执行中的管道」分组不渲染 */
async function expectGhostLandsCompleted(taskStatus: string, runStatus: string) {
  seedGhostTask(taskStatus, runStatus)
  renderPmAllStatuses(<PipelineManagerWidget />)
  expect(await screen.findByText('崩溃残留任务')).toBeInTheDocument()
  expect(screen.queryByText('执行中的管道')).toBeNull()
  expect(screen.getByText('最近完成')).toBeInTheDocument()
}

describe('PipelineManagerWidget 合并第 2 源终态防御（BUG-2b）', () => {
  beforeEach(resetPmContainers)

  it('state 视图持权威 completed 而任务投影残留 running：不归入执行中分组', async () => {
    await expectGhostLandsCompleted('running', 'completed')
  })

  it('state 视图持权威 cancelled 而任务投影残留 running：同款终态防御', async () => {
    await expectGhostLandsCompleted('running', 'cancelled')
  })

  it('state 视图持权威 failed 而任务投影残留 pending：同款终态防御', async () => {
    await expectGhostLandsCompleted('pending', 'failed')
  })

  it('对照：state 视图 run_status=running（真实在飞）仍归执行中分组', async () => {
    seedGhostTask('running', 'running')
    renderWithProviders(<PipelineManagerWidget />)
    expect(await screen.findByText(/执行中的管道/)).toBeInTheDocument()
    expect(screen.getByText('崩溃残留任务')).toBeInTheDocument()
  })

  it('对照：无 state 行（runs 窗口外纯任务源）× 未决任务态：无运行证据落未知不归执行中（BUG-21）', async () => {
    seedGhostTask('running', undefined)
    delete pmSeed.states.ghostPipe
    renderPmAllStatuses(<PipelineManagerWidget />)
    expect(await screen.findByText('崩溃残留任务')).toBeInTheDocument()
    expect(screen.queryByText('执行中的管道')).toBeNull()
    expect(screen.getAllByTitle(/运行状态：未知/).length).toBeGreaterThanOrEqual(1)
  })
})

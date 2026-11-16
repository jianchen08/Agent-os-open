// @feature FP-T12 前端适配 | @ci frontend-test
/**
 * 任务面板显示聚合 + 「暂停」语义组件测试（设计 §4.5 一词一义，ADR 2026-10-30-
 * task-status-authority-vocabulary-convergence 决策 7）。
 *
 * 锁两面：
 * - 显示聚合：任务行状态 = (task.status, 子任务挂号) 折算——stopped×挂号非空
 *   → 等待子任务；stopped×挂号空 → 已暂停（可恢复）；legacy cancelled 过渡
 *   映射 → 已暂停（可恢复）；
 * - 按钮语义：原「取消」按钮改标「暂停」（接口不变），点击弹确认弹窗
 *   （文案说明「任务将暂停，可恢复」），确认后才调原 cancel 端点；
 *   弹窗「取消」不触发端点。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
// 顺序约束：先于被测组件 import——vi.mock 工厂体在被测组件初始化时执行，
// 届时本模块必须已求值（见 pmTestUtils 头注）
// eslint-disable-next-line import-x/order -- pmTestUtils 须先于被测组件求值（vi.mock 工厂时序约束，见 pmTestUtils 头注；禁用排序自动修防回归）
import { pmMod, pmSeed, renderPmAllStatuses, resetPmContainers } from './pmTestUtils'
import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'
import { useSessionStore } from '@/stores/sessionStore'

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
// 确认失败反馈断言面：toast（操作点可见）+ 弹窗留驻
const toastMocks = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), warning: vi.fn(), info: vi.fn() }))
vi.mock('@/components/ui/sonner', () => ({ toast: toastMocks }))

/** 播种显示聚合三态：等待子任务（stopped×挂号非空）/ 已暂停（stopped×挂号空）/
 *  cancelled 过渡遗留 */
function seedDisplayAggregation() {
  pmSeed.runs.rWait = {
    pipeline_id: 'pipe-wait', run_id: 'run-wait', title: '等待任务',
    status: 'suspended', started_at: '2026-10-30T00:00:00Z',
  }
  pmSeed.runs.rStop = {
    pipeline_id: 'pipe-stop', run_id: 'run-stop', title: '暂停任务',
    status: 'suspended', started_at: '2026-10-30T00:01:00Z',
  }
  pmSeed.runs.rChild = {
    pipeline_id: 'pipe-child', run_id: 'run-child', title: '子任务甲',
    status: 'running', started_at: '2026-10-30T00:02:00Z',
  }
  pmSeed.tasks.push(
    { id: 'p-wait', title: '等待任务', status: 'stopped', pipeline_run_id: 'pipe-wait' },
    { id: 'p-stop', title: '暂停任务', status: 'stopped', pipeline_run_id: 'pipe-stop' },
    // 未结算直接子任务（回执未写）⇔ 父挂号非空
    { id: 'c-run', title: '子任务甲', status: 'running', pipeline_run_id: 'pipe-child', parent_task_id: 'p-wait' },
    // legacy cancelled（后端开机迁移前的过渡期旧值）
    { id: 'p-cancel', title: '取消遗留任务', status: 'cancelled', pipeline_run_id: 'pipe-cancel' },
  )
}

/** 播种运行中任务（按钮语义用例） */
function seedRunningTask() {
  pmSeed.runs.r1 = {
    pipeline_id: 'pipe-run', run_id: 'run-1', title: '运行任务',
    status: 'running', started_at: '2026-10-30T00:03:00Z',
  }
  pmSeed.tasks.push(
    { id: 't-run', title: '运行任务', status: 'running', pipeline_run_id: 'pipe-run' },
  )
}

/** 定位含指定条目名的行内按钮（树行=div） */
function rowButton(entryName: string, selector: string): HTMLElement {
  const el = screen.getAllByText(entryName)[0]
  const row = (el.closest('tr') ?? el.closest('div')) as HTMLElement
  const btn = row.querySelector(selector) as HTMLElement | null
  expect(btn, `row button ${selector} for ${entryName}`).toBeTruthy()
  return btn as HTMLElement
}

/** pmSeed 单例句柄跨用例不清调用历史（resetPmContainers 只清容器），动作族
 *  断言前统一清零防串扰 */
function clearActionMocks() {
  pmSeed.cancelTask.mockClear()
  pmSeed.pauseTask.mockClear()
  pmSeed.resumeTask.mockClear()
  pmSeed.invalidateLongTermTasks.mockClear()
}

describe('任务面板显示聚合（§4.5 一词一义）', () => {
  beforeEach(() => {
    resetPmContainers()
    clearActionMocks()
    pmSeed.fetchProjects.mockResolvedValue({ items: [] })
    pmSeed.workspaceOpen.mockResolvedValue({ data: { success: true } })
    useSessionStore.setState({ activeSessionId: null })
  })

  it('stopped × 挂号非空 → 等待子任务；stopped × 挂号空 → 已暂停（可恢复）', async () => {
    seedDisplayAggregation()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('等待任务')
    expect(screen.getAllByTitle(/运行状态：等待子任务/).length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByTitle(/运行状态：已暂停（可恢复）/).length).toBeGreaterThanOrEqual(2)
  })

  it('legacy cancelled 过渡防御：显示已暂停（可恢复），不再出现「已取消」', async () => {
    seedDisplayAggregation()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('取消遗留任务')
    const row = screen.getAllByText('取消遗留任务')[0].closest('div') as HTMLElement
    expect(row.querySelector('[data-testid="pipeline-entry-status"]')?.textContent).toBe(
      '已暂停（可恢复）',
    )
  })
})

describe('「取消」操作改标「暂停」（接口不变 + 确认弹窗可恢复措辞）', () => {
  beforeEach(() => {
    resetPmContainers()
    clearActionMocks()
    pmSeed.fetchProjects.mockResolvedValue({ items: [] })
    pmSeed.workspaceOpen.mockResolvedValue({ data: { success: true } })
    useSessionStore.setState({ activeSessionId: null })
  })

  it('行按钮标「暂停」→ 确认弹窗说明可恢复 → 确认后调原 cancel 端点并刷新任务列表', async () => {
    seedRunningTask()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    // 行按钮新标签（原「取消任务」退役）
    fireEvent.click(rowButton('运行任务', 'button[title="暂停"]'))
    // 确认弹窗措辞：任务将暂停，可恢复
    expect(await screen.findByText(/任务将暂停，可恢复/)).toBeInTheDocument()
    // 未确认前端点不动
    expect(pmSeed.cancelTask).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '确认暂停' }))
    await waitFor(() => expect(pmSeed.cancelTask).toHaveBeenCalledWith('t-run'))
    expect(pmSeed.invalidateLongTermTasks).toHaveBeenCalled()
    // 确认后弹窗关闭
    await waitFor(() =>
      expect(screen.queryByText(/任务将暂停，可恢复/)).not.toBeInTheDocument(),
    )
  })

  it('弹窗「取消」不触发端点（弹窗关闭，任务不动）', async () => {
    seedRunningTask()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    fireEvent.click(rowButton('运行任务', 'button[title="暂停"]'))
    fireEvent.click(await screen.findByRole('button', { name: '取消' }))
    await waitFor(() =>
      expect(screen.queryByText(/任务将暂停，可恢复/)).not.toBeInTheDocument(),
    )
    expect(pmSeed.cancelTask).not.toHaveBeenCalled()
  })

  it('Esc 关闭（onOpenChange false）与按钮「取消」同语义：不触发端点', async () => {
    seedRunningTask()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    fireEvent.click(rowButton('运行任务', 'button[title="暂停"]'))
    expect(await screen.findByText(/任务将暂停，可恢复/)).toBeInTheDocument()
    // Radix Dialog dismissable layer 监听 document keydown（确认中不可关的
    // 守卫在组件内：confirming 态下 onOpenChange false 被忽略）
    fireEvent.keyDown(document.body, { key: 'Escape' })
    await waitFor(() =>
      expect(screen.queryByText(/任务将暂停，可恢复/)).not.toBeInTheDocument(),
    )
    expect(pmSeed.cancelTask).not.toHaveBeenCalled()
    expect(pmSeed.invalidateLongTermTasks).not.toHaveBeenCalled()
  })

  it('确认失败（端点报错）→ toast 报错 + 弹窗留驻可重试，成功才关（诚实状态机）', async () => {
    seedRunningTask()
    pmSeed.cancelTask.mockRejectedValueOnce(new Error('端点故障'))
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    fireEvent.click(rowButton('运行任务', 'button[title="暂停"]'))
    fireEvent.click(await screen.findByRole('button', { name: '确认暂停' }))
    await waitFor(() => expect(pmSeed.cancelTask).toHaveBeenCalledWith('t-run'))
    // 失败反馈落在操作点：toast 带错误描述
    await waitFor(() =>
      expect(toastMocks.error).toHaveBeenCalledWith('暂停任务失败', { description: '端点故障' }),
    )
    // 弹窗留驻（不假装成功），确认态复位可重试
    expect(screen.getByText(/任务将暂停，可恢复/)).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '确认暂停' })).toBeEnabled(),
    )
    // 重试成功 → 弹窗关闭
    fireEvent.click(screen.getByRole('button', { name: '确认暂停' }))
    await waitFor(() =>
      expect(screen.queryByText(/任务将暂停，可恢复/)).not.toBeInTheDocument(),
    )
    expect(pmSeed.cancelTask).toHaveBeenCalledTimes(2)
  })
})

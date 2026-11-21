// @feature FP-T12 前端适配 | @ci frontend-test
/**
 * 任务面板显示聚合 + 删除管道语义组件测试（设计 §4.5 一词一义）。
 *
 * 锁两面：
 * - 显示聚合：任务行状态 = (task.status, 子任务挂号) 折算——stopped×挂号非空
 *   → 等待子任务；stopped×挂号空 → 已暂停（可恢复）；legacy cancelled 过渡
 *   映射 → 已暂停（可恢复）；
 * - 删除管道语义（2026-11-11「打叉」位改删除；方案 B 适配：删除入口=行「⋮」
 *   菜单「删除管道」项，任何状态可用）——点开确认弹窗（文案说明级联删除
 *   不可恢复、运行中先挂起再删除），确认后调 DELETE 任务端点级联删管道；
 *   弹窗「取消」不触发端点。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
// 顺序约束：先于被测组件 import——vi.mock 工厂体在被测组件初始化时执行，
// 届时本模块必须已求值（见 pmTestUtils 头注）
// eslint-disable-next-line import-x/order -- pmTestUtils 须先于被测组件求值（vi.mock 工厂时序约束，见 pmTestUtils 头注；禁用排序自动修防回归）
import { pmMod, pmSeed, renderPmAllStatuses, resetPmContainers } from './pmTestUtils'
import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'
import { useNotificationStore } from '@/stores/notificationStore'
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

/** 打开条目行「⋮」菜单并点「删除管道」→ 确认弹窗（方案 B：删除入口在菜单） */
async function openDeleteDialog(entryName: string): Promise<void> {
  const el = screen.getAllByText(entryName)[0]
  const key = el.closest('div[data-entry-key]')?.getAttribute('data-entry-key')
  expect(key, `entry key for ${entryName}`).toBeTruthy()
  fireEvent.pointerDown(screen.getByTestId(`pipeline-entry-more-${key}`))
  fireEvent.click(await screen.findByText('删除管道'))
}

/** pmSeed 单例句柄跨用例不清调用历史（resetPmContainers 只清容器），动作族
 *  断言前统一清零防串扰 */
function clearActionMocks() {
  pmSeed.cancelTask.mockClear()
  pmSeed.deleteTask.mockClear()
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

describe('「打叉」位改删除管道（2026-11-11 用户指令：确认弹窗 + DELETE 任务端点级联删管道）', () => {
  beforeEach(() => {
    resetPmContainers()
    clearActionMocks()
    pmSeed.fetchProjects.mockResolvedValue({ items: [] })
    pmSeed.workspaceOpen.mockResolvedValue({ data: { success: true } })
    useSessionStore.setState({ activeSessionId: null })
  })

  it('运行中任务行打叉按钮退役 → ⋮ 菜单「删除管道」开确认弹窗（级联措辞）→ 确认调 DELETE 任务端点并刷新', async () => {
    seedRunningTask()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    // 原打叉按钮（title=暂停）退役；删除入口在 ⋮ 菜单（方案 B）
    const row = screen.getAllByText('运行任务')[0].closest('div') as HTMLElement
    expect(row.querySelector('button[title="暂停"]')).toBeNull()
    await openDeleteDialog('运行任务')
    // 确认弹窗措辞：级联删除不可恢复 + 运行中先挂起再删除
    expect(await screen.findByText(/级联删除，不可恢复/)).toBeInTheDocument()
    expect(screen.getByText(/运行中的管道会先挂起再删除/)).toBeInTheDocument()
    // 未确认前端点不动
    expect(pmSeed.deleteTask).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '确认删除' }))
    await waitFor(() => expect(pmSeed.deleteTask).toHaveBeenCalledWith('t-run'))
    expect(pmSeed.invalidateLongTermTasks).toHaveBeenCalled()
    // 确认后弹窗关闭
    await waitFor(() =>
      expect(screen.queryByText(/级联删除，不可恢复/)).not.toBeInTheDocument(),
    )
  })

  it('弹窗「取消」不触发端点（弹窗关闭，管道不动）', async () => {
    seedRunningTask()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    await openDeleteDialog('运行任务')
    fireEvent.click(await screen.findByRole('button', { name: '取消' }))
    await waitFor(() =>
      expect(screen.queryByText(/级联删除，不可恢复/)).not.toBeInTheDocument(),
    )
    expect(pmSeed.deleteTask).not.toHaveBeenCalled()
  })

  it('Esc 关闭（onOpenChange false）与按钮「取消」同语义：不触发端点', async () => {
    seedRunningTask()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    await openDeleteDialog('运行任务')
    expect(await screen.findByText(/级联删除，不可恢复/)).toBeInTheDocument()
    // Radix Dialog dismissable layer 监听 document keydown（删除中不可关的
    // 守卫在组件内：deletingPipeline 态下 onOpenChange false 被忽略）
    fireEvent.keyDown(document.body, { key: 'Escape' })
    await waitFor(() =>
      expect(screen.queryByText(/级联删除，不可恢复/)).not.toBeInTheDocument(),
    )
    expect(pmSeed.deleteTask).not.toHaveBeenCalled()
    expect(pmSeed.invalidateLongTermTasks).not.toHaveBeenCalled()
  })

  it('删除失败（端点 false）→ 失败通知 + 弹窗留驻可重试，成功才关（诚实状态机）', async () => {
    seedRunningTask()
    pmSeed.deleteTask.mockResolvedValueOnce(false)
    const notifications: Array<{ title: string; message?: string }> = []
    const addSpy = vi
      .spyOn(useNotificationStore.getState(), 'addNotification')
      .mockImplementation((n) => {
        notifications.push(n)
        return n
      })
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    await openDeleteDialog('运行任务')
    fireEvent.click(await screen.findByRole('button', { name: '确认删除' }))
    await waitFor(() => expect(pmSeed.deleteTask).toHaveBeenCalledWith('t-run'))
    // 失败反馈：通知带标题，弹窗留驻（不假装成功）
    await waitFor(() =>
      expect(notifications.some((n) => n.title === '删除管道失败')).toBe(true),
    )
    expect(screen.getByText(/级联删除，不可恢复/)).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '确认删除' })).toBeEnabled(),
    )
    addSpy.mockRestore()

    // 重试成功 → 弹窗关闭
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }))
    await waitFor(() =>
      expect(screen.queryByText(/级联删除，不可恢复/)).not.toBeInTheDocument(),
    )
    expect(pmSeed.deleteTask).toHaveBeenCalledTimes(2)
  })
})

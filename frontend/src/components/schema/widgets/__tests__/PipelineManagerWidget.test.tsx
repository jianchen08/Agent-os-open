/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * PipelineManagerWidget 组件测试（2026-08-19 调试中心批次；批次 4 query 化适配）
 *
 * 验证任务管理面板（管道总览）三个行为修复：
 * - 未知状态的任务不再被丢弃（原 taskStatusToPipelineStatus 返回 null 即 continue）；
 * - 条目行显示任务态 chip（两态模型：任务域状态与运行态分离，中文化标签，
 *   未知值回退原串——细态不被运行态映射吞掉）；
 * - 展开详情含 state 真值行（任务状态/State 状态/已结束/当前阶段/消息条数）。
 *
 * 批次 4 适配：runs/states/全量任务已迁 query（usePipelineRunsQuery /
 * usePipelineStatesQuery / useAllTasksQuery）——mock 三个 query hook 直接返回
 * 播种数据，本地 30s 轮询/注册表自动刷新已退役。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { navigateToPipeline } from '@/services/pipelineNavigator'
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
    {
      id: 'task-cancel',
      title: '取消任务',
      status: 'cancelled', // 绑定语义：任务取消 → 运行取消（非失败）
      pipeline_run_id: 'pipeC',
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
  /** 一对一层级 + 树/详情解耦场景：父任务条目行 ← 子任务条目行（childPipe 有 state 真值） */
  const DECOUPLE_TASKS: Record<string, unknown>[] = [
    {
      id: 'parentTask',
      title: '父任务行',
      status: 'completed',
      pipeline_run_id: 'parentPipe',
    },
    {
      id: 'childTask',
      title: '子任务详情行',
      status: 'completed',
      pipeline_run_id: 'childPipe',
      parent_task_id: 'parentTask',
    },
  ]
  const DECOUPLE_STATES: Record<string, unknown> = {
    childPipe: {
      pipeline_id: 'childPipe',
      thread_id: 'th-c',
      state: {
        current_phase: 'exit',
        ended: true,
        status: 'active',
        'task.status': 'completed',
        message_count: 3,
        raw_error: null,
      },
    },
  }
  const mockUseAllTasksQuery = vi.fn(() => ({ data: FAKE_ALL_TASKS }))
  const mockUsePipelineRunsQuery = vi.fn(() => ({ data: FAKE_RUNS }))
  const mockUsePipelineStatesQuery = vi.fn(() => ({ data: FAKE_STATES }))
  /** 会话缓存读数与 ensureSessionsLoaded（S1 阻断用例需按用例覆写） */
  const mockReadSessions = vi.fn(() => [] as unknown[])
  const mockEnsureSessionsLoaded = vi.fn(() => Promise.resolve([] as unknown[]))
  return {
    mockReadSessions,
    mockEnsureSessionsLoaded,
    FAKE_RUNS,
    FAKE_STATES,
    FAKE_ALL_TASKS,
    SUBTASK_TASKS,
    WS_TASKS,
    SESSION_MAIN_RUNS,
    SESSION_MAIN_TASKS,
    DECOUPLE_TASKS,
    DECOUPLE_STATES,
    mockUseAllTasksQuery,
    mockUsePipelineRunsQuery,
    mockUsePipelineStatesQuery,
  }
})

vi.mock('@/hooks/queries/usePipelineRunsQuery', () => ({
  usePipelineRunsQuery: seed.mockUsePipelineRunsQuery,
  usePipelineStatesQuery: seed.mockUsePipelineStatesQuery,
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
  readSessions: seed.mockReadSessions,
  ensureSessionsLoaded: seed.mockEnsureSessionsLoaded,
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
    seed.mockUsePipelineStatesQuery.mockReturnValue({ data: seed.FAKE_STATES })
    // 导航 mock 跨用例清历史清实现（需返回值的用例自行 mockResolvedValue）
    vi.mocked(navigateToPipeline).mockReset()
  })

  it('未知状态任务保留且任务态 chip 可见', async () => {
    renderWithProviders(<PipelineManagerWidget />)
    // 两个任务都出现（未知状态不再被吞）；树视图+列表视图双渲染 → 用 getAll
    expect((await screen.findAllByText('评估中任务')).length).toBeGreaterThanOrEqual(1)
    expect((await screen.findAllByText('未知状态任务')).length).toBeGreaterThanOrEqual(1)
    expect((await screen.findAllByText('取消任务')).length).toBeGreaterThanOrEqual(1)
    // 任务态 chip 与运行态图标分离展示：细态中文化（evaluating → 评估中），
    // 未知值回退原串（pending_review）——原始值经 title 可查不占版面
    expect((await screen.findAllByText(/任务:评估中/)).length).toBeGreaterThanOrEqual(1)
    expect((await screen.findAllByText(/任务:pending_review/)).length).toBeGreaterThanOrEqual(1)
    // 绑定语义：任务取消 → 运行态图标落「已取消」（非失败）
    expect((await screen.findAllByTitle(/运行状态：已取消/)).length).toBeGreaterThanOrEqual(1)
  })

  it('展开详情含 state 真值行', async () => {
    renderWithProviders(<PipelineManagerWidget />)
    // 一对一合并：任务条目行即任务行（无任务节点层），行内「详细信息」按钮直接可用
    const detailBtn = (await screen.findAllByTitle(/详细信息/))[0]
    fireEvent.click(detailBtn)
    await waitFor(() => expect(screen.getAllByText('State 状态').length).toBeGreaterThanOrEqual(1))
    expect(screen.getAllByText('已结束').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('当前阶段').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('消息条数').length).toBeGreaterThanOrEqual(1)
    // state['task.status'] = completed 的真值出现在详情中
    expect(screen.getAllByText('completed').length).toBeGreaterThanOrEqual(1)
  })

  it('树收起不关详情：详情与树展开解耦（信息停留显示）', async () => {
    seed.mockUsePipelineRunsQuery.mockReturnValue({ data: {} })
    seed.mockUseAllTasksQuery.mockReturnValue({ data: seed.DECOUPLE_TASKS })
    seed.mockUsePipelineStatesQuery.mockReturnValue({ data: seed.DECOUPLE_STATES })
    renderWithProviders(<PipelineManagerWidget />)
    // 父任务条目行首 chevron（树=点击展开/收起，默认收起）
    const parentRowChevron = () => {
      const btn = screen.getAllByText('父任务行')[0].closest('div')?.querySelector('button')
      expect(btn).toBeInstanceOf(HTMLElement)
      return btn as HTMLElement
    }
    // 1) 展开树
    fireEvent.click(parentRowChevron())
    // 2) 展开子任务条目行详情（子行直挂父行下——一对一只有一个层级）
    const childRow = (await screen.findAllByText('子任务详情行'))[0].closest('div')
    const childInfoBtn = childRow?.querySelector('button[title*="详细信息"]')
    expect(childInfoBtn).toBeInstanceOf(HTMLElement)
    fireEvent.click(childInfoBtn as HTMLElement)
    await waitFor(() => expect(screen.getAllByText('State 状态').length).toBeGreaterThanOrEqual(1))
    // 3) 收起树：子树整体卸载（详情面板随之不可见），但详情状态不被树操作清除
    fireEvent.click(parentRowChevron())
    await waitFor(() => expect(screen.queryByText('State 状态')).toBeNull())
    // 4) 再展开树：详情仍在（停留显示，没有被树收起关掉）
    fireEvent.click(parentRowChevron())
    await waitFor(() => expect(screen.getAllByText('State 状态').length).toBeGreaterThanOrEqual(1))
  })

  it('任务派生条目落 seen：同管道不再被 states 循环重复建行', async () => {
    renderWithProviders(<PipelineManagerWidget />)
    // pipeA 既有任务派生条目（一对一合并为条目行）也有 state 摘要——去重后不得
    // 再出现 state 回退名"会话 th-a"的重复行（同 key 双行会让展开/详情状态串扰）
    await screen.findAllByText('评估中任务')
    expect(screen.queryByText('会话 th-a')).toBeNull()
  })

  it('子任务按父管道 id 挂到主管道节点下（树形而非顶层平铺）', async () => {
    seed.mockUseAllTasksQuery.mockReturnValue({ data: seed.SUBTASK_TASKS })
    renderWithProviders(<PipelineManagerWidget />)
    // 树默认收起：子任务未展开时不可见（树=点击展开）
    expect(screen.queryByText('子任务')).toBeNull()
    // 点父任务条目行首 chevron 展开（一对一：条目行即任务行）
    const chevron = (await screen.findAllByText('主管道'))[0].closest('div')?.querySelector('button')
    fireEvent.click(chevron!)
    // 单层：一对一绑定不出现"任务节点行+管道条目行"双层（各恰好一行）
    expect((await screen.findAllByText('主管道')).length).toBe(1)
    expect((await screen.findAllByText('子任务')).length).toBe(1)
    // 子任务条目行带缩进（depth>0 的 paddingLeft），直挂父任务条目行下
    const subtaskRow = (await screen.findAllByText('子任务'))[0].closest('div')
    const padding = subtaskRow?.getAttribute('style') ?? ''
    expect(padding).toMatch(/padding-left:\s*2[48]px/)
  })

  it('列表视图：行首 chevron 展开详情（与树视图共用详情状态）', async () => {
    renderWithProviders(<PipelineManagerWidget />)
    fireEvent.click(screen.getByTitle('列表视图'))
    // 表格行首 chevron = 详情展开（列表视图无树层级）
    const row = (await screen.findAllByText('评估中任务'))[0].closest('tr')
    const chevron = row?.querySelector('button')
    expect(chevron).toBeDefined()
    fireEvent.click(chevron!)
    await waitFor(() => expect(screen.getAllByText('State 状态').length).toBeGreaterThanOrEqual(1))
  })

  it('子任务（parent_task_id=会话主管道条目）渲染为该条目的子节点', async () => {
    seed.mockUsePipelineRunsQuery.mockReturnValue({ data: seed.SESSION_MAIN_RUNS })
    seed.mockUseAllTasksQuery.mockReturnValue({ data: seed.SESSION_MAIN_TASKS })
    renderWithProviders(<PipelineManagerWidget />)
    // 树默认收起：点主管道条目行首 chevron（有子级才渲染）展开子树
    const chevron = (await screen.findAllByText('会话 th-1'))[0].closest('div')?.querySelector('button')
    expect(chevron).toBeDefined()
    fireEvent.click(chevron!)
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
    // 树默认收起：先展开主管道条目
    const chevron = (await screen.findAllByText('会话 th-1'))[0].closest('div')?.querySelector('button')
    fireEvent.click(chevron!)
    // 会话列表 mock 为空 → 条目名回退"会话 th-1"（主管道 + 子管道同名）
    const rows = await screen.findAllByText('会话 th-1')
    expect(rows.length).toBeGreaterThanOrEqual(1)
    // 至少一行带缩进（depth>0 = 挂主管道条目下的直接子管道；主管道本身 depth=0 无缩进）
    const indented = rows.some(
      (el) => el.closest('div')?.getAttribute('style')?.match(/padding-left:\s*2[48]px/),
    )
    expect(indented).toBe(true)
  })

  it('任务条目行渲染打开工作空间按钮并开 workspace 文件树标签（0.1 对齐）', async () => {
    seed.mockUseAllTasksQuery.mockReturnValue({ data: seed.WS_TASKS })
    renderWithProviders(<PipelineManagerWidget />)
    // 树默认收起：先点父任务条目行首 chevron 展开，子任务条目行才渲染
    const chevron = (await screen.findAllByText('主管道'))[0].closest('div')?.querySelector('button')
    fireEvent.click(chevron!)
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

  it('会话管道 state 带工作区也渲染按钮（R3：state.workspace 驱动，非任务条目）', async () => {
    // 会话管道：runs 快照有、任务列表无对应任务 → kind=session；工作区坐标
    // 只在 state 真值（ws_meta.path），任务 metadata 通道不适用
    seed.mockUsePipelineRunsQuery.mockReturnValue({
      data: {
        mainRun: {
          pipeline_id: 'sessPipe',
          run_id: 'run-sess',
          thread_id: 'th-sess',
          status: 'running',
          started_at: '2026-08-24T00:00:00Z',
        },
      },
    })
    seed.mockUseAllTasksQuery.mockReturnValue({ data: [] })
    seed.mockUsePipelineStatesQuery.mockReturnValue({
      data: {
        sessPipe: {
          pipeline_id: 'sessPipe',
          thread_id: 'th-sess',
          source: 'memory',
          state: {
            status: 'active',
            ws_meta: { path: 'D:/ws/session-ws', mode: 'plain', project_root: 'D:/proj' },
          },
        },
      },
    })
    renderWithProviders(<PipelineManagerWidget />)
    // 树默认收起：先展开会话分组节点（会话列表 mock 空 → 条目名回退"会话 th-sess"）
    const chevron = (await screen.findAllByText('会话 th-sess'))[0].closest('div')?.querySelector('button')
    fireEvent.click(chevron!)
    // state.ws_meta.path 驱动按钮（project_root 不用于关联）
    const btns = await screen.findAllByTitle('打开工作空间: D:/ws/session-ws')
    expect(btns.length).toBeGreaterThanOrEqual(1)
    fireEvent.click(btns[0])
    // 非任务条目无 taskId → dataSource 用 pipeline_id（state 行解析通道）
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.some((t) => t.dataSource === 'workspace://sessPipe')).toBe(true)
  })

  it('无工作区坐标的条目不渲染打开工作空间按钮（主会话 R1 自然推论）', async () => {
    seed.mockUsePipelineRunsQuery.mockReturnValue({
      data: {
        mainRun: {
          pipeline_id: 'plainSess',
          run_id: 'run-plain',
          thread_id: 'th-plain',
          status: 'running',
          started_at: '2026-08-24T00:00:00Z',
        },
      },
    })
    seed.mockUseAllTasksQuery.mockReturnValue({ data: [] })
    seed.mockUsePipelineStatesQuery.mockReturnValue({ data: {} })
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findAllByText('会话 th-plain')
    expect(screen.queryByTitle(/打开工作空间/)).toBeNull()
  })

  it('S1 会话拉取失败即阻断定位：不误建独立标签，且给出可见通知', async () => {
    // 会话缓存为空 → 点击条目先 ensureSessionsLoaded；此时 rejects
    seed.mockReadSessions.mockReturnValue([])
    seed.mockEnsureSessionsLoaded.mockRejectedValue(new Error('sessions fetch boom'))
    // 会话归属条目：thread_id=th-sess，若不阻断会被误判孤儿 → openSubAgentTab
    seed.mockUsePipelineRunsQuery.mockReturnValue({
      data: {
        mainRun: {
          pipeline_id: 'blockPipe',
          run_id: 'run-block',
          thread_id: 'th-sess',
          status: 'running',
          started_at: '2026-08-24T00:00:00Z',
        },
      },
    })
    seed.mockUseAllTasksQuery.mockReturnValue({ data: [] })
    seed.mockUsePipelineStatesQuery.mockReturnValue({ data: {} })

    const tabsBefore = useAgentTabStore.getState().tabs.length
    renderWithProviders(<PipelineManagerWidget />)
    fireEvent.click((await screen.findAllByText('会话 th-sess'))[0])

    await waitFor(() => {
      expect(useNotificationStore.getState().notifications.some((n) => n.title === '无法定位对话')).toBe(true)
    })
    // 未走导航链路，也未把有归属管道当孤儿建子标签
    expect(navigateToPipeline).not.toHaveBeenCalled()
    expect(useAgentTabStore.getState().tabs.slice(tabsBefore)).toHaveLength(0)
  })

  it('自环子任务管道：threadId 不在会话列表 → 按血缘会话跳转，不当孤儿在当前会话开标签', async () => {
    vi.mocked(navigateToPipeline).mockResolvedValue(true)
    // 会话列表只有根会话 th-root；子管道自环（thread_id=自身 id），
    // state 带血缘根会话 lineage.origin_session_id=th-root
    seed.mockReadSessions.mockReturnValue([{ id: 'th-root', title: '根会话' }])
    seed.mockUsePipelineRunsQuery.mockReturnValue({
      data: {
        subRun: {
          pipeline_id: 'selfLoopPipe',
          run_id: 'run-self',
          thread_id: 'selfLoopPipe',
          status: 'running',
          started_at: '2026-08-29T00:00:00Z',
        },
      },
    })
    seed.mockUsePipelineStatesQuery.mockReturnValue({
      data: {
        selfLoopPipe: {
          pipeline_id: 'selfLoopPipe',
          thread_id: 'selfLoopPipe',
          source: 'memory',
          state: { 'lineage.origin_session_id': 'th-root', 'task.status': 'running' },
        },
      },
    })
    seed.mockUseAllTasksQuery.mockReturnValue({ data: [] })

    const tabsBefore = useAgentTabStore.getState().tabs.length
    renderWithProviders(<PipelineManagerWidget />)
    fireEvent.click((await screen.findAllByText('会话 selfLoop'))[0])

    // 血缘会话作归属提示传给导航器（不在当前会话落孤儿标签的正确前提）
    await waitFor(() => {
      expect(navigateToPipeline).toHaveBeenCalledWith(
        'selfLoopPipe',
        expect.objectContaining({ fallbackSessionId: 'th-root' }),
      )
    })
    expect(useAgentTabStore.getState().tabs.slice(tabsBefore)).toHaveLength(0)
  })

  it('无血缘的自环条目维持孤儿行为：当前会话直接开子标签', async () => {
    // 旧数据（无 lineage.origin_session_id）不可跳转——孤儿分支保持原语义
    seed.mockReadSessions.mockReturnValue([{ id: 'th-root', title: '根会话' }])
    seed.mockUsePipelineRunsQuery.mockReturnValue({
      data: {
        subRun: {
          pipeline_id: 'legacyPipe',
          run_id: 'run-legacy',
          thread_id: 'legacyPipe',
          status: 'running',
          started_at: '2026-08-29T00:00:00Z',
        },
      },
    })
    seed.mockUsePipelineStatesQuery.mockReturnValue({ data: {} })
    seed.mockUseAllTasksQuery.mockReturnValue({ data: [] })

    renderWithProviders(<PipelineManagerWidget />)
    fireEvent.click((await screen.findAllByText('会话 legacyPi'))[0])

    await waitFor(() => {
      expect(useAgentTabStore.getState().tabs.some((t) => t.pipelineRunId === 'legacyPipe')).toBe(
        true,
      )
    })
    expect(navigateToPipeline).not.toHaveBeenCalled()
  })
})

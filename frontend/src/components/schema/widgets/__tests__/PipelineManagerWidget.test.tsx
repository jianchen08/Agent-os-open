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
// 顺序约束：先于被测组件 import——vi.mock 工厂体在被测组件初始化时执行，
// 届时本模块必须已求值（见 pmTestUtils 头注）
import { pmMod, pmSeed } from './pmTestUtils'
import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'
import { navigateToPipeline } from '@/services/pipelineNavigator'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { renderWithProviders } from '@/test/renderWithProviders'

// 端点与导航句柄走共享造型；三个查询 hook 本文件按用例高频覆写返回值，
// 直接绑定 pmSeed 句柄（覆写点即测试数据入口）。
vi.mock('@/services/api/tasks', () => pmMod.tasksApi())
vi.mock('@/services/api/client', () => pmMod.apiClient())
vi.mock('@/services/pipelineNavigator', () => pmMod.pipelineNavigator())

/** 播种数据（形状对齐 query hook 返回；句柄默认返回值在 beforeEach 播种） */
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
/** 子任务带工作空间场景（任务节点"打开工作空间"按钮数据链） */
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
/** 会话主管道缺席场景：主管道 run 被快照过滤后条目集里没有会话根，
 *  threadTop 回退线程组最早任务条目。契约：树与列表同源同量，建树环节
 *  不得丢条目（最早任务自挂自身会让整族从树里静默消失） */
const ORPHAN_THREAD_RUNS: Record<string, unknown> = {
  ghost: {
    pipeline_id: 'ghost13009006',
    run_id: 'run-ghost',
    thread_id: 'th-ghost',
    status: 'cancelled',
    started_at: '2026-08-22T00:05:00Z',
    ended_at: '2026-08-22T00:05:15Z',
  },
}
const ORPHAN_THREAD_TASKS: Record<string, unknown>[] = [
  {
    id: 't-meteor',
    title: '陨石躲避',
    status: 'running',
    pipeline_run_id: 'pipe-meteor',
    threadId: 'th-godot',
    timestamps: { createdAt: '2026-08-22T00:00:00Z' },
  },
  {
    id: 't-tool',
    title: '工具链自检',
    status: 'running',
    pipeline_run_id: 'pipe-tool',
    threadId: 'th-godot',
    timestamps: { createdAt: '2026-08-22T00:01:00Z' },
  },
  {
    id: 't-eval',
    title: '评估子任务',
    status: 'running',
    pipeline_run_id: 'pipe-eval',
    parent_task_id: 't-tool',
  },
  {
    id: 't-mcp',
    title: '通道实测',
    status: 'running',
    pipeline_run_id: 'pipe-mcp',
  },
]

vi.mock('@/hooks/queries/usePipelineRunsQuery', () => ({
  usePipelineRunsQuery: pmSeed.usePipelineRunsQuery,
  usePipelineStatesQuery: pmSeed.usePipelineStatesQuery,
}))
vi.mock('@/hooks/queries/useAllTasksQuery', () => ({
  useAllTasksQuery: pmSeed.useAllTasksQuery,
}))
// 会话列表走真实 query 会打真实 HTTP——mock 为空列表（widget 仅用标题映射）
vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  useSessionsQuery: pmSeed.useSessionsQuery,
  readSessions: pmSeed.readSessions,
  ensureSessionsLoaded: pmSeed.ensureSessionsLoaded,
}))

/** 空查询 + 单项目登记的播种（打开文件夹/项目分组族用例共用；登记项可注入） */
function seedProjectQueries(
  project: { id: string; goal: string } & Record<string, unknown> = {
    id: 'proj-open',
    goal: '可打开项目',
  },
) {
  pmSeed.usePipelineRunsQuery.mockReturnValue({ data: {} })
  pmSeed.usePipelineStatesQuery.mockReturnValue({ data: {} })
  pmSeed.useAllTasksQuery.mockReturnValue({ data: [] })
  pmSeed.fetchProjects.mockResolvedValue({
    items: [{ ...project, timestamps: { createdAt: '2026-08-30T00:00:00Z' } }],
  })
}

/** 空查询 + 单会话在飞管道 + 单项目登记（项目分组行对照用例共用） */
function seedSessionRunWithProject(project: { id: string; goal: string }) {
  seedProjectQueries(project)
  pmSeed.usePipelineRunsQuery.mockReturnValue({
    data: {
      mainRun: {
        pipeline_id: 'sessPipe',
        run_id: 'run-sess',
        thread_id: 'th-p',
        status: 'running',
        started_at: '2026-08-30T00:00:00Z',
      },
    },
  })
}

/** 打开文件夹失败族：播种项目 + 注入端点失败 + 监听通知通道（返回 spy 供恢复） */
function seedOpenFolderFailure(
  project: { id: string; goal: string },
  fail: (open: typeof pmSeed.workspaceOpen) => void,
) {
  seedProjectQueries(project)
  fail(pmSeed.workspaceOpen)
  return vi
    .spyOn(useNotificationStore.getState(), 'addNotification')
    .mockImplementation(() => {})
}

/** 打开文件夹失败族：渲染面板并点击首个「打开文件夹」按钮 */
async function renderAndOpenFirstFolderButton() {
  renderPanelAllStatuses(<PipelineManagerWidget />)
  fireEvent.click((await screen.findAllByLabelText('打开文件夹'))[0])
}

/** 渲染并切到状态筛选「全部」（面板默认只看运行中——用户裁定 2026-09-21；
 *  终态/项目视野用例经此恢复全量断言前提。DOM 序：类型筛选「全部」在前，
 *  状态筛选「全部」第二） */
function renderPanelAllStatuses(ui: React.ReactElement) {
  const ret = renderWithProviders(ui)
  fireEvent.click(screen.getAllByText('全部')[1])
  return ret
}

describe('PipelineManagerWidget', () => {
  beforeEach(() => {
    // 默认播种全量任务；子任务用例覆盖为 SUBTASK_TASKS
    pmSeed.useAllTasksQuery.mockReturnValue({ data: FAKE_ALL_TASKS })
    pmSeed.usePipelineRunsQuery.mockReturnValue({ data: FAKE_RUNS })
    pmSeed.usePipelineStatesQuery.mockReturnValue({ data: FAKE_STATES })
    pmSeed.fetchProjects.mockResolvedValue({ items: [] })
    pmSeed.workspaceOpen.mockResolvedValue({ data: { success: true } })
    // 导航 mock 跨用例清历史清实现（需返回值的用例自行 mockResolvedValue）
    vi.mocked(navigateToPipeline).mockReset()
  })

  it('未知状态任务保留且任务态 chip 可见', async () => {
    renderPanelAllStatuses(<PipelineManagerWidget />)
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
    renderPanelAllStatuses(<PipelineManagerWidget />)
    // 一对一合并：任务条目行即任务行（无任务节点层），行内「详细信息」按钮直接可用。
    // 按钮锚定评估任务行内查询——startedAt 无真值落 '' 后行序不再被 now 兜底
    // 时间隐式钉住，全局首个详情按钮不保证属于带 state 真值的行
    const evalRow = ((await screen.findAllByText('评估中任务'))[0].closest('div') ??
      null) as HTMLElement | null
    const detailBtn = evalRow?.querySelector(
      'button[title*="详细信息"]',
    ) as HTMLElement | null
    expect(detailBtn).toBeTruthy()
    fireEvent.click(detailBtn!)
    await waitFor(() => expect(screen.getAllByText('State 状态').length).toBeGreaterThanOrEqual(1))
    expect(screen.getAllByText('已结束').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('当前阶段').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('消息条数').length).toBeGreaterThanOrEqual(1)
    // state['task.status'] = completed 的真值出现在详情中
    expect(screen.getAllByText('completed').length).toBeGreaterThanOrEqual(1)
  })

  it('树收起不关详情：详情与树展开解耦（信息停留显示）', async () => {
    pmSeed.usePipelineRunsQuery.mockReturnValue({ data: {} })
    pmSeed.useAllTasksQuery.mockReturnValue({ data: DECOUPLE_TASKS })
    pmSeed.usePipelineStatesQuery.mockReturnValue({ data: DECOUPLE_STATES })
    renderPanelAllStatuses(<PipelineManagerWidget />)
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
    renderPanelAllStatuses(<PipelineManagerWidget />)
    // pipeA 既有任务派生条目（一对一合并为条目行）也有 state 摘要——去重后不得
    // 再出现 state 回退名"会话 th-a"的重复行（同 key 双行会让展开/详情状态串扰）
    await screen.findAllByText('评估中任务')
    expect(screen.queryByText('会话 th-a')).toBeNull()
  })

  it('子任务按父管道 id 挂到主管道节点下（树形而非顶层平铺）', async () => {
    pmSeed.useAllTasksQuery.mockReturnValue({ data: SUBTASK_TASKS })
    renderPanelAllStatuses(<PipelineManagerWidget />)
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
    renderPanelAllStatuses(<PipelineManagerWidget />)
    fireEvent.click(screen.getByTitle('列表视图'))
    // 表格行首 chevron = 详情展开（列表视图无树层级）
    const row = (await screen.findAllByText('评估中任务'))[0].closest('tr')
    const chevron = row?.querySelector('button')
    expect(chevron).toBeDefined()
    fireEvent.click(chevron!)
    await waitFor(() => expect(screen.getAllByText('State 状态').length).toBeGreaterThanOrEqual(1))
  })

  it('子任务（parent_task_id=会话主管道条目）渲染为该条目的子节点', async () => {
    pmSeed.usePipelineRunsQuery.mockReturnValue({ data: SESSION_MAIN_RUNS })
    pmSeed.useAllTasksQuery.mockReturnValue({ data: SESSION_MAIN_TASKS })
    renderPanelAllStatuses(<PipelineManagerWidget />)
    // 树默认收起：点主管道条目行首 chevron（有子级才渲染）展开子树
    const chevron = (await screen.findAllByText('会话 th-1'))[0].closest('div')?.querySelector('button')
    expect(chevron).toBeDefined()
    fireEvent.click(chevron!)
    // 主管道条目（kind=session）+ 子任务条目行都渲染（子任务不得整体丢失）
    expect((await screen.findAllByText('子任务A')).length).toBeGreaterThanOrEqual(1)
    // 子任务条目行带缩进（depth>0，挂主管道条目下而非顶层平铺）
    const subtaskRow = (await screen.findAllByText('子任务A')).find((el) =>
      el.closest('div')?.className.includes('hover:bg-accent'),
    )
    expect(subtaskRow).toBeDefined()
    const padding = subtaskRow?.closest('div')?.getAttribute('style') ?? ''
    expect(padding).toMatch(/padding-left:\s*2[48]px/)
  })

  it('会话主管道缺席时线程组任务不自挂丢行（树与列表同量）', async () => {
    pmSeed.usePipelineRunsQuery.mockReturnValue({ data: ORPHAN_THREAD_RUNS })
    pmSeed.useAllTasksQuery.mockReturnValue({ data: ORPHAN_THREAD_TASKS })
    renderPanelAllStatuses(<PipelineManagerWidget />)
    // 顶层即可见：线程组最早任务（threadTop 回退目标）、无线程任务、孤儿会话
    expect((await screen.findAllByText('陨石躲避')).length).toBe(1)
    expect(screen.getAllByText('通道实测').length).toBe(1)
    expect(screen.getAllByText('会话 th-ghost').length).toBe(1)
    // 展开最早任务行：同线程兄弟全部在树中（整族不得消失）
    const chevron = screen.getAllByText('陨石躲避')[0].closest('div')?.querySelector('button')
    fireEvent.click(chevron!)
    expect((await screen.findAllByText('工具链自检')).length).toBe(1)
    // 同线程兄弟挂 threadTop（depth 1 = 24px）
    const toolRow = screen.getAllByText('工具链自检')[0].closest('div')
    expect(toolRow?.getAttribute('style')).toMatch(/padding-left:\s*24px/)
    // 树逐层点击展开：再展开兄弟行，parent_task_id 孙级在树中（depth 2 = 40px）
    const toolChevron = toolRow?.querySelector('button')
    fireEvent.click(toolChevron!)
    const evalRow = (await screen.findAllByText('评估子任务'))[0].closest('div')
    expect(evalRow?.getAttribute('style')).toMatch(/padding-left:\s*40px/)
  })

  it('挂会话主管道下的非任务子管道仍渲染（防回归）', async () => {
    pmSeed.usePipelineRunsQuery.mockReturnValue({ data: SESSION_MAIN_RUNS })
    renderPanelAllStatuses(<PipelineManagerWidget />)
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

  it('任务条目行渲染打开工作空间按钮并开 workspace 文件树标签', async () => {
    pmSeed.useAllTasksQuery.mockReturnValue({ data: WS_TASKS })
    renderPanelAllStatuses(<PipelineManagerWidget />)
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
    pmSeed.usePipelineRunsQuery.mockReturnValue({
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
    pmSeed.useAllTasksQuery.mockReturnValue({ data: [] })
    pmSeed.usePipelineStatesQuery.mockReturnValue({
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
    renderPanelAllStatuses(<PipelineManagerWidget />)
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

  it('state 双键并存时任务域镜像 task.ws_meta 优先（裸 ws_meta 被会话投影污染场景）', async () => {
    // 运行中任务：state 出口同时带 task.ws_meta（任务域镜像，worktree 真值）
    // 与被会话投影污染的裸 ws_meta（会话目录）——按钮路径必须取镜像，
    // 不能落到会话默认文件夹
    pmSeed.useAllTasksQuery.mockReturnValue({
      data: [
        {
          id: 'subPipe',
          title: '镜像优先子任务',
          status: 'running',
          pipeline_run_id: 'subPipe',
          agent_name: 'general_agent',
        },
      ],
    })
    pmSeed.usePipelineStatesQuery.mockReturnValue({
      data: {
        subPipe: {
          pipeline_id: 'subPipe',
          thread_id: 'th-1',
          source: 'memory',
          state: {
            status: 'active',
            ws_meta: { path: 'D:/ws/sessions/thread-x', mode: 'plain' },
            'task.ws_meta': {
              path: 'D:/ws/proj__wt_9',
              mode: 'worktree',
              project_root: 'D:/ws/proj',
            },
          },
        },
      },
    })
    renderPanelAllStatuses(<PipelineManagerWidget />)
    expect(await screen.findAllByText('镜像优先子任务')).toHaveLength(1)
    expect(
      await screen.findAllByTitle('打开工作空间: D:/ws/proj__wt_9')
    ).toHaveLength(1)
  })

  it('无工作区坐标的条目不渲染打开工作空间按钮（主会话 R1 自然推论）', async () => {
    pmSeed.usePipelineRunsQuery.mockReturnValue({
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
    pmSeed.useAllTasksQuery.mockReturnValue({ data: [] })
    pmSeed.usePipelineStatesQuery.mockReturnValue({ data: {} })
    renderPanelAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('会话 th-plain')
    expect(screen.queryByTitle(/打开工作空间/)).toBeNull()
  })

  it('S1 会话拉取失败即阻断定位：不误建独立标签，且给出可见通知', async () => {
    // 会话缓存为空 → 点击条目先 ensureSessionsLoaded；此时 rejects
    pmSeed.readSessions.mockReturnValue([])
    pmSeed.ensureSessionsLoaded.mockRejectedValue(new Error('sessions fetch boom'))
    // 会话归属条目：thread_id=th-sess，若不阻断会被误判孤儿 → openSubAgentTab
    pmSeed.usePipelineRunsQuery.mockReturnValue({
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
    pmSeed.useAllTasksQuery.mockReturnValue({ data: [] })
    pmSeed.usePipelineStatesQuery.mockReturnValue({ data: {} })

    const tabsBefore = useAgentTabStore.getState().tabs.length
    renderPanelAllStatuses(<PipelineManagerWidget />)
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
    pmSeed.readSessions.mockReturnValue([{ id: 'th-root', title: '根会话' }])
    pmSeed.usePipelineRunsQuery.mockReturnValue({
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
    pmSeed.usePipelineStatesQuery.mockReturnValue({
      data: {
        selfLoopPipe: {
          pipeline_id: 'selfLoopPipe',
          thread_id: 'selfLoopPipe',
          source: 'memory',
          state: { 'lineage.origin_session_id': 'th-root', 'task.status': 'running' },
        },
      },
    })
    pmSeed.useAllTasksQuery.mockReturnValue({ data: [] })

    const tabsBefore = useAgentTabStore.getState().tabs.length
    renderPanelAllStatuses(<PipelineManagerWidget />)
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
    pmSeed.readSessions.mockReturnValue([{ id: 'th-root', title: '根会话' }])
    pmSeed.usePipelineRunsQuery.mockReturnValue({
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
    pmSeed.usePipelineStatesQuery.mockReturnValue({ data: {} })
    pmSeed.useAllTasksQuery.mockReturnValue({ data: [] })

    renderPanelAllStatuses(<PipelineManagerWidget />)
    fireEvent.click((await screen.findAllByText('会话 legacyPi'))[0])

    await waitFor(() => {
      expect(useAgentTabStore.getState().tabs.some((t) => t.pipelineRunId === 'legacyPipe')).toBe(
        true,
      )
    })
    expect(navigateToPipeline).not.toHaveBeenCalled()
  })

  it('项目分组行无对话入口：按钮不渲染、点击行不开标签（项目无管道无会话）', async () => {
    // 对照组：同一棵树里会话条目保留「打开对话」按钮——差异只在 kind
    seedSessionRunWithProject({ id: 'proj-ghost', goal: '孤儿测试项目' })

    const tabsBefore = useAgentTabStore.getState().tabs.length
    renderPanelAllStatuses(<PipelineManagerWidget />)
    const projectRow = await screen.findByText('孤儿测试项目')

    // 树视图只有会话行有「打开对话」按钮；项目行没有
    expect(screen.getByLabelText('打开对话')).toBeInTheDocument()

    // 点击项目行（无按钮路径的兜底入口）：不开任何标签
    fireEvent.click(projectRow)
    await waitFor(() => {
      expect(useAgentTabStore.getState().tabs.slice(tabsBefore)).toHaveLength(0)
    })
  })

  it('项目分组行渲染打开文件夹按钮：点击调 workspaces open 端点（项目登记通道）', async () => {
    seedProjectQueries()

    renderPanelAllStatuses(<PipelineManagerWidget />)
    await screen.findByText('可打开项目')

    // 只有项目行有「打开文件夹」按钮（本树无会话/任务条目）
    const folderButtons = screen.getAllByLabelText('打开文件夹')
    expect(folderButtons).toHaveLength(1)

    // 点击 → workspaces open 端点收到裸项目登记 id（后端登记通道解析文件夹）
    fireEvent.click(folderButtons[0])
    await waitFor(() => {
      expect(pmSeed.workspaceOpen).toHaveBeenCalledWith(
        '/ext/workspace_service/workspaces/proj-open/open',
      )
    })
  })

  it('打开文件夹业务失败（success:false）：失败通知携带后端 message', async () => {
    const addNotification = seedOpenFolderFailure(
      { id: 'proj-biz-fail', goal: '业务失败项目' },
      (open) => open.mockResolvedValueOnce({ data: { success: false, message: 'IDE 连接器不可用' } }),
    )
    await renderAndOpenFirstFolderButton()

    await waitFor(() => {
      expect(addNotification).toHaveBeenCalledWith(
        expect.objectContaining({ title: '打开文件夹失败', message: 'IDE 连接器不可用' }),
      )
    })
    addNotification.mockRestore()
  })

  it('打开文件夹传输失败（请求抛错）：失败通知落到用户可见通道', async () => {
    const addNotification = seedOpenFolderFailure(
      { id: 'proj-net-fail', goal: '传输失败项目' },
      (open) => open.mockRejectedValueOnce(new Error('network down')),
    )
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    await renderAndOpenFirstFolderButton()

    await waitFor(() => {
      expect(addNotification).toHaveBeenCalledWith(
        expect.objectContaining({
          title: '打开文件夹失败',
          message: '项目 传输失败项目 打开失败，请稍后重试',
        }),
      )
    })
    consoleError.mockRestore()
    addNotification.mockRestore()
  })

  it('列表视图同款：项目行打开文件夹按钮走同一端点', async () => {
    seedProjectQueries({ id: 'proj-list', goal: '列表视图项目' })

    renderPanelAllStatuses(<PipelineManagerWidget />)
    await screen.findByText('列表视图项目')
    fireEvent.click(screen.getByTitle('列表视图'))

    fireEvent.click((await screen.findAllByLabelText('打开文件夹'))[0])
    await waitFor(() => {
      expect(pmSeed.workspaceOpen).toHaveBeenCalledWith(
        '/ext/workspace_service/workspaces/proj-list/open',
      )
    })
  })

  it('项目登记行无管道语义：无运行态/无归属徽标，详情卡出项目字段', async () => {
    // 回归锚（真机 2026-09-21）：旧实现给项目行硬编码 status:'running'，
    // 登记时间被当开始时间 → 「运行中 45h22m + 管道 ID project-xxx + 无归属」
    seedProjectQueries({
      id: 'proj-dereg',
      goal: '去管道化项目',
      metadata: { path: 'D:/repos/demo' },
    })

    renderPanelAllStatuses(<PipelineManagerWidget />)
    await screen.findByText('去管道化项目')

    // 无运行态：任何行都不渲染管道状态图标（筛选按钮的「运行中」文案常驻，
    // 故以行内状态 title 为准）；无归属徽标不套在项目行上
    expect(screen.queryByTitle(/^运行状态：/)).not.toBeInTheDocument()
    expect(screen.queryByText('无归属')).not.toBeInTheDocument()

    // 详情卡按项目字段展示（旧实现出管道 ID/运行 ID/归属/开始）
    fireEvent.click(screen.getAllByLabelText('切换详细信息')[0])
    expect(await screen.findByText('项目 ID')).toBeInTheDocument()
    expect(screen.getByText('D:/repos/demo')).toBeInTheDocument()
    expect(screen.getByText('登记时间')).toBeInTheDocument()
    expect(screen.queryByText('管道 ID')).not.toBeInTheDocument()
    expect(screen.queryByText('运行 ID')).not.toBeInTheDocument()
    expect(screen.queryByText('归属')).not.toBeInTheDocument()
  })

  it('树视图项目独立成「项目」组：不混入执行中/最近完成分组', async () => {
    seedSessionRunWithProject({ id: 'proj-grp', goal: '分组项目' })

    renderPanelAllStatuses(<PipelineManagerWidget />)
    expect(await screen.findByText('分组项目')).toBeInTheDocument()
    // 项目组标题存在（单视图挂载：组头 1 + 树行类型徽标 1 = 2；旧实现无组头
    // 只有徽标 1 个）
    expect(screen.getAllByText('项目')).toHaveLength(2)
    expect(screen.getByText('执行中的管道')).toBeInTheDocument()
    expect(screen.queryByText('最近完成')).not.toBeInTheDocument()
  })

  it('项目行渲染删除按钮：默认口径确认调删除端点（子任务挂起保留）', async () => {
    seedProjectQueries({ id: 'proj-del', goal: '待删项目' })
    pmSeed.deleteProject.mockResolvedValueOnce({
      message: '项目已删除',
      id: 'proj-del',
      suspended_children: 2,
      deleted_children: 0,
      folder_removed: false,
    })
    const addNotification = vi
      .spyOn(useNotificationStore.getState(), 'addNotification')
      .mockImplementation(() => {})

    renderPanelAllStatuses(<PipelineManagerWidget />)
    await screen.findByText('待删项目')

    // 点击行上删除按钮 → 打开确认弹窗（不是直接删）
    fireEvent.click(screen.getByLabelText('删除项目'))
    expect(await screen.findByText(/请选择名下子任务的处置方式/)).toBeTruthy()
    expect(pmSeed.deleteProject).not.toHaveBeenCalled()

    // 默认口径（仅删项目）直接确认：不带级联/删文件夹参数
    fireEvent.click(screen.getByRole('button', { name: /确认删除/ }))
    await waitFor(() => {
      expect(pmSeed.deleteProject).toHaveBeenCalledWith('proj-del', {
        deleteChildren: false,
        deleteFiles: false,
      })
    })
    await waitFor(() => {
      expect(addNotification).toHaveBeenCalledWith(
        expect.objectContaining({
          title: '项目已删除',
          message: '项目「待删项目」已删除，名下子任务已挂起保留',
        }),
      )
    })
    addNotification.mockRestore()
  })

  it('删除弹窗选级联口径+删文件夹：deleteProject 收到双 true，通知报级联数', async () => {
    seedProjectQueries({ id: 'proj-cas', goal: '级联项目' })
    pmSeed.deleteProject.mockResolvedValueOnce({
      message: '项目已删除',
      id: 'proj-cas',
      suspended_children: 0,
      deleted_children: 3,
      folder_removed: true,
    })
    const addNotification = vi
      .spyOn(useNotificationStore.getState(), 'addNotification')
      .mockImplementation(() => {})

    renderPanelAllStatuses(<PipelineManagerWidget />)
    await screen.findByText('级联项目')
    fireEvent.click(screen.getByLabelText('删除项目'))
    await screen.findByText(/请选择名下子任务的处置方式/)

    fireEvent.click(screen.getByLabelText('连同子任务一起删除（不可恢复）'))
    fireEvent.click(screen.getByLabelText('同时删除项目文件夹（不可恢复）'))
    fireEvent.click(screen.getByRole('button', { name: /确认删除/ }))

    await waitFor(() => {
      expect(pmSeed.deleteProject).toHaveBeenCalledWith('proj-cas', {
        deleteChildren: true,
        deleteFiles: true,
      })
    })
    await waitFor(() => {
      expect(addNotification).toHaveBeenCalledWith(
        expect.objectContaining({
          title: '项目已删除',
          message: '项目「级联项目」已删除，连同 3 个子任务',
        }),
      )
    })
    addNotification.mockRestore()
  })

  it('删除失败（后端报子任务删除失败）：失败通知携带后端 message，弹窗不关', async () => {
    seedProjectQueries({ id: 'proj-fail', goal: '失败项目' })
    pmSeed.deleteProject.mockRejectedValueOnce(
      Object.assign(new Error('x'), {
        message: '子任务删除失败，项目未删除: child-2',
      }),
    )
    const addNotification = vi
      .spyOn(useNotificationStore.getState(), 'addNotification')
      .mockImplementation(() => {})

    renderPanelAllStatuses(<PipelineManagerWidget />)
    await screen.findByText('失败项目')
    fireEvent.click(screen.getByLabelText('删除项目'))
    fireEvent.click(await screen.findByRole('button', { name: /确认删除/ }))

    await waitFor(() => {
      expect(addNotification).toHaveBeenCalledWith(
        expect.objectContaining({
          title: '删除项目失败',
          message: '子任务删除失败，项目未删除: child-2',
        }),
      )
    })
    // 失败后弹窗保持打开（用户可换口径重试或取消）
    expect(screen.getByRole('button', { name: /确认删除/ })).toBeInTheDocument()
    addNotification.mockRestore()
  })

  it('列表视图同款：项目行删除按钮打开同一确认弹窗', async () => {
    seedProjectQueries({ id: 'proj-list-del', goal: '列表删除项目' })

    renderPanelAllStatuses(<PipelineManagerWidget />)
    await screen.findByText('列表删除项目')
    fireEvent.click(screen.getByTitle('列表视图'))

    fireEvent.click((await screen.findAllByLabelText('删除项目'))[0])
    expect(await screen.findByText(/请选择名下子任务的处置方式/)).toBeTruthy()
  })

  it('state 独有条目读内核实际状态 run_status：轮中不按上轮 ended 误报已结束', async () => {
    // 回归场景（2026-09-03 双状态裁定）：runs 不可见的管道仅存在于 state 摘要，
    // 上一轮终态残留（ended=true）+ 本轮进行中（内核 run_status=running）——
    // 状态必须落「运行中」（实际状态优先），不得按预期层残留键误报「已完成」。
    pmSeed.usePipelineRunsQuery.mockReturnValue({ data: {} })
    pmSeed.useAllTasksQuery.mockReturnValue({ data: [] })
    pmSeed.usePipelineStatesQuery.mockReturnValue({
      data: {
        ghostPipe: {
          pipeline_id: 'ghostPipe',
          thread_id: 'th-ghost',
          source: 'memory',
          state: { run_status: 'running', ended: true, current_phase: 'exit' },
        },
      },
    })
    renderPanelAllStatuses(<PipelineManagerWidget />)
    expect((await screen.findAllByTitle(/运行状态：运行中/)).length).toBeGreaterThanOrEqual(1)
  })

  it('state 独有条目无 run_status（旧 checkpoint 数据）回退 ended 推断', async () => {
    pmSeed.usePipelineRunsQuery.mockReturnValue({ data: {} })
    pmSeed.useAllTasksQuery.mockReturnValue({ data: [] })
    pmSeed.usePipelineStatesQuery.mockReturnValue({
      data: {
        coldPipe: {
          pipeline_id: 'coldPipe',
          thread_id: 'th-cold',
          source: 'checkpoint',
          state: { ended: true },
        },
      },
    })
    renderPanelAllStatuses(<PipelineManagerWidget />)
    expect((await screen.findAllByTitle(/运行状态：已完成/)).length).toBeGreaterThanOrEqual(1)
  })

  it('默认状态筛选=运行中：面板默认只显示在跑条目，终态与项目收进「全部」', async () => {
    pmSeed.usePipelineRunsQuery.mockReturnValue({
      data: {
        doneRun: {
          pipeline_id: 'pipeDone',
          run_id: 'r-done',
          thread_id: 'th-done',
          status: 'completed',
          started_at: '2026-08-30T00:00:00Z',
          ended_at: '2026-08-30T00:01:00Z',
        },
        liveRun: {
          pipeline_id: 'pipeLive',
          run_id: 'r-live',
          thread_id: 'th-live',
          status: 'running',
          started_at: '2026-08-30T01:00:00Z',
        },
      },
    })
    pmSeed.usePipelineStatesQuery.mockReturnValue({ data: {} })
    pmSeed.useAllTasksQuery.mockReturnValue({ data: [] })
    pmSeed.fetchProjects.mockResolvedValue({
      items: [{ id: 'proj-x', goal: '默认视野项目', timestamps: { createdAt: '2026-08-30T00:00:00Z' } }],
    })

    renderWithProviders(<PipelineManagerWidget />)

    // 在跑条目可见（会话列表无命中 → 名字回退「会话 <thread 前 8>」）
    expect(await screen.findByText('会话 th-live')).toBeInTheDocument()
    // 终态条目与项目登记行默认隐藏（最近完成组无内容不渲染，空项目组不占位）
    expect(screen.queryByText('会话 th-done')).not.toBeInTheDocument()
    expect(screen.queryByText('默认视野项目')).not.toBeInTheDocument()
    expect(screen.queryByText('最近完成')).not.toBeInTheDocument()

    // 切「全部」恢复全量视野（DOM 序：状态筛选「全部」是第二个）
    fireEvent.click(screen.getAllByText('全部')[1])
    expect(await screen.findByText('会话 th-done')).toBeInTheDocument()
    expect(screen.getByText('默认视野项目')).toBeInTheDocument()
    expect(screen.getByText('最近完成')).toBeInTheDocument()
  })

  it('默认视野下项目分组：有在跑挂靠任务保留，空分组不占位', async () => {
    pmSeed.usePipelineRunsQuery.mockReturnValue({ data: {} })
    pmSeed.usePipelineStatesQuery.mockReturnValue({
      data: {
        livePipe: {
          pipeline_id: 'livePipe',
          thread_id: 'th-att',
          source: 'checkpoint',
          state: { run_status: 'running' },
        },
      },
    })
    pmSeed.useAllTasksQuery.mockReturnValue({
      data: [
        {
          id: 't-att',
          title: '项目在跑任务',
          status: 'running',
          pipeline_run_id: 'livePipe',
          metadata: { parent_project_id: 'proj-live' },
        },
      ],
    })
    pmSeed.fetchProjects.mockResolvedValue({
      items: [
        { id: 'proj-live', goal: '有在跑任务的项目', timestamps: { createdAt: '2026-08-30T00:00:00Z' } },
        { id: 'proj-empty', goal: '空项目', timestamps: { createdAt: '2026-08-30T00:00:00Z' } },
      ],
    })

    renderWithProviders(<PipelineManagerWidget />)

    expect(await screen.findByText('项目在跑任务')).toBeInTheDocument()
    // 项目登记查询与任务列表异步落定有先后：等待式查询组头
    expect(await screen.findByText('有在跑任务的项目')).toBeInTheDocument()
    expect(screen.queryByText('空项目')).not.toBeInTheDocument()

    fireEvent.click(screen.getAllByText('全部')[1])
    expect(await screen.findByText('空项目')).toBeInTheDocument()
  })
})

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
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
// 顺序约束：先于被测组件 import——vi.mock 工厂体在被测组件初始化时执行，
// 届时本模块必须已求值（见 pmTestUtils 头注）
// eslint-disable-next-line import-x/order -- pmTestUtils 须先于被测组件求值（vi.mock 工厂时序约束，见 pmTestUtils 头注；禁用排序自动修防回归）
import { pmMod, pmSeed, renderPmAllStatuses } from './pmTestUtils'
import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'
import { navigateToPipeline } from '@/services/pipelineNavigator'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { useSessionStore } from '@/stores/sessionStore'
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
    // 统一管道标题（ADR 2026-10-27）：行名 = run.title，无域回退
    title: '主管道',
    status: 'completed',
    started_at: '2026-08-22T00:00:00Z',
    ended_at: '2026-08-22T00:01:00Z',
  },
  subRun: {
    pipeline_id: 'subPipe',
    run_id: 'run-sub',
    thread_id: 'th-1',
    title: '子任务',
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
    title: '主会话',
    status: 'running',
    started_at: '2026-08-22T00:00:00Z',
  },
  subRun: {
    pipeline_id: 'subpipe222',
    run_id: 'run-sub',
    thread_id: 'th-1',
    title: '子任务A',
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
/** 同会话兄弟子任务 + 主管道自身已完成（缺省运行中视图会滤掉主管道条目）：
 *  复现 2026-09-24 用户实报——兄弟任务曾被回退锚点互挂成假父子 */
const SIBLING_THREAD_RUNS: Record<string, unknown> = {
  mainRun: {
    pipeline_id: 'pipe-sib-main',
    run_id: 'run-sib-main',
    thread_id: 'th-sib',
    title: '帮我多次调研',
    status: 'completed',
    started_at: '2026-09-24T08:30:00Z',
    ended_at: '2026-09-24T08:31:00Z',
  },
  subRunA: {
    pipeline_id: 'pipe-sib-a',
    run_id: 'run-sib-a',
    thread_id: 'th-sib',
    title: '补充调研A',
    status: 'running',
    started_at: '2026-09-24T08:37:33Z',
  },
  subRunB: {
    pipeline_id: 'pipe-sib-b',
    run_id: 'run-sib-b',
    thread_id: 'th-sib',
    title: '补充调研B',
    status: 'running',
    started_at: '2026-09-24T08:37:34Z',
  },
}
const SIBLING_THREAD_TASKS: Record<string, unknown>[] = [
  {
    id: 't-sib-a',
    title: '补充调研A',
    status: 'pending',
    pipeline_run_id: 'pipe-sib-a',
    parent_task_id: 'pipe-sib-main',
    threadId: 'th-sib',
    agent_name: 'research_agent',
  },
  {
    id: 't-sib-b',
    title: '补充调研B',
    status: 'pending',
    pipeline_run_id: 'pipe-sib-b',
    parent_task_id: 'pipe-sib-main',
    threadId: 'th-sib',
    agent_name: 'research_agent',
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
/** 会话主管道缺席场景：主管道 run 被快照过滤后条目集里没有会话根。
 *  契约：树与列表同源同量，建树环节不得丢条目；线程无主管道锚点时成员
 *  落根平铺（平级可见），兄弟任务之间不互挂（假层级，用户指正 2026-09-24） */
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
// agents 注册表（agent.id → 显示名解析；空注册表 = 徽标走 stem 短名回退）
vi.mock('@/hooks/queries/useAgentsQuery', () => ({
  useAgentsQuery: pmSeed.useAgentsQuery,
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
  renderPmAllStatuses(<PipelineManagerWidget />)
  fireEvent.click((await screen.findAllByLabelText('打开文件夹'))[0])
}

/** 行内「打开对话」按钮：整行点按现为切换详情展开/收起（需求③），
 *  打开对话走行内操作区按钮——行内定位走行容器，不受信息区分区影响 */
function rowOpenChatButton(entryName: string): HTMLElement {
  const el = screen.getAllByText(entryName)[0]
  const row = (el.closest('tr') ?? el.closest('div')) as HTMLElement
  const btn = row.querySelector('button[aria-label="打开对话"]') as HTMLElement | null
  expect(btn, `row open-chat button for ${entryName}`).toBeTruthy()
  return btn as HTMLElement
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
    // 会话联动状态跨用例复位（store 为真实单例，防泄漏串扰）
    useSessionStore.setState({ activeSessionId: null })
  })

  it('未知状态任务保留且任务原始状态进详情（行内不再重复表达）', async () => {
    renderPmAllStatuses(<PipelineManagerWidget />)
    // 三个任务都出现（未知状态不再被吞）
    expect((await screen.findAllByText('评估中任务')).length).toBeGreaterThanOrEqual(1)
    expect((await screen.findAllByText('未知状态任务')).length).toBeGreaterThanOrEqual(1)
    expect((await screen.findAllByText('取消任务')).length).toBeGreaterThanOrEqual(1)
    // 任务态 chip 与运行态图标分离展示：细态中文化（evaluating → 评估中），
    // 未知值回退原串（pending_review）——原始值经 title 可查不占版面
    // 任务原始状态不再占行内版面（④：同类信息单一表达形态）——细态中文化
    // （evaluating → 评估中）、未知值回退原串（pending_review）进详情面板可见
    expect(screen.queryAllByText(/任务:评估中/)).toHaveLength(0)
    expect(screen.queryAllByText(/任务:pending_review/)).toHaveLength(0)
    // 展开任务行 → 详情面板给出任务原始状态（中文化细态 + 原始值）
    async function openDetail(titleText: string) {
      const row = ((await screen.findAllByText(titleText))[0].closest('div') ??
        null) as HTMLElement | null
      const btn = row?.querySelector('button[title*="详细信息"]') as HTMLElement | null
      expect(btn).toBeTruthy()
      fireEvent.click(btn!)
    }
    await openDetail('评估中任务')
    await waitFor(() => expect(screen.getAllByText('任务状态').length).toBeGreaterThanOrEqual(1))
    expect(screen.getAllByText(/评估中（evaluating）/).length).toBeGreaterThanOrEqual(1)
    await openDetail('未知状态任务')
    expect(screen.getAllByText(/pending_review/).length).toBeGreaterThanOrEqual(1)
    // 词汇收敛（ADR 2026-10-30-task-status-authority-vocabulary-convergence）：
    // legacy cancelled 任务显示过渡映射「已暂停（可恢复）」（别名折叠 stopped，
    // 非失败非取消——一词一义）
    expect((await screen.findAllByTitle(/运行状态：已暂停（可恢复）/)).length).toBeGreaterThanOrEqual(1)
  })

  it('展开详情含 state 真值行', async () => {
    renderPmAllStatuses(<PipelineManagerWidget />)
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
    renderPmAllStatuses(<PipelineManagerWidget />)
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
    renderPmAllStatuses(<PipelineManagerWidget />)
    // pipeA 既有任务派生条目（一对一合并为条目行）也有 state 摘要——去重后不得
    // 再出现 state 回退名"会话 th-a"的重复行（同 key 双行会让展开/详情状态串扰）
    await screen.findAllByText('评估中任务')
    expect(screen.queryByText('会话 th-a')).toBeNull()
  })

  it('子任务按父管道 id 挂到主管道节点下（树形而非顶层平铺）', async () => {
    pmSeed.useAllTasksQuery.mockReturnValue({ data: SUBTASK_TASKS })
    renderPmAllStatuses(<PipelineManagerWidget />)
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

  it('子任务（parent_task_id=会话主管道条目）渲染为该条目的子节点', async () => {
    pmSeed.usePipelineRunsQuery.mockReturnValue({ data: SESSION_MAIN_RUNS })
    pmSeed.useAllTasksQuery.mockReturnValue({ data: SESSION_MAIN_TASKS })
    renderPmAllStatuses(<PipelineManagerWidget />)
    // 树默认收起：点主管道条目行首 chevron（有子级才渲染）展开子树
    const chevron = (await screen.findAllByText('主会话'))[0].closest('div')?.querySelector('button')
    expect(chevron).toBeDefined()
    fireEvent.click(chevron!)
    // 主管道条目（kind=session）+ 子任务条目行都渲染（子任务不得整体丢失）
    expect((await screen.findAllByText('子任务A')).length).toBeGreaterThanOrEqual(1)
    // 子任务条目行带缩进（depth>0，挂主管道条目下而非顶层平铺）
    const subtaskRow = (await screen.findAllByText('子任务A')).find((el) =>
      el.closest('div')?.className.includes('hover:bg-muted'),
    )
    expect(subtaskRow).toBeDefined()
    const padding = subtaskRow?.closest('div')?.getAttribute('style') ?? ''
    expect(padding).toMatch(/padding-left:\s*2[48]px/)
  })

  it('运行中视图兄弟任务平级挂主管道锚点行下（主管道自身已完成时合成锚点）', async () => {
    pmSeed.usePipelineRunsQuery.mockReturnValue({ data: SIBLING_THREAD_RUNS })
    pmSeed.useAllTasksQuery.mockReturnValue({ data: SIBLING_THREAD_TASKS })
    pmSeed.sessions.push({ id: 'th-sib', title: '帮我多次调研', pipelineIds: ['pipe-sib-main'] })
    try {
      // 缺省运行中视图：主管道条目自身已完成被滤掉，锚点行由全量条目合成补渲染
      renderWithProviders(<PipelineManagerWidget />)
      // 整族在「执行中的管道」组（分组看子树是否含执行中成员，不按锚点自身终态
      // 误入「最近完成」）；本种子无其他条目 → 「最近完成」组不渲染
      expect(await screen.findByText(/执行中的管道/)).toBeInTheDocument()
      expect(screen.queryByText(/最近完成/)).toBeNull()
      // 主管道锚点行可见（行名 = 会话标题），顶层 depth 0 = 8px
      const mainRow = (await screen.findAllByText('帮我多次调研'))[0].closest('div')
      expect(mainRow?.getAttribute('style')).toMatch(/padding-left:\s*8px/)
      fireEvent.click(mainRow?.querySelector('button') as HTMLElement)
      // 两个兄弟子任务平级挂锚点行下（同为 depth 1 = 24px），不互挂
      const rowA = (await screen.findAllByText('补充调研A'))[0].closest('div')
      const rowB = (await screen.findAllByText('补充调研B'))[0].closest('div')
      expect(rowA?.getAttribute('style')).toMatch(/padding-left:\s*24px/)
      expect(rowB?.getAttribute('style')).toMatch(/padding-left:\s*24px/)
    } finally {
      pmSeed.sessions.length = 0
    }
  })

  it('会话主管道缺席时线程组任务落根平铺（树与列表同量，兄弟不互挂）', async () => {
    pmSeed.usePipelineRunsQuery.mockReturnValue({ data: ORPHAN_THREAD_RUNS })
    pmSeed.useAllTasksQuery.mockReturnValue({ data: ORPHAN_THREAD_TASKS })
    renderPmAllStatuses(<PipelineManagerWidget />)
    // 顶层即可见：同线程任务（无主管道锚点 → 平级落根）、无线程任务、孤儿会话
    expect((await screen.findAllByText('陨石躲避')).length).toBe(1)
    expect(screen.getAllByText('通道实测').length).toBe(1)
    // 孤儿会话行无 title 键（旧数据）→ 行名不落短 ID（用户裁定 2026-11-06），
    // 显「未命名」（行级锚定：泄漏的面外行不参与计数）
    const ghostRow = document.querySelector('div[data-entry-key="ghost13009006"]')
    expect(ghostRow).not.toBeNull()
    expect(
      ghostRow!.querySelector('[data-testid="pipeline-entry-title"]')!.textContent,
    ).toBe('未命名')
    // 兄弟不互挂：工具链自检不再嵌进最早兄弟陨石躲避行下（顶层平铺 depth 0 = 8px）
    const toolRow = screen.getAllByText('工具链自检')[0].closest('div')
    expect(toolRow?.getAttribute('style')).toMatch(/padding-left:\s*8px/)
    // parent_task_id 锚定不受影响：评估子任务仍挂父任务行下（depth 1 = 24px）
    const toolChevron = toolRow?.querySelector('button')
    fireEvent.click(toolChevron!)
    const evalRow = (await screen.findAllByText('评估子任务'))[0].closest('div')
    expect(evalRow?.getAttribute('style')).toMatch(/padding-left:\s*24px/)
  })

  it('挂会话主管道下的非任务子管道仍渲染（防回归）', async () => {
    pmSeed.usePipelineRunsQuery.mockReturnValue({ data: SESSION_MAIN_RUNS })
    // 主管道身份 = session.pipelineIds[0]（61e54023e）——播种会话登记锚定层级
    pmSeed.sessions.push({ id: 'th-1', title: '主会话', pipelineIds: ['mainpipe111'] })
    try {
      renderPmAllStatuses(<PipelineManagerWidget />)
      // 树默认收起：先展开主管道条目
      const chevron = (await screen.findAllByText('主会话'))[0].closest('div')?.querySelector('button')
      fireEvent.click(chevron!)
      // 主管道锚点行 depth 0（8px）；直接子管道行 depth>0（挂主管道条目下）
      const mainRow = (await screen.findAllByText('主会话'))[0].closest('div')
      expect(mainRow?.getAttribute('style')).toMatch(/padding-left:\s*8px/)
      const childRows = await screen.findAllByText('子任务A')
      expect(childRows.length).toBeGreaterThanOrEqual(1)
      const indented = childRows.some(
        (el) => el.closest('div')?.getAttribute('style')?.match(/padding-left:\s*2[48]px/),
      )
      expect(indented).toBe(true)
    } finally {
      pmSeed.sessions.length = 0
    }
  })

  it('任务条目行渲染打开工作空间按钮并开 workspace 文件树标签', async () => {
    pmSeed.useAllTasksQuery.mockReturnValue({ data: WS_TASKS })
    renderPmAllStatuses(<PipelineManagerWidget />)
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
    expect(tab?.title).toBe('子任务')
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
          title: '会话工作区',
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
    renderPmAllStatuses(<PipelineManagerWidget />)
    // 树默认收起：先展开会话分组节点（行名 = state.title）
    const chevron = (await screen.findAllByText('会话工作区'))[0].closest('div')?.querySelector('button')
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
    renderPmAllStatuses(<PipelineManagerWidget />)
    expect(await screen.findAllByText('子任务')).toHaveLength(1)
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
          title: '主会话行',
          status: 'running',
          started_at: '2026-08-24T00:00:00Z',
        },
      },
    })
    pmSeed.useAllTasksQuery.mockReturnValue({ data: [] })
    pmSeed.usePipelineStatesQuery.mockReturnValue({ data: {} })
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('主会话行')
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
          title: '阻断定位行',
          status: 'running',
          started_at: '2026-08-24T00:00:00Z',
        },
      },
    })
    pmSeed.useAllTasksQuery.mockReturnValue({ data: [] })
    pmSeed.usePipelineStatesQuery.mockReturnValue({ data: {} })

    const tabsBefore = useAgentTabStore.getState().tabs.length
    renderPmAllStatuses(<PipelineManagerWidget />)
    // 打开对话走行内按钮（整行点按现为切换详情——需求③）
    fireEvent.click(rowOpenChatButton('阻断定位行'))

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
          title: '自环任务',
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
    renderPmAllStatuses(<PipelineManagerWidget />)
    // 打开对话走行内按钮（整行点按现为切换详情——需求③）
    fireEvent.click(rowOpenChatButton('自环任务'))

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
    // 旧数据（无 lineage.origin_session_id）不可跳转——孤儿分支保持原语义。
    // 孤儿子标签路径以有活跃会话为前提（Tab 条随 ChatContainer 挂载；无活跃
    // 会话时点击走重建/显式反馈链，由 gaps 文件的会话记录缺失族覆盖）
    useSessionStore.setState({ activeSessionId: 'th-root' })
    pmSeed.readSessions.mockReturnValue([{ id: 'th-root', title: '根会话' }])
    pmSeed.usePipelineRunsQuery.mockReturnValue({
      data: {
        subRun: {
          pipeline_id: 'legacyPipe',
          run_id: 'run-legacy',
          thread_id: 'legacyPipe',
          title: '遗留自环行',
          status: 'running',
          started_at: '2026-08-29T00:00:00Z',
        },
      },
    })
    pmSeed.usePipelineStatesQuery.mockReturnValue({ data: {} })
    pmSeed.useAllTasksQuery.mockReturnValue({ data: [] })

    renderPmAllStatuses(<PipelineManagerWidget />)
    // 会话筛选跟随 activeSessionId（th-root）：legacyPipe 无归属会被滤掉，先 × 清除
    fireEvent.click(await screen.findByLabelText('移除会话筛选'))
    // 打开对话走行内按钮（整行点按现为切换详情——需求③）
    fireEvent.click(rowOpenChatButton('遗留自环行'))

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
    renderPmAllStatuses(<PipelineManagerWidget />)
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

    renderPmAllStatuses(<PipelineManagerWidget />)
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

  it('项目行工作空间入口（第二组输入）：裸登记 id 契约对另一 id 同构成立', async () => {
    // 第二组输入（不同项目 id）：裸 id 契约防拟合——proj-ws-a 用例外再验一组
    seedProjectQueries({ id: 'proj-ws-b', goal: '工作区项目B', metadata: { path: 'D:/repos/b' } })

    renderPmAllStatuses(<PipelineManagerWidget />)
    fireEvent.click((await screen.findAllByTitle('打开工作空间: D:/repos/b'))[0])

    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.some((t) => t.dataSource === 'workspace://proj-ws-b')).toBe(true)
    expect(tabs.some((t) => t.dataSource === 'workspace://project-proj-ws-b')).toBe(false)
  })

  it('项目行工作空间入口带裸登记 id 开工作区页（key 的 project- 前缀不得进数据源）', async () => {
    // 缺陷①回归锚（用户实报 2026-10-01）：项目条目无 taskId/pipelineId，wsId
    // 曾落 entry.key=project-<id>——后端项目登记通道按裸 id 查不中，工作区页
    // 永远 no_workspace，文件树空还报「任务树加载失败」
    seedProjectQueries({ id: 'proj-ws-a', goal: '工作区项目A', metadata: { path: 'D:/repos/a' } })

    renderPmAllStatuses(<PipelineManagerWidget />)
    const btns = await screen.findAllByTitle('打开工作空间: D:/repos/a')
    fireEvent.click(btns[0])

    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.some((t) => t.dataSource === 'workspace://proj-ws-a')).toBe(true)
    expect(tabs.some((t) => t.dataSource === 'workspace://project-proj-ws-a')).toBe(false)
  })

  it('打开文件夹成功：成功通知回显后端 message（操作必有着落）', async () => {
    seedProjectQueries({ id: 'proj-ok', goal: '成功项目' })
    pmSeed.workspaceOpen.mockResolvedValueOnce({
      data: { success: true, message: '已在系统文件管理器中打开工作空间' },
    })
    const addNotification = vi
      .spyOn(useNotificationStore.getState(), 'addNotification')
      .mockImplementation(() => {})
    await renderAndOpenFirstFolderButton()

    await waitFor(() => {
      expect(addNotification).toHaveBeenCalledWith(
        expect.objectContaining({
          title: '已打开文件夹',
          message: '已在系统文件管理器中打开工作空间',
        }),
      )
    })
    // 成功路径不得误触发失败通道
    expect(
      addNotification,
    ).not.toHaveBeenCalledWith(expect.objectContaining({ title: '打开文件夹失败' }))
    addNotification.mockRestore()
  })

  it('打开文件夹成功（信封无 message）：通知落项目名兜底文案', async () => {
    // 第二组输入：200 信封无 message——成功通知仍须可读（带项目名）
    seedProjectQueries({ id: 'proj-ok-plain', goal: '朴素成功项目' })
    pmSeed.workspaceOpen.mockResolvedValueOnce({ data: {} })
    const addNotification = vi
      .spyOn(useNotificationStore.getState(), 'addNotification')
      .mockImplementation(() => {})
    await renderAndOpenFirstFolderButton()

    await waitFor(() => {
      expect(addNotification).toHaveBeenCalledWith(
        expect.objectContaining({
          title: '已打开文件夹',
          message: '项目「朴素成功项目」文件夹已在文件管理器中打开',
        }),
      )
    })
    addNotification.mockRestore()
  })

  it('项目登记行无管道语义：无运行态/无归属徽标，详情卡出项目字段', async () => {
    // 回归锚（真机 2026-09-21）：旧实现给项目行硬编码 status:'running'，
    // 登记时间被当开始时间 → 「运行中 45h22m + 管道 ID project-xxx + 无归属」
    seedProjectQueries({
      id: 'proj-dereg',
      goal: '去管道化项目',
      metadata: { path: 'D:/repos/demo' },
    })

    renderPmAllStatuses(<PipelineManagerWidget />)
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

    renderPmAllStatuses(<PipelineManagerWidget />)
    expect(await screen.findByText('分组项目')).toBeInTheDocument()
    // 项目组标题存在（单视图挂载：组头 1 + 树行「项目」徽标 1 = 2；项目行无
    // agent 名——类型徽标只退役任务/会话两字样，用户裁定 2026-11-06）
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

    renderPmAllStatuses(<PipelineManagerWidget />)
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

    renderPmAllStatuses(<PipelineManagerWidget />)
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

    renderPmAllStatuses(<PipelineManagerWidget />)
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
    renderPmAllStatuses(<PipelineManagerWidget />)
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
    renderPmAllStatuses(<PipelineManagerWidget />)
    expect((await screen.findAllByTitle(/运行状态：已完成/)).length).toBeGreaterThanOrEqual(1)
  })

  it('默认状态筛选（无会话）=运行中：「运行中」选中态，点掉恢复全量（空集=全部）', async () => {
    pmSeed.usePipelineRunsQuery.mockReturnValue({
      data: {
        doneRun: {
          pipeline_id: 'pipeDone',
          run_id: 'r-done',
          thread_id: 'th-done',
          title: '已完成行',
          status: 'completed',
          started_at: '2026-08-30T00:00:00Z',
          ended_at: '2026-08-30T00:01:00Z',
        },
        liveRun: {
          pipeline_id: 'pipeLive',
          run_id: 'r-live',
          thread_id: 'th-live',
          title: '进行中行',
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

    // 默认态（无活跃会话）：「运行中」标签选中；无会话 chip、无「全部」项
    // （标签组空集 = 全部）
    expect(screen.getByRole('button', { name: '状态：运行中' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.queryByText(/会话: /)).not.toBeInTheDocument()
    // 在跑条目可见（会话列表无命中 → 名字回退「会话 <thread 前 8>」）
    expect(await screen.findByText('进行中行')).toBeInTheDocument()
    // 终态条目与项目登记行默认隐藏（最近完成组无内容不渲染，空项目组不占位）
    expect(screen.queryByText('已完成行')).not.toBeInTheDocument()
    expect(screen.queryByText('默认视野项目')).not.toBeInTheDocument()
    expect(screen.queryByText('最近完成')).not.toBeInTheDocument()

    // 点「运行中」取消 → 状态组空集 = 全量视野
    fireEvent.click(screen.getByRole('button', { name: '状态：运行中' }))
    expect(await screen.findByText('已完成行')).toBeInTheDocument()
    expect(screen.getByText('默认视野项目')).toBeInTheDocument()
    expect(screen.getByText('最近完成')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '状态：运行中' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
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

    fireEvent.click(screen.getByRole('button', { name: '状态：运行中' }))
    expect(await screen.findByText('空项目')).toBeInTheDocument()
  })

  // ────────────────── chips + 关键词筛选（2026-10 chips 模型） ──────────────────

  it('关键词筛选：名称命中收窄（区分度输入一：任务名子串）', async () => {
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('评估中任务')
    fireEvent.change(screen.getByLabelText('搜索管道'), { target: { value: '评估' } })
    expect(screen.getByText('评估中任务')).toBeInTheDocument()
    expect(screen.queryByText('未知状态任务')).not.toBeInTheDocument()
    expect(screen.queryByText('取消任务')).not.toBeInTheDocument()
  })

  it('关键词筛选：agent 大小写不敏感命中（区分度输入二：agentName 大写）', async () => {
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('评估中任务')
    fireEvent.change(screen.getByLabelText('搜索管道'), { target: { value: 'GENERAL_AGENT' } })
    // 三个任务条目 agentName=general_agent 全命中；无 agent 字段的会话条目被滤掉
    expect(screen.getByText('评估中任务')).toBeInTheDocument()
    expect(screen.getByText('未知状态任务')).toBeInTheDocument()
    expect(screen.getByText('取消任务')).toBeInTheDocument()
    expect(screen.queryByText('主管道')).not.toBeInTheDocument()
    // 搜索框清空 × → 恢复全量（两条会话条目同名，用 getAll 断言）
    fireEvent.click(screen.getByLabelText('清空搜索'))
    expect(screen.getAllByText('主管道').length).toBeGreaterThanOrEqual(1)
  })

  it('关键词 + 会话维度 AND 叠加；无命中显示专用空态', async () => {
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('评估中任务')
    pmSeed.sessions.push({ id: 'th-1', title: '叠加会话' })
    try {
      // 会话维度由活跃会话驱动（行级「只看此会话」入口已下线——需求④）：
      // 切活跃会话即收窄到该会话，再叠关键词 → AND 双维度
      act(() => {
        useSessionStore.setState({ activeSessionId: 'th-1' })
      })
      expect(await screen.findByText(/会话: 叠加会话/)).toBeInTheDocument()
      expect(screen.queryByText('评估中任务')).not.toBeInTheDocument()
      // 关键词命中条目标题（统一管道标题）→ 双维度同过可见
      fireEvent.change(screen.getByLabelText('搜索管道'), { target: { value: '主管道' } })
      expect(screen.getAllByText('主管道').length).toBeGreaterThanOrEqual(1)
      // 关键词换任务名：名称命中但会话维度不过 → 仍被滤掉（AND 叠加）
      fireEvent.change(screen.getByLabelText('搜索管道'), { target: { value: '任务' } })
      expect(screen.queryByText('评估中任务')).not.toBeInTheDocument()
      // 全维度无命中 → 专用空态（与首载 loading、暂无运行记录区分）
      fireEvent.change(screen.getByLabelText('搜索管道'), { target: { value: 'zzz-无此条目' } })
      expect(screen.getByText('无匹配结果（试试清除筛选或关键词）')).toBeInTheDocument()
    } finally {
      pmSeed.sessions.length = 0
    }
  })

  it('会话自动跟随：切换活跃会话 → 出会话 chip 且列表收窄', async () => {
    pmSeed.sessions.push({ id: 'th-1', title: '跟随主会话' })
    try {
      renderPmAllStatuses(<PipelineManagerWidget />)
      await screen.findAllByText('评估中任务')
      // 聊天侧切换会话（store 真实通道）
      act(() => {
        useSessionStore.setState({ activeSessionId: 'th-1' })
      })
      // chip 出现（标题取会话列表），列表收窄到 threadId=th-1 的条目
      // （条目名 = 统一管道标题，两条 runs 各有 title，getAll 断言）
      expect(await screen.findByText(/会话: 跟随主会话/)).toBeInTheDocument()
      expect(screen.getAllByText('主管道').length).toBeGreaterThanOrEqual(1)
      expect(screen.queryByText('评估中任务')).not.toBeInTheDocument()
    } finally {
      pmSeed.sessions.length = 0
    }
  })

  it('× 会话 chip 后再切会话恢复跟随（会话筛选=用户选择的会话）', async () => {
    pmSeed.sessions.push({ id: 'th-1', title: '跟随主会话' }, { id: 'th-2', title: '二号会话' })
    try {
      renderPmAllStatuses(<PipelineManagerWidget />)
      await screen.findAllByText('评估中任务')
      act(() => {
        useSessionStore.setState({ activeSessionId: 'th-1' })
      })
      expect(await screen.findByText(/会话: 跟随主会话/)).toBeInTheDocument()
      // × 会话 chip：筛选清空（× 的「看全部」窗口在下一个会话选择动作前有效）
      fireEvent.click(screen.getByLabelText('移除会话筛选'))
      expect(screen.getByText('评估中任务')).toBeInTheDocument()
      expect(screen.queryByText(/会话: /)).not.toBeInTheDocument()
      // 再切会话 = 用户又选择了会话 → 恢复跟随（chip 换新会话）
      act(() => {
        useSessionStore.setState({ activeSessionId: 'th-2' })
      })
      expect(await screen.findByText(/会话: 二号会话/)).toBeInTheDocument()
      expect(screen.queryByText('评估中任务')).not.toBeInTheDocument()
    } finally {
      pmSeed.sessions.length = 0
    }
  })

  it('行级「只看此会话」入口已下线：会话维度改由活跃会话驱动（切换会话即跟随）', async () => {
    pmSeed.sessions.push({ id: 'th-1', title: '跟随主会话' }, { id: 'th-2', title: '二号会话' })
    try {
      renderPmAllStatuses(<PipelineManagerWidget />)
      await screen.findAllByText('评估中任务')
      // 入口下线：行操作区不再有「只看此会话」按钮（需求④：挤占行内操作区）
      expect(screen.queryAllByLabelText('只看此会话')).toHaveLength(0)
      // 会话维度改由活跃会话驱动：切到 th-1 → 收窄到该会话条目
      act(() => {
        useSessionStore.setState({ activeSessionId: 'th-1' })
      })
      expect(await screen.findByText(/会话: 跟随主会话/)).toBeInTheDocument()
      expect(screen.queryByText('评估中任务')).not.toBeInTheDocument()
      // 随动语义：再切活跃会话 → chip 跟着换（行级钉住入口下线后无钉住通道）
      act(() => {
        useSessionStore.setState({ activeSessionId: 'th-2' })
      })
      expect(await screen.findByText(/会话: 二号会话/)).toBeInTheDocument()
    } finally {
      pmSeed.sessions.length = 0
    }
  })

  it('会话维度血缘兜底（驱动源改由活跃会话）：threadId 不在会话列表按 originSessionId 命中', async () => {
    // 自环子任务管道（thread_id=自身 id 不在会话列表），state 带血缘根会话
    pmSeed.readSessions.mockReturnValue([{ id: 'th-root', title: '根会话' }])
    pmSeed.sessions.push({ id: 'th-root', title: '根会话' })
    pmSeed.usePipelineRunsQuery.mockReturnValue({
      data: {
        subRun: {
          pipeline_id: 'selfLoopPipe',
          run_id: 'run-self',
          thread_id: 'selfLoopPipe',
          title: '自环任务',
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
    try {
      renderPmAllStatuses(<PipelineManagerWidget />)
      // 会话维度由活跃会话驱动：切到血缘根会话 → 血缘子条目按 originSessionId 命中
      act(() => {
        useSessionStore.setState({ activeSessionId: 'th-root' })
      })
      expect(await screen.findByText(/会话: 根会话/)).toBeInTheDocument()
      // 血缘子条目（thread_id=自身 id，不在会话列表）按 originSessionId 命中可见
      expect(screen.getAllByText('自环任务').length).toBeGreaterThanOrEqual(1)
    } finally {
      pmSeed.sessions.length = 0
      pmSeed.readSessions.mockImplementation(() => [])
    }
  })

  it('会话 chip 标题回退：会话不在列表时落「会话 <id 前 8>」', async () => {
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('评估中任务')
    // 会话列表为空：行级解析无归属 → 按钮点了不动；走活跃会话自动跟随通道
    // 直接验证回退标签（activeSessionId 无需登记在会话列表）
    act(() => {
      useSessionStore.setState({ activeSessionId: 'abcdef1234567890' })
    })
    expect(await screen.findByText(/会话: 会话 abcdef12/)).toBeInTheDocument()
  })

  it('平铺筛选标签：状态互斥自动反选 + 点击取消，类型可叠加', async () => {
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('评估中任务')
    // 状态组（空集起步 = 全量）：点「已完成」→ 仅终态条目可见
    // （评估中任务=state 终态 completed；取消任务=cancelled 不匹配）
    fireEvent.click(screen.getByRole('button', { name: '状态：已完成' }))
    expect(screen.getByRole('button', { name: '状态：已完成' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByText('评估中任务')).toBeInTheDocument()
    expect(screen.queryByText('取消任务')).not.toBeInTheDocument()
    expect(screen.queryByText('未知状态任务')).not.toBeInTheDocument()
    // 再点「已完成」→ 取消选中，恢复全量（无「全部」项，空集 = 全部）
    fireEvent.click(screen.getByRole('button', { name: '状态：已完成' }))
    expect(await screen.findByText('评估中任务')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '状态：已完成' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
    // 互斥：点「运行中」→ 独占视野；再点「已完成」→ 运行中自动反选取消
    fireEvent.click(screen.getByRole('button', { name: '状态：运行中' }))
    expect(screen.getByRole('button', { name: '状态：运行中' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.queryByText('评估中任务')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '状态：已完成' }))
    expect(screen.getByRole('button', { name: '状态：已完成' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByRole('button', { name: '状态：运行中' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
    expect(screen.getByText('评估中任务')).toBeInTheDocument()
    // 类型组叠加：任务 + 会话同时选中 = 并集；再点取消单项
    fireEvent.click(screen.getByRole('button', { name: '类型：任务' }))
    expect(screen.getByRole('button', { name: '类型：任务' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByText('评估中任务')).toBeInTheDocument()
    expect(screen.queryByText('主管道')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '类型：会话' }))
    expect(screen.getByRole('button', { name: '类型：会话' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByText('评估中任务')).toBeInTheDocument()
    expect(screen.getAllByText('主管道').length).toBeGreaterThanOrEqual(1)
    fireEvent.click(screen.getByRole('button', { name: '类型：任务' }))
    expect(screen.queryByText('评估中任务')).not.toBeInTheDocument()
    expect(screen.getAllByText('主管道').length).toBeGreaterThanOrEqual(1)
  })

  it('focusTaskId 定位流：清空关键词与会话筛选，终态任务行必可见', async () => {
    pmSeed.sessions.push({ id: 'th-1', title: '跟随主会话' }, { id: 'th-2', title: '二号会话' })
    try {
      // 首挂即联动筛到 th-1（跟随默认开）；终态任务 task-eval（pipeA 无 threadId）
      // 在该视野下不可见——定位流必须清掉会话/状态筛选才能看到目标行
      act(() => {
        useSessionStore.setState({ activeSessionId: 'th-1' })
      })
      renderWithProviders(<PipelineManagerWidget focusTaskId="task-eval" />)
      // 定位生效：目标行可见（状态/会话筛选均被清除——状态组空集，
      // 「运行中」未选中）
      expect(await screen.findByText('评估中任务')).toBeInTheDocument()
      expect(screen.queryByText(/会话: /)).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: '状态：运行中' })).toHaveAttribute(
        'aria-pressed',
        'false',
      )
      // 定位是一次性解除：之后切换会话恢复跟随（会话筛选=用户选择的会话）
      act(() => {
        useSessionStore.setState({ activeSessionId: 'th-2' })
      })
      expect(await screen.findByText(/会话: 二号会话/)).toBeInTheDocument()
    } finally {
      pmSeed.sessions.length = 0
    }
  })
})

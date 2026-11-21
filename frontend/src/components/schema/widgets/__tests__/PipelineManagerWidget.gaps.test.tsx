// @feature FP-T12 前端适配 | @ci frontend-test
/**
 * PipelineManagerWidget 覆盖缺口补充测试（与既有 PipelineManagerWidget.test 互补，不重复）：
 * - 工具栏：状态平铺标签组（可叠加、空集 = 全部；含「运行中」任意状态间取
 *   并集；类型筛选已退役，留断言防回归）
 * - 树视图：执行中/最近完成分组折叠、会话主管道（pipelineIds）子级缩进、
 *   项目挂靠任务缩进、组头计数
 * - 行内操作（方案 B 行布局，契约真值 = PipelineManagerTreeParts.tsx 文件头）：
 *   常驻「发消息」（开对话）、「⋮」菜单（复制管道 ID / 停止 / 继续 / 删除管道 /
 *   打开工作空间）、详情手风琴（点任务名称开合，aria-expanded）；clipboard
 *   成功/拒绝/缺失、删除确认弹窗流、失败 toast 与 pending 复位、既有 Tab 仅激活
 * - 打开对话分流：归属会话直跳、导航失败回退 setActiveSession、回退再失败通知、
 *   既有同管道 Tab 仅切换、孤儿条目建子标签的 6 态状态映射
 * - 详情面板：结束/错误/已结束=否/Token 汇总/Token 实时/进度条（含 >100 clamp）
 * - 空数据空态、项目行点击不开对话、删除弹窗 Esc/取消关闭
 * - 任务管道 ID 双取（metadata.pipelineRunId）、实时 token 优先、total_tokens 三形态
 *
 * 方案 B 适配登记（2026-11-22，用户拍板 8 条新行布局；旧平铺按钮退役，断言
 * 迁移到新形态，禁止按旧布局回退产品代码）：
 * - 「打开对话」平铺按钮 → 常驻「发消息」按钮（aria-label=发消息）
 * - 「复制管道 ID」「暂停任务」「恢复任务」「删除管道」「打开工作空间」平铺
 *   按钮 → 「⋮」菜单项（触发面 pointerdown，菜单项 onSelect 经 click 触发）
 * - 「切换详细信息」chevron 按钮 → 点任务名称行内展开详情（aria-expanded 承载）
 * - 暂停 await 窗口的 disabled 断言 → 详情操作条「停止」按钮（await 窗口内
 *   保持挂载，行内无常驻暂停按钮可断言）
 * - 删除用例：行内「打开任务现场」动作（scene 分流）×2 —— 任务现场列表入口
 *   已移除（用户裁定 2026-11-20，消息流「最新 AI 气泡状态行」为唯一主入口），
 *   ⋮ 菜单与行内均无该动作，方案 B 无对应新行为
 *
 * 行为对齐（2026-09-23，产品行为有意变更后测试跟进，禁止回退产品代码）：
 * - 面板状态筛选缺省：无活跃会话 → 只看运行中（5971fdaa1，用户裁定 2026-09-21，
 *   2026-10-17 起有活跃会话时缺省空集 = 全部）——无会话用例需全量条目可见的
 *   经 renderPmAllStatuses 点掉「运行中」标签；
 * - 项目登记行去管道语义（a2585be70：登记行无运行态，不进执行中分组/状态
 *   筛选/耗时 ticker）——登记行相关断言同样只在全量视图可见；
 * - 会话主管道身份 = session.pipelineIds[0]（61e54023e）——树缩进/导航用例
 *   种子按该语义播种（主管道挂会话 pipelineIds[0]）。
 */

import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
// 顺序约束：先于被测组件 import——vi.mock 工厂体在被测组件初始化时执行，
// 届时本模块必须已求值（见 pmTestUtils 头注）
// eslint-disable-next-line import-x/order -- pmTestUtils 须先于被测组件求值（vi.mock 工厂时序约束，见 pmTestUtils 头注；禁用排序自动修防回归）
import { pmMod, pmSeed, renderPmAllStatuses, resetPmContainers } from './pmTestUtils'
import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useLongTermTaskStore } from '@/stores/longTermTaskStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { useSessionStore } from '@/stores/sessionStore'
import { renderWithProviders } from '@/test/renderWithProviders'
import type { AgentTab } from '@/types/task'

// 交互全量文件：行内操作/删除弹窗/打开工作空间/通知回退全部走真实 handler 路径，
// 全部端点与句柄经 pmSeed 可断言；sessionStore 未进入面板渲染树，无需 mock。
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
// 操作失败反馈断言面：toast（操作点可见，OBS-R258-1 即时性收口）
const toastMocks = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), warning: vi.fn(), info: vi.fn() }))
vi.mock('@/components/ui/sonner', () => ({ toast: toastMocks }))

// ────────────────────────── 公共基建 ──────────────────────────

/** 标准播种：2 任务 + 1 挂起任务 + 1 metadata 双取任务 + 2 会话管道 + 1 孤儿管道 */
function seedStandard() {
  // 统一管道标题（ADR 2026-10-27）：行名 = run.title；无 title 落「未命名」
  // （用户裁定 2026-11-06：代号 ID 不进行内，孤儿行补 title 作定位锚）
  pmSeed.runs.r1 = {
    pipeline_id: 'pipe-running', run_id: 'run-1', thread_id: 'th-1', title: '运行任务',
    status: 'running', started_at: '2026-09-01T00:00:00Z',
  }
  pmSeed.runs.r2 = {
    pipeline_id: 'pipe-done', run_id: 'run-2', thread_id: 'th-1', title: '完成管道',
    status: 'completed',
    started_at: '2026-09-01T00:01:00Z', ended_at: '2026-09-01T00:02:00Z',
    total_tokens: { input: 10, output: 20, total: 51234 },
  }
  pmSeed.runs.r3 = {
    pipeline_id: 'pipe-orph', run_id: 'run-3', status: 'suspended',
    title: '孤儿管道',
    started_at: '2026-09-01T00:03:00Z',
  }
  pmSeed.tasks.push(
    { id: 't-run', title: '运行任务', status: 'running', pipeline_run_id: 'pipe-running', agent_name: 'general_agent' },
    { id: 't-susp', title: '挂起任务', status: 'suspended', pipeline_run_id: 'pipe-susp', agent_name: 'general_agent', timestamps: { startedAt: '2026-09-01T00:04:00Z' } },
    { id: 't-meta', title: 'meta任务', status: 'completed', metadata: { pipelineRunId: 'pipe-meta' }, timestamps: { startedAt: '2026-09-01T00:05:00Z' } },
  )
}

/** meta 任务替换为带工作空间坐标的载荷（打开工作空间族用例共用） */
function seedMetaWorkspaceTask() {
  pmSeed.tasks[2] = {
    ...pmSeed.tasks[2],
    metadata: { pipelineRunId: 'pipe-meta', ws_meta: { path: 'D:/ws/meta' } },
  }
}

/** 注入可写 clipboard 并渲染面板（复制 ID 用例共用；返回 writeText 句柄）。
 *  以全量视野渲染：meta任务等终态条目须在行内才可触发行内操作 */
function renderWithClipboard() {
  const writeText = vi.fn<(t: string) => Promise<void>>().mockResolvedValue(undefined)
  setClipboard({ writeText })
  renderPmAllStatuses(<PipelineManagerWidget />)
  return writeText
}

function resetStores() {
  useAgentTabStore.setState({ tabs: [], activeTabId: null })
  useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
  useNotificationStore.setState({ notifications: [] })
  useSessionStore.setState({ activeSessionId: null })
}

function setClipboard(value: { writeText: (t: string) => Promise<void> } | undefined) {
  Object.defineProperty(window.navigator, 'clipboard', { value, configurable: true })
}

/** 清除「会话筛选跟随」chip：activeSessionId 非空时面板跟随滤到该会话，
 *  播种宿主会话（孤儿子标签路径前提）后须手动 × 才能看全量条目 */
async function clearSessionFollow() {
  const chip = await screen.findByLabelText('移除会话筛选')
  fireEvent.click(chip)
}

/** 定位含指定条目名的行内按钮（title 或 aria-label 精确匹配；树行=div，列表行=tr） */
function rowButton(entryName: string, selector: string): HTMLElement {
  const el = screen.getAllByText(entryName)[0]
  const row = (el.closest('tr') ?? el.closest('div')) as HTMLElement
  const btn = row.querySelector(selector) as HTMLElement | null
  expect(btn, `row button ${selector} for ${entryName}`).toBeTruthy()
  return btn as HTMLElement
}

/** 行根容器（方案 B：@container/entry 行根是缩进 style 与 data-entry-key 载体；
 *  名称文本所在的主行 div 无 style，须再上溯到行容器） */
function rowContainer(entryName: string): HTMLElement {
  const el = screen.getAllByText(entryName)[0]
  const row = (el.closest('div[data-entry-key]') ?? el.closest('tr')) as HTMLElement
  expect(row, `row container for ${entryName}`).toBeTruthy()
  return row as HTMLElement
}

/** 常驻「发消息」按钮（方案 B：原「打开对话」平铺按钮已退役） */
function rowOpenChatButton(entryName: string): HTMLElement {
  return rowButton(entryName, 'button[aria-label="发消息"]')
}

/** 详情展开入口（方案 B：点任务名称行内展开手风琴，aria-expanded 承载开合态） */
function rowDetailToggle(entryName: string): HTMLElement {
  return rowButton(entryName, 'button[data-testid="pipeline-entry-title"]')
}

/** 行内「⋮」更多操作按钮（低频动作唯一常驻入口；详见文件头方案 B 适配登记） */
function rowMoreButton(entryName: string): HTMLElement {
  return rowButton(entryName, 'button[data-testid^="pipeline-entry-more-"]')
}

/** 打开行内「⋮」菜单并等菜单内容挂载（Radix 触发面 = pointerdown；
 *  先等目标行渲染，再触发，避免查询异步就绪前定位落空） */
async function openRowMenu(entryName: string): Promise<void> {
  await screen.findAllByText(entryName)
  fireEvent.pointerDown(rowMoreButton(entryName))
  await screen.findByRole('menu')
}

/** 树视图默认收起：先展开父行（首个 chevron）再取子行元素 */
async function expandTreeChild(parentName: string, childName: string) {
  const parentRow = ((await screen.findAllByText(parentName))[0]).closest('div') as HTMLElement
  fireEvent.click(parentRow.querySelector('button') as HTMLElement)
  return (await screen.findAllByText(childName))[0]
}

function makeTab(partial: Partial<AgentTab> & { id: string }): AgentTab {
  return {
    agentId: partial.id,
    agentName: partial.id,
    agentLevel: 2,
    path: [],
    status: 'running',
    hasUnread: false,
    canClose: true,
    ...partial,
  }
}

beforeEach(() => {
  resetPmContainers()
  resetStores()
  setClipboard(undefined)
  pmSeed.navigateToPipeline.mockReset().mockResolvedValue(true)
  pmSeed.readSessions.mockReset().mockReturnValue([])
  pmSeed.ensureSessionsLoaded.mockReset().mockResolvedValue([])
  pmSeed.setActiveSession.mockReset().mockResolvedValue(undefined)
  pmSeed.rebuildSessionFromThread.mockReset()
  pmSeed.pauseTask.mockReset().mockResolvedValue(undefined)
  pmSeed.resumeTask.mockReset().mockResolvedValue(undefined)
  pmSeed.cancelTask.mockReset().mockResolvedValue(undefined)
  pmSeed.workspaceOpen.mockReset().mockResolvedValue({ data: { success: true } })
  pmSeed.fetchProjects.mockReset().mockResolvedValue({ items: pmSeed.projects })
  toastMocks.error.mockReset()
  vi.spyOn(console, 'error').mockImplementation(() => {})
  useLongTermTaskStore.setState({ actingTaskIds: new Set() })
})

afterEach(() => {
  vi.restoreAllMocks()
})

// ────────────────────────── 工具栏筛选 ──────────────────────────

describe('PipelineManagerWidget 工具栏筛选', () => {
  it('类型筛选已整体移除：工具栏不再出现「类型」标签组，任务/会话条目同屏可见', async () => {
    seedStandard()
    // 断言含终态任务（meta任务）：无会话渲染经 renderPmAllStatuses 点掉缺省「运行中」
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    // 用户裁定 2026-11-20：任务/会话类型筛选不要——两个类型标签入口都不存在
    expect(screen.queryByRole('button', { name: '类型：任务' })).toBeNull()
    expect(screen.queryByRole('button', { name: '类型：会话' })).toBeNull()
    // 工具栏分组标签也不再出现
    expect(screen.queryByText('类型')).toBeNull()
    // 状态筛选仍在（只退役类型维）
    expect(screen.getByRole('button', { name: '状态：运行中' })).toBeInTheDocument()

    // 无类型筛选时任务条目与会话条目同屏（筛选不再按类型收窄）
    expect(screen.getByText('运行任务')).toBeInTheDocument()
    expect(screen.getByText('完成管道')).toBeInTheDocument()
  })

  it('状态筛选：可叠加标签组 + 「运行中」与其余状态可同选取并集 + 点击取消', async () => {
    seedStandard()
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    // 缺省「运行中」单选视野：仅运行态可见
    expect(screen.getByRole('button', { name: '状态：运行中' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.queryByText('meta任务')).toBeNull()

    // 点「已完成」→ 与「运行中」叠加取并集，两态条目都可见
    fireEvent.click(screen.getByRole('button', { name: '状态：已完成' }))
    expect(screen.getByRole('button', { name: '状态：已完成' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByRole('button', { name: '状态：运行中' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByText('meta任务')).toBeInTheDocument()
    expect(screen.getByText('完成管道')).toBeInTheDocument()
    expect(screen.getByText('运行任务')).toBeInTheDocument()

    // 点「已暂停」（键对齐 suspended）→ 三态并集
    fireEvent.click(screen.getByRole('button', { name: '状态：已暂停' }))
    expect(screen.getByRole('button', { name: '状态：已暂停' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByText('挂起任务')).toBeInTheDocument()
    expect(screen.getByText('孤儿管道')).toBeInTheDocument()
    expect(screen.getByText('meta任务')).toBeInTheDocument()
    expect(screen.getByText('运行任务')).toBeInTheDocument()

    // 再点「运行中」→ 仅取消自身，其余状态保持
    fireEvent.click(screen.getByRole('button', { name: '状态：运行中' }))
    expect(screen.getByRole('button', { name: '状态：运行中' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
    expect(screen.getByRole('button', { name: '状态：已完成' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByRole('button', { name: '状态：已暂停' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByText('meta任务')).toBeInTheDocument()
    expect(screen.getByText('挂起任务')).toBeInTheDocument()
    expect(screen.queryByText('运行任务')).toBeNull()
  })

  it('会话筛选激活时缺省空集（不默认只看运行中）：状态组全不选中，终态条目默认可见', async () => {
    seedStandard()
    useSessionStore.setState({ activeSessionId: 'th-1' })
    renderWithProviders(<PipelineManagerWidget />)
    // 会话 chip 在：会话筛选激活
    expect(await screen.findByText(/会话: /)).toBeInTheDocument()
    // 状态组空集：运行中未选中
    expect(screen.getByRole('button', { name: '状态：运行中' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
    // 会话内 completed 管道（pipe-done）默认可见——缺省「运行中」视野下不可见
    expect(await screen.findByText('完成管道')).toBeInTheDocument()
  })
})

// ────────────────────────── 树视图分组与层级 ──────────────────────────

describe('PipelineManagerWidget 树视图', () => {
  it('执行中/最近完成分组渲染，组头折叠收起条目再展开恢复', async () => {
    seedStandard()
    // 「最近完成」组含终态条目：切到全量视野（无会话缺省只看运行中，点掉「运行中」标签）
    renderPmAllStatuses(<PipelineManagerWidget />)

    expect(await screen.findByText(/执行中的管道/)).toBeInTheDocument()
    expect(screen.getByText(/最近完成/)).toBeInTheDocument()
    expect(screen.getByText('运行任务')).toBeInTheDocument()
    expect(screen.getByText('meta任务')).toBeInTheDocument()

    // 折叠执行中分组：其下条目收起
    fireEvent.click(screen.getByText(/执行中的管道/))
    expect(screen.queryByText('运行任务')).toBeNull()
    expect(screen.queryByText('孤儿管道')).toBeNull()
    // 最近完成分组不受影响
    expect(screen.getByText('meta任务')).toBeInTheDocument()

    // 再展开恢复
    fireEvent.click(screen.getByText(/执行中的管道/))
    expect(screen.getByText('运行任务')).toBeInTheDocument()
  })

  it('会话主管道（session.pipelineIds）优先：其余管道缩进挂其下', async () => {
    seedStandard()
    // 主管道身份 = session.pipelineIds[0]（61e54023e）；子管道 pipe-done 为终态，
    // 须全量视野可见（无会话缺省只看运行中，5971fdaa1）
    pmSeed.sessions.push({ id: 'th-1', title: '主会话', pipelineIds: ['pipe-running'] })
    renderPmAllStatuses(<PipelineManagerWidget />)

    // 主管道条目行（任务一对一绑定 → 行名 = 任务名）展开子级
    fireEvent.click(
      (await screen.findAllByText('运行任务'))[0].closest('div')?.querySelector('button') as HTMLElement,
    )
    const childRow = rowContainer('完成管道')
    expect(childRow.getAttribute('style') ?? '').toMatch(/padding-left:\s*2[48]px/)
  })

  it('项目挂靠任务（metadata.parent_project_id）挂项目分组节点下', async () => {
    // 项目登记行已去管道语义（a2585be70：无运行态、不进状态筛选）——全量视图
    // 才保留登记行/分组节点；挂靠任务无 runs 证据落 unknown 视图态（BUG-21）
    pmSeed.projects.push({ id: 'proj-9', goal: '挂靠项目', timestamps: { createdAt: '2026-09-01T00:00:00Z' } })
    pmSeed.tasks.push({
      id: 't-p', title: '挂靠任务', status: 'running', pipeline_run_id: 'pipe-p',
      metadata: { parent_project_id: 'proj-9' },
    })
    renderPmAllStatuses(<PipelineManagerWidget />)

    fireEvent.click(
      (await screen.findAllByText('挂靠项目'))[0].closest('div')?.querySelector('button') as HTMLElement,
    )
    const taskRow = rowContainer('挂靠任务')
    expect(taskRow.getAttribute('style') ?? '').toMatch(/padding-left:\s*2[48]px/)
  })
})

// ────────────────────────── 行内操作 ──────────────────────────

describe('PipelineManagerWidget 行内操作', () => {
  it('复制管道 ID：⋮ 菜单 clipboard 写入 pipeline_id；拒绝与缺失均静默', async () => {
    seedStandard()
    const writeText = renderWithClipboard()
    await screen.findAllByText('运行任务')

    await openRowMenu('运行任务')
    fireEvent.click(screen.getByText('复制管道 ID'))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('pipe-running'))

    // clipboard 拒绝 → 静默
    writeText.mockRejectedValueOnce(new Error('denied'))
    await openRowMenu('运行任务')
    fireEvent.click(screen.getByText('复制管道 ID'))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(2))
    expect(console.error).not.toHaveBeenCalled()

    // clipboard 缺失 → 静默
    setClipboard(undefined)
    await openRowMenu('运行任务')
    fireEvent.click(screen.getByText('复制管道 ID'))
    await waitFor(() => expect(console.error).not.toHaveBeenCalled())
  })

  it('⋮ 菜单停止/删除管道运行中任务 → 对应端点 + 任务列表失效刷新；失败 toast 且 pending 复位', async () => {
    seedStandard()
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    await openRowMenu('运行任务')
    fireEvent.click(screen.getByText('停止（暂停任务）'))
    await waitFor(() => expect(pmSeed.pauseTask).toHaveBeenCalledWith('t-run'))
    expect(pmSeed.invalidateLongTermTasks).toHaveBeenCalled()

    // 「打叉」位改删除管道（2026-11-11 用户指令）：⋮ 菜单点击开确认弹窗，
    // 确认后调 DELETE 任务端点（0.2 任务=管道，级联清理执行数据）
    await openRowMenu('运行任务')
    fireEvent.click(screen.getByText('删除管道'))
    fireEvent.click(await screen.findByRole('button', { name: '确认删除' }))
    await waitFor(() => expect(pmSeed.deleteTask).toHaveBeenCalledWith('t-run'))
    expect(pmSeed.invalidateLongTermTasks).toHaveBeenCalled()

    // 操作失败 → toast 落在操作点（不再 console.error 静默），pending 复位不残留
    pmSeed.pauseTask.mockRejectedValueOnce(new Error('pause boom'))
    await openRowMenu('运行任务')
    fireEvent.click(screen.getByText('停止（暂停任务）'))
    await waitFor(() =>
      expect(toastMocks.error).toHaveBeenCalledWith('暂停任务失败', {
        description: 'pause boom',
      }),
    )
    await waitFor(() =>
      expect(useLongTermTaskStore.getState().actingTaskIds.has('t-run')).toBe(false),
    )
  })

  it('暂停 await 窗口：详情「停止」按钮 disabled + actingTaskIds 置位（行级 pending）', async () => {
    seedStandard()
    let resolvePause: () => void = () => {}
    pmSeed.pauseTask.mockReturnValue(
      new Promise<void>((resolve) => {
        resolvePause = resolve
      }),
    )
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    // 方案 B：详情手风琴经任务名称展开；await 窗口断言落在常驻的详情操作条
    // 「停止」按钮（行内无常驻暂停按钮，⋮ 菜单项随菜单开合不常驻）
    fireEvent.click(rowDetailToggle('运行任务'))
    fireEvent.click(await screen.findByTestId('pipeline-detail-pause'))
    // await 窗口内：停止按钮禁用 + store 行级 pending 置位
    await waitFor(() =>
      expect(useLongTermTaskStore.getState().actingTaskIds.has('t-run')).toBe(true),
    )
    expect(screen.getByTestId('pipeline-detail-pause')).toBeDisabled()

    resolvePause()
    await waitFor(() =>
      expect(useLongTermTaskStore.getState().actingTaskIds.has('t-run')).toBe(false),
    )
    expect(screen.getByTestId('pipeline-detail-pause')).toBeEnabled()
  })

  it('恢复挂起任务 → ⋮ 菜单「继续」走 resume 端点', async () => {
    seedStandard()
    // 挂起条目不在缺省「运行中」视图：切到全量视野
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('挂起任务')

    await openRowMenu('挂起任务')
    fireEvent.click(screen.getByText('继续（恢复任务）'))
    await waitFor(() => expect(pmSeed.resumeTask).toHaveBeenCalledWith('t-susp'))
    expect(pmSeed.invalidateLongTermTasks).toHaveBeenCalled()
  })

  it('打开工作空间：⋮ 菜单入口，任务条目用 taskId 开 ws-tree Tab；已有 Tab 仅激活', async () => {
    seedStandard()
    seedMetaWorkspaceTask()
    useLayoutModeStore.setState({
      workspaceTabs: [
        { id: 'ws-tree-t-meta', title: 'meta任务', moduleId: '__dynamic__', component: 'file_tree', dataSource: 'workspace://t-meta', isActive: false, isPinned: false },
      ],
    })
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('meta任务')

    await openRowMenu('meta任务')
    fireEvent.click(screen.getByText('打开工作空间'))

    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs).toHaveLength(1)
    expect(tabs[0]).toMatchObject({ id: 'ws-tree-t-meta', isActive: true })
  })

  it('发消息（树视图）：归属会话条目走导航器', async () => {
    seedStandard()
    pmSeed.readSessions.mockReturnValue([{ id: 'th-1', title: '会话A' }])
    // 待展开子行（pipe-done，终态）不在缺省「运行中」视图：切到全量视野
    renderPmAllStatuses(<PipelineManagerWidget />)
    // 树默认收起：pipe-done 挂 pipe-running 下，先展开父行
    await expandTreeChild('运行任务', '完成管道')

    fireEvent.click(rowOpenChatButton('完成管道'))

    await waitFor(() =>
      expect(pmSeed.navigateToPipeline).toHaveBeenCalledWith(
        'pipe-done',
        expect.objectContaining({ taskId: undefined, agentLevel: 2 }),
      ),
    )
    // 导航成功 → 不回退切换会话
    expect(pmSeed.setActiveSession).not.toHaveBeenCalled()
  })

  it('导航返回 false → 回退切换归属会话；回退失败 → 用户可见通知', async () => {
    seedStandard()
    pmSeed.readSessions.mockReturnValue([{ id: 'th-1', title: '会话A' }])
    pmSeed.navigateToPipeline.mockReset().mockResolvedValue(false)
    const addNotification = vi
      .spyOn(useNotificationStore.getState(), 'addNotification')
      .mockImplementation(() => {})
    // 待展开子行（pipe-done，终态）不在缺省「运行中」视图：切到全量视野
    renderPmAllStatuses(<PipelineManagerWidget />)
    await expandTreeChild('运行任务', '完成管道')

    // 回退失败版本：先验证通知分支
    pmSeed.setActiveSession.mockReset().mockRejectedValue(new Error('switch boom'))
    fireEvent.click(rowOpenChatButton('完成管道'))
    await waitFor(() =>
      expect(addNotification).toHaveBeenCalledWith(
        expect.objectContaining({
          title: '无法打开对话',
          message: expect.stringContaining('pipe-done'),
        }),
      ),
    )

    // 回退成功版本：setActiveSession 收到归属会话 id
    pmSeed.setActiveSession.mockReset().mockResolvedValue(undefined)
    fireEvent.click(rowOpenChatButton('完成管道'))
    await waitFor(() => expect(pmSeed.setActiveSession).toHaveBeenCalledWith('th-1'))
    addNotification.mockRestore()
  })

  it('导航器拒绝（throw）→ 落回退切换会话', async () => {
    seedStandard()
    pmSeed.readSessions.mockReturnValue([{ id: 'th-1', title: '会话A' }])
    pmSeed.navigateToPipeline.mockReset().mockRejectedValue(new Error('nav down'))
    // 待展开子行（pipe-done，终态）不在缺省「运行中」视图：切到全量视野
    renderPmAllStatuses(<PipelineManagerWidget />)
    await expandTreeChild('运行任务', '完成管道')

    fireEvent.click(rowOpenChatButton('完成管道'))

    await waitFor(() => expect(pmSeed.setActiveSession).toHaveBeenCalledWith('th-1'))
  })

  it('已有同管道 Tab 的孤儿条目 → 仅切换既有 Tab，不新建', async () => {
    seedStandard()
    // 孤儿子标签路径以有活跃会话为前提（无活跃会话时 Tab 条未挂载，点击走
    // 重建/显式反馈链，见下方「会话记录缺失」族）
    useSessionStore.setState({ activeSessionId: 'sess-host' })
    useAgentTabStore.setState({
      tabs: [makeTab({ id: 'tab-x', pipelineRunId: 'pipe-running' })],
      activeTabId: null,
    })
    renderWithProviders(<PipelineManagerWidget />)
    await clearSessionFollow()
    await screen.findAllByText('运行任务')

    fireEvent.click(rowOpenChatButton('运行任务'))

    await waitFor(() => expect(useAgentTabStore.getState().activeTabId).toBe('tab-x'))
    expect(useAgentTabStore.getState().tabs).toHaveLength(1)
  })

  it('孤儿条目点击按运行状态映射建子标签（6 态）', async () => {
    const cases = [
      ['running', 'running'],
      ['completed', 'completed'],
      ['failed', 'failed'],
      ['suspended', 'waiting_input'],
      ['cancelled', 'waiting_input'],
      ['unknown', 'unknown'],
    ] as const
    for (const [runStatus, tabStatus] of cases) {
      resetPmContainers()
      resetStores()
      // 孤儿子标签路径以有活跃会话为前提（Tab 条随 ChatContainer 挂载）
      useSessionStore.setState({ activeSessionId: 'sess-host' })
      const pid = `st-${runStatus}`
      // 行名 = title（无 title 落「未命名」，短 ID 定位已退役——用户裁定
      // 2026-11-06）；用例标题作定位锚，pipeline ID 只作标签断言坐标
      const title = `孤儿-${runStatus}`
      pmSeed.runs.a = {
        pipeline_id: pid, run_id: `run-${pid}`, status: runStatus, title,
        started_at: '2026-09-01T00:00:00Z',
      }
      // 有活跃会话 → 面板缺省空集全量（不默认只看运行中）；renderPmAllStatuses
      // 同此契约不再点击。上一轮先 unmount——辅助按 name 定位标签按钮，多面板
      // 同存会点中旧面板的筛选
      const { unmount } = renderPmAllStatuses(<PipelineManagerWidget />)
      await clearSessionFollow()
      await screen.findAllByText(title)
      fireEvent.click(rowOpenChatButton(title))

      await waitFor(() =>
        expect(
          useAgentTabStore.getState().tabs.some(
            (t) => t.pipelineRunId === pid && t.status === tabStatus,
          ),
        ).toBe(true),
      )
      unmount()
      resetStores()
    }
  })
})

// ────────────────────────── 详情面板 ──────────────────────────

describe('PipelineManagerWidget 详情面板', () => {
  it('结束时间 / Token 汇总 / Token 实时 行完整呈现（点任务名称手风琴）', async () => {
    seedStandard()
    // 待展开子行（pipe-done，终态）不在缺省「运行中」视图：切到全量视野
    renderPmAllStatuses(<PipelineManagerWidget />)
    await expandTreeChild('运行任务', '完成管道')
    // 方案 B：点任务名称行内展开详情，aria-expanded 承载开合态
    fireEvent.click(rowDetailToggle('完成管道'))
    expect(rowDetailToggle('完成管道')).toHaveAttribute('aria-expanded', 'true')

    expect(await screen.findByText('结束')).toBeInTheDocument()
    expect(screen.getByText('input=10 · output=20 · total=51234')).toBeInTheDocument()
    // token 值两处呈现：条目行 token 列 + 详情「Token 实时」行
    expect(screen.getByText('Token 实时')).toBeInTheDocument()
    expect(screen.getAllByText('51,234').length).toBeGreaterThanOrEqual(2)

    // 再点一次任务名称 → 收起详情（手风琴对称：树操作不动详情，详情自身可关）
    fireEvent.click(rowDetailToggle('完成管道'))
    await waitFor(() => expect(screen.queryByText('Token 实时')).toBeNull())
    expect(rowDetailToggle('完成管道')).toHaveAttribute('aria-expanded', 'false')
  })

  it('state 真值行：错误、已结束=是', async () => {
    seedStandard()
    pmSeed.states.s1 = {
      pipeline_id: 'pipe-running',
      state: { raw_error: 'boom 消息', ended: true },
    }
    renderWithProviders(<PipelineManagerWidget />)
    fireEvent.click(rowDetailToggle('运行任务'))

    expect(await screen.findByText('错误')).toBeInTheDocument()
    expect(screen.getByText('boom 消息')).toBeInTheDocument()
    expect(screen.getByText('已结束')).toBeInTheDocument()
    expect(screen.getByText('是')).toBeInTheDocument()
  })

  it('state 真值行：终局署名渲染「停止原因」（人话对照+原词）', async () => {
    seedStandard()
    pmSeed.states.s1 = {
      pipeline_id: 'pipe-running',
      state: { run_status: 'failed', run_stop_reason: 'empty_reply_exhausted' },
    }
    renderWithProviders(<PipelineManagerWidget />)
    fireEvent.click(rowDetailToggle('运行任务'))

    expect(await screen.findByText('停止原因')).toBeInTheDocument()
    expect(
      screen.getByText('空回复耗尽（LLM 连续无内容回复）（empty_reply_exhausted）'),
    ).toBeInTheDocument()
  })

  it('state 真值行：未署名（空串）不渲染停止原因行', async () => {
    seedStandard()
    pmSeed.states.s1 = {
      pipeline_id: 'pipe-running',
      state: { run_status: 'cancelled', run_stop_reason: '' },
    }
    renderWithProviders(<PipelineManagerWidget />)
    fireEvent.click(rowDetailToggle('运行任务'))

    await screen.findByText('管道 ID')
    expect(screen.queryByText('停止原因')).toBeNull()
  })

  it('任务进度条：正常值按百分比渲染，超界值 clamp 到 100%', async () => {
    seedStandard()
    pmSeed.tasks[0] = { ...pmSeed.tasks[0], progress: { progressPercent: 42 } }
    pmSeed.tasks[2] = { ...pmSeed.tasks[2], progress: { progressPercent: 150 } }
    // meta任务（终态）不在缺省「运行中」视图：切到全量视野
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    fireEvent.click(rowDetailToggle('运行任务'))
    expect(await screen.findByText('42%')).toBeInTheDocument()
    // 进度条 = 百分比文案的兄弟容器内的满高条
    const barWrap = screen.getByText('42%').previousElementSibling as HTMLElement
    expect((barWrap.firstElementChild as HTMLElement).style.width).toBe('42%')

    fireEvent.click(rowDetailToggle('meta任务'))
    expect(await screen.findByText('150%')).toBeInTheDocument()
    const wrap150 = screen.getByText('150%').previousElementSibling as HTMLElement
    expect((wrap150.firstElementChild as HTMLElement).style.width).toBe('100%')
  })

  it.each([
    [{ total: 63456 }, '63,456'],
    [{ total_tokens: 74567 }, '74,567'],
    [{ output: 85678 }, '85,678'],
  ])('total_tokens 形态 %s → token 列展示 %s', async (tokens, expected) => {
    seedStandard()
    pmSeed.runs.r2.total_tokens = tokens
    // 待展开子行（pipe-done，终态）不在缺省「运行中」视图：切到全量视野
    renderPmAllStatuses(<PipelineManagerWidget />)
    await expandTreeChild('运行任务', '完成管道')
    expect((await screen.findAllByText(expected)).length).toBeGreaterThanOrEqual(1)
  })

  it('实时 usage 优先于快照 total_tokens', async () => {
    seedStandard()
    pmSeed.usage.usageByPipeline['pipe-running'] = {
      promptTokens: 1, completionTokens: 2, totalTokens: 999999,
    }
    renderWithProviders(<PipelineManagerWidget />)
    expect((await screen.findAllByText('999,999')).length).toBeGreaterThanOrEqual(1)
  })
})

// ────────────────────────── 行内操作全量（树行） ──────────────────────────

describe('PipelineManagerWidget 行内操作全量', () => {
  it('空数据：空态占位（筛选空态上收面板层，非首载 loading）', async () => {
    renderWithProviders(<PipelineManagerWidget />)
    expect(await screen.findByText('无匹配结果（试试清除筛选或关键词）')).toBeInTheDocument()
  })

  it('行内操作齐备：发消息/⋮停止/删除管道/继续/复制/工作空间', async () => {
    seedStandard()
    seedMetaWorkspaceTask()
    pmSeed.readSessions.mockReturnValue([{ id: 'th-1', title: '会话A' }])
    const writeText = renderWithClipboard()
    await screen.findAllByText('运行任务')

    // 常驻「发消息」：开对话（导航器）
    fireEvent.click(rowOpenChatButton('完成管道'))
    await waitFor(() => expect(pmSeed.navigateToPipeline).toHaveBeenCalledWith('pipe-done', expect.anything()))

    // 「⋮」菜单：复制管道 ID
    await openRowMenu('运行任务')
    fireEvent.click(screen.getByText('复制管道 ID'))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('pipe-running'))

    // 「⋮」菜单：停止（暂停任务）
    await openRowMenu('运行任务')
    fireEvent.click(screen.getByText('停止（暂停任务）'))
    await waitFor(() => expect(pmSeed.pauseTask).toHaveBeenCalledWith('t-run'))

    // 「⋮」菜单：删除管道 → 确认弹窗确认后调 DELETE 任务端点（级联删管道）
    await openRowMenu('运行任务')
    fireEvent.click(screen.getByText('删除管道'))
    fireEvent.click(await screen.findByRole('button', { name: '确认删除' }))
    await waitFor(() => expect(pmSeed.deleteTask).toHaveBeenCalledWith('t-run'))

    // 「⋮」菜单：继续（恢复任务）
    await openRowMenu('挂起任务')
    fireEvent.click(screen.getByText('继续（恢复任务）'))
    await waitFor(() => expect(pmSeed.resumeTask).toHaveBeenCalledWith('t-susp'))

    // 「⋮」菜单：打开工作空间
    await openRowMenu('meta任务')
    fireEvent.click(screen.getByText('打开工作空间'))
    await waitFor(() =>
      expect(
        useLayoutModeStore.getState().workspaceTabs.some((t) => t.dataSource === 'workspace://t-meta'),
      ).toBe(true),
    )
  })

  it('项目行点按无动作（对照：普通条目行点名称切详情、开对话走「发消息」）', async () => {
    seedStandard()
    pmSeed.projects.push({ id: 'proj-l', goal: '列表项目', timestamps: { createdAt: '2026-09-01T00:00:00Z' } })
    // 对照组孤儿子标签路径以有活跃会话为前提
    useSessionStore.setState({ activeSessionId: 'sess-host' })
    // 项目登记行无运行态（a2585be70）：不进状态筛选，仅全量视图可见；
    // 有活跃会话缺省即空集全量（不默认只看运行中），对照组 pipe-orph（挂起）同理
    renderPmAllStatuses(<PipelineManagerWidget />)
    await clearSessionFollow()
    await screen.findByText('列表项目')

    // 项目行：点名称无动作（不建标签、不展开详情——project 分支守卫）
    fireEvent.click(screen.getAllByText('列表项目')[0])
    await waitFor(() => expect(useAgentTabStore.getState().tabs).toHaveLength(0))
    expect(screen.queryByText('管道 ID')).toBeNull()

    // 普通条目行：点任务名称 = 切换详情展开（不开对话、不建标签）
    fireEvent.click(screen.getAllByText('孤儿管道')[0])
    expect(await screen.findByText('管道 ID')).toBeInTheDocument()
    expect(useAgentTabStore.getState().tabs).toHaveLength(0)

    // 普通条目行：开对话走常驻「发消息」按钮（孤儿直建子标签）
    fireEvent.click(rowOpenChatButton('孤儿管道'))
    await waitFor(() =>
      expect(
        useAgentTabStore.getState().tabs.some((t) => t.pipelineRunId === 'pipe-orph'),
      ).toBe(true),
    )
  })
})

// ────────────────────────── 删除弹窗关闭 ──────────────────────────

describe('PipelineManagerWidget 项目删除弹窗关闭', () => {
  async function openDialog() {
    pmSeed.projects.push({ id: 'proj-d', goal: '待删项目', timestamps: { createdAt: '2026-09-01T00:00:00Z' } })
    // 项目登记行无运行态（a2585be70）：不进状态筛选，仅全量视图可见
    renderPmAllStatuses(<PipelineManagerWidget />)
    // 方案 B：项目删除入口在行「⋮」菜单（删除项目 → 确认弹窗）
    await openRowMenu('待删项目')
    fireEvent.click(screen.getByText('删除项目'))
    expect(await screen.findByText(/请选择名下子任务的处置方式/)).toBeTruthy()
  }

  it('取消按钮关闭弹窗且不调删除端点（口径可来回切换）', async () => {
    await openDialog()
    // 口选级联再切回默认口径：radio onChange 双向生效
    fireEvent.click(screen.getByLabelText('连同子任务一起删除（不可恢复）'))
    fireEvent.click(screen.getByLabelText('仅删除项目——名下子任务挂起并保留'))
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    await waitFor(() =>
      expect(screen.queryByText(/请选择名下子任务的处置方式/)).toBeNull(),
    )
    expect(pmSeed.deleteProject).not.toHaveBeenCalled()
  })

  it('Esc 关闭弹窗', async () => {
    await openDialog()
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    await waitFor(() =>
      expect(screen.queryByText(/请选择名下子任务的处置方式/)).toBeNull(),
    )
  })
})

// ────────────────────────── 秒级 ticker ──────────────────────────

describe('PipelineManagerWidget 秒级 ticker', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('有运行中条目时每秒刷新耗时列（fake clock 驱动）', async () => {
    vi.useFakeTimers()
    const startedAt = new Date(Date.now() - 61_000).toISOString()
    pmSeed.runs.a = {
      pipeline_id: 'tick-p', run_id: 'run-tick', status: 'running', started_at: startedAt,
    }
    // query hook 全 mock：数据同步就绪，无需异步等待（fake timers 下 findBy 会挂起）
    renderWithProviders(<PipelineManagerWidget />)
    expect(screen.getByText('1m 1s')).toBeInTheDocument()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(screen.getByText('2m 1s')).toBeInTheDocument()
  })
})

// ────────────────────────── 任务管道 ID 双取 ──────────────────────────

describe('PipelineManagerWidget 任务解析边缘', () => {
  it('仅 metadata.pipelineRunId 的任务照常成行并可打开对话（agentId 落任务 id）', async () => {
    seedStandard()
    // meta任务无 threadId → 孤儿子标签路径，以有活跃会话为前提（Tab 条挂载面）
    useSessionStore.setState({ activeSessionId: 'sess-host' })
    // meta任务（终态）不在缺省「运行中」视图：切到全量视野
    renderPmAllStatuses(<PipelineManagerWidget />)
    await clearSessionFollow()
    expect(await screen.findAllByText('meta任务').then((els) => els.length)).toBeGreaterThanOrEqual(1)

    fireEvent.click(rowOpenChatButton('meta任务'))
    await waitFor(() =>
      expect(
        useAgentTabStore.getState().tabs.some(
          (t) => t.pipelineRunId === 'pipe-meta' && t.agentId === 't-meta',
        ),
      ).toBe(true),
    )
  })
})

// ────────────────────────── 会话记录缺失的打开链（禁静默） ──────────────────────────

describe('PipelineManagerWidget 会话记录缺失的打开链', () => {
  it('无活跃会话 + 会话列表无归属 → 从 thread 重建会话条目并切换打开（不再静默）', async () => {
    // 装机版实测场景（2026-10-13）：用户清空会话数据后点击运行中任务行，
    // 旧实现落孤儿子标签链——Tab 条未挂载 + loadTabMessages 因无 currentSessionId
    // 静默 return，点击零反应。修复后：重建会话条目 + 切换打开两条动作均发生。
    seedStandard()
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')
    fireEvent.click(rowOpenChatButton('运行任务'))

    await waitFor(() =>
      expect(pmSeed.rebuildSessionFromThread).toHaveBeenCalledWith('th-1', {
        title: '运行任务',
        pipelineId: 'pipe-running',
      }),
    )
    await waitFor(() => expect(pmSeed.setActiveSession).toHaveBeenCalledWith('th-1'))
    // 不落孤儿子标签（无活跃会话时该标签无处渲染——正是静默根源）
    expect(useAgentTabStore.getState().tabs).toHaveLength(0)
  })

  it('血缘根会话记录缺失（条目无 threadId）→ 按 originSessionId 重建打开', async () => {
    // 自环子任务管道的 state 独有条目：thread_id 缺席、血缘 origin 指向根会话。
    // 全量视野渲染：该条目无任何运行证据（resolvePipelineRunStatus 落 unknown，
    // 不再进缺省「运行中」视图）——本用例锁的是打开链，不依赖状态推断。
    // 行名 = state.title（无 title 落「未命名」，短 ID 定位已退役——用户裁定
    // 2026-11-06），重建载荷 title 随行名同源。
    pmSeed.states.s1 = {
      pipeline_id: 'lineage-p',
      state: { 'lineage.origin_session_id': 'th-root', title: '血缘任务' },
    }
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('血缘任务')
    fireEvent.click(rowOpenChatButton('血缘任务'))

    await waitFor(() =>
      expect(pmSeed.rebuildSessionFromThread).toHaveBeenCalledWith('th-root', {
        title: '血缘任务',
        pipelineId: 'lineage-p',
      }),
    )
    await waitFor(() => expect(pmSeed.setActiveSession).toHaveBeenCalledWith('th-root'))
  })

  it('重建后打开失败 → 用户可见通知（不静默、不伪装成功）', async () => {
    seedStandard()
    pmSeed.setActiveSession.mockReset().mockRejectedValue(new Error('switch boom'))
    const addNotification = vi
      .spyOn(useNotificationStore.getState(), 'addNotification')
      .mockImplementation(() => {})
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')
    fireEvent.click(rowOpenChatButton('运行任务'))

    await waitFor(() =>
      expect(addNotification).toHaveBeenCalledWith(
        expect.objectContaining({
          title: '无法打开对话',
          message: expect.stringContaining('th-1'),
        }),
      ),
    )
    addNotification.mockRestore()
  })

  it('真孤儿（无会话线索）且无活跃会话 → 显式反馈，不建标签不伪装', async () => {
    seedStandard() // pipe-orph 无 thread_id、无血缘
    const addNotification = vi
      .spyOn(useNotificationStore.getState(), 'addNotification')
      .mockImplementation(() => {})
    // pipe-orph（挂起）不在缺省「运行中」视图：切到全量视野
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('孤儿管道')
    fireEvent.click(rowOpenChatButton('孤儿管道'))

    await waitFor(() =>
      expect(addNotification).toHaveBeenCalledWith(
        expect.objectContaining({
          title: '无法打开对话',
          message: expect.stringContaining('无活跃会话'),
        }),
      ),
    )
    expect(useAgentTabStore.getState().tabs).toHaveLength(0)
    expect(pmSeed.rebuildSessionFromThread).not.toHaveBeenCalled()
    addNotification.mockRestore()
  })
})
